# ============================================================
# File   : core/startup/ranking_only_exit_patch.py
# Version: Ver01-RANKING-ONLY-EXIT-PATCH
# ------------------------------------------------------------
# ランキング由来 RANKING_ONLY ENTRY 専用のEXIT判定を追加する。
#
# 対象:
#   source / entry_source が RANKING / ranking
#   または ranking_entry_mode == RANKING_ONLY
#   または entry_mode / entry_type == RANKING_ONLY
#
# 目的:
#   ランキングだけで入った銘柄は、サマリー・板・AIではなく、
#   「建値からの逆行」「最高益/最安益からの戻り」「保持時間」で早く逃げる。
#
# BUY:
#   - 建値から -0.30% で損切り
#   - +0.20%以上の含み益が出た後、最高値から -0.25% で利確/撤退
#   - +0.50% で即利確
#
# SELL:
#   - 建値から +0.30% で損切り
#   - +0.20%以上の含み益が出た後、最安値から +0.25% で利確/撤退
#   - +0.50% で即利確
#
# 環境変数:
#   RANKING_ONLY_EXIT_ENABLED=1
#   RANKING_ONLY_EXIT_STOP_LOSS=0.003
#   RANKING_ONLY_EXIT_TAKE_PROFIT=0.005
#   RANKING_ONLY_EXIT_TRAIL_START=0.002
#   RANKING_ONLY_EXIT_TRAIL_GAP=0.0025
#   RANKING_ONLY_EXIT_MAX_HOLD_SEC=180
#   RANKING_ONLY_EXIT_STAGNATION_SEC=60
#   RANKING_ONLY_EXIT_STAGNATION_MIN_MOVE=0.001
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_CHECK_NORMAL_EXIT = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)
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


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _get(obj: Any, name: str, default=None):
    try:
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)
    except Exception:
        return default


def _set(obj: Any, name: str, value: Any) -> None:
    try:
        if isinstance(obj, dict):
            obj[name] = value
        else:
            setattr(obj, name, value)
    except Exception:
        pass


def _position_symbol(pos: Any) -> str:
    return str(
        _get(pos, "symbol")
        or _get(pos, "Symbol")
        or _get(pos, "stock_code")
        or ""
    ).strip()


def _position_side(pos: Any) -> str:
    return str(_get(pos, "side") or _get(pos, "Side") or "BUY").upper()


def _is_sell_side(pos: Any) -> bool:
    s = _position_side(pos)
    return s.startswith("SELL") or s.startswith("SHORT")


def _entry_price(pos: Any) -> float:
    return _safe_float(
        _get(pos, "avg_price")
        or _get(pos, "entry_price")
        or _get(pos, "price")
        or _get(pos, "current_price"),
        0.0,
    )


def _entry_time(pos: Any, now: dt.datetime) -> dt.datetime:
    v = _get(pos, "entry_time") or _get(pos, "created_at") or _get(pos, "trade_time")
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None)
    try:
        if v:
            return dt.datetime.fromisoformat(str(v).replace("Z", "").replace("T", " ")).replace(tzinfo=None)
    except Exception:
        pass
    return now


def _is_ranking_only_position(pos: Any) -> bool:
    source = str(_get(pos, "source") or _get(pos, "entry_source") or "").upper()
    entry_mode = str(_get(pos, "entry_mode") or _get(pos, "entry_type") or "").upper()
    ranking_mode = str(_get(pos, "ranking_entry_mode") or "").upper()

    if ranking_mode == "RANKING_ONLY":
        return True
    if entry_mode == "RANKING_ONLY":
        return True
    if source == "RANKING" and entry_mode in {"RANKING", "RANKING_ONLY", ""}:
        return True
    return False


def _pnl_rate(pos: Any, price: float) -> float:
    ep = _entry_price(pos)
    if ep <= 0 or price <= 0:
        return 0.0
    rate = (price - ep) / ep
    if _is_sell_side(pos):
        rate = -rate
    return rate


def _update_extreme_and_max_profit(pos: Any, price: float) -> float:
    """
    BUY は entry後高値、SELL は entry後安値を追跡し、最大含み益率を返す。
    """
    ep = _entry_price(pos)
    if ep <= 0 or price <= 0:
        return 0.0

    side = _position_side(pos)

    if _is_sell_side(pos):
        low = _safe_float(_get(pos, "low_since_entry"), 0.0)
        if low <= 0:
            low = price
        low = min(low, price)
        _set(pos, "low_since_entry", low)
        max_profit = (ep - low) / ep
    else:
        high = _safe_float(_get(pos, "high_since_entry"), 0.0)
        if high <= 0:
            high = price
        high = max(high, price)
        _set(pos, "high_since_entry", high)
        max_profit = (high - ep) / ep

    prev = _safe_float(_get(pos, "max_profit_rate"), 0.0)
    max_profit = max(prev, max_profit, 0.0)
    _set(pos, "max_profit_rate", max_profit)

    logger.debug(
        "[RANKING ONLY EXIT PATCH] extreme updated symbol=%s side=%s price=%s max_profit=%.5f",
        _position_symbol(pos),
        side,
        price,
        max_profit,
    )
    return max_profit


def _ranking_only_exit_reason(pos: Any, price: float, now: dt.datetime) -> str | None:
    if not _env_bool("RANKING_ONLY_EXIT_ENABLED", True):
        return None
    if not _is_ranking_only_position(pos):
        return None

    symbol = _position_symbol(pos)
    if not symbol or price <= 0:
        return None

    ep = _entry_price(pos)
    if ep <= 0:
        return None

    pnl = _pnl_rate(pos, price)
    max_profit = _update_extreme_and_max_profit(pos, price)
    hold_sec = (now - _entry_time(pos, now)).total_seconds()

    stop_loss = abs(_env_float("RANKING_ONLY_EXIT_STOP_LOSS", 0.003))
    take_profit = abs(_env_float("RANKING_ONLY_EXIT_TAKE_PROFIT", 0.005))
    trail_start = abs(_env_float("RANKING_ONLY_EXIT_TRAIL_START", 0.002))
    trail_gap = abs(_env_float("RANKING_ONLY_EXIT_TRAIL_GAP", 0.0025))
    max_hold_sec = _env_int("RANKING_ONLY_EXIT_MAX_HOLD_SEC", 180)
    stagnation_sec = _env_int("RANKING_ONLY_EXIT_STAGNATION_SEC", 60)
    stagnation_min_move = abs(_env_float("RANKING_ONLY_EXIT_STAGNATION_MIN_MOVE", 0.001))

    if pnl <= -stop_loss:
        return "RANKING_ONLY_STOP"

    if pnl >= take_profit:
        return "RANKING_ONLY_TAKE_PROFIT"

    if max_profit >= trail_start and pnl <= max_profit - trail_gap:
        return "RANKING_ONLY_TRAIL"

    if hold_sec >= max_hold_sec:
        return "RANKING_ONLY_TIMEOUT"

    # 1分程度経っても建値からほぼ動いていない場合は、ランキングの勢いが続いていないと判断
    if hold_sec >= stagnation_sec and abs(pnl) < stagnation_min_move:
        return "RANKING_ONLY_STAGNATION"

    logger.debug(
        "[RANKING ONLY EXIT HOLD] symbol=%s side=%s price=%.4f entry=%.4f pnl=%.5f max_profit=%.5f hold_sec=%.1f",
        symbol,
        _position_side(pos),
        price,
        ep,
        pnl,
        max_profit,
        hold_sec,
    )
    return None


def _patched_check_normal_exit(pos: Any, price: float, now):
    try:
        if not isinstance(now, dt.datetime):
            now = dt.datetime.now()
        reason = _ranking_only_exit_reason(pos, _safe_float(price, 0.0), now)
        if reason:
            logger.warning(
                "[RANKING ONLY EXIT PATCH] EXIT symbol=%s side=%s price=%s reason=%s pnl=%.5f max_profit=%.5f",
                _position_symbol(pos),
                _position_side(pos),
                price,
                reason,
                _pnl_rate(pos, _safe_float(price, 0.0)),
                _safe_float(_get(pos, "max_profit_rate"), 0.0),
            )
            return reason
    except Exception:
        logger.debug("[RANKING ONLY EXIT PATCH] check failed", exc_info=True)

    if callable(_ORIG_CHECK_NORMAL_EXIT):
        return _ORIG_CHECK_NORMAL_EXIT(pos, price, now)
    return None


def install() -> bool:
    global _INSTALLED, _ORIG_CHECK_NORMAL_EXIT
    try:
        import trading.handlers.exit_handler as eh

        if _INSTALLED:
            return True

        cur = getattr(eh, "check_normal_exit", None)
        if getattr(cur, "_ranking_only_exit_patch", False):
            _INSTALLED = True
            return True

        if not callable(cur):
            logger.error("[RANKING ONLY EXIT PATCH] check_normal_exit unavailable")
            return False

        _ORIG_CHECK_NORMAL_EXIT = cur
        _patched_check_normal_exit._ranking_only_exit_patch = True  # type: ignore[attr-defined]
        eh.check_normal_exit = _patched_check_normal_exit

        _INSTALLED = True
        logger.warning(
            "[RANKING ONLY EXIT PATCH] installed enabled=%s stop=%.4f take=%.4f trail_start=%.4f trail_gap=%.4f max_hold=%s stagnation=%s stagnation_min_move=%.4f",
            _env_bool("RANKING_ONLY_EXIT_ENABLED", True),
            _env_float("RANKING_ONLY_EXIT_STOP_LOSS", 0.003),
            _env_float("RANKING_ONLY_EXIT_TAKE_PROFIT", 0.005),
            _env_float("RANKING_ONLY_EXIT_TRAIL_START", 0.002),
            _env_float("RANKING_ONLY_EXIT_TRAIL_GAP", 0.0025),
            _env_int("RANKING_ONLY_EXIT_MAX_HOLD_SEC", 180),
            _env_int("RANKING_ONLY_EXIT_STAGNATION_SEC", 60),
            _env_float("RANKING_ONLY_EXIT_STAGNATION_MIN_MOVE", 0.001),
        )
        return True
    except Exception:
        logger.exception("[RANKING ONLY EXIT PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING ONLY EXIT PATCH] auto install failed")


__all__ = ["install"]
