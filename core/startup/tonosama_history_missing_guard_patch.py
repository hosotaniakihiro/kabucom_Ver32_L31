# ============================================================
# File   : core/startup/tonosama_history_missing_guard_patch.py
# Version: V1-TONOSAMA-HISTORY-MISSING-FAILOPEN-GUARD
# ------------------------------------------------------------
# 目的:
#   trading.entry.tonosama.volume_surge 側で何らかの理由により
#   _volume_surge_history_missing=True / _volume_surge_failopen=True の行が
#   runner へ渡った場合でも、殿様エントリー候補から最終除外する。
#
# 背景:
#   11:09ログで surge=3.00x / _volume_surge_failopen 相当の行が
#   TONOSAMA PENDING になっていた。
#   volume_surge.py 側のfail-closedに加え、runner入口でも二重ガードする。
#
# ENV:
#   TONOSAMA_HISTORY_MISSING_GUARD_ENABLED=1
#   TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY=0
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
        "_body_change_pct", "_intrabar_range_pct", "_slope",
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
    if _env_bool("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY", False):
        return df
    has_missing = "_volume_surge_history_missing" in df.columns
    has_failopen = "_volume_surge_failopen" in df.columns
    if not has_missing and not has_failopen:
        return df
    missing = _bool_series(df, "_volume_surge_history_missing", False) if has_missing else pd.Series(False, index=df.index)
    failopen = _bool_series(df, "_volume_surge_failopen", False) if has_failopen else pd.Series(False, index=df.index)
    mask = missing | failopen
    if not bool(mask.any()):
        return df
    before = len(df)
    dropped = df.loc[mask].copy()
    out = df.loc[~mask].copy()
    logger.warning(
        "[TONOSAMA HISTORY GUARD] dropped stage=%s before=%s after=%s dropped=%s reason=volume_surge_history_missing_or_failopen sample=%s",
        stage,
        before,
        len(out),
        int(mask.sum()),
        _sample(dropped),
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
            "[TONOSAMA HISTORY GUARD] installed allow_history_missing=%s",
            _env_bool("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY", False),
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
