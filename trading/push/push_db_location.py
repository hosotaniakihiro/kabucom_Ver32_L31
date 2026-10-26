# ============================================================
# File   : trading/push/push_db_location.py
# Version: PUSH-DB-LOCAL-STAGING-V1
# Function:
#   - push{YYYYMMDD}.db を「どこに置くか」を決める唯一の場所
#   - 場中はローカルに書き、区切りで NAS へ複製する
# ============================================================
"""PUSH DB はローカルに作り、NAS へは複製する。

2026-09-04 の実測で、NAS (SMB) 上の SQLite への追記が実需に追いつかない
ことがはっきりした。後場が始まった 12:30 ちょうどからキューが伸び続け、
15:23 に上限 200,000 で溢れて 2,004 件を捨てた。

    executemany push 500 行 =  0.709s
    executemany raw  500 行 = 26.445s   ★
    commit                  =  0.064s

遅いのは commit ではなく **stream_data_raw への executemany**。この表は
(symbol, datetime) と (received_at) の 2 本のインデックスを持ち、1 日で
500MB 超まで育つ。インデックスの更新はランダム I/O なので、SMB の
往復遅延がそのまま効く。書く量ではなく往復回数の問題なので、
raw_json を NULL にする (2026-09-03 の対策) だけでは足りなかった。

読み手も同じ DB を NAS 越しに読んでおり、`stream_data.fastlane` が
41 秒かかっていた。書き手だけをローカルに移すと読み手が古い NAS 側を
見てしまうので、**書き手と読み手が同じ解決関数を使う**ようにする。

NAS へは:
  - 日付が変わるとき (writer のローテーション)
  - writer を閉じるとき (大引け後の停止)
  - 任意で一定間隔ごと (PUSH_DB_NAS_SYNC_INTERVAL_MIN)
に sqlite3 のオンラインバックアップ API で複製する。稼働中の DB を
そのままファイルコピーすると壊れるが、backup() は一貫性を保つ。

環境変数:
  PUSH_DB_LOCAL_STAGING       0 で従来どおり NAS へ直接書く (既定 1)
  PUSH_DB_LOCAL_DIR           ローカルの置き場所 (既定 %LOCALAPPDATA%\\autostock\\push)
  PUSH_DB_NAS_SYNC_INTERVAL_MIN  場中の定期複製の間隔。0 で無効 (既定 30)
  PUSH_DB_PATH                従来どおり最優先。指定があればそれを使う
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
    "push_db_dir",
    "push_db_path",
    "nas_push_db_dir",
    "nas_push_db_path",
    "sync_to_nas",
    "nas_sync_interval_sec",
    "sync_timeout_sec",
]

_DEFAULT_SYNC_INTERVAL_MIN = 30.0

# 複製は同時に 1 本だけ。NAS が詰まっている間に要求を重ねると
# SMB のセッションを食い潰す (2026-09-02 の storm で実際に枯渇した)。
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


def _today_yyyymmdd(date: Optional[dt.date] = None) -> str:
    d = date or dt.datetime.now().date()
    return d.strftime("%Y%m%d")


def local_staging_enabled() -> bool:
    return _env_bool("PUSH_DB_LOCAL_STAGING", True)


def nas_sync_interval_sec() -> float:
    """場中の定期複製の間隔 (秒)。0 以下なら定期複製しない。"""
    return max(0.0, _env_float("PUSH_DB_NAS_SYNC_INTERVAL_MIN", _DEFAULT_SYNC_INTERVAL_MIN) * 60.0)


def nas_push_db_dir() -> Path:
    """NAS 側の置き場所。従来の push DB のディレクトリ。"""
    explicit = os.getenv("PUSH_DB_NAS_DIR")
    if explicit and str(explicit).strip():
        return Path(str(explicit).strip())
    try:
        from config.paths import get_path
        from nas_root import to_real_nas

        # 2026-10-24: config.paths はローカルのデータ root を指しうるので NAS 上へ読み替える
        return Path(to_real_nas(get_path("raw_push")))
    except Exception:
        logger.debug("[PUSH DB LOC] config.paths を読めないので nas_root へ退避", exc_info=True)
        from nas_root import real_nas_path

        return Path(real_nas_path(r"raw_data\kabu_station\push"))


def _local_push_db_dir() -> Path:
    explicit = os.getenv("PUSH_DB_LOCAL_DIR")
    if explicit and str(explicit).strip():
        return Path(str(explicit).strip())
    base = os.getenv("LOCALAPPDATA") or os.getenv("TEMP") or r"C:\Temp"
    return Path(base) / "autostock" / "push"


def push_db_dir() -> Path:
    """今このプロセスが読み書きするべき push DB のディレクトリ。"""
    if not local_staging_enabled():
        d = nas_push_db_dir()
        d.mkdir(parents=True, exist_ok=True)
        return d
    d = _local_push_db_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def push_db_path(date: Optional[dt.date] = None) -> Path:
    """今このプロセスが読み書きするべき push DB のフルパス。

    PUSH_DB_PATH が指定されていればそれを最優先する (従来どおり)。
    """
    explicit = os.getenv("PUSH_DB_PATH") or os.getenv("AUTOSTOCK_PUSH_DB_PATH")
    if explicit and str(explicit).strip():
        return Path(str(explicit).strip())
    return push_db_dir() / f"push{_today_yyyymmdd(date)}.db"


def nas_push_db_path(date: Optional[dt.date] = None) -> Path:
    return nas_push_db_dir() / f"push{_today_yyyymmdd(date)}.db"


def sync_timeout_sec() -> float:
    """複製 1 回に許す時間。超えたら諦めてローカルを優先する。"""
    return max(5.0, _env_float("PUSH_DB_NAS_SYNC_TIMEOUT_SEC", 180.0))


def _do_backup(source: Path, date: Optional[dt.date], pages: int, sleep_sec: float, out: dict) -> None:
    """実際の複製。NAS に触るのは全部この中 (呼び出し側スレッドを固めないため)。

    NAS のパス解決は mkdir を伴うので、これも NAS がハングすれば止まる。
    だからパス解決ごとこのワーカーの中に置く。
    """
    src_conn = None
    dst_conn = None
    tmp: Optional[Path] = None
    try:
        dst = nas_push_db_path(date)
        out["dst"] = str(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)

        # 2026-09-07: 完成品を作ってから置き換える。
        #
        # 本番で複製が 2 回走っているのを観測した (main_database と
        # push_receiver がどちらも定期複製スレッドを持っていた)。
        #   09:19:03 reason=periodic  0.0MB
        #   09:19:20 reason=periodic 78.5MB
        # 直接 dst へ backup していると、順序次第で中身の薄いほうが
        # 後に書かれて NAS の複製が壊れる。プロセスごとに一時ファイルを
        # 作り、出来上がってから os.replace で差し替える (同一共有内なら原子的)。
        tmp = dst.with_name(dst.name + ".tmp%d" % os.getpid())
        for suf in ("", "-wal", "-shm"):
            q = Path(str(tmp) + suf)
            if q.exists():
                try:
                    q.unlink()
                except Exception:
                    pass

        # 書き込み中の DB が相手なので read-only では開かない
        # (WAL の読み取りに -shm への書き込みが要る)。
        src_conn = sqlite3.connect(str(source), timeout=60.0)
        dst_conn = sqlite3.connect(str(tmp), timeout=60.0)
        src_conn.backup(dst_conn, pages=int(pages), sleep=float(sleep_sec))
        dst_conn.commit()
        dst_conn.close()
        dst_conn = None

        size = tmp.stat().st_size
        if size <= 0:
            raise OSError("複製が空になった (置き換えない)")

        os.replace(str(tmp), str(dst))
        tmp = None
        out["ok"] = True
        out["bytes"] = size
    except Exception as e:
        out["error"] = "%s: %s" % (type(e).__name__, e)
    finally:
        for c in (dst_conn, src_conn):
            try:
                if c is not None:
                    c.close()
            except Exception:
                pass
        # 失敗した一時ファイルを残さない
        if tmp is not None:
            for suf in ("", "-wal", "-shm"):
                q = Path(str(tmp) + suf)
                try:
                    if q.exists():
                        q.unlink()
                except Exception:
                    pass


def sync_to_nas(
    src: Optional[Path] = None,
    *,
    date: Optional[dt.date] = None,
    reason: str = "",
    pages: int = 2000,
    sleep_sec: float = 0.0,
    timeout_sec: Optional[float] = None,
    slot_wait_sec: float = 0.0,
) -> dict:
    """ローカルの push DB を NAS へ複製する。

    稼働中の DB を shutil.copy すると壊れるので sqlite3 のオンライン
    バックアップ API を使う。``pages`` ずつ進めるので、writer を
    長時間止めない。

    **NAS がハングしても呼び出し側を固めない。** SMB のハングは
    sqlite3 の timeout では抜けられず、OS の I/O でブロックする。
    そこで実処理は daemon スレッドへ出し、``timeout_sec`` で見切る。
    見切ったスレッドは放置される (daemon なのでプロセス終了を妨げない)。
    ローカルの DB はそのまま残るので、後から複製し直せる。

    複製が詰まっている間に次の複製を重ねると SMB のセッションを
    さらに食うので、実行中なら新しい要求はスキップする。

    戻り値は結果の dict。失敗しても例外は投げない (複製に失敗しても
    ローカルの取り込みは続けるべきなので)。
    """
    started = time.perf_counter()
    out = {
        "ok": False, "reason": reason, "src": None, "dst": None,
        "bytes": 0, "elapsed_sec": 0.0, "error": "", "timed_out": False,
        "skipped": False,
    }

    if not local_staging_enabled():
        out["ok"] = True
        out["error"] = "local staging 無効のため複製不要"
        return out

    source = Path(src) if src else push_db_path(date)
    out["src"] = str(source)
    if not source.exists():
        out["error"] = "source が無い"
        return out

    # 締め切り付きで実行する。NAS のハングで呼び出し側を固めない
    # (詳細は utils/nas_guard.py)。停止時など取りこぼしたくない経路は
    # slot_wait_sec だけ前の複製の完了を待つ。
    limit = float(timeout_sec) if timeout_sec is not None else sync_timeout_sec()

    guard = run_with_deadline(
        lambda: _do_backup(source, date, pages, sleep_sec, out),
        timeout_sec=limit,
        slot=_SYNC_SLOT,
        slot_wait_sec=slot_wait_sec,
        label="push_db_nas_sync:%s" % reason,
        thread_name="push_db_nas_sync_once",
    )

    out["elapsed_sec"] = time.perf_counter() - started

    if guard["skipped"]:
        out["skipped"] = True
        out["error"] = guard["error"]
        return out

    if guard["timed_out"]:
        out["timed_out"] = True
        out["error"] = guard["error"]
        logger.error(
            "[PUSH DB NAS SYNC] 時間切れ reason=%s %.0fs src=%s "
            "(ローカルの DB は無事。NAS が戻ってから複製し直すこと)",
            reason, limit, source,
        )
        return out

    if not out["ok"] and guard["error"]:
        out["error"] = guard["error"]

    if out["ok"]:
        logger.warning(
            "[PUSH DB NAS SYNC] 完了 reason=%s %.1fMB %.1fs src=%s dst=%s",
            reason, out["bytes"] / 1024 / 1024, out["elapsed_sec"], source, out["dst"],
        )
    else:
        logger.warning(
            "[PUSH DB NAS SYNC] 失敗 reason=%s src=%s dst=%s err=%s",
            reason, source, out["dst"], out["error"],
        )
    return out
