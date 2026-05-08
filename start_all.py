# ============================================================
# File   : start_all.py
# Version: START-ALL-LAUNCHER-V2-SUMMARY-SAVE-OWNER
# ------------------------------------------------------------
# Purpose:
#   - main_database.py と main.py を一括起動するランチャー
#   - main_database.py を先に起動して token / DB / ranking / PUSH 受信側を開始
#   - 少し待ってから main.py を起動して summary / AI / entry / 表示側を開始
#   - どちらかが終了した場合もログで検知できるように親プロセスで監視
#
# V2:
#   - 定時サマリー計算は main.py / main_database.py 両方で実施
#   - DB保存 owner は main_database.py 側へ寄せる
#   - main.py は計算・表示・AI/entryを継続し、DB保存だけskip
#
# Usage:
#   python start_all.py
#   python start_all.py --delay 20
#   python start_all.py --no-new-console
#
# Windows recommended:
#   start_all.bat をダブルクリック、または PowerShell/CMD から実行
# ============================================================

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DELAY_SEC = 15.0


# ============================================================
# utility
# ============================================================

def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log(message: str) -> None:
    print(f"[{_now()}] [START_ALL] {message}", flush=True)


def _resolve_python_exe() -> str:
    """
    起動に使う Python を決定する。

    優先順位:
      1. 環境変数 KABU_PYTHON_EXE
      2. 現在 start_all.py を実行している sys.executable
    """
    env_python = os.environ.get("KABU_PYTHON_EXE", "").strip().strip('"')
    if env_python:
        return env_python

    return sys.executable


def _validate_file(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"required file not found: {path}")
    if not path.is_file():
        raise FileNotFoundError(f"required path is not a file: {path}")


def _build_env(overrides: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["KABU_PROJECT_ROOT"] = str(PROJECT_ROOT)

    old_pythonpath = env.get("PYTHONPATH", "")
    if old_pythonpath:
        env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + old_pythonpath
    else:
        env["PYTHONPATH"] = str(PROJECT_ROOT)

    if overrides:
        env.update({str(k): str(v) for k, v in overrides.items()})

    return env


def _creation_flags(new_console: bool) -> int:
    if os.name != "nt":
        return 0

    flags = 0
    if new_console:
        flags |= getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

    return flags


# ============================================================
# process launcher
# ============================================================

def _start_process(
    *,
    name: str,
    python_exe: str,
    script_path: Path,
    new_console: bool,
    env_overrides: Optional[Dict[str, str]] = None,
) -> subprocess.Popen:
    _validate_file(script_path)

    cmd = [python_exe, str(script_path)]
    child_env = _build_env(env_overrides)

    _log(f"launch {name}: {' '.join(cmd)}")
    _log(
        f"env {name}: AUTOSTOCK_SUMMARY_SAVE_OWNER="
        f"{child_env.get('AUTOSTOCK_SUMMARY_SAVE_OWNER', '')} "
        f"AUTOSTOCK_DATA_COLLECTORS_PROCESS="
        f"{child_env.get('AUTOSTOCK_DATA_COLLECTORS_PROCESS', '')} "
        f"AUTOSTOCK_SUMMARY_DB_WRITER="
        f"{child_env.get('AUTOSTOCK_SUMMARY_DB_WRITER', '')}"
    )

    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=child_env,
        creationflags=_creation_flags(new_console),
    )

    _log(f"started {name}: pid={proc.pid}")
    return proc


def _terminate_process(name: str, proc: subprocess.Popen, timeout_sec: float = 10.0) -> None:
    if proc.poll() is not None:
        return

    _log(f"terminate {name}: pid={proc.pid}")

    try:
        proc.terminate()
    except Exception as e:
        _log(f"terminate failed {name}: {e!r}")

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if proc.poll() is not None:
            _log(f"terminated {name}: returncode={proc.returncode}")
            return
        time.sleep(0.2)

    if proc.poll() is None:
        _log(f"kill {name}: pid={proc.pid}")
        try:
            proc.kill()
        except Exception as e:
            _log(f"kill failed {name}: {e!r}")


def _monitor(processes: List[Tuple[str, subprocess.Popen]]) -> int:
    """
    子プロセスを監視する。

    - Ctrl+C を押したら両方終了
    - 片方が異常終了しても、もう片方は即座には落とさない
      （原因ログを確認しやすくするため）
    - 両方終了したら親も終了
    """
    _log("monitor start. Press Ctrl+C to stop both processes.")

    reported: set[str] = set()

    while True:
        alive = []

        for name, proc in processes:
            rc = proc.poll()
            if rc is None:
                alive.append((name, proc))
                continue

            if name not in reported:
                reported.add(name)
                _log(f"process exited: {name} pid={proc.pid} returncode={rc}")

        if not alive:
            _log("all child processes exited")
            rc_list = [proc.returncode for _, proc in processes]
            return 0 if all(rc == 0 for rc in rc_list) else 1

        time.sleep(2.0)


# ============================================================
# main
# ============================================================

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Start main_database.py and main.py together."
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SEC,
        help="Seconds to wait after starting main_database.py before starting main.py.",
    )
    parser.add_argument(
        "--no-new-console",
        action="store_true",
        help="Do not open separate consoles for child processes.",
    )

    args = parser.parse_args(argv)

    python_exe = _resolve_python_exe()
    main_database_path = PROJECT_ROOT / "main_database.py"
    main_path = PROJECT_ROOT / "main.py"
    new_console = not bool(args.no_new_console)

    _log("========== START ALL BOOT ==========")
    _log(f"PROJECT_ROOT={PROJECT_ROOT}")
    _log(f"python_exe={python_exe}")
    _log(f"delay={args.delay}")
    _log(f"new_console={new_console}")

    _validate_file(main_database_path)
    _validate_file(main_path)

    processes: List[Tuple[str, subprocess.Popen]] = []

    main_database_env = {
        "AUTOSTOCK_DATA_COLLECTORS_PROCESS": "1",
        "AUTOSTOCK_MAIN_DATABASE_PROCESS": "1",
        "AUTOSTOCK_SUMMARY_SAVE_OWNER": "database",
    }

    main_env = {
        # main.py でもサマリー計算は行うが、DB保存は database owner に任せる。
        "AUTOSTOCK_SUMMARY_SAVE_OWNER": "database",
        "AUTOSTOCK_DATA_COLLECTORS_PROCESS": "0",
        "AUTOSTOCK_SUMMARY_DB_WRITER": "0",
        "AUTOSTOCK_MAIN_DATABASE_PROCESS": "0",
    }

    try:
        # 1) DB / ranking / PUSH 受信 / サマリーDB保存側を先に起動
        main_database_proc = _start_process(
            name="main_database.py",
            python_exe=python_exe,
            script_path=main_database_path,
            new_console=new_console,
            env_overrides=main_database_env,
        )
        processes.append(("main_database.py", main_database_proc))

        # token refresh / DB準備 / collector 起動の初動時間を確保
        if args.delay > 0:
            _log(f"wait {args.delay:.1f} sec before launching main.py")
            time.sleep(args.delay)

        # main_database.py が即死していたら、main.py は起動しない
        rc = main_database_proc.poll()
        if rc is not None:
            _log(
                "main_database.py exited before main.py launch. "
                f"returncode={rc}. main.py launch skipped."
            )
            return int(rc) if isinstance(rc, int) else 1

        # 2) summary / AI / entry / 表示側を起動。DB保存はskipされる。
        main_proc = _start_process(
            name="main.py",
            python_exe=python_exe,
            script_path=main_path,
            new_console=new_console,
            env_overrides=main_env,
        )
        processes.append(("main.py", main_proc))

        return _monitor(processes)

    except KeyboardInterrupt:
        _log("KeyboardInterrupt received. stopping child processes...")
        return 130

    except Exception as e:
        _log(f"fatal error: {e!r}")
        return 1

    finally:
        for name, proc in reversed(processes):
            _terminate_process(name, proc)

        _log("========== START ALL END ==========")


if __name__ == "__main__":
    raise SystemExit(main())
