# ============================================================
# File   : scripts/data_collectors_runner.py
# Version: DATA-COLLECTORS-PARENT-RUNNER-V5-CLEAR-MAIN-DB-SKIP-ENV
# ------------------------------------------------------------
# Purpose:
#   - DB作成 / ランキング取得 / PUSH受信 / Yahoo補完 / サマリーDB保存を一括起動する親runner
#   - main.py とは別プロセスで動かす
#
# V5:
#   ✔ child process env で main.py専用DB保存skip設定を明示解除
#   ✔ SUMMARY_SKIP_DB_SAVE_IN_MAIN=0
#   ✔ SUMMARY_MAIN_ENTRY_ONLY=0
#   ✔ SUMMARY_DB_WRITER_ROLE=database
#   ✔ AUTOSTOCK_SUMMARY_DB_WRITER=1
#   ✔ AUTOSTOCK_SUMMARY_SAVE_MODE=save
#
# 背景:
#   親環境に SUMMARY_SKIP_DB_SAVE_IN_MAIN=1 / SUMMARY_DB_WRITER_ROLE=entry_only が残ると、
#   summary_database_runner 側でも cache_writer が main_entry_only と判定し、
#   PUSH summary DB保存をskipする。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_collectors.config import HEARTBEAT_INTERVAL_SEC, RESTART_DELAY_SEC
from data_collectors.logging_setup import setup_logging

try:
    from trading.runtime_persistence.heartbeat_watchdog import heartbeat, mark_component_start, mark_component_stop
except Exception:  # heartbeat 自体が壊れてもcollectorは止めない
    def heartbeat(*args, **kwargs):
        return None
    def mark_component_start(*args, **kwargs):
        return None
    def mark_component_stop(*args, **kwargs):
        return None


DB_PREPARE_RUNNER = SCRIPTS_DIR / "db_prepare_runner.py"
RANKING_COLLECTOR_RUNNER = SCRIPTS_DIR / "ranking_collector_runner.py"
PUSH_RECEIVER_RUNNER = SCRIPTS_DIR / "push_receiver_runner.py"
YAHOO_COMPLEMENT_RUNNER = SCRIPTS_DIR / "yahoo_complement_runner.py"
SUMMARY_DATABASE_RUNNER = SCRIPTS_DIR / "summary_database_runner.py"

PROCESS_SPECS = {
    "ranking_collector": RANKING_COLLECTOR_RUNNER,
    "push_receiver": PUSH_RECEIVER_RUNNER,
    "yahoo_complement": YAHOO_COMPLEMENT_RUNNER,
    "summary_database": SUMMARY_DATABASE_RUNNER,
}

_STOP = False


def _python_exe() -> str:
    return sys.executable


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    old = env.get("PYTHONPATH", "")
    root = str(PROJECT_ROOT)
    env["PYTHONPATH"] = root + (os.pathsep + old if old else "")

    env["AUTOSTOCK_DATA_COLLECTORS_PROCESS"] = "1"
    env["AUTOSTOCK_MAIN_DATABASE_PROCESS"] = "1"
    env["AUTOSTOCK_EXTERNAL_DATA_COLLECTORS"] = "1"

    env["AUTOSTOCK_YAHOO_COMPLEMENT_OWNER"] = "database"
    env["AUTOSTOCK_SUMMARY_SAVE_OWNER"] = "database"
    env["AUTOSTOCK_SUMMARY_SAVE_MODE"] = "save"
    env["AUTOSTOCK_SUMMARY_DB_WRITER"] = "1"

    # main.py専用設定を子プロセスへ持ち込まない。
    # summary_database_runner / cache_writer はこの3つを見るため、ここで強制解除する。
    env["SUMMARY_SKIP_DB_SAVE_IN_MAIN"] = "0"
    env["SUMMARY_MAIN_ENTRY_ONLY"] = "0"
    env["SUMMARY_DB_WRITER_ROLE"] = "database"

    # data collector側ではエントリー実行しない。
    env.setdefault("ENABLE_SUMMARY_ENTRY_TICK", "0")

    return env


def _check_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"runner not found: {path}")


def _run_db_prepare(logger: logging.Logger) -> None:
    _check_file(DB_PREPARE_RUNNER)

    cmd = [_python_exe(), str(DB_PREPARE_RUNNER)]
    env = _build_env()
    logger.info("[DATA COLLECTORS] db_prepare start cmd=%s", cmd)
    logger.info(
        "[DATA COLLECTORS] child env summary owner=%s mode=%s writer=%s skip_main=%s role=%s",
        env.get("AUTOSTOCK_SUMMARY_SAVE_OWNER"),
        env.get("AUTOSTOCK_SUMMARY_SAVE_MODE"),
        env.get("AUTOSTOCK_SUMMARY_DB_WRITER"),
        env.get("SUMMARY_SKIP_DB_SAVE_IN_MAIN"),
        env.get("SUMMARY_DB_WRITER_ROLE"),
    )
    mark_component_start("db_prepare_runner", {"cmd": cmd})

    ret = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=env,
        text=True,
    )

    if ret.returncode != 0:
        heartbeat("db_prepare_runner", status="ERROR", detail={"returncode": ret.returncode})
        raise RuntimeError(f"db_prepare failed returncode={ret.returncode}")

    heartbeat("db_prepare_runner", status="DONE", detail={"returncode": ret.returncode})
    logger.info("[DATA COLLECTORS] db_prepare done")


def _start_child(logger: logging.Logger, name: str, path: Path) -> subprocess.Popen:
    _check_file(path)

    cmd = [_python_exe(), str(path)]
    env = _build_env()
    logger.info("[DATA COLLECTORS] start child name=%s cmd=%s", name, cmd)
    logger.info(
        "[DATA COLLECTORS] child env name=%s summary owner=%s mode=%s writer=%s skip_main=%s main_entry_only=%s role=%s yahoo_owner=%s",
        name,
        env.get("AUTOSTOCK_SUMMARY_SAVE_OWNER"),
        env.get("AUTOSTOCK_SUMMARY_SAVE_MODE"),
        env.get("AUTOSTOCK_SUMMARY_DB_WRITER"),
        env.get("SUMMARY_SKIP_DB_SAVE_IN_MAIN"),
        env.get("SUMMARY_MAIN_ENTRY_ONLY"),
        env.get("SUMMARY_DB_WRITER_ROLE"),
        env.get("AUTOSTOCK_YAHOO_COMPLEMENT_OWNER"),
    )
    mark_component_start(f"collector_{name}", {"cmd": cmd, "path": str(path)})

    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=env,
        text=True,
    )

    heartbeat(f"collector_{name}", status="STARTED", detail={"pid": proc.pid, "cmd": cmd})
    logger.info("[DATA COLLECTORS] child started name=%s pid=%s", name, proc.pid)
    return proc


def _terminate_child(logger: logging.Logger, name: str, proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        mark_component_stop(f"collector_{name}", {"pid": proc.pid, "returncode": proc.poll(), "already_stopped": True})
        return

    logger.warning("[DATA COLLECTORS] terminate child name=%s pid=%s", name, proc.pid)
    heartbeat(f"collector_{name}", status="TERMINATING", detail={"pid": proc.pid})

    try:
        proc.terminate()
        proc.wait(timeout=10)
        mark_component_stop(f"collector_{name}", {"pid": proc.pid, "returncode": proc.poll()})
    except subprocess.TimeoutExpired:
        logger.error("[DATA COLLECTORS] kill child name=%s pid=%s", name, proc.pid)
        proc.kill()
        proc.wait(timeout=5)
        mark_component_stop(f"collector_{name}", {"pid": proc.pid, "returncode": proc.poll(), "killed": True})


def _handle_signal(signum, frame) -> None:
    global _STOP
    _STOP = True


def main() -> int:
    logger = setup_logging("data_collectors_runner")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("=" * 80)
    logger.info("[DATA COLLECTORS] START project_root=%s python=%s", PROJECT_ROOT, _python_exe())
    logger.info("[DATA COLLECTORS] specs=%s", {k: str(v) for k, v in PROCESS_SPECS.items()})
    logger.info("=" * 80)

    _run_db_prepare(logger)

    procs: Dict[str, subprocess.Popen] = {}

    try:
        for name, path in PROCESS_SPECS.items():
            procs[name] = _start_child(logger, name, path)
            time.sleep(0.5)

        last_hb = 0.0

        while not _STOP:
            now = time.time()

            for name, proc in list(procs.items()):
                rc = proc.poll()
                if rc is None:
                    continue

                logger.error("[DATA COLLECTORS] child exited name=%s pid=%s returncode=%s -> restart", name, proc.pid, rc)
                heartbeat(f"collector_{name}", status="EXITED", detail={"pid": proc.pid, "returncode": rc})
                mark_component_stop(f"collector_{name}", {"pid": proc.pid, "returncode": rc})

                if _STOP:
                    continue

                time.sleep(RESTART_DELAY_SEC)
                procs[name] = _start_child(logger, name, PROCESS_SPECS[name])

            if now - last_hb >= HEARTBEAT_INTERVAL_SEC:
                last_hb = now
                heartbeat(
                    "data_collectors_runner",
                    status="RUNNING",
                    detail={
                        "children": {
                            name: {"pid": proc.pid, "returncode": proc.poll()}
                            for name, proc in procs.items()
                        }
                    },
                )

            time.sleep(1.0)

    finally:
        logger.warning("[DATA COLLECTORS] stopping children")
        for name, proc in list(procs.items()):
            try:
                _terminate_child(logger, name, proc)
            except Exception:
                logger.exception("[DATA COLLECTORS] child terminate failed name=%s", name)

        heartbeat("data_collectors_runner", status="STOPPED", detail={})
        logger.warning("[DATA COLLECTORS] STOPPED")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
