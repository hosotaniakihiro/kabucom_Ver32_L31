# ============================================================
# File   : trading/ranking/ranking_db_location.py
# Version: RANKING-DB-LOCAL-STAGING-V1
# Function:
#   - ranking{YYYYMMDD}.db を「どこに置くか」を決める唯一の場所
# ============================================================
r"""ranking DB はローカルに作り、NAS へは複製する。

2026-09-07 に、エントリーが 1 件も出ない原因がここだと分かった。

    [RANKING ENTRY SOURCE DB FALLBACK] indexed read aborted
        table=ranking_snapshot_1min elapsed=2.104s budget=2.0s err=interrupted
    [RANKING ENTRY SOURCE DB FALLBACK] rowid read failed elapsed=2.168s err=interrupted
    [RANKING ENTRY SOURCE DB FALLBACK] deadline exceeded deadline=8.0s elapsed=8.0s
        -> use last-good rows=0
    [RANKING ENTRY LOOP] skip reason=no_ranking_df
    -> [RANKING ENTRY SCHEDULE] pending build done created=0   (80 回中 77 回)
    -> 発注 0

ranking DB は NAS 上で 427MB + WAL 129MB まで育ち、`ranking_snapshot_1min`
の読み取りが 2 秒の予算にも 8 秒の締切にも間に合わない。読めないので
候補が作れず、AI_OK が 3,924 件出ていても発注に至らない。

障害時の堅牢性の話ではなく、**平常時に毎日エントリーを止めている**。

push DB (第24ラウンド) と同じ扱いにする。置き場所の判断をここに集約し、
**writer と読み手が同じ関数を通る**ようにする。ここがずれると、
読み手だけが古い NAS 側を黙って読むことになり、「読めない」より悪い。

NAS への複製は utils.nas_guard 経由で締め切りを付ける
(NAS のハングが取り込みを道連れにしないため)。

環境変数:
  RANKING_DB_LOCAL_STAGING           0 で従来どおり NAS へ直接書く (既定 1)
  RANKING_DB_LOCAL_DIR               既定 %LOCALAPPDATA%\autostock\ranking
  RANKING_DB_NAS_SYNC_INTERVAL_MIN   既定 30。0 で定期複製を止める
  RANKING_DB_NAS_SYNC_TIMEOUT_SEC    既定 180
  RANKING_DB_NAS_DIR                 複製先 (既定は従来の NAS パス)
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import List, Optional

from utils.nas_guard import new_slot, run_with_deadline

logger = logging.getLogger(__name__)

__all__ = [
    "local_staging_enabled",
    "ranking_db_dir",
    "ranking_db_path",
    "ranking_db_candidates",
    "nas_ranking_db_dir",
    "nas_ranking_db_path",
    "sync_to_nas",
    "nas_sync_interval_sec",
    "sync_timeout_sec",
]

_SYNC_SLOT = new_slot()
_DEFAULT_SYNC_INTERVAL_MIN = 30.0


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
    return _env_bool("RANKING_DB_LOCAL_STAGING", True)


def nas_sync_interval_sec() -> float:
    return max(0.0, _env_float("RANKING_DB_NAS_SYNC_INTERVAL_MIN", _DEFAULT_SYNC_INTERVAL_MIN) * 60.0)


def sync_timeout_sec() -> float:
    return max(5.0, _env_float("RANKING_DB_NAS_SYNC_TIMEOUT_SEC", 180.0))


def nas_ranking_db_dir() -> Path:
    explicit = os.getenv("RANKING_DB_NAS_DIR")
    if explicit and str(explicit).strip():
        return Path(str(explicit).strip())
    try:
        from nas_root import real_nas_path

        # 2026-10-24: nas_path はローカルのデータ root を指しうるので、複製先は real_nas_path
        return Path(real_nas_path(r"raw_data\kabu_station\ranking"))
    except Exception:
        logger.debug("[RANKING DB LOC] nas_root を読めない", exc_info=True)
        return Path(r"X:\raw_data\kabu_station\ranking")


def _local_ranking_db_dir() -> Path:
    explicit = os.getenv("RANKING_DB_LOCAL_DIR")
    if explicit and str(explicit).strip():
        return Path(str(explicit).strip())
    base = os.getenv("LOCALAPPDATA") or os.getenv("TEMP") or r"C:\Temp"
    return Path(base) / "autostock" / "ranking"


def ranking_db_dir() -> Path:
    """今このプロセスが読み書きするべき ranking DB のディレクトリ。"""
    if not local_staging_enabled():
        d = nas_ranking_db_dir()
    else:
        d = _local_ranking_db_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.debug("[RANKING DB LOC] mkdir 失敗 dir=%s", d, exc_info=True)
    return d


def ranking_db_path(date: Optional[dt.date] = None) -> Path:
    """今このプロセスが読み書きするべき ranking DB のフルパス。

    RANKING_DB_PATH が指定されていればそれを最優先する (従来どおり)。
    """
    for key in ("RANKING_DB_PATH", "ATS_RANKING_DB_PATH", "KABU_RANKING_DB_PATH"):
        v = os.getenv(key)
        if v and str(v).strip():
            return Path(str(v).strip())
    return ranking_db_dir() / f"ranking{_yyyymmdd(date)}.db"


def nas_ranking_db_path(date: Optional[dt.date] = None) -> Path:
    return nas_ranking_db_dir() / f"ranking{_yyyymmdd(date)}.db"


def ranking_db_candidates(date: Optional[dt.date] = None) -> List[str]:
    """探索順つきの候補。ローカルを先に見る。

    移行の取りこぼしがあっても、NAS 側に落ちて動き続けるようにする。
    ただし NAS 側は複製なので最大で複製間隔ぶん古い。
    """
    out: List[str] = []
    seen = set()
    for p in (ranking_db_path(date), nas_ranking_db_path(date)):
        s = str(p)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _do_backup(source: Path, date: Optional[dt.date], pages: int, out: dict) -> None:
    """NAS に触るのは全部この中 (呼び出し側スレッドを固めないため)。"""
    src_conn = None
    dst_conn = None
    tmp: Optional[Path] = None
    try:
        dst = nas_ranking_db_path(date)
        out["dst"] = str(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)

        # 完成品を作ってから置き換える (push DB と同じ理由。直接 dst へ
        # backup すると、失敗や同時実行で NAS 側の複製が壊れる)。
        tmp = dst.with_name(dst.name + ".tmp%d" % os.getpid())
        for suf in ("", "-wal", "-shm"):
            q = Path(str(tmp) + suf)
            if q.exists():
                try:
                    q.unlink()
                except Exception:
                    pass

        src_conn = sqlite3.connect(str(source), timeout=60.0)
        dst_conn = sqlite3.connect(str(tmp), timeout=60.0)
        src_conn.backup(dst_conn, pages=int(pages))
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
    timeout_sec: Optional[float] = None,
    slot_wait_sec: float = 0.0,
) -> dict:
    """ローカルの ranking DB を NAS へ複製する。

    NAS がハングしても呼び出し側を固めない (utils/nas_guard.py)。
    失敗しても例外は投げない。
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

    source = Path(src) if src else ranking_db_path(date)
    out["src"] = str(source)
    if not source.exists():
        out["error"] = "source が無い"
        return out

    limit = float(timeout_sec) if timeout_sec is not None else sync_timeout_sec()
    guard = run_with_deadline(
        lambda: _do_backup(source, date, pages, out),
        timeout_sec=limit,
        slot=_SYNC_SLOT,
        slot_wait_sec=slot_wait_sec,
        label="ranking_db_nas_sync:%s" % reason,
        thread_name="ranking_db_nas_sync_once",
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
            "[RANKING DB NAS SYNC] 時間切れ reason=%s %.0fs src=%s "
            "(ローカルの DB は無事。NAS が戻ってから複製し直すこと)",
            reason, limit, source,
        )
        return out
    if not out["ok"] and guard["error"]:
        out["error"] = guard["error"]

    if out["ok"]:
        logger.warning(
            "[RANKING DB NAS SYNC] 完了 reason=%s %.1fMB %.1fs src=%s dst=%s",
            reason, out["bytes"] / 1024 / 1024, out["elapsed_sec"], source, out["dst"],
        )
    else:
        logger.warning(
            "[RANKING DB NAS SYNC] 失敗 reason=%s src=%s err=%s", reason, source, out["error"]
        )
    return out
