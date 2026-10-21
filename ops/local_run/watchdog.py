# ============================================================
# ops/local_run/watchdog.py
# ------------------------------------------------------------
# 2026-09-17 (A5): 場中に「データが止まっている / main.py が居ない」を人に知らせる。
#
# 9/16 朝は main.py の起動が X: のランキング DB 読みで 10 分以上止まり、
# 昼は positions.db が開けず、どちらも人が気づくまで放置された。
# 売買システム本体とは別プロセスで、軽い確認だけを 60 秒ごとに行う。
#
# 確認項目 (平日 09:00〜15:30 のみ):
#   1) ローカル push DB (%LOCALAPPDATA%\autostock\push\pushYYYYMMDD.db) の
#      最終ティック時刻が WATCHDOG_TICK_STALE_MIN 分 (既定 3) より古くないか
#   2) main.py プロセスが居るか (コマンドラインに main.py を含む python)
#   3) main の console ログ (%LOCALAPPDATA%\autostock\logs\console\console_*.log)
#      の更新が WATCHDOG_LOG_STALE_MIN 分 (既定 5) より古くないか
#
# NG が WATCHDOG_TRIGGER_COUNT 回 (既定 2) 続いたら
#   - %LOCALAPPDATA%\autostock\runtime\watchdog.log に記録
#   - ビープ + 最前面のメッセージボックス (別スレッド、閉じるまで残る)
#   - --restart を付けた時だけ stop_all.ps1 -> start_local.bat を実行する
#     (kabu のトークンは 1 セッションなので既定は通知のみ)
#
# 使い方: C:\autostock\venv\Scripts\python.exe ops\local_run\watchdog.py [--once] [--restart]
# ============================================================
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import glob
import os
import sqlite3
import subprocess
import sys
import threading
import time

LOCAL = os.path.join(os.getenv("LOCALAPPDATA") or r"C:\Temp", "autostock")
PUSH_DIR = os.path.join(LOCAL, "push")
CONSOLE_DIR = os.path.join(LOCAL, "logs", "console")
RUNTIME_DIR = os.path.join(LOCAL, "runtime")
LOG_PATH = os.path.join(RUNTIME_DIR, "watchdog.log")
HERE = os.path.dirname(os.path.abspath(__file__))


def _env_f(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else default
    except Exception:
        return default


def _log(msg: str) -> None:
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _market_hours(now: dt.datetime) -> bool:
    if now.weekday() >= 5:
        return False
    t = now.time()
    return dt.time(9, 0) <= t < dt.time(15, 30)


def _grace_after_open(now: dt.datetime) -> bool:
    # 寄り直後は起動中の可能性があるので WATCHDOG_OPEN_GRACE_MIN 分は黙る
    grace = _env_f("WATCHDOG_OPEN_GRACE_MIN", 10.0)
    return now.time() < (dt.datetime.combine(now.date(), dt.time(9, 0)) + dt.timedelta(minutes=grace)).time()


# ---------------- checks ----------------
def check_push_tick(now: dt.datetime) -> tuple[bool, str]:
    p = os.path.join(PUSH_DIR, f"push{now:%Y%m%d}.db")
    if not os.path.exists(p):
        return False, f"push db missing {p}"
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=2.0)
        try:
            con.execute("PRAGMA busy_timeout=2000")
            row = con.execute("SELECT MAX(datetime) FROM stream_data_raw").fetchone()
        finally:
            con.close()
    except Exception as e:
        return False, f"push db read error {type(e).__name__}: {e}"
    last = row[0] if row else None
    if not last:
        return False, "push db has no ticks"
    try:
        ts = dt.datetime.fromisoformat(str(last)[:19].replace(" ", "T"))
    except Exception:
        return False, f"push db last datetime unparsable {last!r}"
    age_min = (now - ts).total_seconds() / 60.0
    # 昼休みは止まって当然
    if dt.time(11, 30) <= now.time() < dt.time(12, 35):
        return True, f"lunch (last tick {ts:%H:%M:%S})"
    limit = _env_f("WATCHDOG_TICK_STALE_MIN", 3.0)
    if age_min > limit:
        return False, f"last tick {ts:%H:%M:%S} is {age_min:.1f} min old (> {limit:.0f})"
    return True, f"last tick {ts:%H:%M:%S} ({age_min:.1f} min)"


def check_main_process() -> tuple[bool, str]:
    try:
        import psutil
    except Exception:
        return True, "psutil unavailable (skipped)"
    found = []
    for pr in psutil.process_iter(["name", "cmdline"]):
        try:
            name = (pr.info.get("name") or "").lower()
            if not name.startswith("python"):
                continue
            cmd = " ".join(pr.info.get("cmdline") or [])
            if "main.py" in cmd and "main_database" not in cmd and "TaskSchedule" not in cmd:
                found.append(pr.pid)
        except Exception:
            continue
    if not found:
        return False, "main.py process not found"
    return True, f"main.py pid={found}"


def check_console_log(now: dt.datetime) -> tuple[bool, str]:
    files = glob.glob(os.path.join(CONSOLE_DIR, f"console_{now:%Y%m%d}_*.log"))
    if not files:
        return False, "no console log for today"
    newest = max(files, key=os.path.getmtime)
    age_min = (time.time() - os.path.getmtime(newest)) / 60.0
    limit = _env_f("WATCHDOG_LOG_STALE_MIN", 5.0)
    if age_min > limit:
        return False, f"{os.path.basename(newest)} not updated for {age_min:.1f} min (> {limit:.0f})"
    return True, f"{os.path.basename(newest)} ({age_min:.1f} min)"


# ---------------- notify / restart ----------------
def _alert(title: str, body: str) -> None:
    try:
        for _ in range(3):
            ctypes.windll.kernel32.Beep(1200, 250)
            time.sleep(0.15)
    except Exception:
        pass

    def _box():
        try:
            # MB_ICONWARNING(0x30) | MB_TOPMOST(0x40000) | MB_SETFOREGROUND(0x10000)
            ctypes.windll.user32.MessageBoxW(0, body, title, 0x30 | 0x40000 | 0x10000)
        except Exception:
            pass

    threading.Thread(target=_box, daemon=True).start()


def _restart() -> None:
    stop = os.path.join(HERE, "stop_all.ps1")
    start = os.path.join(HERE, "start_local.bat")
    _log(f"restart: {stop} -> {start}")
    try:
        subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File", stop], timeout=120)
    except Exception as e:
        _log(f"stop failed {e}")
    time.sleep(5)
    try:
        subprocess.Popen(["cmd", "/c", "start", "", start], close_fds=True)
    except Exception as e:
        _log(f"start failed {e}")


def run_once(now: dt.datetime | None = None) -> tuple[bool, list[str]]:
    now = now or dt.datetime.now()
    results = [
        ("push_tick", *check_push_tick(now)),
        ("main_proc", *check_main_process()),
        ("console_log", *check_console_log(now)),
    ]
    ok = all(r[1] for r in results)
    lines = [f"{'OK' if r[1] else 'NG'} {r[0]}: {r[2]}" for r in results]
    return ok, lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="1 回だけ確認して終了 (時間帯を問わない)")
    ap.add_argument("--restart", action="store_true", help="NG が続いたら stop_all.ps1 -> start_local.bat を実行")
    ap.add_argument("--interval", type=float, default=_env_f("WATCHDOG_INTERVAL_SEC", 60.0))
    args = ap.parse_args()

    if args.once:
        ok, lines = run_once()
        for l in lines:
            print(l)
        return 0 if ok else 1

    trigger = int(_env_f("WATCHDOG_TRIGGER_COUNT", 2))
    ng_streak = 0
    alerted_at: dt.datetime | None = None
    _log(f"watchdog start interval={args.interval:.0f}s restart={args.restart}")
    while True:
        now = dt.datetime.now()
        if now.time() >= dt.time(15, 35):
            _log("after close; watchdog exit")
            return 0
        if _market_hours(now) and not _grace_after_open(now):
            ok, lines = run_once(now)
            if ok:
                if ng_streak:
                    _log("recovered: " + " | ".join(lines))
                ng_streak = 0
            else:
                ng_streak += 1
                _log(f"NG streak={ng_streak}: " + " | ".join(lines))
                if ng_streak >= trigger and (alerted_at is None or (now - alerted_at) > dt.timedelta(minutes=10)):
                    alerted_at = now
                    _alert("autostock watchdog", f"{now:%H:%M} 売買システムの異常\n\n" + "\n".join(lines))
                    if args.restart:
                        _restart()
                        ng_streak = 0
                        time.sleep(180)
        time.sleep(max(15.0, args.interval))


if __name__ == "__main__":
    sys.exit(main())
