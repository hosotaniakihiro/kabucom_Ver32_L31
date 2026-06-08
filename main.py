# ============================================================
# File   : main.py
# Function:
#   - アプリ全体の起動エントリ
#   - optional / symbol map / startup / push stream / push storage の起動
#   - scheduler loop の起動
#   - summary 初回 tick / ranking 初回 tick の1回実行
#   - holiday mode / market open mode の分岐
#   - realtime engine / stream orchestrator / position sync / ATS / monitor 起動
#   - realtime main loop の実行
#   - summary / entry 用 runtime context を global_data へ注入
# ------------------------------------------------------------
# Version: Ver38.33-INSTALL-MA5-THIRD-BAR-RANKING-FAILOPEN
# ------------------------------------------------------------
# ✔ SUMMARY AI async entry patch を main runtime patch 一覧へ明示追加
# ✔ TONOSAMA final liquidity lost-volume fallback threshold を 2.3 に調整
# ✔ TONOSAMA volatility entry-row rescue を main runtime に追加
# ✔ NAS sqlite I/O guard を main runtime に追加
# ✔ _log_skip reason衝突ガードを全runtime patch後の最後に再適用
# ✔ RANKING ENTRY controller timeoutを拡張
# ✔ RANKING_ENTRY 強スコアの技術フィルタ全落ちを救済
# ✔ 3m/5m summary を最新3m/5m時刻以降の1m差分から補完
# ✔ ENTRY_LUNCH_BLOCK_START のデフォルトを 11:30 に設定
# ✔ RANKING high/low欠損時のLOW MOVE条件をスキャル向けに緩和
# ✔ ENTRY MA5 third bar guard V2 を main runtime patch に追加
# ✔ 既存の起動処理は維持
# ============================================================

import os
import sys
import logging

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

os.environ.setdefault("SUMMARY_MAIN_ENTRY_ONLY", "1")
os.environ.setdefault("SUMMARY_SKIP_DB_SAVE_IN_MAIN", "1")
os.environ.setdefault("SUMMARY_DB_WRITER_ROLE", "entry_only")
os.environ.setdefault("SUMMARY_PARALLEL_FORCE_1_3_5", "0")
os.environ.setdefault("SUMMARY_PARALLEL_INTERVAL_TIMEOUT_SEC", "90")
os.environ.setdefault("SUMMARY_PARALLEL_TIMEOUT_MIN_SEC", "90")
os.environ.setdefault("SUMMARY_PARALLEL_INTERVAL_WORKERS", "3")
os.environ.setdefault("SUMMARY_MTF_DIFF_FROM_1M_ENABLED", "1")
os.environ.setdefault("SUMMARY_MTF_DIFF_HISTORY_ROWS", "74")
os.environ.setdefault("SUMMARY_MTF_DIFF_MAX_1M_ROWS", "250000")
os.environ.setdefault("SUMMARY_MTF_DIFF_ALLOW_PARTIAL_BAR", "0")
os.environ.setdefault("MIN_5SEC_PRICE_CHANGE_PCT", "0.01")
os.environ.setdefault("TONOSAMA_MIN_5SEC_PRICE_CHANGE_PCT", "0.01")
os.environ.setdefault("ENTRY_MIN_5SEC_PRICE_CHANGE_PCT", "0.01")
os.environ.setdefault("SUMMARY_AI_MIN_5SEC_PRICE_CHANGE_PCT", "0.01")
os.environ.setdefault("OPTIONAL_LIGHT_MODE", "1")
os.environ.setdefault("OPTIONAL_SKIP_INGEST", "1")
os.environ.setdefault("OPTIONAL_RUN_INGEST_IN_MAIN", "0")
os.environ.setdefault("RANKING_ENTRY_CONTROLLER_TIMEOUT_SEC", "120")
os.environ.setdefault("RANKING_ENTRY_BUILD_TIMEOUT_SEC", "180")
os.environ.setdefault("NAS_SQLITE_IO_GUARD_COOLDOWN_SEC", "20")
os.environ.setdefault("TONOSAMA_VOL_ENTRYROW_RESCUE_ENABLED", "1")
os.environ.setdefault("TONOSAMA_VOL_ENTRYROW_RESCUE_MIN_RANGE_RATIO", "0.006")
os.environ.setdefault("TONOSAMA_VOL_ENTRYROW_RESCUE_MIN_INTRABAR_PCT", "0.6")
os.environ.setdefault("FINAL_ENTRY_TONOSAMA_SCORE_ONLY_MIN_SCORE", "2.3")
os.environ.setdefault("FINAL_ENTRY_TONOSAMA_DEDICATED_OK_MIN_SCORE", "2.3")
os.environ.setdefault("ENTRY_LUNCH_BLOCK_START", "11:30")
os.environ.setdefault("ENTRY_LUNCH_BLOCK_END", "12:30")
# RANKING pending は high/low が欠損しやすい。高スコア候補を no_high_low だけで全落ちさせない。
os.environ.setdefault("LOW_MOVE_RANKING_MIN_ATR_RATIO", "0.0020")
os.environ.setdefault("LOW_MOVE_RANKING_MIN_ABS_SLOPE", "0.0")
os.environ.setdefault("LOW_MOVE_RANKING_MIN_SCORE_FOR_NO_HIGHLOW", "70.0")
# 強いランキング候補は MA5 third-bar ガード単体で全落ちさせない。
os.environ.setdefault("ENTRY_MA5_THIRD_BAR_RANKING_STRONG_FAILOPEN", "1")
os.environ.setdefault("ENTRY_MA5_THIRD_BAR_RANKING_FAILOPEN_MIN_SCORE", "80.0")

try:
    from core.logging.console_tee import setup_console_tee, rebind_logging_streams_to_console_tee
    CONSOLE_LOG_PATH = setup_console_tee()
    print(f"[BOOT] console tee loaded: {CONSOLE_LOG_PATH}")
except Exception as e:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    logging.exception("console_tee import/setup failed: %s", e)
    CONSOLE_LOG_PATH = None
    def rebind_logging_streams_to_console_tee():
        return None

root = logging.getLogger()
root.setLevel(logging.INFO)
logger = logging.getLogger(__name__)
print("[BOOT] PROJECT_ROOT =", PROJECT_ROOT)

import time
import schedule
import traceback
import importlib
from threading import Thread
from configparser import ConfigParser
from collections import defaultdict

import websocket
websocket.enableTrace(False)
logging.getLogger("websocket").setLevel(logging.CRITICAL)

import trading.push.push_stream as push_stream
from core.startup.push_storage_bootstrap import start_push_storage
from optional.batch.optional_main import optional_main
from core.bootstrap.load_symbol_map import build_symbol_name_map
from global_state import global_data
from core.startup.startup import system_startup
from trading.summary.realtime_engine import init_realtime_engine, process_realtime
from trading.handlers.exit_handler import build_5s_bar_fast, run_exit_pipeline
from force_cancel_loop import start_force_cancel_loop
from test_script.test_force_exit import run_force_exit_test
from trading.positions.position_sync import PositionSyncManager
from ats.ats_register import ats_register_loop, show_should_register_symbols
from utils.business_day_utils import is_market_open
from core.runtime.stream_orchestrator import StreamOrchestrator

try:
    from scheduler_jobs.summary.scheduler import run_summary_tick_once
except Exception:
    run_summary_tick_once = None
try:
    from trading.signals.state.signal_state import SignalState
except Exception:
    SignalState = None
try:
    from trading.signals.state.prev_state import PrevSignalState
except Exception:
    PrevSignalState = None
try:
    from trading.signals.state.position_state import PositionState
except Exception:
    PositionState = None
try:
    from trading.runtime_persistence.heartbeat_watchdog import heartbeat, mark_component_start, mark_component_stop
except Exception:
    def heartbeat(*args, **kwargs): return None
    def mark_component_start(*args, **kwargs): return None
    def mark_component_stop(*args, **kwargs): return None

class _LazyFactoryDict(dict):
    def __init__(self, factory):
        super().__init__(); self._factory = factory
    def __missing__(self, key):
        value = self._factory(); self[key] = value; return value

def _factory_signal_state():
    if SignalState is None: return None
    try: return SignalState()
    except Exception: logger.exception("SignalState() create failed"); return None

def _factory_prev_state():
    if PrevSignalState is None: return None
    try: return PrevSignalState()
    except Exception: logger.exception("PrevSignalState() create failed"); return None

def _factory_position_state():
    if PositionState is None: return None
    try: return PositionState()
    except Exception: logger.exception("PositionState() create failed"); return None

def _install_main_runtime_patches():
    patches = [
        ("core.startup.nas_sqlite_io_guard_patch", "install"),
        ("core.startup.indicator_fragmentation_runtime_patch", "install"),
        ("core.startup.entry_controller_runtime_reject_patch", "install"),
        ("core.startup.fast_startup_runtime_patch", "install"),
        ("core.startup.summary_mtf_diff_from_1m_patch", "install"),
        ("core.startup.entry_qty_min_lot_runtime_patch", "install"),
        ("core.startup.low_movement_entry_guard_patch", "install"),
        ("core.startup.entry_final_filter_failopen_patch", "install"),
        ("core.startup.oneshot_limit_700k_patch", "install"),
        ("core.startup.entry_limit_passive_runtime_patch", "install"),
        ("core.startup.final_entry_safety_guard_patch", "install"),
        ("core.startup.entry_ma5_third_bar_slope_guard_patch", "install"),
        ("core.startup.summary_ai_more_candidates_patch", "install"),
        ("core.startup.summary_ai_async_entry_patch", "install"),
        ("core.startup.entry_order_mtf_slope_fill_patch", "install"),
        ("core.startup.summary_ai_entry_controller_bridge_patch", "install"),
        ("core.startup.discord_summary_display_compact_patch", "install"),
        ("core.startup.discord_summary_kwarg_safety_patch", "install"),
        ("core.startup.ranking_entry_flat_price_guard_patch", "install"),
        ("core.startup.board_retry_patch", "install"),
        ("core.startup.tonosama_5sec_stopped_relax_patch", "install"),
        ("core.startup.volatility_filter_tonosama_entryrow_rescue_patch", "install"),
        ("core.startup.board_wall_stall_exit_patch", "install"),
        ("core.startup.ranking_entry_controller_timeout_patch", "install"),
        ("core.startup.ranking_entry_filter_rescue_patch", "install"),
        ("core.startup.entry_log_skip_reason_collision_patch", "install"),
    ]
    for mod_name, fn_name in patches:
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name, None)
            ok = fn() if callable(fn) else False
            logger.warning("[MAIN RUNTIME PATCH] %s.%s installed=%s", mod_name, fn_name, ok)
            heartbeat("main_runtime_patch", status="OK" if ok else "NG", detail={"module": mod_name, "fn": fn_name})
        except Exception:
            heartbeat("main_runtime_patch", status="ERROR", detail={"module": mod_name, "fn": fn_name})
            logger.exception("[MAIN RUNTIME PATCH] failed %s.%s", mod_name, fn_name)

def _install_summary_entry_runtime_context():
    try:
        if not hasattr(global_data, "signal_state_map") or not isinstance(getattr(global_data, "signal_state_map", None), dict): global_data.signal_state_map = _LazyFactoryDict(_factory_signal_state)
        if not hasattr(global_data, "prev_state_map") or not isinstance(getattr(global_data, "prev_state_map", None), dict): global_data.prev_state_map = _LazyFactoryDict(_factory_prev_state)
        if not hasattr(global_data, "position_state_map") or not isinstance(getattr(global_data, "position_state_map", None), dict): global_data.position_state_map = _LazyFactoryDict(_factory_position_state)
        if not hasattr(global_data, "recent_realized_pnl_map") or not isinstance(getattr(global_data, "recent_realized_pnl_map", None), dict): global_data.recent_realized_pnl_map = defaultdict(float)
        if not hasattr(global_data, "summary_1m_df"): global_data.summary_1m_df = None
        if not hasattr(global_data, "summary_3m_df"): global_data.summary_3m_df = None
        if not hasattr(global_data, "summary_5m_df"): global_data.summary_5m_df = None
        if not hasattr(global_data, "discord_sender"): global_data.discord_sender = None
        if not hasattr(global_data, "discord_webhook_url"): global_data.discord_webhook_url = None
        def _get_summary_runtime_kwargs():
            return {"signal_state_map": getattr(global_data, "signal_state_map", {}), "prev_state_map": getattr(global_data, "prev_state_map", {}), "position_state_map": getattr(global_data, "position_state_map", {}), "recent_realized_pnl_map": getattr(global_data, "recent_realized_pnl_map", {}), "df_1m_summary": getattr(global_data, "summary_1m_df", None), "df_3m_summary": getattr(global_data, "summary_3m_df", None), "df_5m_summary": getattr(global_data, "summary_5m_df", None), "discord_sender": getattr(global_data, "discord_sender", None), "discord_webhook_url": getattr(global_data, "discord_webhook_url", None)}
        global_data.get_summary_runtime_kwargs = _get_summary_runtime_kwargs
        logger.info("✅ summary/entry runtime context installed (signal_state_map=%s prev_state_map=%s position_state_map=%s)", type(global_data.signal_state_map).__name__, type(global_data.prev_state_map).__name__, type(global_data.position_state_map).__name__)
        heartbeat("main_runtime_context", status="OK", detail={"signal_state_map": type(global_data.signal_state_map).__name__})
    except Exception:
        heartbeat("main_runtime_context", status="ERROR"); logger.exception("summary/entry runtime context install failed")

def _register_exit_scheduler():
    try:
        if not callable(run_exit_pipeline):
            logger.warning("[EXIT SCHEDULER] run_exit_pipeline unavailable"); heartbeat("exit_scheduler", status="NG", detail={"reason": "run_exit_pipeline unavailable"}); return False
        for job in list(schedule.jobs):
            try:
                fn = getattr(job, "job_func", None)
                if getattr(fn, "func", fn) is run_exit_pipeline:
                    logger.warning("[EXIT SCHEDULER] already registered"); heartbeat("exit_scheduler", status="OK", detail={"already_registered": True}); return True
            except Exception: pass
        schedule.every(1).seconds.do(run_exit_pipeline)
        logger.warning("[EXIT SCHEDULER] run_exit_pipeline registered every 1s"); heartbeat("exit_scheduler", status="OK", detail={"registered_every_sec": 1}); return True
    except Exception:
        heartbeat("exit_scheduler", status="ERROR"); logger.exception("[EXIT SCHEDULER] register failed"); return False

def _resolve_push_refresh_callable():
    candidates = [("trading.push.push_stream", "refresh_subscriptions"), ("trading.push.push_stream", "refresh_subscription"), ("trading.push.symbol_subscription_manager", "refresh_subscriptions"), ("trading.push.symbol_subscription_manager", "refresh_subscription"), ("trading.push.symbol_subscription_manager", "start_symbol_subscription_manager")]
    for mod_name, fn_name in candidates:
        try:
            mod = importlib.import_module(mod_name); fn = getattr(mod, fn_name, None)
            if callable(fn): logger.info("✅ push refresh callable resolved: %s.%s", mod_name, fn_name); return fn
        except Exception: logger.debug("push refresh callable resolve failed: %s.%s", mod_name, fn_name, exc_info=True)
    logger.warning("⚠ push refresh callable unresolved"); return None

def _install_push_refresh_callable():
    try:
        refresh_fn = _resolve_push_refresh_callable()
        if callable(refresh_fn):
            global_data.push_refresh_callable = refresh_fn; logger.info("✅ global_data.push_refresh_callable installed"); heartbeat("push_refresh_callable", status="OK", detail={"fn": getattr(refresh_fn, "__name__", repr(refresh_fn))})
        else:
            global_data.push_refresh_callable = None; logger.warning("⚠ global_data.push_refresh_callable not installed"); heartbeat("push_refresh_callable", status="NG")
    except Exception:
        heartbeat("push_refresh_callable", status="ERROR"); logger.exception("push_refresh_callable install failed")

def _start_push_stream_safely():
    try:
        start_fn = getattr(push_stream, "start_push_stream", None)
        if not callable(start_fn): logger.error("❌ push_stream.start_push_stream unavailable"); heartbeat("main_push_stream", status="NG", detail={"reason": "start_push_stream unavailable"}); return False
        refresh_fn = getattr(global_data, "push_refresh_callable", None)
        logger.info("🟡 starting push_stream with refresh_callable=%s", getattr(refresh_fn, "__name__", repr(refresh_fn)) if callable(refresh_fn) else None)
        heartbeat("main_push_stream", status="START", detail={"refresh_callable": getattr(refresh_fn, "__name__", repr(refresh_fn)) if callable(refresh_fn) else None})
        start_fn(refresh_callable=refresh_fn, enable_rotate=False); logger.info("✅ push_stream.start_push_stream started"); heartbeat("main_push_stream", status="OK"); return True
    except Exception:
        heartbeat("main_push_stream", status="ERROR"); logger.exception("push_stream start failed"); return False

def _run_initial_summary_tick_once():
    try:
        if callable(run_summary_tick_once): logger.info("🟡 initial summary tick once start"); heartbeat("initial_summary_tick", status="START"); run_summary_tick_once(); logger.info("✅ initial summary tick once done"); heartbeat("initial_summary_tick", status="OK")
        else: logger.warning("⚠ initial summary tick once skipped (run_summary_tick_once unavailable)"); heartbeat("initial_summary_tick", status="SKIP")
    except Exception: heartbeat("initial_summary_tick", status="ERROR"); logger.exception("initial summary tick once failed")

def _run_initial_ranking_tick_once():
    try:
        from trading.summary.ranking.runner import run_time_locked_jobs
        if callable(run_time_locked_jobs): logger.info("🟡 initial ranking tick once start via ranking.runner.run_time_locked_jobs"); heartbeat("initial_ranking_tick", status="START", detail={"via": "run_time_locked_jobs"}); result = run_time_locked_jobs(display=True); logger.info("✅ initial ranking tick once done via run_time_locked_jobs targets=%s", sorted(list(result.keys())) if isinstance(result, dict) else []); heartbeat("initial_ranking_tick", status="OK", detail={"via": "run_time_locked_jobs"}); return
    except Exception: heartbeat("initial_ranking_tick", status="ERROR", detail={"via": "run_time_locked_jobs"}); logger.exception("initial ranking tick via run_time_locked_jobs failed")
    try:
        from trading.ranking.summary.runner import run_ranking_summary_once
        if callable(run_ranking_summary_once): logger.info("🟡 initial ranking summary once start"); heartbeat("initial_ranking_tick", status="START", detail={"via": "run_ranking_summary_once"}); run_ranking_summary_once(); logger.info("✅ initial ranking summary once done"); heartbeat("initial_ranking_tick", status="OK", detail={"via": "run_ranking_summary_once"})
        else: logger.warning("⚠ initial ranking tick skipped (callable unavailable)"); heartbeat("initial_ranking_tick", status="SKIP")
    except Exception: heartbeat("initial_ranking_tick", status="ERROR", detail={"via": "run_ranking_summary_once"}); logger.exception("initial ranking summary tick failed")

def main():
    logger.warning("[MAIN] start")
    mark_component_start("main")
    try:
        _install_main_runtime_patches()
        _install_summary_entry_runtime_context()
        system_startup()
        try: rebind_logging_streams_to_console_tee(); logger.info("✅ logging streams rebound to console tee")
        except Exception: logger.exception("logging rebind failed")
        build_symbol_name_map()
        _install_push_refresh_callable()
        start_push_storage()
        _start_push_stream_safely()
        try:
            optional_main()
        except Exception:
            logger.exception("optional_main failed")
        try:
            init_realtime_engine()
        except Exception:
            logger.exception("init_realtime_engine failed")
        try:
            PositionSyncManager().start()
        except Exception:
            logger.exception("PositionSyncManager start failed")
        try:
            Thread(target=ats_register_loop, daemon=True).start()
        except Exception:
            logger.exception("ats_register_loop start failed")
        try:
            Thread(target=start_force_cancel_loop, daemon=True).start()
        except Exception:
            logger.exception("force_cancel_loop start failed")
        try:
            _register_exit_scheduler()
        except Exception:
            logger.exception("register exit scheduler failed")
        _run_initial_summary_tick_once()
        _run_initial_ranking_tick_once()
        heartbeat("main", status="RUNNING")
        while True:
            try:
                schedule.run_pending()
                process_realtime()
                time.sleep(0.2)
            except KeyboardInterrupt:
                break
            except Exception:
                logger.exception("main loop error")
                time.sleep(1.0)
    finally:
        mark_component_stop("main")
        logger.warning("[MAIN] stop")

if __name__ == "__main__":
    main()
