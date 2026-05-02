# ============================================================
# trading/summary/types.py
# ------------------------------------------------------------
# Summary 系 API 共通の型定義
# ============================================================

from typing import TypedDict
import pandas as pd


class SummaryDict(TypedDict, total=False):
    """
    summary rebuild / incremental / bulk の共通戻り値

    key:
      - "1min"
      - "3min"
      - "5min"

    value:
      - pd.DataFrame（空DF可、int/Noneは禁止）
    """
    **{"1min": pd.DataFrame, "3min": pd.DataFrame, "5min": pd.DataFrame}**
