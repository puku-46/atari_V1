"""懸賞検索アプリ (Streamlit)

起動:  streamlit run sweepstakes_app.py
必要:  pip install streamlit requests beautifulsoup4 pandas

複数の懸賞サイトを巡回して情報を集め、フリーワードで検索します。
ヒットした懸賞は、サイトごとの一覧(リンク付き)で表示します。
取得方法は2通り:
  1. RSS/Atomフィード(推奨): サイトのトップページURLを書くだけ。フィードは自動検出。
  2. HTML解析: RSSが無いサイト用。CSSセレクタの設定が必要。
サイトを増やすには SITES に1項目追加するだけです。
"""
import html as htmllib
import re
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate_to_datetime
from functools import lru_cache
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup

# ------------------------------------------------------------------
# 設定
# ------------------------------------------------------------------
USER_AGENT = "SweepstakesSearchBot/0.3 (personal use)"
REQUEST_INTERVAL_SEC = 2.0  # 同じサイトへの連続アクセスの間隔
TIMEOUT_SEC = 15
MAX_FEEDS_PER_PAGE = 3  # 1ページから自動検出するフィードの最大数
SUMMARY_LEN = 140  # 一覧に表示する概要の最大文字数

# 取得先サイト一覧。ここに追加していきます。
#
# 【RSS/Atom方式】 type="auto"
#   urls       : サイトのトップページ(またはフィードのURLそのもの)。フィードは自動検出します。
#   feed_pages : (省略可) WordPress系サイトで、古い分も何ページ分か取得する(?paged=2,3...)。
#                RSSには最新の数件しか入っていないことが多いため。省略すると1ページのみ。
#   ignore_robots : (省略可) True にすると robots.txt の確認を省く。自分専用のGoogleアラートRSS用。
#
# 【HTML解析方式】 type="html"  ※RSSが無いサイト用
#   urls     : 一覧ページのURL(複数可)
#   item     : 1件分を囲む要素のCSSセレクタ
#   title    : item内の、タイトル(リンク)要素のCSSセレクタ
#   deadline : item内の締切表示のCSSセレクタ(無ければ None)
SITES = {
    # 懸賞CLUB: WordPress製。フィードには最新5件しか入っていないため、8ページ分(約40件)を取得
    "懸賞CLUB": {
        "type": "auto",
        "urls": ["https://kenshou.club/"],
        "feed_pages": 8,
    },
    # Lucky Dip!: RSSがあるか未確認。取得ログで結果を確認してください
    "Lucky Dip!": {
        "type": "auto",
        "urls": ["https://lucky.jp/"],
    },
    # ※ 懸賞生活(knshow.com)は robots.txt で自動アクセスが禁止されていたため入れていません。
    # ※ 懸賞ナビ(kenshonavi.com)も自動アクセス禁止の様子だったため入れていません。
    #
    # HTML解析方式の書き方の例(コメントを外して編集して使う)
    # "サイトB": {
    #     "type": "html",
    #     "urls": ["https://example.com/kensho/list/"],
    #     "item": "div.campaign",
    #     "title": "a",
    #     "deadline": ".deadline",
    # },
}


def _load_alert_sites() -> dict:
    """GoogleアラートのRSS URLを、Streamlitの Secrets(google_alert_feeds)から読み込む。
    URLは本人専用なので、公開されるコードには直接書かず Secrets に置きます。"""
    try:
        urls = st.secrets.get("google_alert_feeds", [])
        if isinstance(urls, str):
            urls = [urls]
        urls = [str(u).strip() for u in urls if str(u).strip()]
    except Exception:
        return {}
    return {f"Googleアラート{i}": {"type": "auto", "urls": [u], "ignore_robots": True}
            for i, u in enumerate(urls, 1)}


SITES.update(_load_alert_sites())


# ------------------------------------------------------------------
# 検索ロジック
# ------------------------------------------------------------------
def normalize(text: str) -> str:
    """全角/半角・大文字/小文字の違いを吸収する(例: ＰＳ５ と PS5 を同じ扱いに)。"""
    return unicodedata.normalize("NFKC", text).casefold().strip()


def split_words(query: str) -> list[str]:
    """スペース(全角含む)・カンマ区切りで複数語に分割。"""
    q = normalize(query).replace(",", " ").replace("、", " ")
    return [w for w in q.split() if w]


def match_row(row: dict, words: list[str]) -> bool:
    """タイトル+概要に、入力した語がすべて含まれていればヒット(部分一致)。"""
    hay = normalize(f"{row['タイトル']} {row.get('概要', '')}")
    return all(w in hay for w in words)


# ------------------------------------------------------------------
# 取得・解析
# ------------------------------------------------------------------
@lru_cache(maxsize=64)
def _robots(base: str) -> RobotFileParser:
    rp = RobotFileParser()
    rp.set_url(urljoin(base, "/robots.txt"))
    try:
        rp.read()
    except Exception:
        pass  # 取得できない場合は can_fetch が許可扱いになる
    return rp


def allowed_by_robots(url: str) -> bool:
    p = urlparse(url)
    return _robots(f"{p.scheme}://{p.netloc}").can_fetch(USER_AGENT, url)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _fmt_date(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return parsedate_to_datetime(raw).strftime("%Y-%m-%d")
    except Exception:
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except Exception:
        return raw[:10]


def clean_summary(raw_html: str) -> str:
    """概要(HTML)から文字だけを取り出し、定型文を除いて短くする。"""
    if not raw_html:
        return ""
    raw_html = re.sub(r"</p>|<br\s*/?>", " ", raw_html, flags=re.I)  # 段落・改行は空白に
    text = BeautifulSoup(raw_html, "html.parser").get_text("")  # <b>などの装飾タグは詰める
    text = re.sub(r"\s*The post .* first appeared on .*$", "", text, flags=re.S)
    text = text.replace("[…]", "…").replace("[&#8230;]", "…")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= SUMMARY_LEN else text[:SUMMARY_LEN] + "…"


def clean_text(text: str) -> str:
    """タイトルに混ざるHTMLタグ(<b>など)や文字参照を取り除く。"""
    if "<" in text or "&" in text:
        text = BeautifulSoup(text, "html.parser").get_text("")
    return re.sub(r"\s+", " ", text).strip()


def unwrap_redirect(url: str) -> str:
    """Googleアラートの転送用URL(google.com/url?...&url=本当のURL)を、本当のURLに戻す。"""
    try:
        p = urlparse(url)
        if p.netloc.endswith("google.com") and p.path == "/url":
            q = parse_qs(p.query)
            for key in ("url", "q"):
                if q.get(key):
                    return q[key][0]
    except Exception:
        pass
    return url


def make_row(site: str, title: str, date: str, summary_html: str, url: str, deadline: str = "") -> dict:
    return {"サイト": site, "タイトル": clean_text(title), "掲載日": date, "締切": deadline,
            "概要": clean_summary(summary_html), "URL": unwrap_redirect(url)}


def looks_like_feed(content: bytes) -> bool:
    head = content[:500].lower()
    return any(m in head for m in (b"<rss", b"<feed", b"<rdf:rdf"))


def parse_feed_strict(content: bytes, site_name: str) -> list[dict]:
    """RSS 2.0 / RSS 1.0(RDF) / Atom に対応(XMLとして正しいフィード用)。"""
    root = ET.fromstring(content)
    rows = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title = link = date = summary = body = ""
        for ch in el:
            name = _local(ch.tag)
            if name == "title":
                title = (ch.text or "").strip()
            elif name == "link" and not link:
                link = (ch.get("href") or ch.text or "").strip()
            elif name in ("pubDate", "published", "updated", "date") and not date:
                date = _fmt_date(ch.text or "")
            elif name in ("description", "summary") and not summary:
                summary = ch.text or ""
            elif name in ("encoded", "content") and not body:
                body = ch.text or ""
        if title and link:
            rows.append(make_row(site_name, title, date, summary or body, link))
    return rows


def _decode_bytes(b: bytes) -> str:
    m = re.search(rb"encoding=[\"']([\w\-]+)[\"']", b[:200])
    encs = ([m.group(1).decode("ascii", "ignore")] if m else []) + ["utf-8", "cp932"]
    for enc in encs:
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("utf-8", errors="replace")


def _rx_tag(block: str, name: str) -> str:
    m = re.search(rf"<{name}\b[^>]*>(.*?)</{name}>", block, re.S | re.I)
    if not m:
        return ""
    v = m.group(1).strip()
    cm = re.fullmatch(r"<!\[CDATA\[(.*)\]\]>", v, re.S)
    return (cm.group(1) if cm else htmllib.unescape(v)).strip()


def parse_feed_lenient(content: bytes, site_name: str) -> list[dict]:
    """XMLとして壊れているフィード用の、ゆるい読み取り(正規表現)。"""
    text = _decode_bytes(content)
    rows = []
    for block in re.findall(r"<(?:item|entry)\b.*?</(?:item|entry)>", text, flags=re.S | re.I):
        title = _rx_tag(block, "title")
        hm = re.search(r"<link\b[^>]*\bhref=[\"']([^\"']+)[\"']", block, re.I)
        link = htmllib.unescape(hm.group(1)) if hm else _rx_tag(block, "link")
        date = _fmt_date(_rx_tag(block, "pubDate") or _rx_tag(block, "published")
                         or _rx_tag(block, "updated") or _rx_tag(block, "dc:date"))
        summary = _rx_tag(block, "description") or _rx_tag(block, "summary")
        if title and link:
            rows.append(make_row(site_name, title, date, summary, link))
    return rows


def parse_feed(content: bytes, site_name: str) -> list[dict]:
    try:
        return parse_feed_strict(content, site_name)
    except Exception:  # XMLの崩れ・Shift_JIS宣言など、厳密な読み取りが失敗した場合
        return parse_feed_lenient(content, site_name)


def discover_feeds(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for tag in soup.find_all("link", attrs={"rel": "alternate"}):
        t = (tag.get("type") or "").lower()
        href = tag.get("href")
        if not href or "comment" in href.lower():  # コメント用フィードは除外
            continue
        if "rss" in t or "atom" in t or "xml" in t:
            u = urljoin(base_url, href)
            if u not in urls:
                urls.append(u)
    return urls[:MAX_FEEDS_PER_PAGE]


def parse_page(html: str, base_url: str, cfg: dict, site_name: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = []
    for el in soup.select(cfg["item"]):
        a = el.select_one(cfg["title"])
        if not a:
            continue
        title = a.get_text(" ", strip=True)
        href = a.get("href")
        if not title or not href:
            continue
        deadline = ""
        if cfg.get("deadline"):
            d = el.select_one(cfg["deadline"])
            deadline = d.get_text(" ", strip=True) if d else ""
        rows.append(make_row(site_name, title, "", "", urljoin(base_url, href), deadline))
    return rows


def _get(url: str) -> requests.Response:
    res = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SEC)
    res.raise_for_status()
    return res


def fetch_site_impl(site_name: str) -> tuple[list[dict], list[str]]:
    """1サイト分を取得。(結果, ログ) を返す。"""
    cfg = SITES[site_name]
    rows, msgs = [], []
    pages = max(1, int(cfg.get("feed_pages", 1)))

    def polite_get(url):
        if not cfg.get("ignore_robots") and not allowed_by_robots(url):
            msgs.append(f"robots.txtで禁止されているためスキップ: {url}")
            return None
        try:
            res = _get(url)
        except Exception as e:
            msgs.append(f"取得失敗 {url}: {e}")
            return None
        time.sleep(REQUEST_INTERVAL_SEC)
        return res

    def read_feed(feed_url, first_res=None):
        """フィードを読む。feed_pages が2以上なら ?paged=N で古い分も読む。"""
        out, seen = [], set()
        for n in range(1, pages + 1):
            u = feed_url if n == 1 else feed_url + ("&" if "?" in feed_url else "?") + f"paged={n}"
            res = first_res if (n == 1 and first_res is not None) else polite_get(u)
            if res is None:
                break
            try:
                found = parse_feed(res.content, site_name)
            except Exception as e:
                msgs.append(f"フィード解析失敗 {u}: {e}")
                break
            fresh = [r for r in found if r["URL"] not in seen]
            if not fresh:  # 空、またはページ指定が効かず同じ内容が返ってきた
                break
            seen.update(r["URL"] for r in fresh)
            out += fresh
        note = "" if out else "(フィード形式ではない可能性があります)"
        msgs.append(f"{feed_url} → {len(out)}件(フィード){note}")
        return out

    for url in cfg["urls"]:
        res = polite_get(url)
        if res is None:
            continue
        try:
            if cfg.get("type", "html") == "html":
                res.encoding = res.apparent_encoding
                found = parse_page(res.text, url, cfg, site_name)
                msgs.append(f"{url} → {len(found)}件(HTML解析)")
                rows += found
            elif looks_like_feed(res.content):  # URL自体がフィード
                rows += read_feed(url, first_res=res)
            else:  # ページからフィードを自動検出
                res.encoding = res.apparent_encoding
                feeds = discover_feeds(res.text, url)
                if not feeds:
                    msgs.append(f"{url} → RSS/Atomが見つかりません(type='html'での設定が必要です)")
                for f in feeds:
                    rows += read_feed(f)
        except Exception as e:
            msgs.append(f"解析失敗 {url}: {e}")
    return rows, msgs


def _safe_fetch(site_name: str) -> tuple[list[dict], list[str]]:
    try:
        return fetch_site_impl(site_name)
    except Exception as e:  # 1サイトの失敗で全体を止めない
        return [], [f"想定外のエラー: {e}"]


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_all(site_names: tuple[str, ...]) -> tuple[list[dict], list[str]]:
    """複数サイトを並列で取得(サイト間は並列、同一サイト内は順番)。1時間キャッシュ。"""
    rows, logs = [], []
    if not site_names:
        return rows, logs
    with ThreadPoolExecutor(max_workers=min(8, len(site_names))) as ex:
        for name, (r, m) in zip(site_names, ex.map(_safe_fetch, site_names)):
            rows += r
            logs += [f"[{name}] {line}" for line in m]
    return rows, logs


# ------------------------------------------------------------------
# 表示
# ------------------------------------------------------------------
def md_escape(text: str) -> str:
    """Markdownとして誤表示される記号をエスケープ。"""
    for ch in ["\\", "[", "]", "*", "_", "`", "<", ">", "$", "~"]:
        text = text.replace(ch, "\\" + ch)
    return text


def safe_url(url: str) -> str:
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def render_results(df: pd.DataFrame):
    """ヒットしたサイトだけを、サイトごとの一覧で表示する。"""
    for site in SITES:
        g = df[df["サイト"] == site]
        if g.empty:
            continue  # ヒットしなかったサイトは表示しない
        st.subheader(f"{site}　{len(g)}件")
        for _, r in g.iterrows():
            with st.container(border=True):
                url = safe_url(r["URL"])
                st.markdown(f"**[{md_escape(r['タイトル'])}]({url})**")
                meta = []
                if r["掲載日"]:
                    meta.append(f"掲載 {r['掲載日']}")
                if r["締切"]:
                    meta.append(f"締切 {r['締切']}")
                if meta:
                    st.caption(" ・ ".join(meta))
                if r["概要"]:
                    st.markdown(md_escape(r["概要"]))
                st.markdown(f"[🔗 応募ページを開く]({url})")


def main():
    st.set_page_config(page_title="懸賞検索", page_icon="🎁", layout="wide")
    st.title("🎁 懸賞検索")

    c1, c2 = st.columns([5, 1])
    with c1:
        query = st.text_input("フリーワード", label_visibility="collapsed",
                              placeholder="フリーワードを入力(例: 旅行 ペア / PS5 / 国際園芸博覧会)")
    with c2:
        if st.button("最新情報を再取得"):
            fetch_all.clear()
    st.caption("複数の語はスペースで区切ると、すべて含む懸賞を探します。タイトルと概要が対象です。")

    names = tuple(SITES)
    with st.spinner(f"{len(names)} サイトを巡回中..."):
        all_rows, logs = fetch_all(names)

    df = pd.DataFrame(all_rows)
    if df.empty:
        st.warning("懸賞情報を取得できませんでした。下の取得ログを確認してください。")
    else:
        # URL重複と、サイトをまたいだ同一タイトルの重複を除外
        df = df.drop_duplicates(subset="URL")
        df = df.assign(_key=df["タイトル"].map(normalize)).drop_duplicates(subset="_key").drop(columns="_key")
        total = len(df)
        words = split_words(query)
        if words:
            hits = df[df.apply(lambda r: match_row(r.to_dict(), words), axis=1)]
        else:
            hits = df
        hits = hits.sort_values("掲載日", ascending=False)

        if words:
            if hits.empty:
                st.info(f"「{query}」に一致する懸賞は見つかりませんでした。"
                        f"({len(names)}サイト・{total}件を検索)")
            else:
                st.success(f"「{query}」に一致: {len(hits)}件 / {total}件中")
        else:
            st.caption(f"{total}件(フリーワードを入力すると絞り込めます)")

        render_results(hits)

        if not hits.empty:
            st.download_button(
                "この一覧をCSVでダウンロード",
                hits.to_csv(index=False).encode("utf-8-sig"),
                file_name="kensho.csv",
                mime="text/csv",
            )

    with st.expander("取得ログ(サイトごとの取得結果)", expanded=df.empty):
        for line in logs:
            st.write(line)


if __name__ == "__main__":
    main()
