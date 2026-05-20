# ============================================================
# File   : core/startup/oneshot_limit_700k_patch.py
# Version: Ver21-EARLY-OPEN-POSITION-STALE-DB-CLEANUP
# ------------------------------------------------------------
# 起動時 runtime patches:
# - PUSH bootstrap fast restore: 起動時PUSH DB復元を軽量化
# - OPEN POSITION stale DB cleanup: broker側に無い古いDB建玉を自動CLOSED化
# - 70万円ワンショット制限
# - ENTRY数量0株の最低100株フォールバック
# - BUYエントリー閾値を後場スコアに合わせて緩和
# - SUMMARY AI daily risk / executed判定 / 売建不可候補除外
# - SUMMARY AI pre-order dedupe/cooldown無効化
# - SUMMARY AI async queue: busy時に候補を捨てずキュー処理
# - SUMMARY AI strict liquidity: 直近1本/平均出来高も必須
# - WATCHLIST recent liquidity: 監視銘柄選定時点で直近1本/平均/売買代金を必須化
# - SUMMARY parallel intervals: 1m/3m/5mを並列実行
# - ENTRY pipeline bucket prefilter: interval/source違いpendingを事前除外
# - SUMMARY_ENTRY duplicate pending を registered 扱いにする
# - EXIT 利益保護 / 板対応
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_INSTALLED = False


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


def _install_push_bootstrap_fast_restore_patch() -> bool:
    try:
        os.environ.setdefault("PUSH_BOOTSTRAP_FAST_RESTORE", "1")
        os.environ.setdefault("PUSH_BOOTSTRAP_FAST_MAX_ROWS", "3000")
        os.environ.setdefault("PUSH_BOOTSTRAP_FAST_LOOKBACK_MINUTES", "45")
        from core.startup import push_bootstrap_fast_restore_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] push_bootstrap_fast_restore_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] push_bootstrap_fast_restore_patch install failed")
        return False


def _install_open_position_stale_db_cleanup_patch() -> bool:
    try:
        os.environ.setdefault("OPEN_POSITION_AUTO_CLOSE_STALE_DB", "1")
        os.environ.setdefault("OPEN_POSITION_STALE_DB_CLEAN_INTERVAL_SEC", "30")
        from core.startup import open_position_stale_db_cleanup_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] open_position_stale_db_cleanup_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] open_position_stale_db_cleanup_patch install failed")
        return False


def _install_entry_threshold_patch() -> bool:
    try:
        import trading.handlers.entry_controller as ec
        old_buy_score = getattr(ec, "MIN_SUMMARY_SCORE_BUY", None)
        old_buy_comp = getattr(ec, "MIN_COMPOSITE_SCORE_BUY", None)
        old_sell_score = getattr(ec, "MIN_SUMMARY_SCORE_SELL", None)
        old_sell_comp = getattr(ec, "MIN_COMPOSITE_SCORE_SELL", None)
        ec.MIN_SUMMARY_SCORE_BUY = _env_float("MIN_SUMMARY_SCORE_BUY", 1.0)
        ec.MIN_COMPOSITE_SCORE_BUY = _env_float("MIN_COMPOSITE_SCORE_BUY", 0.8)
        ec.MIN_SUMMARY_SCORE_SELL = _env_float("MIN_SUMMARY_SCORE_SELL", 1.0)
        ec.MIN_COMPOSITE_SCORE_SELL = _env_float("MIN_COMPOSITE_SCORE_SELL", 1.0)
        logger.warning("[ENTRY THRESHOLD PATCH] installed BUY score %s->%s comp %s->%s SELL score %s->%s comp %s->%s", old_buy_score, ec.MIN_SUMMARY_SCORE_BUY, old_buy_comp, ec.MIN_COMPOSITE_SCORE_BUY, old_sell_score, ec.MIN_SUMMARY_SCORE_SELL, old_sell_comp, ec.MIN_COMPOSITE_SCORE_SELL)
        return True
    except Exception:
        logger.exception("[ENTRY THRESHOLD PATCH] install failed")
        return False


def _install_entry_qty_minlot_patch() -> bool:
    try:
        os.environ.setdefault("ENTRY_MIN_LOT_FALLBACK_WHEN_AFFORDABLE", "1")
        from core.startup import entry_qty_min_lot_runtime_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] entry_qty_min_lot_runtime_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] entry_qty_min_lot_runtime_patch install failed")
        return False


def _install_aligned_summary_display_patch() -> bool:
    try:
        from core.startup import aligned_summary_display_patch as p  # type: ignore
        ok = bool(p.install()) if hasattr(p, "install") else False
        logger.warning("[ONESHOT LIMIT PATCH] aligned_summary_display_patch installed=%s", ok)
        return ok
    except Exception:
        logger.debug("[ONESHOT LIMIT PATCH] aligned_summary_display_patch module not found; skip", exc_info=False)
        return False


def _install_push_flush_auto_recover_patch() -> bool:
    try:
        os.environ.setdefault("PUSH_STREAM_AUTO_RECOVER_FLUSH", "1")
        from core.startup import push_flush_auto_recover_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] push_flush_auto_recover_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] push_flush_auto_recover_patch install failed")
        return False


def _install_summary_ai_symbol_risk_patch() -> bool:
    try:
        os.environ.setdefault("SUMMARY_AI_PRE_FILTER_DAILY_RISK", "1")
        os.environ.setdefault("SUMMARY_AI_PRE_FILTER_DAILY_RISK_SCOPE", "symbol_only")
        os.environ.setdefault("ENTRY_GLOBAL_MAX_DAILY_LOSS_YEN", "-50000")
        os.environ.setdefault("ENTRY_GLOBAL_MAX_CONSECUTIVE_LOSSES", "20")
        from core.startup import summary_ai_daily_risk_symbol_only_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] summary_ai_symbol_risk_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] summary_ai_symbol_risk_patch install failed")
        return False


def _install_summary_ai_executor_result_patch() -> bool:
    try:
        from core.startup import summary_ai_executor_result_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] summary_ai_executor_result_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] summary_ai_executor_result_patch install failed")
        return False


def _install_summary_ai_sell_credit_prefilter_patch() -> bool:
    try:
        from core.startup import summary_ai_sell_credit_prefilter_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] summary_ai_sell_credit_prefilter_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] summary_ai_sell_credit_prefilter_patch install failed")
        return False


def _install_summary_ai_async_entry_patch() -> bool:
    try:
        os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY", "1")
        os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY", "0")
        os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", "20")
        from core.startup import summary_ai_async_entry_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] summary_ai_async_entry_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] summary_ai_async_entry_patch install failed")
        return False


def _install_summary_ai_dedupe_fix_patch() -> bool:
    try:
        os.environ.setdefault("SUMMARY_AI_PREORDER_DEDUPE_ENABLED", "0")
        from core.startup import summary_ai_dedupe_fix_runtime_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] summary_ai_dedupe_fix_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] summary_ai_dedupe_fix_patch install failed")
        return False


def _install_strict_liquidity_patch() -> bool:
    try:
        os.environ.setdefault("SUMMARY_AI_STRICT_LIQ_ENABLED", "1")
        os.environ.setdefault("SUMMARY_AI_LIQ_MIN_LATEST_VOLUME", "30000")
        os.environ.setdefault("SUMMARY_AI_LIQ_MIN_AVG_VOLUME", "30000")
        from core.startup import summary_ai_strict_liquidity_extra_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] summary_ai_strict_liquidity_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] summary_ai_strict_liquidity_patch install failed")
        return False


def _install_watchlist_recent_liquidity_patch() -> bool:
    try:
        os.environ.setdefault("WATCHLIST_RECENT_LIQ_ENABLED", "1")
        os.environ.setdefault("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", "3000")
        os.environ.setdefault("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", "3000")
        os.environ.setdefault("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", "1000000")
        os.environ.setdefault("WATCHLIST_RECENT_LIQ_BARS", "5")
        os.environ.setdefault("WATCHLIST_RECENT_LIQ_PROTECT_BYPASS", "1")
        from core.startup import watchlist_recent_liquidity_guard_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] watchlist_recent_liquidity_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] watchlist_recent_liquidity_patch install failed")
        return False


def _install_summary_parallel_intervals_patch() -> bool:
    try:
        os.environ.setdefault("SUMMARY_PARALLEL_INTERVALS_ENABLED", "1")
        os.environ.setdefault("SUMMARY_PARALLEL_INTERVAL_WORKERS", "3")
        os.environ.setdefault("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC", "55")
        os.environ.setdefault("SUMMARY_PARALLEL_RANKING_ENABLED", "1")
        from core.startup import summary_parallel_intervals_runtime_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] summary_parallel_intervals_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] summary_parallel_intervals_patch install failed")
        return False


def _install_entry_pipeline_bucket_filter_patch() -> bool:
    try:
        os.environ.setdefault("ENTRY_PIPELINE_BUCKET_PREFILTER", "1")
        from core.startup import entry_controller_pipeline_bucket_filter_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] entry_pipeline_bucket_filter_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] entry_pipeline_bucket_filter_patch install failed")
        return False


def _install_summary_entry_pending_existing_fix_patch() -> bool:
    try:
        from core.startup import summary_entry_pending_existing_fix_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] summary_entry_pending_existing_fix_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] summary_entry_pending_existing_fix_patch install failed")
        return False


def _install_entry_direction_failopen_patch() -> bool:
    try:
        os.environ.setdefault("ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI", "1")
        from core.startup import entry_direction_failopen_runtime_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] entry_direction_failopen_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] entry_direction_failopen_patch install failed")
        return False


def _install_exit_profit_protect_patch() -> bool:
    try:
        os.environ.setdefault("EXIT_PROFIT_PROTECT_ENABLED", "1")
        os.environ.setdefault("EXIT_PROFIT_TAKE_PCT", "0.0030")
        os.environ.setdefault("EXIT_PROFIT_PROTECT_START_PCT", "0.0020")
        os.environ.setdefault("EXIT_PROFIT_PROTECT_FLOOR_PCT", "0.0010")
        os.environ.setdefault("EXIT_PROFIT_GIVEBACK_PCT", "0.0010")
        os.environ.setdefault("EXIT_PROFIT_MIN_HOLD_SEC", "0")
        from core.startup import exit_profit_protect_runtime_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] exit_profit_protect_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] exit_profit_protect_patch install failed")
        return False


def _install_exit_board_profit_patch() -> bool:
    try:
        os.environ.setdefault("EXIT_BOARD_PROFIT_ENABLED", "1")
        os.environ.setdefault("EXIT_BOARD_MIN_PROFIT_PCT", "0.0010")
        os.environ.setdefault("EXIT_BOARD_THICK_QTY_MULT", "3.0")
        os.environ.setdefault("EXIT_BOARD_MIN_THICK_QTY", "1000")
        os.environ.setdefault("EXIT_BOARD_MAX_WAIT_SEC", "2.0")
        os.environ.setdefault("EXIT_BOARD_POLL_SEC", "0.25")
        os.environ.setdefault("EXIT_BOARD_REVERSE_GAP_PCT", "0.0010")
        from core.startup import exit_board_profit_runtime_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] exit_board_profit_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] exit_board_profit_patch install failed")
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    ok_push_fast_restore = _install_push_bootstrap_fast_restore_patch()
    ok_stale_db_cleanup = _install_open_position_stale_db_cleanup_patch()

    ok_async_entry = _install_summary_ai_async_entry_patch()
    ok_dedupe_fix = _install_summary_ai_dedupe_fix_patch()
    ok_strict_liq = _install_strict_liquidity_patch()
    ok_watchlist_liq = _install_watchlist_recent_liquidity_patch()
    ok_parallel = _install_summary_parallel_intervals_patch()
    ok_bucket_filter = _install_entry_pipeline_bucket_filter_patch()
    ok_pending_existing = _install_summary_entry_pending_existing_fix_patch()
    ok_direction_failopen = _install_entry_direction_failopen_patch()
    ok_exit_profit = _install_exit_profit_protect_patch()
    ok_exit_board = _install_exit_board_profit_patch()

    ok_display = _install_aligned_summary_display_patch()
    ok_threshold = _install_entry_threshold_patch()
    ok_qty_minlot = _install_entry_qty_minlot_patch()
    ok_push_flush = _install_push_flush_auto_recover_patch()

    ok_main = False
    try:
        from kabu_api import buy_sell_entry as bse
        old_value = getattr(bse, "MAX_ONESHOT", None)
        bse.MAX_ONESHOT = 700_000
        ok_main = True
        logger.warning("[ONESHOT LIMIT PATCH] MAX_ONESHOT changed old=%s new=%s", old_value, bse.MAX_ONESHOT)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] install failed")

    ok_symbol_risk = _install_summary_ai_symbol_risk_patch()
    ok_executor_result = _install_summary_ai_executor_result_patch()
    ok_sell_credit_prefilter = _install_summary_ai_sell_credit_prefilter_patch()

    _INSTALLED = bool(
        ok_push_fast_restore
        or ok_stale_db_cleanup
        or ok_async_entry
        or ok_dedupe_fix
        or ok_strict_liq
        or ok_watchlist_liq
        or ok_parallel
        or ok_bucket_filter
        or ok_pending_existing
        or ok_direction_failopen
        or ok_exit_profit
        or ok_exit_board
        or ok_display
        or ok_threshold
        or ok_qty_minlot
        or ok_push_flush
        or ok_main
        or ok_symbol_risk
        or ok_executor_result
        or ok_sell_credit_prefilter
    )
    return _INSTALLED
