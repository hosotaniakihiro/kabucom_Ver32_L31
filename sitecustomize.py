# ============================================================
# File   : sitecustomize.py
# Version: Ver61-PREVDAY-INDICATOR-WARMUP-AUTO-INSTALL
# ------------------------------------------------------------
# Python起動時に重要runtime patchを自動installする。
# main.py は軽量同期 + background install。
# DB/data collector系はDB専用の最小同期パッチだけにして起動を軽くする。
# 救済/fail-open系はデフォルトOFFにして、本体判定を優先する。
#
# V61:
#   - 前営業日分足を保存せず、indicator_calculator の計算時だけ
#     メモリ上でウォームアップ連結する IND_PREVDAY_WARMUP を
#     main.py / main_database.py の両方で同期installする。
#
# V60:
#   - Summary-AI 実行時に渡された最新1m dfをfresh判定へ優先使用し、
#     古いglobal historyだけを見て stale_push_1m で止まる症状を防ぐ。
#   - executor selection を保護し、外側runtime patchがAI_OK候補を1件へ
#     潰してrolling retryを妨げる状態を防ぐ。
#
# V59:
#   - main.py のPUSHメモリが起動時スナップショットのまま stale になる場合、
#     main_database.py が更新している pushYYYYMMDD.db から読み直して
#     1分SUMMARYを作る SUMMARY_MAIN_PUSH_DB_REFRESH を同期パッチへ追加。
# ============================================================
from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
BOOT_EVIDENCE_DIR = Path(r"\\192.168.0.22\AutoStockBuyAndSell\Logs\boot_evidence")


def _ensure_project_root() -> str:
    try:
        root = os.path.dirname(os.path.abspath(__file__))
        if root and root not in sys.path:
            sys.path.insert(0, root)
        return root
    except Exception:
        return ""


def _boot_path() -> Path:
    try:
        BOOT_EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return BOOT_EVIDENCE_DIR / f"boot_evidence_{datetime.now().strftime('%Y%m%d')}.log"


def _write_boot_evidence(event: str, detail: Any = None) -> None:
    try:
        line = f"[{datetime.now().isoformat(timespec='seconds')}] pid={os.getpid()} event={event} cwd={os.getcwd()} argv={' '.join(map(str, sys.argv))} detail={detail}\n"
        with open(_boot_path(), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def _env_on(name: str, default: bool = False) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _argv_text() -> str:
    try:
        return " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
    except Exception:
        return ""


def _is_main_py_process() -> bool:
    try:
        argv = [str(x).replace("\\", "/").lower() for x in sys.argv]
        return any(x.endswith("/main.py") or x == "main.py" for x in argv)
    except Exception:
        return False


def _is_db_prepare_process() -> bool:
    try:
        return "db_prepare_runner.py" in _argv_text()
    except Exception:
        return False


def _is_main_database_process() -> bool:
    try:
        return "main_database.py" in _argv_text()
    except Exception:
        return False


def _is_database_process() -> bool:
    try:
        argv = _argv_text()
        if any(x in argv for x in (
            "main_database.py",
            "data_collectors_runner.py",
            "db_prepare_runner.py",
            "push_receiver_runner.py",
            "yahoo_complement_runner.py",
            "summary_database_runner.py",
            "ranking_collector_runner.py",
        )):
            return True
    except Exception:
        pass
    return any(_env_on(x, False) for x in (
        "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
        "AUTOSTOCK_SUMMARY_DB_WRITER",
        "AUTOSTOCK_MAIN_DATABASE_PROCESS",
        "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
    ))


def _configure_db_startup_scope_defaults() -> None:
    try:
        db_prepare = _is_db_prepare_process()
        main_database = _is_main_database_process()
        allow_schema_in_main = _env_on("SUMMARY_SCHEMA_REPAIR_IN_MAIN_DATABASE", False)
        if not db_prepare and not (main_database and allow_schema_in_main):
            os.environ.setdefault("DISABLE_SUMMARY_RANKING_SCHEMA_REPAIR_PATCH", "1")
            os.environ.setdefault("SUMMARY_RANKING_SCHEMA_REPAIR_SCOPE", "db_prepare_only")
        else:
            os.environ.pop("DISABLE_SUMMARY_RANKING_SCHEMA_REPAIR_PATCH", None)
            os.environ.setdefault("SUMMARY_RANKING_SCHEMA_REPAIR_SCOPE", "db_prepare_owner")
        if not db_prepare and not _env_on("SUMMARY_MTF_CATCHUP_RUN_IN_THIS_PROCESS", False):
            os.environ.setdefault("DISABLE_SUMMARY_MTF_CATCHUP", "1")
        if db_prepare:
            os.environ["SUMMARY_MTF_CATCHUP_MA_BARS"] = os.environ.get("SUMMARY_MTF_CATCHUP_MA_BARS_OPERATOR", "20")
            os.environ["SUMMARY_MTF_CATCHUP_EXTRA_MINUTES"] = os.environ.get("SUMMARY_MTF_CATCHUP_EXTRA_MINUTES_OPERATOR", "10")
            os.environ["SUMMARY_MTF_CATCHUP_MAX_ROWS"] = os.environ.get("SUMMARY_MTF_CATCHUP_MAX_ROWS_OPERATOR", "20000")
            os.environ["SUMMARY_MTF_CATCHUP_UPSERT_CHUNK_SIZE"] = os.environ.get("SUMMARY_MTF_CATCHUP_UPSERT_CHUNK_SIZE_OPERATOR", "200")
            os.environ["SUMMARY_MTF_CATCHUP_BUSY_TIMEOUT_MS"] = os.environ.get("SUMMARY_MTF_CATCHUP_BUSY_TIMEOUT_MS_OPERATOR", "15000")
            os.environ["SUMMARY_MTF_CATCHUP_SQLITE_TIMEOUT"] = os.environ.get("SUMMARY_MTF_CATCHUP_SQLITE_TIMEOUT_OPERATOR", "20")
            os.environ.setdefault("SUMMARY_MTF_INDICATOR_FILL_AFTER_CATCHUP", "0")
            os.environ.setdefault("SUMMARY_MTF_INDICATOR_FILL_AFTER_EMPTY_CATCHUP", "0")
        _write_boot_evidence("DB_STARTUP_SCOPE_CONFIGURED", {"db_prepare": db_prepare, "main_database": main_database, "schema_disabled": os.environ.get("DISABLE_SUMMARY_RANKING_SCHEMA_REPAIR_PATCH"), "mtf_disabled": os.environ.get("DISABLE_SUMMARY_MTF_CATCHUP"), "mtf_ma_bars": os.environ.get("SUMMARY_MTF_CATCHUP_MA_BARS")})
    except Exception:
        _write_boot_evidence("DB_STARTUP_SCOPE_CONFIG_EXCEPTION", traceback.format_exc())


def _install_boot_exception_hook() -> None:
    try:
        old_hook = sys.excepthook
        def _hook(exc_type, exc, tb):
            try:
                _write_boot_evidence("UNCAUGHT_EXCEPTION", "".join(traceback.format_exception(exc_type, exc, tb)))
            except Exception:
                pass
            try:
                old_hook(exc_type, exc, tb)
            except Exception:
                pass
        sys.excepthook = _hook
        _write_boot_evidence("BOOT_EXCEPTION_HOOK_INSTALLED")
    except Exception:
        pass


def _install_module(module_name: str, label: str, *, disabled_env: str | None = None) -> None:
    try:
        if disabled_env and os.environ.get(disabled_env, "").strip() == "1":
            _write_boot_evidence(f"{label}_DISABLED_BY_ENV")
            return
        _ensure_project_root()
        mod = __import__(module_name, fromlist=["install"])
        fn = getattr(mod, "install", None)
        ok = bool(fn()) if callable(fn) else False
        _write_boot_evidence(f"{label}_INSTALL_DONE", {"ok": ok})
        logger.warning("[SITECUSTOMIZE] %s auto install ok=%s", label, ok)
    except Exception:
        _write_boot_evidence(f"{label}_EXCEPTION", traceback.format_exc())
        try:
            logger.exception("[SITECUSTOMIZE] %s auto install failed", label)
        except Exception:
            pass


def _is_database_like_process() -> bool:
    """旧 core/startup/liquidity_empty_fallback_patch._is_database_like_process()。

    _is_database_process() より狭く、main_database / data_collectors_runner /
    push_receiver_runner の 3 つだけを見る。判定基準が違うのでそのまま残す。
    """
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(x in argv for x in ("main_database.py", "data_collectors_runner.py", "push_receiver_runner.py")):
            return True
    except Exception:
        pass
    return str(os.environ.get("AUTOSTOCK_DATA_COLLECTORS_PROCESS", "")).strip().lower() in {"1", "true", "yes", "on"}


def _enable_push_db_writer_env_if_database_process() -> None:
    """旧 liquidity_empty_fallback_patch.install() が持っていた env 強制。

    DB 系プロセスで PUSH DB writer を明示的に有効化し、memory_only 誤判定を避ける。
    ENABLE_LIQ_EMPTY_FALLBACK_PATCH が真のときだけ走る点も旧構成どおり。
    """
    if not _is_database_like_process():
        return
    os.environ["AUTOSTOCK_DATA_COLLECTORS_PROCESS"] = "1"
    os.environ["AUTOSTOCK_MAIN_DATABASE_PROCESS"] = "1"
    os.environ["AUTOSTOCK_EXTERNAL_DATA_COLLECTORS"] = "1"
    os.environ["PUSH_STREAM_DB_WRITE"] = "1"
    os.environ["PUSH_STREAM_ORDER_BOOK_WRITE"] = os.environ.get("PUSH_STREAM_ORDER_BOOK_WRITE", "1")
    os.environ["AUTOSTOCK_SUMMARY_SAVE_OWNER"] = os.environ.get("AUTOSTOCK_SUMMARY_SAVE_OWNER", "database")
    os.environ["AUTOSTOCK_SUMMARY_DB_WRITER"] = os.environ.get("AUTOSTOCK_SUMMARY_DB_WRITER", "1")
    logger.warning(
        "[LIQ EMPTY FALLBACK] database-like process detected -> PUSH DB writer env forced on argv=%s",
        sys.argv[:3],
    )


def _install_liq_empty_fallback_only_if_enabled() -> None:
    if not _env_on("ENABLE_LIQ_EMPTY_FALLBACK_PATCH", False):
        os.environ.setdefault("DISABLE_LIQ_EMPTY_FALLBACK_PATCH", "1")
        _write_boot_evidence("LIQ_EMPTY_FALLBACK_DISABLED_BY_DEFAULT")
        return
    os.environ.pop("DISABLE_LIQ_EMPTY_FALLBACK_PATCH", None)
    # 段階緩和のロジック本体は
    #   trading/push/subscription_manager/ranking_selector._apply_min_liquidity
    #   trading/ranking/filters/liquidity_filter.apply_liquidity_filter
    # へ本文化済み。両モジュールの _liq_empty_fallback_enabled() が
    # ENABLE_LIQ_EMPTY_FALLBACK_PATCH / DISABLE_LIQ_EMPTY_FALLBACK_PATCH を見て
    # 有効/無効を判定するので、install 呼び出しは不要になった。
    # install() が持っていた env 強制だけをここへ移す。
    _enable_push_db_writer_env_if_database_process()
    _write_boot_evidence("LIQ_EMPTY_FALLBACK_ENABLED")


def _install_runtime_defaults() -> None:
    try:
        _ensure_project_root()
        mod = __import__("core.startup.runtime_env_defaults_patch", fromlist=["install"])
        ok = bool(getattr(mod, "install")())
        os.environ["ENTRY_SHORT_MTF_REQUIRE_ALL"] = "0"
        _write_boot_evidence("CENTRAL_RUNTIME_DEFAULTS_SET", {"ok": ok})
        logger.warning(
            "[SITECUSTOMIZE] centralized runtime defaults ok=%s version=%s ranking_watchdog=%s hard_timeout=%s sqlite_memory=%s rescue=%s full_nonmain=%s",
            ok,
            getattr(mod, "VERSION", "unknown"),
            os.environ.get("RANKING_ENTRY_WATCHDOG_ENABLED"),
            os.environ.get("RANKING_ENTRY_HARD_TIMEOUT_SEC"),
            os.environ.get("SQLITE_MEMORY_PRAGMAS_ENABLED"),
            os.environ.get("SITECUSTOMIZE_ENABLE_RESCUE_PATCHES"),
            os.environ.get("SITECUSTOMIZE_ENABLE_FULL_NONMAIN"),
        )
    except Exception:
        os.environ.setdefault("SQLITE_MEMORY_PRAGMAS_ENABLED", "1")
        os.environ.setdefault("SITECUSTOMIZE_ENABLE_FULL_NONMAIN", "0")
        os.environ.setdefault("SITECUSTOMIZE_ENABLE_RESCUE_PATCHES", "0")
        os.environ["ENTRY_SHORT_MTF_REQUIRE_ALL"] = "0"
        _write_boot_evidence("CENTRAL_RUNTIME_DEFAULTS_EXCEPTION", traceback.format_exc())


def _install_summary_mtf_catchup_safely() -> None:
    try:
        if os.environ.get("DISABLE_SUMMARY_MTF_CATCHUP", "").strip() == "1":
            logger.warning("[SITECUSTOMIZE] summary mtf catchup skipped by scope/env argv=%s", sys.argv)
            return
        if not _is_db_prepare_process() and not _env_on("SUMMARY_MTF_CATCHUP_RUN_IN_THIS_PROCESS", False):
            logger.warning("[SITECUSTOMIZE] summary mtf catchup skipped outside db_prepare argv=%s", sys.argv)
            return
        os.environ.setdefault("SUMMARY_MTF_STARTUP_CATCHUP_ENABLED", "1")
        os.environ.setdefault("SUMMARY_MTF_CATCHUP_INTERVALS", "3,5")
        os.environ.setdefault("SUMMARY_MTF_CATCHUP_RUN_ASYNC", "1")
        # summary_multiframe_startup_catchup_patch は trading/summary/mtf_startup_catchup.py へ
        # 移設済み (モンキーパッチではなく db_prepare 専用のバッチジョブだったため)。
        _install_module("trading.summary.mtf_startup_catchup", "SUMMARY_MTF_CATCHUP")
    except Exception:
        _write_boot_evidence("SUMMARY_MTF_CATCHUP_EXCEPTION", traceback.format_exc())


DB_SYNC_PATCHES = [
    ("core.startup.sqlite_memory_pragmas_patch", "SQLITE_MEMORY_PRAGMAS", "DISABLE_SQLITE_MEMORY_PRAGMAS_PATCH"),
]

# push_summary_fallback_and_active_price_patch (REV5) はインライン化済み。
# trading/ranking/active_symbols/liquidity.py と
# scheduler_jobs/summary/fallback_loader.py の本文に統合されているため、
# ここでの install() は不要。
#
# ranking_entry_market_hours_skip_patch / ranking_entry_hard_timeout_patch /
# ranking_stuck_pending_prune_patch / ranking_entry_controller_timeout_patch
# (main.py側) は、trading/entry_exit/tasks.py の _run_ranking_entry_safe と
# core/startup/schedule_loop.py の _is_job_running へ本文化済みのため削除した。
#
# ranking_entry_snapshot_technical_alias_patch は
# trading/ranking/ranking_technical_store.py の attach_ranking_technicals
# (_copy_snapshot_technical_aliases) へ本文化済みのため削除した。

# entry_controller_source_prefilter_patch は本文化済みで空スタブになっており、
# 実体は「この env を先に確定させてから summary_ai_volatility_rescue_patch を
# install する」だけの中継だった。この値を読んでいたのは旧 V4 rescue 実装。
os.environ["SUMMARY_AI_VOL_RESCUE_MIN_ABS_SLOPE"] = os.getenv(
    "SUMMARY_AI_VOL_RESCUE_MIN_ABS_SLOPE_OPERATOR",
    "0.0002",
)


def _apply_final_board_guard_signature_env_defaults() -> None:
    """final_entry_board_guard_signature_runtime_patch が持っていた env 既定値。

    このパッチは final_entry_safety_guard_patch の _board_guard /
    _call_board_guard / _board_missing_fallback_ok を包み直すものだったが、
    ホストがコミット 46ddd54 で本文化されて属性ごと消えたため、
    _patch_once() の中で実効だったのはこの 6 件の setdefault だけになっていた
    (ラップは 3 つとも target=None で失敗し、install() は常に False を返す)。

    順序について: このうち ENTRY_ALLOW_ENTRY_WITHOUT_BOARD は
    entry_controller._final_guard_board_ok (板データ無しエントリーの許可) を
    左右する load-bearing な値。他に同じキーを書くのは
      - 同値の setdefault (entry_final_filter_failopen "1" /
        entry_count_unblock "0" / runtime_env_defaults の MIN_* 3 件)
      - 後から実行されるハード代入 (board_missing_failopen_runtime 等)
      - sitecustomize より後に走る usercustomize の setdefault "0"
    だけなので、background loop の先頭へ前倒ししても最終値は変わらない。
    """
    os.environ.setdefault("ENTRY_SUMMARY_AI_DELEGATE_BOARD_TO_ORDER_BUILDER", "1")
    os.environ.setdefault("ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", "0")
    # 成行許可ではなく、order_builder の安全指値 fallback へ進ませるための既定値。
    os.environ.setdefault("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", "1")
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_VOLUME", os.getenv("ENTRY_SUMMARY_AI_DELEGATE_MIN_VOLUME", "30000"))
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_TURNOVER", os.getenv("ENTRY_SUMMARY_AI_DELEGATE_MIN_TURNOVER", "10000000"))
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_SCORE", os.getenv("ENTRY_SUMMARY_AI_DELEGATE_MIN_SCORE", "0.90"))


def _apply_summary_ai_vol_rescue_env_defaults() -> None:
    """summary_ai_volatility_rescue_patch (V5 strict stub) が持っていた env 既定値。

    V5 は何もラップしない strict stub で、実体はこの 4 つの setdefault と
    install ログだけだった。パッチファイルは削除し、install されていたのと
    同じ位置・同じスコープ (main lite / full non-main) で env だけを設定する。

    Note: この 4 つと SUMMARY_AI_VOL_RESCUE_MIN_ABS_SLOPE は、現時点で
    リポジトリ内に読み手が存在しない (読んでいたのは削除済みの V4 実装だけ)。
    挙動を変えないため残しているが、消しても実害はない。
    """
    os.environ.setdefault("SUMMARY_AI_VOL_RESCUE_ENABLED", "0")
    os.environ.setdefault("SUMMARY_AI_ZERO_SCORE_PUSH_BOOTSTRAP_ENABLED", "0")
    os.environ.setdefault("SUMMARY_AI_DB_FRESH_INPUT_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_VOL_RESCUE_WATCH_LOOPS", "0")


SYNC_MAIN_PATCHES = [
    ("core.startup.sqlite_memory_pragmas_patch", "SQLITE_MEMORY_PRAGMAS", "DISABLE_SQLITE_MEMORY_PRAGMAS_PATCH"),
    # summary_ai_volatility_rescue_patch は削除済み (env 既定値のみのstubだった)。
    # 代わりに _apply_summary_ai_vol_rescue_env_defaults() を install 前に呼ぶ。
    ("core.startup.summary_ai_runtime_consistency_patch", "SUMMARY_AI_RUNTIME_CONSISTENCY", "DISABLE_SUMMARY_AI_RUNTIME_CONSISTENCY_PATCH"),
    ("core.startup.summary_main_push_db_refresh_patch", "SUMMARY_MAIN_PUSH_DB_REFRESH", "DISABLE_SUMMARY_MAIN_PUSH_DB_REFRESH_PATCH"),
    ("core.startup.summary_main_direct_push_force_patch", "SUMMARY_FORCE_DIRECT_1M_SYNC", "DISABLE_SUMMARY_FORCE_DIRECT_1M_PATCH"),
]

BACKGROUND_MAIN_PATCHES = [
    # tonosama_5sec_advisory_patch は trading/entry/tonosama/runner._apply_climax_guards
    # へ本文化済み。残っていた中継 install (entry_order_5s_breakout_optional_patch) を
    # ここから直接行う。
    # entry_order_5s_breakout_optional_patch は trading/handlers/entry_order_builder.py へ
    # 本文化済み (_five_sec_optional_fallback_order)。NO_5S_BREAKOUT を返すのは
    # build_entry_order の 1 箇所だけなので、そこに直接置いた。
    # install スコープと DISABLE_TONOSAMA_5SEC_ADVISORY_PATCH /
    # ENTRY_ORDER_5S_BREAKOUT_OPTIONAL_ENABLED の判定は
    # entry_order_builder._five_sec_optional_scope_enabled() が再現している。
    # entry_controller への別名差し替えは不要 (entry_controller.py:118 が
    # from-import で同じ関数オブジェクトを束縛しているため)。
    ("core.startup.tonosama_datetime_tz_guard_patch", "TONOSAMA_DT_TZ_GUARD", "DISABLE_TONOSAMA_DT_TZ_GUARD_PATCH"),
    ("core.startup.tonosama_history_missing_guard_patch", "TONOSAMA_HISTORY_MISSING_GUARD", "DISABLE_TONOSAMA_HISTORY_MISSING_GUARD_PATCH"),
    # ranking_entry_source_db_fallback_patch は trading/ranking/entry_from_ranking.py へ
    # 本文化済み (_get_ranking_source_df_with_db_fallback)。元関数を一度も呼ばない
    # 完全置換だったので、本文側も _rsrc_scope_enabled() が真なら置換のまま動く。
    # install スコープ (main lite / full non-main、DBプロセス除外) と
    # env setdefault 10 件は entry_from_ranking 側が再現している。
    # 同じ属性を包んでいた ranking_entry_source_prefilter_patch_v2 も
    # 同時に本文化した (main.py では置換で捨てられていた挙動を維持)。
    ("trading.handlers.short_mtf_direction_guard", "SHORT_MTF_2OF3_GUARD", "DISABLE_SHORT_MTF_2OF3_GUARD_PATCH"),
    # entry_controller_tonosama_ai_bridge_patch は trading/handlers/entry_controller.py へ
    # 本文化済み (_tonosama_bridge_ai_result / _tonosama_bridge_pass_check)。
    # 旧パッチは ec.ai_final_entry_check と ec._passes_ai_gate をモジュール属性ごと
    # 差し替えていたが、どちらも読み手は entry_controller 内の呼び出し 1 箇所だけなので、
    # 呼び出し側で結果に噛ませる形にした。これで summary_ai_entry_controller_bridge_patch /
    # entry_volume_direction_guard_patch / entry_controller_final_gate_relax_patch の
    # 上に載る = 旧構成と同じ最外周の位置を保てる。
    # install スコープと DISABLE_TONOSAMA_AI_BRIDGE_PATCH の判定は
    # entry_controller._tonosama_bridge_scope_enabled() が再現している。
    # final_entry_tonosama_liquidity_patch は削除済み。ラップ対象だった
    # final_entry_safety_guard_patch._liquidity_guard が本文化で消えており、
    # install() が常に False を返す完全な死にコードだった。
    # 流動性ガード本体は trading/handlers/entry_controller._final_guard_liquidity_ok。
    # final_entry_board_guard_signature_runtime_patch は削除済み。
    # ラップ対象の final_entry_safety_guard_patch._board_guard / _call_board_guard が
    # 本文化で消えており、実効は env 既定値 6 件だけだった。
    # それを _apply_final_board_guard_signature_env_defaults() として
    # background loop / full non-main の先頭で設定する。
    # tonosama_fast_score_prefilter_patch は trading/entry/tonosama/fast_score_prefilter.py
    # へ本文化済み。中継していた tonosama_range_from_history_patch も
    # trading/entry/tonosama/runner.py (_repair_intrabar_range_from_history) へ
    # 本文化済みのため、ここでの install は不要になった。
    # スコープと env 条件は runner._range_repair_enabled() が再現している。
    ("trading.entry.tonosama.fresh_summary_wait", "TONOSAMA_FRESH_SUMMARY_WAIT_FIX", "DISABLE_TONOSAMA_FRESH_SUMMARY_WAIT_FIX_PATCH"),
    ("core.startup.summary_ai_entry_hook_dataframe_truth_patch", "SUMMARY_AI_DF_TRUTH_PATCH", "DISABLE_SUMMARY_AI_DF_TRUTH_PATCH"),
    ("core.startup.summary_ai_candidate_refill_patch", "SUMMARY_AI_CANDIDATE_REFILL", "DISABLE_SUMMARY_AI_CANDIDATE_REFILL_PATCH"),
    ("core.startup.summary_ai_runtime_consistency_patch", "SUMMARY_AI_RUNTIME_CONSISTENCY_BG", "DISABLE_SUMMARY_AI_RUNTIME_CONSISTENCY_PATCH"),
    ("trading.filters.summary_ai_range_bridge", "SUMMARY_AI_LOW_MOVE_SOFTPASS", "DISABLE_SUMMARY_AI_LOW_MOVE_SOFTPASS_PATCH"),
    ("core.startup.summary_mtf_early_ready_patch", "SUMMARY_MTF_EARLY_READY", "DISABLE_SUMMARY_MTF_EARLY_READY_PATCH"),
    ("trading.audit_logging.install_audit_logging", "AUDIT_LOGGING", "DISABLE_AUDIT_LOGGING"),
    # summary_main_push_db_refresh_patch / summary_main_direct_push_force_patch の
    # background 再適用は削除した。どちらも冪等な no-op で、SYNC_MAIN_PATCHES
    # (上の 2 件) が先に走った時点で完了している:
    #   push_db_refresh   install() 冒頭が `if _PATCHED: return True`
    #                     (その前の _install_context_stale_guard() はフラグを
    #                      立てて True を返すだけの死にコード)
    #   direct_push_force install() は env setdefault 5 件とログだけ
    #                     (V10 で job_summary のラップは撤去済み)
]

# entry_direction_recursion_failopen_patch は削除した。二重ゲートで既定 OFF
# (runtime_env_defaults.py:98 SITECUSTOMIZE_ENABLE_ENTRY_FAILOPEN_PATCHES="0" と
#  同:108 ENTRY_DIRECTION_RECURSION_FAILOPEN_ENABLED="0")。
# さらに仮に両方を 1 にしても、install() が old (= failclosed / failopen_runtime v5 の
# ラッパー) を退避するだけで一度も呼ばず、base の _direction_confirm を直呼びする
# ため、下の 2 層をまとめて無効化してしまう実装だった (*args/**kwargs も破棄)。
# 再帰時 fail-open の意図は entry_direction_failclosed_patch の
# ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN が担っている。
# グループ自体は将来の追加用に残す。
ENTRY_FAILOPEN_PATCHES: list = []

# ranking_entry_final_rescue_patch は trading/handlers/entry_controller.py へ
# 本文化済み (_ranking_final_rescue_atr + _ranking_rescue_apply_env_defaults)。
# range_5m_filter 側の softpass は volatility_filter へインライン化済み、
# ai_final_entry_check のラップは常時 no-op だったので移していない。
# opt-in スコープは entry_controller._ranking_rescue_scope_enabled() が再現する。
# グループ自体は将来の追加用に残す。
RANKING_RESCUE_PATCHES: list = []

TONOSAMA_EXTRA_RESCUE_PATCHES = [
    # low_movement_tonosama_no_highlow_patch は
    # core/startup/low_movement_entry_guard_patch.py へマージ済み
    # (_range_pct_from_row_core + _no_highlow_range_pct)。ホストがまだ
    # core/startup 配下のパッチなので本文化ではなく統合だが、
    # モンキーパッチは 1 段減った。
    # opt-in スコープと env 既定値は
    # low_movement_entry_guard_patch._no_highlow_scope_enabled() /
    # _no_highlow_apply_env_defaults() が再現している。
    # tonosama_pending_warning_relax_patch は
    # trading/entry/tonosama/pending_writer.py へ本文化済み (_warning_relax_stage)。
    # main.py から入る tonosama_5sec_stopped_relax_patch と同じシンボルを
    # 互いに _original 剥がしで奪い合っていたので、2 件セットで
    # _climax_reject_reason の直列ステージへ畳んだ。
    # opt-in スコープと DISABLE_TONOSAMA_PENDING_WARNING_RELAX_PATCH は
    # pending_writer._warning_relax_scope_enabled() が再現している。
    # install 時のモジュール定数書き換え (REJECT_ZERO_5SEC_FINAL / MIN_FINAL_5SEC_CHANGE_PCT)
    # は pending_writer._effective_zero_5sec_guard() が実効値として返す。
    # tonosama_price_change_or_range_patch / tonosama_volume_surge_zero_rescue_patch /
    # tonosama_slope_range_rescue_patch の 3 件は trading/entry/tonosama/runner.py へ
    # 本文化済み (_rescue_max_price_change_pct / _rescue_max_volume_surge_ratio / _rescue_slope)。
    # 3 件とも runner._num_series() を多段ラップしていたが補正対象カラムが排他なので、
    # _num_series 側の col 分岐で等価になる。
    # opt-in スコープと DISABLE_* / *_ENABLED 条件、install 時の env 副作用は
    # runner._tonosama_extra_rescue_enabled() と
    # runner._apply_tonosama_rescue_env_defaults() が再現している。
]

SUMMARY_AI_RESCUE_PATCHES = [
    ("core.startup.summary_ai_liquidity_rescue_patch", "SUMMARY_AI_LIQ_RESCUE", "DISABLE_SUMMARY_AI_LIQ_RESCUE_PATCH"),
]


def _install_patch_list(items) -> None:
    for module_name, label, disabled_env in items:
        _install_module(module_name, label, disabled_env=disabled_env)


def _install_optional_rescue_patches() -> None:
    all_rescue = _env_on("SITECUSTOMIZE_ENABLE_RESCUE_PATCHES", False)
    groups = [
        ("ENTRY failopen", ENTRY_FAILOPEN_PATCHES, all_rescue or _env_on("SITECUSTOMIZE_ENABLE_ENTRY_FAILOPEN_PATCHES", False)),
        ("RANKING final rescue", RANKING_RESCUE_PATCHES, all_rescue or _env_on("SITECUSTOMIZE_ENABLE_RANKING_FINAL_RESCUE_PATCH", False) or _env_on("USERCUSTOMIZE_ENABLE_RANKING_RESCUE_PATCHES", False)),
        ("TONOSAMA extra rescue", TONOSAMA_EXTRA_RESCUE_PATCHES, all_rescue or _env_on("SITECUSTOMIZE_ENABLE_TONOSAMA_EXTRA_RESCUE_PATCHES", False) or _env_on("USERCUSTOMIZE_ENABLE_TONOSAMA_RESCUE_PATCHES", False)),
        ("SUMMARY AI rescue", SUMMARY_AI_RESCUE_PATCHES, all_rescue or _env_on("SITECUSTOMIZE_ENABLE_SUMMARY_AI_RESCUE_PATCHES", False)),
    ]
    for label, items, enabled in groups:
        if enabled:
            logger.warning("[SITECUSTOMIZE] optional %s patches enabled count=%s", label, len(items))
            _install_patch_list(items)
        else:
            logger.warning("[SITECUSTOMIZE] optional %s patches skipped count=%s", label, len(items))


def _background_main_patch_loop() -> None:
    logger.warning("[SITECUSTOMIZE] main background patches start count=%s", len(BACKGROUND_MAIN_PATCHES))
    _apply_final_board_guard_signature_env_defaults()
    _install_patch_list(BACKGROUND_MAIN_PATCHES)
    _install_optional_rescue_patches()
    _install_liq_empty_fallback_only_if_enabled()
    _install_summary_mtf_catchup_safely()
    # 旧: 「several summary/controller patches re-wrap runner_core.job_summary」
    # という理由で background パッチの後に 2 件を再適用していたが、
    # このコメントは陳腐化していた。job_summary を再ラップするコードは
    # リポジトリに 1 つも残っていない (git grep 'job_summary =' の実結果は
    # ログ文字列 1 件のみ)。3-way race は runner_core.py:300-324 の
    # tier1/tier2/tier3 固定順で解消済み。
    # 2 件とも冪等な no-op なので再適用ごと削除した。
    # 旧: 全 background パッチの後で board signature を再適用していた
    # (一部の entry guard が _board_guard を差し替えるため)。ホストが本文化されて
    # ラップ対象が無くなり、実効は env 既定値だけなので再適用は不要になった。
    # setdefault は冪等なので loop 先頭の 1 回で足りる。
    # Last Summary-AI consistency pass must run after board/signature and candidate-refill patches.
    _install_module("core.startup.summary_ai_runtime_consistency_patch", "SUMMARY_AI_RUNTIME_CONSISTENCY_LAST", disabled_env="DISABLE_SUMMARY_AI_RUNTIME_CONSISTENCY_PATCH")
    logger.warning("[SITECUSTOMIZE] main background patches done")


def _install_nonmain_minimal() -> None:
    _configure_db_startup_scope_defaults()
    _install_patch_list(DB_SYNC_PATCHES)
    _install_liq_empty_fallback_only_if_enabled()
    logger.warning("[SITECUSTOMIZE] non-main helper context detected; minimal patches installed=%s full trading patches skipped argv=%s", len(DB_SYNC_PATCHES), sys.argv)


_write_boot_evidence("PYTHON_START")
_install_boot_exception_hook()
_install_runtime_defaults()
_configure_db_startup_scope_defaults()

if _is_database_process():
    _install_patch_list(DB_SYNC_PATCHES)
    _install_liq_empty_fallback_only_if_enabled()
    _install_summary_mtf_catchup_safely()
    logger.warning("[SITECUSTOMIZE] database context detected; DB minimal patches installed=%s heavy entry/rescue patches skipped argv=%s", len(DB_SYNC_PATCHES), sys.argv)
elif _is_main_py_process() and _env_on("SITECUSTOMIZE_MAIN_LITE", True):
    _apply_summary_ai_vol_rescue_env_defaults()
    _install_patch_list(SYNC_MAIN_PATCHES)
    threading.Thread(target=_background_main_patch_loop, name="sitecustomize-main-bg-patches", daemon=True).start()
    logger.warning("[SITECUSTOMIZE] main lite mode enabled sync=%s background=%s", len(SYNC_MAIN_PATCHES), len(BACKGROUND_MAIN_PATCHES))
elif _env_on("SITECUSTOMIZE_ENABLE_FULL_NONMAIN", False):
    logger.warning("[SITECUSTOMIZE] full non-main mode enabled by env; installing trading patches argv=%s", sys.argv)
    _apply_summary_ai_vol_rescue_env_defaults()
    _apply_final_board_guard_signature_env_defaults()
    _install_patch_list(SYNC_MAIN_PATCHES + BACKGROUND_MAIN_PATCHES)
    _install_optional_rescue_patches()
    _install_liq_empty_fallback_only_if_enabled()
    _install_summary_mtf_catchup_safely()
else:
    _install_nonmain_minimal()

_write_boot_evidence("SITECUSTOMIZE_DONE")
