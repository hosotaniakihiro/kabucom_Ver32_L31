# -*- coding: utf-8 -*-
"""
trading/exit/limit_up_exit.py

買い建玉のストップ高イグジット (2026-10-21 ユーザー判断)。

  - 現値がストップ高に届いたら売る (LIMIT_UP_TOUCH_EXIT)。
  - ストップ高の 1 ティック手前まで来たのに勢いが無ければ、そこで売る (LIMIT_UP_STALL_EXIT)。
    勢いが無い = 1 ティック手前に来てから LIMIT_UP_EXIT_STALL_SEC 秒たってもストップ高に届かない、
    または 1 ティック手前から下に戻った。

前日終値は ranking の騰落率から逆算する (surge_short と同じ)。1 日 1 回取れれば以後はキャッシュ。
決済は既存の finalize_exit (返済成行)。ストップ高では買い気配が並んでいるので成行でその値段に当たる。

  LIMIT_UP_EXIT_ENABLED=1
  LIMIT_UP_EXIT_STALL_SEC=10
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_PREV_CLOSE: Dict[Tuple[str, str], float] = {}
_NEAR_SINCE: Dict[str, dt.datetime] = {}


def _enabled() -> bool:
    return str(os.getenv("LIMIT_UP_EXIT_ENABLED", "1")).strip().lower() not in ("0", "false", "no", "off")


def _stall_sec() -> float:
    try:
        return float(os.getenv("LIMIT_UP_EXIT_STALL_SEC", "10") or 10)
    except Exception:
        return 10.0


def _prev_close(symbol: str, now: dt.datetime) -> Optional[float]:
    key = (now.strftime("%Y%m%d"), symbol)
    if key in _PREV_CLOSE:
        return _PREV_CLOSE[key]
    try:
        from trading.handlers.entry_order_builder import _day_change_liquidity_map

        info = (_day_change_liquidity_map() or {}).get(symbol) or {}
        price = float(info.get("current_price") or 0)
        chg = float(info.get("change_percentage") or 0)
    except Exception:
        logger.debug("[LIMIT UP EXIT] liquidity map unavailable symbol=%s", symbol, exc_info=True)
        return None
    if price <= 0 or chg <= -100:
        return None
    # 端数で制限値幅の境目 (1,000 円など) をまたがないよう 1 円に丸める (surge_short と同じ)。
    pc = float(round(price / (1 + chg / 100.0)))
    if pc <= 0:
        return None
    _PREV_CLOSE[key] = pc
    return pc


def judge_limit_up_exit(*, symbol: str, side: str, current_price: float, now: dt.datetime) -> Tuple[bool, str, Dict[str, Any]]:
    """(exit, reason, detail)。BUY 建玉だけを見る。"""
    if not _enabled() or str(side).upper() != "BUY":
        return False, "", {}
    sym = str(symbol or "").strip()
    px = float(current_price or 0)
    if not sym or px <= 0:
        return False, "", {}
    pc = _prev_close(sym, now)
    if not pc:
        return False, "", {}

    from trading.strategies.surge_short import limit_up_price
    from utils_common import get_order_tick_size

    lu = float(limit_up_price(pc))
    tick = float(get_order_tick_size(lu - 1e-6))
    near = lu - tick
    detail = {"prev_close": pc, "limit_up": lu, "near": near, "price": px}

    if px >= lu - 1e-9:
        _NEAR_SINCE.pop(sym, None)
        return True, f"LIMIT_UP_TOUCH_EXIT limit_up={lu:.1f} price={px:.1f}", detail

    since = _NEAR_SINCE.get(sym)
    if px >= near - 1e-9:
        if since is None:
            _NEAR_SINCE[sym] = now
            logger.warning("[LIMIT UP EXIT] near limit up symbol=%s detail=%s", sym, detail)
            return False, "", detail
        waited = (now - since).total_seconds()
        detail["waited_sec"] = round(waited, 1)
        if waited >= _stall_sec():
            _NEAR_SINCE.pop(sym, None)
            return True, f"LIMIT_UP_STALL_EXIT near={near:.1f} waited={waited:.0f}s", detail
        return False, "", detail

    if since is not None:
        # 1 ティック手前まで来てから下に戻った = 勢いが無い。
        _NEAR_SINCE.pop(sym, None)
        detail["waited_sec"] = round((now - since).total_seconds(), 1)
        return True, f"LIMIT_UP_STALL_EXIT fell_back near={near:.1f} price={px:.1f}", detail
    return False, "", detail


__all__ = ["judge_limit_up_exit"]
