# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from functools import wraps

logger = logging.getLogger(__name__)
VERSION = "V1-BEST-RANK-BRIDGE"
_INSTALLED = False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import pandas as pd
        import trading.entry.summary_ai.runner as runner
        cur = getattr(runner, "_apply_ranking_pre_filter", None)
        if not callable(cur):
            return False
        if getattr(cur, "_best_rank_bridge_v1", False):
            _INSTALLED = True
            return True
        base = getattr(cur, "_original", cur)

        @wraps(base)
        def wrapped(df, *args, **kwargs):
            x = df
            try:
                if isinstance(df, pd.DataFrame) and not df.empty:
                    x = df.copy()
                    rank = None
                    for c in ("best_rank_position", "best_rank", "rank_position", "ranking_rank", "rank", "No", "no"):
                        if c in x.columns:
                            r = pd.to_numeric(x[c], errors="coerce").fillna(0.0)
                            if r.gt(0).any():
                                rank = r
                                break
                    if rank is not None:
                        score = ((51.0 - rank).clip(lower=1.0, upper=50.0) / 50.0).where(rank.gt(0) & rank.le(50), 0.0)
                        for c in ("ranking_score", "ranking_momentum", "score_buy", "buy_score"):
                            if c not in x.columns:
                                x[c] = 0.0
                            s = pd.to_numeric(x[c], errors="coerce").fillna(0.0)
                            x[c] = s.where(s.abs() > 0, score)
                        logger.warning("[SUMMARY AI BEST RANK BRIDGE] prepared rows=%s rank_pos=%s version=%s", len(x), int(score.gt(0).sum()), VERSION)
            except Exception:
                logger.exception("[SUMMARY AI BEST RANK BRIDGE] prepare failed version=%s", VERSION)
                x = df
            out = base(x, *args, **kwargs)
            try:
                if getattr(out, "empty", True) and isinstance(x, pd.DataFrame) and "ranking_score" in x.columns:
                    keep = x[pd.to_numeric(x["ranking_score"], errors="coerce").fillna(0.0).gt(0)].copy()
                    if not keep.empty:
                        logger.warning("[SUMMARY AI BEST RANK BRIDGE] original empty -> keep rows=%s version=%s", len(keep), VERSION)
                        return keep
            except Exception:
                pass
            return out

        wrapped._best_rank_bridge_v1 = True  # type: ignore[attr-defined]
        wrapped._original = base  # type: ignore[attr-defined]
        runner._apply_ranking_pre_filter = wrapped
        _INSTALLED = True
        logger.warning("[SUMMARY AI BEST RANK BRIDGE] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI BEST RANK BRIDGE] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI BEST RANK BRIDGE] auto install failed")
