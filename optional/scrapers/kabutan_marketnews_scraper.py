# ============================================================
# kabutan_marketnews_scraper.py
# ------------------------------------------------------------
# ・株探「明日の材料 / 好材料 / 悪材料」スクレイパー
# ・URL取得 → 記事解析 → DataFrame 化
# ・HTML 変更耐性あり
# ============================================================

import logging
import requests
import pandas as pd
import re
from bs4 import BeautifulSoup
from typing import List
from datetime import date

logger = logging.getLogger(__name__)

BASE_URL = "https://kabutan.jp"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; kabu-bot/1.0)"
}

KEYWORDS = ("明日の材料", "好材料", "悪材料")


# ============================================================
# URL 一覧取得
# ============================================================
def fetch_marketnews_urls(max_pages: int = 5) -> List[str]:
    urls: list[str] = []

    for page in range(1, max_pages + 1):
        url = f"{BASE_URL}/news/?page={page}"

        try:
            r = requests.get(url, headers=HEADERS, timeout=10)
            r.raise_for_status()
        except Exception:
            logger.exception("❌ request failed: %s", url)
            continue

        soup = BeautifulSoup(r.text, "html.parser")

        for a in soup.select("a.news_ttl"):
            title = a.text.strip()
            href = a.get("href")

            if not href:
                continue

            if any(k in title for k in KEYWORDS):
                urls.append(BASE_URL + href)

    # 重複除去（順序保持）
    return list(dict.fromkeys(urls))


# ============================================================
# 記事1本を DataFrame に変換
# ============================================================
def scrape_marketnews_article(url: str, trade_date: str) -> pd.DataFrame:
    try:
        r = requests.get(url, headers=HEADERS, timeout=10)
        r.raise_for_status()
    except Exception:
        logger.exception("❌ article request failed: %s", url)
        return pd.DataFrame()

    soup = BeautifulSoup(r.text, "html.parser")

    title_tag = soup.select_one("h2")
    headline = title_tag.text.strip() if title_tag else ""

    body = soup.get_text("\n", strip=True)

    # --- 銘柄コード抽出（HTML変化耐性） ---
    symbols = set(re.findall(r"\b\d{4}\b", body))

    if not symbols:
        return pd.DataFrame()

    rows = []
    for sym in symbols:
        rows.append(
            {
                "symbol": sym,
                "symbolname": None,
                "headline": headline,
                "category": _detect_category(headline),
                "date": trade_date,
                "source": "kabutan",
            }
        )

    return pd.DataFrame(rows)


def _detect_category(headline: str) -> str:
    if "好材料" in headline:
        return "market_good"
    if "悪材料" in headline:
        return "market_bad"
    return "market_news"


# ============================================================
# 公開 API：まとめて取得
# ============================================================
def fetch_kabutan_marketnews(trade_date: str, max_pages: int = 5) -> pd.DataFrame:
    logger.info(
        "📰 fetch_kabutan_marketnews START trade_date=%s",
        trade_date,
    )

    urls = fetch_marketnews_urls(max_pages=max_pages)

    if not urls:
        logger.warning("⚠ marketnews urls empty")
        return pd.DataFrame()

    dfs = []
    for url in urls:
        df = scrape_marketnews_article(url, trade_date)
        if df is not None and not df.empty:
            dfs.append(df)

    if not dfs:
        logger.warning("⚠ marketnews all empty")
        return pd.DataFrame()

    out = pd.concat(dfs, ignore_index=True)

    logger.info(
        "✅ kabutan marketnews fetched rows=%d symbols=%d",
        len(out),
        out["symbol"].nunique(),
    )
    return out


# ============================================================
# 単体テスト
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    today = date.today().strftime("%Y-%m-%d")
    df = fetch_kabutan_marketnews(today)
    print(df.head(20))
    print("rows:", len(df))
