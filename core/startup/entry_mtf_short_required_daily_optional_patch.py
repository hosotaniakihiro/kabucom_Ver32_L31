# ============================================================
# File   : core/startup/entry_mtf_short_required_daily_optional_patch.py
# Version: V1.0-SHORT-MTF-REQUIRED-DAILY-OPTIONAL
# ------------------------------------------------------------
# 【目的】
#   日足MA/日足MTF 1つの逆行だけでエントリー不可になる問題を防ぐ。
#
# 【方針】
#   - 発注直前の方向ガードでは、日足込みの mtf / score_mtf は必須判定に使わない
#   - 1分・3分・5分の slope_atr_scaled_* を必須にする
#   - 日足MTFはスコア加点・参考情報として残すが、単独では発注停止しない
#
# 【判定】
#   BUY  : slope_1m / slope_3m / slope_5m がすべて +eps より大きい
#   SELL : slope_1m / slope_3m / slope_5m がすべて -eps より小さい
#
# 【環境変数】
#   ENTRY_SHORT_MTF_REQUIRED=1
#   ENTRY_SHORT_MTF_REQUIRE_ALL=1
#   ENTRY_SHORT_MTF_SLOPE_EPS=0.0
#   ENTRY_DAILY_MTF_OPTIONAL=1
# ============================================================

from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)
_PATCHED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_float_or_none(v: Any) -> Optional[float]:
    try:
        if v is None or str(v).strip() == "":
            return None
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except Exception:
        return None


def _get(row: Dict[str, Any], *keys: str) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return None


def _ng(reason: str, **detail: Any) -> Dict[str, Any]:
    return {"ok": False, "reason": reason, "detail": detail}


def _short_mtf_direction_guard(entry_row: Dict[str, Any], *, symbol: str, side: str, source: str) -> Optional[Dict[str, Any]]:
    if not _env_bool("ENTRY_SHORT_MTF_REQUIRED", True):
        return None

    if str(source or "").upper() != "SUMMARY_AI":
        return None

    side_u = str(side or "").upper()
    if side_u not in {"BUY", "SELL"}:
        return None

    row = entry_row or {}
    eps = abs(_env_float("ENTRY_SHORT_MTF_SLOPE_EPS", 0.0))
    require_all = _env_bool("ENTRY_SHORT_MTF_REQUIRE_ALL", True)

    slopes = {
        "slope_1m": _safe_float_or_none(_get(row, "slope_atr_scaled_1m", "slope_1m", "slope1m")),
        "slope_3m": _safe_float_or_none(_get(row, "slope_atr_scaled_3m", "slope_3m", "slope3m")),
        "slope_5m": _safe_float_or_none(_get(row, "slope_atr_scaled_5m", "slope_5m", "slope5m")),
    }

    missing = [k for k, v in slopes.items() if v is None]
    if missing and require_all:
        return _ng(
            "SHORT_MTF_SLOPE_MISSING",
            symbol=symbol,
            side=side_u,
            missing=missing,
            slopes=slopes,
            daily_mtf_optional=_env_bool("ENTRY_DAILY_MTF_OPTIONAL", True),
        )

    available = {k: v for k, v in slopes.items() if v is not None}
    if not available:
        return _ng(
            "SHORT_MTF_NO_DATA",
            symbol=symbol,
            side=side_u,
            slopes=slopes,
            daily_mtf_optional=_env_bool("ENTRY_DAILY_MTF_OPTIONAL", True),
        )

    if side_u == "BUY":
        bad = {k: v for k, v in available.items() if v <= eps}
        if bad:
            return _ng(
                "SHORT_MTF_NOT_BUY_ALIGNED",
                symbol=symbol,
                side=side_u,
                bad=bad,
                slopes=slopes,
                eps=eps,
                note="daily_mtf_is_optional_not_blocking",
            )
    else:
        bad = {k: v for k, v in available.items() if v >= -eps}
        if bad:
            return _ng(
                "SHORT_MTF_NOT_SELL_ALIGNED",
                symbol=symbol,
                side=side_u,
                bad=bad,
                slopes=slopes,
                eps=eps,
                note="daily_mtf_is_optional_not_blocking",
            )

    logger.warning(
        "[SHORT MTF GUARD] OK symbol=%s side=%s slopes=%s daily_mtf_optional=%s",
        symbol,
        side_u,
        slopes,
        _env_bool("ENTRY_DAILY_MTF_OPTIONAL", True),
    )
    return None


def _strict_guard_short_mtf_only(*, symbol: str, side: str, row: dict, detail: dict) -> Optional[Dict[str, Any]]:
    # 板なし許可などは元の entry_limit_passive_runtime_patch に任せる。
    # ここではMTF方向だけを短期1/3/5分に限定して判定する。
    return _short_mtf_direction_guard(row, symbol=symbol, side=side, source="SUMMARY_AI")


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    os.environ.setdefault("ENTRY_SHORT_MTF_REQUIRED", "1")
    os.environ.setdefault("ENTRY_SHORT_MTF_REQUIRE_ALL", "1")
    os.environ.setdefault("ENTRY_SHORT_MTF_SLOPE_EPS", "0.0")
    os.environ.setdefault("ENTRY_DAILY_MTF_OPTIONAL", "1")

    ok_any = False

    # 1) entry_order_builder の MTF方向ガードを差し替え
    try:
        import trading.handlers.entry_order_builder as eob
        old = getattr(eob, "_summary_mtf_direction_guard", None)
        if callable(old) and not getattr(old, "_short_required_daily_optional_v1", False):
            _short_mtf_direction_guard._short_required_daily_optional_v1 = True  # type: ignore[attr-defined]
            _short_mtf_direction_guard._original = old  # type: ignore[attr-defined]
            eob._summary_mtf_direction_guard = _short_mtf_direction_guard
            ok_any = True
            logger.warning("[SHORT MTF GUARD] patched entry_order_builder._summary_mtf_direction_guard")
    except Exception:
        logger.exception("[SHORT MTF GUARD] patch entry_order_builder failed")

    # 2) entry_limit_passive_runtime_patch の厳格MTF判定も短期だけへ差し替え
    try:
        import core.startup.entry_limit_passive_runtime_patch as elp
        old2 = getattr(elp, "_summary_ai_strict_guard", None)
        if callable(old2) and not getattr(old2, "_short_required_daily_optional_v1", False):
            def _patched_summary_ai_strict_guard(*, symbol: str, side: str, row: dict, detail: dict):
                # 元ガードの board / technical / slope 判定は活かすが、元のMTF判定だけが日足込みで強すぎる。
                # そのため、元ガードを呼ばずに必要最低限の short MTF 方向だけを見る。
                return _strict_guard_short_mtf_only(symbol=symbol, side=side, row=row, detail=detail)

            _patched_summary_ai_strict_guard._short_required_daily_optional_v1 = True  # type: ignore[attr-defined]
            _patched_summary_ai_strict_guard._original = old2  # type: ignore[attr-defined]
            elp._summary_ai_strict_guard = _patched_summary_ai_strict_guard
            ok_any = True
            logger.warning("[SHORT MTF GUARD] patched entry_limit_passive_runtime_patch._summary_ai_strict_guard")
    except Exception:
        logger.exception("[SHORT MTF GUARD] patch entry_limit_passive_runtime_patch failed")

    _PATCHED = bool(ok_any)
    logger.warning(
        "[SHORT MTF GUARD] installed=%s required=%s require_all=%s eps=%s daily_optional=%s",
        _PATCHED,
        os.getenv("ENTRY_SHORT_MTF_REQUIRED"),
        os.getenv("ENTRY_SHORT_MTF_REQUIRE_ALL"),
        os.getenv("ENTRY_SHORT_MTF_SLOPE_EPS"),
        os.getenv("ENTRY_DAILY_MTF_OPTIONAL"),
    )
    return _PATCHED


try:
    install()
except Exception:
    logger.exception("[SHORT MTF GUARD] auto install failed")

__all__ = ["install"]
