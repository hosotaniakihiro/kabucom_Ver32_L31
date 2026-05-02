import logging
import requests
import pandas as pd
from bs4 import BeautifulSoup
from typing import List
from datetime import datetime

from optional.scrapers.kabutan_article_parser import parse_tomorrow_article

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://kabutan.jp/",
}

BASE = "https://kabutan.jp"

# ============================================================
# URL自動取得
# ============================================================

def _scan_page_for_tomorrow(soup: BeautifulSoup) -> str | None:
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        if "明日の好悪材料" in text:
            href = a["href"]
            if href.startswith("/news/"):
                return BASE + href
    return None


def find_latest_tomorrow_article_url() -> str | None:

    candidates = [
        "https://kabutan.jp/news/",
        "https://kabutan.jp/news/marketnews/",
    ]

    for url in candidates:
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            r.raise_for_status()
        except Exception:
            logger.warning("⚠ failed to scan: %s", url)
            continue

        soup = BeautifulSoup(r.text, "html.parser")
        found = _scan_page_for_tomorrow(soup)

        if found:
            logger.info("✅ found tomorrow article: %s", found)
            return found

    logger.warning("⚠ tomorrow article not found")
    return None


# ============================================================
# データセット構築
# ============================================================

def _normalize_symbol(symbol: str) -> str:
    if not symbol:
        return ""
    return str(symbol).replace(".T", "").zfill(4)


def build_tomorrow_dataset_auto(trade_date: str | None = None):

    if trade_date is None:
        from datetime import datetime
        trade_date = datetime.today().strftime("%Y-%m-%d")

    logger.info("🚀 tomorrow auto pipeline start")

    url = find_latest_tomorrow_article_url()
    if not url:
        logger.warning("⚠ no tomorrow article found")
        return pd.DataFrame()

    rows = parse_tomorrow_article(url)

    if not rows:
        logger.warning("⚠ no symbols extracted")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    df["date"] = trade_date

    logger.info(
        "📊 tomorrow dataset built: rows=%d symbols=%d",
        len(df),
        df["symbol"].nunique(),
    )

    return df





# ============================================================
# 単体実行
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    df = build_tomorrow_dataset_auto()

    print(df.head())
    print("TOTAL:", len(df))