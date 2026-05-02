import logging
import requests
from bs4 import BeautifulSoup
from typing import List, Dict, Set
import re
import hashlib

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# URL
# ------------------------------------------------------------
URLS = {
    "rise_rate": "https://kabutan.jp/warning/?mode=2_1&dispmode=normal",
    "fall_rate": "https://kabutan.jp/warning/?mode=2_2&dispmode=normal",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://kabutan.jp/",
}

TIMEOUT_SEC = 15


# ============================================================
# 共通ユーティリティ
# ============================================================

def _normalize_symbol(symbol: str) -> str:
    if not symbol:
        return ""
    s = str(symbol).strip()
    s = s.replace(".T", "")
    return s.zfill(4)


def _find_warning_table(soup: BeautifulSoup):
    """
    HTML変更耐性強化：
    複数候補から警告テーブルを探す
    """
    return (
        soup.select_one("table.stock_table")
        or soup.select_one("table.stock_table_01")
        or soup.select_one("table.warning_table")
        or soup.find("table")
    )


def _extract_symbol_and_name(tr):
    """
    行から symbol / symbolname を安全に抽出
    """

    symbol = ""
    symbolname = ""

    # --------------------------------------------------------
    # ① <th> から銘柄名を最優先取得
    # --------------------------------------------------------
    th = tr.find("th")
    if th:
        text = th.get_text(strip=True)
        if text and not re.fullmatch(r"\d{4}", text):
            symbolname = text

    # --------------------------------------------------------
    # ② aタグ解析
    # --------------------------------------------------------
    links = tr.find_all("a", href=True)

    for a in links:
        href = a.get("href", "")
        text = a.get_text(strip=True)

        # code=XXXX パターン
        m = re.search(r"code=(\d{4})", href)
        if m:
            symbol = _normalize_symbol(m.group(1))
            continue

        # 4桁数値リンク
        if re.fullmatch(r"\d{4}", text):
            symbol = _normalize_symbol(text)
            continue

        # 銘柄名補完（まだ無い場合のみ）
        if not symbolname and text and not re.fullmatch(r"\d{4}", text):
            symbolname = text

    # --------------------------------------------------------
    # 最終補正
    # --------------------------------------------------------
    if symbol and not symbolname:
        symbolname = symbol

    return symbol, symbolname


# ============================================================
# メインAPI
# ============================================================

def fetch_kabutan_rise_rate(
    trade_date: str,
    top_n: int = 1000,
    ratio: float = 1.0,
    include_fall: bool = True,
    max_page: int = 10,
) -> List[Dict]:

    categories = ["rise_rate"]
    if include_fall:
        categories.append("fall_rate")

    all_selected: List[Dict] = []

    for category in categories:

        base_url = URLS.get(category)
        if not base_url:
            continue

        collected: List[Dict] = []
        seen: Set[str] = set()
        previous_hash = None

        for page in range(1, max_page + 1):

            full_url = f"{base_url}&page={page}"

            try:
                r = requests.get(full_url, headers=HEADERS, timeout=TIMEOUT_SEC)
                r.raise_for_status()
            except Exception:
                logger.exception("❌ kabutan %s request failed page=%d", category, page)
                break

            # --------------------------------------------------
            # 同一ページ検出（終端判定）
            # --------------------------------------------------
            page_hash = hashlib.md5(r.text.encode("utf-8")).hexdigest()
            if page_hash == previous_hash:
                break
            previous_hash = page_hash

            soup = BeautifulSoup(r.text, "html.parser")
            table = _find_warning_table(soup)

            if not table:
                break

            rows = table.select("tr")
            if len(rows) <= 1:
                break

            page_added = 0

            for tr in rows[1:]:

                symbol, symbolname = _extract_symbol_and_name(tr)

                if not symbol:
                    continue

                if symbol in seen:
                    continue

                seen.add(symbol)
                page_added += 1

                collected.append({
                    "symbol": symbol,
                    "symbolname": symbolname,
                    "date": trade_date,
                    "category": category,
                    "headline": category,
                    "source": "kabutan",
                })

            # ページに追加が無ければ終端
            if page_added == 0:
                break

        if not collected:
            logger.warning("⚠ kabutan %s empty", category)
            continue

        # top_n 制限
        collected = collected[:top_n]

        # ratio 適用
        limit = max(int(len(collected) * ratio), 1)
        selected = collected[:limit]

        logger.info(
            "✅ kabutan %s total=%d selected=%d",
            category,
            len(collected),
            len(selected),
        )

        all_selected.extend(selected)

    logger.info(
        "🎯 kabutan warning grand total selected=%d",
        len(all_selected),
    )

    return all_selected


# ============================================================
# 単体テスト
# ============================================================

if __name__ == "__main__":
    import logging
    from datetime import datetime

    logging.basicConfig(level=logging.INFO)

    data = fetch_kabutan_rise_rate(
        trade_date=datetime.today().strftime("%Y-%m-%d"),
        include_fall=True,
        max_page=10,
    )

    for r in data[:10]:
        print(r)

    print("TOTAL:", len(data))