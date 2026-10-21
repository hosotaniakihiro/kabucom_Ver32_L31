# ============================================================
# File   : trading/push/push_stream/ws_callbacks.py
# Version: Ver1.9-PUSH-STREAM-FORCE-REFRESH-AFTER-WS-STALE-RECOVER
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
#   - V1.8: transport._start_refresh_after_open_thread() の戻り値を見て、
#           実際に起動した時だけ started を出す。
#   - V1.9: WS stale recover 後だけは PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH=1 でも
#           購読復旧 refresh を1回実行する。通常のA/Bローテーションではskipを尊重する。
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

import websocket

from . import state
from .runtime import _now, _safe_iso, _safe_set_runtime, _safe_get_runtime
from .transport import _clear_sender, _install_sender, _start_refresh_after_open_thread, _call_refresh, _wait_for_ws_ready
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


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


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


# 接続世代。on_open のたびに増える。
# websocket-client は切断済み WebSocketApp のコールバックを遅れて呼ぶことがあり、
# その on_close / on_error が「今つながっている別の socket」を
# 切断扱いにしてしまうと、正常受信中でも DISCONNECTED になる。
_CONN_GEN = 0
_CONN_WS: Any = None
_CONN_LOCK = threading.Lock()


def _conn_open(ws: Any) -> int:
    """新しい接続を現行として登録し、その世代番号を返す。"""
    global _CONN_GEN, _CONN_WS
    with _CONN_LOCK:
        _CONN_GEN += 1
        _CONN_WS = ws
        return _CONN_GEN


def current_connection_generation() -> int:
    with _CONN_LOCK:
        return _CONN_GEN


def _is_current_ws(ws: Any) -> bool:
    """この ws が現行の接続か。判定材料が無ければ従来どおり扱う。"""
    if ws is None:
        return True
    with _CONN_LOCK:
        cur = _CONN_WS
    return cur is None or cur is ws


def _ws_alive_safe() -> Any:
    try:
        from .transport import _is_ws_alive

        return bool(_is_ws_alive())
    except Exception:
        return None


def _mark_disconnected(reason: str, ws: Any = None) -> None:
    """on_close を経由しない異常系でも runtime 状態を接続断へ戻す。

    古い socket からの通知では現行接続の状態を触らない。
    """
    if ws is not None and not _is_current_ws(ws):
        logger.warning(
            "[push_stream] stale socket error ignored reason=%s current_generation=%s",
            reason,
            current_connection_generation(),
        )
        return
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


def _update_gapup_breakout_safe(row: dict) -> None:
    """ギャップアップ＋初動5分足高値ブレイク戦略へ tick を渡す。
    既定はドライラン（検知→ログ/Discord通知のみ）。PUSH 本流は絶対に止めない。"""
    try:
        if not isinstance(row, dict):
            return
        from trading.entry import gapup_breakout
        if not gapup_breakout.enabled():
            return
        gapup_breakout.on_tick(row)
    except Exception:
        logger.exception("[GAPUP] update from push_stream row failed row=%s", _safe_row_head(row))


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

        try:
            from . import start_trace

            start_trace.trace_once(
                "conn:%s:first_message" % current_connection_generation(),
                12,
                bytes=raw_len,
                received_total=state._total_received,
            )
        except Exception:
            pass

        payload = _parse_message(message)
        row = _normalize_push_row(payload)
        if not row:
            state._total_dropped += 1
            logger.warning("[push_stream] normalize returned empty payload=%s", _safe_payload_head(payload))
            return
        _update_latest_price_cache_safe(row)
        _update_5sec_bar_safe(row)
        _update_gapup_breakout_safe(row)

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
        _mark_disconnected("sock_none", ws)
        return

    if "10054" in msg or isinstance(error, ConnectionResetError):
        if _is_expected_rotation_disconnect():
            logger.info("[push_stream] expected vendor ws reset during rotation; reconnecting")
            _mark_disconnected("expected_rotation_reset", ws)
            return
        logger.warning("[push_stream] ws reset by peer: %s", msg)
        _mark_disconnected("reset_by_peer", ws)
        return

    if "ping/pong timed out" in msg:
        logger.warning("[push_stream] ws ping timeout: %s", msg)
        _mark_disconnected("ping_timeout", ws)
        return

    logger.error("--- ERROR ---")
    logger.error("%s", error, exc_info=True if isinstance(error, BaseException) else False)


def on_close(ws: websocket.WebSocketApp, close_status_code=None, close_msg=None) -> None:
    if not _is_current_ws(ws):
        # 古い socket の遅れた on_close。現行接続を切断扱いにしない。
        logger.warning(
            "--- DISCONNECTED (stale socket, ignored) --- code=%s msg=%s current_generation=%s",
            close_status_code,
            close_msg,
            current_connection_generation(),
        )
        return

    state._last_disconnect_at = _now()
    if _is_expected_rotation_disconnect():
        logger.info("--- DISCONNECTED expected_rotation --- code=%s msg=%s", close_status_code, close_msg)
    else:
        logger.warning("--- DISCONNECTED --- code=%s msg=%s", close_status_code, close_msg)
    state._connected_event.clear()
    _clear_sender()
    _safe_set_runtime("ws_connected", False)


# stale recover refresh の多重起動防止。
# on_open は再接続のたびに呼ばれるため、ここを守らないと
# 1 回の stale 検出で何十本もの unregister/register が並行してしまう。
_STALE_RECOVER_LOCK = threading.Lock()
_STALE_RECOVER_ACTIVE = False
_STALE_RECOVER_GEN = 0


def _last_recv_age_sec():
    """最後に PUSH を受信してからの経過秒。未受信なら None。"""
    try:
        last = getattr(state, "_last_message_at", None)
        if last is None:
            return None
        return max(0.0, (_now() - last).total_seconds())
    except Exception:
        return None


def _stale_recover_fresh_limit_sec() -> float:
    """この秒数より新しい受信があれば「stale ではない」とみなす。"""
    return max(
        1.0,
        _env_float(
            "PUSH_STREAM_STALE_RECOVER_FRESH_SEC",
            _env_float("PUSH_STREAM_WS_STALE_SEC", 30.0),
        ),
    )


def _force_refresh_after_stale_recover_thread(generation: int = 0, conn_gen: int = 0) -> None:
    global _STALE_RECOVER_ACTIVE
    try:
        delay = max(0.2, _env_float("PUSH_STREAM_STALE_RECOVER_REFRESH_DELAY_SEC", 0.7))
        time.sleep(delay)

        if generation != _STALE_RECOVER_GEN:
            logger.warning(
                "[push_stream] stale recover refresh aborted: newer generation gen=%s current=%s",
                generation,
                _STALE_RECOVER_GEN,
            )
            return

        if not _wait_for_ws_ready(timeout=max(3.0, _env_float("PUSH_STREAM_STALE_RECOVER_WS_READY_TIMEOUT_SEC", 10.0))):
            logger.warning("[push_stream] stale recover refresh skipped: ws not ready")
            return

        # refresh 直前に接続世代が変わっていないか見る。
        # 変わっていれば、この thread の stale 判定はもう別の socket のもの。
        cur_conn = current_connection_generation()
        if conn_gen and cur_conn != conn_gen:
            logger.warning(
                "[push_stream] stale recover refresh aborted: connection changed "
                "conn_gen=%s current=%s gen=%s",
                conn_gen,
                cur_conn,
                generation,
            )
            return

        # 受信が回復しているなら refresh は不要。
        # 古い stale 判定のまま unregister/register をやり直すと、
        # 正常受信中の購読を落として再接続ループを誘発する。
        age = _last_recv_age_sec()
        limit = _stale_recover_fresh_limit_sec()
        if age is not None and age < limit:
            logger.warning(
                "[push_stream] stale recover refresh skipped: push is fresh again "
                "last_recv_age=%.1fs limit=%.1fs gen=%s",
                age,
                limit,
                generation,
            )
            return

        if generation != _STALE_RECOVER_GEN:
            logger.warning(
                "[push_stream] stale recover refresh aborted before call: gen=%s current=%s",
                generation,
                _STALE_RECOVER_GEN,
            )
            return

        logger.warning(
            "[push_stream] stale recover refresh start despite PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH=1 "
            "connection_generation=%s connected=%s ws_alive=%s last_recv_age=%s limit=%.1fs gen=%s",
            cur_conn,
            bool(state._connected_event.is_set()),
            _ws_alive_safe(),
            None if age is None else round(age, 1),
            limit,
            generation,
        )
        _call_refresh(
            force=True,
            reason="stale_recover_on_open",
            clear_first=True,
            unregister_first=True,
            wait_after_clear=0.5,
        )
    except Exception:
        logger.exception("[push_stream] stale recover refresh failed")
    finally:
        with _STALE_RECOVER_LOCK:
            _STALE_RECOVER_ACTIVE = False
        _safe_set_runtime("push_ws_stale_recovering", False)


def _start_stale_recover_refresh_if_needed() -> bool:
    global _STALE_RECOVER_ACTIVE, _STALE_RECOVER_GEN
    if not _env_bool("PUSH_STREAM_FORCE_REFRESH_AFTER_STALE_RECOVER", True):
        return False
    try:
        stale_recovering = bool(_safe_get_runtime("push_ws_stale_recovering", False))
        stale_reason = _safe_get_runtime("push_ws_stale_recover_reason", "")
        if not stale_recovering:
            return False

        # 受信が既に回復しているなら refresh thread 自体を起動しない。
        age = _last_recv_age_sec()
        limit = _stale_recover_fresh_limit_sec()
        if age is not None and age < limit:
            logger.warning(
                "[push_stream] stale recover refresh not started: push is fresh "
                "last_recv_age=%.1fs limit=%.1fs reason=%s",
                age,
                limit,
                stale_reason,
            )
            _safe_set_runtime("push_ws_stale_recovering", False)
            return False

        with _STALE_RECOVER_LOCK:
            if _STALE_RECOVER_ACTIVE:
                logger.warning(
                    "[push_stream] stale recover refresh already running -> skip duplicate reason=%s gen=%s",
                    stale_reason,
                    _STALE_RECOVER_GEN,
                )
                return True
            _STALE_RECOVER_ACTIVE = True
            _STALE_RECOVER_GEN += 1
            gen = _STALE_RECOVER_GEN

        try:
            threading.Thread(
                target=_force_refresh_after_stale_recover_thread,
                args=(gen, current_connection_generation()),
                name="push-refresh-after-stale-recover",
                daemon=True,
            ).start()
        except Exception:
            with _STALE_RECOVER_LOCK:
                _STALE_RECOVER_ACTIVE = False
            raise

        logger.warning(
            "[push_stream] stale recover refresh thread started reason=%s gen=%s "
            "last_recv_age=%s skip_after_open_env=%s",
            stale_reason,
            gen,
            None if age is None else round(age, 1),
            os.getenv("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH"),
        )
        return True
    except Exception:
        logger.exception("[push_stream] stale recover refresh thread start failed")
        return False


def on_open(ws: websocket.WebSocketApp) -> None:
    conn_gen = _conn_open(ws)
    state._last_connect_at = _now()
    logger.info("--- CONNECTED --- connection_generation=%s", conn_gen)
    state._connected_event.set()
    _install_sender(ws)
    _safe_set_runtime("ws_connected", True)
    try:
        from . import start_trace

        start_trace.trace_once("conn:%s:on_open" % conn_gen, 9, connection_generation=conn_gen)
    except Exception:
        logger.debug("[push_stream] start trace 09 failed", exc_info=True)

    # 重要:
    # main.py memory-only mode では rotation_enabled=False のことがある。
    # その場合、rotation worker は起動しないため、on_open refresh が必要になる。
    # ただし main_database.py / push_receiver のA/Bローテーション運用では
    # PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH=1 を尊重し、余計な再登録 thread を起動しない。
    # 例外として、WS stale watchdog が強制closeした直後の再接続では購読が失われやすいため、
    # 1回だけ stale_recover_on_open refresh を実行する。
    try:
        if _env_bool("PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH", False):
            if _start_stale_recover_refresh_if_needed():
                return
            logger.warning("[push_stream] refresh after open thread skipped by ws_callbacks env PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH=1")
            return
        started = bool(_start_refresh_after_open_thread())
        if started:
            logger.warning("[push_stream] refresh after open thread started")
        else:
            logger.warning("[push_stream] refresh after open thread not started by transport status")
    except Exception:
        logger.exception("[push_stream] failed to start refresh after open thread")


__all__ = [
    "on_message",
    "on_error",
    "on_close",
    "on_open",
]
