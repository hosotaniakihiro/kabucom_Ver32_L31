# ============================================================
# File   : trading/push/push_stream/ws_callbacks.py
# Version: Ver1.7-PUSH-STREAM-WS-CALLBACKS-HONOR-SKIP-AFTER-OPEN-REFRESH
# ------------------------------------------------------------
# PUSH WebSocket callback。
#
# 重要修正:
#   - WebSocket接続後に必ず subscription refresh thread を起動する
#   - rotation_enabled=False の memory-only mode でも登録銘柄を送信する
#   - 保有銘柄が protected に入っても、実際のPUSH登録が走らない問題を修正
#   - websocket-client の sock=None 競合を接続断扱いにして再接続へ寄せる
#   - V1.6: rotation中の kabu Station 10054 は想定内切断として扱い、
#           「既存の接続はリモート ホストに強制的に切断されました。 - goodbye」
#           の表示を抑制する。
#   - V1.7: PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH=1 のとき、on_open側でも
#           refresh after open thread を起動せず、started の誤ログを出さない。
# ============================================================

from __future__ import annotations

import logging
import os
import time
from typing import Any

import websocket

from . import state
from .runtime import _now, _safe_iso, _safe_set_runtime
from .transport import _clear_sender, _install_sender, _start_refresh_after_open_thread
from .normalize import _parse_message, _normalize_push_row
from .dataframe import _append_df
from .writers import _queue_put

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _is_expected_rotation_disconnect() -> bool:
    """Return True when a 10054 is expected because we intentionally rotate registrations."""
    try:
        if bool(getattr(state, "_rotation_register_in_progress", False)):
            return True
    except Exception:
        pass
    try:
        last = getattr(state, "_last_expected_ws_close_at", 0.0) or 0.0
        if last and (time.monotonic() - float(last)) <= 8.0:
            return True
    except Exception:
        pass
    return False


class _Expected10054Filter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if not _env_bool("PUSH_WS_SUPPRESS_EXPECTED_10054_TEXT", True):
                return True
            msg = record.getMessage()
            if not isinstance(msg, str):
                return True
            text = msg.lower()
            # websocket-client may emit this independently of our callback as:
            # "[WinError 10054] ... - goodbye". It is noise during A/B rotation;
            # our own monitor/reconnect logs still show the real state.
            if "10054" in text and ("goodbye" in text or "既存の接続" in msg or "reset by peer" in text):
                return False
        except Exception:
            return True
        return True


def _install_expected_10054_filter() -> None:
    try:
        if not _env_bool("PUSH_WS_SUPPRESS_EXPECTED_10054_TEXT", True):
            return
        flt = _Expected10054Filter()
        for name in ("websocket", "websocket._app", "websocket._core", "websocket._logging"):
            lg = logging.getLogger(name)
            if not any(isinstance(f, _Expected10054Filter) for f in getattr(lg, "filters", [])):
                lg.addFilter(flt)
        root = logging.getLogger()
        for h in list(getattr(root, "handlers", []) or []):
            if not any(isinstance(f, _Expected10054Filter) for f in getattr(h, "filters", [])):
                h.addFilter(flt)
    except Exception:
        logger.debug("[push_stream] expected 10054 filter install failed", exc_info=True)


_install_expected_10054_filter()


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


def _mark_disconnected(reason: str) -> None:
    """on_close を経由しない異常系でも runtime 状態を接続断へ戻す。"""
    try:
        state._last_disconnect_at = _now()
        state._connected_event.clear()
        _clear_sender()
        _safe_set_runtime("ws_connected", False)
    except Exception:
        logger.exception("[push_stream] failed to mark disconnected reason=%s", reason)


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

    # websocket-client 内部で、切断済み WebSocketApp の self.sock が None のまま
    # dispatcher.read(self.sock.sock, ...) に進むことがある。
    # これはアプリ側では接続断として扱い、ERROR traceback を連発させない。
    if "NoneType" in msg and "sock" in msg:
        logger.warning("[push_stream] ws socket already closed; mark disconnected: %s", msg)
        _mark_disconnected("sock_none")
        return

    if "10054" in msg or isinstance(error, ConnectionResetError):
        if _is_expected_rotation_disconnect():
            logger.info("[push_stream] expected vendor ws reset during rotation; reconnecting")
            _mark_disconnected("expected_rotation_reset")
            return
        logger.warning("[push_stream] ws reset by peer: %s", msg)
        _mark_disconnected("reset_by_peer")
        return

    if "ping/pong timed out" in msg:
        logger.warning("[push_stream] ws ping timeout: %s", msg)
        _mark_disconnected("ping_timeout")
        return

    logger.error("--- ERROR ---")
    logger.error("%s", error, exc_info=True if isinstance(error, BaseException) else False)


def on_close(ws: websocket.WebSocketApp, close_status_code=None, close_msg=None) -> None:
    state._last_disconnect_at = _now()
    if _is_expected_rotation_disconnect():
        logger.info("--- DISCONNECTED expected_rotation --- code=%s msg=%s", close_status_code, close_msg)
    else:
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
    # その場合、rotation worker は起動しないため、on_open refresh が必要になる。
    # ただし main_database.py / push_receiver のA/Bローテーション運用では
    # PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH=1 を尊重し、余計な再登録 thread を起動しない。
    try:
        if _env_bool("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH", False):
            logger.warning("[push_stream] refresh after open thread skipped by ws_callbacks env PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH=1")
            return
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
