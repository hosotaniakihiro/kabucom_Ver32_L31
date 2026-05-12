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
# Version: Ver38.4-EXIT-SCHEDULER-REGISTERED
# ------------------------------------------------------------
# ✔ PROJECT_ROOT を最初に sys.path へ追加
# ✔ core.logging.console_tee を確実に import / setup
# ✔ stdout / stderr / print / traceback / logging を console_*.log に保存
# ✔ system_startup 後に logging StreamHandler を tee へ再接続
# ✔ 100368 SELL拒否後の entry_controller ログ補正 runtime patch を起動時install
# ✔ 起動高速化 runtime patch を起動時install
# ✔ EXIT scheduler を run_exit_pipeline で1秒ごとに登録
# ✔ 既存の起動処理は維持
# ============================================================

# ------------------------------------------------------------
# PROJECT_ROOT / logging / console tee（★絶対に最初）
# ------------------------------------------------------------
import os
import sys
import logging

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

try:
    from core.logging.console_tee import (
        setup_console_tee,
        rebind_logging_streams_to_console_tee,
    )

    CONSOLE_LOG_PATH = setup_console_tee()
    print(f"[BOOT] console tee loaded: {CONSOLE_LOG_PATH}")

except Exception as e:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.exception("console_tee import/setup failed: %s", e)

    CONSOLE_LOG_PATH = None

    def rebind_logging_streams_to_console_tee():
        return None


root = logging.getLogger()
root.setLevel(logging.INFO)

logger = logging.getLogger(__name__)

print("[BOOT] PROJECT_ROOT =", PROJECT_ROOT)


# ------------------------------------------------------------
# 標準ライブラリ
# ------------------------------------------------------------
import time
import schedule
import traceback
import importlib

from threading import Timer, Thread
from configparser import ConfigParser
from collections import defaultdict


# ------------------------------------------------------------
# WebSocket ログ抑制
# ------------------------------------------------------------
import websocket

websocket.enableTrace(False)
logging.getLogger("websocket").setLevel(logging.CRITICAL)


# ------------------------------------------------------------
# PROJECT IMPORT
# ------------------------------------------------------------
import trading.push.push_stream as push_stream

from core.startup.push_storage_bootstrap import start_push_storage

from optional.batch.optional_main import optional_main
from core.bootstrap.load_symbol_map import build_symbol_name_map

from global_state import global_data
from core.startup.startup import system_startup

from trading.summary.realtime_engine import (
    init_realtime_engine,
    process_realtime,
)

from trading.handlers.exit_handler import build_5s_bar_fast, run_exit_pipeline
from force_cancel_loop import start_force_cancel_loop
from test_script.test_force_exit import run_force_exit_test

from trading.positions.position_sync import PositionSyncManager

from ats.ats_register import (
    ats_register_loop,
    show_should_register_symbols,
)

from utils.business_day_utils import is_market_open
from core.runtime.stream_orchestrator import StreamOrchestrator


# ------------------------------------------------------------
# summary scheduler helper
# ------------------------------------------------------------
try:
    from scheduler_jobs.summary.scheduler import run_summary_tick_once
except Exception:
    run_summary_tick_once = None


# ------------------------------------------------------------
# signal runtime context
# ------------------------------------------------------------
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


# ============================================================
# LAZY STATE MAPS
# ============================================================

class _LazyFactoryDict(dict):
    """
    未登録 key アクセス時に factory() で自動生成する辞書。
    runner 側で map[symbol] 参照しても KeyError にしない。
    """

    def __init__(self, factory):
        super().__init__()
        self._factory = factory

    def __missing__(self, key):
        value = self._factory()
        self[key] = value
        return value


def _factory_signal_state():
    if SignalState is None:
        return None

    try:
        return SignalState()
    except Exception:
        logger.exception("SignalState() create failed")
        return None


def _factory_prev_state():
    if PrevSignalState is None:
        return None

    try:
        return PrevSignalState()
    except Exception:
        logger.exception("PrevSignalState() create failed")
        return None


def _factory_position_state():
    if PositionState is None:
        return None

    try:
        return PositionState()
    except Exception:
        logger.exception("PositionState() create failed")
        return None


# ============================================================
# RUNTIME PATCHES
# ============================================================

def _install_main_runtime_patches():
    """
    起動時の runtime patch をまとめてinstallする。

    - entry_controller_runtime_reject_patch:
        kabu API 100368後に ORDER_ID_EMPTY_RETRYABLE として扱わず、
        SELL_ORDER_REJECTED_BY_KABU_API としてログを分ける。

    - fast_startup_runtime_patch:
        起動直後の重い ranking 初回tickを抑止し、ranking summary jobの
        巨大DataFrame戻り値ログを抑制する。
    """
    patches = [
        ("core.startup.entry_controller_runtime_reject_patch", "install"),
        ("core.startup.fast_startup_runtime_patch", "install"),
    ]

    for mod_name, fn_name in patches:
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name, None)
            ok = fn() if callable(fn) else False
            logger.warning(
                "[MAIN RUNTIME PATCH] %s.%s installed=%s",
                mod_name,
                fn_name,
                ok,
            )
        except Exception:
            logger.exception("[MAIN RUNTIME PATCH] failed %s.%s", mod_name, fn_name)


# ============================================================
# SUMMARY / ENTRY RUNTIME CONTEXT
# ============================================================

def _install_summary_entry_runtime_context():
    """
    scheduler 側 / summary job 側 / entry bridge 側から参照する
    共通 runtime context を global_data へ積む。
    """
    try:
        if not hasattr(global_data, "signal_state_map") or not isinstance(
            getattr(global_data, "signal_state_map", None),
            dict,
        ):
            global_data.signal_state_map = _LazyFactoryDict(_factory_signal_state)

        if not hasattr(global_data, "prev_state_map") or not isinstance(
            getattr(global_data, "prev_state_map", None),
            dict,
        ):
            global_data.prev_state_map = _LazyFactoryDict(_factory_prev_state)

        if not hasattr(global_data, "position_state_map") or not isinstance(
            getattr(global_data, "position_state_map", None),
            dict,
        ):
            global_data.position_state_map = _LazyFactoryDict(_factory_position_state)

        if not hasattr(global_data, "recent_realized_pnl_map") or not isinstance(
            getattr(global_data, "recent_realized_pnl_map", None),
            dict,
        ):
            global_data.recent_realized_pnl_map = defaultdict(float)

        if not hasattr(global_data, "summary_1m_df"):
            global_data.summary_1m_df = None

        if not hasattr(global_data, "summary_3m_df"):
            global_data.summary_3m_df = None

        if not hasattr(global_data, "summary_5m_df"):
            global_data.summary_5m_df = None

        if not hasattr(global_data, "discord_sender"):
            global_data.discord_sender = None

        if not hasattr(global_data, "discord_webhook_url"):
            global_data.discord_webhook_url = None

        def _get_summary_runtime_kwargs():
            return {
                "signal_state_map": getattr(global_data, "signal_state_map", {}),
                "prev_state_map": getattr(global_data, "prev_state_map", {}),
                "position_state_map": getattr(global_data, "position_state_map", {}),
                "recent_realized_pnl_map": getattr(global_data, "recent_realized_pnl_map", {}),
                "df_1m_summary": getattr(global_data, "summary_1m_df", None),
                "df_3m_summary": getattr(global_data, "summary_3m_df", None),
                "df_5m_summary": getattr(global_data, "summary_5m_df", None),
                "discord_sender": getattr(global_data, "discord_sender", None),
                "discord_webhook_url": getattr(global_data, "discord_webhook_url", None),
            }

        global_data.get_summary_runtime_kwargs = _get_summary_runtime_kwargs

        logger.info(
            "✅ summary/entry runtime context installed "
            "(signal_state_map=%s prev_state_map=%s position_state_map=%s)",
            type(global_data.signal_state_map).__name__,
            type(global_data.prev_state_map).__name__,
            type(global_data.position_state_map).__name__,
        )

    except Exception:
        logger.exception("summary/entry runtime context install failed")


# ============================================================
# EXIT SCHEDULER
# ============================================================

def _register_exit_scheduler():
    """
    EXIT パイプラインを schedule に登録する。
    これが未登録だと、ENTRY は成功しても EXIT 判定が一切回らない。
    """
    try:
        if not callable(run_exit_pipeline):
            logger.warning("[EXIT SCHEDULER] run_exit_pipeline unavailable")
            return False

        # 二重登録防止: schedule.Job には job_func が存在する
        for job in list(schedule.jobs):
            try:
                fn = getattr(job, "job_func", None)
                if getattr(fn, "func", fn) is run_exit_pipeline:
                    logger.warning("[EXIT SCHEDULER] already registered")
                    return True
            except Exception:
                pass

        schedule.every(1).seconds.do(run_exit_pipeline)
        logger.warning("[EXIT SCHEDULER] run_exit_pipeline registered every 1s")
        return True

    except Exception:
        logger.exception("[EXIT SCHEDULER] register failed")
        return False


# ============================================================
# PUSH REFRESH CALLABLE RESOLVER
# ============================================================

def _resolve_push_refresh_callable():
    candidates = [
        ("trading.push.push_stream", "refresh_subscriptions"),
        ("trading.push.push_stream", "refresh_subscription"),
        ("trading.push.symbol_subscription_manager", "refresh_subscriptions"),
        ("trading.push.symbol_subscription_manager", "refresh_subscription"),
        ("trading.push.symbol_subscription_manager", "start_symbol_subscription_manager"),
    ]

    for mod_name, fn_name in candidates:
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, fn_name, None)

            if callable(fn):
                logger.info(
                    "✅ push refresh callable resolved: %s.%s",
                    mod_name,
                    fn_name,
                )
                return fn

        except Exception:
            logger.debug(
                "push refresh callable resolve failed: %s.%s",
                mod_name,
                fn_name,
                exc_info=True,
            )

    logger.warning("⚠ push refresh callable unresolved")
    return None


def _install_push_refresh_callable():
    try:
        refresh_fn = _resolve_push_refresh_callable()

        if callable(refresh_fn):
            global_data.push_refresh_callable = refresh_fn
            logger.info("✅ global_data.push_refresh_callable installed")
        else:
            global_data.push_refresh_callable = None
            logger.warning("⚠ global_data.push_refresh_callable not installed")

    except Exception:
        logger.exception("push_refresh_callable install failed")


# ============================================================
# PUSH STREAM STARTER
# ============================================================

def _start_push_stream_safely():
    try:
        start_fn = getattr(push_stream, "start_push_stream", None)

        if not callable(start_fn):
            logger.error("❌ push_stream.start_push_stream unavailable")
            return False

        refresh_fn = getattr(global_data, "push_refresh_callable", None)

        logger.info(
            "🟡 starting push_stream with refresh_callable=%s",
            getattr(refresh_fn, "__name__", repr(refresh_fn))
            if callable(refresh_fn)
            else None,
        )

        start_fn(
            refresh_callable=refresh_fn,
            enable_rotate=False,
        )

        logger.info("✅ push_stream.start_push_stream started")
        return True

    except Exception:
        logger.exception("push_stream start failed")
        return False


# ============================================================
# INITIAL SUMMARY TICK
# ============================================================

def _run_initial_summary_tick_once():
    """
    scheduler 登録直後に 1回だけ summary tick を実行する。
    :00 ちょうどで登録完了した場合、その回を取り逃すことがあるため。
    """
    try:
        if callable(run_summary_tick_once):
            logger.info("🟡 initial summary tick once start")
            run_summary_tick_once()
            logger.info("✅ initial summary tick once done")
        else:
            logger.warning(
                "⚠ initial summary tick once skipped "
                "(run_summary_tick_once unavailable)"
            )

    except Exception:
        logger.exception("initial summary tick once failed")


# ============================================================
# INITIAL RANKING TICK
# ============================================================

def _run_initial_ranking_tick_once():
    """
    scheduler 登録直後に 1回だけ ranking 系の初回 tick を実行する。

    優先順位:
      1) trading.summary.ranking.runner.run_time_locked_jobs
      2) trading.summary.ranking.runner.run_ranking_summary_job(interval=1)
      3) trading.ranking.scheduler.job_save_ranking
    """
    try:
        from trading.summary.ranking.runner import run_time_locked_jobs

        if callable(run_time_locked_jobs):
            logger.info(
                "🟡 initial ranking tick once start "
                "via ranking.runner.run_time_locked_jobs"
            )
            result = run_time_locked_jobs(display=True)
            logger.info(
                "✅ initial ranking tick once done "
                "via run_time_locked_jobs targets=%s",
                sorted(list(result.keys())) if isinstance(result, dict) else [],
            )
            return

    except Exception:
        logger.exception("initial ranking tick via run_time_locked_jobs failed")

    try:
        from trading.summary.ranking.runner import run_ranking_summary_job

        if callable(run_ranking_summary_job):
            logger.info(
                "🟡 initial ranking tick once start "
                "via ranking.runner.run_ranking_summary_job(interval=1)"
            )
            df = run_ranking_summary_job(interval=1, display=True)
            logger.info(
                "✅ initial ranking tick once done "
                "via run_ranking_summary_job rows=%s",
                len(df) if hasattr(df, "__len__") else None,
            )
            return

    except Exception:
        logger.exception("initial ranking tick via run_ranking_summary_job failed")

    try:
        from trading.ranking.scheduler import job_save_ranking

        if callable(job_save_ranking):
            logger.info(
                "🟡 initial ranking tick fallback start "
                "via trading.ranking.scheduler.job_save_ranking"
            )
            job_save_ranking()
            logger.info("✅ initial ranking tick fallback done via job_save_ranking")
        else:
            logger.warning("⚠ initial ranking tick skipped (job_save_ranking unavailable)")

    except Exception:
        logger.exception("initial ranking tick fallback failed")


# ============================================================
# Scheduler Loop
# ============================================================

def scheduler_loop():
    logger.info("⏱ Scheduler loop START")

    while True:
        try:
            schedule.run_pending()
        except Exception:
            logger.exception("[scheduler_loop]")

        time.sleep(0.5)


# ============================================================
# EXIT DEBUG
# ============================================================

def debug_exit_status():
    try:
        logger.info("========== EXIT DEBUG ==========")

        getter = getattr(global_data, "get_push_df", None)
        df = getter() if callable(getter) else None

        rows = 0 if df is None else len(df)

        logger.info("push_df rows=%d", rows)

        if rows > 0:
            logger.info("\n%s", df.tail(3))

        open_positions = getattr(global_data, "open_positions", [])
        logger.info("open_positions=%s", open_positions)

        for sym in list(open_positions):
            try:
                bar = build_5s_bar_fast(sym)
                logger.info("5s bar [%s] = %s", sym, bar)
            except Exception:
                logger.exception("5s bar error [%s]", sym)

        logger.info("======== END EXIT DEBUG ========")

    except Exception:
        logger.exception("[debug_exit_status] fatal")


# ============================================================
# PUSH MONITOR
# ============================================================

def monitor_push_df():
    i = 0

    while True:
        try:
            getter = getattr(global_data, "get_push_df", None)
            df = getter() if callable(getter) else None

            rows = 0 if df is None else len(df)

            logger.info("[PUSH MONITOR] %d rows=%d", i, rows)

            if rows > 0:
                logger.info("\n%s", df.tail(2))

            i += 1

            time.sleep(3)

        except Exception:
            logger.exception("[monitor_push_df] error")
            time.sleep(3)


# ============================================================
# POSITION SYNC LOOP
# ============================================================

def start_position_sync_loop(pos_sync: PositionSyncManager):
    while True:
        try:
            pos_sync.maybe_sync()
        except Exception:
            logger.exception("[PositionSync] error")

        time.sleep(5)


# ============================================================
# SHOULD REGISTER MONITOR
# ============================================================

def should_register_monitor_loop(interval_sec: int = 30):
    logger.info(
        "📋 SHOULD REGISTER monitor START (interval=%s sec)",
        interval_sec,
    )

    while True:
        try:
            logger.info("📋 SHOW SHOULD REGISTER SYMBOLS (PERIODIC)")
            show_should_register_symbols()
        except Exception:
            logger.exception("[should_register_monitor_loop] error")

        time.sleep(interval_sec)


# ============================================================
# MAIN
# ============================================================

def main():
    conf = ConfigParser()
    conf.read("settings.ini", encoding="utf-8")

    force_run = conf.getboolean("test", "force_run", fallback=False)

    logger.info("========== SYSTEM BOOT START ==========")
    logger.info("PROJECT_ROOT=%s", PROJECT_ROOT)
    logger.info("CONSOLE_LOG_PATH=%s", CONSOLE_LOG_PATH)
    logger.info("force_run=%s", force_run)

    _install_summary_entry_runtime_context()
    _install_main_runtime_patches()

    # --------------------------------------------------------
    # OPTIONAL
    # --------------------------------------------------------
    try:
        logger.info("🔧 optional boot START")

        optional_main()

        optional_data = getattr(global_data, "optional_data", None)

        if optional_data is not None:
            try:
                logger.info("optional_data rows=%s cols=%s", len(optional_data), list(optional_data.columns))
                logger.info("optional_data head=\n%s", optional_data.head())
            except Exception:
                logger.exception("optional_data print failed")

        logger.info("✅ optional boot DONE")

    except Exception:
        logger.critical("❌ optional boot FAILED → system abort")
        traceback.print_exc()
        sys.exit(1)

    # --------------------------------------------------------
    # SYMBOL NAME MAP BUILD
    # --------------------------------------------------------
    try:
        logger.info("🔧 building symbol_name_map")

        build_symbol_name_map()

        symbol_name_map = getattr(global_data, "symbol_name_map", {})

        logger.info(
            "symbol_name_map size=%d",
            len(symbol_name_map) if symbol_name_map is not None else 0,
        )
        logger.info(
            "✅ symbol_name_map loaded (%d)",
            len(symbol_name_map) if symbol_name_map is not None else 0,
        )

    except Exception:
        logger.exception("symbol_name_map build failed")

    # --------------------------------------------------------
    # STARTUP
    # --------------------------------------------------------
    system_startup()

    try:
        rebind_logging_streams_to_console_tee()
    except Exception:
        logger.exception("console tee rebind failed after system_startup")

    logger.info("🚀 system_startup DONE")

    _install_summary_entry_runtime_context()
    _install_main_runtime_patches()

    # --------------------------------------------------------
    # PUSH REFRESH CALLABLE INSTALL
    # --------------------------------------------------------
    _install_push_refresh_callable()

    # --------------------------------------------------------
    # PUSH SYSTEM START
    # --------------------------------------------------------
    push_started = _start_push_stream_safely()

    if not push_started:
        logger.warning("⚠ push stream start returned False")

    try:
        start_push_storage()
        logger.info("✅ push storage started")
    except Exception:
        logger.exception("push storage start failed")

    # --------------------------------------------------------
    # EXIT scheduler register
    # --------------------------------------------------------
    _register_exit_scheduler()

    # --------------------------------------------------------
    # Scheduler loop start
    # --------------------------------------------------------
    Thread(
        target=scheduler_loop,
        daemon=True,
        name="scheduler_loop",
    ).start()

    logger.info("✅ scheduler loop started")

    # --------------------------------------------------------
    # initial summary / ranking tick once
    # --------------------------------------------------------
    _run_initial_summary_tick_once()
    _run_initial_ranking_tick_once()

    # --------------------------------------------------------
    # HOLIDAY GUARD
    # --------------------------------------------------------
    if not is_market_open() and not force_run:
        try:
            logger.info("📋 SHOW SHOULD REGISTER SYMBOLS (HOLIDAY MODE)")
            show_should_register_symbols()
        except Exception:
            logger.exception("show_should_register_symbols failed")

        Thread(
            target=should_register_monitor_loop,
            args=(30,),
            daemon=True,
            name="should_register_monitor_holiday",
        ).start()

        logger.info("🧊 HOLIDAY MODE ACTIVE")
        logger.info("🟡 Scheduler is running for summary display / monitoring")
        logger.info("🛑 Realtime / ATS / Entry main flow NOT started")

        while True:
            time.sleep(60)

    # --------------------------------------------------------
    # Realtime Engine
    # --------------------------------------------------------
    init_realtime_engine()

    logger.info("⚡ realtime_engine initialized")

    # --------------------------------------------------------
    # StreamOrchestrator
    # --------------------------------------------------------
    stream = StreamOrchestrator()

    Thread(
        target=stream.start,
        daemon=True,
        name="stream_orchestrator",
    ).start()

    logger.info("🌊 StreamOrchestrator started")

    # --------------------------------------------------------
    # Position Sync
    # --------------------------------------------------------
    pos_sync = PositionSyncManager()

    Thread(
        target=start_position_sync_loop,
        args=(pos_sync,),
        daemon=True,
        name="position_sync_loop",
    ).start()

    logger.info("✅ position sync loop started")

    # --------------------------------------------------------
    # ATS候補一覧を起動時に1回表示
    # --------------------------------------------------------
    try:
        logger.info("📋 SHOW SHOULD REGISTER SYMBOLS (BEFORE ATS START)")
        show_should_register_symbols()
    except Exception:
        logger.exception("show_should_register_symbols failed")

    # --------------------------------------------------------
    # SHOULD REGISTER MONITOR
    # --------------------------------------------------------
    Thread(
        target=should_register_monitor_loop,
        args=(30,),
        daemon=True,
        name="should_register_monitor",
    ).start()

    logger.info("✅ should-register monitor started")

    # --------------------------------------------------------
    # ATS
    # --------------------------------------------------------
    token_value = getattr(global_data, "token_value", None)

    if token_value:
        Thread(
            target=ats_register_loop,
            args=(token_value,),
            daemon=True,
            name="ats_register_loop",
        ).start()

        logger.info("✅ ATS register loop started")

    else:
        logger.warning(
            "⚠ ATS register loop skipped "
            "(global_data.token_value missing)"
        )

    # --------------------------------------------------------
    # Debug & Monitor
    # --------------------------------------------------------
    Timer(10, debug_exit_status).start()

    Thread(
        target=monitor_push_df,
        daemon=True,
        name="push_monitor_df",
    ).start()

    Thread(
        target=start_force_cancel_loop,
        daemon=True,
        name="force_cancel_loop",
    ).start()

    Timer(12, run_force_exit_test).start()

    logger.info("🔥 REALTIME MAIN LOOP START")

    while True:
        try:
            process_realtime()
        except Exception:
            logger.exception("[REALTIME LOOP ERROR]")

        time.sleep(0.2)


# ============================================================
# Entry Point
# ============================================================

if __name__ == "__main__":
    main()
