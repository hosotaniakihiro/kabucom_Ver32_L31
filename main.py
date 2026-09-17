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
# Version: Ver38.39-SUMMARY-AI-DIRECT-DISPATCH-V5-INSTALL
# ------------------------------------------------------------
# ✔ SUMMARY AI direct dispatch V5 を main runtime patch 一覧へ明示追加
# ✔ SUMMARY AI direct snapshot の reentrant lock を main.py 起動時に強制有効化
# ✔ entry_controller_lock_timeout で SUMMARY AI 候補が発注直前に落ちる問題を抑止
# ✔ main.py はPUSH DB保存なしのまま、PUSHメモリから最新1分足summaryを高速生成
# ✔ 発注直前の出来高・売買代金・値動きハードガードを追加
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
os.environ.setdefault("LOW_MOVE_RANKING_MIN_ATR_RATIO", "0.0020")
os.environ.setdefault("LOW_MOVE_RANKING_MIN_ABS_SLOPE", "0.0")
os.environ.setdefault("LOW_MOVE_RANKING_MIN_SCORE_FOR_NO_HIGHLOW", "70.0")
os.environ.setdefault("ENTRY_MA5_THIRD_BAR_RANKING_STRONG_FAILOPEN", "1")
os.environ.setdefault("ENTRY_MA5_THIRD_BAR_RANKING_FAILOPEN_MIN_SCORE", "80.0")
os.environ.setdefault("ENTRY_HARD_LIQUIDITY_MOVEMENT_GUARD_ENABLED", "1")
os.environ.setdefault("ENTRY_HARD_MIN_VOLUME", "100000")
os.environ.setdefault("ENTRY_HARD_MIN_TURNOVER", "50000000")
os.environ.setdefault("ENTRY_HARD_REQUIRE_MOVEMENT", "1")
os.environ.setdefault("ENTRY_HARD_MIN_RANGE_PCT", "0.006")
os.environ.setdefault("ENTRY_HARD_MIN_ATR_RATIO", "0.003")
os.environ.setdefault("ENTRY_HARD_MIN_ABS_SLOPE", "0.001")
# SUMMARY AI は entry_controller._pipeline_lock 保持中に direct snapshot へ入るため、
# 起動時に必ず reentrant 実行を許可する。setdefault では過去の環境値 0 が残るため強制する。
os.environ["SUMMARY_AI_DIRECT_SNAPSHOT_REENTRANT_LOCK"] = "1"
os.environ.setdefault("SUMMARY_AI_DIRECT_ENTRY_SNAPSHOT", "1")
os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY", "1")
os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC", "1")
os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_LATEST_ONLY", "1")
os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", "1")

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
from core.startup.push.storage_bootstrap import start_push_storage
from optional.batch.optional_main import optional_main
from core.bootstrap.load_symbol_map import build_symbol_name_map
from global_state import global_data
from core.startup.startup import system_startup
from trading.summary.realtime_engine import init_realtime_engine, process_realtime
from trading.handlers.exit_handler import build_5s_bar_fast, run_exit_pipeline
from force_cancel_loop import start_force_cancel_loop
from test_script.test_force_exit import run_force_exit_test
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
    def heartbeat(*args, **kwargs):
        return None

    def mark_component_start(*args, **kwargs):
        return None

    def mark_component_stop(*args, **kwargs):
        return None


class _LazyFactoryDict(dict):
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


def _install_main_runtime_patches():
    try:
        from trading.handlers.entry_controller import cleanup_stale_100368_sell_reject_cache

        cleanup_stale_100368_sell_reject_cache()
    except Exception:
        logger.exception("[MAIN RUNTIME PATCH] stale 100368 sell reject cache cleanup failed")

    def _apply_summary_ai_threshold_env() -> None:
        """旧 entry_qty_min_lot_runtime_patch._install_summary_ai_threshold_env()。

        しきい値 env の既定値。旧パッチの install() 冒頭で毎回実行されていたので、
        patches リストの処理直前という同じ位置で適用する。
        """
        # ============================================================
        # P0-3 settings owner 一本化
        # ------------------------------------------------------------
        # ここは以前、閾値の「緩い」既定値を setdefault していた:
        #   SUMMARY_AI_MIN_BUY=1.00 / MIN_CONF=0.55 / MIN_COMPOSITE_SCORE_BUY=0.50
        # その後 strict_entry_defaults.STRICT_ENV が
        #   3.00 / 0.60 / 3.00
        # へ上書きするため、起動ログ上は
        #   1.0/0.55 -> 3.0/0.60
        # と閾値が動いて見えていた (2026-08-28 15:23 起動でも再現)。
        #
        # core/startup/startup.py の SETTINGS SOURCE OF TRUTH 表でも
        # これらの owner は strict_entry_defaults.STRICT_ENV と宣言されている。
        # つまりここは **二重の owner** であり、
        # 起動順によっては緩い値が読まれうる (fail-open 側)。
        #
        # 閾値の owner は strict_entry_defaults 1 か所にする。
        # ここでは閾値系 env を一切設定しない。
        # 閾値そのものは変更していない (最終値は従来どおり STRICT の値)。
        #
        # どうしても旧挙動へ戻す場合だけ
        #   SUMMARY_AI_THRESHOLD_LEGACY_SEED=1
        # を指定する。
        # ============================================================
        legacy = str(
            os.environ.get("SUMMARY_AI_THRESHOLD_LEGACY_SEED", "") or ""
        ).strip().lower() in {"1", "true", "yes", "y", "on"}
        if not legacy:
            logger.warning(
                "[SUMMARY AI THRESHOLD ENV PATCH] skipped: 閾値 env は "
                "strict_entry_defaults.STRICT_ENV が唯一の owner。"
                "ここでは設定しない (起動順で閾値が変わらないようにするため)。"
                "旧挙動は SUMMARY_AI_THRESHOLD_LEGACY_SEED=1"
            )
            return

        defaults = {
            "SUMMARY_AI_MIN_BUY": "1.00",
            "SUMMARY_AI_MIN_SELL": "1.00",
            "SUMMARY_AI_MIN_CONF": "0.55",
            "MIN_ENTRY_SCORE_BUY_SUMMARY": "1.00",
            "MIN_ENTRY_SCORE_SELL_SUMMARY": "1.00",
            "MIN_SUMMARY_SCORE_BUY": "1.00",
            "MIN_SUMMARY_SCORE_SELL": "1.00",
            "MIN_COMPOSITE_SCORE_BUY": "0.50",
            "MIN_COMPOSITE_SCORE_SELL": "0.80",
            "SUMMARY_AI_TOP_N": "20",
        }
        for k, v in defaults.items():
            os.environ.setdefault(k, v)
        logger.warning(
            "[SUMMARY AI THRESHOLD ENV PATCH] installed (LEGACY SEED) %s", defaults
        )

    _apply_summary_ai_threshold_env()

    def _fast_startup_background_heavy() -> None:
        """旧 fast_startup_runtime_patch._background_heavy()。

        重い install を別スレッドへ逃がす。symbol_flags のキャッシュ構築のあと、
        direct_push_1m を再適用して job_summary を包み直す
        (他パッチが後から job_summary をラップしても直販路を残すため)。
        旧 jobs リストのうち summary_mtf_push_raw_fallback_patch は
        モジュールごと本文化済みで存在しないため落とした。
        """
        try:
            from core.startup.bootstrap.symbol_flags import install_symbol_flags_cache

            ok = bool(install_symbol_flags_cache(force=True))
            logger.warning("[FAST STARTUP] symbol_flags_bootstrap installed=%s", ok)
        except Exception:
            logger.exception("[FAST STARTUP] symbol_flags_bootstrap install failed")

        for label in ("bg", "bg-final"):
            try:
                from scheduler_jobs.summary import direct_push_1m

                logger.warning("[FAST STARTUP] direct_push_1m:%s installed=%s", label, bool(direct_push_1m.install()))
            except Exception:
                logger.exception("[FAST STARTUP] direct_push_1m:%s install failed", label)
        logger.warning("[FAST STARTUP] background heavy installs done")

    def _start_fast_startup_background_heavy() -> bool:
        """FAST_STARTUP_ASYNC_HEAVY_PATCHES=0 のときだけ同期実行する。"""
        raw = os.getenv("FAST_STARTUP_ASYNC_HEAVY_PATCHES")
        async_heavy = True
        if raw is not None and str(raw).strip() != "":
            async_heavy = str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
        if async_heavy:
            Thread(
                target=_fast_startup_background_heavy,
                name="fast-startup-heavy-installs",
                daemon=True,
            ).start()
            logger.warning("[FAST STARTUP] heavy installs scheduled background")
        else:
            _fast_startup_background_heavy()
        return True

    patches = [
        # fast_startup_runtime_patch は本文化して解体済み。
        # 旧 install() の並びをそのままこの位置に展開してある。
        #   - _patch_summary_schema_bootstrap
        #       -> database/session.apply_fast_startup_schema_skip()
        #   - _patch_entry_max_approved
        #       -> trading/handlers/entry_controller.apply_fast_startup_entry_limits()
        #   - _patch_ranking_bootstrap
        #       -> core/startup/scheduler/bootstrap.apply_fast_startup_ranking_defaults()
        #         (併せて掛かっていた _run_ranking_summary_all_job_safe の
        #          「戻り値を捨てる」ラッパーは同関数本体へ畳み込み済み)
        #   - _background_heavy
        #       -> 直下の _start_fast_startup_background_heavy()
        # 旧 install が呼んでいた summary_parallel_intervals_runtime_patch /
        # entry_limit_passive_runtime_patch / summary_mtf_push_raw_fallback_patch は
        # いずれも本文化でモジュールごと消えており、import 失敗で毎回 NG に
        # なるだけの死に呼び出しだったため復元していない。
        # summary_parallel_main_scope.install() は _INSTALLED で冪等なので、
        # 旧 pre/post の 2 回呼びは 1 回へ畳んだ。
        ("core.startup.summary_parallel_main_scope", "install"),
        ("scheduler_jobs.summary.light_tick_1m", "install"),
        ("database.session", "apply_fast_startup_schema_skip"),
        ("trading.handlers.entry_controller", "apply_fast_startup_entry_limits"),
        ("core.startup.scheduler.bootstrap", "apply_fast_startup_ranking_defaults"),
        ("scheduler_jobs.summary.direct_push_1m", "install"),
        _start_fast_startup_background_heavy,
        # 最後にもう一度同期で direct_push_1m を再適用する。
        # 上の install 群が job_summary を包み直している可能性があるため
        # (旧 install() 末尾の "direct_push_1m:final" と同じ意図)。
        ("scheduler_jobs.summary.direct_push_1m", "install"),
        # summary_scheduler_shutdown_guard_patch は
        # scheduler_jobs/summary/scheduler.py へ本文化済みのため削除
        # (_run_with_timeout/_invoke_job/_run_push_summary_tick/_run_ranking_summary_tick/
        # _run_summary_tick/_run_push_fallback_when_unified_blocked/_push_fallback_worker/
        # run_summary_tick_once の8関数を shutdown/executor-recovery guard 込みで公開名に維持)。
        # nas_sqlite_io_guard_patch は各ホストへ本文化済み:
        #   utils/nas_sqlite_io_guard.py (共有ネガティブキャッシュ+スコープ判定) を
        #   ranking_selector._load_ranking_df /
        #   ranking_source_selector.read_ranking_df_from_db /
        #   filters.read_symbol_flags_target_sets / read_symbol_flags_keep_set /
        #   ats.ats_ranking.db_path._db_has_usable_ranking_tables
        #   の各公開関数が直接使う。
        # entry_controller_runtime_reject_patch は trading/handlers/entry_controller.py の
        # cleanup_stale_100368_sell_reject_cache() へ native化済み (直接呼び出しは下記)。
        ("scheduler_jobs.summary.memory_latest_1m", "install"),
        ("trading.summary.mtf_diff_from_1m", "install"),
        # entry_qty_min_lot_runtime_patch は3責務に分割して本文化済み:
        #   - 発注数量の最小単元フォールバック -> trading/handlers/entry_controller.py
        #   - PIPE SUMMARY 表示整形            -> core/global_context/context.py
        #   - SUMMARY AI しきい値の env 既定値 -> 直下の _apply_summary_ai_threshold_env()
        ("trading.entry.low_movement_guard", "install"),
        # entry_final_filter_failopen_patch は本文化済み:
        #   env 既定値 53 件 + entry_order_builder の 4 定数 + 連鎖 install 2 件
        #     -> trading/handlers/entry_order_builder.apply_entry_final_filter_defaults()
        #   range_5m_filter の fail-open ラップ
        #     -> trading/handlers/entry_controller.range_5m_filter (low_movement の外側)
        #   atr_1m_filter のラップ
        #     -> tonosama rescue に巻き戻されて到達しない死にラッパーだったので撤去
        # 位置は旧 install と同じ (定数を書き戻す watcher との順序があるため)。
        ("trading.handlers.entry_order_builder", "apply_entry_final_filter_defaults"),
        # oneshot_limit_700k_patch は本文化済み:
        #   env 既定値 45 件 + MAX_ONESHOT=700000 + entry_controller の
        #   スコアしきい値 4 定数 + 生きている連鎖 install 3 件
        #     -> kabu_api.buy_sell_entry.apply_oneshot_limit_defaults()
        #   他 15 個のサブ install は本文化済みの空スタブか env のみだった。
        # 位置は旧 install と同じ (しきい値を書き戻す watcher との順序があるため)。
        ("kabu_api.buy_sell_entry", "apply_oneshot_limit_defaults"),
        # entry_limit_passive_runtime_patch は
        # trading/handlers/entry_order_builder.py へ本文化済み (_elp_*)。
        # 定数の上書きと _aggressive_limit_price の差し替えは
        # 同ファイルの _elp_apply_constants() が import 時に行う。
        # discord_summary_display_compact_patch / discord_summary_kwarg_safety_patch は
        # scheduler_jobs/summary/display.py 本体へインライン化済みのため削除。
        # entry_ma5_third_bar_slope_guard_patch は entry_controller._build_scored_candidates
        # 本体へインライン化済みのため削除。
        # summary_ai_more_candidates_patch は trading/entry/summary_ai/more_candidates.py へ移設した
        # (runner の候補数拡張 + スコア clamp と low move pool refill だけが残っている。
        #  final gate relax 定数 -> entry_controller.apply_final_gate_relax_constants()、
        #  range_pct 補修 / SELL short_ok 事前フィルタ -> candidates.py 本体へ本文化済み)。
        ("trading.entry.summary_ai.more_candidates", "install"),
        ("trading.entry.summary_ai.direct_snapshot", "apply_async_entry_defaults"),
        # async entry が executor を差し替えた後で、V5 direct dispatch を必ず重ねる。
        # summary_ai_async_direct_dispatch_patch は本文化済み:
        #   _positive_result の strict 化 -> trading/entry/summary_ai/executor.py
        #   (_strict_result_executed / _strict_positive_enabled)。
        #   price floor 2500 の上書きは 2026-08-11 のユーザー判断
        #   「エントリー最低価格は200円」により移植せず破棄した。
        # entry_order_mtf_slope_fill_patch は
        # trading/handlers/entry_order_builder.py へ本文化済み (_mtf_*)。
        # 旧 install が連鎖していた 2 件をここへ移した
        # (summary_no_overlap は本文化済みで空スタブ)。
        # entry_order_short_mtf_neutral_rescue_patch は
        # trading/handlers/entry_order_builder.py へ本文化済み (_smr_*)。
        # build_entry_order のラッパーは本文に組み込んだが、guard の
        # 追いラップだけは旧 install と同じこの位置で掛ける必要がある
        # (entry_ma5_breakout_count_patch より後 = 外側でないと
        #  ma5 の NG を解除できなくなるため)。
        ("trading.handlers.entry_order_builder", "apply_short_mtf_guard_wraps"),
        # summary_ai_entry_controller_bridge_patch は本文化済みなので削除した。
        #   ai_final_entry_check ラッパー -> trading/handlers/entry_controller.py (_bri_*)
        #   _result_executed 厳格判定   -> trading/summary/pipeline/entry_pipeline.py
        #                                  trading/summary/summary_entry.py
        ("kabu_api.board_retry", "apply_env_defaults"),
        ("core.startup.board_safety_defaults", "install"),
        # tonosama_5sec_stopped_relax_patch は
        # trading/entry/tonosama/pending_writer.py へ本文化済み
        # (_five_sec_relax_stage)。main.py 専用スコープは
        # pending_writer._five_sec_relax_scope_enabled() が再現している。
        # volatility_filter_tonosama_entryrow_rescue_patch は
        # trading/filters/volatility_filter.py の atr_1m_filter へ本文化済み (_tvr_*)。
        # 実 install 順の測定で、この層が atr_1m_filter を包む 6 パッチの中で
        # 最後に入り native の上へ載せ直す = 実質の最内層だと確認したうえで畳んだ。
        # entry_controller.py:121 の from-import が最初からこの実装を掴む。
        # board_wall_stall_exit_patch は trading/handlers/exit_handler.py へ本文化済み
        # (_bfe_* / run_exit_pipeline / check_normal_exit / check_tonosama_exit)。
        # entry board imbalance guard は元から no-op だったので移していない。
        # ranking_entry_filter_rescue_patch は trading/ranking/entry_from_ranking.py
        # (_passes_ranking_only_filters の再帰救済ラップ) と
        # trading/summary/engine/summary_incremental_engine.py._build_bars /
        # trading/ranking/ranking_technical_store.py._calculate_technicals
        # (期間出来高補正) へ本文化済みのため削除。
    ]
    for item in patches:
        # 要素は ("module", "function") のほか、main.py 内で完結する処理を
        # 並び順どおりに挟むための素の callable も許す。
        if callable(item):
            mod_name, fn_name = "main", getattr(item, "__name__", "<callable>")
        else:
            mod_name, fn_name = item
        try:
            if callable(item):
                fn = item
            else:
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
        if not hasattr(global_data, "signal_state_map") or not isinstance(getattr(global_data, "signal_state_map", None), dict):
            global_data.signal_state_map = _LazyFactoryDict(_factory_signal_state)
        if not hasattr(global_data, "prev_state_map") or not isinstance(getattr(global_data, "prev_state_map", None), dict):
            global_data.prev_state_map = _LazyFactoryDict(_factory_prev_state)
        if not hasattr(global_data, "position_state_map") or not isinstance(getattr(global_data, "position_state_map", None), dict):
            global_data.position_state_map = _LazyFactoryDict(_factory_position_state)
        if not hasattr(global_data, "recent_realized_pnl_map") or not isinstance(getattr(global_data, "recent_realized_pnl_map", None), dict):
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
            "✅ summary/entry runtime context installed (signal_state_map=%s prev_state_map=%s position_state_map=%s)",
            type(global_data.signal_state_map).__name__,
            type(global_data.prev_state_map).__name__,
            type(global_data.position_state_map).__name__,
        )
        heartbeat("main_runtime_context", status="OK", detail={"signal_state_map": type(global_data.signal_state_map).__name__})
    except Exception:
        heartbeat("main_runtime_context", status="ERROR")
        logger.exception("summary/entry runtime context install failed")


_SCHEDULE_DRIVE_STATE = {"allowed": None, "logged": False}


def _may_drive_schedule() -> bool:
    """main loop が schedule.run_pending() を呼んでよいか。

    core.startup.bootstrap.schedule_loop の dispatcher thread が
    schedule を回しているなら、ここでは回さない (二重 dispatch 防止)。
    判定は毎周ではなく、状態が変わったときだけログへ出す。
    """
    try:
        from core.startup.bootstrap.schedule_loop import (
            is_schedule_run_pending_loop_running,
            may_run_pending_here,
        )
    except Exception:
        # dispatcher が居ないビルドでは従来どおり main loop が回す。
        return True

    allowed = bool(may_run_pending_here("main_loop"))
    if allowed != _SCHEDULE_DRIVE_STATE["allowed"]:
        _SCHEDULE_DRIVE_STATE["allowed"] = allowed
        if allowed:
            logger.warning(
                "[EXIT SCHEDULER] main loop が schedule を駆動する "
                "(dispatcher thread running=%s)",
                is_schedule_run_pending_loop_running(),
            )
        else:
            logger.warning(
                "[EXIT SCHEDULER] main loop は schedule.run_pending を呼ばない "
                "(owner=schedule_loop dispatcher。同じ job の二重起動を防ぐ)",
            )
    return allowed


def _register_exit_scheduler():
    try:
        if not callable(run_exit_pipeline):
            logger.warning("[EXIT SCHEDULER] run_exit_pipeline unavailable")
            heartbeat("exit_scheduler", status="NG", detail={"reason": "run_exit_pipeline unavailable"})
            return False
        for job in list(schedule.jobs):
            try:
                fn = getattr(job, "job_func", None)
                if getattr(fn, "func", fn) is run_exit_pipeline:
                    logger.warning("[EXIT SCHEDULER] already registered")
                    heartbeat("exit_scheduler", status="OK", detail={"already_registered": True})
                    return True
            except Exception:
                pass
        # P0-8: Exit の owner は一系統にする。
        # exit_bootstrap の exit_loop_5s (5 秒ごと) が実働しており
        # (実測 424 回正常完走)、ここで run_exit_pipeline を 1 秒ごとに
        # 登録すると **2 系統が同時に返済注文を出す**ことになる。
        # 既定では登録しない。切り替えたい場合だけ env で明示する。
        _owner = (os.getenv("EXIT_PIPELINE_OWNER") or "exit_loop_5s").strip().lower()
        if _owner != "run_exit_pipeline":
            logger.warning(
                "[EXIT SCHEDULER] run_exit_pipeline は登録しない owner=%s "
                "(exit_bootstrap の exit_loop_5s が Exit owner。"
                "二重に返済注文を出さないため。"
                "EXIT_PIPELINE_OWNER=run_exit_pipeline で切り替え可能だが "
                "その場合は exit_loop_5s 側を止めること)",
                _owner,
            )
            heartbeat("exit_scheduler", status="OK",
                      detail={"registered": False, "owner": _owner})
            _audit_exit_owners(_owner)
            return True

        schedule.every(1).seconds.do(run_exit_pipeline)
        logger.warning("[EXIT SCHEDULER] run_exit_pipeline registered every 1s owner=%s", _owner)
        heartbeat("exit_scheduler", status="OK", detail={"registered_every_sec": 1, "owner": _owner})
        _audit_exit_owners(_owner)
        return True
    except Exception:
        heartbeat("exit_scheduler", status="ERROR")
        logger.exception("[EXIT SCHEDULER] register failed")
        return False


def _audit_exit_owners(owner: str) -> int:
    """P0-13: EXIT を駆動しているジョブを列挙し、owner が 1 系統か確認する。

    positions DB 障害時に 2 系統が同じ DB へ重複 SELECT を投げないよう、
    Exit の scheduler owner は必ず 1 本にする。
    次回ログでそのまま検証できるように、実際の登録内容を出す。
    """
    try:
        exit_jobs = []
        for job in list(schedule.jobs):
            try:
                fn = getattr(job, "job_func", None)
                fname = getattr(getattr(fn, "func", fn), "__name__", str(fn))
                tags = sorted(str(t) for t in (getattr(job, "tags", None) or set()))
                if "exit" in tags or "exit" in fname.lower():
                    exit_jobs.append({"func": fname, "tags": tags, "interval": str(job)})
            except Exception:
                logger.debug("[EXIT SCHEDULER] job audit skip", exc_info=True)

        n = len(exit_jobs)
        log = logger.warning if n == 1 else logger.error
        log(
            "[EXIT OWNER AUDIT] exit_scheduler_owner_count=%d owner=%s jobs=%s "
            "(完成条件5: Exit scheduler owner = 1)",
            n, owner, exit_jobs,
        )
        heartbeat("exit_owner_audit",
                  status="OK" if n == 1 else "NG",
                  detail={"count": n, "owner": owner})
        return n
    except Exception:
        logger.exception("[EXIT OWNER AUDIT] failed")
        return -1


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
                logger.info("✅ push refresh callable resolved: %s.%s", mod_name, fn_name)
                return fn
        except Exception:
            logger.debug("push refresh callable resolve failed: %s.%s", mod_name, fn_name, exc_info=True)
    logger.warning("⚠ push refresh callable unresolved")
    return None


def _install_push_refresh_callable():
    try:
        refresh_fn = _resolve_push_refresh_callable()
        if callable(refresh_fn):
            global_data.push_refresh_callable = refresh_fn
            logger.info("✅ global_data.push_refresh_callable installed")
            heartbeat("push_refresh_callable", status="OK", detail={"fn": getattr(refresh_fn, "__name__", repr(refresh_fn))})
        else:
            global_data.push_refresh_callable = None
            logger.warning("⚠ global_data.push_refresh_callable not installed")
            heartbeat("push_refresh_callable", status="NG")
    except Exception:
        heartbeat("push_refresh_callable", status="ERROR")
        logger.exception("push_refresh_callable install failed")


def _start_push_stream_safely():
    try:
        start_fn = getattr(push_stream, "start_push_stream", None)
        if not callable(start_fn):
            logger.error("❌ push_stream.start_push_stream unavailable")
            heartbeat("main_push_stream", status="NG", detail={"reason": "start_push_stream unavailable"})
            return False
        refresh_fn = getattr(global_data, "push_refresh_callable", None)
        logger.info("🟡 starting push_stream with refresh_callable=%s", getattr(refresh_fn, "__name__", repr(refresh_fn)) if callable(refresh_fn) else None)
        heartbeat("main_push_stream", status="START", detail={"refresh_callable": getattr(refresh_fn, "__name__", repr(refresh_fn)) if callable(refresh_fn) else None})
        start_fn(refresh_callable=refresh_fn, enable_rotate=False)
        logger.info("✅ push_stream.start_push_stream started")
        heartbeat("main_push_stream", status="OK")
        return True
    except Exception:
        heartbeat("main_push_stream", status="ERROR")
        logger.exception("push_stream start failed")
        return False


def _run_initial_summary_tick_once():
    try:
        if callable(run_summary_tick_once):
            logger.info("🟡 initial summary tick once start")
            heartbeat("initial_summary_tick", status="START")
            run_summary_tick_once()
            logger.info("✅ initial summary tick once done")
            heartbeat("initial_summary_tick", status="OK")
        else:
            logger.warning("⚠ initial summary tick once skipped (run_summary_tick_once unavailable)")
            heartbeat("initial_summary_tick", status="SKIP")
    except Exception:
        heartbeat("initial_summary_tick", status="ERROR")
        logger.exception("initial summary tick once failed")


def _run_initial_ranking_tick_once():
    try:
        from trading.summary.ranking.runner import run_time_locked_jobs
        if callable(run_time_locked_jobs):
            logger.info("🟡 initial ranking tick once start via ranking.runner.run_time_locked_jobs")
            heartbeat("initial_ranking_tick", status="START", detail={"via": "run_time_locked_jobs"})
            result = run_time_locked_jobs(display=True)
            logger.info("✅ initial ranking tick once done via run_time_locked_jobs targets=%s", sorted(list(result.keys())) if isinstance(result, dict) else [])
            heartbeat("initial_ranking_tick", status="OK", detail={"via": "run_time_locked_jobs"})
            return
    except Exception:
        heartbeat("initial_ranking_tick", status="ERROR", detail={"via": "run_time_locked_jobs"})
        logger.exception("initial ranking tick via run_time_locked_jobs failed")
    try:
        from trading.ranking.summary.runner import run_ranking_summary_once
        if callable(run_ranking_summary_once):
            logger.info("🟡 initial ranking summary once start")
            heartbeat("initial_ranking_tick", status="START", detail={"via": "run_ranking_summary_once"})
            run_ranking_summary_once()
            logger.info("✅ initial ranking summary once done")
            heartbeat("initial_ranking_tick", status="OK", detail={"via": "run_ranking_summary_once"})
        else:
            logger.warning("⚠ initial ranking tick skipped (callable unavailable)")
            heartbeat("initial_ranking_tick", status="SKIP")
    except Exception:
        heartbeat("initial_ranking_tick", status="ERROR", detail={"via": "run_ranking_summary_once"})
        logger.exception("initial ranking summary tick failed")


def main():
    logger.warning("[MAIN] start")
    mark_component_start("main")
    try:
        # P0-6: 資源スナップショットを最初に立ち上げる。
        # 2026-08-27 の disk I/O error は positions.db 固有の問題ではなく、
        # プロセス全体のハンドル枯渇の症状だった
        # (15:21:44 に push DB と positions.db が同時に CANTOPEN、
        #  15:30:37 にローカルパスで Errno 24 Too many open files)。
        # 次からは推測せずに handles / open_files / pool / thread で特定する。
        try:
            from core.diagnostics.resource_monitor import start_resource_monitor

            start_resource_monitor()
        except Exception:
            logger.exception("resource monitor start failed")

        _install_main_runtime_patches()
        _install_summary_entry_runtime_context()
        system_startup()
        try:
            rebind_logging_streams_to_console_tee()
            logger.info("✅ logging streams rebound to console tee")
        except Exception:
            logger.exception("logging rebind failed")
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
        # PositionSyncManager は start() を持たない未完成クラスで、毎起動
        # AttributeError を出していただけだった (maybe_sync/on_ws_reconnect も
        # 呼び出し元なし)。建玉の broker 再同期は
        # trading/position/open_position_sync.py の
        # broker_credit_authoritative 経路が source of truth なので、
        # 二重の position ポーラを復活させず呼び出しを削除する。
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
        # 2026-09-16 (B6): 3 分足・5 分足の並びから直接候補を出す走査
        #   (trading/entry/mtf_candidate_source.py、MTF_CANDIDATE_SOURCE_ENABLED=0 で停止)
        try:
            from trading.entry.mtf_candidate_source import start_background_scanner as _start_mtf_cand
            _start_mtf_cand()
        except Exception:
            logger.exception("mtf candidate source start failed")
        _run_initial_summary_tick_once()
        _run_initial_ranking_tick_once()
        heartbeat("main", status="RUNNING")
        while True:
            try:
                # schedule の default scheduler はプロセスで 1 つ。
                # core.startup.bootstrap.schedule_loop の dispatcher thread が
                # 動いているときにここでも run_pending() を回すと、
                # **同じ job (exit_loop_5s など) が 2 本同時に走る**。
                # 2026-09-01 12:53 のログでは 244ms ずれた exit_loop_5s が
                # 常時 2 本走り、/positions への GET も 2 倍になっていた。
                # 駆動の所有者は 1 つに限定する。
                if _may_drive_schedule():
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
