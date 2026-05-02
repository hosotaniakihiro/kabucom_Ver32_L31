# ============================================================
# File   : trading/summary/engine/summary_result.py
# Ver    : PRODUCTION-STABLE-REV1.0-SPLIT-PUSH-RANKING
# ------------------------------------------------------------
# ✔ PUSH由来 / ランキング由来 の結果構造を分離
# ✔ merged_summary に依存しない
# ✔ 表示用 / 保存用 / デバッグ用を分ける
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

import pandas as pd


def empty_df() -> pd.DataFrame:
    return pd.DataFrame()


@dataclass
class SummaryRunResult:
    interval: int
    source_kind: str                  # "push" or "ranking"
    df: pd.DataFrame = field(default_factory=empty_df)
    stored: bool = False
    latest_dt: Optional[Any] = None
    rows: int = 0
    symbols: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)

    def finalize(self) -> "SummaryRunResult":
        x = self.df if isinstance(self.df, pd.DataFrame) else pd.DataFrame()
        self.df = x
        self.rows = len(x)
        self.symbols = int(x["symbol"].nunique()) if ("symbol" in x.columns and not x.empty) else 0
        self.latest_dt = (
            x["datetime"].max()
            if ("datetime" in x.columns and not x.empty)
            else None
        )
        return self

    @property
    def is_empty(self) -> bool:
        return self.df is None or self.df.empty