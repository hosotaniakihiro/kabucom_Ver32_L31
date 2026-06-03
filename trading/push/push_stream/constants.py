# ============================================================
# File   : trading/push/push_stream/constants.py
# Version: Ver1.2-PUSH-STREAM-CONSTANTS-LOW-PRESSURE
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

# kabu Station WebSocketが50銘柄登録直後にWinError 10054で切断される環境があるため、
# 既定の同時登録数を30へ落とす。必要時はrotation_settings側の環境変数で上書きする。
DEFAULT_REGISTER_CHUNK_SIZE = 30
DEFAULT_REGISTER_MAX_SYMBOLS = 60

# PUSHローテーションのデフォルト登録維持時間。
# 30銘柄登録 -> 30秒維持 -> 次の30銘柄登録、を想定する。
DEFAULT_ROTATE_WAIT_SEC = 30.0
