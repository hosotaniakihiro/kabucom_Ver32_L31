# ============================================================
# File   : trading/exit/exit_utils.py
# Version: V1.0-SPLIT-EXIT-UTILS
# ------------------------------------------------------------
# 【概要】
#   EXIT系共通ユーティリティ。
#
# 【役割】
#   - 安全な float / int / bool / str 変換
#   - dict / attr からの安全取得
#   - open position snapshot
#   - holding_seconds 取得
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Dict, Optional

from core.global_context.context import global_context as GC

logger = logging.getLogger(__name__)


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def safe_float_or_none(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except Exception:
        return None


def safe_int_or_none(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        if isinstance(v, bool):
            return int(v)
        fv = float(v)
        if math.isnan(fv) or math.isinf(fv):
            return None
        return int(fv)
    except Exception:
        return None


def safe_bool(v: Any, default: bool = False) -> bool:
    try:
        if v is None:
            return bool(default)
        if isinstance(v, bool):
            return v

        s = str(v).strip().lower()

        if s in {"1", "true", "yes", "on", "y", "ok"}:
            return True

        if s in {"0", "false", "no", "off", "n", "ng", ""}:
            return False

        return bool(default)

    except Exception:
        return bool(default)


def safe_bool_or_none(v: Any) -> Optional[bool]:
    try:
        if v is None:
            return None
        if isinstance(v, bool):
            return v

        s = str(v).strip().lower()

        if s in {"1", "true", "yes", "on", "y", "ok", "break", "broken", "below"}:
            return True

        if s in {"0", "false", "no", "off", "n", "ng", "", "none", "above"}:
            return False

        return None

    except Exception:
        return None


def safe_str(v: Any, default: str = "") -> str:
    try:
        if v is None:
            return default
        return str(v)
    except Exception:
        return default


def dict_get_any(d: Any, *names: str, default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default

    for name in names:
        try:
            if name in d:
                return d.get(name)
        except Exception:
            pass

    return default


def attr_get_any(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default

    for name in names:
        try:
            if hasattr(obj, name):
                return getattr(obj, name)
        except Exception:
            pass

    return default


def get_open_positions_safe() -> Dict[str, Dict[str, Any]]:
    try:
        if not hasattr(GC, "positions") or GC.positions is None:
            return {}

        if hasattr(GC.positions, "snapshot_open"):
            return GC.positions.snapshot_open() or {}

        if hasattr(GC.positions, "snapshot_dict"):
            return GC.positions.snapshot_dict() or {}

        if hasattr(GC.positions, "open_positions"):
            return dict(GC.positions.open_positions or {})

    except Exception:
        logger.exception("[POSITION_SNAPSHOT_ERROR]")

    return {}


def get_holding_seconds_safe(ctx: Any, now: dt.datetime) -> int:
    try:
        if ctx is not None and hasattr(ctx, "holding_seconds"):
            return int(ctx.holding_seconds(now))
    except Exception:
        pass

    return 0


def get_feature_value(
    features: Dict[str, Any],
    ctx: Any,
    *names: str,
    default: float = 0.0,
) -> float:
    for name in names:
        try:
            if isinstance(features, dict) and name in features:
                return safe_float(features.get(name), default)
        except Exception:
            pass

    for name in names:
        try:
            if ctx is not None and hasattr(ctx, name):
                return safe_float(getattr(ctx, name), default)
        except Exception:
            pass

    return default


def get_feature_value_or_none(
    features: Dict[str, Any],
    ctx: Any,
    pos: Optional[Dict[str, Any]],
    *names: str,
) -> Optional[float]:
    for name in names:
        try:
            if isinstance(features, dict) and name in features:
                v = safe_float_or_none(features.get(name))
                if v is not None:
                    return v
        except Exception:
            pass

    for name in names:
        try:
            if ctx is not None and hasattr(ctx, name):
                v = safe_float_or_none(getattr(ctx, name))
                if v is not None:
                    return v
        except Exception:
            pass

    for name in names:
        try:
            if isinstance(pos, dict) and name in pos:
                v = safe_float_or_none(pos.get(name))
                if v is not None:
                    return v
        except Exception:
            pass

    return None


def get_feature_int_or_none(
    features: Dict[str, Any],
    ctx: Any,
    pos: Optional[Dict[str, Any]],
    *names: str,
) -> Optional[int]:
    for name in names:
        try:
            if isinstance(features, dict) and name in features:
                v = safe_int_or_none(features.get(name))
                if v is not None:
                    return v
        except Exception:
            pass

    for name in names:
        try:
            if ctx is not None and hasattr(ctx, name):
                v = safe_int_or_none(getattr(ctx, name))
                if v is not None:
                    return v
        except Exception:
            pass

    for name in names:
        try:
            if isinstance(pos, dict) and name in pos:
                v = safe_int_or_none(pos.get(name))
                if v is not None:
                    return v
        except Exception:
            pass

    return None


def get_feature_bool_or_none(
    features: Dict[str, Any],
    ctx: Any,
    pos: Optional[Dict[str, Any]],
    *names: str,
) -> Optional[bool]:
    for name in names:
        try:
            if isinstance(features, dict) and name in features:
                v = safe_bool_or_none(features.get(name))
                if v is not None:
                    return v
        except Exception:
            pass

    for name in names:
        try:
            if ctx is not None and hasattr(ctx, name):
                v = safe_bool_or_none(getattr(ctx, name))
                if v is not None:
                    return v
        except Exception:
            pass

    for name in names:
        try:
            if isinstance(pos, dict) and name in pos:
                v = safe_bool_or_none(pos.get(name))
                if v is not None:
                    return v
        except Exception:
            pass

    return None


__all__ = [
    "safe_float",
    "safe_float_or_none",
    "safe_int_or_none",
    "safe_bool",
    "safe_bool_or_none",
    "safe_str",
    "dict_get_any",
    "attr_get_any",
    "get_open_positions_safe",
    "get_holding_seconds_safe",
    "get_feature_value",
    "get_feature_value_or_none",
    "get_feature_int_or_none",
    "get_feature_bool_or_none",
]