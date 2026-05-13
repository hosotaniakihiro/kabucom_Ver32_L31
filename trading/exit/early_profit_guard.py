# ============================================================
# File   : trading/exit/early_profit_guard.py
# Version: V1.2-EARLY-PROFIT-TRAILING-STOP-WITH-MEMORY
# ------------------------------------------------------------
# エントリー後の高値/安値をEXIT側でも保持して、
# BUY : エントリー後の最高値から -0.30% 下落したらEXIT
# SELL: エントリー後の最安値から +0.30% 上昇したらEXIT
# を確実に判定する。
#
# 重要:
#   ctx.high_after_entry / low_after_entry が更新されない環境でも、
#   このモジュール内のメモリで銘柄ごとの最高値/最安値を追跡する。
#   また、建値を初期最高値/初期最安値として扱うため、
#   エントリー直後に建値から -0.30% / +0.30% 逆行した場合もEXITする。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

# symbol -> tracking state
_TRAILING_STATE: dict[str, dict[str, Any]] = {}


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        if x != x or x in (float("inf"), float("-inf")):
            return float(default)
        return x
    except Exception:
        return float(default)


def _normalize_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "BUY_CREDIT", "LONG", "2", "信用買"}:
        return "BUY"
    if s in {"SELL", "SELL_CREDIT", "SHORT", "1", "信用売"}:
        return "SELL"
    return s


def _parse_datetime(v: Any) -> Optional[dt.datetime]:
    if isinstance(v, dt.datetime):
        try:
            if v.tzinfo is not None:
                return v.replace(tzinfo=None)
        except Exception:
            pass
        return v
    try:
        s = str(v or "").strip()
        if not s:
            return None
        s = s.replace("T", " ").split("+", 1)[0]
        if s.endswith("Z"):
            s = s[:-1]
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _pos_get(pos: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        try:
            if isinstance(pos, dict) and name in pos:
                return pos.get(name)
            if hasattr(pos, name):
                return getattr(pos, name)
        except Exception:
            continue
    return default


def _get_hold_seconds(pos: dict[str, Any], ctx: Any, now: dt.datetime) -> float:
    entry_time = None
    try:
        entry_time = getattr(ctx, "entry_time", None)
    except Exception:
        entry_time = None
    if entry_time is None:
        entry_time = _pos_get(pos, "entry_time", "created_at", "timestamp", default=None)
    t = _parse_datetime(entry_time)
    if t is None:
        return 0.0
    try:
        return max(0.0, (now - t).total_seconds())
    except Exception:
        return 0.0


def _get_ctx_extreme(*, ctx: Any) -> tuple[Optional[float], Optional[float]]:
    high = None
    low = None
    for name in ["high_after_entry", "highest_price", "max_price", "high"]:
        try:
            v = getattr(ctx, name, None)
            x = _safe_float(v, 0.0)
            if x > 0:
                high = x if high is None else max(high, x)
        except Exception:
            pass
    for name in ["low_after_entry", "lowest_price", "min_price", "low"]:
        try:
            v = getattr(ctx, name, None)
            x = _safe_float(v, 0.0)
            if x > 0:
                low = x if low is None else min(low, x)
        except Exception:
            pass
    return high, low


def _tracking_key(symbol: str, side: str, entry_price: float, pos: dict[str, Any], ctx: Any) -> str:
    entry_time = None
    try:
        entry_time = getattr(ctx, "entry_time", None)
    except Exception:
        entry_time = None
    if entry_time is None:
        entry_time = _pos_get(pos, "entry_time", "created_at", "timestamp", default="")
    return f"{str(symbol)}|{side}|{float(entry_price):.6f}|{str(entry_time)}"


def _get_tracked_extreme(
    *,
    symbol: str,
    side: str,
    entry_price: float,
    current_price: float,
    pos: dict[str, Any],
    ctx: Any,
    now: dt.datetime,
) -> tuple[float, float]:
    """
    建値を初期高値/初期安値として、以後の最高値/最安値をメモリ追跡する。
    ctx側に high_after_entry / low_after_entry があれば、それも取り込む。
    """
    key = _tracking_key(symbol, side, entry_price, pos, ctx)
    ctx_high, ctx_low = _get_ctx_extreme(ctx=ctx)

    high0 = max(entry_price, current_price, ctx_high or 0.0)
    low_candidates = [entry_price, current_price]
    if ctx_low and ctx_low > 0:
        low_candidates.append(ctx_low)
    low0 = min(low_candidates)

    state = _TRAILING_STATE.get(key)
    if not state:
        state = {
            "symbol": str(symbol),
            "side": side,
            "entry_price": float(entry_price),
            "high_after_entry": float(high0),
            "low_after_entry": float(low0),
            "created_at": now,
            "updated_at": now,
        }
        _TRAILING_STATE[key] = state
        logger.warning(
            "[EARLY PROFIT GUARD] tracking start symbol=%s side=%s entry=%.4f price=%.4f high=%.4f low=%.4f",
            symbol,
            side,
            entry_price,
            current_price,
            state["high_after_entry"],
            state["low_after_entry"],
        )
    else:
        state["high_after_entry"] = max(
            _safe_float(state.get("high_after_entry"), high0),
            float(high0),
        )
        state["low_after_entry"] = min(
            _safe_float(state.get("low_after_entry"), low0),
            float(low0),
        )
        state["updated_at"] = now

    # ctxにも戻しておく。別のEXITロジックが参照できるようにする。
    try:
        setattr(ctx, "high_after_entry", float(state["high_after_entry"]))
        setattr(ctx, "low_after_entry", float(state["low_after_entry"]))
    except Exception:
        pass

    # メモリ肥大防止。古い追跡を軽く掃除する。
    try:
        if len(_TRAILING_STATE) > 500:
            cutoff = now - dt.timedelta(hours=4)
            stale = [k for k, v in _TRAILING_STATE.items() if v.get("updated_at", now) < cutoff]
            for k in stale[:200]:
                _TRAILING_STATE.pop(k, None)
    except Exception:
        pass

    return float(state["high_after_entry"]), float(state["low_after_entry"])


def judge_early_profit_guard(
    *,
    symbol: str,
    pos: dict[str, Any],
    side: str,
    entry_price: float,
    current_price: float,
    ctx: Any,
    now: dt.datetime,
) -> Tuple[bool, str]:
    if not _env_bool("EARLY_PROFIT_GUARD_ENABLED", True):
        return False, ""

    side = _normalize_side(side)
    entry_price = _safe_float(entry_price)
    current_price = _safe_float(current_price)
    if side not in {"BUY", "SELL"} or entry_price <= 0 or current_price <= 0:
        return False, ""

    early_seconds = _env_float("EARLY_EXIT_SECONDS", 30.0)
    take_profit_pct = _env_float("EARLY_TAKE_PROFIT_PCT", 0.0)
    stop_loss_pct = _env_float("EARLY_STOP_LOSS_PCT", 0.0020)
    no_progress_seconds = _env_float("EARLY_NO_PROGRESS_SECONDS", 15.0)
    no_progress_need_pct = _env_float("EARLY_NO_PROGRESS_NEED_PCT", 0.0005)
    breakeven_activate_pct = _env_float("BREAKEVEN_PROTECT_ACTIVATE_PCT", 0.0010)
    breakeven_exit_buffer_pct = _env_float("BREAKEVEN_PROTECT_EXIT_BUFFER_PCT", 0.0002)
    trailing_drawdown_pct = _env_float("TRAILING_DRAWDOWN_PCT", 0.0030)

    hold_seconds = _get_hold_seconds(pos, ctx, now)
    high_after_entry, low_after_entry = _get_tracked_extreme(
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        current_price=current_price,
        pos=pos,
        ctx=ctx,
        now=now,
    )

    if side == "BUY":
        profit_pct = (current_price - entry_price) / entry_price
        max_profit_pct = (high_after_entry - entry_price) / entry_price
        trailing_drawdown_now = (high_after_entry - current_price) / high_after_entry if high_after_entry > 0 else 0.0
    else:
        profit_pct = (entry_price - current_price) / entry_price
        max_profit_pct = (entry_price - low_after_entry) / entry_price
        trailing_drawdown_now = (current_price - low_after_entry) / low_after_entry if low_after_entry > 0 else 0.0

    # BUY: 最高値から0.30%下落、SELL: 最安値から0.30%上昇で撤退。
    # 建値を初期高値/初期安値にしているため、建値から即0.30%逆行した場合もここで拾う。
    if trailing_drawdown_pct > 0 and trailing_drawdown_now >= trailing_drawdown_pct:
        reason = f"TRAILING_DRAWDOWN_{side}"
        logger.warning(
            "[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s hold=%.1fs profit=%.4f%% max_profit=%.4f%% trailing=%.4f%% entry=%.4f price=%.4f high=%.4f low=%.4f",
            symbol,
            reason,
            hold_seconds,
            profit_pct * 100.0,
            max_profit_pct * 100.0,
            trailing_drawdown_now * 100.0,
            entry_price,
            current_price,
            high_after_entry,
            low_after_entry,
        )
        return True, reason

    # 一度プラスになった後、建値近辺まで戻ったら撤退。
    if max_profit_pct >= breakeven_activate_pct and profit_pct <= breakeven_exit_buffer_pct:
        reason = f"BREAKEVEN_PROTECT_{side}"
        logger.warning(
            "[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s hold=%.1fs profit=%.4f%% max_profit=%.4f%% entry=%.4f price=%.4f high=%.4f low=%.4f",
            symbol,
            reason,
            hold_seconds,
            profit_pct * 100.0,
            max_profit_pct * 100.0,
            entry_price,
            current_price,
            high_after_entry,
            low_after_entry,
        )
        return True, reason

    if hold_seconds <= early_seconds:
        if take_profit_pct > 0 and profit_pct >= take_profit_pct:
            reason = f"EARLY_TAKE_PROFIT_{side}"
            logger.warning(
                "[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s hold=%.1fs profit=%.4f%% entry=%.4f price=%.4f high=%.4f low=%.4f",
                symbol,
                reason,
                hold_seconds,
                profit_pct * 100.0,
                entry_price,
                current_price,
                high_after_entry,
                low_after_entry,
            )
            return True, reason

        if profit_pct <= -stop_loss_pct:
            reason = f"EARLY_STOP_LOSS_{side}"
            logger.warning(
                "[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s hold=%.1fs profit=%.4f%% entry=%.4f price=%.4f high=%.4f low=%.4f",
                symbol,
                reason,
                hold_seconds,
                profit_pct * 100.0,
                entry_price,
                current_price,
                high_after_entry,
                low_after_entry,
            )
            return True, reason

        if hold_seconds >= no_progress_seconds and max_profit_pct < no_progress_need_pct:
            reason = f"EARLY_NO_PROGRESS_{side}"
            logger.warning(
                "[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s hold=%.1fs profit=%.4f%% max_profit=%.4f%% entry=%.4f price=%.4f high=%.4f low=%.4f",
                symbol,
                reason,
                hold_seconds,
                profit_pct * 100.0,
                max_profit_pct * 100.0,
                entry_price,
                current_price,
                high_after_entry,
                low_after_entry,
            )
            return True, reason

    return False, ""


__all__ = ["judge_early_profit_guard"]
