# ============================================================
# File   : trading/scoring/preprocess/normalize_columns.py
# Version: Ver2.0-PRODUCTION-COLUMN-NORMALIZER
# ------------------------------------------------------------
# ✔ column name normalization
# ✔ multiple data source compatibility
# ✔ lowercase normalization
# ✔ alias mapping
# ✔ missing column safe
# ✔ production stable
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# column alias map
# ============================================================

COLUMN_ALIASES = {

    # symbol
    "symbol_code": "symbol",
    "code": "symbol",

    # name
    "name": "symbolname",
    "symbol_name": "symbolname",

    # price
    "c": "close",
    "close_price": "close",

    "o": "open",
    "open_price": "open",

    "h": "high",
    "high_price": "high",

    "l": "low",
    "low_price": "low",

    # volume
    "v": "volume",
    "vol": "volume",

    # datetime
    "time": "datetime",
    "timestamp": "datetime",

}


# ============================================================
# normalize columns
# ============================================================

def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        df_out = df.copy()

        # ----------------------------------------------------
        # lowercase
        # ----------------------------------------------------

        df_out.columns = [str(c).lower().strip() for c in df_out.columns]

        # ----------------------------------------------------
        # alias rename
        # ----------------------------------------------------

        rename_map = {}

        for col in df_out.columns:

            if col in COLUMN_ALIASES:

                rename_map[col] = COLUMN_ALIASES[col]

        if rename_map:

            df_out = df_out.rename(columns=rename_map)

        logger.debug(
            "[NORMALIZE] columns normalized count=%s",
            len(df_out.columns)
        )

        return df_out

    except Exception:

        logger.exception("[NORMALIZE] error")

        return df