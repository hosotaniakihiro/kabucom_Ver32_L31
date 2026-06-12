# ============================================================
# File   : main_database.py
# Version: DATA-COLLECTORS-MAIN-DATABASE-ENTRY-V4-SUMMARY-SQLITE-LOCK-TOLERANCE
# ------------------------------------------------------------
# Purpose:
#   - DB作成 / ランキング取得 / PUSH銘柄登録 / PUSH受信 を起動する入口
#   - 既存 main.py とは分離する
#   - 実体は scripts/data_collectors_runner.py に委譲する
# ============================================================

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from configparser import ConfigParser


PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    os.chdir(str(PROJECT_ROOT))
except Exception:
    pass

logger = logging.getLogger(__name__)


def _ensure_basic_logging() -> None:
    try:
        root = logging.getLogger()
        if not root.handlers:
            logging.basicConfig(
                level=logging.INFO,
                format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            )
        root.setLevel(logging.INFO)
    except Exception:
        pass


def _install_cpu_guard_env() -> None:
    try:
        from core.startup.main_database_cpu_guard_env import install
        ok = install()
        logger.info("[MAIN DATABASE] cpu guard env installed ok=%s", ok)
    except Exception:
        logger.exception("[MAIN DATABASE] cpu guard env install failed; continue")


def _install_summary_sqlite_lock_tolerance() -> None:
    """Install summary DB lock tolerance before spawning child collectors.

    The actual summary/Yahoo/MTF work runs in child processes. Installing this
    patch here is still useful because it sets environment defaults that are
    inherited by those child processes. The sqlite3.connect monkey patch also
    protects any summary DB access done directly in main_database.py.
    """
    try:
        from core.startup.summary_sqlite_lock_tolerance_patch import install
        ok = install()
        logger.warning("[MAIN DATABASE] summary sqlite lock tolerance installed ok=%s", ok)
    except Exception:
        logger.exception("[MAIN DATABASE] summary sqlite lock tolerance install failed; continue")


def _read_api_password_from_settings() -> str:
    conf = ConfigParser()
    conf.read(str(PROJECT_ROOT / "settings.ini"), encoding="utf-8")

    if conf.has_section("aukabu"):
        return conf.get("aukabu", "apipassword", fallback="")

    if conf.has_section("kabusapi"):
        return conf.get("kabusapi", "apipassword", fallback="")

    return ""


def _bootstrap_kabu_token_for_data_collectors() -> bool:
    _ensure_basic_logging()

    try:
        api_password = _read_api_password_from_settings()

        if not api_password:
            logger.error(
                "[MAIN DATABASE] token bootstrap failed: settings.ini apipassword missing"
            )
            return False

        from token_manager import refresh_token, get_valid_token

        token = refresh_token(api_password)

        if not token:
            logger.error("[MAIN DATABASE] token bootstrap failed: empty token returned")
            return False

        try:
            _ = get_valid_token()
        except Exception:
            pass

        try:
            from global_state import global_data
            global_data.token_value = token
        except Exception:
            logger.debug("[MAIN DATABASE] global_data.token_value set skipped", exc_info=True)

        logger.info(
            "[MAIN DATABASE] kabu token refreshed for data collectors token_len=%s",
            len(str(token)),
        )
        return True

    except Exception:
        logger.exception("[MAIN DATABASE] token bootstrap failed")
        return False


def main() -> int:
    _ensure_basic_logging()

    try:
        os.chdir(str(PROJECT_ROOT))
    except Exception:
        logger.exception("[MAIN DATABASE] chdir PROJECT_ROOT failed path=%s", PROJECT_ROOT)
        return 1

    logger.info("========== MAIN DATABASE BOOT START ==========")
    logger.info("[MAIN DATABASE] PROJECT_ROOT=%s", PROJECT_ROOT)
    logger.info("[MAIN DATABASE] cwd=%s", os.getcwd())

    _install_cpu_guard_env()
    _install_summary_sqlite_lock_tolerance()

    try:
        from data_collectors.split_mode import mark_as_data_collector_process
        mark_as_data_collector_process()
    except Exception:
        logger.exception("[MAIN DATABASE] failed to mark data collector process")
        return 1

    if not _bootstrap_kabu_token_for_data_collectors():
        logger.error(
            "[MAIN DATABASE] abort because token bootstrap failed. "
            "Please confirm kabu Station is running and API password is correct."
        )
        return 1

    try:
        from scripts.data_collectors_runner import main as data_collectors_main
    except Exception:
        logger.exception("[MAIN DATABASE] failed to import scripts.data_collectors_runner.main")
        return 1

    return int(data_collectors_main())


if __name__ == "__main__":
    raise SystemExit(main())
