# ============================================================
# File   : trading/push/push_stream/ws_callbacks.py
# Version: Ver1.4-PUSH-STREAM-WS-CALLBACKS-REFRESH-AFTER-OPEN-FIX
# ------------------------------------------------------------
# PUSH WebSocket callback。
#
# 重要修正:
#   - WebSocket接続後に必ず subscription refresh thread を起動する
#   - rotation_enabled=False の memory-only mode でも登録銘柄を送信する
#   - 保有銘柄が protected に入っても、実際のPUSH登録が走らない問題を修正
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import websocket

from . import state
from .runtime import _now, _safe_iso, _safe_set_runtime
from .transport import _clear_sender, _install_sender, _start_refresh_after_open_thread
from .normalize import _parse_message, _normalize_push_row
from .dataframe import _append_df
from .writers import _queue_put

logger = logging.getLogger(__name__)


def _safe_payload_head(payload: Any) -> str:
    try:
        if isinstance(payload, dict):
            keys = sorted(list(payload.keys()))[:20]
            return f"dict keys={keys}"
        return f"type={type(payload).__name__}"
    except Exception:
        return "unknown"


def _safe_row_head(row: Any) -> str:
    try:
        if isinstance(row, dict):
            return (
                f"symbol={row.get('symbol')} "
                f"dt={row.get('datetime')} "
                f"price={row.get('current_price') or row.get('price') or row.get('close')}"
            )
        return f"type={type(row).__name__}"
    except Exception:
        return "unknown"


def _update_latest_price_cache_safe(row: dict) -> None:
    try:
        if not isinstance(row, dict):
            return
        from trading.push.latest_price_cache import update_latest_price_from_push
        update_latest_price_from_push(row, source="push_stream")
    except Exception:
        logger.exception("[PUSH PRICE CACHE] update from push_stream row failed row=%s", _safe_row_head(row))


def _update_5sec_bar_safe(row: dict) -> None:
    try:
        if not isinstance(row, dict):
            return
        symbol = row.get("symbol") or row.get("Symbol") or row.get("code")
        if not symbol:
            return
        from trading.monitor.five_sec_bar_builder import update_five_sec_bar_from_tick
        update_five_sec_bar_from_tick(symbol=str(symbol), tick=row)
    except Exception:
        logger.exception("[5SEC BAR] update from push_stream row failed row=%s", _safe_row_head(row))


def on_message(ws: websocket.WebSocketApp, message: Any) -> None:
    raw_len = 0
    try:
        raw_len = len(message) if message is not None else 0
    except Exception:
        raw_len = 0

    try:
        state._last_message_at = _now()
        _safe_set_runtime("last_push_received_at", _safe_iso(state._last_message_at))
        state._total_received += 1

        payload = _parse_message(message)
        row = _normalize_push_row(payload)
        if not row:
            state._total_dropped += 1
            logger.warning("[push_stream] normalize returned empty payload=%s", _safe_payload_head(payload))
            return

        _update_latest_price_cache_safe(row)
        _update_5sec_bar_safe(row)

        try:
            if state._ring_buffer is not None:
                if hasattr(state._ring_buffer, "append"):
                    state._ring_buffer.append(row)
                elif hasattr(state._ring_buffer, "add"):
                    state._ring_buffer.add(row)
        except Exception:
            logger.exception("[push_stream] ring buffer append failed")

        try:
            _append_df(row)
        except Exception:
            logger.exception("[push_stream] dataframe append failed row=%s", _safe_row_head(row))

        try:
            _queue_put(row)
        except Exception:
            logger.exception("[push_stream] queue put failed row=%s", _safe_row_head(row))
            state._total_errors += 1

    except Exception:
        state._total_errors += 1
        logger.exception("[push_stream] on_message failed bytes=%d", raw_len)


def on_error(ws, error):
    state._last_error_at = _now()
    state._total_errors += 1
    msg = str(error)
    if "10054" in msg or isinstance(error, ConnectionResetError):
        logger.warning("[push_stream] ws reset by peer: %s", msg)
        return
    if "ping/pong timed out" in msg:
        logger.warning("[push_stream] ws ping timeout: %s", msg)
        return
    logger.error("--- ERROR ---")
    logger.error("%s", error, exc_info=True if isinstance(error, BaseException) else False)


def on_close(ws: websocket.WebSocketApp, close_status_code=None, close_msg=None) -> None:
    state._last_disconnect_at = _now()
    logger.warning("--- DISCONNECTED --- code=%s msg=%s", close_status_code, close_msg)
    state._connected_event.clear()
    _clear_sender()
    _safe_set_runtime("ws_connected", False)


def on_open(ws: websocket.WebSocketApp) -> None:
    state._last_connect_at = _now()
    logger.info("--- CONNECTED ---")
    state._connected_event.set()
    _install_sender(ws)
    _safe_set_runtime("ws_connected", True)

    # 重要:
    # main.py memory-only mode では rotation_enabled=False のことがある。
    # その場合、rotation worker は起動しないため、on_open で refresh を起動しないと
    # protected銘柄を含む登録対象がkabuステーションへ送信されない。
    try:
        _start_refresh_after_open_thread()
        logger.warning("[push_stream] refresh after open thread started")
    except Exception:
        logger.exception("[push_stream] failed to start refresh after open thread")


__all__ = [
    "on_message",
    "on_error",
    "on_close",
    "on_open",
]
