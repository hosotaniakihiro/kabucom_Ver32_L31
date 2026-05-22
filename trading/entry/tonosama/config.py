# ============================================================
# File   : trading/entry/tonosama/config.py
# Version: Ver1.2-TONOSAMA-ENTRY-CONFIG-REALISTIC-PRICE-CHANGE
# ------------------------------------------------------------
# Ver1.2:
#   - 固定値を環境変数対応
#   - MIN_PRICE_CHANGE_PCT 既定値を 0.6% -> 0.03% に緩和
#   - MIN_FINAL_SCORE 既定値を 3.0 -> 2.0 に緩和
#   - 理由:
#       15:24〜15:25ログで volume_surge fail-open 後、
#       価格条件 close と volume_surge は通過したが、
#       price_change_low threshold=0.6 により全件落ち。
#       実データでは 4506=0.033%, 6779=0.119% 程度だったため、
#       0.6%は殿様15秒監視には厳しすぎる。
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


TONOSAMA_EXPIRE_SEC = _env_int("TONOSAMA_EXPIRE_SEC", 180)

MIN_PRICE = _env_float("TONOSAMA_MIN_PRICE", 200.0)

# pending作成直前の最終スコア。厳しすぎると候補は出ても登録されない。
MIN_FINAL_SCORE = _env_float("TONOSAMA_MIN_FINAL_SCORE", 2.0)

# raw score は 0 より大きければ候補として残す。
MIN_RAW_SCORE = _env_float("TONOSAMA_MIN_RAW_SCORE", 0.01)

# 履歴不足時は volume_surge.py 側で fail-open value=2.0 を入れるため、既定2.0のまま。
MIN_VOLUME_SURGE_RATIO = _env_float("TONOSAMA_MIN_VOLUME_SURGE_RATIO", 2.0)

# 以前の 0.6% は15秒/1〜5分スキャルピングでは厳しすぎる。
# 例: 6779=0.119856%, 4506=0.033113% が全落ちしていたため、既定0.03%。
MIN_PRICE_CHANGE_PCT = _env_float("TONOSAMA_MIN_PRICE_CHANGE_PCT", 0.03)

VOLUME_AVG_LOOKBACK_BARS = _env_int("TONOSAMA_VOLUME_AVG_LOOKBACK_BARS", 5)

USE_5SEC_CONFIRM = _env_bool("TONOSAMA_USE_5SEC_CONFIRM", True)
MIN_5SEC_PRICE_CHANGE_PCT = _env_float("TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT", 0.03)
MIN_5SEC_VOLUME_SURGE_RATIO = _env_float("TONOSAMA_MIN_5SEC_VOLUME_SURGE_RATIO", 1.5)
MAX_5SEC_DROP_PCT = _env_float("TONOSAMA_MAX_5SEC_DROP_PCT", -0.20)
REQUIRE_5SEC_BAR = _env_bool("TONOSAMA_REQUIRE_5SEC_BAR", False)

MAX_PENDING_PER_LOOP = _env_int("TONOSAMA_MAX_PENDING_PER_LOOP", 20)
MAX_CANDIDATES = _env_int("TONOSAMA_MAX_CANDIDATES", 80)
SCHEDULER_INTERVAL_SEC = _env_int("TONOSAMA_SCHEDULER_INTERVAL_SEC", 15)
DISCORD_NOTIFY_ON_PENDING = _env_bool("TONOSAMA_DISCORD_NOTIFY_ON_PENDING", True)

# 5秒足確認は重いため、全銘柄ではなく1分足側の一次フィルタ通過後の上位だけに限定する。
# 15秒ジョブが100秒以上詰まって previous_still_running になる事故を防ぐ。
MAX_5SEC_FEATURE_SYMBOLS = _env_int("TONOSAMA_MAX_5SEC_FEATURE_SYMBOLS", 30)
