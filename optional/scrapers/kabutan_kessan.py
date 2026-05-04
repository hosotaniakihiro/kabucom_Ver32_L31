# ============================================================
# scrapers/kabutan_kessan.py
# ------------------------------------------------------------
# ・株探 決算速報ページを DataFrame 化
# ・HTML 変更耐性あり
# ・optional_data / news_events 互換
# ・Ver22-L02 FIXED（NameError 完全防止）
# ============================================================

import pandas as pd
import logging
from datetime import date

logger = logging.getLogger(__name__)

# ============================================================
# main
# ============================================================

def fetch_kabutan_kessan(trade_date: str | None = None) -> pd.DataFrame:
    """
    株探 決算速報を取得

    return:
      DataFrame[
        symbol,
        symbolname,
        headline,
        category,
        date,
        source
      ]
    """

    url = "https://kabutan.jp/news/?mode=kessan"
    trade_date = trade_date or date.today().strftime("%Y-%m-%d")

    try:
        tables = pd.read_html(url)
    except Exception as e:
        logger.warning("⚠ read_html failed: %s", e)
        return pd.DataFrame()

    if not tables:
        logger.warning("⚠ kessan tables not found")
        return pd.DataFrame()

    df = tables[0]

    # --------------------------------------------------------
    # 列名ゆらぎ吸収
    # --------------------------------------------------------
    col_map = {}

    for c in df.columns:
        if "コード" in c:
            col_map[c] = "symbol"
        elif "銘柄" in c:
            col_map[c] = "symbolname"
        elif "決算" in c:
            col_map[c] = "headline"

    if not {"symbol", "symbolname", "headline"} <= set(col_map.values()):
        logger.warning("⚠ unexpected columns: %s", list(df.columns))
        return pd.DataFrame()

    df = df.rename(columns=col_map)

    # --------------------------------------------------------
    # 正規化
    # --------------------------------------------------------
    df["symbol"] = (
        df["symbol"]
        .astype(str)
        .str.strip()
        .str.replace(".0", "", regex=False)
        .str.zfill(4)
    )

    df["symbolname"] = df["symbolname"].astype(str).str.strip()
    df["headline"] = df["headline"].astype(str).str.strip()

    # --------------------------------------------------------
    # 付加情報
    # --------------------------------------------------------
    df["category"] = "kessan"
    df["date"] = trade_date
    df["source"] = "kabutan"

    out = df[
        [
            "symbol",
            "symbolname",
            "headline",
            "category",
            "date",
            "source",
        ]
    ].copy()

    logger.info("✅ kabutan kessan fetched: %d rows", len(out))
    return out


# ============================================================
# 互換エイリアス（marketnews 用）
# ============================================================

def fetch_tomorrow_article_urls(*args, **kwargs) -> list[str]:
    """
    明日の好悪材料 記事URL取得（互換API）

    ※ このモジュールでは URL スクレイピングは行わない
    ※ ingest_kabutan_marketnews 側で実装する設計
    """
    logger.warning(
        "⚠ fetch_tomorrow_article_urls called in kabutan_kessan.py "
        "(no implementation here, return empty list)"
    )
    return []


# ============================================================
# 単体テスト
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    df = fetch_kabutan_kessan()
    print(df.head(10))
    print("rows:", len(df))
