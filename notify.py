"""毎朝の懸賞チェック: 登録したキーワードに合う「新着」を、Make経由でLINEに通知する。
キーワードは、アプリで登録したスプレッドシートから読み込む(未設定なら keywords.txt を使う)。

GitHub Actions から毎日1回実行されます(.github/workflows/daily_notify.yml)。
  - MAKE_WEBHOOK_URL  : MakeのWebhook URL(GitHubのSecretsに登録)
  - GOOGLE_ALERT_FEEDS: GoogleアラートのRSS URL(複数なら改行かカンマ区切り。省略可)
  - KENSHO_SPREADSHEET_ID / GSPREAD_JSON: キーワード保存用スプレッドシートのIDと、サービスアカウントのJSON
  - NOTIFY_TEST=true  : テスト通知だけ送って終了
通知済みの懸賞は seen.json に記録し、同じものは二度通知しません。
巡回した結果は results.json に保存し、アプリ(sweepstakes_app.py)はそれを表示します
(アプリを開くたびにサイトを巡回しないため、表示が速くなります)。
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

import kensho_core as core

KEYWORDS_FILE = "keywords.txt"
SEEN_FILE = "seen.json"
RESULTS_FILE = "results.json"
MAX_RESULTS = 1500    # アプリ表示用に保存する最大件数
MAX_SEEN = 5000        # 通知済みとして覚えておく最大件数
MAX_PER_KEYWORD = 10   # 1つのキーワードあたり、通知に載せる最大件数
CHUNK_LIMIT = 4000     # 1通あたりの最大文字数(LINEの上限5000に余裕を持たせる)
JST = timezone(timedelta(hours=9))


def load_keywords() -> tuple[list[str], str]:
    """(キーワード一覧, 読み込み元) を返す。スプレッドシートが設定されていればそちらを使い、
    無ければ keywords.txt を使う。"""
    sheet_id = os.environ.get("KENSHO_SPREADSHEET_ID", "").strip()
    sa_json = os.environ.get("GSPREAD_JSON", "").strip()
    if sheet_id and sa_json:
        import keyword_store
        kws = keyword_store.load_keywords(json.loads(sa_json), sheet_id)
        print(f"スプレッドシートからキーワードを{len(kws)}件読み込みました")
        return kws, "sheet"
    if not os.path.exists(KEYWORDS_FILE):
        return [], "file"
    out = []
    with open(KEYWORDS_FILE, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                out.append(line)
    print(f"keywords.txt からキーワードを{len(out)}件読み込みました")
    return out, "file"


def load_seen():
    """通知済みURLのリスト。ファイルが無ければ None(=初回実行)。"""
    if not os.path.exists(SEEN_FILE):
        return None
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return list(json.load(f).get("urls", []))
    except Exception:
        return []


def save_seen(urls: list[str]) -> None:
    urls = list(dict.fromkeys(urls))[-MAX_SEEN:]
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now(JST).isoformat(timespec="seconds"), "urls": urls},
                  f, ensure_ascii=False, indent=0)


def save_results(rows: list[dict], logs: list[str]) -> None:
    """巡回結果をアプリ表示用に保存する。"""
    with open(RESULTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.now(JST).isoformat(timespec="seconds"),
                   "rows": rows[:MAX_RESULTS], "logs": logs}, f, ensure_ascii=False)


def send(webhook: str, text: str) -> None:
    # Makeのシナリオが message か text のどちらを読んでも届くように、両方に入れて送る
    r = requests.post(webhook, json={"message": text, "text": text}, timeout=30)
    r.raise_for_status()


def build_blocks(new_by_kw: dict) -> list[str]:
    """キーワードごとの通知文(ブロック)を作る。"""
    blocks = []
    for kw, rows in new_by_kw.items():
        if not rows:
            continue
        lines = [f"■ {kw}({len(rows)}件)"]
        for r in rows[:MAX_PER_KEYWORD]:
            lines.append(f"・{r['タイトル']}")
            if r.get("締切"):
                lines.append(f"  締切 {r['締切']}")
            lines.append(f"  {r['URL']}")
        if len(rows) > MAX_PER_KEYWORD:
            lines.append(f"…ほか{len(rows) - MAX_PER_KEYWORD}件(アプリで確認できます)")
        blocks.append("\n".join(lines)[:CHUNK_LIMIT])
    return blocks


def pack_chunks(header: str, blocks: list[str]) -> list[str]:
    """ブロックを、1通がCHUNK_LIMIT以内に収まるようにまとめる。"""
    chunks, cur = [], header
    for b in blocks:
        if len(cur) + len(b) + 2 > CHUNK_LIMIT and cur != header:
            chunks.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        chunks.append(cur)
    return chunks


def main() -> int:
    webhook = os.environ.get("MAKE_WEBHOOK_URL", "").strip()

    if os.environ.get("NOTIFY_TEST", "").strip().lower() == "true":
        if not webhook:
            print("MAKE_WEBHOOK_URL が設定されていません")
            return 1
        send(webhook, "🎁 懸賞通知のテストです。このメッセージが届けば、設定は成功です。")
        print("テスト通知を送信しました")
        return 0

    # --- 1. 巡回して、結果をアプリ表示用に保存する(通知の設定に不備があっても、ここまでは行う) ---
    feeds = [u.strip() for u in re.split(r"[\n,]", os.environ.get("GOOGLE_ALERT_FEEDS", "")) if u.strip()]
    core.SITES.update(core.make_alert_sites(feeds))

    rows, logs = core.fetch_all(tuple(core.SITES))
    for line in logs:
        print(line)
    if not rows:
        print("懸賞情報を1件も取得できませんでした")
        return 1

    # URLの重複と、サイトをまたいだ同一タイトルの重複を除く
    uniq, urls, titles = [], set(), set()
    for r in rows:
        t = core.normalize(r["タイトル"])
        if r["URL"] in urls or t in titles:
            continue
        urls.add(r["URL"])
        titles.add(t)
        uniq.append(r)
    save_results(uniq, logs)
    print(f"巡回結果{len(uniq)}件を {RESULTS_FILE} に保存しました")

    # --- 2. キーワードに合う新着を通知する ---
    if not webhook:
        print("MAKE_WEBHOOK_URL が設定されていません(巡回結果の保存のみ行いました)")
        return 1
    keywords, source = load_keywords()
    if not keywords:
        if source == "sheet":
            print("キーワードが未登録のため、通知は行いません")
            return 0
        print("keywords.txt にキーワードがありません")
        return 1

    hits = {}
    for kw in keywords:
        words = core.split_words(kw)
        if words:
            hits[kw] = [r for r in uniq if core.match_row(r, words)]
    matched_urls = [r["URL"] for m in hits.values() for r in m]

    seen_list = load_seen()
    today = datetime.now(JST).strftime("%m/%d")

    if seen_list is None:  # 初回: 今ある該当分は「通知済み」として記録し、開始のお知らせだけ送る
        send(webhook, f"🎁 懸賞チェックを開始しました({today})。\n"
                      f"キーワード{len(keywords)}件、現在の該当は{len(set(matched_urls))}件です。"
                      f"これらは通知済みとして記録しました。明日以降、新着だけお知らせします。")
        save_seen(matched_urls)
        print("初回実行: 開始通知を送信しました")
        return 0

    seen = set(seen_list)
    new_by_kw = {kw: [r for r in m if r["URL"] not in seen] for kw, m in hits.items()}
    new_urls = list(dict.fromkeys(r["URL"] for m in new_by_kw.values() for r in m))
    if not new_urls:
        print("新着はありませんでした")
        return 0

    header = f"🎁 懸賞の新着 {len(new_urls)}件({today})"
    chunks = pack_chunks(header, build_blocks(new_by_kw))
    for i, text in enumerate(chunks):
        send(webhook, text)
        if i < len(chunks) - 1:
            time.sleep(1)
    save_seen(seen_list + new_urls)  # 送信に成功してから記録(失敗したら次回また通知される)
    print(f"{len(new_urls)}件を通知しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
