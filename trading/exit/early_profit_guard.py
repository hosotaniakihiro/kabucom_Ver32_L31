# ============================================================
# File   : trading/exit/early_profit_guard.py
# Version: V1.8-PERSIST-STAGNATION-PROGRESS-STATE
# ------------------------------------------------------------
# エントリー後の高値/安値/保持開始時刻をSQLiteに保存し、
# main.py再起動後も復元する。
#
# 追加修正:
#   - エントリー直後だけでなく、常時「有利方向へ進んでいるか」を監視
#   - BUY : 直近の高値更新から一定時間進展なしなら EARLY_STAGNATION_BUY
#   - SELL: 直近の安値更新から一定時間進展なしなら EARLY_STAGNATION_SELL
#   - last_progress_at / last_progress_price をSQLiteへ保存して再起動後も継続
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

_STATE: dict[str, dict[str, Any]] = {}


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


def _sf(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        if x != x or x in (float("inf"), float("-inf")):
            return float(default)
        return x
    except Exception:
        return float(default)


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        try:
            if isinstance(obj, dict) and name in obj:
                return obj.get(name)
            if hasattr(obj, name):
                return getattr(obj, name)
        except Exception:
            pass
    return default


def _side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "BUY_CREDIT", "LONG", "L", "2", "02", "20", "B", "信用買", "買", "買建", "買い", "新規買"}:
        return "BUY"
    if s in {"SELL", "SELL_CREDIT", "SHORT", "S", "1", "01", "10", "信用売", "売", "売建", "売り", "新規売"}:
        return "SELL"
    return s


def _parse_time(v: Any) -> Optional[dt.datetime]:
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None) if v.tzinfo else v
    try:
        s = str(v or "").strip()
        if not s:
            return None
        s = s.replace("T", " ").split("+", 1)[0].rstrip("Z")
        return dt.datetime.fromisoformat(s)
    except Exception:
        return None


def _external_hold_seconds(pos: dict[str, Any], ctx: Any, now: dt.datetime) -> float:
    t = _parse_time(_get(ctx, "entry_time", default=None) or _get(pos, "entry_time", "created_at", "timestamp", default=None))
    if t is None:
        return 0.0
    try:
        return max(0.0, (now - t).total_seconds())
    except Exception:
        return 0.0


def _position_identity(pos: dict[str, Any]) -> str:
    try:
        for name in ("hold_id", "HoldID", "execution_id", "ExecutionID", "id", "order_id"):
            v = _get(pos, name, default=None)
            if v is not None and str(v).strip():
                return f"{name}:{str(v).strip()}"
    except Exception:
        pass
    return ""


def _key(symbol: str, side: str, entry_price: float, pos: dict[str, Any] | None = None) -> str:
    pid = _position_identity(pos or {})
    if pid:
        return f"{str(symbol)}|{side}|{entry_price:.6f}|{pid}"
    return f"{str(symbol)}|{side}|{entry_price:.6f}"


def _extract_high_low(ctx: Any, bar5s: Any, entry_price: float, current_price: float) -> tuple[float, float]:
    highs = [entry_price, current_price]
    lows = [entry_price, current_price]
    for obj in (ctx, bar5s):
        for name in ("high_after_entry", "highest_price", "max_price", "high", "High", "h", "H"):
            x = _sf(_get(obj, name, default=None), 0.0)
            if x > 0:
                highs.append(x)
        for name in ("low_after_entry", "lowest_price", "min_price", "low", "Low", "l", "L"):
            x = _sf(_get(obj, name, default=None), 0.0)
            if x > 0:
                lows.append(x)
    return max(highs), min(lows)


def _load_persisted_state(key: str) -> dict[str, Any] | None:
    if not _env_bool("EARLY_PROFIT_STATE_PERSIST_ENABLED", True):
        return None
    try:
        from trading.exit.early_profit_state_store import load_state
        return load_state(key)
    except Exception:
        logger.exception("[EARLY PROFIT GUARD] persisted state load failed key=%s", key)
        return None


def _save_persisted_state(
    *,
    key: str,
    symbol: str,
    side: str,
    entry_price: float,
    high: float,
    low: float,
    started_at: dt.datetime | None,
    updated_at: dt.datetime | None,
    last_progress_at: dt.datetime | None,
    last_progress_price: float,
) -> None:
    if not _env_bool("EARLY_PROFIT_STATE_PERSIST_ENABLED", True):
        return
    try:
        from trading.exit.early_profit_state_store import save_state
        save_state(
            state_key=key,
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            high_after_entry=high,
            low_after_entry=low,
            started_at=started_at,
            updated_at=updated_at,
            last_progress_at=last_progress_at,
            last_progress_price=last_progress_price,
        )
    except TypeError:
        # 旧 state_store がローカルに残っている場合でも落とさない。
        try:
            from trading.exit.early_profit_state_store import save_state
            save_state(
                state_key=key,
                symbol=symbol,
                side=side,
                entry_price=entry_price,
                high_after_entry=high,
                low_after_entry=low,
                started_at=started_at,
                updated_at=updated_at,
            )
        except Exception:
            logger.exception("[EARLY PROFIT GUARD] persisted state save legacy failed key=%s", key)
    except Exception:
        logger.exception("[EARLY PROFIT GUARD] persisted state save failed key=%s", key)


def _progress_baseline(side: str, high: float, low: float, entry_price: float, current_price: float) -> float:
    if side == "BUY":
        return max(high, entry_price, current_price)
    return min(x for x in (low, entry_price, current_price) if x > 0)


def _is_progress_update(*, side: str, prev_progress_price: float, high: float, low: float, entry_price: float, current_price: float, progress_need: float) -> tuple[bool, float, float]:
    """有利方向へ十分に進んだかを判定する。

    returns:
        updated, new_progress_price, progress_move_pct
    """
    need = max(float(progress_need or 0.0), 0.0)
    prev = _sf(prev_progress_price, 0.0)
    if prev <= 0:
        prev = entry_price

    if side == "BUY":
        candidate = max(high, current_price)
        move = (candidate - prev) / prev if prev > 0 else 0.0
        if move >= need:
            return True, candidate, move
        return False, prev, move

    candidate = min(x for x in (low, current_price) if x > 0)
    move = (prev - candidate) / prev if prev > 0 else 0.0
    if move >= need:
        return True, candidate, move
    return False, prev, move


def _tracked(
    symbol: str,
    side: str,
    entry_price: float,
    current_price: float,
    pos: dict[str, Any],
    ctx: Any,
    now: dt.datetime,
    bar5s: Any,
    progress_need: float,
) -> tuple[float, float, float, float, dt.datetime | None, float]:
    high0, low0 = _extract_high_low(ctx, bar5s, entry_price, current_price)
    key = _key(symbol, side, entry_price, pos)
    st = _STATE.get(key)
    ext_hold = _external_hold_seconds(pos, ctx, now)
    ext_started = now - dt.timedelta(seconds=ext_hold) if ext_hold > 0 else now

    if not st:
        persisted = _load_persisted_state(key)
        if persisted:
            p_high = _sf(persisted.get("high"), high0)
            p_low = _sf(persisted.get("low"), low0)
            p_started = persisted.get("started_at") if isinstance(persisted.get("started_at"), dt.datetime) else ext_started
            p_progress_at = persisted.get("last_progress_at") if isinstance(persisted.get("last_progress_at"), dt.datetime) else p_started
            p_progress_price = _sf(persisted.get("last_progress_price"), 0.0)
            st = {
                "high": max(p_high, high0),
                "low": min(p_low if p_low > 0 else low0, low0),
                "started_at": p_started,
                "updated_at": now,
                "last_progress_at": p_progress_at,
                "last_progress_price": p_progress_price or _progress_baseline(side, max(p_high, high0), min(p_low if p_low > 0 else low0, low0), entry_price, current_price),
            }
            _STATE[key] = st
            logger.warning(
                "[EARLY PROFIT GUARD] restored state symbol=%s side=%s entry=%.4f high=%.4f low=%.4f started_at=%s last_progress_at=%s last_progress_price=%.4f",
                symbol, side, entry_price, st["high"], st["low"], st["started_at"], st.get("last_progress_at"), _sf(st.get("last_progress_price"), 0.0),
            )
        else:
            baseline = _progress_baseline(side, high0, low0, entry_price, current_price)
            st = {
                "high": high0,
                "low": low0,
                "started_at": ext_started,
                "updated_at": now,
                "last_progress_at": ext_started,
                "last_progress_price": baseline,
            }
            _STATE[key] = st
            logger.warning(
                "[EARLY PROFIT GUARD] tracking start symbol=%s side=%s entry=%.4f price=%.4f high=%.4f low=%.4f started_at=%s hold=%.1fs last_progress_price=%.4f",
                symbol, side, entry_price, current_price, high0, low0, st["started_at"], ext_hold, baseline,
            )
    else:
        old_high = _sf(st.get("high"), high0)
        old_low = _sf(st.get("low"), low0)
        st["high"] = max(old_high, high0)
        st["low"] = min(old_low if old_low > 0 else low0, low0)
        try:
            if isinstance(st.get("started_at"), dt.datetime) and ext_started < st["started_at"]:
                st["started_at"] = ext_started
        except Exception:
            pass
        if not isinstance(st.get("last_progress_at"), dt.datetime):
            st["last_progress_at"] = st.get("started_at", ext_started)
        if _sf(st.get("last_progress_price"), 0.0) <= 0:
            st["last_progress_price"] = _progress_baseline(side, st["high"], st["low"], entry_price, current_price)
        st["updated_at"] = now

    # 常時進展チェック: BUYは高値を、SELLは安値を一定以上更新した時だけ last_progress_at を更新する。
    updated, new_progress_price, progress_move = _is_progress_update(
        side=side,
        prev_progress_price=_sf(st.get("last_progress_price"), 0.0),
        high=_sf(st.get("high"), high0),
        low=_sf(st.get("low"), low0),
        entry_price=entry_price,
        current_price=current_price,
        progress_need=progress_need,
    )
    if updated:
        st["last_progress_at"] = now
        st["last_progress_price"] = new_progress_price
        logger.warning(
            "[EARLY PROFIT GUARD] progress updated symbol=%s side=%s price=%.4f progress_price=%.4f move=%.4f%% need=%.4f%%",
            symbol, side, current_price, new_progress_price, progress_move * 100.0, progress_need * 100.0,
        )

    try:
        setattr(ctx, "high_after_entry", float(st["high"]))
        setattr(ctx, "low_after_entry", float(st["low"]))
        setattr(ctx, "last_progress_at", st.get("last_progress_at"))
        setattr(ctx, "last_progress_price", float(_sf(st.get("last_progress_price"), 0.0)))
    except Exception:
        pass

    started_at = st.get("started_at", now)
    try:
        state_hold = max(0.0, (now - started_at).total_seconds()) if isinstance(started_at, dt.datetime) else 0.0
    except Exception:
        state_hold = 0.0

    last_progress_at = st.get("last_progress_at")
    try:
        idle = max(0.0, (now - last_progress_at).total_seconds()) if isinstance(last_progress_at, dt.datetime) else max(float(ext_hold), float(state_hold))
    except Exception:
        idle = max(float(ext_hold), float(state_hold))

    _save_persisted_state(
        key=key,
        symbol=symbol,
        side=side,
        entry_price=entry_price,
        high=float(st["high"]),
        low=float(st["low"]),
        started_at=st.get("started_at"),
        updated_at=now,
        last_progress_at=st.get("last_progress_at"),
        last_progress_price=float(_sf(st.get("last_progress_price"), 0.0)),
    )

    return float(st["high"]), float(st["low"]), max(float(ext_hold), float(state_hold)), idle, st.get("last_progress_at"), float(_sf(st.get("last_progress_price"), 0.0))


def judge_early_profit_guard(*, symbol: str, pos: dict[str, Any], side: str, entry_price: float, current_price: float, ctx: Any, now: dt.datetime, bar5s: Any = None) -> Tuple[bool, str]:
    if not _env_bool("EARLY_PROFIT_GUARD_ENABLED", True):
        return False, ""

    side = _side(side)
    entry_price = _sf(entry_price)
    current_price = _sf(current_price)
    if side not in {"BUY", "SELL"} or entry_price <= 0 or current_price <= 0:
        logger.warning("[EARLY PROFIT GUARD] skip invalid symbol=%s side=%s entry=%.4f price=%.4f", symbol, side, entry_price, current_price)
        return False, ""

    threshold = _env_float("TRAILING_DRAWDOWN_PCT", 0.0030)
    take_profit = _env_float("TAKE_PROFIT_PCT", _env_float("EARLY_TAKE_PROFIT_PCT", 0.0))

    # 既存の「エントリー後5分で一度も動かなければEXIT」基準
    no_progress_sec = _env_float("EARLY_NO_PROGRESS_SECONDS", 300.0)
    no_progress_need = _env_float("EARLY_NO_PROGRESS_NEED_PCT", 0.0005)

    # 追加の「常時停滞監視」基準。未指定なら既存基準と同じ5分/0.05%。
    stagnation_enabled = _env_bool("EARLY_STAGNATION_EXIT_ENABLED", True)
    stagnation_sec = _env_float("EARLY_STAGNATION_SECONDS", no_progress_sec)
    stagnation_need = _env_float("EARLY_STAGNATION_NEED_PCT", no_progress_need)

    high, low, hold, idle, last_progress_at, last_progress_price = _tracked(
        symbol,
        side,
        entry_price,
        current_price,
        pos,
        ctx,
        now,
        bar5s,
        progress_need=stagnation_need,
    )

    if side == "BUY":
        profit = (current_price - entry_price) / entry_price
        adverse_from_entry = (entry_price - current_price) / entry_price
        adverse_from_extreme = (high - current_price) / high if high > 0 else 0.0
        max_profit = (high - entry_price) / entry_price
    else:
        profit = (entry_price - current_price) / entry_price
        adverse_from_entry = (current_price - entry_price) / entry_price
        adverse_from_extreme = (current_price - low) / low if low > 0 else 0.0
        max_profit = (entry_price - low) / entry_price

    logger.warning(
        "[EARLY PROFIT GUARD] check symbol=%s side=%s hold=%.1fs idle=%.1fs entry=%.4f price=%.4f high=%.4f low=%.4f profit=%.4f%% entry_adverse=%.4f%% extreme_adverse=%.4f%% threshold=%.4f%% no_progress_sec=%.1f no_progress_need=%.4f%% stagnation_enabled=%s stagnation_sec=%.1f stagnation_need=%.4f%% last_progress_at=%s last_progress_price=%.4f",
        symbol,
        side,
        hold,
        idle,
        entry_price,
        current_price,
        high,
        low,
        profit * 100.0,
        adverse_from_entry * 100.0,
        adverse_from_extreme * 100.0,
        threshold * 100.0,
        no_progress_sec,
        no_progress_need * 100.0,
        stagnation_enabled,
        stagnation_sec,
        stagnation_need * 100.0,
        last_progress_at,
        last_progress_price,
    )

    if threshold > 0 and adverse_from_entry >= threshold:
        reason = f"ENTRY_ADVERSE_EXIT_{side}"
        logger.warning("[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s", symbol, reason)
        return True, reason

    if threshold > 0 and adverse_from_extreme >= threshold:
        reason = f"TRAILING_DRAWDOWN_{side}"
        logger.warning("[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s", symbol, reason)
        return True, reason

    if take_profit > 0 and profit >= take_profit:
        reason = f"TAKE_PROFIT_{side}"
        logger.warning("[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s", symbol, reason)
        return True, reason

    # 既存仕様: エントリー後、最大含み益が一度も必要幅に届かなければ5分で撤退。
    if hold >= no_progress_sec and max_profit < no_progress_need:
        reason = f"EARLY_NO_PROGRESS_{side}"
        logger.warning(
            "[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s hold=%.1fs max_profit=%.4f%% need=%.4f%%",
            symbol,
            reason,
            hold,
            max_profit * 100.0,
            no_progress_need * 100.0,
        )
        return True, reason

    # 追加仕様: 一度進んだあとでも、直近の有利方向更新から5分止まったら撤退。
    if stagnation_enabled and stagnation_sec > 0 and idle >= stagnation_sec:
        reason = f"EARLY_STAGNATION_{side}"
        logger.warning(
            "[EARLY PROFIT GUARD] EXIT symbol=%s reason=%s idle=%.1fs need_idle=%.1fs last_progress_at=%s last_progress_price=%.4f price=%.4f progress_need=%.4f%%",
            symbol,
            reason,
            idle,
            stagnation_sec,
            last_progress_at,
            last_progress_price,
            current_price,
            stagnation_need * 100.0,
        )
        return True, reason

    return False, ""


__all__ = ["judge_early_profit_guard"]
