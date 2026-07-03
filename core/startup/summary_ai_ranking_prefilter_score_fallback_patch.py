# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_ranking_prefilter_score_fallback_patch.py
# Version: V1-RANKING-PREFILTER-SCORE-FALLBACK
# ------------------------------------------------------------
# RANKING source の summary_ai runner は ranking_score / ranking_momentum 等が
# 全0の場合、RANKING_PRE_FILTER before=N after=0 で候補を全落ちさせる。
# ただし、既存の score_buy / score_sell / score_mtf / score_slope 等に
# 有効なスコアがある場合は、それを ranking_score / ranking_momentum へ
# 橋渡しする。
#
# 低スコア行を無条件通過させる fail-open ではない。
# ============================================================
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-RANKING-PREFILTER-SCORE-FALLBACK"
_INSTALLED = False


def _num_series(df: Any, names: tuple[str, ...], default: float = 0.0):
    import pandas as pd
    idx = getattr(df, "index", None)
    for name in names:
        try:
            if name in getattr(df, "columns", []):
                return pd.to_numeric(df[name], errors="coerce").fillna(default).astype(float)
        except Exception:
            continue
    return pd.Series(default, index=idx, dtype="float64")


def _max_series(*series: Any):
    import pandas as pd
    frames = []
    for s in series:
        try:
            frames.append(pd.to_numeric(s, errors="coerce").fillna(0.0).astype(float).abs())
        except Exception:
            pass
    if not frames:
        return None
    return pd.concat(frames, axis=1).max(axis=1).fillna(0.0)


def _prepare_ranking_scores(df: Any) -> Any:
    try:
        import pandas as pd
        if not isinstance(df, pd.DataFrame) or df.empty:
            return df
        out = df.copy()
        buy = _max_series(
            _num_series(out, ("score_buy", "buy_score", "ai_disp_buy_score", "config_buy_score", "buy"), 0.0),
            _num_series(out, ("score_total", "total_score", "final_score", "display_score", "score"), 0.0).clip(lower=0.0),
        )
        sell = _max_series(
            _num_series(out, ("score_sell", "sell_score", "ai_disp_sell_score", "config_sell_score", "sell"), 0.0),
            (-_num_series(out, ("score_total", "total_score", "final_score", "display_score", "score"), 0.0)).clip(lower=0.0),
        )
        mtf = _num_series(out, ("score_mtf", "mtf_score", "mtf"), 0.0).abs()
        slope = _num_series(out, ("score_slope", "slope_atr_scaled", "slope"), 0.0).abs()
        effective = _max_series(buy, sell, mtf, slope)
        if effective is None:
            return df
        effective = pd.Series(effective, index=out.index).fillna(0.0).astype(float)

        for col in ("ranking_score", "ranking_momentum", "score_buy", "buy_score", "score_sell", "sell_score"):
            if col not in out.columns:
                out[col] = 0.0
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0).astype(float)

        # ranking_score は売買どちらでも候補として残すため abs effective を使う。
        out["ranking_score"] = out["ranking_score"].where(out["ranking_score"].abs() > 0, effective)
        out["ranking_momentum"] = out["ranking_momentum"].where(out["ranking_momentum"].abs() > 0, _max_series(mtf, slope).fillna(0.0))

        # 後段AI gateが見る列も、既存スコアがあれば補完する。
        out["score_buy"] = out["score_buy"].where(out["score_buy"].abs() > 0, buy.fillna(0.0))
        out["buy_score"] = out["buy_score"].where(out["buy_score"].abs() > 0, buy.fillna(0.0))
        out["score_sell"] = out["score_sell"].where(out["score_sell"].abs() > 0, sell.fillna(0.0))
        out["sell_score"] = out["sell_score"].where(out["sell_score"].abs() > 0, sell.fillna(0.0))

        logger.warning(
            "[SUMMARY AI RANKING PREFILTER FALLBACK] prepared rows=%s effective_pos=%s buy_pos=%s sell_pos=%s rank_pos=%s momentum_pos=%s version=%s",
            len(out), int((effective > 0).sum()), int((out["score_buy"] > 0).sum()), int((out["score_sell"] > 0).sum()), int((out["ranking_score"] > 0).sum()), int((out["ranking_momentum"] > 0).sum()), VERSION,
        )
        return out
    except Exception:
        logger.exception("[SUMMARY AI RANKING PREFILTER FALLBACK] prepare failed version=%s", VERSION)
        return df


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.entry.summary_ai.runner as runner
        cur = getattr(runner, "_apply_ranking_pre_filter", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI RANKING PREFILTER FALLBACK] target missing version=%s", VERSION)
            return False
        if getattr(cur, "_summary_ai_ranking_prefilter_score_fallback_v1", False):
            _INSTALLED = True
            return True
        base = getattr(cur, "_original", cur)

        def _patched_apply_ranking_pre_filter(df, *args, **kwargs):
            prepared = _prepare_ranking_scores(df)
            out = base(prepared, *args, **kwargs)
            try:
                if getattr(out, "empty", True) and not getattr(prepared, "empty", True):
                    import pandas as pd
                    rank = pd.to_numeric(prepared.get("ranking_score", 0), errors="coerce").fillna(0.0)
                    fallback = prepared[rank > 0].copy()
                    if not fallback.empty:
                        logger.warning(
                            "[SUMMARY AI RANKING PREFILTER FALLBACK] original empty -> score fallback rows=%s source=%s interval=%s version=%s",
                            len(fallback), kwargs.get("source"), kwargs.get("interval"), VERSION,
                        )
                        return fallback
            except Exception:
                logger.debug("[SUMMARY AI RANKING PREFILTER FALLBACK] post fallback skipped", exc_info=True)
            return out

        _patched_apply_ranking_pre_filter._summary_ai_ranking_prefilter_score_fallback_v1 = True  # type: ignore[attr-defined]
        _patched_apply_ranking_pre_filter._original = base  # type: ignore[attr-defined]
        runner._apply_ranking_pre_filter = _patched_apply_ranking_pre_filter
        _INSTALLED = True
        logger.warning("[SUMMARY AI RANKING PREFILTER FALLBACK] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI RANKING PREFILTER FALLBACK] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI RANKING PREFILTER FALLBACK] auto install failed")


__all__ = ["install", "VERSION"]
