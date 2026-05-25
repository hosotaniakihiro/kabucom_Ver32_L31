# ============================================================
# File   : trading/entry/tonosama/config.py
# Version: Ver2.0-TONOSAMA-LIVE-HISTORY-MISSING-RELAX
# ------------------------------------------------------------
# Ver1.9:
#   - 古い緩い環境変数を下限で締めた。
#
# Ver2.0 Fix:
#   - 最新ログで 3m/5m 履歴不足のため _max_volume_surge_ratio=0.0 になり、
#     MIN_VOLUME_SURGE_RATIO=3.0 で全候補が volume_surge_low 落ち。
#   - main.py のPUSH対象は38〜45銘柄程度で symbol_hist_len=1〜2 のことがあり、
#     3m/5mの出来高平均を常に作れない。
#   - TONOSAMAは「候補生成 → entry_controller の板/リスク/発注前ガード」へ
#     渡す入口なので、履歴不足時に全停止させない。
#   - 5秒足は任意。取れている時だけ0.0未満の急落を止める。
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


def _env_float_cap(name: str, default: float, cap: float) -> float:
    """古いbat/settings側の厳しすぎる値が残っていても、live用上限に丸める。"""
    try:
        return min(float(cap), _env_float(name, default))
    except Exception:
        return float(min(default, cap))


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

# 低位株は引き続き抑止。ただし以前の200円より少し厳しく300円。
MIN_PRICE = _env_float_floor("TONOSAMA_MIN_PRICE", 300.0, 300.0)

# pending作成直前の最終スコア。entry_controller側で最終安全確認するため1.5。
MIN_FINAL_SCORE = _env_float_cap("TONOSAMA_MIN_FINAL_SCORE", 1.5, 1.5)

MIN_RAW_SCORE = _env_float("TONOSAMA_MIN_RAW_SCORE", 0.01)

# 履歴不足時に ratio=0 になるため、ここを厳しくしすぎると全停止する。
# volume_surge.py側で履歴不足時だけ救済し、通常時はこの閾値を使用する。
MIN_VOLUME_SURGE_RATIO = _env_float_cap("TONOSAMA_MIN_VOLUME_SURGE_RATIO", 2.0, 2.0)

# 1.5%は15秒/1〜5分スキャルでは厳しすぎるため0.30%上限。
MIN_PRICE_CHANGE_PCT = _env_float_cap("TONOSAMA_MIN_PRICE_CHANGE_PCT", 0.03, 0.30)

# ほぼ横ばい排除はrunner側の既存 -0.02 ガードに任せる。ここは将来用。
MIN_SLOPE = _env_float_cap("TONOSAMA_MIN_SLOPE", 0.0, 0.0)

MIN_BODY_CHANGE_PCT = _env_float("TONOSAMA_MIN_BODY_CHANGE_PCT", 0.0)
MIN_INTRABAR_RANGE_PCT = _env_float_floor("TONOSAMA_MIN_INTRABAR_RANGE_PCT", 0.10, 0.10)
MIN_LATEST_VOLUME = _env_float_floor("TONOSAMA_MIN_LATEST_VOLUME", 50000.0, 50000.0)

VOLUME_AVG_LOOKBACK_BARS = _env_int("TONOSAMA_VOLUME_AVG_LOOKBACK_BARS", 5)

USE_5SEC_CONFIRM = _env_bool("TONOSAMA_USE_5SEC_CONFIRM", True)
# 5秒足は0.05以下なら必須条件にしない。急落防止だけrunner側で使う。
MIN_5SEC_PRICE_CHANGE_PCT = 0.0 if _env_float("TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT", 0.0) <= 0.05 else _env_float_cap("TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT", 0.05, 0.10)
MIN_5SEC_VOLUME_SURGE_RATIO = _env_float("TONOSAMA_MIN_5SEC_VOLUME_SURGE_RATIO", 1.5)
MAX_5SEC_DROP_PCT = _env_float("TONOSAMA_MAX_5SEC_DROP_PCT", -0.20)

# 5秒足は必須にしない。
REQUIRE_5SEC_BAR = False

MAX_PENDING_PER_LOOP = _env_int("TONOSAMA_MAX_PENDING_PER_LOOP", 10)
MAX_CANDIDATES = _env_int("TONOSAMA_MAX_CANDIDATES", 40)
SCHEDULER_INTERVAL_SEC = _env_int("TONOSAMA_SCHEDULER_INTERVAL_SEC", 15)
DISCORD_NOTIFY_ON_PENDING = _env_bool("TONOSAMA_DISCORD_NOTIFY_ON_PENDING", True)
MAX_5SEC_FEATURE_SYMBOLS = _env_int("TONOSAMA_MAX_5SEC_FEATURE_SYMBOLS", 20)
