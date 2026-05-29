# ============================================================
# File   : sitecustomize.py
# Version: Ver22-TONOSAMA-PENDING-WARNING-RELAX
# ------------------------------------------------------------
# Python起動時に重要runtime patchを自動installする。
# 失敗しても本体起動は止めない。
#
# Ver22:
#   - TONOSAMA pending_writer の warning-only climax guard を自動install。
#   - buying_climax_upper_wick_warning / selling_climax_lower_wick_warning は、
#     価格変化が小さく出来高とランキングMA方向がOKなら pending登録を許可。
#
# Ver21:
#   - summaryYYYYMMDD.db に別日 datetime が混入しないよう、
#     SUMMARY_DB_DATE_GUARD を自動install。
#
# Ver20:
#   - TONOSAMA runner は5秒足を必須にしない方針。
#   - AI fallback側だけ 5s=0.000% で全候補を落としていたため、
#     TONOSAMA_AI_FALLBACK_REJECT_ZERO_5SEC=0 を既定化。
#
# Ver19:
#   - LIQ_EMPTY_FALLBACK は低流動性銘柄を戻してしまうため、自動installを既定OFFへ変更。
# ============================================================

from __future__ import annotations

import logging
import os
import sys
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
            f.flush()
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
        _write_boot_evidence(f"{label}_INSTALL_START")
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
        logger.warning("[SITECUSTOMIZE] LIQ_EMPTY_FALLBACK auto install skipped by default; set ENABLE_LIQ_EMPTY_FALLBACK_PATCH=1 to enable")
        return
    os.environ.pop("DISABLE_LIQ_EMPTY_FALLBACK_PATCH", None)
    _install_module("core.startup.liquidity_empty_fallback_patch", "LIQ_EMPTY_FALLBACK")


def _install_tonosama_surge_defaults() -> None:
    try:
        os.environ.setdefault("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING", "1")
        os.environ.setdefault("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY", "1")
        os.environ.setdefault("TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE", "3.0")
        os.environ.setdefault("TONOSAMA_5SEC_ADVISORY_ENABLED", "1")
        os.environ.setdefault("TONOSAMA_5SEC_ALLOW_ZERO_IF_PRIMARY_PASS", "1")
        os.environ.setdefault("TONOSAMA_AI_FALLBACK_REJECT_ZERO_5SEC", "0")
        os.environ.setdefault("TONOSAMA_AI_FALLBACK_MIN_5SEC_CHANGE_PCT", "0.0")
        os.environ.setdefault("TONOSAMA_ALLOW_HISTORY_MISSING_STRONG_MOVE", "1")
        os.environ.setdefault("TONOSAMA_ALLOW_WARNING_ONLY_CLIMAX", "1")
        os.environ.setdefault("TONOSAMA_WARNING_ONLY_MAX_PRICE_CHANGE_PCT", "0.50")
        os.environ.setdefault("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED", "1")
        os.environ.setdefault("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_SEC", "35")
        os.environ.setdefault("ENTRY_CONTROLLER_SOURCE_PREFILTER_ENABLED", "1")
        os.environ.setdefault("ENTRY_CONTROLLER_TONOSAMA_AI_BRIDGE", "1")
        os.environ.setdefault("ENTRY_CONTROLLER_TONOSAMA_MIN_SCORE", "0.01")
        os.environ.setdefault("LOW_MOVE_TONOSAMA_MIN_ENTRY_PRICE", "300")
        os.environ.setdefault("LOW_MOVE_TONOSAMA_ALLOW_NO_HIGHLOW_FALLBACK", "1")
        os.environ.setdefault("LOW_MOVE_TONOSAMA_NO_HIGHLOW_FALLBACK_RANGE_PCT", "0.012")
        os.environ.setdefault("FINAL_ENTRY_TONOSAMA_LIQUIDITY_FALLBACK", "1")
        os.environ.setdefault("FINAL_ENTRY_TONOSAMA_MIN_VOLUME", "10000")
        os.environ.setdefault("FINAL_ENTRY_TONOSAMA_MIN_TURNOVER", "3000000")
        os.environ.setdefault("FINAL_ENTRY_TONOSAMA_MIN_VOLUME_SPEED", "1.0")
        os.environ.setdefault("SUMMARY_DB_DATE_GUARD_ENABLED", "1")
        os.environ.setdefault("SUMMARY_DB_DATE_GUARD_CLEANUP_ENABLED", "1")
        _write_boot_evidence("TONOSAMA_SURGE_DEFAULTS_SET", {
            "failopen": os.environ.get("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING"),
            "allow_without_history": os.environ.get("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY"),
            "failopen_value": os.environ.get("TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE"),
            "five_sec_advisory": os.environ.get("TONOSAMA_5SEC_ADVISORY_ENABLED"),
            "five_sec_allow_zero": os.environ.get("TONOSAMA_5SEC_ALLOW_ZERO_IF_PRIMARY_PASS"),
            "ai_reject_zero_5s": os.environ.get("TONOSAMA_AI_FALLBACK_REJECT_ZERO_5SEC"),
            "ai_min_5s": os.environ.get("TONOSAMA_AI_FALLBACK_MIN_5SEC_CHANGE_PCT"),
            "warning_only_climax": os.environ.get("TONOSAMA_ALLOW_WARNING_ONLY_CLIMAX"),
            "warning_only_max_price_change": os.environ.get("TONOSAMA_WARNING_ONLY_MAX_PRICE_CHANGE_PCT"),
            "history_missing_strong_move": os.environ.get("TONOSAMA_ALLOW_HISTORY_MISSING_STRONG_MOVE"),
            "ranking_lock_wait": os.environ.get("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED"),
            "source_prefilter": os.environ.get("ENTRY_CONTROLLER_SOURCE_PREFILTER_ENABLED"),
            "tonosama_ai_bridge": os.environ.get("ENTRY_CONTROLLER_TONOSAMA_AI_BRIDGE"),
            "low_move_tonosama_min_price": os.environ.get("LOW_MOVE_TONOSAMA_MIN_ENTRY_PRICE"),
            "low_move_tonosama_no_highlow": os.environ.get("LOW_MOVE_TONOSAMA_ALLOW_NO_HIGHLOW_FALLBACK"),
            "final_tonosama_liq": os.environ.get("FINAL_ENTRY_TONOSAMA_LIQUIDITY_FALLBACK"),
            "summary_date_guard": os.environ.get("SUMMARY_DB_DATE_GUARD_ENABLED"),
        })
        logger.warning(
            "[SITECUSTOMIZE] tonosama defaults failopen=%s allow_without_history=%s value=%s five_sec_advisory=%s allow_zero=%s ai_reject_zero_5s=%s ai_min_5s=%s warning_only_climax=%s warning_only_max_price_change=%s history_missing_strong_move=%s ranking_lock_wait=%s source_prefilter=%s tonosama_ai_bridge=%s low_move_tonosama_min_price=%s no_highlow_fallback=%s final_tonosama_liq=%s summary_date_guard=%s",
            os.environ.get("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING"),
            os.environ.get("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY"),
            os.environ.get("TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE"),
            os.environ.get("TONOSAMA_5SEC_ADVISORY_ENABLED"),
            os.environ.get("TONOSAMA_5SEC_ALLOW_ZERO_IF_PRIMARY_PASS"),
            os.environ.get("TONOSAMA_AI_FALLBACK_REJECT_ZERO_5SEC"),
            os.environ.get("TONOSAMA_AI_FALLBACK_MIN_5SEC_CHANGE_PCT"),
            os.environ.get("TONOSAMA_ALLOW_WARNING_ONLY_CLIMAX"),
            os.environ.get("TONOSAMA_WARNING_ONLY_MAX_PRICE_CHANGE_PCT"),
            os.environ.get("TONOSAMA_ALLOW_HISTORY_MISSING_STRONG_MOVE"),
            os.environ.get("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED"),
            os.environ.get("ENTRY_CONTROLLER_SOURCE_PREFILTER_ENABLED"),
            os.environ.get("ENTRY_CONTROLLER_TONOSAMA_AI_BRIDGE"),
            os.environ.get("LOW_MOVE_TONOSAMA_MIN_ENTRY_PRICE"),
            os.environ.get("LOW_MOVE_TONOSAMA_ALLOW_NO_HIGHLOW_FALLBACK"),
            os.environ.get("FINAL_ENTRY_TONOSAMA_LIQUIDITY_FALLBACK"),
            os.environ.get("SUMMARY_DB_DATE_GUARD_ENABLED"),
        )
    except Exception:
        _write_boot_evidence("TONOSAMA_SURGE_DEFAULTS_EXCEPTION", traceback.format_exc())


def _install_summary_mtf_catchup_safely() -> None:
    try:
        if os.environ.get("DISABLE_SUMMARY_MTF_CATCHUP", "").strip() == "1":
            _write_boot_evidence("SUMMARY_MTF_CATCHUP_DISABLED_BY_ENV")
            return
        if _is_main_py_process() and not _is_database_process() and not _env_on("SUMMARY_MTF_CATCHUP_RUN_IN_MAIN", False):
            _write_boot_evidence("SUMMARY_MTF_CATCHUP_SKIPPED_IN_MAIN_PROCESS")
            logger.warning("[SITECUSTOMIZE] summary mtf catchup skipped in main.py; main_database.py handles DB catchup")
            return
        os.environ.setdefault("SUMMARY_MTF_STARTUP_CATCHUP_ENABLED", "1")
        os.environ.setdefault("SUMMARY_MTF_CATCHUP_INTERVALS", "3,5")
        os.environ.setdefault("SUMMARY_MTF_CATCHUP_MA_BARS", "75")
        os.environ.setdefault("SUMMARY_MTF_CATCHUP_EXTRA_MINUTES", "30")
        os.environ.setdefault("SUMMARY_MTF_CATCHUP_INCLUDE_CURRENT_PARTIAL", "1")
        os.environ.setdefault("SUMMARY_MTF_CATCHUP_MAX_ROWS", "80000")
        os.environ.setdefault("SUMMARY_MTF_CATCHUP_RUN_ASYNC", "1")
        _install_module("core.startup.summary_multiframe_startup_catchup_patch", "SUMMARY_MTF_CATCHUP")
    except Exception:
        _write_boot_evidence("SUMMARY_MTF_CATCHUP_EXCEPTION", traceback.format_exc())


_write_boot_evidence("PYTHON_START")
_install_boot_exception_hook()
_install_tonosama_surge_defaults()
_install_module("core.startup.summary_db_date_guard_patch", "SUMMARY_DB_DATE_GUARD", disabled_env="DISABLE_SUMMARY_DB_DATE_GUARD_PATCH")
_install_module("core.startup.summary_save_quality_guard_patch", "SUMMARY_SAVE_QUALITY_GUARD", disabled_env="DISABLE_SUMMARY_SAVE_QUALITY_GUARD")
_install_module("core.startup.tonosama_5sec_advisory_patch", "TONOSAMA_5SEC_ADVISORY", disabled_env="DISABLE_TONOSAMA_5SEC_ADVISORY_PATCH")
_install_module("core.startup.tonosama_history_missing_guard_patch", "TONOSAMA_HISTORY_MISSING_GUARD", disabled_env="DISABLE_TONOSAMA_HISTORY_MISSING_GUARD_PATCH")
_install_module("core.startup.ranking_entry_flat_price_guard_patch", "RANKING_FLAT_PRICE_DB_FALLBACK", disabled_env="DISABLE_RANKING_FLAT_PRICE_PATCH")
_install_module("core.startup.entry_controller_pipeline_lock_wait_patch", "ENTRY_CONTROLLER_LOCK_WAIT", disabled_env="DISABLE_ENTRY_CONTROLLER_LOCK_WAIT_PATCH")
_install_module("core.startup.entry_controller_source_prefilter_patch", "ENTRY_CONTROLLER_SOURCE_PREFILTER", disabled_env="DISABLE_ENTRY_CONTROLLER_SOURCE_PREFILTER_PATCH")
_install_module("core.startup.entry_controller_tonosama_ai_bridge_patch", "TONOSAMA_AI_BRIDGE", disabled_env="DISABLE_TONOSAMA_AI_BRIDGE_PATCH")
_install_module("core.startup.low_movement_tonosama_no_highlow_patch", "LOW_MOVE_TONOSAMA_FALLBACK", disabled_env="DISABLE_LOW_MOVE_TONOSAMA_FALLBACK_PATCH")
_install_module("core.startup.final_entry_tonosama_liquidity_patch", "FINAL_TONOSAMA_LIQUIDITY", disabled_env="DISABLE_FINAL_TONOSAMA_LIQUIDITY_PATCH")
_install_module("core.startup.tonosama_pending_warning_relax_patch", "TONOSAMA_PENDING_WARNING_RELAX", disabled_env="DISABLE_TONOSAMA_PENDING_WARNING_RELAX_PATCH")
_install_liq_empty_fallback_only_if_enabled()
_install_module("core.startup.summary_ai_liquidity_rescue_patch", "SUMMARY_AI_LIQ_RESCUE", disabled_env="DISABLE_SUMMARY_AI_LIQ_RESCUE_PATCH")
_install_module("core.startup.summary_ai_entry_hook_dataframe_truth_patch", "SUMMARY_AI_DF_TRUTH_PATCH", disabled_env="DISABLE_SUMMARY_AI_DF_TRUTH_PATCH")
_install_module("core.startup.summary_mtf_early_ready_patch", "SUMMARY_MTF_EARLY_READY", disabled_env="DISABLE_SUMMARY_MTF_EARLY_READY_PATCH")
_install_module("trading.audit_logging.install_audit_logging", "AUDIT_LOGGING", disabled_env="DISABLE_AUDIT_LOGGING")
_install_module("core.startup.summary_controller_concat_duplicate_columns_patch", "SUMMARY_CONTROLLER_DUPCOL_PATCH", disabled_env="DISABLE_SUMMARY_CONTROLLER_DUPCOL_PATCH")
_install_summary_mtf_catchup_safely()
_write_boot_evidence("SITECUSTOMIZE_DONE")
