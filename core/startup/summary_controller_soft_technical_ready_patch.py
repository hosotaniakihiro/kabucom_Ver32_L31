# ============================================================
# File   : core/startup/summary_controller_soft_technical_ready_patch.py
# Version: V1-SOFT-TECHNICAL-READY-FOR-SHORT-HISTORY-PUSH
# ------------------------------------------------------------
# 目的:
#   summary_controller の rebuild_technical_ready が短履歴を厳格に落とし、
#   rsi/macd/slope/score が存在しても technical_ready=False のままになる問題を補正する。
#
# 方針:
#   - 元の厳格判定は維持する。
#   - その後、display_ready + close + score + 何らかの指標がある行を soft_ready=True にする。
#   - controller_projection.rebuild_technical_ready と、summary_controller が import 済みの
#     rebuild_technical_ready 参照の両方を差し替える。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False


def _num(df: pd.DataFrame, col: str) -> pd.Series:
    try:
        if col in df.columns:
            return pd.to_numeric(df[col], errors="coerce")
    except Exception:
        pass
    return pd.Series(pd.NA, index=df.index, dtype="float64")


def _bool(df: pd.DataFrame, col: str, default: bool = False) -> pd.Series:
    try:
        if col in df.columns:
            s = df[col]
            if getattr(s, "dtype", None) == bool:
                return s.fillna(False).astype(bool)
            return s.fillna(False).astype(str).str.lower().str.strip().isin(["1", "true", "yes", "on"])
    except Exception:
        pass
    return pd.Series(default, index=df.index, dtype="bool")


def _build_soft_ready(out: pd.DataFrame) -> pd.Series:
    idx = out.index
    try:
        if "symbol" in out.columns:
            symbol_ok = out["symbol"].fillna("").astype(str).str.strip().ne("")
        else:
            symbol_ok = pd.Series(False, index=idx)

        close_ok = pd.Series(False, index=idx)
        for c in ("close", "close_price", "price", "current_price"):
            s = _num(out, c)
            close_ok = close_ok | (s.notna() & s.fillna(0).ne(0))

        score_ok = pd.Series(False, index=idx)
        for c in ("score", "score_buy", "score_sell", "buy_score", "sell_score", "score_total", "final_score", "display_score"):
            s = _num(out, c)
            score_ok = score_ok | s.notna()

        indicator_ok = pd.Series(False, index=idx)
        for c in ("rsi", "macd", "signal", "hist", "slope", "slope_atr_scaled", "score_slope", "mtf", "score_mtf", "mtf_score", "ma5", "ma25", "ma75"):
            s = _num(out, c)
            indicator_ok = indicator_ok | s.notna()

        display_ok = _bool(out, "display_ready", True)
        return (symbol_ok & close_ok & score_ok & indicator_ok & display_ok).fillna(False).astype(bool)
    except Exception:
        logger.debug("[SUMMARY SOFT TECH READY] soft mask failed", exc_info=True)
        return pd.Series(False, index=idx, dtype="bool")


def _wrap_rebuild(orig: Any):
    def rebuild_technical_ready_patched(df: pd.DataFrame) -> pd.DataFrame:
        try:
            out = orig(df) if callable(orig) else df
        except Exception:
            logger.exception("[SUMMARY SOFT TECH READY] original rebuild failed")
            out = df

        if not isinstance(out, pd.DataFrame) or out.empty:
            return out

        try:
            if "technical_ready" not in out.columns:
                out["technical_ready"] = False
            before = int(pd.Series(out["technical_ready"]).fillna(False).astype(bool).sum())
            soft = _build_soft_ready(out)
            old = pd.Series(out["technical_ready"]).fillna(False).astype(bool)
            out["technical_ready"] = (old | soft).fillna(False).astype(bool)
            after = int(pd.Series(out["technical_ready"]).fillna(False).astype(bool).sum())
            if "usable_technical_ready" not in out.columns:
                out["usable_technical_ready"] = soft.fillna(False).astype(bool)
            else:
                out["usable_technical_ready"] = (_bool(out, "usable_technical_ready", False) | soft).fillna(False).astype(bool)
            logger.warning(
                "[SUMMARY SOFT TECH READY] applied rows=%s technical_ready %s->%s soft_ready=%s",
                len(out), before, after, int(soft.sum())
            )
        except Exception:
            logger.exception("[SUMMARY SOFT TECH READY] apply failed")
        return out

    rebuild_technical_ready_patched._soft_technical_ready_patch = True  # type: ignore[attr-defined]
    rebuild_technical_ready_patched._original = orig  # type: ignore[attr-defined]
    return rebuild_technical_ready_patched


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    try:
        import trading.summary.controller_projection as projection
    except Exception:
        logger.exception("[SUMMARY SOFT TECH READY] projection import failed")
        return False

    try:
        orig = getattr(projection, "rebuild_technical_ready", None)
        if not callable(orig):
            logger.warning("[SUMMARY SOFT TECH READY] rebuild_technical_ready not callable")
            return False
        if getattr(orig, "_soft_technical_ready_patch", False):
            patched = orig
        else:
            patched = _wrap_rebuild(orig)
            projection.rebuild_technical_ready = patched

        # summary_controller は from controller_projection import rebuild_technical_ready 済みなので、参照も差し替える。
        try:
            import trading.summary.summary_controller as summary_controller
            summary_controller.rebuild_technical_ready = patched
        except Exception:
            logger.debug("[SUMMARY SOFT TECH READY] summary_controller ref patch skipped", exc_info=True)

        _PATCHED = True
        logger.warning("[SUMMARY SOFT TECH READY] installed")
        return True
    except Exception:
        logger.exception("[SUMMARY SOFT TECH READY] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY SOFT TECH READY] auto install failed")


__all__ = ["install"]
