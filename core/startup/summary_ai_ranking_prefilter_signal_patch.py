# -*- coding: utf-8 -*-
"""
Patch Summary-AI RANKING pre-filter so ranking-derived candidates are not
incorrectly dropped when ranking_score/ranking_momentum columns are all zero.

This is not a fail-open rescue. It only uses already-existing ranking signal
columns such as display_score/score_buy/score_sell/best_rank/rank_type and
ranking movement columns when present. If no ranking signal exists, candidates
still stay blocked.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V3-RANKING-PREFILTER-SIGNAL-FAST-ENTRY"
_INSTALLED = False
_WATCHER_STARTED = False
_ORIG_APPLY_RANKING_PRE_FILTER = None


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _num_series(df: Any, col: str):
    import pandas as pd
    if col not in df.columns:
        return pd.Series(0.0, index=df.index)
    s = df[col]
    try:
        if getattr(s, "dtype", None) == object:
            s = s.astype(str).str.replace("％", "%", regex=False).str.replace("%", "", regex=False).str.replace(",", "", regex=False).str.replace("+", "", regex=False)
    except Exception:
        pass
    return pd.to_numeric(s, errors="coerce").fillna(0.0).astype(float)


def _num_first(df: Any, cols: tuple[str, ...]):
    import pandas as pd
    out = pd.Series(0.0, index=df.index)
    for col in cols:
        if col in df.columns:
            v = _num_series(df, col)
            out = out.where(out.abs() > 0, v)
    return out.fillna(0.0)


def _str_series(df: Any, col: str):
    import pandas as pd
    if col not in df.columns:
        return pd.Series("", index=df.index)
    return df[col].astype(str).fillna("").str.strip()


def _pct_to_ratio(s: Any):
    x = s.fillna(0.0).astype(float)
    try:
        return x.where(x.abs() <= 1.0, x / 100.0)
    except Exception:
        return x


def _max_series(*series: Any):
    import pandas as pd
    frames = []
    for s in series:
        try:
            frames.append(pd.to_numeric(s, errors="coerce").fillna(0.0).astype(float))
        except Exception:
            pass
    if not frames:
        return None
    return pd.concat(frames, axis=1).max(axis=1).fillna(0.0)


def _patched_apply_ranking_pre_filter(
    df,
    *,
    source: str,
    interval: int | str,
    enabled: bool = True,
    min_ranking_score: float | None = None,
    min_ranking_momentum: float | None = None,
):
    import pandas as pd

    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()
    if not enabled:
        return df

    out = df.copy()
    min_score = float(min_ranking_score) if min_ranking_score is not None else _env_float("RANKING_AI_MIN_SCORE", 0.0)
    min_mom = float(min_ranking_momentum) if min_ranking_momentum is not None else _env_float("RANKING_AI_MIN_MOMENTUM", 0.0)

    ranking_score = _num_series(out, "ranking_score")
    ranking_momentum = _num_series(out, "ranking_momentum")
    price_delta_pct = _num_series(out, "price_delta_pct")
    price_delta = _num_series(out, "price_delta")
    rank_improve = _num_series(out, "rank_improve")
    volume_delta = _num_series(out, "volume_delta")

    score_cols = (
        "score_buy", "buy_score", "score_sell", "sell_score", "score_total",
        "final_score", "display_score", "score", "pending_score", "priority",
        "score_mtf", "mtf_score", "rank_strength", "summary_ai_priority",
    )
    score_signal = pd.Series(0.0, index=out.index)
    present_score_cols: list[str] = []
    for col in score_cols:
        if col in out.columns:
            present_score_cols.append(col)
            score_signal = _max_series(score_signal, _num_series(out, col).abs())

    rank_cols = ("best_rank", "best_rank_position", "rank_position", "rank", "ranking_rank", "rank_no", "display_rank", "順位")
    rank_value = _num_first(out, rank_cols)
    present_rank_cols = [c for c in rank_cols if c in out.columns]
    max_rank = int(max(1, _env_float("RANKING_AI_PREFILTER_MAX_BEST_RANK", 50.0)))
    best_rank_signal = (rank_value > 0) & (rank_value <= max_rank)
    rank_score_signal = ((float(max_rank) + 1.0 - rank_value) / float(max_rank)).where(best_rank_signal, 0.0).clip(lower=0.0)

    rank_type = _str_series(out, "rank_type") if "rank_type" in out.columns else _str_series(out, "ranking_type")
    ranking_type_signal = rank_type.ne("") & ~rank_type.str.lower().isin({"nan", "none", "<na>", "0"})

    change_signal = pd.Series(0.0, index=out.index)
    for col in ("change_rate", "change_ratio", "change_percentage", "change_pct", "騰落率", "rate"):
        if col in out.columns:
            change_signal = _max_series(change_signal, _pct_to_ratio(_num_series(out, col)))

    range_signal = pd.Series(False, index=out.index)
    for col in ("range_pct", "intraday_range_pct", "day_range_pct", "_intrabar_range_pct", "_max_price_change_pct"):
        if col in out.columns:
            rv = _pct_to_ratio(_num_series(out, col).abs())
            range_signal = range_signal | (rv >= _env_float("RANKING_AI_PREFILTER_MIN_RANGE_PCT", 0.0015))

    volume_signal = pd.Series(False, index=out.index)
    for col in ("volume_surge_ratio", "volume_surge_ratio_1m", "volume_surge_ratio_3m", "volume_surge_ratio_5m"):
        if col in out.columns:
            volume_signal = volume_signal | (_num_series(out, col) >= _env_float("RANKING_AI_PREFILTER_MIN_SURGE_RATIO", 1.2))

    out["ranking_score"] = _max_series(ranking_score, score_signal, rank_score_signal)
    out["ranking_momentum"] = _max_series(ranking_momentum, price_delta_pct, change_signal.clip(lower=0.0))
    out["price_delta_pct"] = _max_series(price_delta_pct, change_signal)
    out["rank_improve"] = _max_series(rank_improve, best_rank_signal.astype(float) * 0.001)
    out["volume_delta"] = _max_series(volume_delta, volume_signal.astype(float) * 0.001)

    mask = (
        (out["ranking_score"] > min_score)
        | (out["ranking_momentum"] > min_mom)
        | (out["price_delta_pct"] > 0)
        | (price_delta > 0)
        | (out["rank_improve"] > 0)
        | (out["volume_delta"] > 0)
        | (ranking_type_signal & (range_signal | volume_signal | (score_signal > 0)))
    )

    before = len(out)
    skipped_df = out.loc[~mask].copy()
    out = out.loc[mask].copy()

    try:
        skipped_head = skipped_df[[c for c in ("symbol", "ranking_score", "ranking_momentum", "display_score", "score_buy", "score_sell", "best_rank", "best_rank_position", "rank_position", "rank", "rank_type", "range_pct") if c in skipped_df.columns]].head(20).to_dict(orient="records")
    except Exception:
        skipped_head = []

    logger.warning(
        "[SUMMARY AI RANKING PREFILTER SIGNAL] result source=%s interval=%s before=%s after=%s skipped=%s min_score=%.4f min_mom=%.4f score_cols=%s rank_cols=%s max_rank=%s signal_rows=%s version=%s skipped_head=%s",
        source, interval, before, len(out), before - len(out), min_score, min_mom,
        present_score_cols, present_rank_cols, max_rank, int(mask.sum()), VERSION, skipped_head,
    )
    return out


def _install_fast_ranking_summary() -> bool:
    try:
        from core.startup.ranking_summary_fast_entry_patch import install as _install_fast
        ok = bool(_install_fast())
        logger.warning("[SUMMARY AI RANKING PREFILTER SIGNAL] fast ranking summary patch ok=%s version=%s", ok, VERSION)
        return ok
    except Exception:
        logger.exception("[SUMMARY AI RANKING PREFILTER SIGNAL] fast ranking summary patch failed version=%s", VERSION)
        return False


def _apply_patch_once(reason: str = "install") -> bool:
    global _ORIG_APPLY_RANKING_PRE_FILTER
    try:
        from trading.entry.summary_ai import runner
        cur = getattr(runner, "_apply_ranking_pre_filter", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI RANKING PREFILTER SIGNAL] target missing reason=%s version=%s", reason, VERSION)
            return False
        if getattr(cur, "_summary_ai_ranking_prefilter_signal_v3", False):
            return True
        _ORIG_APPLY_RANKING_PRE_FILTER = getattr(cur, "_original", cur)
        wrapped = wraps(_ORIG_APPLY_RANKING_PRE_FILTER)(_patched_apply_ranking_pre_filter)
        wrapped._summary_ai_ranking_prefilter_signal_v1 = True  # type: ignore[attr-defined]
        wrapped._summary_ai_ranking_prefilter_signal_v2 = True  # type: ignore[attr-defined]
        wrapped._summary_ai_ranking_prefilter_signal_v3 = True  # type: ignore[attr-defined]
        wrapped._original = _ORIG_APPLY_RANKING_PRE_FILTER  # type: ignore[attr-defined]
        runner._apply_ranking_pre_filter = wrapped
        logger.warning("[SUMMARY AI RANKING PREFILTER SIGNAL] patched reason=%s version=%s original=%s", reason, VERSION, getattr(_ORIG_APPLY_RANKING_PRE_FILTER, "__name__", type(_ORIG_APPLY_RANKING_PRE_FILTER).__name__))
        return True
    except Exception:
        logger.exception("[SUMMARY AI RANKING PREFILTER SIGNAL] patch failed reason=%s version=%s", reason, VERSION)
        return False


def _watcher() -> None:
    loops = max(1, _env_int("SUMMARY_AI_RANKING_PREFILTER_WATCHER_LOOPS", 120))
    sleep_sec = max(0.5, _env_float("SUMMARY_AI_RANKING_PREFILTER_WATCHER_SLEEP", 1.0))
    for i in range(loops):
        try:
            _install_fast_ranking_summary()
            _apply_patch_once(reason=f"watcher:{i + 1}")
        except Exception:
            logger.debug("[SUMMARY AI RANKING PREFILTER SIGNAL] watcher loop failed", exc_info=True)
        time.sleep(sleep_sec)
    logger.warning("[SUMMARY AI RANKING PREFILTER SIGNAL] watcher done loops=%s version=%s", loops, VERSION)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    fast_ok = _install_fast_ranking_summary()
    ok = _apply_patch_once(reason="install")
    _INSTALLED = bool(ok or fast_ok)
    if (ok or fast_ok) and not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        try:
            threading.Thread(target=_watcher, name="summary-ai-ranking-prefilter-signal-watch", daemon=True).start()
            logger.warning("[SUMMARY AI RANKING PREFILTER SIGNAL] watcher started version=%s", VERSION)
        except Exception:
            logger.exception("[SUMMARY AI RANKING PREFILTER SIGNAL] watcher start failed version=%s", VERSION)
    return bool(ok or fast_ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI RANKING PREFILTER SIGNAL] auto install failed")
