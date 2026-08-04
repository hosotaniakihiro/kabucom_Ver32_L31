# ============================================================
# File   : scripts/yahoo_complement_runner.py
# Version: DATA-COLLECTORS-YAHOO-COMPLEMENT-RUNNER-V2-SAFETY-PATCH
# ------------------------------------------------------------
# Purpose:
#   main_database.py 側で Yahoo 補完を担当する子プロセス。
#
# Responsibilities:
#   - Yahoo 1分足の差分取得
#   - Yahoo 1分足保存
#   - Yahoo由来サマリー計算
#   - summary DB への反映
#
# Design:
#   - main.py ではYahoo補完保存ジョブを起動しない
#   - main.py は summary DB に保存済みのYahoo補完結果を読み、
#     PUSH由来サマリーとマージして利用する
#   - schedule はこのプロセス内だけで回す
#
# V2 Fix (現在は trading/yahoo/pipeline/complement/compute.py 本体へインライン化済み):
#   ✔ Yahoo 3分/5分補完の pd.NA schema エラーを抑止
#   ✔ score全ゼロフレームを slope/macd/rsi から最低限復元
# ============================================================

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

import schedule

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    os.chdir(str(PROJECT_ROOT))
except Exception:
    pass

from data_collectors.logging_setup import setup_logging
from data_collectors.split_mode import mark_as_data_collector_process

logger = logging.getLogger(__name__)
_STOP = False


def _handle_signal(signum, frame) -> None:
    global _STOP
    _STOP = True


def _bootstrap_token_if_needed() -> None:
    """
    親 main_database.py が token refresh 済みの想定だが、
    子プロセス単独起動にも耐えるように最低限確認する。
    """
    try:
        from token_manager import get_valid_token, refresh_token
        token = get_valid_token()
        if token:
            logger.info("[YAHOO COMPLEMENT RUNNER] token available token_len=%s", len(str(token)))
            return

        from configparser import ConfigParser
        conf = ConfigParser()
        conf.read(str(PROJECT_ROOT / "settings.ini"), encoding="utf-8")
        section = "aukabu" if conf.has_section("aukabu") else "kabusapi"
        api_password = conf.get(section, "apipassword", fallback="")
        if not api_password:
            logger.warning("[YAHOO COMPLEMENT RUNNER] apipassword missing; token refresh skipped")
            return

        token = refresh_token(api_password)
        logger.info("[YAHOO COMPLEMENT RUNNER] token refreshed token_len=%s", len(str(token)))

    except Exception:
        logger.exception("[YAHOO COMPLEMENT RUNNER] token bootstrap failed; runtime continues")


def _run_startup_once() -> None:
    try:
        from trading.yahoo.scheduler.complement_scheduler import run_yahoo_complement_once
        logger.info("[YAHOO COMPLEMENT RUNNER] startup yahoo complement once start")
        run_yahoo_complement_once()
        logger.info("[YAHOO COMPLEMENT RUNNER] startup yahoo complement once done")
    except Exception:
        logger.exception("[YAHOO COMPLEMENT RUNNER] startup yahoo complement once failed")


def _register_tasks() -> None:
    try:
        from core.yahoo_tasks import register_yahoo_tasks
        register_yahoo_tasks()
        logger.info("[YAHOO COMPLEMENT RUNNER] yahoo tasks registered jobs=%s", len(schedule.jobs))
    except Exception:
        logger.exception("[YAHOO COMPLEMENT RUNNER] yahoo task registration failed")
        raise


def main() -> int:
    global _STOP

    mark_as_data_collector_process()
    os.environ.setdefault("AUTOSTOCK_YAHOO_COMPLEMENT_OWNER", "database")

    logger = setup_logging("yahoo_complement_runner")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("=" * 80)
    logger.info("[YAHOO COMPLEMENT RUNNER] START")
    logger.info("[YAHOO COMPLEMENT RUNNER] PROJECT_ROOT=%s", PROJECT_ROOT)
    logger.info("[YAHOO COMPLEMENT RUNNER] PYTHON=%s", sys.executable)
    logger.info("[YAHOO COMPLEMENT RUNNER] cwd=%s", os.getcwd())
    logger.info("=" * 80)

    _bootstrap_token_if_needed()
    _run_startup_once()
    _register_tasks()

    last_heartbeat = 0.0

    while not _STOP:
        try:
            schedule.run_pending()

            now = time.time()
            if now - last_heartbeat >= 30.0:
                logger.info(
                    "[YAHOO COMPLEMENT RUNNER] heartbeat jobs=%s next_runs=%s",
                    len(schedule.jobs),
                    [str(getattr(j, "next_run", None)) for j in schedule.jobs[:10]],
                )
                last_heartbeat = now

        except Exception:
            logger.exception("[YAHOO COMPLEMENT RUNNER] loop error")

        time.sleep(0.5)

    logger.warning("[YAHOO COMPLEMENT RUNNER] STOPPED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
