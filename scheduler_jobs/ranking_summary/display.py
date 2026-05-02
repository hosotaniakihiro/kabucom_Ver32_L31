# ============================================================
# File   : scheduler_jobs/ranking_summary/display.py
# Version: Ver31_L23-RANKING-SUMMARY-DISPLAY-SEPARATED
# ------------------------------------------------------------
# 機能:
#   - ランキング由来サマリーの専用表示
#   - ranking専用キャッシュからDataFrame取得
#   - RANKING SUMMARY TOP10 をログへ出力
#   - 株価は小数点第1位、指標は小数点第2位で整形
#   - symbolname/name の重複を避けて表示
#
# 目的:
#   - PUSH由来表示と完全分離
#   - ranking summary の表示経路を独立させる
#
# 主な関数:
#   - display_ranking_summary(interval=1, top_n=10)
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable

import pandas as pd

from trading.ranking_summary.cache import (
    get_ranking_summary,
    get_ranking_summary_latest_dt,
)

logger = logging.getLogger(__name__)


def _safe_df(df) -> pd.DataFrame:
    try:
        if isinstance(df, pd.DataFrame):
            return df.copy()
        return pd.DataFrame()
    except Exception:
        logger.exception("[RANKING DISPLAY] _safe_df failed")
        return pd.DataFrame()


def _pick_name(row: pd.Series) -> str:
    for col in ("symbolname", "name"):
        v = row.get(col)
        if pd.notna(v) and str(v).strip():
            return str(v).strip()
    return "-"


def _fmt_price(v) -> str:
    try:
        if pd.isna(v):
            return "-"
        return f"{float(v):,.1f}"
    except Exception:
        return "-"


def _fmt_metric(v) -> str:
    try:
        if pd.isna(v):
            return "-"
        return f"{float(v):,.2f}"
    except Exception:
        return "-"


def _find_col(df: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    cols = set(df.columns)
    for c in candidates:
        if c in cols:
            return c
    return None


def _prepare_display_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty:
        return out

    score_col = _find_col(out, ["score", "ranking_score", "final_score"])
    if score_col and score_col != "score":
        out["score"] = out[score_col]

    if "score" in out.columns:
        out = out.sort_values("score", ascending=False, na_position="last")

    return out.reset_index(drop=True)


def display_ranking_summary(interval: int | str = 1, top_n: int = 10) -> None:
    """
    ランキング由来サマリーのTOP表示
    """
    try:
        df = get_ranking_summary(interval)
        df = _prepare_display_df(df)

        latest_dt = get_ranking_summary_latest_dt(interval)
        interval_label = f"{int(str(interval).replace('min', ''))}min"

        logger.info("")
        logger.info("=== ⏱ 最新 %s ランキングサマリー｜%s ===", interval_label, latest_dt if latest_dt else "-")
        logger.info("")
        logger.info("========== 📊 RANKING SUMMARY TOP10 (%s) ==========", interval_label)
        logger.info("🔵 BUY TOP10（score / slope / rsi / macd / best_rank / hist / type）")

        if df.empty:
            logger.info(" (no ranking candidates)")
            return

        buy_df = df.head(top_n).copy()

        for i, (_, row) in enumerate(buy_df.iterrows(), start=1):
            symbol = str(row.get("symbol", "-"))
            name = _pick_name(row)

            close_v = row.get("close", row.get("current_price"))
            score_v = row.get("score")
            slope_v = row.get("slope")
            rsi_v = row.get("rsi")
            macd_v = row.get("macd")
            best_v = row.get("best_rank")
            hist_v = row.get("hist")
            type_v = row.get("rank_type", row.get("type"))

            line = (
                f"{i:>2}. ⚪ {symbol:<6} {name:<30} "
                f"close={_fmt_price(close_v):>8} "
                f"score={_fmt_metric(score_v):>7} "
                f"slope={_fmt_metric(slope_v):>7} "
                f"rsi={_fmt_metric(rsi_v):>7} "
                f"macd={_fmt_metric(macd_v):>7} "
                f"best={str(best_v) if pd.notna(best_v) else '-':>4} "
                f"hist={str(hist_v) if pd.notna(hist_v) else '-':>3} "
                f"type={str(type_v) if pd.notna(type_v) else '-'}"
            )
            logger.info(line)

    except Exception:
        logger.exception(
            "[RANKING DISPLAY] display_ranking_summary failed interval=%r",
            interval,
        )