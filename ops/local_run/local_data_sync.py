# ============================================================
# File   : ops/local_run/local_data_sync.py
# 2026-10-24: 場中は NAS を使わない。データはこの PC のローカル root
#   (nas_root.default_local_data_root(), 既定 %LOCALAPPDATA%\autostock\nas_local)
#   に置き、NAS へは引け後・夜間バッチ後にこのスクリプトで写す。
#
#   setup    : ローカル root を作り、push / ranking / order_book / positions を
#              既存のローカル置き場へのジャンクションにする
#   from-nas : NAS -> ローカル (初回の写し)。完了したら --ready でマーカーを置く
#   to-nas   : ローカル -> NAS (引け後 / 夜間バッチ後)。ローカルが正
#   status   : 状態の表示
#
#   戻し方: ローカル root の _LOCAL_DATA_READY.txt を消す (次の起動から NAS を使う)
# ============================================================
"""ローカルのデータ root と NAS のあいだでデータを写す。

SQLite の扱い:
  - 書き手がいるかもしれない DB (to-nas で -wal が残っている) は sqlite3 の
    backup() でローカルの一時ファイルへ一貫した写しを作り、それを NAS へ置く。
  - DB を置き換えるときは、写し先の -wal / -shm を消す。古い -wal が残ると
    新しい DB に古い変更が当てられて壊れる。
  - NAS のハングで固まらないよう、ファイルごとに締め切りを付ける。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve()
SRC = HERE.parents[2]
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import nas_root as NR  # noqa: E402

SKIP_SUFFIXES = ("-wal", "-shm", "-journal")

# ローカル root のサブフォルダ -> 既存のローカル置き場 (ジャンクションにする)
def _junctions() -> dict[str, Path]:
    la = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or r"C:\Temp") / "autostock"
    return {
        r"raw_data\kabu_station\push": la / "push",
        r"raw_data\kabu_station\ranking": la / "ranking",
        r"raw_data\order_book": la / "order_book",
        r"trading\runtime\positions": la / "positions",
    }


# from-nas の既定の対象。場中に NAS を読んでいた場所 (10/23 のログから)。
# (相対パス, 何日分か。0 = 全部)
# 10/24 の実測: summary は全体 90GB、daily_db は stock_analysis.db (3.1GB) だけが使われ
# 6 月の退避 (17GB 等) は対象外になるよう日数で絞る。
DEFAULT_SEED = [
    ("Basic", 0),
    ("daily_db", 30),
    (r"raw_data\kabu_station\summary", 35),
    (r"raw_data\kabu_station\ranking_summary", 10),
    (r"raw_data\kabu_station\push_subscription", 10),
    (r"raw_data\kabu_station\runtime_state", 10),
    (r"raw_data\kabu_station\runtime", 0),
    (r"raw_data\kabu_station\audit", 10),
    (r"raw_data\trade_guard", 10),
    (r"raw_data\yahoo\status", 10),
    (r"raw_data\yahoo\intraday", 10),
    (r"raw_data\kabutan", 0),
    (r"raw_data\SBI", 10),
    (r"raw_data\AI", 0),
    ("ai", 0),
    (r"trading\ai", 0),
    (r"trading\runtime", 0),
    (r"runtime\summary_save_spool", 0),
]


def _log(msg: str) -> None:
    print(f"{dt.datetime.now():%H:%M:%S} {msg}", flush=True)


def _with_deadline(fn, timeout: float):
    box: dict = {}

    def run():
        try:
            box["v"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"{timeout:.0f}s を超えた (NAS のハングを疑う)")
    if "e" in box:
        raise box["e"]
    return box.get("v")


def _is_junction(p: Path) -> bool:
    try:
        return bool(os.path.isjunction(p)) if hasattr(os.path, "isjunction") else (p.is_dir() and os.path.realpath(p) != str(p.absolute()))
    except OSError:
        return False


def setup(local: Path) -> None:
    local.mkdir(parents=True, exist_ok=True)
    for rel, target in _junctions().items():
        link = local / rel
        target.mkdir(parents=True, exist_ok=True)
        if link.exists() or _is_junction(link):
            if _is_junction(link):
                _log(f"junction ok {link} -> {os.path.realpath(link)}")
                continue
            if link.is_dir() and not any(link.iterdir()):
                link.rmdir()
            else:
                raise SystemExit(f"{link} が普通のフォルダで中身がある。手で確認すること")
        link.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        _log(f"junction made {link} -> {target}")


def _iter_files(base: Path, days: int, skip_dirs: set[str]):
    cutoff = time.time() - days * 86400 if days > 0 else 0
    for dp, dns, fns in os.walk(base, followlinks=True):
        rel_dp = os.path.relpath(dp, base)
        dns[:] = [d for d in dns if os.path.normcase(os.path.normpath(os.path.join(rel_dp, d))) not in skip_dirs]
        for f in fns:
            if f.endswith(SKIP_SUFFIXES) or ".tmp" in f or f == NR.READY_MARKER:
                continue
            p = Path(dp) / f
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_mtime >= cutoff:
                yield p, st


def _remove_sidecars(dst: Path) -> None:
    for suf in ("-wal", "-shm", "-journal"):
        q = Path(str(dst) + suf)
        if q.exists():
            q.unlink()


def _copy_one(src: Path, dst: Path, *, consistent_db: bool) -> int:
    """src -> dst。dst は一時名で作ってから置き換える。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmpsync%d" % os.getpid())
    wal = Path(str(src) + "-wal")
    is_db = src.suffix.lower() == ".db"
    try:
        if is_db and consistent_db and wal.exists() and wal.stat().st_size > 0:
            # 書き手がいるかもしれないので backup() で一貫した写しをローカルに作る
            fd, ltmp = tempfile.mkstemp(suffix=".db", prefix="lds_")
            os.close(fd)
            try:
                s = sqlite3.connect(str(src), timeout=60.0)
                d = sqlite3.connect(ltmp)
                try:
                    s.backup(d, pages=4000)
                    d.commit()
                finally:
                    d.close()
                    s.close()
                shutil.copyfile(ltmp, tmp)
            finally:
                try:
                    os.unlink(ltmp)
                except OSError:
                    pass
        else:
            shutil.copy2(src, tmp)
            if is_db and wal.exists() and wal.stat().st_size > 0:
                # 書き手のいない DB (from-nas)。-wal も一緒に写す
                shutil.copy2(wal, Path(str(tmp) + "-wal"))
        if is_db:
            _remove_sidecars(dst)
        os.replace(tmp, dst)
        if is_db and Path(str(tmp) + "-wal").exists():
            os.replace(Path(str(tmp) + "-wal"), Path(str(dst) + "-wal"))
        return dst.stat().st_size
    finally:
        for q in (tmp, Path(str(tmp) + "-wal")):
            try:
                if q.exists():
                    q.unlink()
            except OSError:
                pass


def _needs_copy(src_st, dst: Path) -> bool:
    try:
        d = dst.stat()
    except OSError:
        return True
    # backup() で作った写しは mtime が「写した時刻」になるので、src の方が新しいときだけ写す
    return src_st.st_mtime > d.st_mtime + 2


def to_nas(local: Path, days: int, per_file_timeout: float, dry: bool) -> int:
    nas = Path(NR.real_nas_root())
    if not _with_deadline(lambda: nas.exists(), 30):
        _log(f"NAS {nas} が見えない。中止")
        return 2
    n = b = ng = 0
    for p, st in _iter_files(local, days, set()):
        rel = p.relative_to(local)
        dst = nas / rel
        try:
            if not _with_deadline(lambda: _needs_copy(st, dst), per_file_timeout):
                continue
            if dry:
                _log(f"would copy {rel} {st.st_size/1e6:.1f}MB")
                n += 1
                continue
            size = _with_deadline(lambda: _copy_one(p, dst, consistent_db=True), per_file_timeout)
            n += 1
            b += size or 0
            _log(f"copied {rel} {st.st_size/1e6:.1f}MB")
        except Exception as e:  # noqa: BLE001
            ng += 1
            _log(f"NG {rel}: {type(e).__name__}: {e}")
            if isinstance(e, TimeoutError):
                _log("NAS が詰まっているので残りは次回に回す")
                break
    _log(f"to-nas done files={n} {b/1e9:.2f}GB ng={ng}")
    return 1 if ng else 0


def from_nas(local: Path, seed: list[tuple[str, int]], per_file_timeout: float, dry: bool) -> int:
    nas = Path(NR.real_nas_root())
    skip = {os.path.normcase(os.path.normpath(k)) for k in _junctions()}
    n = b = ng = 0
    for rel_dir, days in seed:
        base = nas / rel_dir
        if not base.exists():
            _log(f"skip (NAS に無い) {rel_dir}")
            continue
        # NAS 側でジャンクション相当の場所 (push 等) は写さない
        rel_skip = {os.path.normcase(os.path.normpath(os.path.relpath(local / k, local / rel_dir))) for k in skip}
        for p, st in _iter_files(base, days, rel_skip):
            rel = p.relative_to(nas)
            dst = local / rel
            try:
                if not _needs_copy(st, dst):
                    continue
                if dry:
                    _log(f"would copy {rel} {st.st_size/1e6:.1f}MB")
                    n += 1
                    b += st.st_size
                    continue
                size = _with_deadline(lambda: _copy_one(p, dst, consistent_db=False), per_file_timeout)
                os.utime(dst, (st.st_atime, st.st_mtime))
                n += 1
                b += size or 0
            except Exception as e:  # noqa: BLE001
                ng += 1
                _log(f"NG {rel}: {type(e).__name__}: {e}")
        _log(f"seeded {rel_dir} (days={days}) total files={n} {b/1e9:.2f}GB")
    _log(f"from-nas done files={n} {b/1e9:.2f}GB ng={ng}")
    return 1 if ng else 0


def status(local: Path) -> None:
    _log(f"local root   = {local}")
    _log(f"ready marker = {(local / NR.READY_MARKER).exists()}")
    _log(f"nas_root()   = {NR.nas_root()}  real={NR.real_nas_root()}")
    for rel in _junctions():
        link = local / rel
        _log(f"  {rel}: junction={_is_junction(link)} -> {os.path.realpath(link) if link.exists() else '-'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["setup", "from-nas", "to-nas", "status"])
    ap.add_argument("--days", type=int, default=3, help="to-nas: 何日以内に更新したファイルを写すか (0=全部)")
    ap.add_argument("--only", action="append", default=[], help="from-nas: 相対パス[:日数] (複数可)。省略時は既定一式")
    ap.add_argument("--timeout", type=float, default=900.0, help="1 ファイルの締め切り秒")
    ap.add_argument("--ready", action="store_true", help="from-nas が全部成功したらマーカーを置く")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    local = Path(NR.default_local_data_root())

    if a.mode == "status":
        status(local)
        return 0
    if a.mode == "setup":
        setup(local)
        return 0
    if a.mode == "to-nas":
        if not (local / NR.READY_MARKER).exists():
            _log("ローカル root が未使用 (マーカーなし)。写すものは無い")
            return 0
        return to_nas(local, a.days, a.timeout, a.dry_run)

    seed = DEFAULT_SEED
    if a.only:
        seed = []
        for s in a.only:
            rel, _, d = s.partition(":")
            seed.append((rel, int(d or 0)))
    setup(local)
    rc = from_nas(local, seed, a.timeout, a.dry_run)
    if rc == 0 and a.ready and not a.dry_run:
        (local / NR.READY_MARKER).write_text(
            f"seeded {dt.datetime.now():%Y-%m-%d %H:%M:%S} from {NR.real_nas_root()}\n"
            "このファイルがある間、nas_root() はこのフォルダを返す。消すと次の起動から NAS に戻る。\n",
            encoding="utf-8",
        )
        _log(f"ready marker を置いた: {local / NR.READY_MARKER}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
