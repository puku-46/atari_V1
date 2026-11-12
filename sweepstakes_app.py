"""懸賞検索アプリ (Streamlit)
 
起動:  streamlit run sweepstakes_app.py
必要:  pip install streamlit requests beautifulsoup4 pandas
 
懸賞まとめサイトの一覧ページを取得し、フリーワードで検索します
(部分一致 / 完全一致、複数語はAND / OR を選択可能)。
※ SITES のCSSセレクタは、実際のサイトのHTMLに合わせて調整してください。
"""
import time
import unicodedata
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
 
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
 
# ------------------------------------------------------------------
# 設定
# ------------------------------------------------------------------
USER_AGENT = "SweepstakesSearchBot/0.1 (personal use)"
REQUEST_INTERVAL_SEC = 2.0  # サイトへの負荷を抑える待ち時間
TIMEOUT_SEC = 15
 
# 取得先サイト。サイトごとにセレクタが違うのでここを編集します。
#   urls     : 一覧ページのURL(複数可)
#   item     : 1件分を囲む要素のCSSセレクタ
#   title    : item内の、タイトル(リンク)要素のCSSセレクタ
#   deadline : item内の締切表示のCSSセレクタ(無ければ None)
SITES = {
    "サンプルサイト(要編集)": {
        "urls": ["https://example.com/kensho/list/"],
        "item": "div.campaign",
        "title": "a",
        "deadline": ".deadline",
    },
}
 
 
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
 
 
def match_title(title: str, words: list[str], mode: str, logic: str) -> bool:
    t = normalize(title)
    if mode == "完全一致":
        hits = [t == w for w in words]
    else:  # 部分一致
        hits = [w in t for w in words]
    return all(hits) if logic == "すべて含む(AND)" else any(hits)
 
 
# ------------------------------------------------------------------
# 取得・解析
# ------------------------------------------------------------------
@st.cache_resource
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
    base = f"{p.scheme}://{p.netloc}"
    return _robots(base).can_fetch(USER_AGENT, url)
 
 
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
        rows.append({
            "サイト": site_name,
            "タイトル": title,
            "締切": deadline,
            "URL": urljoin(base_url, href),
        })
    return rows
 
 
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_site(site_name: str) -> tuple[list[dict], list[str]]:
    """1サイト分を取得。(結果, メッセージ) を返す。1時間キャッシュ。"""
    cfg = SITES[site_name]
    rows, msgs = [], []
    for url in cfg["urls"]:
        if not allowed_by_robots(url):
            msgs.append(f"robots.txtで禁止されているためスキップ: {url}")
            continue
        try:
            res = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SEC)
            res.raise_for_status()
            res.encoding = res.apparent_encoding
            found = parse_page(res.text, url, cfg, site_name)
            msgs.append(f"{url} → {len(found)}件")
            rows.extend(found)
        except Exception as e:
            msgs.append(f"取得失敗 {url}: {e}")
        time.sleep(REQUEST_INTERVAL_SEC)
    return rows, msgs
 
 
# ------------------------------------------------------------------
# 画面
# ------------------------------------------------------------------
st.set_page_config(page_title="懸賞検索", page_icon="🎁", layout="wide")
st.title("🎁 懸賞検索")
 
with st.sidebar:
    st.header("検索条件")
    query = st.text_input("フリーワード", placeholder="例: 旅行 ペア  /  PS5")
    st.caption("複数の語はスペースまたはカンマで区切ります。")
    mode = st.radio("一致方法", ["部分一致", "完全一致"], horizontal=True,
                    help="部分一致: タイトルに含まれていればOK / 完全一致: タイトルが入力と完全に同じ")
    logic = st.radio("複数語の条件", ["すべて含む(AND)", "どれか含む(OR)"],
                     help="フリーワードを複数入れたときの組み合わせ方")
    sites = st.multiselect("取得するサイト", list(SITES), default=list(SITES))
    if st.button("最新情報を再取得"):
        fetch_site.clear()
 
all_rows, logs = [], []
with st.spinner("懸賞情報を取得中..."):
    for s in sites:
        r, m = fetch_site(s)
        all_rows += r
        logs += m
 
df = pd.DataFrame(all_rows)
 
if df.empty:
    st.warning("懸賞情報を取得できませんでした。下の取得ログを確認し、SITES のURL/セレクタを調整してください。")
else:
    df = df.drop_duplicates(subset="URL")
    total = len(df)
    words = split_words(query)
    if words:
        mask = df["タイトル"].apply(lambda t: match_title(t, words, mode, logic))
        df = df[mask]
        st.caption(f"{total} 件中 {len(df)} 件が一致")
    else:
        st.caption(f"{total} 件(フリーワードを入力すると絞り込めます)")
 
    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={"URL": st.column_config.LinkColumn("応募ページ", display_text="開く")},
    )
    st.download_button(
        "CSVでダウンロード",
        df.to_csv(index=False).encode("utf-8-sig"),
        file_name="kensho.csv",
        mime="text/csv",
    )
 
with st.expander("取得ログ"):
    for line in logs:
        st.write(line)
 
