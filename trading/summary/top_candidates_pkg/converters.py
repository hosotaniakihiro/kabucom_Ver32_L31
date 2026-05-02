# ============================================================
# File   : trading/summary/top_candidates_pkg/converters.py
# Version: Ver3.1-PRODUCTION-SUMMARY-TOP-CANDIDATES-CONVERTERS
# ------------------------------------------------------------
# Function:
#   - summary row を AI-Gate 用 candidate dict に正規化
#   - 指定 side の TOP rows を抽出
#   - score_reasons 要約列を candidate dict に直接反映
#   - entry_setup_label / pullback_subtype_label を付与
# ============================================================

from __future__ import annotations

from typing import Any, Dict, Iterable, List

import pandas as pd

from .utils import (
    safe_symbol,
    safe_str,
    safe_float,
    normalize_side,
    normalize_interval,
    first_existing_value,
    main_score,
    side_score_column,
    ensure_numeric_col,
)

from .reason_utils import (
    attach_score_reason_columns,
    format_score_reasons,
    setup_label,
)


# ============================================================
# internal helpers
# ============================================================

def _first_float(row: pd.Series, names: Iterable[str], default: float = 0.0) -> float:
    for name in names:
        try:
            if name in row.index:
                return safe_float(row.get(name), default=default)
        except Exception:
            continue
    return default


def _first_str(row: pd.Series, names: Iterable[str], default: str = "") -> str:
    for name in names:
        try:
            if name in row.index:
                value = safe_str(row.get(name), default=default)
                if value != "":
                    return value
        except Exception:
            continue
    return default


def _ensure_reason_strings(row: pd.Series) -> Dict[str, str]:
    """
    row から score_reason_* 列を安全に取り出す。
    無い場合は score_reasons からその場で生成する。
    """
    score_reasons = row.get("score_reasons")

    top3 = safe_str(row.get("score_reason_top3"))
    top5 = safe_str(row.get("score_reason_top5"))
    summary = safe_str(row.get("score_reason_summary"))

    if not top3:
        top3 = format_score_reasons(score_reasons, top_n=3)
    if not top5:
        top5 = format_score_reasons(score_reasons, top_n=5)
    if not summary:
        summary = top3

    return {
        "score_reason_top3": top3,
        "score_reason_top5": top5,
        "score_reason_summary": summary,
    }


def _copy_optional_float_fields(row: pd.Series, names: List[str]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for name in names:
        try:
            if name in row.index:
                out[name] = safe_float(row.get(name), default=0.0)
        except Exception:
            out[name] = 0.0
    return out


def _copy_optional_str_fields(row: pd.Series, names: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for name in names:
        try:
            if name in row.index:
                out[name] = safe_str(row.get(name), default="")
        except Exception:
            out[name] = ""
    return out


# ============================================================
# main APIs
# ============================================================

def row_to_ai_candidate(
    row: pd.Series,
    *,
    side: str,
    source: str,
    interval: Any,
) -> Dict[str, Any]:
    """
    summary row を AI-Gate 用 candidate dict に正規化する。
    """

    side = normalize_side(side) or "BUY"
    interval_label = normalize_interval(interval)

    current_price = first_existing_value(
        row,
        ["current_price", "price", "close", "last_price", "close_price"],
        default=0.0,
    )

    symbolname = first_existing_value(
        row,
        ["symbolname", "name", "銘柄名", "symbolname_view"],
        default="",
    )

    ranking_type = first_existing_value(
        row,
        ["ranking_type", "rank_type", "type", "category"],
        default="",
    )

    best_rank = first_existing_value(
        row,
        ["best_rank", "rank", "ranking_rank"],
        default=0,
    )

    rank_history = first_existing_value(
        row,
        ["rank_history", "hist", "history"],
        default="",
    )

    datetime_value = first_existing_value(
        row,
        ["datetime", "inserted_at", "updated_at", "timestamp"],
        default="",
    )

    entry_score = safe_float(row.get("_entry_score"), default=0.0)
    if entry_score == 0.0:
        entry_score = main_score(row, side)

    reason_map = _ensure_reason_strings(row)

    entry_setup_type = safe_str(row.get("entry_setup_type"))
    pullback_subtype = safe_str(row.get("pullback_subtype"))

    candidate: Dict[str, Any] = {
        # ----------------------------------------------------
        # identity
        # ----------------------------------------------------
        "symbol": safe_symbol(row.get("symbol")),
        "symbolname": safe_str(symbolname),
        "side": side,
        "signal": side,
        "source": source,
        "interval": interval_label,

        # ----------------------------------------------------
        # base scores
        # ----------------------------------------------------
        "score": safe_float(row.get("score")),
        "score_buy": safe_float(row.get("score_buy")),
        "score_sell": safe_float(row.get("score_sell")),
        "score_total": safe_float(row.get("score_total")),
        "final_score": safe_float(row.get("final_score")),
        "display_score": safe_float(row.get("display_score")),

        # ----------------------------------------------------
        # trend / mtf / slope
        # ----------------------------------------------------
        "slope": safe_float(row.get("slope")),
        "slope_atr_scaled": safe_float(row.get("slope_atr_scaled")),
        "score_slope": safe_float(row.get("score_slope")),
        "mtf": safe_float(row.get("mtf")),
        "score_mtf": safe_float(row.get("score_mtf")),
        "mtf_score": safe_float(row.get("mtf_score")),

        # ----------------------------------------------------
        # indicators
        # ----------------------------------------------------
        "rsi": safe_float(row.get("rsi")),
        "macd": safe_float(row.get("macd")),
        "signal_value": safe_float(
            row.get(
                "signal_value",
                row.get("macd_signal", row.get("signal_line", row.get("signal", 0.0))),
            )
        ),

        # ----------------------------------------------------
        # price / ohlcv
        # ----------------------------------------------------
        "current_price": safe_float(current_price),
        "close": safe_float(row.get("close", current_price)),
        "open": safe_float(row.get("open")),
        "high": safe_float(row.get("high")),
        "low": safe_float(row.get("low")),
        "volume": safe_float(row.get("volume")),

        # ----------------------------------------------------
        # time
        # ----------------------------------------------------
        "datetime": safe_str(datetime_value),

        # ----------------------------------------------------
        # ranking meta
        # ----------------------------------------------------
        "ranking_type": safe_str(ranking_type),
        "rank_type": safe_str(ranking_type),
        "best_rank": safe_float(best_rank),
        "rank": safe_float(row.get("rank", best_rank)),
        "rank_history": safe_str(rank_history),
        "hist": safe_str(row.get("hist", rank_history)),

        # ----------------------------------------------------
        # entry score
        # ----------------------------------------------------
        "entry_score": entry_score,

        # ----------------------------------------------------
        # score reasons
        # ----------------------------------------------------
        "score_reason_top3": reason_map["score_reason_top3"],
        "score_reason_top5": reason_map["score_reason_top5"],
        "score_reason_summary": reason_map["score_reason_summary"],

        # ----------------------------------------------------
        # setup meta
        # ----------------------------------------------------
        "entry_setup_type": entry_setup_type,
        "entry_setup_label": setup_label(entry_setup_type),
        "pullback_subtype": pullback_subtype,
        "pullback_subtype_label": setup_label(pullback_subtype),
        "setup_score": safe_float(row.get("setup_score")),
        "entry_score_v4": safe_float(row.get("entry_score_v4")),
    }

    # --------------------------------------------------------
    # optional setup / reason related floats
    # --------------------------------------------------------
    candidate.update(
        _copy_optional_float_fields(
            row,
            [
                "entry_timing_score",
                "danger_penalty_score",
                "pullback_score_v2",
                "breakout_score",
                "reversal_score",
                "trend_continuation_score",
                "vwap_reclaim_score",
                "range_break_score",
                "retest_success_score",
                "opening_range_break_score",
                "multi_tf_resonance_score",
                "relative_strength_score",
                "phase_shift_score",
                "ranking_persistence_score",
                "fakeout_reversal_score",
                "gap_go_score",
                "volatility_squeeze_score",
            ],
        )
    )

    # --------------------------------------------------------
    # optional extra descriptive fields
    # --------------------------------------------------------
    candidate.update(
        _copy_optional_str_fields(
            row,
            [
                "score_reason_top3",
                "score_reason_top5",
                "score_reason_summary",
                "pullback_reason",
                "setup_reason",
            ],
        )
    )

    return candidate


def prepare_top_rows_for_side(
    df: pd.DataFrame,
    *,
    side: str,
    top_n: int,
) -> pd.DataFrame:
    """
    指定 side の TOP rows を返す。
    """

    if df is None or df.empty:
        return pd.DataFrame()

    work = df.copy()

    if "symbol" not in work.columns:
        return pd.DataFrame()

    work["symbol"] = work["symbol"].map(safe_symbol)
    work = work[work["symbol"] != ""].copy()

    if work.empty:
        return pd.DataFrame()

    side = normalize_side(side) or "BUY"
    score_col = side_score_column(side)

    if score_col not in work.columns:
        if side == "BUY":
            if "score" in work.columns:
                work[score_col] = work["score"]
            elif "display_score" in work.columns:
                work[score_col] = work["display_score"]
            elif "final_score" in work.columns:
                work[score_col] = work["final_score"]
            else:
                work[score_col] = 0.0
        else:
            work[score_col] = 0.0

    work = ensure_numeric_col(work, score_col, 0.0)

    other_score_col = "score_sell" if side == "BUY" else "score_buy"
    work = ensure_numeric_col(work, other_score_col, 0.0)

    # setup系の score がある場合は補助的に見えるよう数値保証
    optional_numeric_cols = [
        "setup_score",
        "entry_score_v4",
        "score_total",
        "final_score",
        "display_score",
    ]
    for col in optional_numeric_cols:
        if col in work.columns:
            work = ensure_numeric_col(work, col, 0.0)

    # 既存 main_score を基本 entry score とする
    work["_entry_score"] = work.apply(
        lambda r: main_score(r, side),
        axis=1,
    )

    # setup_score / entry_score_v4 がある場合は強い方を採用しやすくする
    if "entry_score_v4" in work.columns:
        try:
            entry_score_v4 = pd.to_numeric(work["entry_score_v4"], errors="coerce").fillna(0.0)
            work["_entry_score"] = work["_entry_score"].where(
                work["_entry_score"] >= entry_score_v4,
                entry_score_v4,
            )
        except Exception:
            pass

    elif "setup_score" in work.columns:
        try:
            setup_score = pd.to_numeric(work["setup_score"], errors="coerce").fillna(0.0)
            work["_entry_score"] = work["_entry_score"].where(
                work["_entry_score"] >= setup_score,
                setup_score,
            )
        except Exception:
            pass

    work = work[work["_entry_score"] > 0].copy()

    if work.empty:
        return pd.DataFrame()

    # score_reasons / setup label 由来列を事前付与
    work = attach_score_reason_columns(work)

    # symbol ごとに最高行を残し上位抽出
    sort_cols = ["_entry_score", "symbol"]
    ascending = [False, True]

    # setup_score / final_score がある場合は tie-breaker に使う
    if "setup_score" in work.columns:
        sort_cols.insert(1, "setup_score")
        ascending.insert(1, False)

    if "final_score" in work.columns:
        insert_at = 2 if "setup_score" in work.columns else 1
        sort_cols.insert(insert_at, "final_score")
        ascending.insert(insert_at, False)

    work = (
        work.sort_values(
            by=sort_cols,
            ascending=ascending,
            kind="mergesort",
        )
        .drop_duplicates(subset=["symbol"], keep="first")
        .head(int(top_n))
        .copy()
    )

    work["signal"] = side

    return work