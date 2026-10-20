# -*- coding: utf-8 -*-
"""
Centralized runtime environment defaults for startup customization.

This module is intentionally conservative:
- It only fills missing environment variables by default.
- Existing operator/task-scheduler settings always win.
- sitecustomize.py / usercustomize.py can import this module safely.

The goal is to gradually move scattered os.environ.setdefault(...) calls
from sitecustomize.py, usercustomize.py, and compatibility patches into one
small, auditable place.
"""
from __future__ import annotations

import os
from typing import Dict, Mapping, MutableMapping, Optional

VERSION = "REV7-RUNTIME-ENV-DEFAULTS-COMPAT-WRAPPERS-SUMMARY-PRICE-CAP"

_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n", ""}


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    val = str(raw).strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    return bool(default)


def _set_defaults(defaults: Mapping[str, str], *, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    env = environ if environ is not None else os.environ
    applied: Dict[str, str] = {}
    for key, value in defaults.items():
        if key not in env:
            env[key] = str(value)
            applied[key] = str(value)
    return applied


def _merge_applied(*items: Dict[str, str]) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for item in items:
        merged.update(item or {})
    return merged


PUSH_DEFAULTS: Dict[str, str] = {
    "PUSH_WS_VENDOR_RUN_FOREVER": "1",
    "PUSH_WS_ENABLE_PING": "0",
    "PUSH_ROTATION_CLOSE_BEFORE_REGISTER": "0",
    "PUSH_ROTATION_CLOSE_WS_BEFORE_REGISTER": "0",
    # 2026-09-02 の reconnect storm (consecutive=170) の原因。
    #
    # rotation_A / rotation_B の登録は **REST の PUT /kabusapi/register** で行う。
    # WebSocket を使わないので、WS が切れている間でも実行できる
    # (transport.py REV1.6 のコメントがまさにそう書いている)。
    # ところがここが "0" だと transport._allow_refresh_with_ws_closed() が False になり、
    #   [push_stream] refresh skipped reason=rotation_A ws_not_ready
    # で登録そのものが行われない。
    #
    # PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH=1 により on_open 側でも登録しないため、
    # **銘柄登録を行う経路が 1 つも残らない**。実測 (09:14:30 起動):
    #   09:15:10 rotation A register failed (ws_not_ready) failure_count=1 -> backoff 180s
    #   以降 600s backoff で 10 分に 1 回だけ再試行し、毎回同じ理由で失敗
    #   その間 WS は 3〜7 秒で切られ続け generation=171 まで到達
    # 登録が無い -> kabu Station が PUSH を切る -> 切れているから登録できない、
    # という相互ロックになっていた。
    #
    # REST 登録は WS の状態に依存しないので "1" が本来の設計値。
    "PUSH_ROTATION_REGISTER_WITH_WS_CLOSED": "1",
    "PUSH_ROTATION_WAIT_WS_READY_AFTER_REGISTER": "1",
    "PUSH_ROTATION_HOLD_SEC": "4.8",
    "PUSH_ROTATION_CLEAR_GAP_SEC": "0.2",
    "PUSH_ROTATION_WS_CLOSE_SETTLE_SEC": "0.15",
    "PUSH_ROTATION_UNREGISTER_WAIT_SEC": "0.2",
    "PUSH_ROTATION_POST_REGISTER_WS_READY_TIMEOUT_SEC": "4.0",
    "PUSH_ROTATION_POST_REGISTER_WS_POLL_SEC": "0.05",
    "PUSH_ROTATION_POST_REGISTER_WS_SETTLE_SEC": "0.25",
    "PUSH_ROTATION_FAILURE_BACKOFF_SEC": "180",
    "PUSH_ROTATION_FAILURE_BACKOFF_MAX_SEC": "600",
    "PUSH_ROTATION_FAILURE_BACKOFF_MULTIPLIER": "1.5",
    "PUSH_STREAM_SKIP_AFTER_OPEN_REFRESH": "1",
    "PUSH_STREAM_ONOPEN_REFRESH_CLEAR_FIRST": "0",
    "PUSH_STREAM_ONOPEN_REFRESH_UNREGISTER_FIRST": "0",
    "PUSH_STREAM_ONOPEN_REFRESH_WAIT_AFTER_CLEAR": "0.0",
    "PUSH_STREAM_AFTER_OPEN_REFRESH_DELAY_SEC": "0.50",
    "PUSH_STREAM_ONOPEN_WS_READY_TIMEOUT_SEC": "1.0",
    "PUSH_STREAM_PAUSE_RECONNECT_DURING_REGISTER": "1",
    "PUSH_STREAM_RECONNECT_BACKOFF_BASE_SEC": "0.3",
    # 即死が続くときの上限。1.0 だと 300 回連続でも 1 秒間隔で撃ち続けるため 15 秒まで許す。
    "PUSH_STREAM_RECONNECT_BACKOFF_MAX_SEC": "15.0",
    "PUSH_STREAM_RECONNECT_BACKOFF_LADDER_SEC": "1,2,4,8,15",
    "PUSH_STREAM_RECONNECT_STABLE_RESET_SEC": "5.0",
    "PUSH_STREAM_SHORT_LIVED_SEC": "1.0",
    "PUSH_STREAM_SHORT_LIVED_EXTRA_COOLDOWN_SEC": "0.0",
    "PUSH_STREAM_SINGLE_OWNER_LOCK": "1",
    # kabu Station の PUSH は 1 セッション 1 接続。OS レベルの single-instance lock で担保する。
    "KABU_PUSH_WS_OWNER_LOCK": "1",
    "KABU_REGISTER_REQUIRE_WS_OWNER": "1",
    "PUSH_ROTATION_REQUIRE_WS_OWNER": "1",
    "ATS_REGISTER_REQUIRE_WS_OWNER": "1",
    "PUSH_STREAM_SINGLE_OWNER_WAIT_RETRY": "1",
    "PUSH_STREAM_SINGLE_OWNER_RETRY_SEC": "1.0",
    "PUSH_STREAM_SINGLE_OWNER_LOG_EVERY_SEC": "20.0",
    "PUSH_STREAM_EMPTY_OWNER_LOCK_FAIL_OPEN": "1",
    "PUSH_STREAM_EMPTY_OWNER_LOCK_REQUIRE_OWNER_CONTEXT": "1",
    "ACTIVE_PREMARKET_ALLOW_NO_PRICE": "1",
    "PUSH_REGISTER_LIQUIDITY_ROTATION_PRESERVE_FULL_POOL": "0",
    "PUSH_REGISTER_LIQUIDITY_ROTATION_MIN_SURVIVOR_COUNT": "50",
    "PUSH_REGISTER_LIQUIDITY_ROTATION_MIN_SURVIVOR_RATIO": "0.50",
    "USERCUSTOMIZE_ENABLE_LEGACY_PUSH_PATCHES": "0",
}

RESCUE_DEFAULTS: Dict[str, str] = {
    "SITECUSTOMIZE_ENABLE_RESCUE_PATCHES": "0",
    "SITECUSTOMIZE_ENABLE_ENTRY_FAILOPEN_PATCHES": "0",
    "SITECUSTOMIZE_ENABLE_RANKING_FINAL_RESCUE_PATCH": "0",
    "SITECUSTOMIZE_ENABLE_TONOSAMA_EXTRA_RESCUE_PATCHES": "0",
    "SITECUSTOMIZE_ENABLE_SUMMARY_AI_RESCUE_PATCHES": "0",
    "USERCUSTOMIZE_ENABLE_RANKING_RESCUE_PATCHES": "0",
    "USERCUSTOMIZE_ENABLE_LEGACY_RANKING_FAILOPEN_PATCHES": "0",
    "USERCUSTOMIZE_ENABLE_TONOSAMA_RESCUE_PATCHES": "0",
    "USERCUSTOMIZE_ENABLE_LEGACY_TONOSAMA_FAILOPEN_PATCHES": "0",
    "RANKING_AI_GATE_FAILOPEN_ENABLED": "0",
    "RANKING_FINAL_RESCUE_AI_FAILOPEN": "0",
    "ENTRY_DIRECTION_RECURSION_FAILOPEN_ENABLED": "0",
    "TONOSAMA_FAILOPEN_DIRECTION_RESCUE": "0",
    "TONOSAMA_ATR1M_FILTER_RESCUE": "0",
    "TONOSAMA_RANGE5M_FILTER_RESCUE": "0",
    "TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING": "0",
    "TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY": "0",
    "TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY": "0",
    "TONOSAMA_ALLOW_HISTORY_MISSING_STRONG_MOVE": "0",
    "TONOSAMA_DROP_HISTORY_MISSING_ENTRY": "1",
    "LOW_MOVE_TONOSAMA_ALLOW_NO_HIGHLOW_FALLBACK": "0",
}

DB_DEFAULTS: Dict[str, str] = {
    "SQLITE_MEMORY_PRAGMAS_ENABLED": "1",
    "SQLITE_MEMORY_TEMP_STORE": "MEMORY",
    "SQLITE_MEMORY_CACHE_KB": "-65536",
    "SQLITE_BUSY_TIMEOUT_MS": "5000",
    "SQLITE_MMAP_SIZE_BYTES": "268435456",
    "SQLITE_CACHE_SPILL_OFF": "1",
    "SQLITE_WAL_AUTOCHECKPOINT": "1000",
    "SQLITE_SYNCHRONOUS": "NORMAL",
    # 旧 core/startup/summary_db_lock_pressure_patch.py の install() が
    # setdefault していた値。upsert_executor が import 時に読むため、
    # sitecustomize から最も早く適用されるここで確定させる。
    # 同パッチは SQLITE_BUSY_TIMEOUT_MS も 45000 で setdefault していたが、
    # apply_site_defaults がこの dict を先に適用するため上の 5000 が勝っており
    # 実際には不発だった。挙動を変えないためここには入れない。
    "SUMMARY_REALTIME_FORCE_LATEST_ONLY": "1",
    "SUMMARY_REALTIME_SKIP_IF_BUSY": "1",
    "SUMMARY_REALTIME_LOCK_TIMEOUT_SEC": "2.0",
    "SUMMARY_REALTIME_LATEST_ONLY_ROW_THRESHOLD": "500",
    "SUMMARY_UPSERT_WAL_CHECKPOINT": "0",
    "SUMMARY_UPSERT_CHUNK_SIZE": "250",
    "SUMMARY_UPSERT_RETRY": "6",
    "SUMMARY_UPSERT_LOCK_BACKOFF_MAX_SEC": "1.2",
}

HELPER_DEFAULTS: Dict[str, str] = {
    "SITECUSTOMIZE_ENABLE_FULL_NONMAIN": "0",
}

MAIN_RESTORE_DEFAULTS: Dict[str, str] = {
    "AUTOSTOCK_MAIN_OPERATION_MODE": "full",
    "AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS": "0",
    "AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP": "0",
    "AUTOSTOCK_MAIN_SKIP_RANKING_ENTRY": "0",
    "AUTOSTOCK_MAIN_SKIP_TONOSAMA_ENTRY": "0",
    "AUTOSTOCK_MAIN_SKIP_SUMMARY_PUSH_BG": "0",
    "AUTOSTOCK_MAIN_SKIP_RANKING_SUMMARY_SCHEDULE": "0",
    "AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK": "0",
    "AUTOSTOCK_MAIN_SKIP_EXIT_LOOP_WHEN_BROKER_EMPTY": "0",
    "AUTOSTOCK_MAIN_ENABLE_EXIT_LOOP": "1",
    "AUTOSTOCK_MAIN_ENABLE_RANKING_ENTRY": "1",
    "AUTOSTOCK_MAIN_ENABLE_TONOSAMA_ENTRY": "1",
    "AUTOSTOCK_MAIN_ENABLE_SUMMARY_AI_ENTRY": "1",
    "AUTOSTOCK_MAIN_ENABLE_SUMMARY_PARENT_TICK": "1",
    "AUTOSTOCK_MAIN_ENABLE_RANKING_SUMMARY_SCHEDULE": "1",
    "FORCE_ENABLE_MAIN_SUMMARY_PARENT_TICK": "1",
}

RANKING_ENTRY_DEFAULTS: Dict[str, str] = {
    "RANKING_ENTRY_WATCHDOG_ENABLED": "1",
    "RANKING_ENTRY_WATCHDOG_TIMEOUT_SEC": "55",
    "RANKING_ENTRY_HARD_TIMEOUT_ENABLED": "1",
    "RANKING_ENTRY_HARD_TIMEOUT_SEC": "28",
    "RANKING_ENTRY_FAST_RUNTIME_BUDGET_SEC": "15",
    "RANKING_ENTRY_FAST_BUILD_TIMEOUT_SEC": "18",
    "RANKING_ENTRY_FAST_CONTROLLER_TIMEOUT_SEC": "12",
    "RANKING_ENTRY_SNAPSHOT_TECH_ALIAS_ENABLED": "1",
    "RANKING_ENTRY_SOURCE_DB_FALLBACK_ENABLED": "1",
    "RANKING_ENTRY_HIGH_LOW_SNAPSHOT_PATCH_ENABLED": "1",
    "RANKING_ENTRY_HIGH_LOW_SNAPSHOT_LOOKBACK_ROWS": "12",
    "RANKING_ENTRY_HIGH_LOW_SNAPSHOT_MAX_AGE_MIN": "30",
    "RANKING_STUCK_PENDING_MAX_CONTROLLER_RETRY": "2",
    "RANKING_STUCK_PENDING_MAX_AGE_SEC": "120",
    "RANKING_TODAY_EMPTY_FAIL_CLOSED": "1",
    "RANKING_ENTRY_STALE_FAIL_CLOSED": "1",
    "RANKING_ENTRY_STALE_FAILOPEN_ENABLED": "0",
    "RANKING_ENTRY_ABORT_ON_STALE": "1",
    "RANKING_ENTRY_CLEAR_PENDING_ON_STALE": "1",
    "RANKING_ENTRY_REQUIRE_TODAY": "1",
    "RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC": "300",
    "RANKING_PRECHECK_MAX_AGE_SEC": "300",
    "RANKING_SNAPSHOT_MAX_AGE_SEC": "300",
    "RANKING_ENTRY_ALLOW_STALE_FALLBACK": "0",
    "RANKING_ENTRY_RAW_FALLBACK_ONLY_TODAY": "1",
    "RANKING_PRECHECK_PENDING_FAILOPEN_ENABLED": "0",
    "RANKING_SNAPSHOT_TECH_BRIDGE_ENABLED": "1",
    "RANKING_AI_GATE_FAILOPEN_MIN_SCORE": "50",
    "RANKING_AI_GATE_FAILOPEN_MIN_TURNOVER": "30000000",
    "RANKING_AI_GATE_FAILOPEN_MIN_VOLUME": "30000",
    "RANKING_ENTRY_LIGHT_MIN_SCORE": "50",
    "RANKING_ENTRY_LIGHT_MIN_TURNOVER": "30000000",
    "RANKING_FINAL_RESCUE_MIN_SCORE": "50",
    "RANKING_FINAL_RESCUE_MIN_TURNOVER": "30000000",
    "RANKING_FINAL_RESCUE_ATR_MIN_RATIO": "0.0005",
    "LOW_MOVE_RANKING_MIN_ENTRY_PRICE": "150",  # 2026-10-20: 300 -> 150 (ユーザー判断)
    "LOW_MOVE_RANKING_MAX_ENTRY_PRICE": "7000",
    "LOW_MOVE_RANKING_MIN_RANGE_PCT_LOW_PRICE": "0.008",
    "LOW_MOVE_RANKING_MIN_RANGE_PCT_HIGH_PRICE": "0.006",
    "LOW_MOVE_RANKING_STRONG_RANGE_PCT": "0.014",
    "LOW_MOVE_RANKING_MIN_ABS_SLOPE": "0.0000",
    "LOW_MOVE_RANKING_ZERO_ATR_MIN_SCORE": "50",
    "LOW_MOVE_RANKING_MIN_SCORE_FOR_NO_HIGHLOW": "50",
    "LOW_MOVE_RANKING_ZERO_ATR_MIN_TURNOVER": "30000000",
    "ENTRY_RANKING_SCALP_MIN_PRICE": "1500",
    "ENTRY_RANKING_SCALP_MAX_PRICE": "7000",
    "ENTRY_RANKING_SCALP_MIN_MTF": "0.5",
    "ENTRY_RANKING_SCALP_ALLOW_ZERO_MTF_RESCUE": "0",
    "ENTRY_RANKING_SCALP_AI_FALLBACK_ANY_NG": "0",
    "ENTRY_RANKING_SCALP_RANGE_NO_HIGHLOW_FAILOPEN": "0",
    "ENTRY_RANKING_SCALP_RANGE_ERROR_FAILOPEN": "0",
    "USERCUSTOMIZE_ALLOW_DUPLICATE_PATCHES": "0",
}

TONOSAMA_DEFAULTS: Dict[str, str] = {
    "TONOSAMA_RAW1_RESAMPLE_FALLBACK": "1",
    "TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE": "3.0",
    "TONOSAMA_5SEC_ADVISORY_ENABLED": "1",
    "TONOSAMA_5SEC_ALLOW_ZERO_IF_PRIMARY_PASS": "0",
    "TONOSAMA_AI_FALLBACK_REJECT_ZERO_5SEC": "1",
    "TONOSAMA_AI_FALLBACK_MIN_5SEC_CHANGE_PCT": "0.0",
    "TONOSAMA_ALLOW_WARNING_ONLY_CLIMAX": "1",
    "TONOSAMA_WARNING_ONLY_MAX_PRICE_CHANGE_PCT": "0.50",
    "TONOSAMA_PRICE_CHANGE_OR_RANGE_ENABLED": "0",
    "TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_RANGE_PCT": "3.0",
    "TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_VOLUME": "50000",
    "TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_SURGE": "3.0",
    "TONOSAMA_ENTRY_TIMEOUT_SEC": "45",
    "TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC": "12",
    "TONOSAMA_DISPATCH_CONTROLLER_ON_TIMEOUT_PENDING": "1",
    "TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC": "10",
    "TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC": "60",
    "ENTRY_CONTROLLER_TONOSAMA_AI_BRIDGE": "1",
    "ENTRY_CONTROLLER_TONOSAMA_MIN_PENDING_SCORE": "1.0",
    "TONOSAMA_ORDER_MAX_AGE_SEC": "240",
    "FINAL_ENTRY_TONOSAMA_LIQUIDITY_FALLBACK": "1",
    "FINAL_ENTRY_TONOSAMA_MIN_VOLUME": "30000",
    "FINAL_ENTRY_TONOSAMA_MIN_TURNOVER": "10000000",
    "TONOSAMA_STALE_SUMMARY_FAILOPEN": "0",
    "TONOSAMA_RECENT_3M5M_FAILOPEN": "0",
    "TONOSAMA_ALLOW_STALE_MTF_ENTRY": "0",
    "TONOSAMA_MTF_STALE_FAIL_CLOSED": "1",
    "TONOSAMA_SUMMARY_MAX_AGE_SEC": "300",
    "TONOSAMA_HISTORY_MAX_AGE_SEC": "300",
    "SUMMARY_MTF_ENTRY_MAX_AGE_SEC": "300",
}

ENTRY_DEFAULTS: Dict[str, str] = {
    "ENTRY_CONTROLLER_LOCK_WAIT_ENABLED": "1",
    "ENTRY_CONTROLLER_LOCK_WAIT_SOURCES": "RANKING,TONOSAMA,SUMMARY",
    "ENTRY_CONTROLLER_LOCK_WAIT_SEC": "75",
    "ENTRY_CONTROLLER_SUMMARY_LOCK_WAIT_SEC": "75",
    "ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED": "1",
    "ENTRY_CONTROLLER_RANKING_LOCK_WAIT_SEC": "75",
    "ENTRY_CONTROLLER_LOCK_WAIT_TIMEOUT_SKIP_ORIGINAL": "1",
    "ENTRY_CONTROLLER_SOURCE_PREFILTER_ENABLED": "1",
    "ENTRY_SHORT_MTF_REQUIRED": "1",
    "ENTRY_SHORT_MTF_FORCE_2OF3": "1",
    "ENTRY_SHORT_MTF_MIN_ALIGNED": "2",
    "ENTRY_SHORT_MTF_MIN_AVAILABLE": "2",
    "ENTRY_SHORT_MTF_SLOPE_EPS": "0.0",
    "ENTRY_DAILY_MTF_OPTIONAL": "1",
    "ENTRY_SHORT_MTF_DB_BACKFILL": "1",
    "ENTRY_SHORT_MTF_ZERO_NEUTRAL": "1",
    "ENTRY_ORDER_EXCHANGE": "9",
    "KABU_ORDER_EXCHANGE": "9",
    "SUMMARY_AI_PRE_FILTER_DAILY_RISK": "0",
    "SUMMARY_AI_DISABLE_SYMBOL_STOP_AFTER_FIRST_LOSS": "1",
    "DAILY_RISK_SYMBOL_STOP_AFTER_FIRST_LOSS": "0",
    "DAILY_RISK_STOP_AFTER_FIRST_LOSS": "0",
    "SYMBOL_STOP_AFTER_FIRST_LOSS_ENABLED": "0",
    "WATCHLIST_RECENT_LIQ_ENABLED": "1",
    "WATCHLIST_RECENT_LIQ_BULK_RUN_IN_MAIN": "1",
    "WATCHLIST_RECENT_LIQ_BULK_SKIP_DB_IN_MAIN": "0",
    "WATCHLIST_RECENT_LIQ_FAIL_OPEN_ON_TIMEOUT": "0",
    "WATCHLIST_RECENT_LIQ_BULK_TIMEOUT_SEC": "0.75",
    "WATCHLIST_RECENT_LIQ_BULK_SQL_HARD_TIMEOUT_SEC": "1.0",
    "WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME": "3000",
    "WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME": "3000",
    "WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN": "1000000",
    "ACTIVE_PROTECT_PENDING_SYMBOLS": "1",
    "ACTIVE_PROTECT_EXIT_COOLDOWN_SYMBOLS": "1",
    "ACTIVE_EXIT_COOLDOWN_PROTECT_SEC": "60",
    "ACTIVE_PROTECT_BOARD_RETRY_SYMBOLS": "1",
    "ACTIVE_PROTECT_HOT_SYMBOLS": "1",
    "ENTRY_ALLOW_WITHOUT_BOARD_MIN_VOLUME": "30000",
    "ENTRY_ALLOW_WITHOUT_BOARD_MIN_TURNOVER": "10000000",
    "ENTRY_ALLOW_WITHOUT_BOARD_MIN_PRICE": "150",  # 2026-10-20: 200 -> 150 (ユーザー判断)
    "ENTRY_ALLOW_WITHOUT_BOARD_MIN_SCORE": "0.90",
    # 2026-09-15: 1 分足の方向確認ガード (close vs ma5/ma25 の構造・乖離) は
    #   3 分/5 分足の MA/RSI/MACD ゲート (trading/filters/mtf_ma_gate) と役割が重複し、
    #   ゲート通過後に 1 分の揺れで落としていた ([ENTRY DIRECTION SAFETY] BLOCK
    #   original_guard_ng が 9/15 に多発)。既定で無効化。戻すときは env で 1。
    "ENTRY_DIRECTION_CONFIRM_ENABLED": "0",
    # 2026-09-16: 「3分/5分足で MA5 を超えて 3 本目、かつ MA5 が順行」を要求するガード。
    #   3m/5m ゲート (終値>5MA>25MA, RSI, MACD) を通った候補をさらに 9 分待たせ、
    #   in-memory の古い 3m 履歴で判定していた (4075: closes [1473,1608,1618] で NG)。
    #   9/16 午前、ゲート通過後の唯一の候補をこれが落とした。既定で無効化。
    "ENTRY_MA5_THIRD_BAR_SLOPE_GUARD_ENABLED": "0",
    # 2026-09-16 (C9): SUMMARY の初回指値は相手側気配に触れない (BUY=bid+tick を ask-tick で
    #   頭打ち、SELL はその逆)。未約定キャンセル→再queue (round>=1) で従来の板タッチへ寄せる。
    #   受け身で待つ分、未約定キャンセルまでの秒数を 4 → 8 に延ばす (再queue 後の
    #   板タッチ注文は通常すぐ約定するので影響は小さい)。
    # 2026-09-17 ユーザー指示「終値 > 5MA > 25MA はあまり気にしないでほしい」:
    #   3m/5m ゲートは RSI と MACD だけで判定し、MA の並びは見ない (1 で復活)。
    "MTF_MA_GATE_MA_CHECK_ENABLED": "0",
    # 2026-09-18 ユーザー指示「利確をすぐにするのではなく、利益が伸び反転したら
    #   利確にしたい」: BLOWOFF_FULL_TAKE の +0.45% 全量利確を実質無効化し、
    #   トレーリング (HIGH_0P2_TRAIL、高値から 0.2%) と建値ストップ (0.35%) に任せる。
    #   9/18 の 16 件を 1 分値でシミュレーションした結果:
    #     実績 (FULL_TAKE 0.45%)          +13,700 円
    #     FULL_TAKE なし + トレール 0.2%  +20,900 円  ← 最良
    #     FULL_TAKE なし + トレール 0.5%  +20,100 円
    #   トレール幅は広げると銘柄ごとの改善と悪化が相殺するので 0.2% のまま。
    #   戻すときは 0.45 / 1.20 に戻す。
    #   2026-09-18 追記: 全銘柄一律で無効化するのはやめ、勢いのある銘柄だけ
    #   伸ばす形にした (BLOWOFF_TREND_HOLD_DAY_UP_PCT)。しきい値は元に戻す。
    # 2026-09-28: 0.45 -> 0.80。早利確を減らす。
    #   9/15〜9/28 の実績 41 件: FULL_TAKE 5 件は勝率 100% (+21,820円) だが
    #   HIGH_0P2_TRAIL 15 件は勝率 33% (-1,290円)、ENTRY_PRICE_STOP 20 件は
    #   勝率 0% (-26,270円)。全体で勝率 24% / -6,070円。
    #   1 分足で 72 建玉を再生すると早利確ほど単調に悪化した:
    #     無効 +75,233 / 1.00% +33,472 / 0.60% +11,404 /
    #     0.45% -340 / 0.40% -3,362 / 0.30% -12,213
    #   ただし上位 6 件が全体の 89% を占め、うち 5 件が 9/18 に集中している。
    #   検証は 1 分スナップショットなので分内の押し目が見えず、伸ばす側を
    #   過大評価している。9/19 にも 9/18 の 16 件だけで「トレール主体が最良」と
    #   結論して失敗したので、無効化まで行かず中間の 0.80 で実データを積む。
    #   戻すときは 0.45。
    "BLOWOFF_FULL_TAKE_PCT": "0.80",
    # 2026-09-28: 引け間際の意図しない持ち越しを防ぐ。
    #   ENTRY_DAYTRADE_CUTOFF_TIME=15:00 以降の新規は日計り信用が使えず
    #   制度信用 (MarginTradeType=1) へ自動で切り替わる。ところが
    #   15:24:30 の強制決済は DAYTRADE_FORCE_CLOSE_ONLY_MARGIN_TYPE="3" で
    #   日計り信用だけを対象にしているため、15:00〜15:20 (ENTRY_NO_NEW_AFTER)
    #   に入った建玉は強制決済されず翌日へ持ち越されうる。
    #   実際に 9/15 15:03 の 6723 が制度信用で建っていた (6 分後に通常の
    #   決済条件で閉じたので結果的に助かっただけ)。positions.db には
    #   margin_trade_type=1 の建玉が 33 件ある。
    #   1) 日計り信用が使えない時刻には新規を入れない
    #   2) 強制決済は信用区分を問わず全建玉に効かせる (すり抜けの保険)
    "ENTRY_NO_NEW_AFTER": "15:00",
    # 2026-10-20: 新規買いを再開 (ユーザー判断)。10-19 の 769bf45 で止めていた (コード側の既定は 0)。
    #   止めた根拠は 9/07〜 の実売買で 買い 215 回 -62,140 円。止めるときは "0"。
    "ENTRY_BUY_ENABLED": "1",
    # 2026-10-20: 最低株価を 150 円へ (ユーザー判断、2440 = 148 円がきっかけ。100 -> 150 に修正)。
    #   150〜333 円は 1 呼値が株価の 0.3% 超 (148 円で 0.68%) になり、スプレッドだけで負けやすい点は伝えた。
    #   最低株価はモジュールごとに散っているので、ここでまとめて 150 にする。戻すときは各キーを消す。
    "SUMMARY_AI_APPROVAL_MIN_PRICE_OVERRIDE": "150",
    "SUMMARY_AI_ENTRY_MIN_PRICE": "150",
    "LIQUIDITY_MIN_PRICE": "150",
    "PUSH_RANKING_SELECTOR_MIN_PRICE": "150",
    "PUSH_RELAY_REGULARS_MIN_PRICE": "150",
    "RANKING_ENTRY_PREFILTER_MIN_PRICE": "150",
    "RANKING_ENTRY_PRICE_MIN": "150",  # core/startup/__init__.py が RANKING_ENTRY_CONFIG["PRICE"]["MIN"] を上書きする (既定 200)
    "RANKING_ENTRY_RESCUE_MIN_PRICE": "150",
    "SURGE_SHORT_MIN_PRICE": "150",
    "OVEREXT_SHORT_MIN_PRICE": "150",
    "TONOSAMA_MIN_PRICE": "150",
    "ENTRY_MIN_PRICE": "150",
    "ACTIVE_MIN_PRICE": "150",
    "LOW_MOVE_TONOSAMA_MIN_ENTRY_PRICE": "150",
    "SELLING_CLIMAX_MIN_PRICE": "150",
    "DAYTRADE_FORCE_CLOSE_ONLY_MARGIN_TYPE": "0",
    "BLOWOFF_FULL_TAKE_PCT_UNCONFIRMED": "1.20",
    #   しきい値は 9/18 の 16 件で 3.0〜8.0 を振って決めた。5.5% が一致率
    #   15/16 (94%) で最良。6433 (+5.25〜5.30%) を早降り側に、4052 (+5.97%) を
    #   伸ばす側に分けられる。ただし 16 件で決めた値なので過学習ぎみ。
    #   運用しながら 5.5〜6.0 の範囲で見直すこと。
    "BLOWOFF_TREND_HOLD_DAY_UP_PCT": "5.5",
    # 2026-09-18: PUSH ローテーションが 38 秒周期になり、1 グループ 50 銘柄が
    #   4.8 秒しか観測されていなかった (観測率 12.6%)。原因は登録枠の想定が
    #   100 なのに実際の母集団が 50 しかないこと:
    #     min_keep = min(100, max_keep) = 100
    #     merged (50 件) < min_keep (100) が常に成立
    #     -> 毎サイクル active_symbol_manager / dynamic_providers / DB fallback が
    #        全部走り、summary 参照と ranking/summary DB 読み込みで 30 秒以上かかる
    #   kabu API の登録上限は 50 で、増枠は 2027 年 3 月予定。100 を集める意味が
    #   無いので実態に合わせる。これで
    #     - min_keep を満たすので重いフォールバックが走らない
    #     - max_keep=50 なので _split_ab の B 側が空になり B サイクルも消える
    #   戻すときは 100 / 100 に。
    # 2026-09-19: 選定を当日変化率順に変えた副作用で、50 枠の売買代金中央値が
    #   80〜180 億 -> 1.7〜8.5 億に落ち、最低では 440 万円の銘柄まで入っていた。
    #   1 回 60〜70 万円のロットだと出来高の 14% を自分で叩くことになる。
    #   10 日分で最低売買代金としきい値を振った結果:
    #     100万 (旧既定) 捕捉184件 最低 440万   ロット占有 14%   <- 危険
    #     5000万         捕捉143件 最低 5,580万 ロット占有 1.1%  <- 採用
    #     1億            捕捉118件 最低 1.06億  ロット占有 0.6%
    #   5000 万でも現行の売買代金順 (27 件) の 5.3 倍の捕捉を保てる。
    # 2026-09-25: 受け身指値の round メモリ TTL。120 秒では短すぎて 9/24 は
    #   requeue 5 回に対し round=1 の発注が 0 件だった。実測した
    #   「requeue -> 次の発注」の間隔は 185 秒 / 2,272 秒 / 6,203 秒 で
    #   中央値 37.9 分。TTL 120 秒だと 0%、3600 秒で 67% が間に合う。
    # 2026-09-25: kabu Station 側の銘柄登録はシステムを止めても残る。
    #   9/24 に W: から起動した際の 100 銘柄が残っており、起動直後から
    #   registered_count=100 で、そこへ今日の 50 を足そうとして
    #   Code 4001019「一部の銘柄が登録できませんでした」になっていた。
    #   登録上限 50 を超えると /board が全銘柄 4002006 で全滅する。
    #   register の前に unregister_all を挟んで必ず 50 に保つ。
    #   (unregister_all -> 0.2 秒 -> register。周期 6 秒に対し 3% 程度の
    #    ティック欠けになるが、上限超過で板が全滅するより軽い)
    "PUSH_ROTATION_UNREGISTER_FIRST": "1",
    "ENTRY_PASSIVE_ROUND_MEMORY_TTL_SEC": "3600",
    # 2026-09-25: RANKING 候補の元データの鮮度上限。既定 300 秒は発注直前の
    #   ENTRY_EXECUTE_MAX_BAR_AGE_SEC=180 と矛盾しており、180〜300 秒の帯で
    #   拾った候補は作られた瞬間に STALE_CANDIDATE_SKIP が確定していた。
    #   11:13 の実測: source age=195.8s を「fresh」と判定 -> 17 秒後に
    #   bar_age=213.0s で却下。10:55〜11:15 の却下 8 件すべてこれ。
    #   DB 側は 60〜90 秒前まで入っており、読み直し (indexed read 1.67 秒)
    #   は LOOP budget 30 秒に対して軽い。120 秒に下げて読み直させる。
    "RANKING_ENTRY_SOURCE_MAX_AGE_SEC": "120",
    # 2026-09-25: RANKING 候補の元テーブルを、順位が実際に入っている 2 つに限定する。
    #   DB フォールバックは「datetime が最も新しいテーブル」を選ぶが、
    #   ranking_summary_1min / ranking_summary_5min は rank が全行 NULL。
    #   5min は足の終値時刻を先に書くため一番新しく見えて選ばれてしまい、
    #   14:05 以降 rank=999999 で PREFILTER_RANK (上限 80 位) が 200/200 却下、
    #   RANKING 候補が 0 件になった (14:01/14:03 は 11 件)。
    #   さらにその結果が last-good キャッシュに残り 0 件が続いた。
    "RANKING_ENTRY_SOURCE_DB_TABLES": "ranking_snapshot_1min,ranking_raw_1min",
    "ACTIVE_MIN_TRADING_VALUE": "50000000",
    "PUSH_ROTATION_TARGET_MIN_KEEP": "50",
    "PUSH_ROTATION_TARGET_MAX_KEEP": "50",
    "ENTRY_PASSIVE_LIMIT_ENABLED": "1",
    "ENTRY_PASSIVE_LIMIT_MODE": "near",
    "ENTRY_PASSIVE_CANCEL_AFTER_SEC": "8",
    "PULLBACK_ENTRY_ENABLED": "1",
    "PULLBACK_ENTRY_MAX_CANDIDATES": "5",
    "PULLBACK_ENTRY_LOT_RATIO": "0.5",
    "PULLBACK_ENTRY_MIN_PULLBACK_PCT": "0.25",
    "PULLBACK_ENTRY_MAX_PULLBACK_PCT": "1.50",
    "PULLBACK_ENTRY_NEAR_MA_PCT": "0.35",
    "PULLBACK_ENTRY_MIN_REBOUND_VOL_RATIO": "0.80",
    "PULLBACK_ENTRY_MIN_VOLUME": "30000",
    "PULLBACK_ENTRY_MIN_TURNOVER": "10000000",
}

SUMMARY_YAHOO_DEFAULTS: Dict[str, str] = {
    # 2026-09-29: 前日ウォームアップを全 interval で止める。
    #   main.py が 1,156 銘柄 x 3 時間枠 = 175,341 行/サイクルを計算して
    #   1 分足フレームが 6 分遅れになり、STALE で全行破棄されて発注ゼロだった。
    #   前日ウォームアップだけで 3分足 75,860 行 + 5分足 52,688 行を足していた。
    #   エントリージョブも前回が終わらず tonosama 25 回 / ranking 12 回スキップ。
    #   実売買できるのは PUSH 登録中の約 50 銘柄だけ (候補 49 件中 10 件が登録中)。
    #   代償: 3分/5分の RSI が当日足だけになる (0a6f222 の効果は寄り後 1 時間
    #   ほど落ちる。3分足は 09:45 頃、5分足は 10:10 頃に 14 本そろう)。
    #   "1min" だけに戻せば 3分/5分の前日ウォームアップは復活する。
    # 2026-09-29: ranking_summary_3min/5min の指標が 50 分古いままだったのを直す。
    #   RANKING_COLLECTOR_ENABLE_FULL_TICK の既定が False で full tick が一度も
    #   走らず、update_ranking_summary_cache() が毎分スキップされていた
    #   ([RANKING FAST] skipped update_ranking_summary_cache が 26 回/26 分)。
    #   ランキング自体は毎分 1,400 銘柄取れているのに、3分/5分の指標だけ
    #   13:42 で止まっていた (now 14:31)。
    #
    #   その結果 MTF ゲートは 1,295 銘柄分ある DB を使えず、PUSH 由来の
    #   81 銘柄だけを見ていた (3分足 attach 17/67)。MTF_MA_GATE_NG が
    #   最多却下理由になっていた上流がここ。
    #
    #   full tick は ranking_collector_runner (別プロセス) で走るので
    #   発注する main.py を止めない。ただし同じ schedule ループ上の
    #   fast tick (毎分) を遅らせうるので、周期は 3 分にして様子を見る。
    #   [RANKING TECH STAGE] の実測は 18〜56 秒。
    #   RANKING_COLLECTOR_ENABLE_FULL_TICK=0 で従来動作に戻る。
    "RANKING_COLLECTOR_ENABLE_FULL_TICK": "1",
    "RANKING_COLLECTOR_FULL_EVERY_MINUTES": "3",
    # 2026-09-30: 前日の 1/3/5 分足 summary を指標計算の前に連結する (設計どおり)。
    #   9/29 10:01 に全 interval で止めていたため、寄り直後は当日の数本だけで
    #   RSI/MACD/5MA/25MA を計算していた (3 分足の本当の 25MA は 10:15、5 分足は 11:05)。
    #   止めた理由の重さは、ranking summary の監視銘柄化 (約 100 銘柄) と
    #   前日ウォームアップの銘柄単位キャッシュ (indicator_calculator) で解消。
    #   100 銘柄の実測: 当日最初の足で ma25/rsi が 97-99/100 埋まる。
    #   追加コストは初回だけ NAS 読み 0.8-8.8 秒、以降 +0.1-0.3 秒。
    #   "none" はどの interval とも一致しない = 全 interval で前日を読む。
    "PREVDAY_WARMUP_SKIP_INTERVALS": "none",
    "AUTOSTOCK_MAIN_SKIP_SUMMARY_DB_SAVE": "1",
    "AUTOSTOCK_MAIN_SKIP_YAHOO_COMPLEMENT": "1",
    "YAHOO_COMPLEMENT_RUN_IN_MAIN": "0",
    "AUTOSTOCK_ENABLE_YAHOO_COMPLEMENT_IN_MAIN": "0",
    "YAHOO_COMPLEMENT_DB_WARMUP_ENABLED": "1",
    "YAHOO_COMPLEMENT_DB_WARMUP_MIN_BARS": "75",
    "YAHOO_COMPLEMENT_DB_WARMUP_LOOKBACK_DAYS": "7",
    "SUMMARY_DB_DATE_GUARD_ENABLED": "1",
    "SUMMARY_DB_DATE_GUARD_CLEANUP_ENABLED": "0",
    "SUMMARY_SUPPRESS_LUNCH_FALLBACK_AFTER_PM": "1",
    "TRADE_UNIVERSE_MIN_PRICE": "150",  # 2026-10-20: 200 -> 150 (ユーザー判断)
    "TRADE_UNIVERSE_MAX_PRICE": "7000",
    "SUMMARY_PIPELINE_MIN_PRICE": "150",  # 2026-10-20: 200 -> 150 (ユーザー判断)
    "SUMMARY_PIPELINE_MAX_PRICE": "7000",
    "SUMMARY_DISPLAY_MAX_PRICE": "7000",
    "ENTRY_MAX_PRICE": "7000",
    "RANKING_MAX_PRICE": "7000",
}


def apply_push_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(PUSH_DEFAULTS, environ=environ)


def apply_rescue_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(RESCUE_DEFAULTS, environ=environ)


def apply_db_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(DB_DEFAULTS, environ=environ)


def apply_helper_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(HELPER_DEFAULTS, environ=environ)


def apply_main_restore_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(MAIN_RESTORE_DEFAULTS, environ=environ)


def apply_ranking_entry_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(RANKING_ENTRY_DEFAULTS, environ=environ)


def apply_tonosama_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(TONOSAMA_DEFAULTS, environ=environ)


def apply_entry_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(ENTRY_DEFAULTS, environ=environ)


def apply_summary_yahoo_defaults(*, environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    return _set_defaults(SUMMARY_YAHOO_DEFAULTS, environ=environ)


def apply_site_defaults(*, context: str = "", environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    """Compatibility wrapper used by runtime_env_defaults_patch.py/sitecustomize.py."""
    return _merge_applied(
        apply_push_defaults(environ=environ),
        apply_rescue_defaults(environ=environ),
        apply_db_defaults(environ=environ),
        apply_helper_defaults(environ=environ),
        apply_ranking_entry_defaults(environ=environ),
        apply_tonosama_defaults(environ=environ),
        apply_entry_defaults(environ=environ),
        apply_summary_yahoo_defaults(environ=environ),
    )


def apply_user_defaults(*, context: str = "", environ: Optional[MutableMapping[str, str]] = None) -> Dict[str, str]:
    """Compatibility wrapper used by runtime_env_defaults_patch.py/usercustomize.py."""
    return _merge_applied(
        apply_main_restore_defaults(environ=environ),
        apply_ranking_entry_defaults(environ=environ),
        apply_tonosama_defaults(environ=environ),
        apply_entry_defaults(environ=environ),
        apply_summary_yahoo_defaults(environ=environ),
    )


__all__ = [
    "VERSION",
    "env_bool",
    "apply_push_defaults",
    "apply_rescue_defaults",
    "apply_db_defaults",
    "apply_helper_defaults",
    "apply_main_restore_defaults",
    "apply_ranking_entry_defaults",
    "apply_tonosama_defaults",
    "apply_entry_defaults",
    "apply_summary_yahoo_defaults",
    "apply_site_defaults",
    "apply_user_defaults",
]
