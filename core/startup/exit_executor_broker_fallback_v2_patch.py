# ============================================================
# File   : core/startup/exit_executor_broker_fallback_v2_patch.py
# Version: V2.0-EXIT-EXECUTOR-BROKER-FALLBACK-SOFT-DB
# ------------------------------------------------------------
# Purpose:
#   EXIT判定後、最終返済発注で止まる問題を防ぐ。
#
# Fixes:
#   1) execute_exit() の保有取得で global_data/GC に無い場合、
#      kabu Station 信用建玉を直接再読込して使う。
#   2) 注入 sender が失敗/拒否した場合、builtin kabu close order へfallbackする。
#   3) send_order_common の戻り値が Result=0 / ResultCode=0 / Code=0 の場合も成功扱いする。
#   4) 返済注文が受理済みなのに positions.db にOPEN行が無い場合、
#      order accepted を優先して DB close failed 扱いで止めない。
#
# Safety:
#   - 現物は対象にせず、kabu_position_reader の信用建玉だけをfallback対象にする。
#   - 既存 execute_exit 本体は差し替えず、内部関数だけpatchする。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _normalize_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _normalize_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "LONG", "2", "02", "20", "B", "信用買", "買", "買建", "BUY_CREDIT"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "01", "10", "S", "信用売", "売", "売建", "SELL_CREDIT"}:
        return "SELL"
    return s


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _broker_position_to_executor_dict(symbol: str, raw: dict[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    out["symbol"] = _normalize_symbol(out.get("symbol") or out.get("Symbol") or symbol)
    out["side"] = _normalize_side(out.get("side") or out.get("Side") or out.get("SellBuy"))
    out["status"] = "OPEN"
    out["_position_source"] = "KABU.positions.credit_only.executor_direct_fallback"

    qty = (
        out.get("qty")
        or out.get("quantity")
        or out.get("LeavesQty")
        or out.get("HoldQty")
        or out.get("Qty")
    )
    if qty not in (None, ""):
        out["qty"] = qty
        out["quantity"] = qty

    entry = (
        out.get("avg_price")
        or out.get("entry_price")
        or out.get("AveragePrice")
        or out.get("AvgPrice")
        or out.get("ExecutionPrice")
        or out.get("Price")
        or out.get("price")
    )
    if entry not in (None, ""):
        out["avg_price"] = entry
        out["entry_price"] = entry

    out.setdefault("exchange", out.get("Exchange") or 1)
    out.setdefault("margin_trade_type", out.get("MarginTradeType") or out.get("margin_trade_type") or 1)
    out.setdefault("account_type", out.get("AccountType") or out.get("account_type") or 4)
    out.setdefault("hold_id", out.get("HoldID") or out.get("hold_id") or out.get("execution_id") or out.get("ExecutionID") or "")
    out.setdefault("entry_time", out.get("entry_time") or dt.datetime.now())
    return out


def _read_broker_position(symbol: str) -> Optional[dict[str, Any]]:
    if not _env_bool("EXIT_EXECUTOR_BROKER_DIRECT_FALLBACK", True):
        return None
    symbol = _normalize_symbol(symbol)
    if not symbol:
        return None
    try:
        from trading.position.kabu_position_reader import read_kabu_open_positions
        rows = read_kabu_open_positions() or {}
        if not isinstance(rows, dict):
            return None
        raw = rows.get(symbol) or rows.get(str(symbol))
        if raw is None:
            for k, v in rows.items():
                if _normalize_symbol(k) == symbol:
                    raw = v
                    break
        if not isinstance(raw, dict):
            return None
        pos = _broker_position_to_executor_dict(symbol, raw)
        if pos.get("side") not in {"BUY", "SELL"} or _safe_float(pos.get("qty"), 0.0) <= 0:
            logger.warning("[EXIT EXECUTOR BROKER V2] broker fallback invalid symbol=%s pos=%s", symbol, pos)
            return None
        try:
            from global_state import global_data
            gd = getattr(global_data, "open_positions", None)
            if not isinstance(gd, dict):
                gd = {}
                setattr(global_data, "open_positions", gd)
            gd[symbol] = pos
        except Exception:
            pass
        logger.warning("[EXIT EXECUTOR BROKER V2] recovered broker credit position symbol=%s side=%s qty=%s avg=%s hold_id=%s", symbol, pos.get("side"), pos.get("qty"), pos.get("avg_price"), pos.get("hold_id"))
        return pos
    except Exception:
        logger.exception("[EXIT EXECUTOR BROKER V2] broker fallback read failed symbol=%s", symbol)
        return None


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.exit.executor as ex

        old_get_open_position = getattr(ex, "_get_open_position", None)
        old_send_close_order = getattr(ex, "_send_close_order", None)
        old_parse_order_result = getattr(ex, "_parse_order_result", None)
        old_close_db_position = getattr(ex, "_close_db_position", None)

        if getattr(old_get_open_position, "_exit_executor_broker_v2", False):
            _INSTALLED = True
            return True

        def _patched_parse_order_result(ret: Any):
            try:
                if isinstance(ret, dict):
                    order_id = str(ret.get("order_id") or ret.get("orderId") or ret.get("OrderId") or ret.get("ID") or ret.get("id") or "")
                    result_like = ret.get("Result", ret.get("result", ret.get("ResultCode", ret.get("Code", ret.get("code", None)))))
                    ok_flag = ret.get("ok") or ret.get("success") or ret.get("accepted")
                    if order_id or ok_flag is True or str(result_like).strip() in {"0", "0.0"}:
                        msg = str(ret.get("message") or ret.get("msg") or ret.get("Message") or ret)
                        return True, order_id, msg
            except Exception:
                pass
            if callable(old_parse_order_result):
                return old_parse_order_result(ret)
            return False, "", str(ret)

        def _patched_get_open_position(symbol: str):
            if callable(old_get_open_position):
                try:
                    pos, source = old_get_open_position(symbol)
                    if pos is not None:
                        return pos, source
                except Exception:
                    logger.exception("[EXIT EXECUTOR BROKER V2] original _get_open_position failed symbol=%s", symbol)
            pos = _read_broker_position(symbol)
            if pos is not None:
                return pos, "broker_credit_direct_fallback_v2"
            return None, ""

        def _patched_send_close_order(*, symbol: str, side: str, close_side: str, qty: float, exit_price: float, reason: str, position: dict[str, Any]):
            first_ok = False
            first_order_id = ""
            first_message = ""
            if callable(old_send_close_order):
                try:
                    first_ok, first_order_id, first_message = old_send_close_order(
                        symbol=symbol,
                        side=side,
                        close_side=close_side,
                        qty=qty,
                        exit_price=exit_price,
                        reason=reason,
                        position=position,
                    )
                    if first_ok:
                        return first_ok, first_order_id, first_message
                except Exception as e:
                    first_message = str(e)
                    logger.exception("[EXIT EXECUTOR BROKER V2] original sender failed symbol=%s", symbol)

            if not _env_bool("EXIT_FALLBACK_BUILTIN_ON_SENDER_FAIL", True):
                return first_ok, first_order_id, first_message

            builtin = getattr(ex, "_send_kabu_close_order_builtin", None)
            if not callable(builtin):
                return first_ok, first_order_id, first_message

            logger.warning(
                "[EXIT EXECUTOR BROKER V2] sender failed/rejected -> builtin fallback symbol=%s side=%s close_side=%s qty=%s first_message=%s",
                symbol, side, close_side, qty, first_message,
            )
            try:
                return builtin(
                    symbol=symbol,
                    side=side,
                    close_side=close_side,
                    qty=qty,
                    exit_price=exit_price,
                    reason=reason,
                    position=position,
                )
            except Exception as e:
                logger.exception("[EXIT EXECUTOR BROKER V2] builtin fallback failed symbol=%s", symbol)
                return False, first_order_id, f"sender_failed={first_message}; builtin_failed={e}"

        def _patched_close_db_position(*, symbol: str, exit_price: float, pnl: float, reason: str, order_id: str) -> bool:
            if callable(old_close_db_position):
                try:
                    ok = bool(old_close_db_position(symbol=symbol, exit_price=exit_price, pnl=pnl, reason=reason, order_id=order_id))
                    if ok:
                        return True
                except Exception:
                    logger.exception("[EXIT EXECUTOR BROKER V2] original DB close failed symbol=%s", symbol)
            if _env_bool("EXIT_SOFT_SUCCESS_IF_DB_POSITION_MISSING", True):
                logger.warning(
                    "[EXIT EXECUTOR BROKER V2] DB close missing/failed but order path already accepted -> soft success symbol=%s order_id=%s reason=%s",
                    symbol, order_id, reason,
                )
                return True
            return False

        _patched_parse_order_result._exit_executor_broker_v2 = True  # type: ignore[attr-defined]
        _patched_get_open_position._exit_executor_broker_v2 = True  # type: ignore[attr-defined]
        _patched_send_close_order._exit_executor_broker_v2 = True  # type: ignore[attr-defined]
        _patched_close_db_position._exit_executor_broker_v2 = True  # type: ignore[attr-defined]

        ex._parse_order_result = _patched_parse_order_result
        ex._get_open_position = _patched_get_open_position
        ex._send_close_order = _patched_send_close_order
        ex._close_db_position = _patched_close_db_position

        os.environ.setdefault("EXIT_EXECUTOR_BROKER_DIRECT_FALLBACK", "1")
        os.environ.setdefault("EXIT_FALLBACK_BUILTIN_ON_SENDER_FAIL", "1")
        os.environ.setdefault("EXIT_SOFT_SUCCESS_IF_DB_POSITION_MISSING", "1")

        _INSTALLED = True
        logger.warning(
            "[EXIT EXECUTOR BROKER V2] installed broker_direct=%s builtin_fallback=%s soft_db_missing=%s",
            os.environ.get("EXIT_EXECUTOR_BROKER_DIRECT_FALLBACK"),
            os.environ.get("EXIT_FALLBACK_BUILTIN_ON_SENDER_FAIL"),
            os.environ.get("EXIT_SOFT_SUCCESS_IF_DB_POSITION_MISSING"),
        )
        return True
    except Exception:
        logger.exception("[EXIT EXECUTOR BROKER V2] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[EXIT EXECUTOR BROKER V2] auto install failed")


__all__ = ["install"]
