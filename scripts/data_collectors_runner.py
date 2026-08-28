# ============================================================
# File   : scripts/data_collectors_runner.py
# Version: DATA-COLLECTORS-PARENT-RUNNER-V11-CHILD-CONSOLE-CAPTURE
# ------------------------------------------------------------
# Purpose:
#   - DB作成 / ランキング取得 / PUSH受信 / Yahoo補完 / サマリーDB保存を一括起動する親runner
#   - main.py とは別プロセスで動かす
#   - 子プロセスの stdout/stderr を親runnerで受け取り、時刻付きでコンソールとログへ保存する
#
# V11:
#   ✔ db_prepare / push_receiver / yahoo_complement / summary_database / ranking_collector の
#     print / logging / sitecustomize 出力を [CHILD:name] として時刻付き保存する。
#   ✔ 保存先は data_collectors.config.LOG_DIR。既定は X:\logs\console。
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

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_collectors.config import HEARTBEAT_INTERVAL_SEC, RESTART_DELAY_SEC
from data_collectors.logging_setup import setup_logging

_STOP = False

# 子プロセスの起動自体に失敗し続ける場合の再試行間隔の上限。
# NAS が一時的に見えなくなっても、復帰したら自動で立ち上がるようにする。
RESTART_MAX_DELAY_SEC = 60.0

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}
_CAPTURE_THREADS: list[threading.Thread] = []


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

    # 子プロセスの print/logging を親runnerが即時に読めるようにする。
    env.setdefault("PYTHONUNBUFFERED", "1")

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


def _pump_child_output(logger: logging.Logger, name: str, stream) -> None:
    try:
        for line in iter(stream.readline, ""):
            text = str(line).rstrip("\r\n")
            if text:
                logger.info("[CHILD:%s] %s", name, text)
    except Exception:
        logger.exception("[DATA COLLECTORS] child output pump failed name=%s", name)
    finally:
        try:
            stream.close()
        except Exception:
            pass


def _popen_captured(logger: logging.Logger, name: str, cmd: list[str], env: dict[str, str]) -> subprocess.Popen:
    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
    )
    if proc.stdout is not None:
        th = threading.Thread(
            target=_pump_child_output,
            args=(logger, name, proc.stdout),
            name=f"console-capture-{name}-{proc.pid}",
            daemon=True,
        )
        th.start()
        _CAPTURE_THREADS.append(th)
    return proc


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
    proc = _popen_captured(logger, "db_prepare", cmd, env)
    ret = proc.wait()
    if ret != 0:
        _safe_heartbeat("db_prepare_runner", status="ERROR", detail={"returncode": ret})
        raise RuntimeError(f"db_prepare failed returncode={ret}")
    _safe_heartbeat("db_prepare_runner", status="DONE", detail={"returncode": ret})
    logger.info("[DATA COLLECTORS] db_prepare done")


def _start_summary_null_repair(logger: logging.Logger) -> None:
    """summary DB の既存NULL項目補修をBGで開始する。

    この補修は「DB担当プロセスが受け持つ」設計だが、呼び出し口が
    core/startup/__init__.py の main.py 専用ブロックにしか無く、
    実際にはどのプロセスでも走っていなかった。ここが本来の担当プロセス。

    SUMMARY_EXISTING_NULL_REPAIR_ENABLED=0 で無効化できる。
    """
    try:
        from trading.summary.existing_null_repair import start_background_repair

        started = start_background_repair(reason="data_collectors_startup")
        logger.info("[DATA COLLECTORS] summary existing-null repair scheduled=%s", started)
    except Exception:
        logger.exception("[DATA COLLECTORS] summary existing-null repair start failed")


# ============================================================
# P0-1: SMB 障害と アプリ障害を区別する
# ------------------------------------------------------------
# 2026-08-28 11:13 に PUSH DB の書き込みが止まった原因。
#
# 子プロセスが自分のスクリプトすら読めずに即死していた:
#   OSError: [WinError 6] ハンドルが無効です。:
#       'W:\...\.venv\Lib\site-packages\pytz'
#   python.exe: can't open file
#       '\\192.168.0.22\business\...\scripts\push_receiver_runner.py'
#
# 親ログの実測 (09:39-12:07 の 1 プロセス分):
#   WinError 4  (Too many open files in system) : 183 回
#   WinError 6  (ハンドルが無効)                :  42 回
#   WinError 59 (予期しないネットワークエラー)   :  14 回
#   can't open file                             : 102 回
#   push_receiver の start child                : 152 回
#
# つまり W: (ソース + .venv がある SMB 共有) のハンドル/セッションが
# 枯渇し、子プロセスが起動できなくなっていた。
#
# 悪化させていたのは再起動ループ自身:
#   11:13:17 started -> 11:13:24 exited(rc=2)
#   11:13:29 started -> 11:13:35 exited(rc=2)
#   11:13:40 started -> 11:13:46 exited(rc=2)
# 「起動には成功したがすぐ死んだ」場合は fail_counts が増えず、
# backoff が一切かからずに 5 秒間隔で回り続けていた。
# 1 回の spawn は NAS 上の venv から pandas/numpy/sqlalchemy を
# import するので数千回の SMB open を伴う。
# つまり **リトライが SMB 枯渇を悪化させる自己増幅ループ**だった。
#
# 対策:
#   1. spawn 前に安価な健全性プローブを行い、読めないなら spawn しない
#   2. 「起動直後に死んだ」も失敗として backoff に数える
#   3. SMB 由来の失敗を app 由来と分けてログに出す
# ============================================================

# 子がこの秒数生き延びたら「起動成功」とみなす。
CHILD_MIN_HEALTHY_SEC = _env_float("AUTOSTOCK_CHILD_MIN_HEALTHY_SEC", 30.0)

# SMB 障害を示す Windows エラー番号。
_SMB_WINERRORS = {4, 6, 59, 64, 121, 1450}


def _looks_like_smb_fault(exc: BaseException) -> bool:
    try:
        we = getattr(exc, "winerror", None)
        if we is not None and int(we) in _SMB_WINERRORS:
            return True
        errno_v = getattr(exc, "errno", None)
        if errno_v in (24,):          # EMFILE
            return True
        text = str(exc)
        if any(("WinError %d" % w) in text for w in _SMB_WINERRORS):
            return True
        return "can't open file" in text
    except Exception:
        return False


def _probe_paths_readable(logger: logging.Logger, name: str, path: Path) -> bool:
    """spawn 前の安価なプローブ。

    spawn は NAS 上の venv から数千ファイルを読むので非常に高価。
    SMB が死んでいるときに spawn を繰り返すと枯渇を悪化させるだけなので、
    まずインタプリタとスクリプトが読めるかだけ確かめる。
    """
    if not _env_bool("AUTOSTOCK_CHILD_SPAWN_PROBE", True):
        return True
    for label, p in (("interpreter", Path(_python_exe())), ("script", path)):
        try:
            with open(p, "rb") as f:
                f.read(1)
        except Exception as e:  # noqa: BLE001
            logger.error(
                "[DATA COLLECTORS] spawn probe failed name=%s target=%s path=%s "
                "smb_fault=%s err=%r (SMB が読めないので spawn しない。"
                "spawn は NAS venv から数千ファイルを読むため、"
                "枯渇中の再試行は状況を悪化させる)",
                name, label, p, _looks_like_smb_fault(e), e,
            )
            return False
    return True


def _normalized_child_path(path: Path) -> Path:
    """インタプリタと同じ共有表記へ揃える。

    2026-08-28 の実測では
        interpreter = W:\\...\\.venv\\Scripts\\python.exe      (ドライブレター)
        script      = \\\\192.168.0.22\\business\\...\\xxx.py   (UNC)
    と **同じ共有に 2 種類の表記**で触っていた。
    Windows はこれを別セッションとして扱うので、SMB のセッション/
    ハンドルを二重に消費する。インタプリタ側の表記へ揃える。
    """
    if not _env_bool("AUTOSTOCK_CHILD_PATH_NORMALIZE", True):
        return path
    try:
        # .resolve() を使わないこと。
        # マップドライブ (W:) を resolve すると UNC へ展開されてしまい、
        # 「インタプリタ側の表記」が失われて正規化できなくなる
        # (実測: Path('W:\\...\\python.exe').resolve()
        #        -> '\\\\192.168.0.22\\business\\...')。
        exe_s = str(_python_exe())
        path_s = str(path)
        # W:\...\ソースコード\.venv\Scripts\python.exe -> W:\...\ソースコード
        marker = os.sep + ".venv" + os.sep
        if marker not in exe_s:
            return path
        exe_root = exe_s.split(marker)[0]
        root_s = str(PROJECT_ROOT)
        if path_s.startswith(root_s) and exe_root and exe_root != root_s:
            tail = path_s[len(root_s):].lstrip("\\/")
            cand = Path(exe_root) / tail
            if cand.exists():
                return cand
    except Exception:
        pass
    return path


def _start_child(logger: logging.Logger, name: str, path: Path) -> subprocess.Popen:
    path = _normalized_child_path(path)
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
    proc = _popen_captured(logger, name, cmd, env)
    logger.info("[DATA COLLECTORS] child started name=%s pid=%s", name, proc.pid)

    _safe_mark_start(f"collector_{name}", {"cmd": cmd, "path": str(path), "pid": proc.pid})
    _safe_heartbeat(f"collector_{name}", status="STARTED", detail={"pid": proc.pid, "cmd": cmd})
    return proc


def _start_child_safe(logger: logging.Logger, name: str, path: Path) -> Optional[subprocess.Popen]:
    """_start_child の失敗を監視ループの外へ伝播させない版。

    ソースコードも .venv も NAS 上にあるため、一過性のSMB障害で
    _check_file / Popen が例外になることがある
    (実例 2026-08-06 12:44:56: ranking_collector の起動中に例外 ->
     監視ループを抜けて finally に入り、全プロセスが停止した)。

    1つの子の起動失敗でシステム全体を落とさないよう、ここで受け止めて
    None を返す。呼び出し側が間隔を空けて再試行する。
    """
    if not _probe_paths_readable(logger, name, path):
        _safe_heartbeat(f"collector_{name}", status="START_SKIPPED_SMB",
                        detail={"path": str(path), "reason": "spawn_probe_failed"})
        return None
    try:
        return _start_child(logger, name, path)
    except Exception as e:  # noqa: BLE001
        logger.exception(
            "[DATA COLLECTORS] child start failed name=%s path=%s smb_fault=%s -> retry later",
            name, path, _looks_like_smb_fault(e),
        )
        _safe_heartbeat(f"collector_{name}", status="START_FAILED",
                        detail={"path": str(path), "smb_fault": _looks_like_smb_fault(e)})
        return None


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
    _start_summary_null_repair(logger)
    procs: Dict[str, Optional[subprocess.Popen]] = {}
    # 起動に失敗した子の次回再試行時刻。起動できるまで監視ループ内で粘る。
    retry_at: Dict[str, float] = {}
    fail_counts: Dict[str, int] = {}
    started_at: Dict[str, Optional[float]] = {}

    def _schedule_retry(name: str) -> None:
        n = fail_counts.get(name, 0) + 1
        fail_counts[name] = n
        # 連続失敗が続く間は間隔を伸ばす (最大 RESTART_MAX_DELAY_SEC)。
        delay = min(RESTART_DELAY_SEC * (2 ** min(n - 1, 5)), RESTART_MAX_DELAY_SEC)
        retry_at[name] = time.time() + delay
        logger.warning(
            "[DATA COLLECTORS] child start retry scheduled name=%s consecutive_failures=%s next_in=%.1fs",
            name, n, delay,
        )

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
            proc = _start_child_safe(logger, name, path)
            procs[name] = proc
            if proc is None:
                _schedule_retry(name)
            else:
                fail_counts[name] = 0
                started_at[name] = time.time()
            time.sleep(1.0)

        last_hb = 0.0
        while not _STOP:
            now = time.time()
            for name, proc in list(procs.items()):
                if proc is None:
                    # 起動できていない子。再試行時刻になったら立ち上げ直す。
                    if _STOP or now < retry_at.get(name, 0.0):
                        continue
                    new_proc = _start_child_safe(logger, name, specs[name])
                    procs[name] = new_proc
                    if new_proc is None:
                        _schedule_retry(name)
                    else:
                        retry_at.pop(name, None)
                        started_at[name] = time.time()
                        logger.warning("[DATA COLLECTORS] child recovered name=%s pid=%s", name, new_proc.pid)
                    continue

                rc = proc.poll()
                if rc is None:
                    # 十分に生きたら「起動成功」として失敗カウンタを落とす。
                    started = started_at.get(name)
                    if started is not None and (now - started) >= CHILD_MIN_HEALTHY_SEC:
                        if fail_counts.get(name):
                            logger.warning(
                                "[DATA COLLECTORS] child healthy name=%s pid=%s alive=%.1fs "
                                "-> reset consecutive_failures (was %s)",
                                name, proc.pid, now - started, fail_counts.get(name),
                            )
                        fail_counts[name] = 0
                        started_at[name] = None      # 一度だけ判定する
                    continue

                lifetime = None
                started = started_at.get(name)
                if started is not None:
                    lifetime = now - started
                started_at.pop(name, None)

                died_fast = lifetime is not None and lifetime < CHILD_MIN_HEALTHY_SEC
                logger.error(
                    "[DATA COLLECTORS] child exited name=%s pid=%s returncode=%s "
                    "lifetime=%s died_fast=%s -> restart",
                    name, proc.pid, rc,
                    None if lifetime is None else round(lifetime, 1), died_fast,
                )
                _safe_heartbeat(f"collector_{name}", status="EXITED",
                                detail={"pid": proc.pid, "returncode": rc,
                                        "lifetime_sec": lifetime, "died_fast": died_fast})
                _safe_mark_stop(f"collector_{name}", {"pid": proc.pid, "returncode": rc})
                if _STOP:
                    continue

                if died_fast:
                    # P0-1: 「起動はできたが即死」も失敗として backoff する。
                    # 旧実装はここで backoff せず 5 秒間隔で回り続けていた。
                    # 1 spawn = NAS venv から数千ファイルの import なので、
                    # SMB 枯渇中にこれを繰り返すと自分で状況を悪化させる。
                    procs[name] = None
                    _schedule_retry(name)
                    continue

                time.sleep(RESTART_DELAY_SEC)
                new_proc = _start_child_safe(logger, name, specs[name])
                procs[name] = new_proc
                if new_proc is None:
                    _schedule_retry(name)
                else:
                    fail_counts[name] = 0
                    started_at[name] = time.time()

            if now - last_hb >= HEARTBEAT_INTERVAL_SEC:
                last_hb = now
                _safe_heartbeat(
                    "data_collectors_runner",
                    status="RUNNING",
                    detail={
                        "children": {
                            name: (
                                {"pid": p.pid, "returncode": p.poll()}
                                if p is not None
                                else {"pid": None, "returncode": None, "state": "start_failed",
                                      "consecutive_failures": fail_counts.get(name, 0)}
                            )
                            for name, p in procs.items()
                        }
                    },
                )
            time.sleep(1.0)
    finally:
        logger.warning("[DATA COLLECTORS] stopping children")
        for name, proc in list(procs.items()):
            if proc is None:
                continue
            try:
                _terminate_child(logger, name, proc)
            except Exception:
                logger.exception("[DATA COLLECTORS] child terminate failed name=%s", name)
        _safe_heartbeat("data_collectors_runner", status="STOPPED", detail={})
        logger.warning("[DATA COLLECTORS] STOPPED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
