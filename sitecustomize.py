# ============================================================
# File   : sitecustomize.py
# Version: Ver39-YAHOO-DB-WARMUP-RANKING-HARD-TIMEOUT
# ------------------------------------------------------------
# Python起動時に重要runtime patchを自動installする。
# main.py は軽量同期 + background install。
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


def _is_main_py_process() -> bool:
    try:
        argv = [str(x).replace("\\", "/").lower() for x in sys.argv]
        return any(x.endswith("/main.py") or x == "main.py" for x in argv)
    except Exception:
        return False


def _is_database_process() -> bool:
    return any(_env_on(x, False) for x in ("AUTOSTOCK_DATA_COLLECTORS_PROCESS", "AUTOSTOCK_SUMMARY_DB_WRITER", "AUTOSTOCK_MAIN_DATABASE_PROCESS"))


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


def _install_liq_empty_fallback_only_if_enabled() -> None:
    if not _env_on("ENABLE_LIQ_EMPTY_FALLBACK_PATCH", False):
        os.environ.setdefault("DISABLE_LIQ_EMPTY_FALLBACK_PATCH", "1")
        _write_boot_evidence("LIQ_EMPTY_FALLBACK_DISABLED_BY_DEFAULT")
        return
    os.environ.pop("DISABLE_LIQ_EMPTY_FALLBACK_PATCH", None)
    _install_module("core.startup.liquidity_empty_fallback_patch", "LIQ_EMPTY_FALLBACK")


def _install_runtime_defaults() -> None:
    try:
        defaults = {
            "RANKING_ENTRY_WATCHDOG_ENABLED": "1",
            "RANKING_ENTRY_WATCHDOG_TIMEOUT_SEC": "55",
            "RANKING_ENTRY_HARD_TIMEOUT_ENABLED": "1",
            "RANKING_ENTRY_HARD_TIMEOUT_SEC": "28",
            "RANKING_ENTRY_SNAPSHOT_TECH_ALIAS_ENABLED": "1",
            "RANKING_ENTRY_SOURCE_DB_FALLBACK_ENABLED": "1",
            "RANKING_ENTRY_HIGH_LOW_SNAPSHOT_PATCH_ENABLED": "1",
            "RANKING_ENTRY_HIGH_LOW_SNAPSHOT_LOOKBACK_ROWS": "12",
            "RANKING_ENTRY_HIGH_LOW_SNAPSHOT_MAX_AGE_MIN": "30",
            "RANKING_STUCK_PENDING_MAX_CONTROLLER_RETRY": "2",
            "RANKING_STUCK_PENDING_MAX_AGE_SEC": "120",
            "RANKING_FINAL_RESCUE_MIN_SCORE": "55",
            "RANKING_FINAL_RESCUE_ATR_MIN_RATIO": "0.0005",
            "RANKING_FINAL_RESCUE_AI_FAILOPEN": "1",
            # Ranking prefilter accepts low-price liquid names from config; final low-move guard must not re-block only by price.
            "LOW_MOVE_RANKING_MIN_ENTRY_PRICE": "300",
            "LOW_MOVE_RANKING_MAX_ENTRY_PRICE": "7000",
            "LOW_MOVE_RANKING_MIN_RANGE_PCT_LOW_PRICE": "0.008",
            "LOW_MOVE_RANKING_MIN_RANGE_PCT_HIGH_PRICE": "0.006",
            "LOW_MOVE_RANKING_STRONG_RANGE_PCT": "0.014",
            "LOW_MOVE_RANKING_MIN_ABS_SLOPE": "0.0000",
            "TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING": "1",
            "TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY": "1",
            "TONOSAMA_ALLOW_HISTORY_MISSING_ENTRY": "1",
            "TONOSAMA_DROP_HISTORY_MISSING_ENTRY": "0",
            "TONOSAMA_RAW1_RESAMPLE_FALLBACK": "1",
            "TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE": "3.0",
            "TONOSAMA_5SEC_ADVISORY_ENABLED": "1",
            "TONOSAMA_5SEC_ALLOW_ZERO_IF_PRIMARY_PASS": "1",
            "TONOSAMA_AI_FALLBACK_REJECT_ZERO_5SEC": "0",
            "TONOSAMA_AI_FALLBACK_MIN_5SEC_CHANGE_PCT": "0.0",
            "TONOSAMA_ALLOW_HISTORY_MISSING_STRONG_MOVE": "1",
            "TONOSAMA_ALLOW_WARNING_ONLY_CLIMAX": "1",
            "TONOSAMA_WARNING_ONLY_MAX_PRICE_CHANGE_PCT": "0.50",
            "TONOSAMA_PRICE_CHANGE_OR_RANGE_ENABLED": "1",
            "TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_RANGE_PCT": "3.0",
            "TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_VOLUME": "50000",
            "TONOSAMA_PRICE_CHANGE_OR_RANGE_MIN_SURGE": "3.0",
            "TONOSAMA_ENTRY_TIMEOUT_SEC": "45",
            "TONOSAMA_ENTRY_CONTROLLER_TIMEOUT_SEC": "12",
            "TONOSAMA_DISPATCH_CONTROLLER_ON_TIMEOUT_PENDING": "1",
            "TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_SEC": "10",
            "TONOSAMA_ENTRY_TIMEOUT_COOLDOWN_MAX_SEC": "60",
            "ENTRY_CONTROLLER_LOCK_WAIT_ENABLED": "1",
            "ENTRY_CONTROLLER_LOCK_WAIT_SOURCES": "RANKING,TONOSAMA,SUMMARY",
            "ENTRY_CONTROLLER_LOCK_WAIT_SEC": "75",
            "ENTRY_CONTROLLER_SUMMARY_LOCK_WAIT_SEC": "75",
            "ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED": "1",
            "ENTRY_CONTROLLER_RANKING_LOCK_WAIT_SEC": "75",
            "ENTRY_CONTROLLER_LOCK_WAIT_TIMEOUT_SKIP_ORIGINAL": "1",
            "ENTRY_CONTROLLER_SOURCE_PREFILTER_ENABLED": "1",
            "ENTRY_CONTROLLER_TONOSAMA_AI_BRIDGE": "1",
            "ENTRY_CONTROLLER_TONOSAMA_MIN_SCORE": "0.01",
            "ENTRY_DIRECTION_RECURSION_FAILOPEN_ENABLED": "1",
            "ENTRY_SHORT_MTF_REQUIRED": "1",
            "ENTRY_SHORT_MTF_FORCE_2OF3": "1",
            "ENTRY_SHORT_MTF_MIN_ALIGNED": "2",
            "ENTRY_SHORT_MTF_MIN_AVAILABLE": "2",
            "ENTRY_SHORT_MTF_SLOPE_EPS": "0.0",
            "ENTRY_DAILY_MTF_OPTIONAL": "1",
            "ENTRY_SHORT_MTF_DB_BACKFILL": "1",
            "ENTRY_SHORT_MTF_ZERO_NEUTRAL": "1",
            "LOW_MOVE_TONOSAMA_MIN_ENTRY_PRICE": "300",
            "LOW_MOVE_TONOSAMA_ALLOW_NO_HIGHLOW_FALLBACK": "1",
            "FINAL_ENTRY_TONOSAMA_LIQUIDITY_FALLBACK": "1",
            "FINAL_ENTRY_TONOSAMA_MIN_VOLUME": "10000",
            "FINAL_ENTRY_TONOSAMA_MIN_TURNOVER": "3000000",
            "YAHOO_COMPLEMENT_DB_WARMUP_ENABLED": "1",
            "YAHOO_COMPLEMENT_DB_WARMUP_MIN_BARS": "75",
            "YAHOO_COMPLEMENT_DB_WARMUP_LOOKBACK_DAYS": "7",
            "SUMMARY_DB_DATE_GUARD_ENABLED": "1",
            "SUMMARY_DB_DATE_GUARD_CLEANUP_ENABLED": "0",
        }
        for k, v in defaults.items():
            os.environ.setdefault(k, v)
        os.environ["ENTRY_SHORT_MTF_REQUIRE_ALL"] = "0"
        _write_boot_evidence("RUNTIME_DEFAULTS_SET", {"ranking_snapshot_alias": os.environ.get("RANKING_ENTRY_SNAPSHOT_TECH_ALIAS_ENABLED")})
        logger.warning(
            "[SITECUSTOMIZE] defaults lite ranking_watchdog=%s timeout=%s hard_timeout=%s snapshot_tech_alias=%s ranking_price=%s-%s tonosama_raw1_resample=%s yahoo_db_warmup=%s",
            os.environ.get("RANKING_ENTRY_WATCHDOG_ENABLED"),
            os.environ.get("RANKING_ENTRY_WATCHDOG_TIMEOUT_SEC"),
            os.environ.get("RANKING_ENTRY_HARD_TIMEOUT_SEC"),
            os.environ.get("RANKING_ENTRY_SNAPSHOT_TECH_ALIAS_ENABLED"),
            os.environ.get("LOW_MOVE_RANKING_MIN_ENTRY_PRICE"),
            os.environ.get("LOW_MOVE_RANKING_MAX_ENTRY_PRICE"),
            os.environ.get("TONOSAMA_RAW1_RESAMPLE_FALLBACK"),
            os.environ.get("YAHOO_COMPLEMENT_DB_WARMUP_ENABLED"),
        )
    except Exception:
        _write_boot_evidence("RUNTIME_DEFAULTS_EXCEPTION", traceback.format_exc())


def _install_summary_mtf_catchup_safely() -> None:
    try:
        if os.environ.get("DISABLE_SUMMARY_MTF_CATCHUP", "").strip() == "1":
            return
        if _is_main_py_process() and not _is_database_process() and not _env_on("SUMMARY_MTF_CATCHUP_RUN_IN_MAIN", False):
            logger.warning("[SITECUSTOMIZE] summary mtf catchup skipped in main.py; main_database.py handles DB catchup")
            return
        os.environ.setdefault("SUMMARY_MTF_STARTUP_CATCHUP_ENABLED", "1")
        os.environ.setdefault("SUMMARY_MTF_CATCHUP_INTERVALS", "3,5")
        os.environ.setdefault("SUMMARY_MTF_CATCHUP_RUN_ASYNC", "1")
        _install_module("core.startup.summary_multiframe_startup_catchup_patch", "SUMMARY_MTF_CATCHUP")
    except Exception:
        _write_boot_evidence("SUMMARY_MTF_CATCHUP_EXCEPTION", traceback.format_exc())


SYNC_MAIN_PATCHES = [
    ("core.startup.ranking_entry_market_hours_skip_patch", "RANKING_ENTRY_WATCHDOG", "DISABLE_RANKING_ENTRY_WATCHDOG_PATCH"),
    ("core.startup.ranking_entry_snapshot_technical_alias_patch", "RANKING_SNAPSHOT_TECH_ALIAS", "DISABLE_RANKING_SNAPSHOT_TECH_ALIAS_PATCH"),
    ("core.startup.entry_log_skip_reason_collision_patch", "ENTRY_LOG_SKIP_GUARD", "DISABLE_ENTRY_LOG_SKIP_GUARD"),
    ("core.startup.entry_controller_pipeline_lock_wait_patch", "ENTRY_CONTROLLER_LOCK_WAIT", "DISABLE_ENTRY_CONTROLLER_LOCK_WAIT_PATCH"),
    ("core.startup.entry_controller_source_prefilter_patch", "ENTRY_CONTROLLER_SOURCE_PREFILTER", "DISABLE_ENTRY_CONTROLLER_SOURCE_PREFILTER_PATCH"),
]

BACKGROUND_MAIN_PATCHES = [
    ("core.startup.summary_db_date_guard_patch", "SUMMARY_DB_DATE_GUARD", "DISABLE_SUMMARY_DB_DATE_GUARD_PATCH"),
    ("core.startup.summary_save_quality_guard_patch", "SUMMARY_SAVE_QUALITY_GUARD", "DISABLE_SUMMARY_SAVE_QUALITY_GUARD"),
    ("core.startup.tonosama_5sec_advisory_patch", "TONOSAMA_5SEC_ADVISORY", "DISABLE_TONOSAMA_5SEC_ADVISORY_PATCH"),
    ("core.startup.tonosama_history_missing_guard_patch", "TONOSAMA_HISTORY_MISSING_GUARD", "DISABLE_TONOSAMA_HISTORY_MISSING_GUARD_PATCH"),
    ("core.startup.ranking_entry_flat_price_guard_patch", "RANKING_FLAT_PRICE_DB_FALLBACK", "DISABLE_RANKING_FLAT_PRICE_PATCH"),
    ("core.startup.ranking_entry_source_db_fallback_patch", "RANKING_ENTRY_SOURCE_DB_FALLBACK", "DISABLE_RANKING_ENTRY_SOURCE_DB_FALLBACK_PATCH"),
    ("core.startup.ranking_entry_high_low_from_snapshot_patch", "RANKING_ENTRY_HIGH_LOW_SNAPSHOT", "DISABLE_RANKING_ENTRY_HIGH_LOW_SNAPSHOT_PATCH"),
    ("core.startup.ranking_entry_final_rescue_patch", "RANKING_FINAL_RESCUE", "DISABLE_RANKING_FINAL_RESCUE_PATCH"),
    ("core.startup.ranking_stuck_pending_prune_patch", "RANKING_STUCK_PENDING_PRUNE", "DISABLE_RANKING_STUCK_PENDING_PRUNE_PATCH"),
    ("core.startup.ranking_entry_hard_timeout_patch", "RANKING_ENTRY_HARD_TIMEOUT", "DISABLE_RANKING_ENTRY_HARD_TIMEOUT_PATCH"),
    ("core.startup.yahoo_complement_db_warmup_patch", "YAHOO_COMPLEMENT_DB_WARMUP", "DISABLE_YAHOO_COMPLEMENT_DB_WARMUP_PATCH"),
    ("core.startup.entry_direction_recursion_failopen_patch", "ENTRY_DIRECTION_RECURSION_FAILOPEN", "DISABLE_ENTRY_DIRECTION_RECURSION_FAILOPEN_PATCH"),
    ("core.startup.entry_mtf_short_required_daily_optional_patch", "SHORT_MTF_2OF3_GUARD", "DISABLE_SHORT_MTF_2OF3_GUARD_PATCH"),
    ("core.startup.entry_controller_tonosama_ai_bridge_patch", "TONOSAMA_AI_BRIDGE", "DISABLE_TONOSAMA_AI_BRIDGE_PATCH"),
    ("core.startup.low_movement_tonosama_no_highlow_patch", "LOW_MOVE_TONOSAMA_FALLBACK", "DISABLE_LOW_MOVE_TONOSAMA_FALLBACK_PATCH"),
    ("core.startup.final_entry_tonosama_liquidity_patch", "FINAL_TONOSAMA_LIQUIDITY", "DISABLE_FINAL_TONOSAMA_LIQUIDITY_PATCH"),
    ("core.startup.tonosama_pending_warning_relax_patch", "TONOSAMA_PENDING_WARNING_RELAX", "DISABLE_TONOSAMA_PENDING_WARNING_RELAX_PATCH"),
    ("core.startup.tonosama_price_change_or_range_patch", "TONOSAMA_PRICE_RANGE_RESCUE", "DISABLE_TONOSAMA_PRICE_RANGE_RESCUE_PATCH"),
    ("core.startup.tonosama_volume_surge_zero_rescue_patch", "TONOSAMA_VOLUME_SURGE_ZERO_RESCUE", "DISABLE_TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_PATCH"),
    ("core.startup.tonosama_slope_range_rescue_patch", "TONOSAMA_SLOPE_RANGE_RESCUE", "DISABLE_TONOSAMA_SLOPE_RANGE_RESCUE_PATCH"),
    ("core.startup.tonosama_fast_score_prefilter_patch", "TONOSAMA_FAST_SCORE_PREFILTER", "DISABLE_TONOSAMA_FAST_SCORE_PREFILTER_PATCH"),
    ("core.startup.tonosama_fresh_summary_wait_fix_patch", "TONOSAMA_FRESH_SUMMARY_WAIT_FIX", "DISABLE_TONOSAMA_FRESH_SUMMARY_WAIT_FIX_PATCH"),
    ("core.startup.summary_ai_liquidity_rescue_patch", "SUMMARY_AI_LIQ_RESCUE", "DISABLE_SUMMARY_AI_LIQ_RESCUE_PATCH"),
    ("core.startup.summary_ai_entry_hook_dataframe_truth_patch", "SUMMARY_AI_DF_TRUTH_PATCH", "DISABLE_SUMMARY_AI_DF_TRUTH_PATCH"),
    ("core.startup.summary_mtf_early_ready_patch", "SUMMARY_MTF_EARLY_READY", "DISABLE_SUMMARY_MTF_EARLY_READY_PATCH"),
    ("trading.audit_logging.install_audit_logging", "AUDIT_LOGGING", "DISABLE_AUDIT_LOGGING"),
    ("core.startup.summary_controller_concat_duplicate_columns_patch", "SUMMARY_CONTROLLER_DUPCOL_PATCH", "DISABLE_SUMMARY_CONTROLLER_DUPCOL_PATCH"),
]


def _install_patch_list(items) -> None:
    for module_name, label, disabled_env in items:
        _install_module(module_name, label, disabled_env=disabled_env)


def _background_main_patch_loop() -> None:
    logger.warning("[SITECUSTOMIZE] main background patches start count=%s", len(BACKGROUND_MAIN_PATCHES))
    _install_patch_list(BACKGROUND_MAIN_PATCHES)
    _install_liq_empty_fallback_only_if_enabled()
    _install_summary_mtf_catchup_safely()
    logger.warning("[SITECUSTOMIZE] main background patches done")


_write_boot_evidence("PYTHON_START")
_install_boot_exception_hook()
_install_runtime_defaults()

if _is_main_py_process() and not _is_database_process() and _env_on("SITECUSTOMIZE_MAIN_LITE", True):
    _install_patch_list(SYNC_MAIN_PATCHES)
    threading.Thread(target=_background_main_patch_loop, name="sitecustomize-main-bg-patches", daemon=True).start()
    logger.warning("[SITECUSTOMIZE] main lite mode enabled sync=%s background=%s", len(SYNC_MAIN_PATCHES), len(BACKGROUND_MAIN_PATCHES))
else:
    _install_patch_list(SYNC_MAIN_PATCHES + BACKGROUND_MAIN_PATCHES)
    _install_liq_empty_fallback_only_if_enabled()
    _install_summary_mtf_catchup_safely()

_write_boot_evidence("SITECUSTOMIZE_DONE")
