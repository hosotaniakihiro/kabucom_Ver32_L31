# ============================================================
# File   : trading/entry/tonosama/config.py
# Version: Ver2.2-TONOSAMA-BALANCED-PRODUCTION
# ------------------------------------------------------------
# 方針:
#   - 出来高急増履歴が不足する起動直後でも、volume_surge.py 側で fail-open 可能にした。
#   - その一方で Ver2.1 の MIN_PRICE_CHANGE_PCT=1.5% / MIN_SLOPE=0.01 は
#     厳しすぎ、ログ上 base_rows=37 primary_rows=0 で全滅していた。
#   - ここでは「誤発注を増やしすぎないが、候補が全滅しない」中間値にする。
#
# Balanced settings:
#   MIN_PRICE               >= 300円
#   MIN_FINAL_SCORE         >= 2.5
#   MIN_VOLUME_SURGE_RATIO  >= 3.0
#   MIN_PRICE_CHANGE_PCT    >= 0.05%
#   MIN_SLOPE               >= 0.0003
#   MIN_LATEST_VOLUME       >= 50,000株
#   MIN_5SEC_PRICE_CHANGE   >= 0.01% when 5s bar exists
#   REQUIRE_5SEC_BAR        default False
#
# 注意:
#   5秒足は必須にしない。取れている場合だけ弱い動きは落とす。
#   完全に止まっている銘柄は runner.py 側の latest_volume / price_change / slope / 5s で落とす。
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

# fail-open 値も 3.0 に合わせるため、出来高急増の入口は 3倍以上を維持。
MIN_VOLUME_SURGE_RATIO = _env_float_floor("TONOSAMA_MIN_VOLUME_SURGE_RATIO", 3.0, 3.0)

# Ver2.1 の 1.5% は intraday scalping では厳しすぎたため 0.05% へ戻す。
# 旧 0.03% よりはやや厳しめ。
MIN_PRICE_CHANGE_PCT = _env_float_floor("TONOSAMA_MIN_PRICE_CHANGE_PCT", 0.05, 0.05)

# Ver2.1 の 0.01 は 1%相当の傾き扱いになりやすく全滅したため、
# 0.0003 を下限にする。小さすぎる 0.0001 は避ける。
MIN_SLOPE = _env_float_floor("TONOSAMA_MIN_SLOPE", 0.0003, 0.0003)

# body は open==close の足で 0 になりやすいため、既定では強制しない。
MIN_BODY_CHANGE_PCT = _env_float("TONOSAMA_MIN_BODY_CHANGE_PCT", 0.0)
MIN_INTRABAR_RANGE_PCT = _env_float_floor("TONOSAMA_MIN_INTRABAR_RANGE_PCT", 0.10, 0.10)

# 直近出来高が少ない銘柄のアラートを抑止する。
MIN_LATEST_VOLUME = _env_float_floor("TONOSAMA_MIN_LATEST_VOLUME", 50000.0, 50000.0)

VOLUME_AVG_LOOKBACK_BARS = _env_int("TONOSAMA_VOLUME_AVG_LOOKBACK_BARS", 5)

USE_5SEC_CONFIRM = _env_bool("TONOSAMA_USE_5SEC_CONFIRM", True)
MIN_5SEC_PRICE_CHANGE_PCT = _env_float_floor("TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT", 0.01, 0.01)
MIN_5SEC_VOLUME_SURGE_RATIO = _env_float("TONOSAMA_MIN_5SEC_VOLUME_SURGE_RATIO", 1.5)
MAX_5SEC_DROP_PCT = _env_float("TONOSAMA_MAX_5SEC_DROP_PCT", -0.20)

# 5秒足は必須にしない。取れている場合だけ runner.py 側で 0.01%以上を要求する。
REQUIRE_5SEC_BAR = _env_bool("TONOSAMA_REQUIRE_5SEC_BAR", False)

MAX_PENDING_PER_LOOP = _env_int("TONOSAMA_MAX_PENDING_PER_LOOP", 10)
MAX_CANDIDATES = _env_int("TONOSAMA_MAX_CANDIDATES", 40)
SCHEDULER_INTERVAL_SEC = _env_int("TONOSAMA_SCHEDULER_INTERVAL_SEC", 15)
DISCORD_NOTIFY_ON_PENDING = _env_bool("TONOSAMA_DISCORD_NOTIFY_ON_PENDING", True)
MAX_5SEC_FEATURE_SYMBOLS = _env_int("TONOSAMA_MAX_5SEC_FEATURE_SYMBOLS", 20)
