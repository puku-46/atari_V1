"""懸賞検索アプリ (Streamlit)

起動:  streamlit run sweepstakes_app.py
必要:  pip install streamlit requests beautifulsoup4 pandas
       (同じフォルダに kensho_core.py が必要です)

複数の懸賞サイトを巡回して情報を集め、フリーワードで検索します。
ヒットした懸賞は、サイトごとの一覧(リンク付き)で表示します。
巡回先サイトの設定は kensho_core.py の SITES にあります。
GoogleアラートのRSS URLは、Streamlitの Secrets の google_alert_feeds に入れます。
"""
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
