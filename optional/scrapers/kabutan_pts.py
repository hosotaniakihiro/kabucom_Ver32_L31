# ============================================================
# scrapers/kabutan_pts.py
# ------------------------------------------------------------
# ・株探 PTS（夜間）ランキング取得
# ・上昇 / 下落 両対応
# ・optional_data.db / pts_rank 完全互換
# ============================================================

import requests
import pandas as pd
from bs4 import BeautifulSoup
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

# ============================================================
# 定数
# ============================================================

PTS_PAGES = {
    "up": "https://kabutan.jp/warning/pts_night_price_increase",
    "down": "https://kabutan.jp/warning/pts_night_price_decrease",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://kabutan.jp/",
}

# ============================================================
# helper
# ============================================================

def _to_float(x):
    """
    株探の数値表記を安全に float 化
    """
    if x is None:
        return None

    s = str(x).strip()
    if s in ("", "－", "-", "--"):
        return None

    try:
        return float(
            s.replace(",", "")
             .replace("+", "")
             .replace("%", "")
        )
    except Exception:
        return None


def _normalize_symbol(symbol: str) -> str:
    """
    symbol 正規化（6976.T → 6976）
    """
    if not symbol:
        return ""

    s = str(symbol).strip().upper()
    if ".T" in s:
        s = s.replace(".T", "")
    return s


# ============================================================
# 1ページ取得
# ============================================================

def _fetch_pts_page(url: str, page: int) -> pd.DataFrame:
    full_url = f"{url}?page={page}"
    logger.info(f"🔍 fetch PTS: {full_url}")

    try:
        res = requests.get(full_url, headers=HEADERS, timeout=15)
        res.raise_for_status()
    except Exception as e:
        logger.warning(f"⚠ request failed: {e}")
        return pd.DataFrame()

    soup = BeautifulSoup(res.text, "html.parser")
    table = soup.find("table", class_="stock_table")
    if not table or not table.tbody:
        logger.info("ℹ no table / tbody found")
        return pd.DataFrame()

    rows = []

    for tr in table.tbody.find_all("tr"):
        tds = tr.find_all("td")
        th = tr.find("th")

        if len(tds) < 12 or th is None:
            continue

        try:
            rows.append({
                "symbol": _normalize_symbol(tds[0].get_text(strip=True)),
                "symbolname": th.get_text(strip=True),
                "market": tds[1].get_text(strip=True),

                "close_price": _to_float(tds[4].text),
                "pts_price": _to_float(tds[5].text),
                "pts_diff": _to_float(tds[6].text),
                "change_pct": _to_float(tds[7].text),

                "pts_volume": _to_float(tds[8].text),
                "per": _to_float(tds[9].text),
                "pbr": _to_float(tds[10].text),
                "yield": _to_float(tds[11].text),

                "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
        except Exception:
            continue

    df = pd.DataFrame(rows)
    logger.info(f"📄 fetched rows={len(df)}")

    return df


# ============================================================
# 全ページ取得
# ============================================================

def fetch_pts_all(max_page: int = 3) -> pd.DataFrame:
    """
    return:
      DataFrame[
        symbol, symbolname, market,
        close_price, pts_price, pts_diff, change_pct,
        pts_volume, per, pbr, yield,
        rank_type, fetched_at
      ]
    """

    dfs = []

    for rank_type, url in PTS_PAGES.items():
        for page in range(1, max_page + 1):
            df = _fetch_pts_page(url, page)
            if df.empty:
                break

            df["rank_type"] = rank_type
            dfs.append(df)

    if not dfs:
        logger.warning("⚠ PTS data empty")
        return pd.DataFrame()

    out = pd.concat(dfs, ignore_index=True)

    logger.info(
        f"✅ PTS fetched total={len(out)} "
        f"(up={sum(out['rank_type']=='up')}, down={sum(out['rank_type']=='down')})"
    )

    return out


# ============================================================
# 単体実行
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    df = fetch_pts_all()
    print(df.head(10))
    print("rows:", len(df))
