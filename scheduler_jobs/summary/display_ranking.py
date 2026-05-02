# ============================================================
# File   : scheduler_jobs/summary/display_ranking.py
# Function:
#   - ranking 表示列
#   - ranking 1行文字列生成
# ------------------------------------------------------------
# Version: Ver1.0-PRODUCTION-DISPLAY-SPLIT-RANKING
# ============================================================

from __future__ import annotations

import numpy as np
import pandas as pd

from .display_base import pick_text_series, pick_series_nan, first_existing, fmt_metric


def attach_ranking_display_columns(out: pd.DataFrame) -> pd.DataFrame:
    if out is None or out.empty:
        return out

    out["disp_ranking_type"] = pick_text_series(
        out,
        ["ranking_type_view", "ranking_type", "type_name", "type"],
        default="",
    ).astype(str).str.strip()

    out["disp_ranking_rank"] = pick_series_nan(
        out,
        ["ranking_rank_view", "ranking_rank", "rank"],
    )

    out["disp_ranking_change_pct"] = pick_series_nan(
        out,
        ["ranking_change_pct_view", "change_percentage"],
    )

    out["disp_ranking_turnover"] = pick_series_nan(
        out,
        ["ranking_turnover_view", "turnover"],
    )

    out["disp_ranking_tick_count"] = pick_series_nan(
        out,
        ["ranking_tick_count_view", "tick_count"],
    )

    return out


def build_ranking_line(row: pd.Series) -> str | None:
    rank_type = first_existing(
        row,
        ["disp_ranking_type", "ranking_type_view", "ranking_type", "type_name", "type"],
        "",
    )
    rank_no = first_existing(
        row,
        ["disp_ranking_rank", "ranking_rank_view", "ranking_rank", "rank"],
        np.nan,
    )
    chg = first_existing(
        row,
        ["disp_ranking_change_pct", "ranking_change_pct_view", "change_percentage"],
        np.nan,
    )
    turn = first_existing(
        row,
        ["disp_ranking_turnover", "ranking_turnover_view", "turnover"],
        np.nan,
    )
    tick = first_existing(
        row,
        ["disp_ranking_tick_count", "ranking_tick_count_view", "tick_count"],
        np.nan,
    )

    try:
        if (
            (rank_type is None or str(rank_type).strip() == "")
            and pd.isna(rank_no)
            and pd.isna(chg)
            and pd.isna(turn)
            and pd.isna(tick)
        ):
            return None
    except Exception:
        pass

    rank_no_text = "-"
    try:
        if not pd.isna(rank_no):
            rank_no_text = str(int(float(rank_no)))
    except Exception:
        pass

    return (
        f"    ranking={str(rank_type).strip() or '-'} "
        f"rank={rank_no_text} "
        f"chg={fmt_metric(chg):>6} "
        f"turn={fmt_metric(turn):>6} "
        f"tick={fmt_metric(tick):>6}"
    )