# ============================================================
# File   : trading/entry/tonosama/config.py
# Version: Ver1.8-TONOSAMA-STRICT-BUT-5SEC-NOT-REQUIRED
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
#
# Ver1.6:
#   - 最新ログで candidates=1, raw/final=1.7198, min_final_score=2.0 のため
#     final_score_low で登録されない。
#   - TONOSAMA は既に 出来高/値幅/5秒足/AI fallback を通過した短期候補なので、
#     最終閾値の既定を 2.0 -> 1.5 に緩和する。
#   - これにより 6762 のような raw=1.7 台候補を pending に登録し、
#     entry_controller の板/リスク/発注前ガードへ進める。
#
# Ver1.7:
#   - 「もう少し絞りたい」対応。
#   - 1914 のような 5s=0.000% / slope=0.0000 / AI未接続の pending を抑止する。
#   - 既定値を厳格化:
#       MIN_PRICE                 200   -> 300
#       MIN_FINAL_SCORE           1.5   -> 2.5
#       MIN_VOLUME_SURGE_RATIO    2.0   -> 3.0
#       MIN_PRICE_CHANGE_PCT      0.03  -> 1.5
#       MIN_LATEST_VOLUME         3000  -> 50000
#       MIN_5SEC_PRICE_CHANGE_PCT 実質OFF -> 0.05
#   - main.py 側が TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT=0.01 を setdefault していても、
#     0.05 未満は 0.05 に引き上げる。
#
# Ver1.8:
#   - 5秒足は必須にしない。
#   - 5秒足が取れている場合だけ 5秒変化率 0.05%以上を要求する。
#   - 5秒足が無い場合は 3m/5m値幅・出来高急増・直近出来高・最終スコアで絞る。
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
    Ver1.8:
    5秒足は必須にしないが、取れている場合は 0.000% を通さない。
    main.py が 0.01 を setdefault していても、0.05% 未満は緩すぎるため
    既定では 0.05% に引き上げる。

    5秒確認自体を無効化したい場合だけ TONOSAMA_USE_5SEC_CONFIRM=0 を使う。
    """
    v = _env_float("TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT", 0.05)
    if v < 0.05:
        return 0.05
    return float(v)


TONOSAMA_EXPIRE_SEC = _env_int("TONOSAMA_EXPIRE_SEC", 180)

# 低位・板が薄い銘柄を減らす。
MIN_PRICE = _env_float("TONOSAMA_MIN_PRICE", 300.0)

# pending作成直前の最終スコア。
# Ver1.7以降: AI未接続・5秒停止の候補を減らすため、既定を厳格化。
MIN_FINAL_SCORE = _env_float("TONOSAMA_MIN_FINAL_SCORE", 2.5)

# raw score は 0 より大きければ候補として残す。
MIN_RAW_SCORE = _env_float("TONOSAMA_MIN_RAW_SCORE", 0.01)

# 履歴不足時の fail-open 2.0 では緩すぎるため、殿様は3倍以上を既定にする。
MIN_VOLUME_SURGE_RATIO = _env_float("TONOSAMA_MIN_VOLUME_SURGE_RATIO", 3.0)

# 3m/5m の一瞬の微小変化ではなく、明確な急騰だけを拾う。
MIN_PRICE_CHANGE_PCT = _env_float("TONOSAMA_MIN_PRICE_CHANGE_PCT", 1.5)

# body は open==close の足で 0 になりやすいため、既定では強制しない。
# 動いているかどうかは intrabar range と latest volume と、取れている場合の5秒変化で判定する。
MIN_BODY_CHANGE_PCT = _env_float("TONOSAMA_MIN_BODY_CHANGE_PCT", 0.0)
MIN_INTRABAR_RANGE_PCT = _env_float("TONOSAMA_MIN_INTRABAR_RANGE_PCT", 0.10)

# 直近出来高が少ない銘柄のアラートを抑止する。
MIN_LATEST_VOLUME = _env_float("TONOSAMA_MIN_LATEST_VOLUME", 50000.0)

VOLUME_AVG_LOOKBACK_BARS = _env_int("TONOSAMA_VOLUME_AVG_LOOKBACK_BARS", 5)

USE_5SEC_CONFIRM = _env_bool("TONOSAMA_USE_5SEC_CONFIRM", True)
MIN_5SEC_PRICE_CHANGE_PCT = _tonosama_5sec_threshold()
MIN_5SEC_VOLUME_SURGE_RATIO = _env_float("TONOSAMA_MIN_5SEC_VOLUME_SURGE_RATIO", 1.5)
MAX_5SEC_DROP_PCT = _env_float("TONOSAMA_MAX_5SEC_DROP_PCT", -0.20)

# 5秒足は必須にしない。取れている場合だけ runner.py 側で 0.05%以上を要求する。
REQUIRE_5SEC_BAR = _env_bool("TONOSAMA_REQUIRE_5SEC_BAR", False)

MAX_PENDING_PER_LOOP = _env_int("TONOSAMA_MAX_PENDING_PER_LOOP", 10)
MAX_CANDIDATES = _env_int("TONOSAMA_MAX_CANDIDATES", 40)
SCHEDULER_INTERVAL_SEC = _env_int("TONOSAMA_SCHEDULER_INTERVAL_SEC", 15)
DISCORD_NOTIFY_ON_PENDING = _env_bool("TONOSAMA_DISCORD_NOTIFY_ON_PENDING", True)

# 5秒足確認は重いため、全銘柄ではなく1分足側の一次フィルタ通過後の上位だけに限定する。
MAX_5SEC_FEATURE_SYMBOLS = _env_int("TONOSAMA_MAX_5SEC_FEATURE_SYMBOLS", 20)
