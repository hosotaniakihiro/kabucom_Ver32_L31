# ============================================================
# File   : sitecustomize.py
# Version: Ver09-SHORT-AUTO-INSTALL-PATCHES
# ------------------------------------------------------------
# Python起動時に重要runtime patchを自動installする。
# 失敗しても本体起動は止めない。
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


def _install_tonosama_surge_defaults() -> None:
    try:
        os.environ.setdefault("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING", "1")
        os.environ.setdefault("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY", "1")
        os.environ.setdefault("TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE", "3.0")
        _write_boot_evidence("TONOSAMA_SURGE_DEFAULTS_SET", {
            "failopen": os.environ.get("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING"),
            "allow_without_history": os.environ.get("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY"),
            "failopen_value": os.environ.get("TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE"),
        })
        logger.warning(
            "[SITECUSTOMIZE] tonosama surge defaults failopen=%s allow_without_history=%s value=%s",
            os.environ.get("TONOSAMA_VOLUME_SURGE_FAILOPEN_IF_HISTORY_MISSING"),
            os.environ.get("TONOSAMA_ALLOW_ENTRY_WITHOUT_SURGE_HISTORY"),
            os.environ.get("TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE"),
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
_install_module("core.startup.summary_ai_liquidity_rescue_patch", "SUMMARY_AI_LIQ_RESCUE", disabled_env="DISABLE_SUMMARY_AI_LIQ_RESCUE_PATCH")
_install_module("core.startup.summary_ai_entry_hook_dataframe_truth_patch", "SUMMARY_AI_DF_TRUTH_PATCH", disabled_env="DISABLE_SUMMARY_AI_DF_TRUTH_PATCH")
_install_module("core.startup.summary_mtf_early_ready_patch", "SUMMARY_MTF_EARLY_READY", disabled_env="DISABLE_SUMMARY_MTF_EARLY_READY_PATCH")
_install_module("trading.audit_logging.install_audit_logging", "AUDIT_LOGGING", disabled_env="DISABLE_AUDIT_LOGGING")
_install_module("core.startup.summary_controller_concat_duplicate_columns_patch", "SUMMARY_CONTROLLER_DUPCOL_PATCH", disabled_env="DISABLE_SUMMARY_CONTROLLER_DUPCOL_PATCH")
_install_summary_mtf_catchup_safely()
_write_boot_evidence("SITECUSTOMIZE_DONE")
