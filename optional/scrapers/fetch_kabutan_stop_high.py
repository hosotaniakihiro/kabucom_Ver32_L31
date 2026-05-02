# ============================================================
# OPTIONAL/scrapers/fetch_kabutan_stop_high.py
# Ver37-FINAL-PRICE-FIX
# ============================================================

import logging
import requests
from bs4 import BeautifulSoup
from typing import List, Dict, Set
from datetime import datetime

logger = logging.getLogger(__name__)

URLS = {
    "stop_high": "https://kabutan.jp/warning/?mode=3_1&dispmode=normal",
    "stop_low":  "https://kabutan.jp/warning/?mode=3_2&dispmode=normal",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://kabutan.jp/",
}

TIMEOUT_SEC = 15


def _to_float(text: str) -> float:
    if not text:
        return 0.0
    try:
        return float(
            text.replace(",", "")
                .replace("+", "")
                .replace("%", "")
                .strip()
        )
    except Exception:
        return 0.0


def fetch_kabutan_stop_high(
    trade_date: str,
    include_stop_low: bool = True,
    max_page: int = 10,
) -> List[Dict]:

    categories = ["stop_high"]
    if include_stop_low:
        categories.append("stop_low")

    all_rows: List[Dict] = []
    seen: Set[str] = set()

    for category in categories:

        base_url = URLS.get(category)
        if not base_url:
            continue

        category_count = 0

        for page in range(1, max_page + 1):

            url = f"{base_url}&page={page}"

            try:
                r = requests.get(url, headers=HEADERS, timeout=TIMEOUT_SEC)
                r.raise_for_status()
            except Exception:
                logger.exception(
                    "❌ kabutan %s request failed page=%d",
                    category,
                    page,
                )
                break

            soup = BeautifulSoup(r.text, "html.parser")
            table = soup.select_one("table.stock_table")

            if not table:
                break

            rows = table.select("tr")[1:]
            if not rows:
                break

            for tr in rows:

                # --- symbol ---
                code_tag = tr.select_one("td.tac a")
                if not code_tag:
                    continue

                symbol = code_tag.get_text(strip=True)

                # --- symbolname ---
                name_tag = tr.find("th")
                if not name_tag:
                    continue

                symbolname = name_tag.get_text(strip=True)

                # --- 終値（★ 正しくは tds[4]）---
                tds = tr.find_all("td")
                if len(tds) < 5:
                    continue

                price = _to_float(tds[4].get_text(strip=True))

                key = f"{category}_{symbol}"
                if key in seen:
                    continue
                seen.add(key)

                all_rows.append({
                    "symbol": symbol,
                    "symbolname": symbolname,
                    "price": price,
                    "date": trade_date,
                    "category": category,
                    "headline": category,
                    "source": "kabutan",
                })

                category_count += 1

        logger.info(
            "✅ kabutan %s total fetched: %d",
            category,
            category_count,
        )

    logger.info(
        "🎯 kabutan stop grand total fetched: %d",
        len(all_rows),
    )

    return all_rows


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    data = fetch_kabutan_stop_high(
        trade_date=datetime.today().strftime("%Y-%m-%d"),
        include_stop_low=True,
        max_page=10,
    )

    for r in data[:10]:
        print(r)

    print("TOTAL:", len(data))