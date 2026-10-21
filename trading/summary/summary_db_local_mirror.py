# ============================================================
# trading/summary/summary_db_local_mirror.py
# ------------------------------------------------------------
# 2026-09-17: 起動時の summary 復元・MA75 増分読みが NAS (X:) の前日/前々日の
#   summary DB (600〜740MB) をランダムに読んで 10〜30 分かかっていた。
#   同じファイルを順に丸ごとコピーすると 740MB が 15 秒 (実測 50MB/s) なので、
#   **当日以外の summary DB はローカルへミラーしてから読む**。
#   当日の DB は書き込み中 (WAL) なので触らず NAS のまま。
#
#   置き場: %LOCALAPPDATA%\autostock\summary_cache\summaryYYYYMMDD.db
#   同名ファイルがあってサイズが一致すれば再コピーしない (前日の DB は引け後に固定される)。
#   env: SUMMARY_DB_LOCAL_MIRROR=0 で無効、SUMMARY_DB_LOCAL_MIRROR_DIR で置き場変更。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Iterable, List

logger = logging.getLogger(__name__)

VERSION = "V1.1-SUMMARY-DB-LOCAL-MIRROR-TODAY-SNAPSHOT"
_lock = threading.Lock()
_RE_YMD = re.compile(r"summary(\d{8})\.db$", re.IGNORECASE)


def enabled() -> bool:
    v = os.getenv("SUMMARY_DB_LOCAL_MIRROR", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def mirror_dir() -> Path:
    env = os.getenv("SUMMARY_DB_LOCAL_MIRROR_DIR", "").strip()
    if env:
        return Path(env)
    base = os.getenv("LOCALAPPDATA") or os.getenv("TEMP") or r"C:\Temp"
    return Path(base) / "autostock" / "summary_cache"


def _ymd_of(path: str) -> str:
    m = _RE_YMD.search(os.path.basename(str(path)))
    return m.group(1) if m else ""


def _is_today(path: str, today: dt.date | None = None) -> bool:
    ymd = _ymd_of(path)
    return bool(ymd) and ymd == (today or dt.date.today()).strftime("%Y%m%d")


def _today_snapshot_enabled() -> bool:
    # 2026-09-17 21:5x: 書き込み中の db+wal を素朴にコピーすると "database disk image is malformed" になった
    #   (289MB/157MB の WAL が動いている最中)。当日は既定 OFF。使うなら sqlite backup API に置き換えること。
    v = os.getenv("SUMMARY_DB_LOCAL_MIRROR_TODAY", "0").strip().lower()
    return v not in ("0", "false", "no", "off")


def snapshot_today(src: str) -> str:
    """当日の summary DB (WAL で書き込み中) を db + wal ごとローカルへ写して読む。
    2026-09-17 16:02〜16:06: NAS の当日 DB が "database is locked" で 3.5 分待たされて 0 本扱いになった。
    起動時の読み取り専用 seed なので、数秒古いスナップショットで十分。60 秒以内の写しは使い回す。"""
    try:
        sp = Path(src)
        if not sp.exists():
            return src
        d = mirror_dir() / "today"
        d.mkdir(parents=True, exist_ok=True)
        dst = d / sp.name
        with _lock:
            try:
                if dst.exists() and (time.time() - dst.stat().st_mtime) < 60.0 and dst.stat().st_size > 0:
                    return str(dst)
            except Exception:
                pass
            t0 = time.time()
            for suf in ("", "-wal"):
                s_ = Path(str(sp) + suf)
                if s_.exists():
                    shutil.copyfile(str(s_), str(dst) + suf)
                elif suf and Path(str(dst) + suf).exists():
                    Path(str(dst) + suf).unlink()
            shm = Path(str(dst) + "-shm")
            if shm.exists():
                shm.unlink()
            os.utime(str(dst), None)
            logger.warning("[SUMMARY DB MIRROR] today snapshot %s -> %s bytes=%s elapsed=%.1fs", sp, dst, dst.stat().st_size, time.time() - t0)
            return str(dst)
    except Exception:
        logger.warning("[SUMMARY DB MIRROR] today snapshot failed src=%s (NAS のまま読む)", src, exc_info=True)
        return src


def mirror_one(src: str, *, today: dt.date | None = None) -> str:
    """当日以外の summary DB をローカルへコピーし、ローカルのパスを返す。失敗時は元のパス。"""
    if not enabled():
        return src
    try:
        sp = Path(src)
        if not sp.exists():
            return src
        if _is_today(src, today):
            return snapshot_today(src) if _today_snapshot_enabled() else src
        # 既にローカル配下ならそのまま
        try:
            if sp.resolve().drive.upper() == "C:":
                return src
        except Exception:
            pass
        dst = mirror_dir() / sp.name
        with _lock:
            try:
                st_s, st_d = sp.stat(), (dst.stat() if dst.exists() else None)
                if st_d is not None and st_d.st_size == st_s.st_size and st_d.st_size > 0 and abs(st_d.st_mtime - st_s.st_mtime) < 2.0:
                    return str(dst)
            except Exception:
                pass
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + ".tmp%d" % os.getpid())
            t0 = time.time()
            shutil.copyfile(str(sp), str(tmp))
            try:
                os.utime(str(tmp), (st_s.st_atime, st_s.st_mtime))
            except Exception:
                pass
            os.replace(str(tmp), str(dst))
            logger.warning(
                "[SUMMARY DB MIRROR] copied %s -> %s bytes=%s elapsed=%.1fs",
                sp, dst, dst.stat().st_size, time.time() - t0,
            )
            return str(dst)
    except Exception:
        logger.warning("[SUMMARY DB MIRROR] copy failed src=%s (NAS のまま読む)", src, exc_info=True)
        try:
            tmp = mirror_dir() / (Path(src).name + ".tmp%d" % os.getpid())
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return src


def mirror_paths(paths: Iterable[str], *, today: dt.date | None = None) -> List[str]:
    cleanup_old()
    return [mirror_one(p, today=today) for p in paths]


def cleanup_old(keep_days: int = 5) -> None:
    """古いミラーを消す (1 日 600〜740MB)。"""
    try:
        d = mirror_dir()
        if not d.exists():
            return
        cutoff = (dt.date.today() - dt.timedelta(days=keep_days)).strftime("%Y%m%d")
        for p in d.glob("summary*.db*"):
            ymd = _ymd_of(str(p)) or _ymd_of(str(p).split(".db")[0] + ".db")
            if ymd and ymd < cutoff:
                try:
                    p.unlink()
                    logger.info("[SUMMARY DB MIRROR] removed old %s", p)
                except Exception:
                    pass
    except Exception:
        logger.debug("[SUMMARY DB MIRROR] cleanup failed", exc_info=True)
