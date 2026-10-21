# -*- coding: utf-8 -*-
# ============================================================
# File   : scripts/archive_dbs_to_onedrive.py
# Version: ARCHIVE-DBS-TO-ONEDRIVE-V1
# ------------------------------------------------------------
# 大引け後に、その日の push / ranking DB を OneDrive へ保管する。
# ============================================================
r"""1 日 1 回、当日の DB を圧縮して OneDrive へ置く。

現在の構成 (2026-09-05 / 09-07 に移行):

    稼働中   ローカル %LOCALAPPDATA%\autostock\{push,ranking}
    30 分ごと NAS へ複製
    大引け後  OneDrive へ保管   <- これ

**なぜ OneDrive に「稼働中の DB」を置かないか**

OneDrive はファイル単位で同期し、Files On-Demand で実体を剥がす。
剥がれたファイルの読み取りはネットワーク取得でブロックするので、
2026-09-07 に直した `err=interrupted` と同じ失敗の仕方になる。
さらに `-wal` / `-shm` が本体と別々に同期されるため、SQLite が
公式に警告している破損しうる構成になる。

**だから「完成した 1 日ぶんの静的なファイル」だけを置く。**

ローカルの C: は不良ブロック実績のある SSD で、NAS は不安定。
両方に加えて OneDrive にも残しておけば、どれか 1 つが死んでも復旧できる。

zip は OneDrive の外で作ってから移動する。OneDrive のフォルダで直接
作ると、書きかけのファイルが同期対象になる。

使い方:
    python scripts/archive_dbs_to_onedrive.py
    python scripts/archive_dbs_to_onedrive.py --date 20260907
    python scripts/archive_dbs_to_onedrive.py --dry-run
    python scripts/archive_dbs_to_onedrive.py --keep-days 60
"""
from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

logger = logging.getLogger("archive_to_onedrive")

DEFAULT_KEEP_DAYS = 30
ARCHIVE_DIRNAME = "_BACKUP_AUTOSTOCK"
_NAME_RE = re.compile(r"^(push|ranking)(\d{8})\.db\.zip$")


def default_dest() -> Path:
    od = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if not od:
        od = str(Path.home() / "OneDrive")
    return Path(od) / ARCHIVE_DIRNAME


def _setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(sh)
    try:
        from data_collectors.config import LOG_DIR

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(LOG_DIR / "archive_to_onedrive.log", encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except Exception:
        pass


def sources(date_str: str) -> List[Tuple[str, Path]]:
    """(種別, ローカルのDBパス) の一覧。ローカルを正とする。"""
    out: List[Tuple[str, Path]] = []
    try:
        from trading.push import push_db_location

        out.append(("push", push_db_location.push_db_dir() / f"push{date_str}.db"))
    except Exception:
        logger.warning("push_db_location を読めない", exc_info=True)
    try:
        from trading.ranking import ranking_db_location

        out.append(("ranking", ranking_db_location.ranking_db_dir() / f"ranking{date_str}.db"))
    except Exception:
        logger.warning("ranking_db_location を読めない", exc_info=True)
    return out


def consistent_copy(src: Path, dst: Path) -> None:
    """稼働中でも壊れない複製。

    `shutil.copy` だと WAL の途中を掴んで壊れるので backup API を使う。
    """
    s = sqlite3.connect("file:%s?mode=ro" % src, uri=True, timeout=120.0)
    d = sqlite3.connect(str(dst), timeout=120.0)
    try:
        s.backup(d, pages=4000)
        d.commit()
    finally:
        d.close()
        s.close()


def archive_one(kind: str, src: Path, dest_dir: Path, *, dry_run: bool) -> Optional[dict]:
    if not src.exists():
        logger.warning("%s: 元が無いので飛ばす %s", kind, src)
        return None

    out_name = f"{src.stem}.db.zip" if not src.name.endswith(".db") else f"{src.name}.zip"
    final = dest_dir / out_name
    src_mb = src.stat().st_size / 1024 / 1024

    if dry_run:
        logger.info("[DRY RUN] %s %.0fMB -> %s", kind, src_mb, final)
        return {"kind": kind, "dry_run": True}

    t0 = time.perf_counter()
    tmp_dir = Path(tempfile.mkdtemp(prefix="autostock_archive_"))
    try:
        # 1) 一貫した複製をローカルに作る
        snap = tmp_dir / src.name
        consistent_copy(src, snap)
        snap_mb = snap.stat().st_size / 1024 / 1024

        # 2) OneDrive の外で zip にする
        #    (OneDrive フォルダで直接作ると書きかけが同期対象になる)
        tmp_zip = tmp_dir / out_name
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            z.write(snap, arcname=src.name)
        zip_mb = tmp_zip.stat().st_size / 1024 / 1024

        # 3) 完成品だけを OneDrive へ置く
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_zip), str(final))

        el = time.perf_counter() - t0
        logger.warning(
            "%s 保管 %.0fMB -> zip %.0fMB (%.0f%%) %.1fs -> %s",
            kind, snap_mb, zip_mb, (zip_mb / snap_mb * 100.0) if snap_mb else 0.0, el, final,
        )
        return {"kind": kind, "src_mb": snap_mb, "zip_mb": zip_mb, "elapsed": el, "path": str(final)}
    except Exception as e:  # noqa: BLE001
        logger.error("%s の保管に失敗: %s: %s", kind, type(e).__name__, e)
        return None
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def prune(dest_dir: Path, keep_days: int, *, dry_run: bool) -> int:
    """古い保管ぶんを消す。ファイル名の日付で判断する (mtime ではない)。"""
    if keep_days <= 0 or not dest_dir.exists():
        return 0
    cutoff = (dt.datetime.now().date() - dt.timedelta(days=keep_days)).strftime("%Y%m%d")
    removed = 0
    for p in sorted(dest_dir.glob("*.db.zip")):
        m = _NAME_RE.match(p.name)
        if not m:
            continue
        if m.group(2) >= cutoff:
            continue
        if dry_run:
            logger.info("[DRY RUN] 削除 %s", p.name)
            removed += 1
            continue
        try:
            p.unlink()
            logger.info("削除 %s", p.name)
            removed += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("削除できない %s: %s", p.name, e)
    return removed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=dt.datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--dest", default="")
    ap.add_argument("--keep-days", type=int, default=DEFAULT_KEEP_DAYS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    _setup_logging()

    dest_dir = Path(args.dest) if args.dest else default_dest()
    logger.warning("=== DB を OneDrive へ保管 date=%s dest=%s keep=%d日 ===",
                   args.date, dest_dir, args.keep_days)

    results = []
    for kind, src in sources(args.date):
        r = archive_one(kind, src, dest_dir, dry_run=args.dry_run)
        if r:
            results.append(r)

    n = prune(dest_dir, args.keep_days, dry_run=args.dry_run)
    if n:
        logger.warning("古い保管ぶんを %d 件削除した (%d 日より前)", n, args.keep_days)

    if not results:
        logger.error("1 件も保管できなかった")
        return 1
    logger.warning("完了 %d 件", len(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
