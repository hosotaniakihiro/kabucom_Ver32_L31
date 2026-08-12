# ============================================================
# File   : core/startup/summary_controller_neutral_score_guard_patch.py
# Version: V1-SUMMARY-CONTROLLER-NEUTRAL-SCORE-GUARD
# ------------------------------------------------------------
# Purpose:
#   summary_controller / global_context 投入直前で、短履歴・中立指標なのに
#   score=-1 / score_sell=1 が復活する問題を抑制する。
#
# Fix:
#   - controller_cache.attach_display_ready
#   - controller_projection rebuild/latest 系
#   - core.global_context.context の set/put 系候補
#   を監視して、DataFrame を保存・返却する直前に neutral SELL を 0 化する。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_WATCHER_STARTED = False
_ORIGINALS: dict[str, Callable[..., Any]] = {}

_TRUE_SET = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE_SET = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE_SET:
            return True
        if s in _FALSE_SET:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(str(raw).replace(",", ""))
    except Exception:
        return float(default)


def _num(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").replace([float("inf"), float("-inf")], pd.NA).fillna(default)


def _safe_df(x: Any) -> pd.DataFrame:
    if isinstance(x, pd.DataFrame):
        return x.copy()
    try:
        return pd.DataFrame(x).copy()
    except Exception:
        return pd.DataFrame()


def neutralize_summary_scores(df: Any, *, context: str = "") -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    if not _env_bool("SUMMARY_CONTROLLER_NEUTRAL_SCORE_GUARD_ENABLED", True):
        return df
    out = _safe_df(df)
    try:
        score = _num(out, "score", 0.0)
        score_sell = _num(out, "score_sell", _num(out, "sell_score", 0.0))
        score_buy = _num(out, "score_buy", _num(out, "buy_score", 0.0))
        slope = _num(out, "slope", 0.0).abs()
        slope_atr = _num(out, "slope_atr_scaled", 0.0).abs()
        score_slope = _num(out, "score_slope", 0.0).abs()
        rsi = _num(out, "rsi", 50.0)
        macd = _num(out, "macd", 0.0).abs()
        signal = _num(out, "signal", 0.0).abs()
        hist_len = _num(out, "symbol_hist_len", 1.0)
        mtf = _num(out, "score_mtf", _num(out, "mtf", 0.0)).abs()

        neutral = (
            (slope <= 1e-12)
            & (slope_atr <= 1e-12)
            & (score_slope <= 1e-12)
            & ((rsi - 50.0).abs() <= 1e-9)
            & (macd <= 1e-12)
            & (signal <= 1e-12)
        )
        default_sell = (score < 0) & (score_sell > 0) & (score_buy <= 0)
        # symbol_hist_len が過去patchで3以上に補正されていても、中立指標なら抑制する。
        # ただし強いMTFだけは残したい場合があるため、既定ではmtf<2を抑制対象にする。
        max_mtf = _env_float("SUMMARY_CONTROLLER_NEUTRAL_SCORE_GUARD_MAX_MTF", 2.0)
        mask = neutral & default_sell & ((hist_len < 5) | (mtf < max_mtf))
        n = int(mask.sum())
        if n <= 0:
            return out
        for col in ("score", "score_total", "total_score", "final_score", "display_score", "score_sell", "sell_score"):
            if col in out.columns:
                out.loc[mask, col] = 0.0
        for col in ("score_buy", "buy_score"):
            if col in out.columns:
                out.loc[mask, col] = 0.0
        out.loc[mask, "neutral_default_score_suppressed"] = True
        logger.warning(
            "[SUMMARY CONTROLLER NEUTRAL SCORE GUARD] suppressed context=%s rows=%s suppressed=%s score_nonzero_after=%s sell_nonzero_after=%s",
            context,
            len(out),
            n,
            int((_num(out, "score", 0.0) != 0).sum()),
            int((_num(out, "score_sell", 0.0) != 0).sum()),
        )
    except Exception:
        logger.exception("[SUMMARY CONTROLLER NEUTRAL SCORE GUARD] failed context=%s", context)
        return df
    return out


def _wrap_df_function(mod: Any, name: str) -> bool:
    try:
        fn = getattr(mod, name, None)
        if not callable(fn) or getattr(fn, "_summary_controller_neutral_score_guard_v1", False):
            return False
        key = f"{getattr(mod, '__name__', 'module')}.{name}"
        _ORIGINALS[key] = fn

        def wrapped(*args, **kwargs):
            new_args = args
            if args and isinstance(args[0], pd.DataFrame):
                new_args = (neutralize_summary_scores(args[0], context=f"pre:{key}"),) + tuple(args[1:])
            ret = fn(*new_args, **kwargs)
            if isinstance(ret, pd.DataFrame):
                return neutralize_summary_scores(ret, context=f"post:{key}")
            return ret

        wrapped._summary_controller_neutral_score_guard_v1 = True  # type: ignore[attr-defined]
        setattr(mod, name, wrapped)
        logger.warning("[SUMMARY CONTROLLER NEUTRAL SCORE GUARD] patched %s", key)
        return True
    except Exception:
        logger.exception("[SUMMARY CONTROLLER NEUTRAL SCORE GUARD] patch failed %s.%s", mod, name)
        return False


def _patch_known_modules() -> int:
    patched = 0
    try:
        import trading.summary.controller_cache as cc
        for name in ("attach_display_ready",):
            patched += int(_wrap_df_function(cc, name))
    except Exception:
        pass
    try:
        import trading.summary.controller_projection as cp
        for name in ("rebuild_display_ready", "rebuild_technical_ready", "latest_row_per_symbol", "latest_row_per_symbol_mature_first"):
            patched += int(_wrap_df_function(cp, name))
    except Exception:
        pass
    return patched


def _watcher_loop() -> None:
    interval = max(1.0, _env_float("SUMMARY_CONTROLLER_NEUTRAL_SCORE_GUARD_WATCH_SEC", 2.0))
    while True:
        try:
            if not _env_bool("SUMMARY_CONTROLLER_NEUTRAL_SCORE_GUARD_ENABLED", True):
                return
            _patch_known_modules()
        except Exception:
            logger.exception("[SUMMARY CONTROLLER NEUTRAL SCORE GUARD] watcher error")
        time.sleep(interval)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    if not _env_bool("SUMMARY_CONTROLLER_NEUTRAL_SCORE_GUARD_ENABLED", True):
        logger.warning("[SUMMARY CONTROLLER NEUTRAL SCORE GUARD] disabled by env")
        return False
    patched = _patch_known_modules()
    if not _WATCHER_STARTED and _env_bool("SUMMARY_CONTROLLER_NEUTRAL_SCORE_GUARD_WATCHER", True):
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher_loop, name="summary-controller-neutral-score-guard", daemon=True).start()
        logger.warning("[SUMMARY CONTROLLER NEUTRAL SCORE GUARD] watcher started")
    _INSTALLED = True
    logger.warning("[SUMMARY CONTROLLER NEUTRAL SCORE GUARD] installed V1 patched=%s", patched)
    return True


try:
    install()
except Exception:
    logger.exception("[SUMMARY CONTROLLER NEUTRAL SCORE GUARD] auto install failed")


__all__ = ["install", "neutralize_summary_scores"]
