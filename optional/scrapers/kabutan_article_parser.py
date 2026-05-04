# ============================================================
# scrapers/kabutan_article_parser.py
# Ver7-FINAL-BODY-DIRECT-STRUCTURE
# ============================================================

import requests
from bs4 import BeautifulSoup, Tag, NavigableString
from datetime import datetime
import re
import logging
from typing import List, Dict

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://kabutan.jp/",
}

STOCK_RE = re.compile(r"/stock/.*?code=(\d{4}[A-Z]?)")

# ------------------------------------------------------------

def clean(text):
    if not text:
        return None
    return text.replace("★", "").strip()

# ------------------------------------------------------------

def parse_kabutan_article(
    url: str,
    article_type: str,
    trade_date: str,
) -> List[Dict]:

    logger.info(f"📄 parse article: {url}")

    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
    except Exception as e:
        logger.warning(f"⚠ fetch failed: {url} ({e})")
        return []

    soup = BeautifulSoup(r.text, "html.parser")

    headline_tag = soup.select_one("h1")
    headline = clean(headline_tag.get_text(strip=True)) if headline_tag else None

    body = soup.select_one("div.body")
    if not body:
        logger.warning("⚠ body not found")
        return []

    fetched_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []

    # --------------------------------------------------------
    # body内の全aタグを走査（最も安定）
    # --------------------------------------------------------
    for a in body.find_all("a", href=True):

        href = a["href"]
        m = STOCK_RE.search(href)
        if not m:
            continue

        symbol = m.group(1)

        # 企業名取得
        # aの直前テキストを参照
        prev_text = ""
        if isinstance(a.previous_sibling, NavigableString):
            prev_text = str(a.previous_sibling).strip()

        symbolname = clean(prev_text.replace("■", "").strip())
        if not symbolname:
            symbolname = clean(a.get_text())

        # 行全体取得
        line = a.parent.get_text(" ", strip=True)
        comment = clean(line)

        if not comment:
            continue

        rows.append({
            "symbol": symbol,
            "symbolname": symbolname,
            "market": None,
            "category": article_type,
            "headline": headline,
            "comment": comment,
            "score": None,
            "date": trade_date,
            "source": "kabutan",
            "fetched_at": fetched_at,
        })

    logger.info(f"✅ extracted symbols = {len(rows)}")
    return rows

# ------------------------------------------------------------

def parse_tomorrow_article(url: str) -> List[Dict]:

    trade_date = datetime.today().strftime("%Y-%m-%d")

    return parse_kabutan_article(
        url=url,
        article_type="tomorrow_material",
        trade_date=trade_date,
    )

# ------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    test_url = "https://kabutan.jp/news/marketnews/?b=n202602121225"

    data = parse_tomorrow_article(test_url)

    for r in data[:5]:
        print(r)

    print("TOTAL:", len(data))