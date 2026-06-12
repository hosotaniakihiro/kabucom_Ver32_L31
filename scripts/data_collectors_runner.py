# ============================================================
# File   : scripts/data_collectors_runner.py
# Version: DATA-COLLECTORS-PARENT-RUNNER-V10-RANKING-DELAYED-START
# ------------------------------------------------------------
# Purpose:
#   - DB作成 / ランキング取得 / PUSH受信 / Yahoo補完 / サマリーDB保存を一括起動する親runner
#   - main.py とは別プロセスで動かす
#
# V10:
#   ✔ ranking_collector を既定で起動に戻す。
#   ✔ 起動時 0xC0000006 再発を避けるため、ranking_collector は他collector起動後に遅延起動する。
#   ✔ 明示的に止めたい場合だけ AUTOSTOCK_SKIP_RANKING_COLLECTOR=1 を使う。
#
# V9:
#   ✔ main_database.py CPU高止まり対策として、子プロセス起動前の環境変数で
#     起動時の重い修復/MTF indicator fill/並列summaryを抑制する。
#   ✔ SQLite temp/cache はメモリ寄せにする。
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
from typing import Dict

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_collectors.config import HEARTBEAT_INTERVAL_SEC, RESTART_DELAY_SEC
from data_collectors.logging_setup import setup_logging

_STOP = False
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in _TRUE:
            return True
        if raw in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = str(os.getenv(name, "")).strip()
        if raw:
            return float(raw)
    except Exception:
        pass
    return float(default)


def _parent_heartbeat_enabled() -> bool:
    # 0xC0000006 対策として、親runnerからNAS heartbeat DBへは既定で書かない。
    return _env_bool("AUTOSTOCK_COLLECTOR_PARENT_HEARTBEAT", False)


def _safe_heartbeat(component: str, status: str = "OK", detail: dict | None = None) -> None:
    if not _parent_heartbeat_enabled():
        return
    try:
        from trading.runtime_persistence.heartbeat_watchdog import heartbeat
        heartbeat(component, status=status, detail=detail)
    except Exception:
        logging.getLogger(__name__).debug(
            "[DATA COLLECTORS] heartbeat skipped component=%s status=%s", component, status, exc_info=True
        )


def _safe_mark_start(component: str, detail: dict | None = None) -> None:
    if not _parent_heartbeat_enabled():
        return
    try:
        from trading.runtime_persistence.heartbeat_watchdog import mark_component_start
        mark_component_start(component, detail)
    except Exception:
        logging.getLogger(__name__).debug(
            "[DATA COLLECTORS] mark start skipped component=%s", component, exc_info=True
        )


def _safe_mark_stop(component: str, detail: dict | None = None) -> None:
    if not _parent_heartbeat_enabled():
        return
    try:
        from trading.runtime_persistence.heartbeat_watchdog import mark_component_stop
        mark_component_stop(component, detail)
    except Exception:
        logging.getLogger(__name__).debug(
            "[DATA COLLECTORS] mark stop skipped component=%s", component, exc_info=True
        )


DB_PREPARE_RUNNER = SCRIPTS_DIR / "db_prepare_runner.py"
RANKING_COLLECTOR_RUNNER = SCRIPTS_DIR / "ranking_collector_runner.py"
PUSH_RECEIVER_RUNNER = SCRIPTS_DIR / "push_receiver_runner.py"
YAHOO_COMPLEMENT_RUNNER = SCRIPTS_DIR / "yahoo_complement_runner.py"
SUMMARY_DATABASE_RUNNER = SCRIPTS_DIR / "summary_database_runner.py"

BASE_PROCESS_SPECS = {
    "push_receiver": PUSH_RECEIVER_RUNNER,
    "yahoo_complement": YAHOO_COMPLEMENT_RUNNER,
    "summary_database": SUMMARY_DATABASE_RUNNER,
    # ranking は最後に起動する。起動直後のDB/NAS負荷を避けるため main() 側で遅延する。
    "ranking_collector": RANKING_COLLECTOR_RUNNER,
}


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
    env["SUMMARY_SKIP_DB_SAVE_IN_MAIN"] = "0"
    env["SUMMARY_MAIN_ENTRY_ONLY"] = "0"
    env["SUMMARY_DB_WRITER_ROLE"] = "database"

    # data collector側ではエントリー/ランキング/表示系を実行しない。
    env["ENABLE_SUMMARY_ENTRY_TICK"] = "0"
    env["ENABLE_RANKING_SUMMARY_TICK"] = "0"
    env["SUMMARY_DATABASE_RUNNER_DISPLAY"] = "0"
    env["SUMMARY_DISCORD_EMPTY_FALLBACK_NOTIFY"] = "0"

    # CPU高止まり対策: 1m/3m/5mを毎分すべて並列実行しない。
    env["SUMMARY_PUSH_DISPLAY_ALL_INTERVALS"] = "0"
    env["SUMMARY_PARALLEL_FORCE_1_3_5"] = "0"
    env["SUMMARY_PARALLEL_INTERVAL_WORKERS"] = "1"
    env["SUMMARY_PUSH_BG_INTERVAL_WORKERS"] = "1"
    env["SUMMARY_PUSH_BG_ALL_INTERVALS"] = "0"
    env["SUMMARY_PUSH_BG_LONG_INTERVALS"] = "0"

    # 起動時の重い補修処理を抑制。必要な場合だけ手動で有効化する。
    env.setdefault("SUMMARY_EXISTING_NULL_REPAIR_ENABLED", "0")
    env.setdefault("SUMMARY_EXISTING_NULL_REPAIR_STARTUP", "0")
    env.setdefault("SUMMARY_EXISTING_NULL_REPAIR_MAX_ROWS", "20000")
    env.setdefault("SUMMARY_MTF_INDICATOR_FILL_ENABLED", "0")
    env.setdefault("SUMMARY_MTF_INDICATOR_FILL_STARTUP", "0")
    env.setdefault("SUMMARY_MTF_INDICATOR_FILL_HISTORY_DAYS", "1")
    env.setdefault("SUMMARY_MTF_INDICATOR_FILL_CHUNK", "100")
    env.setdefault("SUMMARY_MTF_INDICATOR_FILL_RETRIES", "2")
    env.setdefault("SUMMARY_MTF_CATCHUP_INDICATOR_FILL", "0")

    # 親runner/子起動直後の監視DB書き込みでNAS SQLiteを踏まない。
    env.setdefault("AUTOSTOCK_COLLECTOR_PARENT_HEARTBEAT", "0")
    env.setdefault("AUTOSTOCK_DISABLE_NAS_HEARTBEAT", "1")

    # ---- Memory / SQLite WAL guard defaults ----
    env.setdefault("PYTHONMALLOC", "malloc")
    env.setdefault("MALLOC_TRIM_THRESHOLD_", "65536")
    env.setdefault("OMP_NUM_THREADS", "1")
    env.setdefault("OPENBLAS_NUM_THREADS", "1")
    env.setdefault("MKL_NUM_THREADS", "1")
    env.setdefault("NUMEXPR_NUM_THREADS", "1")

    env.setdefault("SQLITE_MEMORY_PRAGMAS_ENABLED", "1")
    env.setdefault("SQLITE_MEMORY_TEMP_STORE", "MEMORY")
    env.setdefault("SQLITE_MEMORY_CACHE_KB", "-65536")
    env.setdefault("SQLITE_MMAP_SIZE_BYTES", "268435456")
    env.setdefault("SQLITE_CACHE_SPILL_OFF", "1")
    env.setdefault("SQLITE_BUSY_TIMEOUT_MS", "5000")

    # ranking DB writer WAL肥大化対策。
    env.setdefault("RANKING_WRITER_PASSIVE_CHECKPOINT_AFTER_FLUSH", "1")
    env.setdefault("RANKING_WRITER_IDLE_PASSIVE_CHECKPOINT", "1")
    env.setdefault("RANKING_WRITER_IDLE_CHECKPOINT_SEC", "60")
    env.setdefault("RANKING_WRITER_WAL_TRUNCATE_MB", "64")
    env.setdefault("RANKING_WRITER_WAL_AUTOCHECKPOINT", "100")
    env.setdefault("RANKING_SQLITE_CACHE_KB", "-65536")
    env.setdefault("RANKING_SQLITE_TEMP_STORE", "MEMORY")
    env.setdefault("SUMMARY_SQLITE_CACHE_KB", "-131072")
    env.setdefault("SUMMARY_SQLITE_TEMP_STORE", "MEMORY")
    env.setdefault("PUSH_SQLITE_CACHE_KB", "-65536")
    env.setdefault("PUSH_SQLITE_TEMP_STORE", "MEMORY")
    env.setdefault("YAHOO_SQLITE_CACHE_KB", "-65536")
    env.setdefault("YAHOO_SQLITE_TEMP_STORE", "MEMORY")
    env.setdefault("RANKING_WRITER_GC_AFTER_FLUSH", "1")
    env.setdefault("RANKING_WRITER_IDLE_GC", "1")

    # ranking 保存は軽量運用を既定にする。legacy保存はfull時だけに限定する既存設計を尊重。
    env.setdefault("RANKING_WRITER_BUFFER_SIZE", "1")
    env.setdefault("RANKING_WRITER_FLUSH_INTERVAL_SEC", "1.0")
    env.setdefault("RANKING_WRITER_FLUSH_ON_THRESHOLD", "1")

    return env


def _process_specs(logger: logging.Logger) -> dict[str, Path]:
    specs = dict(BASE_PROCESS_SPECS)

    # ranking_collector は既定で起動する。
    # 起動直後の 0xC0000006 / NAS負荷が気になる場合は、main() 側で遅延起動する。
    # 完全に止めたい場合だけ AUTOSTOCK_SKIP_RANKING_COLLECTOR=1 を指定する。
    enable_ranking = _env_bool("AUTOSTOCK_ENABLE_RANKING_COLLECTOR", True)
    skip_ranking = _env_bool("AUTOSTOCK_SKIP_RANKING_COLLECTOR", not enable_ranking)
    if skip_ranking and "ranking_collector" in specs:
        specs.pop("ranking_collector", None)
        logger.warning(
            "[DATA COLLECTORS] ranking_collector skipped by explicit env. "
            "Set AUTOSTOCK_SKIP_RANKING_COLLECTOR=0 or AUTOSTOCK_ENABLE_RANKING_COLLECTOR=1 to enable."
        )

    # push_receiver も切り分けたい場合だけ明示スキップ可能。
    if _env_bool("AUTOSTOCK_SKIP_PUSH_RECEIVER", False) and "push_receiver" in specs:
        specs.pop("push_receiver", None)
        logger.warning("[DATA COLLECTORS] push_receiver skipped by AUTOSTOCK_SKIP_PUSH_RECEIVER=1")

    return specs


def _check_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"runner not found: {path}")


def _run_db_prepare(logger: logging.Logger) -> None:
    _check_file(DB_PREPARE_RUNNER)
    cmd = [_python_exe(), str(DB_PREPARE_RUNNER)]
    env = _build_env()
    logger.info("[DATA COLLECTORS] db_prepare start cmd=%s", cmd)
    logger.info(
        "[DATA COLLECTORS] child env summary owner=%s mode=%s writer=%s skip_main=%s role=%s wal_truncate_mb=%s sqlite_cache=%s parent_hb=%s summary_parallel=%s mtf_fill=%s null_repair=%s",
        env.get("AUTOSTOCK_SUMMARY_SAVE_OWNER"),
        env.get("AUTOSTOCK_SUMMARY_SAVE_MODE"),
        env.get("AUTOSTOCK_SUMMARY_DB_WRITER"),
        env.get("SUMMARY_SKIP_DB_SAVE_IN_MAIN"),
        env.get("SUMMARY_DB_WRITER_ROLE"),
        env.get("RANKING_WRITER_WAL_TRUNCATE_MB"),
        env.get("RANKING_SQLITE_CACHE_KB"),
        env.get("AUTOSTOCK_COLLECTOR_PARENT_HEARTBEAT"),
        env.get("SUMMARY_PARALLEL_INTERVAL_WORKERS"),
        env.get("SUMMARY_MTF_INDICATOR_FILL_ENABLED"),
        env.get("SUMMARY_EXISTING_NULL_REPAIR_ENABLED"),
    )
    _safe_mark_start("db_prepare_runner", {"cmd": cmd})
    ret = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env, text=True)
    if ret.returncode != 0:
        _safe_heartbeat("db_prepare_runner", status="ERROR", detail={"returncode": ret.returncode})
        raise RuntimeError(f"db_prepare failed returncode={ret.returncode}")
    _safe_heartbeat("db_prepare_runner", status="DONE", detail={"returncode": ret.returncode})
    logger.info("[DATA COLLECTORS] db_prepare done")


def _start_child(logger: logging.Logger, name: str, path: Path) -> subprocess.Popen:
    _check_file(path)
    cmd = [_python_exe(), str(path)]
    env = _build_env()
    logger.info("[DATA COLLECTORS] start child name=%s cmd=%s", name, cmd)
    logger.info(
        "[DATA COLLECTORS] child env name=%s summary owner=%s mode=%s writer=%s skip_main=%s main_entry_only=%s role=%s yahoo_owner=%s wal_truncate_mb=%s sqlite_cache=%s parent_hb=%s summary_parallel=%s mtf_fill=%s null_repair=%s",
        name,
        env.get("AUTOSTOCK_SUMMARY_SAVE_OWNER"),
        env.get("AUTOSTOCK_SUMMARY_SAVE_MODE"),
        env.get("AUTOSTOCK_SUMMARY_DB_WRITER"),
        env.get("SUMMARY_SKIP_DB_SAVE_IN_MAIN"),
        env.get("SUMMARY_MAIN_ENTRY_ONLY"),
        env.get("SUMMARY_DB_WRITER_ROLE"),
        env.get("AUTOSTOCK_YAHOO_COMPLEMENT_OWNER"),
        env.get("RANKING_WRITER_WAL_TRUNCATE_MB"),
        env.get("RANKING_SQLITE_CACHE_KB"),
        env.get("AUTOSTOCK_COLLECTOR_PARENT_HEARTBEAT"),
        env.get("SUMMARY_PARALLEL_INTERVAL_WORKERS"),
        env.get("SUMMARY_MTF_INDICATOR_FILL_ENABLED"),
        env.get("SUMMARY_EXISTING_NULL_REPAIR_ENABLED"),
    )

    # 重要: Popen前にNAS heartbeat DBへ書かない。ここで落ちると child started が出ない。
    proc = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), env=env, text=True)
    logger.info("[DATA COLLECTORS] child started name=%s pid=%s", name, proc.pid)

    _safe_mark_start(f"collector_{name}", {"cmd": cmd, "path": str(path), "pid": proc.pid})
    _safe_heartbeat(f"collector_{name}", status="STARTED", detail={"pid": proc.pid, "cmd": cmd})
    return proc


def _terminate_child(logger: logging.Logger, name: str, proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        _safe_mark_stop(f"collector_{name}", {"pid": proc.pid, "returncode": proc.poll(), "already_stopped": True})
        return
    logger.warning("[DATA COLLECTORS] terminate child name=%s pid=%s", name, proc.pid)
    _safe_heartbeat(f"collector_{name}", status="TERMINATING", detail={"pid": proc.pid})
    try:
        proc.terminate()
        proc.wait(timeout=10)
        _safe_mark_stop(f"collector_{name}", {"pid": proc.pid, "returncode": proc.poll()})
    except subprocess.TimeoutExpired:
        logger.error("[DATA COLLECTORS] kill child name=%s pid=%s", name, proc.pid)
        proc.kill()
        proc.wait(timeout=5)
        _safe_mark_stop(f"collector_{name}", {"pid": proc.pid, "returncode": proc.poll(), "killed": True})


def _handle_signal(signum, frame) -> None:
    global _STOP
    _STOP = True


def main() -> int:
    logger = setup_logging("data_collectors_runner")
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    specs = _process_specs(logger)

    logger.info("=" * 80)
    logger.info("[DATA COLLECTORS] START project_root=%s python=%s", PROJECT_ROOT, _python_exe())
    logger.info("[DATA COLLECTORS] specs=%s", {k: str(v) for k, v in specs.items()})
    logger.info("[DATA COLLECTORS] parent heartbeat enabled=%s", _parent_heartbeat_enabled())
    logger.info("=" * 80)

    _run_db_prepare(logger)
    procs: Dict[str, subprocess.Popen] = {}
    try:
        for name, path in specs.items():
            if name == "ranking_collector":
                delay_sec = max(0.0, _env_float("AUTOSTOCK_RANKING_COLLECTOR_START_DELAY_SEC", 20.0))
                if delay_sec > 0:
                    logger.warning(
                        "[DATA COLLECTORS] ranking_collector delayed start waiting %.1fs after db/push/yahoo/summary startup",
                        delay_sec,
                    )
                    time.sleep(delay_sec)
            procs[name] = _start_child(logger, name, path)
            time.sleep(1.0)
        last_hb = 0.0
        while not _STOP:
            now = time.time()
            for name, proc in list(procs.items()):
                rc = proc.poll()
                if rc is None:
                    continue
                logger.error("[DATA COLLECTORS] child exited name=%s pid=%s returncode=%s -> restart", name, proc.pid, rc)
                _safe_heartbeat(f"collector_{name}", status="EXITED", detail={"pid": proc.pid, "returncode": rc})
                _safe_mark_stop(f"collector_{name}", {"pid": proc.pid, "returncode": rc})
                if _STOP:
                    continue
                time.sleep(RESTART_DELAY_SEC)
                procs[name] = _start_child(logger, name, specs[name])
            if now - last_hb >= HEARTBEAT_INTERVAL_SEC:
                last_hb = now
                _safe_heartbeat(
                    "data_collectors_runner",
                    status="RUNNING",
                    detail={"children": {name: {"pid": proc.pid, "returncode": proc.poll()} for name, proc in procs.items()}},
                )
            time.sleep(1.0)
    finally:
        logger.warning("[DATA COLLECTORS] stopping children")
        for name, proc in list(procs.items()):
            try:
                _terminate_child(logger, name, proc)
            except Exception:
                logger.exception("[DATA COLLECTORS] child terminate failed name=%s", name)
        _safe_heartbeat("data_collectors_runner", status="STOPPED", detail={})
        logger.warning("[DATA COLLECTORS] STOPPED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
