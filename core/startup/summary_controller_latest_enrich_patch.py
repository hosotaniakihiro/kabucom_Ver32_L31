# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_controller_latest_enrich_patch.py
# Version: V3-SUMMARY-CONTROLLER-LATEST-RANKING-MTF-SCORE-REPAIR
# ------------------------------------------------------------
# Purpose:
#   summary_controller の df_latest が保存/cache/entryへ流れる直前に
#   controller_enrich.enrich_summary_latest() を通す。
#
# V2:
#   - buy_score/sell_score が空または全0の場合、score_buy/score_sell から復元する。
#   - display_score が全0の場合、final_score/score_total/score から復元する。
#
# V3:
#   - 2026-06-30 12:29ログで3分足 push_incremental_3min が
#     ranking_score/score_mtf/daily_mtf はnonzeroなのに、scoring_pipeline後に
#     score/score_buy/score_sell が全0へ戻った。
#   - score系が全0の場合だけ、ranking_score + daily MTF から軽量復元する。
# ============================================================
from __future__ import annotations

import logging
from functools import wraps
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V3-SUMMARY-CONTROLLER-LATEST-RANKING-MTF-SCORE-REPAIR"
_INSTALLED = False


def _nonzero(df: Any, col: str) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or col not in df.columns:
            return 0
        return int(pd.to_numeric(df[col], errors="coerce").fillna(0).ne(0).sum())
    except Exception:
        return 0


def _num_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    try:
        if col not in df.columns:
            return pd.Series(default, index=df.index, dtype="float64")
        return pd.to_numeric(df[col], errors="coerce").replace([float("inf"), float("-inf")], pd.NA).fillna(default).astype("float64")
    except Exception:
        return pd.Series(default, index=getattr(df, "index", None), dtype="float64")


def _ensure_numeric_col(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        df[col] = 0.0
    return _num_series(df, col, 0.0)


def _first_nonzero(df: pd.DataFrame, cols: tuple[str, ...]) -> pd.Series:
    out = pd.Series(0.0, index=df.index, dtype="float64")
    for c in cols:
        if c not in df.columns:
            continue
        s = _num_series(df, c, 0.0)
        out = out.where(out.ne(0), s)
    return out.fillna(0.0)


def _repair_zero_scores_from_ranking_mtf(df: pd.DataFrame, *, interval: int | None, context: str) -> pd.DataFrame:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return df
        out = df.copy()
        score_before = _nonzero(out, "score")
        buy_before = _nonzero(out, "score_buy")
        sell_before = _nonzero(out, "score_sell")
        if score_before or buy_before or sell_before:
            return out

        rank = _first_nonzero(out, ("ranking_score", "ranking_score_total"))
        mtf = _first_nonzero(out, ("score_mtf", "mtf_score", "mtf", "mtf_alignment", "score_mtf_daily"))
        daily_buy = _first_nonzero(out, ("daily_mtf_buy", "daily_mtf_buy_daily_src"))
        daily_sell = _first_nonzero(out, ("daily_mtf_sell", "daily_mtf_sell_daily_src"))

        # ranking_score は 0〜1 程度、MTFは 0〜6.5 程度なので、rankは補助点にする。
        rank_boost = rank.clip(lower=0.0, upper=1.5) * 2.0
        buy_score = (daily_buy.clip(lower=0.0) + rank_boost).where(daily_buy.ne(0), 0.0)
        sell_score = (daily_sell.clip(lower=0.0) + rank_boost).where(daily_sell.ne(0), 0.0)

        # daily_mtf が無いが mtf/score_mtf だけある場合は、方向不明なので両側に小さく載せる。
        no_side = buy_score.eq(0) & sell_score.eq(0) & mtf.ne(0)
        if bool(no_side.any()):
            neutral = (mtf.clip(lower=0.0, upper=6.5) * 0.5 + rank_boost).fillna(0.0)
            buy_score = buy_score.where(~no_side, neutral)
            sell_score = sell_score.where(~no_side, neutral)

        has_any = buy_score.ne(0) | sell_score.ne(0)
        if not bool(has_any.any()):
            return out

        signed = buy_score - sell_score
        display = pd.concat([buy_score.abs(), sell_score.abs(), mtf.abs(), rank_boost.abs()], axis=1).max(axis=1).fillna(0.0)

        for col in ("score_buy", "buy_score"):
            out[col] = _ensure_numeric_col(out, col).where(_ensure_numeric_col(out, col).ne(0), buy_score)
        for col in ("score_sell", "sell_score"):
            out[col] = _ensure_numeric_col(out, col).where(_ensure_numeric_col(out, col).ne(0), sell_score)
        for col in ("score", "score_total", "final_score"):
            out[col] = _ensure_numeric_col(out, col).where(_ensure_numeric_col(out, col).ne(0), signed)
        out["display_score"] = _ensure_numeric_col(out, "display_score").where(_ensure_numeric_col(out, "display_score").ne(0), display)
        out["summary_score_repaired_from_ranking_mtf"] = has_any

        logger.warning(
            "[SUMMARY CONTROLLER SCORE REPAIR] context=%s interval=%s rows=%s repaired=%s score %s->%s buy %s->%s sell %s->%s",
            context,
            interval,
            len(out),
            int(has_any.sum()),
            score_before,
            _nonzero(out, "score"),
            buy_before,
            _nonzero(out, "score_buy"),
            sell_before,
            _nonzero(out, "score_sell"),
        )
        return out
    except Exception:
        logger.debug("[SUMMARY CONTROLLER SCORE REPAIR] failed context=%s interval=%s", context, interval, exc_info=True)
        return df


def _repair_score_aliases(df: Any, *, interval: int | None = None, context: str = "runtime") -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    try:
        out = df.copy()
        changed: dict[str, tuple[int, int]] = {}

        out = _repair_zero_scores_from_ranking_mtf(out, interval=interval, context=context)

        for dst, src in (("buy_score", "score_buy"), ("sell_score", "score_sell")):
            if src in out.columns:
                before = _nonzero(out, dst)
                src_s = _num_series(out, src, 0.0)
                dst_s = _ensure_numeric_col(out, dst)
                if before == 0 and int(src_s.ne(0).sum()) > 0:
                    out[dst] = src_s
                else:
                    out[dst] = dst_s.where(dst_s.ne(0), src_s)
                after = _nonzero(out, dst)
                if after != before:
                    changed[dst] = (before, after)

        if "display_score" in out.columns:
            before = _nonzero(out, "display_score")
            disp = _ensure_numeric_col(out, "display_score")
            if before == 0:
                for src in ("final_score", "score_total", "score"):
                    if src in out.columns:
                        src_s = _num_series(out, src, 0.0)
                        if int(src_s.ne(0).sum()) > 0:
                            out["display_score"] = src_s.abs()
                            break
            else:
                for src in ("final_score", "score_total", "score"):
                    if src in out.columns:
                        src_s = _num_series(out, src, 0.0).abs()
                        out["display_score"] = disp.where(disp.ne(0), src_s)
                        break
            after = _nonzero(out, "display_score")
            if after != before:
                changed["display_score"] = (before, after)

        if changed:
            logger.warning(
                "[SUMMARY CONTROLLER SCORE ALIAS REPAIR] context=%s interval=%s rows=%s changed=%s",
                context,
                interval,
                len(out),
                changed,
            )
        return out
    except Exception:
        logger.debug("[SUMMARY CONTROLLER SCORE ALIAS REPAIR] failed context=%s interval=%s", context, interval, exc_info=True)
        return df


def _enrich(df: Any, *, interval: int | None = None, context: str = "runtime") -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return df
    try:
        from trading.summary.controller_enrich import enrich_summary_latest
        before_rank = _nonzero(df, "ranking_score")
        out = enrich_summary_latest(df, interval=int(interval or 1), context=context)
        out = _repair_score_aliases(out, interval=interval, context=context)
        after_rank = _nonzero(out, "ranking_score")
        if after_rank != before_rank or "ranking_score" not in df.columns:
            logger.warning(
                "[SUMMARY CONTROLLER LATEST ENRICH] context=%s interval=%s rows=%s ranking_score_nonzero %s->%s",
                context,
                interval,
                len(out) if isinstance(out, pd.DataFrame) else len(df),
                before_rank,
                after_rank,
            )
        return out if isinstance(out, pd.DataFrame) else df
    except Exception:
        logger.debug("[SUMMARY CONTROLLER LATEST ENRICH] enrich failed context=%s interval=%s", context, interval, exc_info=True)
        return _repair_score_aliases(df, interval=interval, context=context)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.summary.summary_controller as sc

        old_save = getattr(sc, "save_summary", None)
        if callable(old_save) and not getattr(old_save, "_latest_enrich_v3", False):
            @wraps(old_save)
            def _save_summary_enriched(df, interval, *args, **kwargs):
                return old_save(_enrich(df, interval=int(interval), context="before-save"), interval, *args, **kwargs)
            _save_summary_enriched._latest_enrich_v3 = True  # type: ignore[attr-defined]
            _save_summary_enriched._latest_enrich_v2 = True  # type: ignore[attr-defined]
            _save_summary_enriched._latest_enrich_v1 = True  # type: ignore[attr-defined]
            _save_summary_enriched._original = old_save  # type: ignore[attr-defined]
            sc.save_summary = _save_summary_enriched

        old_run_ranking = getattr(sc, "run_ranking_pipeline", None)
        if callable(old_run_ranking) and not getattr(old_run_ranking, "_latest_enrich_v3", False):
            @wraps(old_run_ranking)
            def _run_ranking_enriched(df_latest, interval, *args, **kwargs):
                return old_run_ranking(_enrich(df_latest, interval=int(interval), context="before-ranking-pipeline"), interval, *args, **kwargs)
            _run_ranking_enriched._latest_enrich_v3 = True  # type: ignore[attr-defined]
            _run_ranking_enriched._latest_enrich_v2 = True  # type: ignore[attr-defined]
            _run_ranking_enriched._latest_enrich_v1 = True  # type: ignore[attr-defined]
            _run_ranking_enriched._original = old_run_ranking  # type: ignore[attr-defined]
            sc.run_ranking_pipeline = _run_ranking_enriched

        old_log_probe = getattr(sc, "log_scoring_probe", None)
        if callable(old_log_probe) and not getattr(old_log_probe, "_latest_enrich_v3", False):
            @wraps(old_log_probe)
            def _log_probe_enriched(label, interval, df, *args, **kwargs):
                return old_log_probe(label, interval, _enrich(df, interval=int(interval), context=f"log-{label}"), *args, **kwargs)
            _log_probe_enriched._latest_enrich_v3 = True  # type: ignore[attr-defined]
            _log_probe_enriched._latest_enrich_v2 = True  # type: ignore[attr-defined]
            _log_probe_enriched._latest_enrich_v1 = True  # type: ignore[attr-defined]
            _log_probe_enriched._original = old_log_probe  # type: ignore[attr-defined]
            sc.log_scoring_probe = _log_probe_enriched

        old_set_latest = getattr(sc, "safe_global_set_latest", None)
        if callable(old_set_latest) and not getattr(old_set_latest, "_latest_enrich_v3", False):
            @wraps(old_set_latest)
            def _set_latest_enriched(interval, df, *args, **kwargs):
                return old_set_latest(interval, _enrich(df, interval=int(interval), context="cache-latest"), *args, **kwargs)
            _set_latest_enriched._latest_enrich_v3 = True  # type: ignore[attr-defined]
            _set_latest_enriched._latest_enrich_v2 = True  # type: ignore[attr-defined]
            _set_latest_enriched._latest_enrich_v1 = True  # type: ignore[attr-defined]
            _set_latest_enriched._original = old_set_latest  # type: ignore[attr-defined]
            sc.safe_global_set_latest = _set_latest_enriched

        _INSTALLED = True
        logger.warning("[SUMMARY CONTROLLER LATEST ENRICH] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY CONTROLLER LATEST ENRICH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY CONTROLLER LATEST ENRICH] auto install failed")

__all__ = ["VERSION", "install"]
