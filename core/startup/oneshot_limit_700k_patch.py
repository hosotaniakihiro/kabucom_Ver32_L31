# ============================================================
# File   : core/startup/oneshot_limit_700k_patch.py
# Version: Ver15-EARLY-STRICT-LIQUIDITY
# ------------------------------------------------------------
# 起動時 runtime patches:
# - 70万円ワンショット制限
# - ENTRY数量0株の最低100株フォールバック
# - BUYエントリー閾値を後場スコアに合わせて緩和
# - SUMMARY AI daily risk / executed判定 / 売建不可候補除外
# - SUMMARY AI pre-order dedupe/cooldown無効化
# - SUMMARY AI strict liquidity: 直近1本/平均出来高も必須
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
        logger.warning(
            "[ENTRY THRESHOLD PATCH] installed BUY score %s->%s comp %s->%s SELL score %s->%s comp %s->%s",
            old_buy_score, ec.MIN_SUMMARY_SCORE_BUY,
            old_buy_comp, ec.MIN_COMPOSITE_SCORE_BUY,
            old_sell_score, ec.MIN_SUMMARY_SCORE_SELL,
            old_sell_comp, ec.MIN_COMPOSITE_SCORE_SELL,
        )
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
        # Ver15では表示整形パッチは既存実装を維持するため、読み込みのみ行う。
        # 失敗しても売買ロジックには影響させない。
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
        os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY", "1")
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

    ok_async_entry = _install_summary_ai_async_entry_patch()
    ok_dedupe_fix = _install_summary_ai_dedupe_fix_patch()
    ok_strict_liq = _install_strict_liquidity_patch()
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
        ok_async_entry
        or ok_dedupe_fix
        or ok_strict_liq
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
