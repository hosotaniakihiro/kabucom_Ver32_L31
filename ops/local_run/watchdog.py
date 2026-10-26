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
#
# 2026-10-22 追加: NAS 障害の自動復旧 (ユーザー承認「自動で再起動してよい」)
#   10 月はほぼ毎営業日、起動から 1〜3 時間で NAS (W:/X:/Y: は同じ接続) が
#   "Too many open files" になり、NAS 上の当日 summary DB に書けず足が止まって発注 0 になっていた。
#   4) NAS の当日 summary DB を開けるか 30 秒ごとに試す。2 回続けて開けなければ
#      X:/W:/Y: をつなぎ直す (net use /delete → 再マップ。10/8・10/22 に手で直した手順)。
#   5) main の console ログの [SUMMARY STALE DROP] tf=1 after=0 で、本体が古い 1 分足
#      (latest_age_sec > WATCHDOG_STALE_AGE_SEC 既定 600) を見続けているかを見る。
#      NAS の当日 DB は更新されているのに本体だけ古い状態が WATCHDOG_STALE_RESTART_MIN 分
#      (既定 10) 続いたら再起動する (10/22: つなぎ直しても本体は再起動まで直らなかった)。
#      再起動は restart_local.ps1 を切り離して起動する (stop_all.ps1 はこの watchdog も止めるため)。
#      1 日 WATCHDOG_NAS_RESTART_MAX 回 (既定 2) まで。WATCHDOG_NAS_RESTART=0 で通知だけ。
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
SRC = os.path.dirname(os.path.dirname(HERE))
NAS_DRIVES = {
    "X:": r"\\192.168.0.22\AutoStockBuyAndSell",
    "W:": r"\\192.168.0.22\business",
    "Y:": r"\\192.168.0.22\kabu",
}


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


# ---------------- NAS (2026-10-22) ----------------
def nas_summary_db_path(now: dt.datetime) -> str:
    try:
        if SRC not in sys.path:
            sys.path.insert(0, SRC)
        from nas_root import nas_path

        base = nas_path(r"raw_data\kabu_station\summary")
    except Exception:
        base = r"X:\raw_data\kabu_station\summary"
    return os.path.join(str(base), f"summary{now:%Y%m%d}.db")


def check_nas_probe(now: dt.datetime) -> tuple[bool, str]:
    """NAS の当日 summary DB を実際に開いて先頭を読む(存在確認だけでは劣化を見逃す)。"""
    p = nas_summary_db_path(now)
    try:
        with open(p, "rb") as f:
            head = f.read(16)
    except FileNotFoundError:
        # 寄り前など、まだ作られていないだけのことがある。フォルダが読めれば NAS は生きている
        try:
            os.listdir(os.path.dirname(p))
            return True, f"summary db not yet created ({os.path.basename(p)})"
        except Exception as e:
            return False, f"nas dir unreadable {type(e).__name__}: {e}"
    except Exception as e:
        return False, f"nas open failed {type(e).__name__}: {e}"
    if not head.startswith(b"SQLite format 3"):
        return False, f"nas read returned unexpected header {head[:16]!r}"
    return True, "nas ok"


def nas_summary_age_min(now: dt.datetime) -> float | None:
    p = nas_summary_db_path(now)
    try:
        newest = max(os.path.getmtime(x) for x in (p, p + "-wal") if os.path.exists(x))
    except Exception:
        return None
    return (time.time() - newest) / 60.0


def reset_nas_drives() -> list[str]:
    """X:/W:/Y: をつなぎ直す。カレントが NAS 上だと net use が失敗するので C: ドライブ直下で実行する。"""
    out = []
    for d, default_remote in NAS_DRIVES.items():
        remote = default_remote
        try:
            r = subprocess.run(["net", "use", d], capture_output=True, text=True, timeout=30, cwd="C:\\")
            for line in (r.stdout or "").splitlines():
                if line.strip().lower().startswith("remote name"):
                    remote = line.split(None, 2)[-1].strip() or default_remote
        except Exception:
            pass
        try:
            subprocess.run(["net", "use", d, "/delete", "/y"], capture_output=True, text=True, timeout=30, cwd="C:\\")
            r = subprocess.run(["net", "use", d, remote, "/persistent:yes"], capture_output=True, text=True,
                               timeout=60, cwd="C:\\")
            out.append(f"{d}->{remote} rc={r.returncode}")
        except Exception as e:
            out.append(f"{d} failed {type(e).__name__}: {e}")
    return out


def _tail_text(path: str, nbytes: int = 400_000) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", errors="replace")
    except Exception:
        return ""


def newest_main_console_log(now: dt.datetime) -> str | None:
    files = glob.glob(os.path.join(CONSOLE_DIR, f"console_{now:%Y%m%d}_*.log"))
    return max(files, key=os.path.getmtime) if files else None


def parse_main_summary_stale(text: str, now: dt.datetime, *, within_sec: float = 120.0) -> float | None:
    """直近 within_sec 秒の [SUMMARY STALE DROP] tf=1 ... after=0 の latest_age_sec の最小値。
    該当行が無ければ None (= 古い足で全部捨てている状態ではない)。"""
    ages = []
    for line in text.splitlines():
        if "[SUMMARY STALE DROP]" not in line or " tf=1 " not in line or " after=0 " not in line:
            continue
        try:
            ts = dt.datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue
        if (now - ts).total_seconds() > within_sec:
            continue
        i = line.find("latest_age_sec=")
        if i < 0:
            continue
        try:
            ages.append(float(line[i + len("latest_age_sec="):].split()[0]))
        except Exception:
            continue
    return min(ages) if ages else None


def _stale_window(now: dt.datetime) -> bool:
    t = now.time()
    return (dt.time(9, 15) <= t < dt.time(11, 25)) or (dt.time(12, 45) <= t < dt.time(15, 15))


def check_main_summary_stale(now: dt.datetime) -> tuple[bool, str]:
    """本体だけが古い 1 分足を見ている(= NAS の当日 DB は進んでいる)なら NG。"""
    log = newest_main_console_log(now)
    if not log:
        return True, "no console log (skipped)"
    started_min = (time.time() - os.path.getctime(log)) / 60.0
    if started_min < _env_f("WATCHDOG_STALE_STARTUP_GRACE_MIN", 20.0):
        return True, f"main started {started_min:.0f} min ago (grace)"
    age = parse_main_summary_stale(_tail_text(log), now)
    if age is None or age <= _env_f("WATCHDOG_STALE_AGE_SEC", 600.0):
        return True, "main summary fresh" if age is None else f"main summary age {age:.0f}s"
    nas_age = nas_summary_age_min(now)
    if nas_age is None or nas_age > 5.0:
        # NAS の当日 DB も止まっている = データ自体が来ていない(PUSH 停止など)。再起動では直らない
        return True, f"main summary {age:.0f}s old but nas summary also stale ({nas_age}) -> not main-only"
    return False, f"main sees 1m summary {age:.0f}s old while nas summary updated {nas_age:.1f} min ago"


def _restart_count_path(now: dt.datetime) -> str:
    return os.path.join(RUNTIME_DIR, f"watchdog_nas_restart_{now:%Y%m%d}.txt")


def nas_restart_count(now: dt.datetime) -> int:
    try:
        with open(_restart_count_path(now), encoding="utf-8") as f:
            return len([x for x in f.read().splitlines() if x.strip()])
    except Exception:
        return 0


def spawn_detached_restart(now: dt.datetime, reason: str) -> None:
    """restart_local.ps1 を切り離して起動する。stop_all.ps1 はこの watchdog(python) も止めるので、
    自分で stop → start を順に呼ぶと start に届かない。"""
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    with open(_restart_count_path(now), "a", encoding="utf-8") as f:
        f.write(f"{now:%H:%M:%S} {reason}\n")
    ps1 = os.path.join(HERE, "restart_local.ps1")
    # 2026-10-23: DETACHED_PROCESS だと PowerShell 5.1 はスクリプトを 1 行も実行せずに rc=0 で終わる
    #   (10/23 13:26 の再起動が空振りし、main は古い足のまま)。CREATE_NO_WINDOW なら動く。
    flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", ps1, "-Reason", reason],
                     cwd="C:\\", creationflags=flags, close_fds=True)
    _log(f"[NAS] restart spawned: {ps1} reason={reason}")


class NasGuard:
    """NAS のつなぎ直しと、本体だけ古いときの再起動を判断する(ループから 30 秒ごとに呼ぶ)。"""

    def __init__(self) -> None:
        self.probe_ng = 0
        self.last_reset: dt.datetime | None = None
        self.resets_today = 0
        self.stale_since: dt.datetime | None = None

    def step(self, now: dt.datetime) -> None:
        ok, msg = check_nas_probe(now)
        if ok:
            if self.probe_ng:
                _log(f"[NAS] probe recovered: {msg}")
            self.probe_ng = 0
        else:
            self.probe_ng += 1
            _log(f"[NAS] probe NG streak={self.probe_ng}: {msg}")
            cool = self.last_reset is None or (now - self.last_reset) > dt.timedelta(minutes=3)
            if self.probe_ng >= 2 and cool and self.resets_today < int(_env_f("WATCHDOG_NAS_RESET_MAX", 10)):
                self.last_reset = now
                self.resets_today += 1
                res = reset_nas_drives()
                ok2, msg2 = check_nas_probe(dt.datetime.now())
                _log(f"[NAS] drives reset #{self.resets_today}: {res} -> probe {'OK' if ok2 else 'NG'} {msg2}")
                if not ok2:
                    _alert("autostock watchdog", f"{now:%H:%M} NAS をつなぎ直しても開けません\n\n{msg2}")

        if not _stale_window(now):
            self.stale_since = None
            return
        ok, msg = check_main_summary_stale(now)
        if ok:
            if self.stale_since is not None:
                _log(f"[NAS] main summary recovered: {msg}")
            self.stale_since = None
            return
        if self.stale_since is None:
            self.stale_since = now
            _log(f"[NAS] main-only stale start: {msg}")
            return
        mins = (now - self.stale_since).total_seconds() / 60.0
        if mins < _env_f("WATCHDOG_STALE_RESTART_MIN", 10.0):
            return
        allow = str(os.getenv("WATCHDOG_NAS_RESTART", "1")).strip().lower() not in ("0", "false", "no", "off")
        done = nas_restart_count(now)
        limit = int(_env_f("WATCHDOG_NAS_RESTART_MAX", 2))
        if allow and done < limit:
            _alert("autostock watchdog", f"{now:%H:%M} 本体が古い足のまま {mins:.0f} 分。自動で再起動します\n\n{msg}")
            spawn_detached_restart(now, f"main_summary_stale_{mins:.0f}min")
            self.stale_since = None
            time.sleep(300)
        else:
            _log(f"[NAS] main-only stale {mins:.0f} min, restart not allowed (allow={allow} done={done}/{limit})")
            _alert("autostock watchdog", f"{now:%H:%M} 本体が古い足のまま {mins:.0f} 分(自動再起動なし)\n\n{msg}")
            self.stale_since = now


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
    nas = NasGuard()
    nas_interval = max(10.0, _env_f("WATCHDOG_NAS_INTERVAL_SEC", 30.0))
    last_main_check = 0.0
    _log(f"watchdog start interval={args.interval:.0f}s restart={args.restart} nas_interval={nas_interval:.0f}s")
    while True:
        now = dt.datetime.now()
        if now.time() >= dt.time(15, 35):
            _log("after close; watchdog exit")
            return 0
        if now.weekday() < 5 and dt.time(8, 0) <= now.time():
            try:
                nas.step(now)
            except Exception as e:
                _log(f"[NAS] guard error {type(e).__name__}: {e}")
        if time.monotonic() - last_main_check < max(15.0, args.interval):
            time.sleep(nas_interval)
            continue
        last_main_check = time.monotonic()
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
        time.sleep(nas_interval)


if __name__ == "__main__":
    sys.exit(main())
