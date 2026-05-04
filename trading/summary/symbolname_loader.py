# ============================================================
# symbolname_loader.py
# ------------------------------------------------------------
# ✔ symbol_flags.db から symbolname 補完
# ✔ 無くても落ちない
# ✔ 空の行だけを対象
# ✔ paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import logging
import pandas as pd
from pathlib import Path
from sqlalchemy import create_engine

from config.paths import get_path

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# symbol_flags DB（paths.py 経由）
# ------------------------------------------------------------
SYMBOL_FLAGS_DB: Path = get_path("symbol_flags_db")


def load_symbolname_map() -> dict:
    """
    symbol_flags.db から symbol → symbolname の辞書を作る
    """
    if not SYMBOL_FLAGS_DB.exists():
        logger.warning(f"[symbolname] DB not found: {SYMBOL_FLAGS_DB}")
        return {}

    try:
        engine = create_engine(f"sqlite:///{SYMBOL_FLAGS_DB}")
        df = pd.read_sql(
            "SELECT symbol, symbolname FROM symbol_flags",
            engine,
        )
        return dict(
            zip(df["symbol"].astype(str), df["symbolname"].fillna(""))
        )
    except Exception:
        logger.error("❌ failed to load symbolname map", exc_info=True)
        return {}


def fill_symbolname(df: pd.DataFrame) -> pd.DataFrame:
    """
    symbolname が空の行のみ補完する
    """
    if df is None or df.empty:
        return df

    if "symbol" not in df.columns:
        return df

    if "symbolname" not in df.columns:
        df["symbolname"] = ""

    symbol_map = load_symbolname_map()
    if not symbol_map:
        return df

    mask = df["symbolname"].isna() | (df["symbolname"] == "")
    if not mask.any():
        return df

    df.loc[mask, "symbol"] = df.loc[mask, "symbol"].astype(str)
    df.loc[mask, "symbolname"] = (
        df.loc[mask, "symbol"].map(symbol_map).fillna("")
    )

    logger.info(
        f"[symbolname] filled {mask.sum()} rows from symbol_flags.db"
    )

    return df
