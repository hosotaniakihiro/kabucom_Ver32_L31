# ============================================================
# File   : scripts/data_collectors_runner.py
# Version: DATA-COLLECTORS-PARENT-RUNNER-V2-WITH-YAHOO-COMPLEMENT
# ------------------------------------------------------------
# Purpose:
#   - DB作成 / ランキング取得 / PUSH受信 / Yahoo補完を一括起動する親runner
#   - main.py とは別プロセスで動かす
#
# V2:
#   ✔ yahoo_complement_runner.py を子プロセスとして起動
#   ✔ Yahoo取得・Yahoo 1分足保存・YahooサマリーDB反映は main_database 側に集約
#   ✔ main.py 側はDBに保存済みのYahoo補完サマリーを読むだけにする
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


DB_PREPARE_RUNNER = SCRIPTS_DIR / "db_prepare_runner.py"
RANKING_COLLECTOR_RUNNER = SCRIPTS_DIR / "ranking_collector_runner.py"
PUSH_RECEIVER_RUNNER = SCRIPTS_DIR / "push_receiver_runner.py"
YAHOO_COMPLEMENT_RUNNER = SCRIPTS_DIR / "yahoo_complement_runner.py"

PROCESS_SPECS = {
    "ranking_collector": RANKING_COLLECTOR_RUNNER,
    "push_receiver": PUSH_RECEIVER_RUNNER,
    "yahoo_complement": YAHOO_COMPLEMENT_RUNNER,
}

_STOP = False


def _python_exe() -> str:
    return sys.executable


def _build_env() -> dict[str, str]:
    env = os.environ.copy()
    old = env.get("PYTHONPATH", "")
    root = str(PROJECT_ROOT)
    env["PYTHONPATH"] = root + (os.pathsep + old if old else "")

    # data collectors 側が Yahoo補完保存の owner。
    # main.py 側はこの処理を直接起動しない。
    env["AUTOSTOCK_DATA_COLLECTORS_PROCESS"] = "1"
    env.setdefault("AUTOSTOCK_EXTERNAL_DATA_COLLECTORS", "1")
    env.setdefault("AUTOSTOCK_YAHOO_COMPLEMENT_OWNER", "database")

    return env


def _check_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"runner not found: {path}")


def _run_db_prepare(logger: logging.Logger) -> None:
    _check_file(DB_PREPARE_RUNNER)

    cmd = [_python_exe(), str(DB_PREPARE_RUNNER)]
    logger.info("[DATA COLLECTORS] db_prepare start cmd=%s", cmd)

    ret = subprocess.run(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=_build_env(),
        text=True,
    )

    if ret.returncode != 0:
        raise RuntimeError(f"db_prepare failed returncode={ret.returncode}")

    logger.info("[DATA COLLECTORS] db_prepare done")


def _start_child(logger: logging.Logger, name: str, path: Path) -> subprocess.Popen:
    _check_file(path)

    cmd = [_python_exe(), str(path)]
    logger.info("[DATA COLLECTORS] start child name=%s cmd=%s", name, cmd)

    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=_build_env(),
        text=True,
    )

    logger.info("[DATA COLLECTORS] child started name=%s pid=%s", name, proc.pid)
    return proc


def _terminate_child(logger: logging.Logger, name: str, proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return

    logger.warning("[DATA COLLECTORS] terminate child name=%s pid=%s", name, proc.pid)

    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        logger.warning("[DATA COLLECTORS] kill child name=%s pid=%s", name, proc.pid)
        proc.kill()
    except Exception:
        logger.exception("[DATA COLLECTORS] terminate failed name=%s", name)


def _handle_signal(signum, frame) -> None:
    global _STOP
    _STOP = True


def main() -> int:
    logger = setup_logging("data_collectors_runner")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    logger.info("=" * 80)
    logger.info("[DATA COLLECTORS] START")
    logger.info("[DATA COLLECTORS] PROJECT_ROOT=%s", PROJECT_ROOT)
    logger.info("[DATA COLLECTORS] PYTHON=%s", _python_exe())
    logger.info("[DATA COLLECTORS] process_specs=%s", {k: str(v) for k, v in PROCESS_SPECS.items()})
    logger.info("=" * 80)

    try:
        _run_db_prepare(logger)
    except Exception:
        logger.exception("[DATA COLLECTORS] db_prepare failed. abort.")
        return 1

    children: Dict[str, subprocess.Popen] = {}

    for name, path in PROCESS_SPECS.items():
        try:
            children[name] = _start_child(logger, name, path)
        except Exception:
            logger.exception("[DATA COLLECTORS] child start failed name=%s", name)

    last_heartbeat = 0.0

    try:
        while not _STOP:
            now = time.time()

            for name, path in PROCESS_SPECS.items():
                proc: Optional[subprocess.Popen] = children.get(name)

                if proc is None:
                    logger.warning("[DATA COLLECTORS] child missing name=%s. restart.", name)
                    time.sleep(RESTART_DELAY_SEC)
                    children[name] = _start_child(logger, name, path)
                    continue

                ret = proc.poll()
                if ret is not None:
                    logger.error(
                        "[DATA COLLECTORS] child exited name=%s pid=%s returncode=%s. restart after %.1fs",
                        name,
                        proc.pid,
                        ret,
                        RESTART_DELAY_SEC,
                    )
                    time.sleep(RESTART_DELAY_SEC)
                    children[name] = _start_child(logger, name, path)

            if now - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
                parts = []
                for name, proc in children.items():
                    parts.append(f"{name}:pid={proc.pid}:alive={proc.poll() is None}")

                logger.info(
                    "[DATA COLLECTORS] heartbeat time=%s %s",
                    dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    " | ".join(parts),
                )
                last_heartbeat = now

            time.sleep(1.0)

    finally:
        logger.warning("[DATA COLLECTORS] stopping children...")
        for name, proc in children.items():
            _terminate_child(logger, name, proc)
        logger.warning("[DATA COLLECTORS] STOPPED")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
