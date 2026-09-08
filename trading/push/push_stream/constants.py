# ============================================================
# File   : trading/push/push_stream/constants.py
# Version: Ver1.3-PUSH-STREAM-CONSTANTS-50-SYMBOL-AB
# ============================================================

DEFAULT_WS_URL = "ws://localhost:18080/kabusapi/websocket"

import os as _os


def _env_int(name, default):
    try:
        v = _os.getenv(name)
        return int(str(v).strip()) if v not in (None, "") else int(default)
    except Exception:
        return int(default)


def _env_float(name, default):
    try:
        v = _os.getenv(name)
        return float(str(v).strip()) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


# ------------------------------------------------------------
# 2026-09-03 P0: NAS の commit が遅いので 1 回の commit で多く書く。
#
# 本番実測 (push20260903.db, SMB, WAL, synchronous=NORMAL):
#     executemany 100 行 = 0.063-0.111s
#     commit             = 6.3 / 8.8 / 11.8 / 12.2 / 16.1 s
# commit のコストは行数にほとんど依存せず、SMB の fsync 往復で決まる。
# つまり batch を大きくするのはほぼ無料で、スループットに直結する。
#
# batch=100 のままだと 12 秒に 100 行 = 約 500 行/分しか書けず、
# 後場寄りの 1,300-1,700 msg/分に追いつかない
# (実測: queue 242 -> 4,482、db_age 29s -> 180s と発散)。
# ------------------------------------------------------------
FLUSH_BATCH_SIZE = max(1, _env_int("PUSH_FLUSH_BATCH_SIZE", 1000))
FLUSH_INTERVAL_SEC = max(0.2, _env_float("PUSH_FLUSH_INTERVAL_SEC", 2.0))
MONITOR_INTERVAL_SEC = 5.0
RECONNECT_WAIT_SEC = 0.5
AFTER_OPEN_REFRESH_DELAY_SEC = 1.2
WS_READY_WAIT_SEC = 3.0
WS_READY_POLL_SEC = 0.1

MAX_DF_ROWS = 50000
MAX_RAW_LOG_CHARS = 1200

# kabu Station のPUSH登録上限に合わせ、A/B 各50銘柄で100銘柄を回す。
# 設計:
#   A 50銘柄 register -> 4.8秒受信 -> unregister/all -> 0.2秒待機
#   B 50銘柄 register -> 4.8秒受信 -> unregister/all -> 0.2秒待機
# 必要時は rotation_settings 側の環境変数で上書きする。
DEFAULT_REGISTER_CHUNK_SIZE = 50
DEFAULT_REGISTER_MAX_SYMBOLS = 100

# PUSHローテーションのデフォルト登録維持時間。
DEFAULT_ROTATE_WAIT_SEC = 4.8
