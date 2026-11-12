"""懸賞検索アプリ (Streamlit)

起動:  streamlit run sweepstakes_app.py
必要:  pip install streamlit requests beautifulsoup4 pandas


懸賞まとめサイトの一覧ページを取得し、タイトルから賞品ジャンルを自動判定して
ジャンルで絞り込みます。
※ SITES のCSSセレクタは、実際のサイトのHTMLに合わせて調整してください。
"""
import re
import time
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
#   url           : 一覧ページのURL(複数可)
#   item          : 1件分を囲む要素のCSSセレクタ
#   title         : item内の、タイトル(リンク)要素のCSSセレクタ
#   deadline      : item内の締切表示のCSSセレクタ(無ければ None)
SITES = {
    "サンプルサイト(要編集)": {
        "urls": ["https://example.com/kensho/list/"],
        "item": "div.campaign",
        "title": "a",
        "deadline": ".deadline",
    },
}

# ジャンル判定用キーワード(タイトルに含まれるかで判定)。自由に追加できます。
GENRES = {
    "家電": ["家電", "テレビ", "掃除機", "炊飯器", "冷蔵庫", "電子レンジ", "ドライヤー",
             "イヤホン", "スピーカー", "PC", "パソコン", "タブレット", "iPad", "カメラ"],
    "食品・飲料": ["食品", "ビール", "お米", "米", "肉", "牛", "うなぎ", "お菓子", "スイーツ",
                  "コーヒー", "ジュース", "ワイン", "ハム", "ラーメン", "果物", "ギフト"],
    "旅行・宿泊": ["旅行", "宿泊", "ホテル", "温泉", "ペア", "航空", "ツアー", "チケット"],
    "現金・ギフト券": ["現金", "円分", "QUO", "クオカード", "ギフト券", "ギフトカード",
                     "商品券", "ポイント", "Amazon", "PayPay", "電子マネー"],
    "日用品・美容": ["日用品", "洗剤", "シャンプー", "化粧品", "コスメ", "スキンケア", "タオル",
                   "ティッシュ", "美容"],
    "ゲーム・ホビー": ["ゲーム", "Switch", "PS5", "プレイステーション", "フィギュア", "グッズ",
                      "ガチャ", "トレカ"],
    "車・乗り物": ["車", "自動車", "バイク", "自転車", "ガソリン"],
}


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


def classify(text: str) -> list[str]:
    hits = [g for g, kws in GENRES.items() if any(k.lower() in text.lower() for k in kws)]
    return hits or ["その他"]


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
            "ジャンル": classify(title),
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
    st.header("絞り込み")
    genres = st.multiselect("賞品ジャンル", list(GENRES) + ["その他"], default=[])
    keyword = st.text_input("キーワード(タイトルに含む)")
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
    if genres:
        df = df[df["ジャンル"].apply(lambda gs: any(g in gs for g in genres))]
    if keyword:
        df = df[df["タイトル"].str.contains(re.escape(keyword), case=False)]

    st.caption(f"{len(df)} 件")
    view = df.assign(ジャンル=df["ジャンル"].apply(" / ".join))
    st.dataframe(
        view,
        use_container_width=True,
        hide_index=True,
        column_config={"URL": st.column_config.LinkColumn("応募ページ", display_text="開く")},
    )
    st.download_button(
        "CSVでダウンロード",
        view.to_csv(index=False).encode("utf-8-sig"),
        file_name="kensho.csv",
        mime="text/csv",
    )

with st.expander("取得ログ"):
    for line in logs:
        st.write(line)
