# ============================================================
# File   : core/startup/summary_controller_ready_mtf_patch.py
# Version: V1.0-CONTROLLER-BEFORE-CACHE-MTF-READY
# ------------------------------------------------------------
# 目的:
#   summary_controller の before-cache-sync 時点で
#   mtf/score_mtf/mtf_score が 0、technical_ready=False のまま残る問題を補正する。
#
# 背景:
#   後段の safe_io / AI 直前では Daily MTF が付与されるが、
#   before-cache-sync ログ時点では mtf=0 のままになり、
#   cache/merged summary へ弱い状態が流れることがある。
#
# 修正内容:
#   - controller_cache.attach_display_ready をpatch
#   - controller_projection.rebuild_display_ready をpatch
#   - controller_projection.rebuild_technical_ready をpatch
#   - 日足MTFを早めにmerge
#   - score_mtf/mtf/mtf_score alias を揃える
#   - macd/signal/rsi/slope/mtf のいずれかが有効なら technical_ready を復元
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINALS: dict[str, Callable] = {}


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _safe_df(x: Any) -> pd.DataFrame:
    if isinstance(x, pd.DataFrame):
        return x.copy()
    try:
        return pd.DataFrame(x).copy()
    except Exception:
        return pd.DataFrame()


def _nonzero_count(df: pd.DataFrame, col: str) -> int:
    try:
        if df.empty or col not in df.columns:
            return -1
        return int((pd.to_numeric(df[col], errors="coerce").fillna(0) != 0).sum())
    except Exception:
        return -1


def _bool_count(df: pd.DataFrame, col: str) -> int:
    try:
        if df.empty or col not in df.columns:
            return -1
        return int(pd.Series(df[col]).fillna(False).astype(bool).sum())
    except Exception:
        return -1


def _normalize_symbol(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty:
        return out
    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    return out


def _merge_daily_mtf_early(df: pd.DataFrame, *, context: str) -> pd.DataFrame:
    out = _normalize_symbol(df)
    if out.empty or "symbol" not in out.columns:
        return out
    if not _env_bool("SUMMARY_CONTROLLER_EARLY_DAILY_MTF", True):
        return out
    try:
        from trading.summary.mtf.daily_runtime_patch import merge_daily_mtf_for_ai

        before = _nonzero_count(out, "score_mtf")
        merged = merge_daily_mtf_for_ai(out, source=f"SUMMARY_CONTROLLER_EARLY-{context}")
        out2 = _normalize_symbol(merged)
        logger.warning(
            "[SUMMARY CONTROLLER READY MTF PATCH] daily merge context=%s rows=%s score_mtf %s->%s mtf=%s mtf_score=%s",
            context,
            len(out2),
            before,
            _nonzero_count(out2, "score_mtf"),
            _nonzero_count(out2, "mtf"),
            _nonzero_count(out2, "mtf_score"),
        )
        return out2
    except Exception:
        logger.exception("[SUMMARY CONTROLLER READY MTF PATCH] daily mtf merge failed context=%s", context)
        return out


def _align_mtf_aliases(df: pd.DataFrame, *, context: str) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty:
        return out
    try:
        for col in ("mtf", "score_mtf", "mtf_score"):
            if col not in out.columns:
                out[col] = 0.0

        s_mtf = pd.to_numeric(out["mtf"], errors="coerce")
        s_score = pd.to_numeric(out["score_mtf"], errors="coerce")
        s_mtf_score = pd.to_numeric(out["mtf_score"], errors="coerce")

        # score_mtf が入っていて mtf/mtf_score が 0 の場合は横展開
        score_valid = s_score.fillna(0) != 0
        mtf_zero = s_mtf.fillna(0) == 0
        mtf_score_zero = s_mtf_score.fillna(0) == 0
        out.loc[score_valid & mtf_zero, "mtf"] = s_score.loc[score_valid & mtf_zero]
        out.loc[score_valid & mtf_score_zero, "mtf_score"] = s_score.loc[score_valid & mtf_score_zero]

        # mtf 側だけ入っている場合も score_mtf/mtf_score へ横展開
        s_mtf = pd.to_numeric(out["mtf"], errors="coerce")
        mtf_valid = s_mtf.fillna(0) != 0
        s_score = pd.to_numeric(out["score_mtf"], errors="coerce")
        s_mtf_score = pd.to_numeric(out["mtf_score"], errors="coerce")
        out.loc[mtf_valid & (s_score.fillna(0) == 0), "score_mtf"] = s_mtf.loc[mtf_valid & (s_score.fillna(0) == 0)]
        out.loc[mtf_valid & (s_mtf_score.fillna(0) == 0), "mtf_score"] = s_mtf.loc[mtf_valid & (s_mtf_score.fillna(0) == 0)]

        logger.warning(
            "[SUMMARY CONTROLLER READY MTF PATCH] alias context=%s rows=%s mtf=%s score_mtf=%s mtf_score=%s",
            context,
            len(out),
            _nonzero_count(out, "mtf"),
            _nonzero_count(out, "score_mtf"),
            _nonzero_count(out, "mtf_score"),
        )
    except Exception:
        logger.exception("[SUMMARY CONTROLLER READY MTF PATCH] align mtf alias failed context=%s", context)
    return out


def _relax_technical_ready(df: pd.DataFrame, *, context: str) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty:
        return out
    try:
        idx = out.index
        if "technical_ready" not in out.columns:
            out["technical_ready"] = False

        symbol_ok = out["symbol"].fillna("").astype(str).str.strip().ne("") if "symbol" in out.columns else pd.Series(False, index=idx)
        close_ok = pd.Series(False, index=idx)
        for col in ("close", "close_price", "price", "current_price"):
            if col in out.columns:
                s = pd.to_numeric(out[col], errors="coerce")
                close_ok = close_ok | (s.notna() & s.fillna(0).ne(0))

        macd_ok = pd.Series(False, index=idx)
        if "macd" in out.columns:
            macd_ok = macd_ok | pd.to_numeric(out["macd"], errors="coerce").fillna(0).ne(0)
        if "signal" in out.columns:
            macd_ok = macd_ok | pd.to_numeric(out["signal"], errors="coerce").fillna(0).ne(0)

        slope_ok = pd.Series(False, index=idx)
        for col in ("slope", "slope_atr_scaled", "score_slope"):
            if col in out.columns:
                slope_ok = slope_ok | pd.to_numeric(out[col], errors="coerce").fillna(0).ne(0)

        mtf_ok = pd.Series(False, index=idx)
        for col in ("mtf", "score_mtf", "mtf_score"):
            if col in out.columns:
                mtf_ok = mtf_ok | pd.to_numeric(out[col], errors="coerce").fillna(0).ne(0)

        rsi_ok = pd.Series(False, index=idx)
        if "rsi" in out.columns:
            rsi = pd.to_numeric(out["rsi"], errors="coerce")
            # rsi=50 は初期値扱い。0/100 はシグナルとして扱う。
            rsi_ok = rsi.notna() & rsi.ne(50)

        ready = (symbol_ok & close_ok & (macd_ok | slope_ok | mtf_ok | rsi_ok)).fillna(False)
        before = _bool_count(out, "technical_ready")
        old = pd.Series(out["technical_ready"]).fillna(False).astype(bool)
        out["technical_ready"] = (old | ready).fillna(False).astype(bool)

        if "symbol_hist_len" not in out.columns:
            out["symbol_hist_len"] = pd.NA
        hist = pd.to_numeric(out["symbol_hist_len"], errors="coerce")
        fix_mask = out["technical_ready"].fillna(False).astype(bool) & (hist.fillna(0) < 3)
        out.loc[fix_mask, "symbol_hist_len"] = 3

        after = _bool_count(out, "technical_ready")
        logger.warning(
            "[SUMMARY CONTROLLER READY MTF PATCH] ready context=%s rows=%s ready %s->%s macd=%s signal=%s slope=%s mtf=%s score_mtf=%s rsi_non50=%s",
            context,
            len(out),
            before,
            after,
            _nonzero_count(out, "macd"),
            _nonzero_count(out, "signal"),
            _nonzero_count(out, "slope"),
            _nonzero_count(out, "mtf"),
            _nonzero_count(out, "score_mtf"),
            int(rsi_ok.sum()) if isinstance(rsi_ok, pd.Series) else -1,
        )
    except Exception:
        logger.exception("[SUMMARY CONTROLLER READY MTF PATCH] relax technical_ready failed context=%s", context)
    return out


def _repair(df: Any, *, context: str) -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    out = _merge_daily_mtf_early(df, context=context)
    out = _align_mtf_aliases(out, context=context)
    out = _relax_technical_ready(out, context=context)
    return out


def _patch_function(mod: Any, name: str, *, post: bool = True) -> bool:
    fn = getattr(mod, name, None)
    if not callable(fn) or getattr(fn, "_summary_controller_ready_mtf_patch", False):
        return False
    key = f"{getattr(mod, '__name__', 'module')}.{name}"
    _ORIGINALS[key] = fn

    def wrapped(*args, **kwargs):
        # rebuild_technical_ready は入力時点でdaily MTFを入れてから本体へ渡す。
        if name == "rebuild_technical_ready" and args and isinstance(args[0], pd.DataFrame):
            args = (_repair(args[0], context=f"pre:{name}"),) + tuple(args[1:])
        ret = fn(*args, **kwargs)
        if isinstance(ret, pd.DataFrame):
            return _repair(ret, context=f"post:{name}")
        return ret

    wrapped._summary_controller_ready_mtf_patch = True  # type: ignore[attr-defined]
    setattr(mod, name, wrapped)
    logger.warning("[SUMMARY CONTROLLER READY MTF PATCH] patched %s", key)
    return True


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    if not _env_bool("SUMMARY_CONTROLLER_READY_MTF_PATCH_ENABLED", True):
        logger.warning("[SUMMARY CONTROLLER READY MTF PATCH] disabled by env")
        return False
    try:
        patched = 0
        import trading.summary.controller_cache as cc
        import trading.summary.controller_projection as cp

        for name in ("attach_display_ready",):
            patched += int(_patch_function(cc, name))
        for name in ("rebuild_display_ready", "rebuild_technical_ready", "latest_row_per_symbol", "latest_row_per_symbol_mature_first"):
            patched += int(_patch_function(cp, name))

        _PATCHED = True
        logger.warning("[SUMMARY CONTROLLER READY MTF PATCH] installed V1 patched=%s", patched)
        return True
    except Exception:
        logger.exception("[SUMMARY CONTROLLER READY MTF PATCH] install failed")
        return False


__all__ = ["install"]
