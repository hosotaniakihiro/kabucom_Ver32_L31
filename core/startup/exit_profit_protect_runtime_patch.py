# ============================================================
# File   : core/startup/exit_profit_protect_runtime_patch.py
# Version: Ver01-SCALP-PROFIT-PROTECT
# ------------------------------------------------------------
# 含み益が出たのに利確せず、損切りになる問題への runtime patch。
#
# 目的:
#   - 通常EXITの +0.8% 利確 / +0.6% トレーリング開始はスキャルピングには遅い
#   - +0.20% 程度の含み益が出たら利益保護を開始
#   - +0.30% に到達したら早めに利確
#   - 一度利益が出た銘柄をマイナス損切りまで放置しない
#
# ENV default:
#   EXIT_PROFIT_PROTECT_ENABLED=1
#   EXIT_PROFIT_TAKE_PCT=0.0030          # +0.30% 到達で利確
#   EXIT_PROFIT_PROTECT_START_PCT=0.0020 # +0.20% から保護開始
#   EXIT_PROFIT_PROTECT_FLOOR_PCT=0.0010 # 保護後 +0.10% まで落ちたら逃げる
#   EXIT_PROFIT_GIVEBACK_PCT=0.0010      # 高値利益から -0.10% 戻したら逃げる
#   EXIT_PROFIT_MIN_HOLD_SEC=0           # 最短保有秒数。0なら即保護
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_NORMAL = None
_ORIG_TONOSAMA = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng"}:
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


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _symbol(pos: Any) -> str:
    try:
        return str(_get(pos, "symbol") or _get(pos, "Symbol") or _get(pos, "stock_code") or "").strip()
    except Exception:
        return ""


def _side(pos: Any) -> str:
    try:
        return str(_get(pos, "side") or _get(pos, "Side") or "BUY").upper()
    except Exception:
        return "BUY"


def _entry_price(pos: Any) -> float:
    return _safe_float(
        _get(pos, "avg_price")
        or _get(pos, "entry_price")
        or _get(pos, "price")
        or _get(pos, "current_price"),
        0.0,
    )


def _entry_time(pos: Any) -> dt.datetime:
    v = _get(pos, "entry_time")
    if isinstance(v, dt.datetime):
        return v
    try:
        if v:
            return dt.datetime.fromisoformat(str(v))
    except Exception:
        pass
    return dt.datetime.now()


def _pnl_rate(pos: Any, price: float) -> float:
    entry = _entry_price(pos)
    if entry <= 0 or price <= 0:
        return 0.0
    r = (price - entry) / entry
    side = _side(pos)
    if side.startswith("SELL") or side.startswith("SHORT"):
        r = -r
    return r


def _update_max_profit(pos: Any, pnl_rate: float) -> float:
    max_profit = _safe_float(_get(pos, "max_profit_rate"), 0.0)
    max_profit = max(max_profit, pnl_rate)
    _set(pos, "max_profit_rate", max_profit)
    return max_profit


def _profit_protect_reason(pos: Any, price: float, now: dt.datetime, *, label: str) -> str | None:
    if not _env_bool("EXIT_PROFIT_PROTECT_ENABLED", True):
        return None

    entry = _entry_price(pos)
    if entry <= 0 or price <= 0:
        return None

    hold_sec = (now - _entry_time(pos)).total_seconds()
    min_hold = _env_float("EXIT_PROFIT_MIN_HOLD_SEC", 0.0)
    if hold_sec < min_hold:
        return None

    pnl = _pnl_rate(pos, price)
    max_profit = _update_max_profit(pos, pnl)

    take_pct = _env_float("EXIT_PROFIT_TAKE_PCT", 0.0030)
    start_pct = _env_float("EXIT_PROFIT_PROTECT_START_PCT", 0.0020)
    floor_pct = _env_float("EXIT_PROFIT_PROTECT_FLOOR_PCT", 0.0010)
    giveback_pct = _env_float("EXIT_PROFIT_GIVEBACK_PCT", 0.0010)

    symbol = _symbol(pos)
    side = _side(pos)

    # 1) +0.30% 到達なら即利確
    if pnl >= take_pct:
        logger.warning(
            "[EXIT PROFIT PROTECT] TAKE symbol=%s side=%s label=%s price=%.4f entry=%.4f pnl=%.4f max=%.4f take=%.4f hold=%.1fs",
            symbol, side, label, price, entry, pnl, max_profit, take_pct, hold_sec,
        )
        return "PROFIT_TAKE_FAST"

    # 2) 一度 +0.20% 以上乗ったら、+0.10%まで落ちた時点で逃げる
    if max_profit >= start_pct and pnl <= floor_pct:
        logger.warning(
            "[EXIT PROFIT PROTECT] FLOOR symbol=%s side=%s label=%s price=%.4f entry=%.4f pnl=%.4f max=%.4f start=%.4f floor=%.4f hold=%.1fs",
            symbol, side, label, price, entry, pnl, max_profit, start_pct, floor_pct, hold_sec,
        )
        return "PROFIT_PROTECT_FLOOR"

    # 3) 高値利益から -0.10% 戻したら逃げる
    if max_profit >= start_pct and (max_profit - pnl) >= giveback_pct:
        logger.warning(
            "[EXIT PROFIT PROTECT] GIVEBACK symbol=%s side=%s label=%s price=%.4f entry=%.4f pnl=%.4f max=%.4f giveback=%.4f hold=%.1fs",
            symbol, side, label, price, entry, pnl, max_profit, giveback_pct, hold_sec,
        )
        return "PROFIT_PROTECT_GIVEBACK"

    logger.debug(
        "[EXIT PROFIT PROTECT] HOLD symbol=%s side=%s label=%s pnl=%.4f max=%.4f price=%.4f entry=%.4f hold=%.1fs",
        symbol, side, label, pnl, max_profit, price, entry, hold_sec,
    )
    return None


def _patched_check_normal_exit(pos: Any, price: float, now: dt.datetime):
    reason = _profit_protect_reason(pos, price, now, label="NORMAL")
    if reason:
        return reason
    if callable(_ORIG_NORMAL):
        return _ORIG_NORMAL(pos, price, now)
    return None


def _patched_check_tonosama_exit(pos: Any, price: float, now: dt.datetime):
    reason = _profit_protect_reason(pos, price, now, label="TONOSAMA")
    if reason:
        return reason
    if callable(_ORIG_TONOSAMA):
        return _ORIG_TONOSAMA(pos, price, now)
    return None


def install() -> bool:
    global _INSTALLED, _ORIG_NORMAL, _ORIG_TONOSAMA
    if _INSTALLED:
        return True

    try:
        import trading.handlers.exit_handler as eh

        cur_normal = getattr(eh, "check_normal_exit", None)
        cur_tonosama = getattr(eh, "check_tonosama_exit", None)

        if getattr(cur_normal, "_exit_profit_protect_patch_v1", False):
            _INSTALLED = True
            return True

        _ORIG_NORMAL = cur_normal
        _ORIG_TONOSAMA = cur_tonosama

        _patched_check_normal_exit._exit_profit_protect_patch_v1 = True  # type: ignore[attr-defined]
        _patched_check_tonosama_exit._exit_profit_protect_patch_v1 = True  # type: ignore[attr-defined]

        eh.check_normal_exit = _patched_check_normal_exit
        eh.check_tonosama_exit = _patched_check_tonosama_exit

        _INSTALLED = True
        logger.warning(
            "[EXIT PROFIT PROTECT] installed enabled=%s take=%.4f start=%.4f floor=%.4f giveback=%.4f min_hold=%.1f",
            _env_bool("EXIT_PROFIT_PROTECT_ENABLED", True),
            _env_float("EXIT_PROFIT_TAKE_PCT", 0.0030),
            _env_float("EXIT_PROFIT_PROTECT_START_PCT", 0.0020),
            _env_float("EXIT_PROFIT_PROTECT_FLOOR_PCT", 0.0010),
            _env_float("EXIT_PROFIT_GIVEBACK_PCT", 0.0010),
            _env_float("EXIT_PROFIT_MIN_HOLD_SEC", 0.0),
        )
        return True
    except Exception as e:
        logger.exception("[EXIT PROFIT PROTECT] install failed err=%s", e)
        return False


try:
    install()
except Exception as e:
    logger.exception("[EXIT PROFIT PROTECT] auto install failed err=%s", e)

__all__ = ["install"]
