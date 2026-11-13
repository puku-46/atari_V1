"""懸賞検索アプリ (Streamlit)
 
起動:  streamlit run sweepstakes_app.py
必要:  pip install streamlit requests beautifulsoup4 pandas
 
複数の懸賞サイトを巡回して情報を集め、フリーワードで検索します。
取得方法は2通り:
  1. RSS/Atomフィード(推奨): サイトのトップページURLを書くだけ。フィードは自動検出。
  2. HTML解析: RSSが無いサイト用。CSSセレクタの設定が必要。
サイトを増やすには SITES に1項目追加するだけです。
"""
import time
import unicodedata
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.utils import parsedate_to_datetime
from functools import lru_cache
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
 
import pandas as pd
import requests
import streamlit as st
from bs4 import BeautifulSoup
 
# ------------------------------------------------------------------
# 設定
# ------------------------------------------------------------------
USER_AGENT = "SweepstakesSearchBot/0.2 (personal use)"
REQUEST_INTERVAL_SEC = 2.0  # 同じサイトへの連続アクセスの間隔
TIMEOUT_SEC = 15
MAX_FEEDS_PER_PAGE = 3  # 1ページから自動検出するフィードの最大数
 
# 取得先サイト一覧。ここに追加していきます。
#
# 【RSS/Atom方式】 type="auto"
#   urls : サイトのトップページ(またはフィードのURLそのもの)。フィードは自動検出します。
#
# 【HTML解析方式】 type="html"  ※RSSが無いサイト用
#   urls     : 一覧ページのURL(複数可)
#   item     : 1件分を囲む要素のCSSセレクタ
#   title    : item内の、タイトル(リンク)要素のCSSセレクタ
#   deadline : item内の締切表示のCSSセレクタ(無ければ None)
SITES = {
    # 例: 懸賞のつぼ(RSS配信の案内あり。現在も動いているかは取得ログで確認してください)
    "懸賞のつぼ(例・要確認)": {
        "type": "auto",
        "urls": ["https://www.tubox.com/"],
    },
    # HTML解析方式の書き方の例(コメントを外して編集して使う)
    # "サイトB": {
    #     "type": "html",
    #     "urls": ["https://example.com/kensho/list/"],
    #     "item": "div.campaign",
    #     "title": "a",
    #     "deadline": ".deadline",
    # },
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
 
 
def looks_like_feed(content: bytes) -> bool:
    head = content[:500].lower()
    return any(m in head for m in (b"<rss", b"<feed", b"<rdf:rdf"))
 
 
def parse_feed(content: bytes, site_name: str) -> list[dict]:
    """RSS 2.0 / RSS 1.0(RDF) / Atom に対応。"""
    root = ET.fromstring(content)
    rows = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title = link = date = ""
        for ch in el:
            name = _local(ch.tag)
            if name == "title":
                title = (ch.text or "").strip()
            elif name == "link" and not link:
                link = (ch.get("href") or ch.text or "").strip()
            elif name in ("pubDate", "published", "updated", "date") and not date:
                date = _fmt_date(ch.text or "")
        if title and link:
            rows.append({"サイト": site_name, "タイトル": title, "掲載日": date, "締切": "", "URL": link})
    return rows
 
 
def discover_feeds(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls = []
    for tag in soup.find_all("link", attrs={"rel": "alternate"}):
        t = (tag.get("type") or "").lower()
        if tag.get("href") and ("rss" in t or "atom" in t or "xml" in t):
            urls.append(urljoin(base_url, tag["href"]))
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
        rows.append({"サイト": site_name, "タイトル": title, "掲載日": "",
                     "締切": deadline, "URL": urljoin(base_url, href)})
    return rows
 
 
def _get(url: str) -> requests.Response:
    res = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SEC)
    res.raise_for_status()
    return res
 
 
def fetch_site_impl(site_name: str) -> tuple[list[dict], list[str]]:
    """1サイト分を取得。(結果, ログ) を返す。"""
    cfg = SITES[site_name]
    rows, msgs = [], []
 
    def polite_get(url):
        if not allowed_by_robots(url):
            msgs.append(f"robots.txtで禁止されているためスキップ: {url}")
            return None
        try:
            res = _get(url)
        except Exception as e:
            msgs.append(f"取得失敗 {url}: {e}")
            return None
        time.sleep(REQUEST_INTERVAL_SEC)
        return res
 
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
                found = parse_feed(res.content, site_name)
                msgs.append(f"{url} → {len(found)}件(フィード)")
                rows += found
            else:  # ページからフィードを自動検出
                res.encoding = res.apparent_encoding
                feeds = discover_feeds(res.text, url)
                if not feeds:
                    msgs.append(f"{url} → RSS/Atomが見つかりません(type='html'での設定が必要です)")
                for f in feeds:
                    fres = polite_get(f)
                    if fres is None:
                        continue
                    found = parse_feed(fres.content, site_name)
                    msgs.append(f"{f} → {len(found)}件(自動検出フィード)")
                    rows += found
        except Exception as e:
            msgs.append(f"解析失敗 {url}: {e}")
    return rows, msgs
 
 
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_all(site_names: tuple[str, ...]) -> tuple[list[dict], list[str]]:
    """複数サイトを並列で取得(サイト間は並列、同一サイト内は順番)。1時間キャッシュ。"""
    rows, logs = [], []
    if not site_names:
        return rows, logs
    with ThreadPoolExecutor(max_workers=min(8, len(site_names))) as ex:
        for name, (r, m) in zip(site_names, ex.map(fetch_site_impl, site_names)):
            rows += r
            logs += [f"[{name}] {line}" for line in m]
    return rows, logs
 
 
# ------------------------------------------------------------------
# 画面
# ------------------------------------------------------------------
def main():
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
        sites = st.multiselect("巡回するサイト", list(SITES), default=list(SITES))
        if st.button("最新情報を再取得"):
            fetch_all.clear()
 
    with st.spinner(f"{len(sites)} サイトを巡回中..."):
        all_rows, logs = fetch_all(tuple(sites))
 
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
            mask = df["タイトル"].apply(lambda t: match_title(t, words, mode, logic))
            df = df[mask]
            st.caption(f"{len(sites)} サイト・{total} 件中 {len(df)} 件が一致")
        else:
            st.caption(f"{len(sites)} サイト・{total} 件(フリーワードを入力すると絞り込めます)")
 
        df = df.sort_values("掲載日", ascending=False)
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
 
    with st.expander("取得ログ(サイトごとの取得結果)", expanded=df.empty):
        for line in logs:
            st.write(line)
 
 
if __name__ == "__main__":
    main()
 
