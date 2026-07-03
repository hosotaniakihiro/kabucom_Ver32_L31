# -*- coding: utf-8 -*-
"""
Intraday credit control and end-of-day repayment guard.

Purpose
-------
- Before the cutoff, new credit entries use day-trade margin.
- At/after the cutoff, new entries must not use day-trade margin; if an entry is still
  placed, it is automatically switched to system margin.
- Open day-trade credit positions are repaid by market order near the close.

Default behavior
----------------
- New credit before ENTRY_DAYTRADE_CUTOFF_TIME: MarginTradeType=3
  (一般信用デイトレード / day-trade credit)
- New credit at/after ENTRY_DAYTRADE_CUTOFF_TIME: MarginTradeType=1
  (制度信用 / system margin)
- ENTRY_DAYTRADE_CUTOFF_TIME default: 15:00:00 JST.
- New credit CashMargin: 2
- Close credit CashMargin: 3
- Close order: market order, FrontOrderType=10, Price=0
- EOD force close target: DAYTRADE_FORCE_CLOSE_TIME=15:24:30 JST by default.

Disable with:
- DISABLE_DAYTRADE_CREDIT_FORCE_CLOSE_PATCH=1
- DAYTRADE_FORCE_CLOSE_ENABLED=0
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "DAYTRADE-CREDIT-FORCE-CLOSE-V3-CUTOFF-1500-SYSTEM-MARGIN"
_INSTALLED = False
_THREAD_STARTED = False
_FORCE_CLOSE_DONE_DATE: str | None = None
_ORIG_BSE_MAKE_PAYLOAD = None
_ORIG_CLOSE_SEND_CREDIT_CLOSE_ORDER = None
_ORIG_CLOSE_PROCESS_EXIT = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.environ.get(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.environ.get(name)
        if raw is None or str(raw).strip() == "":
            return int(default)
        return int(float(raw))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(raw)
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _parse_hhmmss(value: str, default: str = "15:24:30") -> tuple[int, int, int]:
    text = str(value or default).strip()
    parts = text.split(":")
    h = int(parts[0]) if len(parts) >= 1 else int(default.split(":")[0])
    m = int(parts[1]) if len(parts) >= 2 else int(default.split(":")[1])
    s = int(parts[2]) if len(parts) >= 3 else int(default.split(":")[2])
    return max(0, min(h, 23)), max(0, min(m, 59)), max(0, min(s, 59))


def _time_today(value: str, default: str) -> dt.datetime:
    h, m, s = _parse_hhmmss(value, default)
    now = dt.datetime.now()
    return now.replace(hour=h, minute=m, second=s, microsecond=0)


def _after_or_at_daytrade_cutoff(now: dt.datetime | None = None) -> bool:
    try:
        now = now or dt.datetime.now()
        cutoff_text = os.environ.get("ENTRY_DAYTRADE_CUTOFF_TIME", "15:00:00")
        cutoff = _time_today(cutoff_text, "15:00:00")
        return now >= cutoff
    except Exception:
        # Fail safe: after parse errors, do not force day-trade margin late in the day.
        return True


def _entry_margin_type_for_now(explicit_margin_type: Any = None) -> tuple[int, str]:
    """Return (MarginTradeType, mode) for a new credit entry."""
    daytrade_mt = _env_int("KABU_DAYTRADE_MARGIN_TYPE", 3)
    system_mt = _env_int("KABU_SYSTEM_MARGIN_TYPE", 1)
    if explicit_margin_type not in (None, ""):
        mt = _safe_int(explicit_margin_type, daytrade_mt)
        # Even when a caller explicitly asks day-trade after cutoff, force system margin.
        if _after_or_at_daytrade_cutoff() and mt == daytrade_mt and _env_bool("ENTRY_AFTER_CUTOFF_FORCE_SYSTEM_MARGIN", True):
            return system_mt, "SYSTEM_AFTER_CUTOFF_FORCED"
        return mt, "EXPLICIT"
    if _after_or_at_daytrade_cutoff():
        return system_mt, "SYSTEM_AFTER_CUTOFF"
    return daytrade_mt, "DAYTRADE_BEFORE_CUTOFF"


def _set_daytrade_env_defaults() -> None:
    os.environ.setdefault("KABU_DAYTRADE_MARGIN_TYPE", "3")
    os.environ.setdefault("KABU_SYSTEM_MARGIN_TYPE", "1")
    # 15:00以降の新規エントリーはデイトレ信用ではなく制度信用に切替。
    os.environ.setdefault("ENTRY_DAYTRADE_CUTOFF_TIME", "15:00:00")
    os.environ.setdefault("ENTRY_AFTER_CUTOFF_FORCE_SYSTEM_MARGIN", "1")
    # Keep the legacy envs as day-trade defaults. The payload wrapper will override late entries.
    os.environ.setdefault("ENTRY_ORDER_MARGIN_TRADE_TYPE", os.environ.get("KABU_DAYTRADE_MARGIN_TYPE", "3"))
    os.environ.setdefault("KABU_ORDER_MARGIN_TRADE_TYPE", os.environ.get("KABU_DAYTRADE_MARGIN_TYPE", "3"))
    os.environ.setdefault("DAYTRADE_FORCE_CLOSE_ENABLED", "1")
    os.environ.setdefault("DAYTRADE_FORCE_CLOSE_TIME", "15:24:30")
    os.environ.setdefault("DAYTRADE_FORCE_CLOSE_POLL_SEC", "1.0")
    os.environ.setdefault("DAYTRADE_FORCE_CLOSE_RETRY", "2")
    os.environ.setdefault("DAYTRADE_FORCE_CLOSE_RETRY_SLEEP_SEC", "1.5")
    # EOD market repayment must target day-trade positions only; system-margin positions are not forcibly closed here.
    os.environ.setdefault("DAYTRADE_FORCE_CLOSE_ONLY_MARGIN_TYPE", os.environ.get("KABU_DAYTRADE_MARGIN_TYPE", "3"))
    # Do not force close-order margin type to day-trade when the position/caller has an actual margin type.
    os.environ.setdefault("DAYTRADE_FORCE_CLOSE_FORCE_MARGIN_TYPE", "0")


def _patch_buy_sell_entry_payload() -> bool:
    """Use day-trade margin before cutoff and system margin at/after cutoff."""
    global _ORIG_BSE_MAKE_PAYLOAD
    try:
        import kabu_api.buy_sell_entry as bse
        if getattr(bse, "_daytrade_credit_payload_patched_v3", False):
            return True
        orig = getattr(bse, "_make_payload", None)
        if not callable(orig):
            return False
        _ORIG_BSE_MAKE_PAYLOAD = orig

        def _make_payload_daytrade_cutoff(symbol, side, qty, price, *, exchange=1, margin_type=None, cash_margin=2, front_order_type=20, stop_price=None):
            mt, mode = _entry_margin_type_for_now(margin_type)
            cm = _safe_int(cash_margin, 2)
            payload = orig(
                symbol,
                side,
                qty,
                price,
                exchange=exchange,
                margin_type=mt,
                cash_margin=cm,
                front_order_type=front_order_type,
                stop_price=stop_price,
            )
            try:
                if int(payload.get("CashMargin", cm)) == 2:
                    payload["MarginTradeType"] = mt
                    payload["DelivType"] = 0
                    payload["AccountType"] = _env_int("KABU_ORDER_ACCOUNT_TYPE", _safe_int(payload.get("AccountType"), 4))
            except Exception:
                pass
            logger.warning(
                "[CREDIT ENTRY MARGIN MODE] symbol=%s side=%s cash_margin=%s margin_type=%s mode=%s cutoff=%s front_order_type=%s",
                symbol,
                side,
                payload.get("CashMargin"),
                payload.get("MarginTradeType"),
                mode,
                os.environ.get("ENTRY_DAYTRADE_CUTOFF_TIME", "15:00:00"),
                payload.get("FrontOrderType"),
            )
            return payload

        bse._make_payload = _make_payload_daytrade_cutoff
        bse._daytrade_credit_payload_patched = True
        bse._daytrade_credit_payload_patched_v2 = True
        bse._daytrade_credit_payload_patched_v3 = True
        return True
    except Exception:
        logger.exception("[DAYTRADE CREDIT] buy_sell_entry payload patch failed")
        return False


def _patch_close_defaults() -> bool:
    """Respect the actual position MarginTradeType on repayment orders."""
    global _ORIG_CLOSE_SEND_CREDIT_CLOSE_ORDER, _ORIG_CLOSE_PROCESS_EXIT
    try:
        import kabu_api.close as close_mod
        if getattr(close_mod, "_daytrade_credit_close_patched_v3", False):
            return True

        # If V1/V2 was already installed, bypass its wrapper and call the true original when available.
        base_orig = None
        try:
            from core.startup import daytrade_credit_force_close_patch as self_mod
            base_orig = getattr(self_mod, "_ORIG_CLOSE_SEND_CREDIT_CLOSE_ORDER", None)
        except Exception:
            base_orig = None

        orig_send = base_orig if callable(base_orig) else getattr(close_mod, "send_credit_close_order", None)
        orig_process = getattr(close_mod, "process_exit", None)
        if not callable(orig_send) or not callable(orig_process):
            return False
        _ORIG_CLOSE_SEND_CREDIT_CLOSE_ORDER = orig_send
        _ORIG_CLOSE_PROCESS_EXIT = orig_process

        def send_credit_close_order_respect_margin(symbol, qty, hold_id, side, exchange, margin_type, account_type):
            mt = _safe_int(margin_type, 0)
            if mt <= 0:
                # Fallback only. Existing day-trade positions from this system normally have MarginTradeType=3.
                mt = _env_int("KABU_DAYTRADE_MARGIN_TYPE", 3)
            logger.warning(
                "[CREDIT CLOSE MARGIN MODE] symbol=%s side=%s qty=%s hold_id=%s margin_type=%s order=MARKET",
                symbol,
                side,
                qty,
                hold_id,
                mt,
            )
            return orig_send(symbol, qty, hold_id, side, exchange, mt, account_type)

        def process_exit_respect_margin(position, exit_price: float, reason: str):
            try:
                mt = _safe_int(getattr(position, "margin_trade_type", 0), 0)
                if mt <= 0:
                    setattr(position, "margin_trade_type", _env_int("KABU_DAYTRADE_MARGIN_TYPE", 3))
            except Exception:
                pass
            return orig_process(position, exit_price, reason)

        close_mod.send_credit_close_order = send_credit_close_order_respect_margin
        close_mod.process_exit = process_exit_respect_margin
        close_mod._daytrade_credit_close_patched = True
        close_mod._daytrade_credit_close_patched_v2 = True
        close_mod._daytrade_credit_close_patched_v3 = True
        return True
    except Exception:
        logger.exception("[DAYTRADE CREDIT] close defaults patch failed")
        return False


def _position_side_to_close_side(p: dict[str, Any]) -> int:
    # kabu /positions Side: 1=売建, 2=買建 in the existing sync code.
    side = str(p.get("Side") or "").strip()
    if side == "1":
        return 2  # 売建は買い戻し
    return 1      # 買建は売り返済


def _is_target_position(p: dict[str, Any]) -> bool:
    try:
        mt = _safe_int(p.get("MarginTradeType"), 0)
        if mt <= 0:
            return False
        only = str(os.environ.get("DAYTRADE_FORCE_CLOSE_ONLY_MARGIN_TYPE", "3") or "").strip()
        if only and only != "0" and mt != _safe_int(only, 3):
            return False
        qty = _safe_int(p.get("LeavesQty") or p.get("Qty"), 0)
        if qty <= 0:
            return False
        if not _norm_symbol(p.get("Symbol")):
            return False
        if not (p.get("ExecutionID") or p.get("HoldID")):
            return False
        return True
    except Exception:
        return False


def _force_close_positions_once() -> dict[str, int]:
    """Close all target day-trade credit positions by market repayment order."""
    result = {"seen": 0, "target": 0, "sent": 0, "failed": 0, "skipped": 0}
    try:
        from kabu_api.positions import get_positions
        import kabu_api.close as close_mod

        positions = get_positions() or []
        result["seen"] = len(positions) if isinstance(positions, list) else 0
        if not isinstance(positions, list):
            return result

        for p in positions:
            if not isinstance(p, dict):
                continue
            if not _is_target_position(p):
                result["skipped"] += 1
                continue
            result["target"] += 1
            symbol = _norm_symbol(p.get("Symbol"))
            qty = _safe_int(p.get("LeavesQty") or p.get("Qty"), 0)
            hold_id = p.get("ExecutionID") or p.get("HoldID")
            side = _position_side_to_close_side(p)
            exchange = _safe_int(p.get("Exchange"), _env_int("ENTRY_ORDER_EXCHANGE", 9)) or _env_int("ENTRY_ORDER_EXCHANGE", 9)
            margin_type = _safe_int(p.get("MarginTradeType"), _env_int("KABU_DAYTRADE_MARGIN_TYPE", 3)) or _env_int("KABU_DAYTRADE_MARGIN_TYPE", 3)
            account_type = _safe_int(p.get("AccountType"), 4) or 4
            retries = max(1, _env_int("DAYTRADE_FORCE_CLOSE_RETRY", 2))
            ok = False
            for attempt in range(1, retries + 1):
                logger.warning(
                    "[DAYTRADE FORCE CLOSE] send symbol=%s qty=%s hold_id=%s close_side=%s margin_type=%s attempt=%s/%s",
                    symbol,
                    qty,
                    hold_id,
                    side,
                    margin_type,
                    attempt,
                    retries,
                )
                res = close_mod.send_credit_close_order(symbol, qty, hold_id, side, exchange, margin_type, account_type)
                if res and res.get("order_id"):
                    ok = True
                    result["sent"] += 1
                    break
                time.sleep(max(0.1, _env_float("DAYTRADE_FORCE_CLOSE_RETRY_SLEEP_SEC", 1.5)))
            if not ok:
                result["failed"] += 1
                logger.error("[DAYTRADE FORCE CLOSE] failed symbol=%s qty=%s hold_id=%s", symbol, qty, hold_id)
        return result
    except Exception:
        logger.exception("[DAYTRADE FORCE CLOSE] unexpected failure")
        return result


def _force_close_loop() -> None:
    global _FORCE_CLOSE_DONE_DATE
    h, m, s = _parse_hhmmss(os.environ.get("DAYTRADE_FORCE_CLOSE_TIME", "15:24:30"), "15:24:30")
    poll = max(0.5, _env_float("DAYTRADE_FORCE_CLOSE_POLL_SEC", 1.0))
    logger.warning("[DAYTRADE FORCE CLOSE] loop started target_time=%02d:%02d:%02d poll=%.1fs only_margin_type=%s", h, m, s, poll, os.environ.get("DAYTRADE_FORCE_CLOSE_ONLY_MARGIN_TYPE", "3"))
    while True:
        try:
            if not _env_bool("DAYTRADE_FORCE_CLOSE_ENABLED", True):
                time.sleep(30.0)
                continue
            now = dt.datetime.now()
            today = now.strftime("%Y%m%d")
            target = now.replace(hour=h, minute=m, second=s, microsecond=0)
            if now >= target and _FORCE_CLOSE_DONE_DATE != today:
                _FORCE_CLOSE_DONE_DATE = today
                logger.warning("[DAYTRADE FORCE CLOSE] trigger now=%s target=%s", now, target)
                result = _force_close_positions_once()
                logger.warning("[DAYTRADE FORCE CLOSE] done result=%s", result)
            time.sleep(poll)
        except Exception:
            logger.exception("[DAYTRADE FORCE CLOSE] loop error")
            time.sleep(5.0)


def _start_force_close_thread() -> bool:
    global _THREAD_STARTED
    try:
        if _THREAD_STARTED:
            return True
        if not _env_bool("DAYTRADE_FORCE_CLOSE_ENABLED", True):
            logger.warning("[DAYTRADE FORCE CLOSE] disabled by env")
            return False
        th = threading.Thread(target=_force_close_loop, name="daytrade-force-close", daemon=True)
        th.start()
        _THREAD_STARTED = True
        return True
    except Exception:
        logger.exception("[DAYTRADE FORCE CLOSE] thread start failed")
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if os.environ.get("DISABLE_DAYTRADE_CREDIT_FORCE_CLOSE_PATCH", "").strip() == "1":
        logger.warning("[DAYTRADE CREDIT] disabled by env")
        return False
    _set_daytrade_env_defaults()
    payload_ok = _patch_buy_sell_entry_payload()
    close_ok = _patch_close_defaults()
    thread_ok = _start_force_close_thread()
    _INSTALLED = bool(payload_ok or close_ok or thread_ok)
    logger.warning(
        "[DAYTRADE CREDIT] installed version=%s payload=%s close=%s force_close_thread=%s daytrade_margin_type=%s system_margin_type=%s cutoff=%s close_time=%s only_margin_type=%s",
        VERSION,
        payload_ok,
        close_ok,
        thread_ok,
        os.environ.get("KABU_DAYTRADE_MARGIN_TYPE"),
        os.environ.get("KABU_SYSTEM_MARGIN_TYPE"),
        os.environ.get("ENTRY_DAYTRADE_CUTOFF_TIME"),
        os.environ.get("DAYTRADE_FORCE_CLOSE_TIME"),
        os.environ.get("DAYTRADE_FORCE_CLOSE_ONLY_MARGIN_TYPE"),
    )
    return _INSTALLED


__all__ = ["VERSION", "install", "_force_close_positions_once"]
