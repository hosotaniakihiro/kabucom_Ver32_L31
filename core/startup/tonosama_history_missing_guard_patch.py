# ============================================================
# File   : core/startup/tonosama_history_missing_guard_patch.py
# Version: V2-TONOSAMA-HISTORY-MISSING-STRONG-MOVE-ALLOW
# ------------------------------------------------------------
# 目的:
#   _volume_surge_history_missing=True / _volume_surge_failopen=True の行を
#   原則除外する。ただし、後場寄り直後などで3m/5mの出来高履歴が
#   足りない場合でも、価格変化・値幅・slope が十分強いものだけは残す。
#
# 背景:
#   12:52ログでは 1m/3m/5m summary は最新化されていたが、
#   volume_surge の rolling 履歴不足により全行 _volume_surge_failopen=True。
#   V1 は全落ちさせるため TONOSAMA が完全停止していた。
#
# 安全方針:
#   - 偽の出来高急増 3.0x だけでは通さない
#   - _max_price_change_pct / _intrabar_range_pct / _slope の実値が強い場合だけ通す
#   - マイナス方向はBUY候補としては落ちるため、ここでは絶対値ではなく正方向を見る
#
# ENV:
#   TONOSAMA_HISTORY_MISSING_GUARD_ENABLED=1
#   TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY=0
#   TONOSAMA_ALLOW_HISTORY_MISSING_STRONG_MOVE=1
#   TONOSAMA_HISTORY_MISSING_MIN_PRICE_CHANGE_PCT=0.20
#   TONOSAMA_HISTORY_MISSING_MIN_INTRABAR_RANGE_PCT=1.50
#   TONOSAMA_HISTORY_MISSING_MIN_SLOPE=0.0005
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


def _num_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series(default, index=df.index if df is not None else None, dtype="float64")
    try:
        return pd.to_numeric(df[col], errors="coerce").fillna(default).astype(float)
    except Exception:
        return pd.Series(default, index=df.index, dtype="float64")


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


def _strong_move_mask(df: pd.DataFrame) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(False, index=df.index if df is not None else None, dtype="bool")
    if not _env_bool("TONOSAMA_ALLOW_HISTORY_MISSING_STRONG_MOVE", True):
        return pd.Series(False, index=df.index, dtype="bool")

    price_chg = _num_series(df, "_max_price_change_pct", 0.0)
    intrabar = _num_series(df, "_intrabar_range_pct", 0.0)
    body = _num_series(df, "_body_change_pct", 0.0)
    slope = _num_series(df, "_slope", 0.0)

    min_price_chg = _env_float("TONOSAMA_HISTORY_MISSING_MIN_PRICE_CHANGE_PCT", 0.20)
    min_range = _env_float("TONOSAMA_HISTORY_MISSING_MIN_INTRABAR_RANGE_PCT", 1.50)
    min_slope = _env_float("TONOSAMA_HISTORY_MISSING_MIN_SLOPE", 0.0005)

    # 出来高急増率はfailopen由来で信用しない。実際の価格変化・値幅・slopeだけを見る。
    # bodyが入っている場合は、ヒゲだけの荒れではなく実体変化も少し要求する。
    body_ok = body >= _env_float("TONOSAMA_HISTORY_MISSING_MIN_BODY_CHANGE_PCT", 0.02)
    body_missing = "_body_change_pct" not in df.columns
    body_cond = body_ok if not body_missing else pd.Series(True, index=df.index, dtype="bool")

    return (price_chg >= min_price_chg) & (intrabar >= min_range) & (slope >= min_slope) & body_cond


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
    hist_mask = missing | failopen
    if not bool(hist_mask.any()):
        return df

    strong = _strong_move_mask(df)
    drop_mask = hist_mask & ~strong
    keep_mask = ~drop_mask

    before = len(df)
    dropped = df.loc[drop_mask].copy()
    kept_hist = df.loc[hist_mask & strong].copy()
    out = df.loc[keep_mask].copy()

    if not kept_hist.empty:
        logger.warning(
            "[TONOSAMA HISTORY GUARD] allow strong move despite missing history stage=%s kept=%s threshold_price_chg=%.3f threshold_range=%.3f threshold_slope=%.5f sample=%s",
            stage,
            len(kept_hist),
            _env_float("TONOSAMA_HISTORY_MISSING_MIN_PRICE_CHANGE_PCT", 0.20),
            _env_float("TONOSAMA_HISTORY_MISSING_MIN_INTRABAR_RANGE_PCT", 1.50),
            _env_float("TONOSAMA_HISTORY_MISSING_MIN_SLOPE", 0.0005),
            _sample(kept_hist),
        )

    if not dropped.empty:
        logger.warning(
            "[TONOSAMA HISTORY GUARD] dropped stage=%s before=%s after=%s dropped=%s reason=volume_surge_history_missing_or_failopen_not_strong_enough sample=%s",
            stage,
            before,
            len(out),
            int(drop_mask.sum()),
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
            "[TONOSAMA HISTORY GUARD] installed v2 allow_history_missing=%s allow_strong_move=%s",
            _env_bool("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY", False),
            _env_bool("TONOSAMA_ALLOW_HISTORY_MISSING_STRONG_MOVE", True),
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
