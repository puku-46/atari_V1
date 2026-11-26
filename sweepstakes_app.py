"""懸賞検索アプリ (Streamlit)

起動:  streamlit run sweepstakes_app.py
必要:  pip install streamlit requests beautifulsoup4 pandas gspread
       (同じフォルダに kensho_core.py と keyword_store.py が必要です)

「検索」タブ: 毎朝の自動収集(GitHub Actions)が保存した結果(results.json)を、フリーワードで検索して表示。
           アプリを開くたびにサイトを巡回することはありません(「今すぐ最新情報を取得」を押したときだけ巡回)。
「通知キーワード」タブ: 毎朝のLINE通知に使うキーワードを登録(Googleスプレッドシートに保存)。
巡回先サイトの設定は kensho_core.py の SITES にあります。
Streamlitの Secrets に入れるもの:
  google_alert_feeds   : GoogleアラートのRSS URL(リスト)
  KENSHO_SPREADSHEET_ID: キーワード保存用スプレッドシートのID
  [gspread] json_secrets(または [connections.gsheets]): サービスアカウントの情報
"""
import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

import kensho_core as core
from kensho_core import SITES, make_alert_sites, match_row, normalize, split_words


def _alert_urls_from_secrets() -> list:
    try:
        urls = st.secrets.get("google_alert_feeds", [])
        return [urls] if isinstance(urls, str) else list(urls)
    except Exception:
        return []


SITES.update(make_alert_sites(_alert_urls_from_secrets()))


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_all(site_names: tuple[str, ...]) -> tuple[list[dict], list[str]]:
    """巡回結果を1時間キャッシュする。"""
    return core.fetch_all(site_names)


RESULTS_FILE = Path(__file__).with_name("results.json")


@st.cache_data(show_spinner=False)
def _read_saved(path: str, mtime: float):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return None


def load_saved():
    """自動収集が保存した結果(results.json)を読む。まだ無ければ None。"""
    try:
        mtime = RESULTS_FILE.stat().st_mtime
    except OSError:
        return None
    return _read_saved(str(RESULTS_FILE), mtime)


def _fmt_updated(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%m/%d %H:%M")
    except Exception:
        return iso or "不明"


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
    order = list(SITES) + [x for x in df["サイト"].unique() if x not in SITES]
    for site in order:
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


def load_data():
    """表示するデータを返す。通常は自動収集の保存結果。「今すぐ取得」を押した後だけ、その場で巡回する。
    戻り値: (一覧DataFrame, ログ, データの説明)。保存結果がまだ無いときは説明が None。"""
    if st.session_state.get("use_live"):
        names = tuple(SITES)
        with st.spinner(f"{len(names)} サイトを巡回中...(数十秒かかります)"):
            rows, logs = fetch_all(names)
        label = "いま取得した結果(このページの表示中のみ)"
    else:
        saved = load_saved()
        if saved is None:
            return pd.DataFrame(), [], None
        rows, logs = saved.get("rows", []), saved.get("logs", [])
        label = f"自動収集の結果(最終更新 {_fmt_updated(saved.get('updated', ''))})"

    df = pd.DataFrame(rows)
    if not df.empty:
        # URL重複と、サイトをまたいだ同一タイトルの重複を除外
        df = df.drop_duplicates(subset="URL")
        df = df.assign(_key=df["タイトル"].map(normalize)).drop_duplicates(subset="_key").drop(columns="_key")
    return df, logs, label


def search_tab(df: pd.DataFrame):
    query = st.text_input("フリーワード", label_visibility="collapsed",
                          placeholder="フリーワードを入力(例: 旅行 ペア / PS5 / 国際園芸博覧会)")
    st.caption("複数の語はスペースで区切ると、すべて含む懸賞を探します。タイトルと概要が対象です。")

    if df.empty:
        st.caption("表示できるデータがありません。")
        return
    total = len(df)
    words = split_words(query)
    if words:
        hits = df[df.apply(lambda r: match_row(r.to_dict(), words), axis=1)]
    else:
        hits = df
    hits = hits.sort_values("掲載日", ascending=False)

    if words:
        if hits.empty:
            st.info(f"「{query}」に一致する懸賞は見つかりませんでした。({df['サイト'].nunique()}サイト・{total}件を検索)")
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


def _service_account_info() -> dict:
    """サービスアカウント情報を Secrets から取り出す。次の2通りの書き方に対応:
      [gspread] json_secrets = '''{...JSON...}'''
      [connections.gsheets]  type = "service_account" ...(他のアプリと同じ形)"""
    sec = st.secrets
    if "gspread" in sec and "json_secrets" in sec["gspread"]:
        raw = sec["gspread"]["json_secrets"]
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    info = dict(sec["connections"]["gsheets"])
    info.pop("spreadsheet", None)  # 他のアプリ用のスプレッドシートURL。このアプリでは使いません
    return info


def _sheet_config():
    """(サービスアカウント情報, スプレッドシートID) を返す。未設定なら None。"""
    try:
        sheet_id = str(st.secrets.get("KENSHO_SPREADSHEET_ID", "")).strip()
        info = _service_account_info()
    except Exception:
        return None
    return (info, sheet_id) if sheet_id and info.get("private_key") else None


def keyword_tab(df: pd.DataFrame):
    st.subheader("🔔 通知キーワード")
    st.caption("登録した言葉に合う新着の懸賞を、毎朝8時ごろにLINEでお知らせします。"
               "スペースで区切ると「すべて含む」懸賞だけが対象です(例: 旅行 ペア)。")

    cfg = _sheet_config()
    if cfg is None:
        st.warning("キーワードの保存先(Googleスプレッドシート)が未設定です。"
                   "StreamlitのSecretsに KENSHO_SPREADSHEET_ID と、サービスアカウントの情報([gspread] json_secrets など)を登録してください。")
        return
    try:
        import keyword_store
    except ImportError:
        st.error("gspread が入っていません。requirements.txt に gspread を追加してください。")
        return
    info, sheet_id = cfg

    msg = st.session_state.pop("kw_msg", None)
    if msg:
        st.success(msg)

    if "kw_saved" not in st.session_state:
        try:
            st.session_state["kw_saved"] = keyword_store.load_keywords(info, sheet_id)
            st.session_state.setdefault("kw_ver", 0)
        except Exception as e:
            st.error(f"キーワードを読み込めませんでした。スプレッドシートの共有設定とIDを確認してください。({e})")
            return
    saved = st.session_state["kw_saved"]

    st.caption("下の表で、追加・書き換え・削除ができます(表の末尾の空行に入力すると追加、行を選んで Delete キーで削除)。"
               "編集したら「保存する」を押してください。")
    edited = st.data_editor(
        pd.DataFrame({"キーワード": pd.Series(saved, dtype="object")}),
        num_rows="dynamic", hide_index=True, use_container_width=True,
        key=f"kw_editor_{st.session_state['kw_ver']}",
    )
    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button("💾 保存する", type="primary"):
            try:
                new = keyword_store.save_keywords(info, sheet_id, edited["キーワード"].tolist())
            except Exception as e:
                st.error(f"保存できませんでした: {e}")
            else:
                st.session_state["kw_saved"] = new
                st.session_state["kw_ver"] += 1  # 表を保存後の内容で作り直す
                st.session_state["kw_msg"] = f"{len(new)}件のキーワードを保存しました。次回(明朝8時ごろ)の通知から反映されます。"
                st.rerun()
    with c2:
        if st.button("スプレッドシートから読み込み直す"):
            st.session_state.pop("kw_saved", None)
            st.rerun()

    if saved and not df.empty:
        rows = []
        for k in saved:
            words = split_words(k)
            n = int(df.apply(lambda r: match_row(r.to_dict(), words), axis=1).sum()) if words else 0
            rows.append({"キーワード": k, "いまの該当件数": n})
        st.markdown("**いまの該当件数**(今回の巡回結果から)")
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
        st.caption("新しく追加したキーワードは、すでに公開中の該当分も含めて、次回の通知にまとめて届きます。")


def main():
    st.set_page_config(page_title="懸賞検索", page_icon="🎁", layout="wide")
    st.title("🎁 懸賞検索")
    if st.button("🔄 今すぐ最新情報を取得", help="サイトを巡回し直します(数十秒かかります)。通常は自動収集の結果が表示されます。"):
        fetch_all.clear()
        st.session_state["use_live"] = True

    df, logs, label = load_data()
    if label:
        st.caption(f"📌 {label}。自動収集は毎朝8時ごろに行われます。")
    else:
        st.info("まだ自動収集の結果がありません。GitHubの「Actions」→「daily-notify」→「Run workflow」を一度実行するか、"
                "上のボタンで取得してください。")

    tab_search, tab_kw = st.tabs(["🔎 検索", "🔔 通知キーワード"])
    with tab_search:
        search_tab(df)
    with tab_kw:
        keyword_tab(df)

    with st.expander("取得ログ(サイトごとの取得結果)", expanded=bool(label) and df.empty):
        for line in logs:
            st.write(line)


if __name__ == "__main__":
    main()
