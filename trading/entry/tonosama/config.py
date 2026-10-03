# ============================================================
# File   : trading/entry/tonosama/config.py
# Version: Ver2.12-NATIVE-ALERT-LIQ-AND-RUNTIME-BUDGET
# ------------------------------------------------------------
# 方針:
#   - volume_surge.py / history guard で履歴不足fail-openを許可した後、
#     runner.py 側の本体条件が強すぎて candidates=0 に戻る問題を緩和。
#   - Ver2.9:
#     PENDING/Discordアラート直前にも出来高・売買代金ガードを入れ、
#     低出来高銘柄の通知を止める。
#   - Ver2.10:
#     config.py 初期化途中に pending_writer をimportして循環importになる問題を修正。
#     アラート流動性ガードは全定数定義後にinstallする。
#   - Ver2.11:
#     TONOSAMAの古いdaemon threadが100秒超走り続ける問題を防ぐため、
#     runtime budget patch を遅延installする。
#   - Ver2.12:
#     アラート流動性ガード (旧 core/startup/tonosama_alert_liquidity_guard_patch.py) を
#     pending_writer.add_tonosama_pending() へ native化。
#     runtime budget guard (旧 core/startup/tonosama_runtime_budget_patch.py) を
#     runner.build_tonosama_entries() へ native化。
#     どちらも起動時モンキーパッチではなくなったため、ここでの install 呼び出しは不要。
#     (日中流動性ガード tonosama_intraday_liquidity_patch は、実運用の import 順序では
#     install() が volume_surge/runner 未ロード時点で一度だけ呼ばれ、リトライ機構が
#     無いため実際にはフィルタが一度も適用されない = 現状ノーオペであることを確認済み。
#     native化すると挙動が変わってしまうため、今回は意図的に手を付けていない。)
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
os.environ.setdefault("TONOSAMA_MIN_FINAL_SCORE", "2.3")
os.environ.setdefault("TONOSAMA_MIN_PRICE_CHANGE_PCT", "0.20")
os.environ.setdefault("TONOSAMA_MIN_SLOPE", "0.0010")
os.environ.setdefault("TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT", "0.01")
os.environ.setdefault("TONOSAMA_MAX_BUY_PRICE_CHANGE_PCT", "1.20")
os.environ.setdefault("TONOSAMA_MAX_SELL_PRICE_DROP_PCT", "1.20")

# Runtime budget guard。timeout wrapperではPythonスレッドを強制停止できないため、
# runner本体がこの時間予算を見て自分で早期終了する。
os.environ.setdefault("TONOSAMA_LOOP_TIME_BUDGET_SEC", "10")
os.environ.setdefault("TONOSAMA_RUNTIME_MAX_EVAL_CANDIDATES", "5")

# 日中流動性ガード。後からイナゴが付いてきやすい銘柄へ寄せる。
os.environ.setdefault("TONOSAMA_INTRADAY_LIQUIDITY_GUARD", "1")
os.environ.setdefault("TONOSAMA_MIN_DAY_VOLUME", "100000")
os.environ.setdefault("TONOSAMA_MIN_DAY_TURNOVER", "100000000")
os.environ.setdefault("TONOSAMA_MIN_ACTIVE_VOLUME_MINUTES", "8")
os.environ.setdefault("TONOSAMA_MIN_RECENT_3M_VOLUME", "50000")
os.environ.setdefault("TONOSAMA_MIN_RECENT_5M_VOLUME", "80000")

# PENDING / Discordアラート直前の最終流動性ガード。
# 発注最終ガードとは別に、通知だけ先に出てしまう問題を防ぐ。
os.environ.setdefault("TONOSAMA_ALERT_LIQ_GUARD", "1")
os.environ.setdefault("TONOSAMA_ALERT_REJECT_VOLUME_MISSING", "1")
os.environ.setdefault("TONOSAMA_ALERT_MIN_LATEST_VOLUME", "30000")
os.environ.setdefault("TONOSAMA_ALERT_MIN_RECENT_BAR_VOLUME", "30000")
os.environ.setdefault("TONOSAMA_ALERT_MIN_TURNOVER", "10000000")


# 日中流動性ガード (旧 core/startup/tonosama_intraday_liquidity_patch.py) は
# ここから install していたが、フィルタが一度も適用されない完全なノーオペ
# だったことを再検証したうえでパッチごと削除した。
#
#   - install() の呼び出しはこの 1 箇所だけで、リトライ機構が無い
#   - この行が走る時点で config は初期化途中であり、
#     VOLUME_AVG_LOOKBACK_BARS (下の 151 行付近) がまだ未定義。
#     そのため _try_patch_volume_surge() は volume_surge を import せず False を返す
#   - volume_surge は config を import する側なので、config より先に
#     読み込まれることが無い。runner も同様に初期化途中で
#     build_scalping_feature_df をまだ持たないため、
#     _install_runner_lazy_hook() も hook を張れずに _DEFERRED=True で終わる
#
# 本文化するとこの「動いていなかったフィルタ」が有効になり、
# TONOSAMA のエントリー候補が実際に絞られてしまう。挙動を変えないため
# フィルタ本体は移植していない。
# 有効化したい場合は volume_surge.build_scalping_feature_df へ
# 明示的に組み込むこと (env TONOSAMA_INTRADAY_LIQUIDITY_GUARD)。


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
MIN_FINAL_SCORE = _env_float_floor("TONOSAMA_MIN_FINAL_SCORE", 2.3, 2.3)
MIN_RAW_SCORE = _env_float("TONOSAMA_MIN_RAW_SCORE", 0.01)

# 2026-10-03: 3 倍 -> 10 倍。backtest.db 24 日で 3 倍以上は 15 分後 +0.03% (効果なし)、
#   10 倍以上は +0.33% / 60 分 +0.45% (前半 +0.20 / 後半 +0.55)。ユーザー承認。
MIN_VOLUME_SURGE_RATIO = _env_float_floor("TONOSAMA_MIN_VOLUME_SURGE_RATIO", 10.0, 3.0)

# Ver2.6: 0.30%では候補0が続いたため、0.20%へ緩和。
MIN_PRICE_CHANGE_PCT = _env_float_floor("TONOSAMA_MIN_PRICE_CHANGE_PCT", 0.20, 0.20)

# Ver2.6: slope=0.0030では短期履歴不足時に厳しいため、0.0010へ緩和。
MIN_SLOPE = _env_float_floor("TONOSAMA_MIN_SLOPE", 0.0010, 0.0010)

MIN_BODY_CHANGE_PCT = _env_float("TONOSAMA_MIN_BODY_CHANGE_PCT", 0.0)
MIN_INTRABAR_RANGE_PCT = _env_float_floor("TONOSAMA_MIN_INTRABAR_RANGE_PCT", 0.10, 0.10)

# ------------------------------------------------------------
# 流動性の下限 (TONOSAMA 本体フィルタ)
# ------------------------------------------------------------
# 2026-09-01 のログでは実効値が MIN_LATEST_VOLUME=50000 になっており、
# base 39 行が 1 行残らず latest_volume_low で落ちていた。
#
# 原因は _env_float_floor(..., 50000.0, 50000.0) の **第3引数 (floor)**。
# ENV / settings.ini で 10000 を指定しても max(50000, 10000) = 50000 になり、
# 設定値が一切効かない「起動時上書き」になっていた。
#
# 想定設定 (運用側の意図):
#     最小出来高     10,000 株
#     最小売買代金  3,000,000 円
# floor は「設定ミスで 0 にしない」ための下限なので、
# 想定値そのものを floor にする (= 想定値より緩くはできないが、
# ENV でより厳しくはできる)。
#
# _latest_volume の意味:
#     **最新の確定 1 分足の出来高** (runner._ensure_actual_movement_cols_core が
#     summary base の volume / volume_1m / latest_volume 列から取る)。
#     当日累計出来高でも PUSH の最終 tick 出来高でもない。
#     当日累計は TONOSAMA_MIN_DAY_VOLUME (別名) が担当する。
# _latest_turnover の意味:
#     **最新の確定 1 分足の売買代金** = _latest_volume x close。
#     turnover 列がある場合はそれを優先する。
MIN_LATEST_VOLUME = _env_float_floor("TONOSAMA_MIN_LATEST_VOLUME", 10000.0, 10000.0)
MIN_LATEST_TURNOVER = _env_float_floor("TONOSAMA_MIN_LATEST_TURNOVER", 20_000_000.0, 3_000_000.0)
# 2026-10-03 (ユーザー判断): 直近 1 本の出来高は「株数」でなく「売買代金」で見る。
#   株数だと 1 万円の銘柄で 3 万株 = 3 億円、100 円の銘柄なら 300 万円で通り、値がさ株がまず通らない
#   (10/1〜10/2 は殿様イナゴ候補の 118/121 件が株数 3 万株未満で落ちていた)。
#   既定は株数の下限を使わず MIN_LATEST_TURNOVER (直近 1 本の売買代金、既定 2,000 万円) だけ。
#   従来の株数の下限に戻す: TONOSAMA_LATEST_VOLUME_SHARE_GUARD=1
LATEST_VOLUME_SHARE_GUARD = str(os.getenv("TONOSAMA_LATEST_VOLUME_SHARE_GUARD", "0")).strip().lower() in ("1", "true", "yes", "on")


def latest_value_ok(volume, price) -> bool:
    """直近 1 本の出来高の下限判定 (既定は売買代金 = 出来高 x 株価 >= MIN_LATEST_TURNOVER)。"""
    try:
        v, p = float(volume or 0.0), float(price or 0.0)
    except Exception:
        return False
    if LATEST_VOLUME_SHARE_GUARD:
        return v >= MIN_LATEST_VOLUME
    if p <= 0:
        return v > 0
    return v * p >= MIN_LATEST_TURNOVER

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


# ============================================================
# 実効設定の 1 回だけのログ
# ------------------------------------------------------------
# 2026-09-01: 「設定は 10000 のはずなのに実効値が 50000」だった件を
# 二度と目視で追わなくて済むようにする。閾値の意味 (どの出来高か) も
# 一緒に出す。呼び出しは runner の初回ループから。
# ============================================================

import logging as _logging

_logger = _logging.getLogger(__name__)
_EFFECTIVE_LOGGED = False


def effective_liquidity_config() -> dict:
    """TONOSAMA 本体フィルタの実効流動性設定 (テストからも読む)。"""
    return {
        "MIN_LATEST_VOLUME": MIN_LATEST_VOLUME,
        "MIN_LATEST_TURNOVER": MIN_LATEST_TURNOVER,
        "MIN_PRICE": MIN_PRICE,
        "_latest_volume_meaning": "latest_completed_1min_bar_volume",
        "_latest_turnover_meaning": "latest_completed_1min_bar_turnover(volume x close)",
        "env_TONOSAMA_MIN_LATEST_VOLUME": os.getenv("TONOSAMA_MIN_LATEST_VOLUME"),
        "env_TONOSAMA_MIN_LATEST_TURNOVER": os.getenv("TONOSAMA_MIN_LATEST_TURNOVER"),
    }


def log_effective_config_once() -> dict:
    """起動時に 1 回だけ実効値を出す。"""
    global _EFFECTIVE_LOGGED
    cfg = effective_liquidity_config()
    if _EFFECTIVE_LOGGED:
        return cfg
    _EFFECTIVE_LOGGED = True
    _logger.warning(
        "[TONOSAMA CONFIG] 実効流動性設定 %s "
        "(_latest_volume = 最新の確定1分足の出来高。当日累計でも PUSH 最終tickでもない。"
        " _latest_turnover = その1分足の売買代金。"
        " 値の持ち主は trading/entry/tonosama/config.py だけ)",
        cfg,
    )
    return cfg


def reset_effective_log_for_test() -> None:
    global _EFFECTIVE_LOGGED
    _EFFECTIVE_LOGGED = False
