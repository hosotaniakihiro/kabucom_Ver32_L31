# ============================================================
# optional/scrapers/ingest_kabutan_marketnews.py
# ------------------------------------------------------------
# ・株探マーケットニュース ingest（scraper 側エントリ）
# ・batch から呼ばれる公開 API
# ・Ver22-L02 base_dir / DI 完全対応版
# ============================================================

import logging
import pandas as pd
from pathlib import Path
from typing import Optional

from optional.scrapers.build_tomorrow_dataset import build_tomorrow_dataset
from optional.scrapers.kabutan_kessan import fetch_tomorrow_article_urls

logger = logging.getLogger(__name__)


# ============================================================
# ★ batch から呼ばれる唯一の公開関数
# ============================================================
def ingest_kabutan_marketnews(
    trade_date: str,
    base_dir: Optional[Path] = None,
    db_path: Optional[Path] = None,
    logger_: Optional[logging.Logger] = None,
    **kwargs,
) -> pd.DataFrame:
    """
    株探 マーケットニュース ingest

    Parameters
    ----------
    trade_date : str
        YYYY-MM-DD
    base_dir : Path | None
        batch 側 DI 用（現状は未使用・将来拡張用）
    db_path : Path | None
        batch 側互換用（現時点では使用しない）
    logger_ : logging.Logger | None
        batch 側 DI 用 logger（無ければ module logger 使用）
    **kwargs :
        将来拡張用（unexpected keyword argument 完全防止）

    Returns
    -------
    DataFrame
        news_events 互換 DataFrame
    """

    log = logger_ or logger

    log.info(
        "📰 ingest_kabutan_marketnews START (trade_date=%s)",
        trade_date,
    )

    # --- DI 引数は将来用として受けるだけ（現時点では使わない） ---
    if base_dir is not None:
        log.debug("ingest_kabutan_marketnews base_dir=%s", base_dir)

    if db_path is not None:
        log.debug("ingest_kabutan_marketnews db_path=%s", db_path)

    if kwargs:
        log.debug(
            "ingest_kabutan_marketnews extra kwargs ignored: %s",
            list(kwargs.keys()),
        )

    # --------------------------------------------------------
    # URL 取得
    # --------------------------------------------------------
    try:
        urls = fetch_tomorrow_article_urls(max_pages=7)
    except Exception:
        log.exception("❌ failed to fetch tomorrow article urls")
        return pd.DataFrame()

    if not urls:
        log.warning("⚠ no tomorrow article urls")
        return pd.DataFrame()

    # --------------------------------------------------------
    # 明日の好悪材料データセット構築
    # --------------------------------------------------------
    try:
        df = build_tomorrow_dataset(urls)
    except Exception:
        log.exception("❌ failed to build tomorrow dataset")
        return pd.DataFrame()

    if df is None or df.empty:
        log.warning("⚠ kabutan marketnews empty")
        return pd.DataFrame()

    # --------------------------------------------------------
    # 日付付与
    # --------------------------------------------------------
    df = df.copy()
    df["date"] = trade_date

    log.info(
        "✅ kabutan marketnews built rows=%d symbols=%d",
        len(df),
        df["symbol"].nunique() if "symbol" in df.columns else -1,
    )

    return df


# ============================================================
# 単体実行テスト
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    df = ingest_kabutan_marketnews(
        trade_date="2099-01-01",
        base_dir=Path("/dummy/base_dir"),
    )
    print(df.head())
    print("rows:", len(df))
