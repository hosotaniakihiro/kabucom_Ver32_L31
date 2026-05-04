# ============================================================
# File   : trading/entry/tonosama/config.py
# Version: Ver1.0-TONOSAMA-ENTRY-CONFIG
# ============================================================
from __future__ import annotations

TONOSAMA_EXPIRE_SEC = 180
MIN_PRICE = 200.0
MIN_FINAL_SCORE = 3.0
MIN_RAW_SCORE = 0.01
MIN_VOLUME_SURGE_RATIO = 2.0
MIN_PRICE_CHANGE_PCT = 0.6
VOLUME_AVG_LOOKBACK_BARS = 5
USE_5SEC_CONFIRM = True
MIN_5SEC_PRICE_CHANGE_PCT = 0.10
MIN_5SEC_VOLUME_SURGE_RATIO = 1.5
MAX_5SEC_DROP_PCT = -0.20
REQUIRE_5SEC_BAR = False
MAX_PENDING_PER_LOOP = 20
MAX_CANDIDATES = 80
SCHEDULER_INTERVAL_SEC = 15
DISCORD_NOTIFY_ON_PENDING = True
