# ============================================================
# File   : trading/push/push_stream/constants.py
# Version: Ver1.0-PUSH-STREAM-CONSTANTS
# ============================================================

DEFAULT_WS_URL = "ws://localhost:18080/kabusapi/websocket"

FLUSH_BATCH_SIZE = 100
FLUSH_INTERVAL_SEC = 2.0
MONITOR_INTERVAL_SEC = 5.0
RECONNECT_WAIT_SEC = 0.5
AFTER_OPEN_REFRESH_DELAY_SEC = 1.2
WS_READY_WAIT_SEC = 3.0
WS_READY_POLL_SEC = 0.1

MAX_DF_ROWS = 50000
MAX_RAW_LOG_CHARS = 1200

DEFAULT_REGISTER_CHUNK_SIZE = 50
DEFAULT_REGISTER_MAX_SYMBOLS = 100
DEFAULT_ROTATE_WAIT_SEC = 5.0