# ============================================================
# quality_score.py
# ------------------------------------------------------------
# ✔ summary の健康診断
# ✔ AI 学習・ENTRY 前の安全装置
# ============================================================

import datetime as dt
import pandas as pd

def calc_summary_quality(
    df: pd.DataFrame,
    *,
    today: dt.date | None = None,
) -> dict:
    today = today or dt.date.today()

    if df.empty:
        return {
            "rows": 0,
            "symbols": 0,
            "fresh_ratio": 0.0,
            "ma75_ratio": 0.0,
            "score": 0.0,
        }

    rows = len(df)
    symbols = df["symbol"].nunique()

    latest_by_symbol = (
        df.groupby("symbol")["datetime"].max().dt.date
    )

    fresh_ratio = (
        (latest_by_symbol == today).sum() / symbols
        if symbols else 0
    )

    ma75_ratio = (
        df.groupby("symbol")["ma75"]
        .apply(lambda s: s.notna().any())
        .mean()
        if "ma75" in df.columns else 0
    )

    score = round(
        fresh_ratio * 0.6 + ma75_ratio * 0.4,
        3
    )

    return {
        "rows": rows,
        "symbols": symbols,
        "fresh_ratio": round(fresh_ratio, 3),
        "ma75_ratio": round(ma75_ratio, 3),
        "score": score,
    }
