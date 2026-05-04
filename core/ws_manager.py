# ============================================================
# ws_manager.py（Ver24-NOLOG — サイレントWebSocket版）
# ============================================================

import websocket
import threading
import json
import datetime as dt
import logging

from global_state import global_data
from core.push_manager import on_push_message

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# WebSocket: message handler
# ------------------------------------------------------------
def _on_message(ws, message):
    try:
        now = dt.datetime.now()

        try:
            content = json.loads(message)
        except Exception:
            logger.debug(f"WS message JSON decode error")
            return

        on_push_message(content, now)

    except Exception as e:
        logger.debug(f"WS message error: {e}")

# ------------------------------------------------------------
# WebSocket: error handler
# ------------------------------------------------------------
def _on_error(ws, error):
    logger.debug(f"WS error suppressed: {error}")

# ------------------------------------------------------------
# WebSocket: closed
# ------------------------------------------------------------
def _on_close(ws, a, b):
    logger.debug("WS closed")

# ------------------------------------------------------------
# WebSocket: opened
# ------------------------------------------------------------
def _on_open(ws):
    logger.debug("WS opened")

# ------------------------------------------------------------
# WebSocket Start
# ------------------------------------------------------------
def start_ws():
    url = global_data.ws_url

    if not url:
        raise RuntimeError("❌ global_data.ws_url がセットされていません")

    def run():
        while True:
            try:
                ws = websocket.WebSocketApp(
                    url,
                    on_message=_on_message,
                    on_error=_on_error,
                    on_close=_on_close,
                    on_open=_on_open,
                )
                ws.run_forever()
            except Exception as e:
                logger.debug(f"WS fatal error suppressed: {e}")

    threading.Thread(target=run, daemon=True).start()
    logger.debug(f"WebSocket connecting → {url}")
