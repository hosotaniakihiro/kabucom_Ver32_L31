# ============================================================
# File   : core/startup/startup_orchestrator.py
# Version: FINAL-PRODUCTION-REV23.3-STARTUP-ORCHESTRATOR
#          -RANKING-SUMMARY-BOOTSTRAP-BEFORE-SUMMARY-RESTORE
# ------------------------------------------------------------
# 【概要】
#   system_startup の実行順序だけを管理する司令塔
#
# 【設計】
#   - 詳細処理は各分割モジュールに委譲
#   - このファイルは「何をどの順序で起動するか」だけを表現する
#
# 【起動順序】
#   1. settings / runtime flags
#   2. token refresh
#   3. ensure dirs
#   4. safe migration
#   5. summary unique index bootstrap
#   6. PUSH stack 起動
#   7. scheduler stack 起動
#   8. market state 判定
#   9. ranking summary bootstrap
#  10. startup summary restore
#  11. push stream fallback
#  12. summary fast boot / MTF history
#  13. anchor
#  14. market mode
#  15. realtime combat
#  16. background
#  17. scheduler fallback / schedule loop fallback
#  18. final status
#
# 【REV23.3 変更点】
#   ✔ ranking summary bootstrap を startup_summary_restore より前へ移動
#   ✔ RANKING SUMMARY BOOTSTRAP ログが表示されない問題を回避
#   ✔ startup_summary_restore が重い/ブロックしても ranking summary bootstrap を先に実行
#   ✔ ranking_summary_bootstrap_* flags を global_data へ詳細反映
#   ✔ ranking_summary_bootstrap_saved / snapshot_rows / db_path / message を更新
#   ✔ ranking summary bootstrap 失敗時も system_startup は継続
#   ✔ scheduler / schedule loop は従来どおり先に起動
#   ✔ 機能削除ゼロ
# ============================================================

from __future__ import annotations

import logging
from typing import Any

from config.paths import ensure_dirs
from global_state import global_data
from utils.business_day_utils import is_market_open

from core.startup.startup_flags import reset_startup_flags
from core.startup.startup_runtime import safe_migration_phase
from core.startup.summary_index_bootstrap import bootstrap_summary_unique_indexes_safe
from core.startup.anchor_bootstrap import bootstrap_anchor
from core.startup.background_bootstrap import bootstrap_background
from core.startup.runtime_mode import enter_realtime_combat_mode
from core.startup.closed_day_display import display_closed_day_summary_priority

from core.startup.startup_config import (
    SUMMARY_DIR,
    RANKING_DIR,
    load_settings,
    init_runtime_flags,
    refresh_token_safe,
)
from core.startup.push_startup import (
    start_push_stack_before_scheduler,
    start_push_stream_fallback_safe,
)
from core.startup.scheduler_startup import (
    start_scheduler_stack_before_restore,
    register_scheduler_fallback_safe,
    ensure_schedule_loop_running_safe,
)
from core.startup.summary_startup import (
    run_startup_summary_restore_safe,
    start_summary_fast_boot_safe,
    run_mtf_history_bootstrap_startup_safe,
)
from core.startup.startup_status import log_startup_complete

logger = logging.getLogger(__name__)

VERSION = "FINAL-PRODUCTION-REV23.3-STARTUP-ORCHESTRATOR-RANKING-SUMMARY-BEFORE-SUMMARY-RESTORE"


# ============================================================
# small helpers
# ============================================================

def _safe_set_global(name: str, value: Any) -> None:
    try:
        setattr(global_data, name, value)
    except Exception:
        logger.debug(
            "[orchestrator] global_data set failed name=%s value=%s",
            name,
            value,
            exc_info=True,
        )


def _safe_get_result_attr(result: Any, name: str, default: Any = None) -> Any:
    try:
        if isinstance(result, dict):
            return result.get(name, default)
        return getattr(result, name, default)
    except Exception:
        return default


def _reset_ranking_summary_bootstrap_flags() -> None:
    """
    ranking summary bootstrap 用 flag を安全に初期化する。

    startup_config.py 側でも初期化しているが、
    safe_migration_phase で global_data.clear_all() が呼ばれる可能性があるため、
    実行直前にも最低限初期化する。
    """
    _safe_set_global("ranking_summary_bootstrap_started", False)
    _safe_set_global("ranking_summary_bootstrap_done", False)
    _safe_set_global("ranking_summary_bootstrap_failed", False)
    _safe_set_global("ranking_summary_bootstrap_result", None)
    _safe_set_global("ranking_summary_bootstrap_saved", {})
    _safe_set_global("ranking_summary_bootstrap_snapshot_rows", 0)
    _safe_set_global("ranking_summary_bootstrap_db_path", None)
    _safe_set_global("ranking_summary_bootstrap_message", "")


def _store_ranking_summary_bootstrap_result(result: Any) -> bool:
    """
    bootstrap_ranking_summary_on_startup の戻り値を global_data に展開する。

    想定 result:
      RankingSummaryBootstrapResult(
          ok=True/False,
          intervals={1: n, 3: n, 5: n},
          db_path="...",
          snapshot_rows=n,
          message="..."
      )
    """
    ok = bool(_safe_get_result_attr(result, "ok", False))
    saved = _safe_get_result_attr(result, "intervals", {})
    snapshot_rows = _safe_get_result_attr(result, "snapshot_rows", 0)
    db_path = _safe_get_result_attr(result, "db_path", None)
    message = _safe_get_result_attr(result, "message", "")

    _safe_set_global("ranking_summary_bootstrap_result", result)
    _safe_set_global("ranking_summary_bootstrap_done", ok)
    _safe_set_global("ranking_summary_bootstrap_failed", not ok)
    _safe_set_global("ranking_summary_bootstrap_saved", saved if saved is not None else {})
    _safe_set_global("ranking_summary_bootstrap_snapshot_rows", snapshot_rows or 0)
    _safe_set_global("ranking_summary_bootstrap_db_path", db_path)
    _safe_set_global("ranking_summary_bootstrap_message", message or "")

    return ok


# ============================================================
# safe wrappers
# ============================================================

def _run_bootstrap_anchor_safe() -> None:
    try:
        bootstrap_anchor()
    except Exception:
        logger.exception("❌ Anchor bootstrap failed")


def _set_market_mode(market_open_now: bool) -> None:
    """
    market open / closed に応じて注文可否を設定する。

    重要:
      - PUSH保存 writer
      - push_stream
      - scheduler
    は market_open に関係なく起動済みにする。

    market closed の場合:
      - 注文は禁止
      - background / scheduler / summary 表示は継続
    """
    if not market_open_now:
        try:
            display_closed_day_summary_priority()
        except Exception:
            logger.exception("❌ closed day display failed")

        logger.info("🟡 MARKET CLOSED MODE → background WILL start (orders disabled)")
        global_data.allow_orders = False
    else:
        global_data.allow_orders = True
        logger.info("📈 Market open fast-start mode: summary bootstrap continues in background")


def _safe_migration_phase_or_raise() -> None:
    try:
        safe_migration_phase(SUMMARY_DIR, RANKING_DIR)
    except Exception:
        logger.exception("❌ SAFE MIGRATION FAILED")
        raise


def _bootstrap_summary_unique_indexes_or_raise() -> None:
    try:
        bootstrap_summary_unique_indexes_safe()
    except Exception:
        logger.exception("❌ SUMMARY UNIQUE INDEX BOOTSTRAP FAILED")
        raise


def _enter_realtime_combat_mode_safe(*, market_open_now: bool) -> None:
    try:
        enter_realtime_combat_mode(run_entry=market_open_now)
    except Exception:
        logger.exception("❌ Realtime combat mode failed")


def _bootstrap_background_or_raise() -> None:
    try:
        bootstrap_background()
    except Exception:
        logger.exception("❌ Background bootstrap failed")
        raise


def _bootstrap_ranking_summary_safe() -> None:
    """
    起動時にランキング由来サマリーを復元・追加計算する。

    処理内容:
      - ranking_snapshot_1min を読む
      - PUSH由来 summary DB を補助として読む
      - ranking snapshot から擬似OHLCVを作る
      - 1min / 3min / 5min のランキング由来サマリーを作成
      - MA5 / MA25 / MA75 / RSI / MACD / ATR / VWAP / slope 等を計算
      - ranking_summary DB へ UPSERT
      - global_data に反映

    重要:
      - 失敗しても system_startup は止めない
      - startup_summary_restore より前に実行する
      - scheduler 起動後に実行するため、表示・定時更新の土台が揃う
      - PUSH由来 summary DB は読むだけ
      - ranking_snapshot DB は読むだけ
      - ranking_summary DB に保存する
    """
    _reset_ranking_summary_bootstrap_flags()

    try:
        _safe_set_global("ranking_summary_bootstrap_started", True)
        _safe_set_global("ranking_summary_bootstrap_done", False)
        _safe_set_global("ranking_summary_bootstrap_failed", False)
        _safe_set_global("ranking_summary_bootstrap_result", None)
        _safe_set_global("ranking_summary_bootstrap_message", "running")

        logger.info("📊 [RANKING SUMMARY BOOTSTRAP] startup call begin")

        from trading.ranking.summary.bootstrap import bootstrap_ranking_summary_on_startup

        result = bootstrap_ranking_summary_on_startup(
            intervals=(1, 3, 5),
            save=True,
            update_global_cache=True,
        )

        ok = _store_ranking_summary_bootstrap_result(result)

        if ok:
            logger.info(
                "✅ ranking summary bootstrap done ok=%s saved=%s snapshot_rows=%s db=%s message=%s",
                _safe_get_result_attr(result, "ok", None),
                _safe_get_result_attr(result, "intervals", None),
                _safe_get_result_attr(result, "snapshot_rows", None),
                _safe_get_result_attr(result, "db_path", None),
                _safe_get_result_attr(result, "message", ""),
            )
        else:
            logger.warning(
                "⚠ ranking summary bootstrap completed with warning ok=%s saved=%s snapshot_rows=%s db=%s message=%s",
                _safe_get_result_attr(result, "ok", None),
                _safe_get_result_attr(result, "intervals", None),
                _safe_get_result_attr(result, "snapshot_rows", None),
                _safe_get_result_attr(result, "db_path", None),
                _safe_get_result_attr(result, "message", ""),
            )

    except Exception as e:
        _safe_set_global("ranking_summary_bootstrap_failed", True)
        _safe_set_global("ranking_summary_bootstrap_done", False)
        _safe_set_global("ranking_summary_bootstrap_result", None)
        _safe_set_global("ranking_summary_bootstrap_message", str(e))

        logger.exception("❌ ranking summary bootstrap failed")

    finally:
        _safe_set_global("ranking_summary_bootstrap_started", False)

def _warmup_daily_signal_cache_safe() -> None:
    """
    起動時に日足DB stock_analysis_latest を1回だけ読み、
    daily_signal_cache に保持する。

    重要:
      - 場中の entry / AI gate / exit ではDBを読まない
      - 失敗しても system_startup は止めない
      - stock_analysis_history は読まない
    """
    try:
        logger.info("📅 [DAILY SIGNAL CACHE] startup warmup begin")

        from trading.daily.daily_signal_cache import (
            warmup_daily_signal_cache,
            debug_daily_cache_sample,
            get_daily_cache_size,
        )

        n = warmup_daily_signal_cache()

        logger.info(
            "✅ [DAILY SIGNAL CACHE] startup warmup done symbols=%s cache_size=%s",
            n,
            get_daily_cache_size(),
        )

        if n > 0:
            debug_daily_cache_sample(limit=10)

    except Exception as e:
        logger.exception("❌ [DAILY SIGNAL CACHE] startup warmup failed err=%s", e)
# ============================================================
# main orchestrator
# ============================================================

def run_system_startup():
    logger.info(
        "🚀 system_startup START "
        "(REV23.3 THIN-ORCHESTRATOR "
        "PUSH-STARTUP "
        "SCHEDULER-STARTUP "
        "SCHEDULE-RUN-PENDING-LOOP "
        "SUMMARY-STARTUP "
        "RANKING-SUMMARY-BOOTSTRAP-BEFORE-SUMMARY-RESTORE)"
    )

    # --------------------------------------------------------
    # 1. settings / runtime flags
    # --------------------------------------------------------
    api_password, ws_url = load_settings()

    init_runtime_flags(ws_url=ws_url)
    reset_startup_flags()
    _warmup_daily_signal_cache_safe()
    try:
        global_data.ws_url = ws_url
        global_data.push_ws_url = ws_url
    except Exception:
        logger.debug("[orchestrator] ws_url set failed", exc_info=True)

    # --------------------------------------------------------
    # 2. token refresh
    # --------------------------------------------------------
    refresh_token_safe(api_password)

    # --------------------------------------------------------
    # 3. dirs
    # --------------------------------------------------------
    ensure_dirs()

    # --------------------------------------------------------
    # 4. database / migration
    # --------------------------------------------------------
    _safe_migration_phase_or_raise()

    # --------------------------------------------------------
    # 5. summary unique indexes
    # --------------------------------------------------------
    _bootstrap_summary_unique_indexes_or_raise()

    # --------------------------------------------------------
    # 6. PUSH stack
    # --------------------------------------------------------
    # ここまでで:
    #   - PUSH保存 writer
    #   - 既存 PUSH DB 読込
    #   - symbol bootstrap
    #   - real-symbol bridge
    #   - PUSH WebSocket
    # を起動する。
    # --------------------------------------------------------
    start_push_stack_before_scheduler()

    # --------------------------------------------------------
    # 7. scheduler stack
    # --------------------------------------------------------
    # 重要:
    #   startup_summary_restore より前に scheduler 登録と
    #   schedule.run_pending loop を起動する。
    #
    # 理由:
    #   run_summary_tick_once() だけでは1回しか表示されない。
    #   schedule.run_pending loop が必要。
    # --------------------------------------------------------
    start_scheduler_stack_before_restore()

    # --------------------------------------------------------
    # 8. market state
    # --------------------------------------------------------
    market_open_now = bool(is_market_open())

    # --------------------------------------------------------
    # 9. ranking summary bootstrap
    # --------------------------------------------------------
    # ランキング由来サマリーを先に復元・追加計算する。
    #
    # 理由:
    #   startup_summary_restore が重い/ブロックする場合でも、
    #   ranking summary bootstrap を確実に実行・ログ表示させる。
    #
    # 実行内容:
    #   - ranking_snapshot_1min 読み込み
    #   - PUSH由来 summary DB 補助読み込み
    #   - ranking_summary_1min / 3min / 5min 作成
    #   - ranking_summary DB 保存
    #   - global_data 反映
    #
    # 重要:
    #   - migration 後なので ranking DB / summary DB は解決済み
    #   - PUSH由来 summary DB は読むだけ
    #   - 失敗しても startup は止めない
    # --------------------------------------------------------
    _bootstrap_ranking_summary_safe()

    # --------------------------------------------------------
    # 10. startup summary restore
    # --------------------------------------------------------
    # PUSH由来サマリーを summary DB / PUSH DB から復元する。
    # ranking summary bootstrap はこの前に実行済み。
    #
    # デバッグ:
    #   この前後ログで startup_summary_restore が詰まっているか確認できる。
    # --------------------------------------------------------
    logger.info("🧪 before startup summary restore")
    run_startup_summary_restore_safe()
    logger.info("🧪 after startup summary restore")

    # --------------------------------------------------------
    # 11. PUSH stream fallback
    # --------------------------------------------------------
    start_push_stream_fallback_safe()

    # --------------------------------------------------------
    # 12. summary fast boot / MTF history
    # --------------------------------------------------------
    start_summary_fast_boot_safe()
    run_mtf_history_bootstrap_startup_safe(market_open_now=market_open_now)

    # --------------------------------------------------------
    # 13. anchor
    # --------------------------------------------------------
    _run_bootstrap_anchor_safe()

    # --------------------------------------------------------
    # 14. market mode
    # --------------------------------------------------------
    _set_market_mode(market_open_now)

    # --------------------------------------------------------
    # 15. realtime combat mode
    # --------------------------------------------------------
    _enter_realtime_combat_mode_safe(market_open_now=market_open_now)

    # --------------------------------------------------------
    # 16. background
    # --------------------------------------------------------
    _bootstrap_background_or_raise()

    # --------------------------------------------------------
    # 17. fallback checks
    # --------------------------------------------------------
    register_scheduler_fallback_safe()
    ensure_schedule_loop_running_safe()

    # --------------------------------------------------------
    # 18. final status
    # --------------------------------------------------------
    return log_startup_complete()


__all__ = [
    "VERSION",
    "run_system_startup",
]