# ============================================================
# AI/monitor/ai_degradation_check.py
# STEP3-①-② AI 劣化検知
# ============================================================

import sqlite3
import pandas as pd
from pathlib import Path

METRIC_DB = Path("AI/data/ai_metrics.db")
TABLE = "ai_metrics"


def is_ai_degraded(source: str = "mtf_summary") -> bool:

    df = pd.read_sql(
        f"SELECT * FROM {TABLE} WHERE source=? ORDER BY date DESC",
        sqlite3.connect(METRIC_DB),
        params=(source,),
    )

    if len(df) < 5:
        return False  # データ不足

    recent = df.head(1)
    past = df.head(5)

    # 劣化条件
    if recent["win_rate"].iloc[0] < 0.45:
        return True

    if recent["ev"].iloc[0] < past["ev"].mean() * 0.7:
        return True

    return False
