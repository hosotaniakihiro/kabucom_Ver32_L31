# ============================================================
# File   : trading/exit/early_profit_guard.py
# Version: V1.1-EARLY-PROFIT-TRAILING-DRAWDOWN-GUARD
# ------------------------------------------------------------
# エントリー直後に一瞬プラスになってからマイナス化する問題を抑える。
#
# ルール:
#   1) トレーリング撤退
#      BUY : エントリー後の最高値から -0.30% 下落したら撤退
#      SELL: エントリー後の最安値から +0.30% 上昇したら撤退
#   2) 建値撤退ガード
#      一度 +0.10% 以上の含み益を見た後、建値近辺まで戻ったら撤退
#   3) 早期損切り
#      エントリー後30秒以内に -0.20% 以下なら損切り
#   4) 進まない撤退
#      エントリー後15秒以内に +0.05% も進まなければ撤退
#   5) 固定早期利確
#      デフォルト無効。EARLY_TAKE_PROFIT_PCT を 0 より大きくした場合だけ有効。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


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


def _get_extreme_after_entry(*, ctx: Any, price: float) -> tuple[float, float]:
    high = price
    low = price
    for name in ["high_after_entry", "highest_price", "max_price", "high"]:
        try:
            v = getattr(ctx, name, None)
            if v is not None:
                high = max(high, _safe_float(v, high))
        except Exception:
            pass
    for name in ["low_after_entry", "lowest_price", "min_price", "low"]:
        try:
            v = getattr(ctx, name, None)
            if v is not None:
                low = min(low, _safe_float(v, low))
        except Exception:
            pass
    return high, low


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
    high_after_entry, low_after_entry = _get_extreme_after_entry(ctx=ctx, price=current_price)

    if side == "BUY":
        profit_pct = (current_price - entry_price) / entry_price
        max_profit_pct = (high_after_entry - entry_price) / entry_price
        trailing_drawdown_now = (high_after_entry - current_price) / high_after_entry if high_after_entry > 0 else 0.0
    else:
        profit_pct = (entry_price - current_price) / entry_price
        max_profit_pct = (entry_price - low_after_entry) / entry_price
        trailing_drawdown_now = (current_price - low_after_entry) / low_after_entry if low_after_entry > 0 else 0.0

    # BUY: 最高値から0.30%下落、SELL: 最安値から0.30%上昇で撤退。
    if trailing_drawdown_pct > 0 and max_profit_pct > 0 and trailing_drawdown_now >= trailing_drawdown_pct:
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
            "[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s hold=%.1fs profit=%.4f%% max_profit=%.4f%% entry=%.4f price=%.4f",
            symbol,
            reason,
            hold_seconds,
            profit_pct * 100.0,
            max_profit_pct * 100.0,
            entry_price,
            current_price,
        )
        return True, reason

    if hold_seconds <= early_seconds:
        if take_profit_pct > 0 and profit_pct >= take_profit_pct:
            reason = f"EARLY_TAKE_PROFIT_{side}"
            logger.warning(
                "[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s hold=%.1fs profit=%.4f%% entry=%.4f price=%.4f",
                symbol,
                reason,
                hold_seconds,
                profit_pct * 100.0,
                entry_price,
                current_price,
            )
            return True, reason

        if profit_pct <= -stop_loss_pct:
            reason = f"EARLY_STOP_LOSS_{side}"
            logger.warning(
                "[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s hold=%.1fs profit=%.4f%% entry=%.4f price=%.4f",
                symbol,
                reason,
                hold_seconds,
                profit_pct * 100.0,
                entry_price,
                current_price,
            )
            return True, reason

        if hold_seconds >= no_progress_seconds and max_profit_pct < no_progress_need_pct:
            reason = f"EARLY_NO_PROGRESS_{side}"
            logger.warning(
                "[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s hold=%.1fs profit=%.4f%% max_profit=%.4f%% entry=%.4f price=%.4f",
                symbol,
                reason,
                hold_seconds,
                profit_pct * 100.0,
                max_profit_pct * 100.0,
                entry_price,
                current_price,
            )
            return True, reason

    return False, ""


__all__ = ["judge_early_profit_guard"]
