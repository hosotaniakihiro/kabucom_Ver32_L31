# -*- coding: utf-8 -*-
"""Repair ranking pre-filter feature columns before Summary-AI candidate selection.

The Summary-AI runner intentionally requires at least one ranking-derived signal for
RANKING/RANKING_SUMMARY sources. Some current ranking summary frames arrive with
`ranking_score`, `ranking_momentum`, `price_delta_pct`, `rank_improve`, and
`volume_delta` all present but zero, even though `best_rank`/`rank` and change-rate
columns are available. In that state the runner logs `RANKING_PRE_FILTER ... after=0`
and never reaches the AI/order stage.

This patch does not make ranking entries fail-open. It only reconstructs the
pre-filter signals from explicit ranking columns already present in the row.
"""
from __future__ import annotations

import logging
import math
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-RANKING-PREFILTER-FEATURE-REPAIR"
_INSTALLED = False
_ORIG_APPLY_RANKING_PRE_FILTER = None


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _to_numeric_series(df: Any, names: tuple[str, ...], default: float = 0.0):
    import pandas as pd
    try:
        idx = df.index
    except Exception:
        idx = None
    for name in names:
        try:
            if name in getattr(df, "columns", []):
                s = df[name]
                if getattr(s, "dtype", None) == object:
                    s = s.astype(str).str.replace("％", "%", regex=False).str.replace("%", "", regex=False).str.replace(",", "", regex=False).str.replace("+", "", regex=False)
                return pd.to_numeric(s, errors="coerce").fillna(default).astype(float)
        except Exception:
            continue
    return pd.Series(default, index=idx, dtype="float64")


def _coerce_ratio_series(s: Any):
    """Normalize percentage-like columns to ratio units.

    Ranking data may store 1.23 as percent or 0.0123 as ratio. Values whose
    absolute value is greater than 1 are treated as percent.
    """
    import pandas as pd
    x = pd.to_numeric(s, errors="coerce").fillna(0.0).astype(float)
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


def _repair_ranking_prefilter_features(df: Any) -> Any:
    try:
        import pandas as pd
        if df is None or getattr(df, "empty", True):
            return df
        out = df.copy()
        max_rank = max(1.0, _env_float("SUMMARY_AI_RANKING_REPAIR_MAX_RANK", 50.0))
        min_rank_score = max(0.0001, _env_float("SUMMARY_AI_RANKING_REPAIR_MIN_SCORE", 0.02))

        rank = _to_numeric_series(out, ("best_rank", "rank", "Rank", "順位", "source_rank", "current_rank", "ranking_rank"), 0.0)
        valid_rank = (rank > 0) & (rank <= max_rank)
        rank_score = ((max_rank + 1.0 - rank) / max_rank).where(valid_rank, 0.0).clip(lower=0.0)
        rank_score = rank_score.where(rank_score <= 0.0, rank_score.clip(lower=min_rank_score))

        current_score = _to_numeric_series(out, ("ranking_score",), 0.0)
        repaired_score = _max_series(current_score, rank_score)
        if repaired_score is not None:
            out["ranking_score"] = repaired_score

        change_raw = _to_numeric_series(out, ("price_delta_pct", "change_rate", "change_ratio", "change_percentage", "change_pct", "騰落率", "rate"), 0.0)
        change_ratio = _coerce_ratio_series(change_raw)
        # Preserve only upward momentum for BUY-side ranking pre-filter. SELL is handled later by normal candidate scoring.
        price_delta_pct = _max_series(_to_numeric_series(out, ("price_delta_pct",), 0.0), change_ratio)
        if price_delta_pct is not None:
            out["price_delta_pct"] = price_delta_pct
            out["ranking_momentum"] = _max_series(_to_numeric_series(out, ("ranking_momentum",), 0.0), price_delta_pct.clip(lower=0.0))

        rank_delta = _to_numeric_series(out, ("rank_improve", "rank_delta", "rank_diff", "rank_change", "順位変化"), 0.0)
        if "rank_improve" not in out.columns:
            out["rank_improve"] = rank_delta.clip(lower=0.0)
        else:
            out["rank_improve"] = _max_series(_to_numeric_series(out, ("rank_improve",), 0.0), rank_delta.clip(lower=0.0))

        volume_delta = _to_numeric_series(out, ("volume_delta", "volume_change", "volume_change_pct", "volume_ratio", "出来高変化", "出来高増加"), 0.0)
        if "volume_delta" not in out.columns:
            out["volume_delta"] = volume_delta.clip(lower=0.0)
        else:
            out["volume_delta"] = _max_series(_to_numeric_series(out, ("volume_delta",), 0.0), volume_delta.clip(lower=0.0))

        try:
            repaired = int(((pd.to_numeric(out.get("ranking_score"), errors="coerce").fillna(0.0) > 0)
                            | (pd.to_numeric(out.get("ranking_momentum"), errors="coerce").fillna(0.0) > 0)
                            | (pd.to_numeric(out.get("price_delta_pct"), errors="coerce").fillna(0.0) > 0)
                            | (pd.to_numeric(out.get("rank_improve"), errors="coerce").fillna(0.0) > 0)
                            | (pd.to_numeric(out.get("volume_delta"), errors="coerce").fillna(0.0) > 0)).sum())
            logger.warning(
                "[SUMMARY AI RANKING PREFILTER REPAIR] applied rows=%s signal_rows=%s rank_valid=%s max_rank=%.1f version=%s",
                len(out), repaired, int(valid_rank.sum()), max_rank, VERSION,
            )
        except Exception:
            pass
        return out
    except Exception:
        logger.exception("[SUMMARY AI RANKING PREFILTER REPAIR] failed version=%s", VERSION)
        return df


def install() -> bool:
    global _INSTALLED, _ORIG_APPLY_RANKING_PRE_FILTER
    if _INSTALLED:
        return True
    try:
        from trading.entry.summary_ai import runner
        cur = getattr(runner, "_apply_ranking_pre_filter", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI RANKING PREFILTER REPAIR] target missing version=%s", VERSION)
            return False
        if getattr(cur, "_summary_ai_ranking_prefilter_feature_repair_v1", False):
            _INSTALLED = True
            return True
        _ORIG_APPLY_RANKING_PRE_FILTER = getattr(cur, "_original", cur)

        @wraps(_ORIG_APPLY_RANKING_PRE_FILTER)
        def _patched_apply_ranking_pre_filter(df, *args, **kwargs):
            return _ORIG_APPLY_RANKING_PRE_FILTER(_repair_ranking_prefilter_features(df), *args, **kwargs)

        _patched_apply_ranking_pre_filter._summary_ai_ranking_prefilter_feature_repair_v1 = True  # type: ignore[attr-defined]
        _patched_apply_ranking_pre_filter._original = _ORIG_APPLY_RANKING_PRE_FILTER  # type: ignore[attr-defined]
        runner._apply_ranking_pre_filter = _patched_apply_ranking_pre_filter
        _INSTALLED = True
        logger.warning("[SUMMARY AI RANKING PREFILTER REPAIR] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI RANKING PREFILTER REPAIR] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI RANKING PREFILTER REPAIR] auto install failed")


__all__ = ["install", "VERSION"]
