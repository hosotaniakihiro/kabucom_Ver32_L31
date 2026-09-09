# ============================================================
# File   : trading/push/order_book_db_location.py
# Version: ORDER-BOOK-DB-LOCAL-STAGING-V1
# Function:
#   - orderbook{YYYYMMDD}.db を「どこに置くか」を決める唯一の場所
#   - 場中はローカルに書き、区切りで NAS へ複製する
# ============================================================
"""板情報 DB はローカルに作り、NAS へは複製する。

2026-09-09 の障害:
    push の flush ワーカーは 1 行ごとに OrderBookDBWriter.add_row を呼び、
    その中で 1,000 行たまるごとに **同期で** flush する。この DB が NAS 上に
    あったため、executemany 1 回に十数分かかる状態になり、flush ワーカーが
    15:13 から止まった。結果 111,862 件が push DB へ書かれないまま失われ、
    プロセスは taskkill でも落ちなくなった (SMB の I/O でカーネル内に固着し、
    net use X: /delete で初めて解放された)。

    実測 (2026-09-09 16:02、同一マシン):
        接続+DDL       ローカル   6.8 ms / NAS  996.6 ms
        2000 行 INSERT ローカル   0.9 ms / NAS   96.5 ms

    書き込み先をローカルにすれば、同期 flush のままでも 100 分の 1 の時間で
    終わるので、flush ワーカーを止めない。

読み手について:
    この DB を稼働中に読む箇所は無い (grep で確認)。読むのは AI の学習
    スクリプト (AI/train/entry/... など) で、いずれも場外バッチ。
    そのため NAS へは停止時と日付ローテーション時に複製すれば足りる。

環境変数:
  ORDER_BOOK_DB_LOCAL_STAGING   0 で従来どおり NAS へ直接書く (既定 1)
  ORDER_BOOK_DB_LOCAL_DIR       ローカルの置き場所
                                (既定 %LOCALAPPDATA%\\autostock\\order_book)
  ORDER_BOOK_DB_NAS_DIR         NAS 側の置き場所 (既定 get_path("raw_order_book"))
  ORDER_BOOK_DB_SYNC_TIMEOUT_SEC 複製 1 回に許す時間 (既定 180)
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from utils.nas_guard import run_with_deadline

logger = logging.getLogger(__name__)

__all__ = [
    "local_staging_enabled",
    "order_book_db_dir",
    "order_book_db_path",
    "nas_order_book_db_dir",
    "nas_order_book_db_path",
    "sync_to_nas",
]

# 複製は同時に 1 本だけ。詰まっている間に重ねると SMB のセッションを食う。
_SYNC_SLOT = threading.Semaphore(1)


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return default
        return float(str(v).strip())
    except Exception:
        return default


def _yyyymmdd(date: Optional[dt.date] = None) -> str:
    return (date or dt.datetime.now().date()).strftime("%Y%m%d")


def local_staging_enabled() -> bool:
    return _env_bool("ORDER_BOOK_DB_LOCAL_STAGING", True)


def sync_timeout_sec() -> float:
    return max(5.0, _env_float("ORDER_BOOK_DB_SYNC_TIMEOUT_SEC", 180.0))


def nas_order_book_db_dir() -> Path:
    explicit = os.getenv("ORDER_BOOK_DB_NAS_DIR")
    if explicit and str(explicit).strip():
        return Path(str(explicit).strip())
    from config.paths import get_path

    return Path(get_path("raw_order_book"))


def _local_order_book_db_dir() -> Path:
    explicit = os.getenv("ORDER_BOOK_DB_LOCAL_DIR")
    if explicit and str(explicit).strip():
        return Path(str(explicit).strip())
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    return Path(base) / "autostock" / "order_book"


def order_book_db_dir() -> Path:
    """今このプロセスが読み書きするべき板情報 DB のディレクトリ。"""
    d = nas_order_book_db_dir() if not local_staging_enabled() else _local_order_book_db_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def order_book_db_path(date: Optional[dt.date] = None) -> Path:
    return order_book_db_dir() / f"orderbook{_yyyymmdd(date)}.db"


def nas_order_book_db_path(date: Optional[dt.date] = None) -> Path:
    return nas_order_book_db_dir() / f"orderbook{_yyyymmdd(date)}.db"


def _do_backup(src: Path, date: Optional[dt.date], out: dict) -> None:
    """実際の複製。NAS に触るのは全部この中 (呼び出し側スレッドを固めないため)。"""
    src_conn = dst_conn = None
    tmp: Optional[Path] = None
    try:
        dst = nas_order_book_db_path(date)
        out["dst"] = str(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        if tmp.exists():
            tmp.unlink()
        src_conn = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30.0)
        dst_conn = sqlite3.connect(str(tmp), timeout=60.0)
        src_conn.backup(dst_conn, pages=2000)
        dst_conn.close()
        dst_conn = None
        src_conn.close()
        src_conn = None
        os.replace(tmp, dst)
        tmp = None
        out["ok"] = True
    except Exception as e:
        out["error"] = repr(e)
    finally:
        for c in (dst_conn, src_conn):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
        try:
            if tmp is not None and tmp.exists():
                tmp.unlink()
        except Exception:
            pass


def sync_to_nas(
    src: Optional[Path] = None,
    *,
    date: Optional[dt.date] = None,
    reason: str = "",
    timeout_sec: Optional[float] = None,
) -> dict:
    """ローカルの板情報 DB を NAS へ複製する。

    稼働中の DB を copy すると壊れるので sqlite3 のオンラインバックアップを使う。
    NAS がハングしても呼び出し側を固めないよう、実処理は daemon スレッドへ出し
    ``timeout_sec`` で見切る。失敗しても例外は投げない。
    """
    out: dict = {"ok": False, "reason": reason, "src": None, "dst": None, "elapsed": 0.0}
    started = time.perf_counter()

    if not local_staging_enabled():
        out["skipped"] = "local_staging_disabled"
        return out

    source = Path(src) if src else order_book_db_path(date)
    out["src"] = str(source)
    if not source.exists():
        out["skipped"] = "src_not_found"
        return out

    if not _SYNC_SLOT.acquire(blocking=False):
        out["skipped"] = "already_running"
        logger.warning("[ORDER BOOK DB NAS SYNC] 実行中なのでスキップ reason=%s", reason)
        return out

    try:
        res = run_with_deadline(
            lambda: _do_backup(source, date, out),
            timeout_sec=float(timeout_sec if timeout_sec is not None else sync_timeout_sec()),
            label="order_book_db_sync",
            thread_name="order_book_db_nas_sync",
        )
        if not res.get("ok"):
            out.setdefault("error", "deadline_exceeded")
    except Exception as e:
        out["error"] = repr(e)
    finally:
        _SYNC_SLOT.release()
        out["elapsed"] = time.perf_counter() - started

    if out.get("ok"):
        logger.warning(
            "[ORDER BOOK DB NAS SYNC] 複製した reason=%s %.1fs src=%s dst=%s",
            reason, out["elapsed"], out["src"], out.get("dst"),
        )
    else:
        logger.warning(
            "[ORDER BOOK DB NAS SYNC] 複製できなかった reason=%s %.1fs detail=%s "
            "(ローカルは残るので後から複製し直せる)",
            reason, out["elapsed"], out.get("error") or out.get("skipped"),
        )
    return out
