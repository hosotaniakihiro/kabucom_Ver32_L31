# ============================================================
# File   : trading/entry/tonosama/config.py
# Version: Ver2.6-TONOSAMA-BALANCED-CANDIDATE-RECOVERY
# ------------------------------------------------------------
# 方針:
#   - volume_surge.py / history guard で履歴不足fail-openを許可した後、
#     runner.py 側の本体条件が強すぎて candidates=0 に戻る問題を緩和。
#   - 2026-05-27ログでは43件作成後、price_change_low_abs等で最終0件。
#   - 6996のような微小変化だけの候補は避けつつ、4592/6072のように
#     実値動きがある候補を残す水準へ調整。
#
# Balanced settings:
#   MIN_PRICE               >= 300円
#   MIN_FINAL_SCORE         >= 2.5
#   MIN_VOLUME_SURGE_RATIO  >= 3.0
#   MIN_PRICE_CHANGE_PCT    >= 0.20%
#   MIN_SLOPE               >= 0.0010
#   MIN_5SEC_PRICE_CHANGE   >= 0.01% when 5秒足あり
#   MIN_LATEST_VOLUME       >= 50,000株
#   MAX_BUY_PRICE_CHANGE    <= 1.20%
#   MAX_SELL_PRICE_DROP     <= 1.20%
#   REQUIRE_5SEC_BAR        default False
# ============================================================

from __future__ import annotations

import os

# volume_surge.py が参照するfail-open系ENV。
# settings/batで明示的に 0 を入れている場合はそちらを尊重する。
os.environ.setdefault("TONOSAMA_FORCE_SURGE_FAILOPEN", "1")
os.environ.setdefault("TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE", "3.0")
os.environ.setdefault("TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY", "1")
os.environ.setdefault("TONOSAMA_DROP_HISTORY_MISSING_ENTRY", "0")

# 本体フィルタの既定値。外部ENVがあれば外部値を尊重する。
os.environ.setdefault("TONOSAMA_MIN_PRICE_CHANGE_PCT", "0.20")
os.environ.setdefault("TONOSAMA_MIN_SLOPE", "0.0010")
os.environ.setdefault("TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT", "0.01")
os.environ.setdefault("TONOSAMA_MAX_BUY_PRICE_CHANGE_PCT", "1.20")
os.environ.setdefault("TONOSAMA_MAX_SELL_PRICE_DROP_PCT", "1.20")


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_float_floor(name: str, default: float, floor: float) -> float:
    try:
        return max(float(floor), _env_float(name, default))
    except Exception:
        return float(max(default, floor))


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


TONOSAMA_EXPIRE_SEC = _env_int("TONOSAMA_EXPIRE_SEC", 180)

MIN_PRICE = _env_float_floor("TONOSAMA_MIN_PRICE", 300.0, 300.0)
MIN_FINAL_SCORE = _env_float_floor("TONOSAMA_MIN_FINAL_SCORE", 2.5, 2.5)
MIN_RAW_SCORE = _env_float("TONOSAMA_MIN_RAW_SCORE", 0.01)

MIN_VOLUME_SURGE_RATIO = _env_float_floor("TONOSAMA_MIN_VOLUME_SURGE_RATIO", 3.0, 3.0)

# Ver2.6: 0.30%では候補0が続いたため、0.20%へ緩和。
MIN_PRICE_CHANGE_PCT = _env_float_floor("TONOSAMA_MIN_PRICE_CHANGE_PCT", 0.20, 0.20)

# Ver2.6: slope=0.0030では短期履歴不足時に厳しいため、0.0010へ緩和。
MIN_SLOPE = _env_float_floor("TONOSAMA_MIN_SLOPE", 0.0010, 0.0010)

MIN_BODY_CHANGE_PCT = _env_float("TONOSAMA_MIN_BODY_CHANGE_PCT", 0.0)
MIN_INTRABAR_RANGE_PCT = _env_float_floor("TONOSAMA_MIN_INTRABAR_RANGE_PCT", 0.10, 0.10)
MIN_LATEST_VOLUME = _env_float_floor("TONOSAMA_MIN_LATEST_VOLUME", 50000.0, 50000.0)

# ------------------------------------------------------------
# BUY buying climax / high-chase guard
# ------------------------------------------------------------
MAX_BUY_PRICE_CHANGE_PCT = _env_float("TONOSAMA_MAX_BUY_PRICE_CHANGE_PCT", 1.20)
MAX_BUY_CLOSE_POSITION_PCT = _env_float("TONOSAMA_MAX_BUY_CLOSE_POSITION_PCT", 90.0)
MAX_BUY_UPPER_WICK_PCT = _env_float("TONOSAMA_MAX_BUY_UPPER_WICK_PCT", 45.0)
BUYING_CLIMAX_MIN_SURGE_RATIO = _env_float("TONOSAMA_BUYING_CLIMAX_MIN_SURGE_RATIO", 3.0)
BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT = _env_float("TONOSAMA_BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT", 0.50)

# ------------------------------------------------------------
# SELL selling climax / low-chase guard
# ------------------------------------------------------------
MAX_SELL_PRICE_DROP_PCT = _env_float("TONOSAMA_MAX_SELL_PRICE_DROP_PCT", 1.20)
MIN_SELL_CLOSE_POSITION_PCT = _env_float("TONOSAMA_MIN_SELL_CLOSE_POSITION_PCT", 10.0)
MAX_SELL_LOWER_WICK_PCT = _env_float("TONOSAMA_MAX_SELL_LOWER_WICK_PCT", 45.0)
SELLING_CLIMAX_MIN_SURGE_RATIO = _env_float("TONOSAMA_SELLING_CLIMAX_MIN_SURGE_RATIO", 3.0)
SELLING_CLIMAX_MIN_PRICE_DROP_PCT = _env_float("TONOSAMA_SELLING_CLIMAX_MIN_PRICE_DROP_PCT", 0.50)

VOLUME_AVG_LOOKBACK_BARS = _env_int("TONOSAMA_VOLUME_AVG_LOOKBACK_BARS", 5)

USE_5SEC_CONFIRM = _env_bool("TONOSAMA_USE_5SEC_CONFIRM", True)
MIN_5SEC_PRICE_CHANGE_PCT = _env_float_floor("TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT", 0.01, 0.01)
MIN_5SEC_VOLUME_SURGE_RATIO = _env_float("TONOSAMA_MIN_5SEC_VOLUME_SURGE_RATIO", 1.5)
MAX_5SEC_DROP_PCT = _env_float("TONOSAMA_MAX_5SEC_DROP_PCT", -0.20)
REQUIRE_5SEC_BAR = _env_bool("TONOSAMA_REQUIRE_5SEC_BAR", False)

MAX_PENDING_PER_LOOP = _env_int("TONOSAMA_MAX_PENDING_PER_LOOP", 10)
MAX_CANDIDATES = _env_int("TONOSAMA_MAX_CANDIDATES", 40)
SCHEDULER_INTERVAL_SEC = _env_int("TONOSAMA_SCHEDULER_INTERVAL_SEC", 15)
DISCORD_NOTIFY_ON_PENDING = _env_bool("TONOSAMA_DISCORD_NOTIFY_ON_PENDING", True)
MAX_5SEC_FEATURE_SYMBOLS = _env_int("TONOSAMA_MAX_5SEC_FEATURE_SYMBOLS", 20)
