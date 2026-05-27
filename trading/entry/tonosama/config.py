# ============================================================
# File   : trading/entry/tonosama/config.py
# Version: Ver2.5-TONOSAMA-NO-ZERO-MOVEMENT-FLOORS
# ------------------------------------------------------------
# 方針:
#   - 起動直後やPUSH再接続直後は、3m/5mの行は存在しても、
#     prev5_volume_avg がまだ作れず volume_surge_ratio が全NaNになりやすい。
#   - Ver1.9 の volume_surge.py は TONOSAMA_FORCE_SURGE_FAILOPEN=1 が無いと
#     この状態で base feature empty にする。
#   - 09:21ログでは base_rows=44 / df3=44 / df5=44 があるのに
#     force_failopen=False で全落ちしていたため、既定で fail-open を戻す。
#   - 完全に止まっている銘柄は runner.py 側の latest_volume / price_change / slope / 5s で落とす。
#
# Ver2.4:
#   - 出来高急増だけで高値掴み/安値売りをしない。
#   - BUY: 上がり過ぎ・高値圏・上ヒゲ反落・バイイングクライマックス疑いを除外。
#   - SELL: 下がり過ぎ・安値圏・下ヒゲ反発・セリングクライマックス疑いを除外。
#
# Ver2.5:
#   - 6996 のような max_surge=3.00x だけで、価格変化0.14% / 5s=0.000% /
#     slope=0.0014 の候補が pending になる問題を抑止。
#   - 5秒足は必須にしないが、取れている場合のゼロ変化は runner/AI fallback 側で落とす。
#   - 価格変化と傾きの最低下限を引き上げる。
#
# Balanced strict settings:
#   MIN_PRICE               >= 300円
#   MIN_FINAL_SCORE         >= 2.5
#   MIN_VOLUME_SURGE_RATIO  >= 3.0
#   MIN_PRICE_CHANGE_PCT    >= 0.30%
#   MIN_SLOPE               >= 0.0030
#   MIN_5SEC_PRICE_CHANGE   >= 0.05% when 5秒足あり
#   MIN_LATEST_VOLUME       >= 50,000株
#   REQUIRE_5SEC_BAR        default False
# ============================================================

from __future__ import annotations

import os

# volume_surge.py Ver1.9 はこのENVだけを見る。
# settings/batで明示的に 0 を入れている場合はそちらを尊重する。
os.environ.setdefault("TONOSAMA_FORCE_SURGE_FAILOPEN", "1")
os.environ.setdefault("TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE", "3.0")


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

# Ver2.5: 0.14% のような微小変化は通さない。
MIN_PRICE_CHANGE_PCT = _env_float_floor("TONOSAMA_MIN_PRICE_CHANGE_PCT", 0.30, 0.30)

# Ver2.5: slope=0.0014 程度の横ばいは通さない。
MIN_SLOPE = _env_float_floor("TONOSAMA_MIN_SLOPE", 0.0030, 0.0030)

MIN_BODY_CHANGE_PCT = _env_float("TONOSAMA_MIN_BODY_CHANGE_PCT", 0.0)
MIN_INTRABAR_RANGE_PCT = _env_float_floor("TONOSAMA_MIN_INTRABAR_RANGE_PCT", 0.10, 0.10)
MIN_LATEST_VOLUME = _env_float_floor("TONOSAMA_MIN_LATEST_VOLUME", 50000.0, 50000.0)

# ------------------------------------------------------------
# BUY buying climax / high-chase guard
# ------------------------------------------------------------
MAX_BUY_PRICE_CHANGE_PCT = _env_float("TONOSAMA_MAX_BUY_PRICE_CHANGE_PCT", 0.80)
MAX_BUY_CLOSE_POSITION_PCT = _env_float("TONOSAMA_MAX_BUY_CLOSE_POSITION_PCT", 90.0)
MAX_BUY_UPPER_WICK_PCT = _env_float("TONOSAMA_MAX_BUY_UPPER_WICK_PCT", 45.0)
BUYING_CLIMAX_MIN_SURGE_RATIO = _env_float("TONOSAMA_BUYING_CLIMAX_MIN_SURGE_RATIO", 3.0)
BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT = _env_float("TONOSAMA_BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT", 0.50)

# ------------------------------------------------------------
# SELL selling climax / low-chase guard
# ------------------------------------------------------------
MAX_SELL_PRICE_DROP_PCT = _env_float("TONOSAMA_MAX_SELL_PRICE_DROP_PCT", 0.80)
MIN_SELL_CLOSE_POSITION_PCT = _env_float("TONOSAMA_MIN_SELL_CLOSE_POSITION_PCT", 10.0)
MAX_SELL_LOWER_WICK_PCT = _env_float("TONOSAMA_MAX_SELL_LOWER_WICK_PCT", 45.0)
SELLING_CLIMAX_MIN_SURGE_RATIO = _env_float("TONOSAMA_SELLING_CLIMAX_MIN_SURGE_RATIO", 3.0)
SELLING_CLIMAX_MIN_PRICE_DROP_PCT = _env_float("TONOSAMA_SELLING_CLIMAX_MIN_PRICE_DROP_PCT", 0.50)

VOLUME_AVG_LOOKBACK_BARS = _env_int("TONOSAMA_VOLUME_AVG_LOOKBACK_BARS", 5)

USE_5SEC_CONFIRM = _env_bool("TONOSAMA_USE_5SEC_CONFIRM", True)
MIN_5SEC_PRICE_CHANGE_PCT = _env_float_floor("TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT", 0.05, 0.05)
MIN_5SEC_VOLUME_SURGE_RATIO = _env_float("TONOSAMA_MIN_5SEC_VOLUME_SURGE_RATIO", 1.5)
MAX_5SEC_DROP_PCT = _env_float("TONOSAMA_MAX_5SEC_DROP_PCT", -0.20)
REQUIRE_5SEC_BAR = _env_bool("TONOSAMA_REQUIRE_5SEC_BAR", False)

MAX_PENDING_PER_LOOP = _env_int("TONOSAMA_MAX_PENDING_PER_LOOP", 10)
MAX_CANDIDATES = _env_int("TONOSAMA_MAX_CANDIDATES", 40)
SCHEDULER_INTERVAL_SEC = _env_int("TONOSAMA_SCHEDULER_INTERVAL_SEC", 15)
DISCORD_NOTIFY_ON_PENDING = _env_bool("TONOSAMA_DISCORD_NOTIFY_ON_PENDING", True)
MAX_5SEC_FEATURE_SYMBOLS = _env_int("TONOSAMA_MAX_5SEC_FEATURE_SYMBOLS", 20)
