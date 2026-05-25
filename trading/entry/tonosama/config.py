# ============================================================
# File   : trading/entry/tonosama/config.py
# Version: Ver1.5-TONOSAMA-5SEC-FLAT-ALLOW
# ------------------------------------------------------------
# Ver1.2:
#   - 固定値を環境変数対応
#   - MIN_PRICE_CHANGE_PCT 既定値を 0.6% -> 0.03% に緩和
#   - MIN_FINAL_SCORE 既定値を 3.0 -> 2.0 に緩和
#
# Ver1.3:
#   - 「全然動いていない銘柄」に殿様アラートが出る問題を防ぐため、
#     1分足の実体値動き・高安値幅・直近出来高の下限を追加。
#
# Ver1.4:
#   - _body_change_pct が全銘柄 0.0 のケースで全落ちしないよう、
#     body_change 既定値を 0.0 に緩和。
#
# Ver1.5:
#   - 最新ログで primary は 1件通過したが、price_change_5s_pct=0.0 のため
#     five_sec_price_change_ng で全落ち。
#   - 5秒足は取得タイミングにより 0.0 になりやすいので、TONOSAMA では
#     5秒変化率を必須にしない。
#   - 動きの確認は _max_price_change_pct / _intrabar_range_pct / volume で行う。
#   - main.py が TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT=0.01 を setdefault していても、
#     0.01 以下は 0.0 に丸めて全落ちを防ぐ。
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


def _tonosama_5sec_threshold() -> float:
    """
    main.py 側で TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT=0.01 が setdefault されるため、
    config default を 0.0 にしても環境変数経由で 0.01 になり得る。
    最新ログでは 5秒足が has_5sec_bar=True でも price_change_5s_pct=0.0 のため
    全落ちしていたので、0.01 以下は実質OFFとして扱う。
    """
    v = _env_float("TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT", 0.0)
    if v <= 0.01:
        return 0.0
    return float(v)


TONOSAMA_EXPIRE_SEC = _env_int("TONOSAMA_EXPIRE_SEC", 180)

MIN_PRICE = _env_float("TONOSAMA_MIN_PRICE", 200.0)

# pending作成直前の最終スコア。厳しすぎると候補は出ても登録されない。
MIN_FINAL_SCORE = _env_float("TONOSAMA_MIN_FINAL_SCORE", 2.0)

# raw score は 0 より大きければ候補として残す。
MIN_RAW_SCORE = _env_float("TONOSAMA_MIN_RAW_SCORE", 0.01)

# 履歴不足時は volume_surge.py 側で fail-open value=2.0 を入れるため、既定2.0のまま。
MIN_VOLUME_SURGE_RATIO = _env_float("TONOSAMA_MIN_VOLUME_SURGE_RATIO", 2.0)

# 以前の 0.6% は15秒/1〜5分スキャルピングでは厳しすぎる。
MIN_PRICE_CHANGE_PCT = _env_float("TONOSAMA_MIN_PRICE_CHANGE_PCT", 0.03)

# body は open==close の足で 0 になりやすいため、既定では強制しない。
# 動いているかどうかは intrabar range と latest volume で判定する。
MIN_BODY_CHANGE_PCT = _env_float("TONOSAMA_MIN_BODY_CHANGE_PCT", 0.0)
MIN_INTRABAR_RANGE_PCT = _env_float("TONOSAMA_MIN_INTRABAR_RANGE_PCT", 0.10)
MIN_LATEST_VOLUME = _env_float("TONOSAMA_MIN_LATEST_VOLUME", 3000.0)

VOLUME_AVG_LOOKBACK_BARS = _env_int("TONOSAMA_VOLUME_AVG_LOOKBACK_BARS", 5)

USE_5SEC_CONFIRM = _env_bool("TONOSAMA_USE_5SEC_CONFIRM", True)
MIN_5SEC_PRICE_CHANGE_PCT = _tonosama_5sec_threshold()
MIN_5SEC_VOLUME_SURGE_RATIO = _env_float("TONOSAMA_MIN_5SEC_VOLUME_SURGE_RATIO", 1.5)
MAX_5SEC_DROP_PCT = _env_float("TONOSAMA_MAX_5SEC_DROP_PCT", -0.20)
REQUIRE_5SEC_BAR = _env_bool("TONOSAMA_REQUIRE_5SEC_BAR", False)

MAX_PENDING_PER_LOOP = _env_int("TONOSAMA_MAX_PENDING_PER_LOOP", 20)
MAX_CANDIDATES = _env_int("TONOSAMA_MAX_CANDIDATES", 80)
SCHEDULER_INTERVAL_SEC = _env_int("TONOSAMA_SCHEDULER_INTERVAL_SEC", 15)
DISCORD_NOTIFY_ON_PENDING = _env_bool("TONOSAMA_DISCORD_NOTIFY_ON_PENDING", True)

# 5秒足確認は重いため、全銘柄ではなく1分足側の一次フィルタ通過後の上位だけに限定する。
MAX_5SEC_FEATURE_SYMBOLS = _env_int("TONOSAMA_MAX_5SEC_FEATURE_SYMBOLS", 30)
