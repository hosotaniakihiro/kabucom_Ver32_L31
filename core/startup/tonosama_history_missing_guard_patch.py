# ============================================================
# File   : core/startup/tonosama_history_missing_guard_patch.py
# Version: V2.4-HISTORY-FAIL-CLOSE-ATR-RELAX
# ------------------------------------------------------------
# 目的:
#   1) 3m/5mの出来高急増履歴が無い状態で controlled fail-open された
#      TONOSAMA候補を、既定で全件DROPする。
#   2) 起動時sitecustomize.pyが入れた fail-open 系ENVも、明示許可が無い限りOFFへ戻す。
#   3) ATR/値幅フィルタの閾値を実運用向けに緩和し、ENTRY_ATR_1M_MIN_RATIO 等で調整可能にする。
#
# 背景:
#   09:04直後など、3m/5mの履歴が揃う前に
#   _max_volume_surge_ratio=3.0 の仮値で候補化され、
#   実際の出来高急増ではない銘柄が entry 直前まで進む問題を防ぐ。
#
# ENV:
#   TONOSAMA_EXPLICIT_ALLOW_SURGE_FAILOPEN=1 # set only if old fail-open behavior is needed
#   TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY=0   # default reject
#   TONOSAMA_DROP_HISTORY_MISSING_ENTRY=1    # default drop
#   ENTRY_ATR_1M_MIN_RATIO=0.0010
#   ENTRY_RANGE_5M_MIN_PCT=0.008
#   ENTRY_ROW_RANGE_MIN_PCT=0.003
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIGINAL_BUILD = None

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(raw)
    except Exception:
        return float(default)


def _setdefault_env(name: str, value: str) -> None:
    try:
        cur = os.getenv(name)
        if cur is None or str(cur).strip() == "":
            os.environ[name] = str(value)
            logger.warning("[TONOSAMA HISTORY GUARD] env default set %s=%s", name, value)
    except Exception:
        pass


def _set_env(name: str, value: str) -> None:
    try:
        old = os.getenv(name)
        os.environ[name] = str(value)
        if str(old) != str(value):
            logger.warning("[TONOSAMA HISTORY GUARD] env forced %s: %s -> %s", name, old, value)
    except Exception:
        pass


def _disable_surge_failopen_defaults() -> None:
    """
    sitecustomize.py が setdefault で fail-open をONにしている場合でも、
    明示許可が無い限りここでOFFへ戻す。
    """
    if _env_bool("TONOSAMA_EXPLICIT_ALLOW_SURGE_FAILOPEN", False):
        logger.warning("[TONOSAMA HISTORY GUARD] surge fail-open explicitly allowed by env")
        return
    _set_env("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING", "0")
    _set_env("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY", "0")
    _set_env("TONOSAMA_FORCE_SURGE_FAILOPEN", "0")
    _set_env("TONOSAMA_ALLOW_EARLY_SURGE_FAILOPEN", "0")


def _patch_volatility_filter_thresholds() -> None:
    """
    6770 のように atr_ratio=0.000986 程度で全部落ちるのを防ぐ。
    既存 volatility_filter.py は default 引数に定数を束縛しているため、
    モジュール定数だけでなく関数 default/kwdefault も更新する。
    """
    try:
        import trading.filters.volatility_filter as vf

        atr_min = _env_float("ENTRY_ATR_1M_MIN_RATIO", 0.0010)
        range5_min = _env_float("ENTRY_RANGE_5M_MIN_PCT", 0.008)
        row_range_min = _env_float("ENTRY_ROW_RANGE_MIN_PCT", 0.003)

        vf.DEFAULT_ATR_1M_MIN_RATIO = atr_min
        vf.DEFAULT_RANGE_5M_MIN_PCT = range5_min
        vf.DEFAULT_ENTRY_ROW_RANGE_MIN_PCT = row_range_min

        try:
            vf._atr_1m_filter_from_entry_row.__defaults__ = (atr_min,)
        except Exception:
            pass
        try:
            vf._range_5m_filter_from_entry_row.__defaults__ = (range5_min,)
        except Exception:
            pass
        try:
            vf._entry_row_range_ok.__defaults__ = (row_range_min,)
        except Exception:
            pass
        try:
            if getattr(vf.atr_1m_filter, "__kwdefaults__", None):
                vf.atr_1m_filter.__kwdefaults__["min_ratio"] = atr_min
        except Exception:
            pass
        try:
            if getattr(vf.range_5m_filter, "__kwdefaults__", None):
                vf.range_5m_filter.__kwdefaults__["min_pct"] = range5_min
        except Exception:
            pass

        logger.warning(
            "[VOL FILTER THRESHOLD PATCH] installed atr_min_ratio=%.6f range5_min_pct=%.6f entry_row_range_min_pct=%.6f",
            atr_min,
            range5_min,
            row_range_min,
        )
    except Exception:
        logger.exception("[VOL FILTER THRESHOLD PATCH] install failed")


def _bool_series(df: pd.DataFrame, col: str, default: bool = False) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series(default, index=df.index if df is not None else None, dtype="bool")
    try:
        s = df[col]
        if getattr(s, "dtype", None) == bool:
            return s.fillna(default).astype(bool)
        return s.fillna(default).astype(str).str.strip().str.lower().isin(_TRUE)
    except Exception:
        return pd.Series(default, index=df.index, dtype="bool")


def _sample(df: pd.DataFrame, limit: int = 8) -> list[dict[str, Any]]:
    if df is None or df.empty:
        return []
    cols = [c for c in [
        "symbol", "symbolname", "close", "_max_volume_surge_ratio", "_max_price_change_pct",
        "_surge_tf", "_volume_surge_history_missing", "_volume_surge_failopen",
        "_body_change_pct", "_intrabar_range_pct", "_slope", "price_change_5s_pct",
    ] if c in df.columns]
    out: list[dict[str, Any]] = []
    for _, row in df.head(limit).iterrows():
        item: dict[str, Any] = {}
        for c in cols:
            v = row.get(c)
            try:
                if pd.isna(v):
                    v = None
            except Exception:
                pass
            if isinstance(v, float):
                v = round(v, 6)
            item[c] = v
        out.append(item)
    return out


def _drop_history_missing_failopen(df: pd.DataFrame, *, stage: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    has_missing = "_volume_surge_history_missing" in df.columns
    has_failopen = "_volume_surge_failopen" in df.columns
    if not has_missing and not has_failopen:
        return df

    missing = _bool_series(df, "_volume_surge_history_missing", False) if has_missing else pd.Series(False, index=df.index)
    failopen = _bool_series(df, "_volume_surge_failopen", False) if has_failopen else pd.Series(False, index=df.index)
    hist_mask = missing | failopen
    affected = int(hist_mask.sum()) if bool(hist_mask.any()) else 0
    if affected <= 0:
        return df

    allow = _env_bool("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY", False)
    drop = _env_bool("TONOSAMA_DROP_HISTORY_MISSING_ENTRY", True)
    if allow and not drop:
        logger.warning(
            "[TONOSAMA HISTORY GUARD] explicitly pass-through history-missing/failopen rows stage=%s rows=%s affected=%s sample=%s",
            stage,
            len(df),
            affected,
            _sample(df.loc[hist_mask].copy()),
        )
        return df

    out = df.loc[~hist_mask].copy()
    logger.warning(
        "[TONOSAMA HISTORY GUARD] fail-close dropped history-missing/failopen rows stage=%s before=%s after=%s dropped=%s allow=%s drop=%s sample=%s",
        stage,
        len(df),
        len(out),
        affected,
        allow,
        drop,
        _sample(df.loc[hist_mask].copy()),
    )
    return out


def _patched_build_scalping_feature_df(*args, **kwargs):
    df = _ORIGINAL_BUILD(*args, **kwargs) if callable(_ORIGINAL_BUILD) else pd.DataFrame()
    if isinstance(df, pd.DataFrame):
        return _drop_history_missing_failopen(df, stage="build_scalping_feature_df")
    return df


def install() -> bool:
    global _PATCHED, _ORIGINAL_BUILD
    if _PATCHED:
        return True

    _disable_surge_failopen_defaults()
    _patch_volatility_filter_thresholds()

    _setdefault_env("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY", "0")
    _setdefault_env("TONOSAMA_DROP_HISTORY_MISSING_ENTRY", "1")

    if not _env_bool("TONOSAMA_HISTORY_MISSING_GUARD_ENABLED", True):
        logger.warning("[TONOSAMA HISTORY GUARD] disabled by env")
        return False
    try:
        import trading.entry.tonosama.runner as runner
    except Exception:
        logger.exception("[TONOSAMA HISTORY GUARD] import runner failed")
        return False
    try:
        cur = getattr(runner, "build_scalping_feature_df", None)
        if not callable(cur):
            logger.warning("[TONOSAMA HISTORY GUARD] target build_scalping_feature_df not callable")
            return False
        if getattr(cur, "_tonosama_history_guard_patch", False):
            _PATCHED = True
            return True
        _ORIGINAL_BUILD = cur
        _patched_build_scalping_feature_df._tonosama_history_guard_patch = True  # type: ignore[attr-defined]
        _patched_build_scalping_feature_df._original = cur  # type: ignore[attr-defined]
        runner.build_scalping_feature_df = _patched_build_scalping_feature_df
        _PATCHED = True
        logger.warning(
            "[TONOSAMA HISTORY GUARD] installed v2.4 fail_close allow=%s drop=%s explicit_failopen=%s",
            _env_bool("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY", False),
            _env_bool("TONOSAMA_DROP_HISTORY_MISSING_ENTRY", True),
            _env_bool("TONOSAMA_EXPLICIT_ALLOW_SURGE_FAILOPEN", False),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA HISTORY GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA HISTORY GUARD] auto install failed")


__all__ = ["install"]
