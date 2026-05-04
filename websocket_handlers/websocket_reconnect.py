# ============================================================
# websocket_reconnect.py（Ver24-RECONNECT-CLEAN）
# ------------------------------------------------------------
# WebSocket が閉じたときに自動で再接続する
# ・startup が connect を呼ばないようにする
# ・初回接続もここで実行
# ・WS が閉じたら即再接続
# ============================================================

import threading
import time
import logging

logger = logging.getLogger("websocket_reconnect")

_WS_URL = None
_CONNECT_FUNC = None
_WS_CLOSED = False


def set_ws_url(url: str):
    global _WS_URL
    _WS_URL = url


def set_connect_func(func):
    global _CONNECT_FUNC
    _CONNECT_FUNC = func


def notify_ws_closed():
    """WebSocket CLOSED コールバック"""
    global _WS_CLOSED
    _WS_CLOSED = True


def _reconnect_loop():
    """
    WS CLOSE → connect のループ
    """
    global _WS_CLOSED

    while True:
        if _WS_CLOSED:
            _WS_CLOSED = False

            try:
                logger.warning("[WebSocket] Disconnected → reconnecting ...")
                if _CONNECT_FUNC and _WS_URL:
                    _CONNECT_FUNC(_WS_URL)
            except Exception as e:
                logger.error(f"Reconnect error: {e}")

        time.sleep(3)


def start_reconnect_monitor(initial_connect=True):
    """
    最初の1回もここで接続する（startup.py から呼ぶだけ）
    """
    if initial_connect:
        try:
            if _CONNECT_FUNC and _WS_URL:
                logger.info("[WebSocket] Initial connect")
                _CONNECT_FUNC(_WS_URL)
        except Exception as e:
            logger.error(f"Initial connect error: {e}")

    th = threading.Thread(target=_reconnect_loop, daemon=True)
    th.start()
