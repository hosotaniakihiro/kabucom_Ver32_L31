# ============================================================
# File   : trading/entry/tonosama/config.py
# Version: Ver1.9-TONOSAMA-STRICT-FLOOR-5SEC-OPTIONAL
# ------------------------------------------------------------
# Ver1.8:
#   - 5秒足は必須にしない。
#   - 5秒足が取れている場合だけ 5秒変化率 0.05%以上を要求する。
#
# Ver1.9:
#   - main.py / bat / 環境変数が古い緩い値を入れていても、殿様側で下限を強制する。
#   - 6981 のような 出来高急増2.00x / 価格変化0.06% / 5s0.012% / AI未接続 は通さない。
#   - 5秒足は任意のまま。取れている場合だけ 5s>=0.05 を要求する。
# ============================================================

from __future__ import annotations

import os


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_float_floor(name: str, default: float, floor: float) -> float:
    """古い環境変数が緩い値を入れていても、最低下限より下にはしない。"""
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

# 低位・板が薄い銘柄を減らす。
MIN_PRICE = _env_float_floor("TONOSAMA_MIN_PRICE", 300.0, 300.0)

# pending作成直前の最終スコア。
MIN_FINAL_SCORE = _env_float_floor("TONOSAMA_MIN_FINAL_SCORE", 2.5, 2.5)

# raw score は 0 より大きければ候補として残す。
MIN_RAW_SCORE = _env_float("TONOSAMA_MIN_RAW_SCORE", 0.01)

# 履歴不足時の fail-open 2.0 では緩すぎるため、3倍以上を下限にする。
MIN_VOLUME_SURGE_RATIO = _env_float_floor("TONOSAMA_MIN_VOLUME_SURGE_RATIO", 3.0, 3.0)

# 明確な短期変化だけを拾う。0.06% のような微小変化は通さない。
MIN_PRICE_CHANGE_PCT = _env_float_floor("TONOSAMA_MIN_PRICE_CHANGE_PCT", 1.5, 1.5)

# slope=0.0001 のようなほぼ横ばいを減らす。
MIN_SLOPE = _env_float_floor("TONOSAMA_MIN_SLOPE", 0.01, 0.01)

# body は open==close の足で 0 になりやすいため、既定では強制しない。
MIN_BODY_CHANGE_PCT = _env_float("TONOSAMA_MIN_BODY_CHANGE_PCT", 0.0)
MIN_INTRABAR_RANGE_PCT = _env_float_floor("TONOSAMA_MIN_INTRABAR_RANGE_PCT", 0.10, 0.10)

# 直近出来高が少ない銘柄のアラートを抑止する。
MIN_LATEST_VOLUME = _env_float_floor("TONOSAMA_MIN_LATEST_VOLUME", 50000.0, 50000.0)

VOLUME_AVG_LOOKBACK_BARS = _env_int("TONOSAMA_VOLUME_AVG_LOOKBACK_BARS", 5)

USE_5SEC_CONFIRM = _env_bool("TONOSAMA_USE_5SEC_CONFIRM", True)
MIN_5SEC_PRICE_CHANGE_PCT = _env_float_floor("TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT", 0.05, 0.05)
MIN_5SEC_VOLUME_SURGE_RATIO = _env_float("TONOSAMA_MIN_5SEC_VOLUME_SURGE_RATIO", 1.5)
MAX_5SEC_DROP_PCT = _env_float("TONOSAMA_MAX_5SEC_DROP_PCT", -0.20)

# 5秒足は必須にしない。取れている場合だけ runner.py 側で 0.05%以上を要求する。
REQUIRE_5SEC_BAR = _env_bool("TONOSAMA_REQUIRE_5SEC_BAR", False)

MAX_PENDING_PER_LOOP = _env_int("TONOSAMA_MAX_PENDING_PER_LOOP", 10)
MAX_CANDIDATES = _env_int("TONOSAMA_MAX_CANDIDATES", 40)
SCHEDULER_INTERVAL_SEC = _env_int("TONOSAMA_SCHEDULER_INTERVAL_SEC", 15)
DISCORD_NOTIFY_ON_PENDING = _env_bool("TONOSAMA_DISCORD_NOTIFY_ON_PENDING", True)
MAX_5SEC_FEATURE_SYMBOLS = _env_int("TONOSAMA_MAX_5SEC_FEATURE_SYMBOLS", 20)
