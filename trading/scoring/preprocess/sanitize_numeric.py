# ============================================================
# File   : trading/scoring/preprocess/sanitize_numeric.py
# Version: Ver2.0-PRODUCTION-NUMERIC-SANITIZER
# ------------------------------------------------------------
# ✔ numeric conversion
# ✔ NaN handling
# ✔ inf handling
# ✔ object → numeric safe conversion
# ✔ production stable
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


# ============================================================
# sanitize numeric
# ============================================================

def sanitize_numeric(df: pd.DataFrame) -> pd.DataFrame:

    if df is None or df.empty:
        return df

    try:

        df_out = df.copy()

        for col in df_out.columns:

            try:

                # object → numeric attempt
                if pd.api.types.is_string_dtype(df_out[col]):

                    try:
                        df_out[col] = pd.to_numeric(df_out[col], errors="raise")
                    except (ValueError, TypeError):
                        pass

                # numeric sanitize
                if pd.api.types.is_numeric_dtype(df_out[col]):

                    df_out[col] = (
                        df_out[col]
                        .replace([np.inf, -np.inf], np.nan)
                        .fillna(0)
                    )

            except Exception:

                logger.debug(
                    "[SANITIZE] column skip %s",
                    col
                )

        logger.debug(
            "[SANITIZE] numeric sanitize done rows=%s",
            len(df_out)
        )

        return df_out

    except Exception:

        logger.exception("[SANITIZE] error")

        return df