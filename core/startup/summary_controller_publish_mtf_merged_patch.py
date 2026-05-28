# ============================================================
# File   : core/startup/summary_controller_publish_mtf_merged_patch.py
# Version: V2-PUBLISH-MTF-REPAIRED-PUSH-MERGED-ARG-INTERVAL
# ------------------------------------------------------------
# 目的:
#   attach_display_ready / rebuild_display_ready 後に score_mtf が入ったDFを、
#   GlobalContext の PUSH merged latest へ再投入する。
#
# 背景:
#   2026-05-27 15:08ログでは、表示/AI用DFでは
#     daily_hit=44/45, score_mtf_nonzero=32/33
#   まで改善している。
#   しかし直後の MERGED GET source=push は cols=45 の古いDFを返し、
#     score_mtf=0 / mtf=-1 / mtf_score=-1
#   のままになっていた。
#
# 追加背景:
#   2026-05-28 09:07ログでは、3分/5分は publish されているが、
#   1分の戻り値DFに interval/source 列が無いケースで interval 推定に失敗し、
#   TONOSAMA が tf=1 source=push rows=0 を読んでいた。
#
# 方針:
#   - controller_cache.attach_display_ready 等の戻り値がPUSH系DFで、
#     score_mtf/mtf/mtf_score のいずれかが有効なら、
#     global_data.set_push_merged_summary(interval, df) へ再投入する。
#   - DF内の interval/source 列だけでなく、関数引数 interval/tf/timeframe も使う。
#   - DB保存はしない。main_entry_onlyでもメモリmergedだけ更新する。
# ============================================================

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIGINALS: dict[str, Callable] = {}
_LAST_PUBLISH_AT: dict[str, float] = {}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return max(0.0, float(raw))
    except Exception:
        return float(default)


def _safe_df(x: Any) -> pd.DataFrame:
    if isinstance(x, pd.DataFrame):
        return x.copy()
    try:
        return pd.DataFrame(x).copy()
    except Exception:
        return pd.DataFrame()


def _nz(df: pd.DataFrame, col: str) -> int:
    try:
        if df.empty or col not in df.columns:
            return -1
        return int((pd.to_numeric(df[col], errors="coerce").fillna(0) != 0).sum())
    except Exception:
        return -1


def _coerce_interval(v: Any) -> int | None:
    try:
        if v is None:
            return None
        if isinstance(v, str):
            s = v.strip().lower().replace("min", "").replace("m", "")
            if not s:
                return None
            iv = int(float(s))
        else:
            iv = int(float(v))
        if iv in (1, 3, 5):
            return iv
    except Exception:
        pass
    return None


def _infer_interval_from_call(args: tuple[Any, ...] | None = None, kwargs: dict[str, Any] | None = None) -> int | None:
    """関数引数から interval を推定する。

    controller系関数は戻り値DFに interval/source が無いことがあるため、
    kwargs の interval/tf/timeframe、または小さな整数引数を優先して使う。
    """
    try:
        kw = kwargs or {}
        for name in ("interval", "tf", "timeframe", "minutes", "minute_interval"):
            iv = _coerce_interval(kw.get(name))
            if iv in (1, 3, 5):
                return iv
    except Exception:
        pass
    try:
        for a in args or ():
            iv = _coerce_interval(a)
            if iv in (1, 3, 5):
                return iv
    except Exception:
        pass
    return None


def _infer_interval(df: pd.DataFrame, *, args: tuple[Any, ...] | None = None, kwargs: dict[str, Any] | None = None) -> int | None:
    # まず呼び出し引数を優先する。DFに interval/source が無い1分足対策。
    iv_call = _infer_interval_from_call(args, kwargs)
    if iv_call in (1, 3, 5):
        return iv_call

    try:
        if "interval" in df.columns:
            vals = pd.to_numeric(df["interval"], errors="coerce").dropna().astype(int)
            vals = vals[vals.isin([1, 3, 5])]
            if not vals.empty:
                return int(vals.mode().iloc[0])
    except Exception:
        pass
    try:
        if "source" in df.columns:
            s = " ".join(df["source"].astype(str).head(20).tolist()).lower()
            if "5min" in s or "5m" in s:
                return 5
            if "3min" in s or "3m" in s:
                return 3
            if "1min" in s or "1m" in s:
                return 1
    except Exception:
        pass
    return None


def _is_push_like(df: pd.DataFrame) -> bool:
    try:
        if "source" not in df.columns:
            # controller戻り値DFには source が無いことがある。
            # 本patchはPUSH系summary controllerの戻り値publish用なので、未知は通す。
            return True
        s = df["source"].astype(str).str.lower()
        return bool(s.str.contains("push", na=False).any())
    except Exception:
        return True


def _publish_if_repaired(
    df: Any,
    *,
    context: str,
    args: tuple[Any, ...] | None = None,
    kwargs: dict[str, Any] | None = None,
) -> Any:
    if not _env_bool("SUMMARY_CONTROLLER_PUBLISH_MTF_MERGED_ENABLED", True):
        return df
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    out = _safe_df(df)
    if out.empty or not _is_push_like(out):
        return df

    interval = _infer_interval(out, args=args, kwargs=kwargs)
    if interval not in {1, 3, 5}:
        logger.debug(
            "[SUMMARY PUBLISH MTF MERGED] skip interval unknown context=%s rows=%s cols=%s",
            context,
            len(out),
            len(out.columns),
        )
        return df

    score_nz = max(_nz(out, "score_mtf"), _nz(out, "mtf"), _nz(out, "mtf_score"))
    if score_nz <= 0:
        return df

    key = f"{interval}:{context}"
    now = time.time()
    min_gap = _env_float("SUMMARY_CONTROLLER_PUBLISH_MTF_MERGED_MIN_GAP_SEC", 1.0)
    last = _LAST_PUBLISH_AT.get(key, 0.0)
    if now - last < min_gap:
        return df
    _LAST_PUBLISH_AT[key] = now

    try:
        from global_state import global_data
        # 重複列を落としてから、PUSH merged latest に載せる。
        clean = out.loc[:, ~out.columns.duplicated()].copy()
        # interval列が無いDFは、後続の判定でも迷わないよう補完する。
        if "interval" not in clean.columns:
            clean["interval"] = int(interval)
        global_data.set_push_merged_summary(int(interval), clean)
        logger.warning(
            "[SUMMARY PUBLISH MTF MERGED] published context=%s interval=%s rows=%s cols=%s score_mtf=%s mtf=%s mtf_score=%s latest_dt=%s inferred_from_call=%s",
            context,
            interval,
            len(clean),
            len(clean.columns),
            _nz(clean, "score_mtf"),
            _nz(clean, "mtf"),
            _nz(clean, "mtf_score"),
            str(pd.to_datetime(clean["datetime"], errors="coerce").max()) if "datetime" in clean.columns else None,
            _infer_interval_from_call(args, kwargs),
        )
    except Exception:
        logger.exception("[SUMMARY PUBLISH MTF MERGED] publish failed context=%s interval=%s", context, interval)
    return df


def _patch_function(mod: Any, name: str) -> bool:
    fn = getattr(mod, name, None)
    if not callable(fn) or getattr(fn, "_summary_publish_mtf_merged_patch", False):
        return False
    key = f"{getattr(mod, '__name__', 'module')}.{name}"
    _ORIGINALS[key] = fn

    def wrapped(*args, **kwargs):
        ret = fn(*args, **kwargs)
        return _publish_if_repaired(ret, context=name, args=args, kwargs=kwargs)

    wrapped._summary_publish_mtf_merged_patch = True  # type: ignore[attr-defined]
    setattr(mod, name, wrapped)
    logger.warning("[SUMMARY PUBLISH MTF MERGED] patched %s", key)
    return True


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    try:
        import trading.summary.controller_cache as cc
        import trading.summary.controller_projection as cp

        patched = 0
        for name in ("attach_display_ready",):
            patched += int(_patch_function(cc, name))
        for name in ("rebuild_display_ready", "rebuild_technical_ready", "latest_row_per_symbol", "latest_row_per_symbol_mature_first"):
            patched += int(_patch_function(cp, name))
        _PATCHED = True
        logger.warning("[SUMMARY PUBLISH MTF MERGED] installed V2 patched=%s", patched)
        return True
    except Exception:
        logger.exception("[SUMMARY PUBLISH MTF MERGED] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY PUBLISH MTF MERGED] auto install failed")


__all__ = ["install"]
