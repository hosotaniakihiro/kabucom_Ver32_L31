# ============================================================
# trading/handlers/entry_precheck_ranking.py
# Ver: FINAL-RANKING-PRECHECK-ROBUST-SNAPSHOT-COLS
# ------------------------------------------------------------
# ✔ 既存機能100%保持（削除ゼロ）
# ✔ DB WAL / timeout対応
# ✔ snapshot鮮度チェック
# ✔ global_data復元
# ✔ ロック耐性
# ✔ 異常データ防止
# ✔ production hardened
#
# Fix:
#   - ranking_snapshot_1min が snapshot_time 列ではなく datetime / created_at
#     などで保存されている場合、precheck が has_snapshot=False になって
#     RANKING_PRECHECK_NG になる問題を修正。
#   - ranking DB path 解決を ats.ats_ranking.db_path と合わせる。
#   - 場中のランキングAPI更新遅延/再接続を考慮し、鮮度許容を90秒→既定300秒に緩和。
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.paths import get_path
from global_state import global_data

logger = logging.getLogger(__name__)

try:
    from ats.ats_ranking.db_path import get_usable_ranking_db_path
except Exception:  # pragma: no cover - runtime fallback
    get_usable_ranking_db_path = None

RANKING_DB_DIR = get_path("raw_ranking")
SNAPSHOT_TABLE = "ranking_snapshot_1min"

# ------------------------------------------------------------
# 許容遅延（秒）
# ------------------------------------------------------------
def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


MAX_SNAPSHOT_AGE_SEC = _env_int("RANKING_PRECHECK_MAX_SNAPSHOT_AGE_SEC", 300)
MIN_ROWS_READY = _env_int("RANKING_PRECHECK_MIN_ROWS", 1)


# ============================================================
# debug
# ============================================================
print(
    "### ENTRY_PRECHECK_RANKING LOADED FROM:",
    __file__,
    "PID:",
    __import__("os").getpid(),
    flush=True,
)


# ============================================================
# utils
# ============================================================
def _today_ranking_db_path() -> Optional[str]:
    try:
        if callable(get_usable_ranking_db_path):
            p = get_usable_ranking_db_path(force_refresh=False, allow_fallback=False, prefer_today_even_if_empty=True)
            if p:
                return str(p)
    except Exception:
        logger.debug("[RANKING PRECHECK] ats ranking db path resolver failed", exc_info=True)

    try:
        ymd = datetime.now().strftime("%Y%m%d")
        return str(Path(RANKING_DB_DIR) / f"ranking{ymd}.db")
    except Exception:
        return None


def _connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=5, check_same_thread=False)
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=3000;")
    except Exception:
        logger.debug("[RANKING PRECHECK] pragma setup failed path=%s", path, exc_info=True)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        return cur.fetchone() is not None
    except Exception:
        return False


def _table_columns(cur: sqlite3.Cursor, table: str) -> list[str]:
    try:
        cur.execute(f"PRAGMA table_info({table})")
        return [str(r[1]) for r in cur.fetchall()]
    except Exception:
        return []


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _count_rows(cur: sqlite3.Cursor, table: str) -> int:
    try:
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        row = cur.fetchone()
        return int(row[0] if row else 0)
    except Exception:
        return 0


def _pick_time_col(cols: list[str]) -> Optional[str]:
    priority = [
        "snapshot_time",
        "datetime",
        "timestamp",
        "created_at",
        "updated_at",
        "time",
        "date_time",
        "dt",
    ]
    colset = {str(c).lower(): c for c in cols}
    for p in priority:
        if p in colset:
            return colset[p]
    for c in cols:
        lc = str(c).lower()
        if "time" in lc or lc in {"date", "dt"}:
            return c
    return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        s = str(value).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1]
        s = s.replace("T", " ")
        # ranking側では YYYY-MM-DD HH:MM:SS が多い。
        for fmt in (
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y%m%d %H:%M:%S",
            "%Y%m%d%H%M%S",
            "%H:%M:%S",
        ):
            try:
                if fmt == "%H:%M:%S":
                    t = datetime.strptime(s, fmt).time()
                    return datetime.combine(datetime.now().date(), t)
                return datetime.strptime(s, fmt)
            except Exception:
                pass
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _load_latest_snapshot_time(cur: sqlite3.Cursor, table: str, time_col: Optional[str]) -> tuple[Optional[Any], Optional[datetime]]:
    if not time_col:
        return None, None
    try:
        qcol = _quote_ident(time_col)
        cur.execute(f"SELECT MAX({qcol}) FROM {table} WHERE {qcol} IS NOT NULL AND TRIM(CAST({qcol} AS TEXT)) <> ''")
        row = cur.fetchone()
        raw = row[0] if row and row[0] is not None else None
        return raw, _parse_dt(raw)
    except Exception:
        logger.debug("[RANKING PRECHECK] latest time load failed table=%s col=%s", table, time_col, exc_info=True)
        return None, None


def _load_snapshot_rows(cur: sqlite3.Cursor, table: str, time_col: Optional[str], latest_raw: Optional[Any], *, limit: int = 2000) -> List[Dict[str, Any]]:
    try:
        if time_col and latest_raw is not None:
            qcol = _quote_ident(time_col)
            cur.execute(f"SELECT * FROM {table} WHERE {qcol} = ? LIMIT ?", (latest_raw, int(limit)))
        else:
            # 時刻列が無いDBでも、rows>0ならランキングDBは利用可能とみなす。
            cur.execute(f"SELECT * FROM {table} LIMIT ?", (int(limit),))
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.debug("[RANKING PRECHECK] snapshot rows load failed table=%s col=%s", table, time_col, exc_info=True)
        return []


def _is_snapshot_fresh(latest_dt: Optional[datetime]) -> bool:
    if latest_dt is None:
        # 時刻列がないDBでは、row countがあればREADY扱いにする。
        return True
    try:
        age = (datetime.now() - latest_dt).total_seconds()
        # 未来時刻はPC時刻差・秒丸めとして許容。
        return age <= float(MAX_SNAPSHOT_AGE_SEC)
    except Exception:
        return False


def _not_ready(reason: str = "unknown", **extra: Any) -> Dict[str, Any]:
    d = {
        "is_ready": False,
        "explicit_ready": False,
        "derived_ready": False,
        "has_snapshot": False,
        "snapshot_count": 0,
        "ranking_type": None,
        "source": None,
        "reason": reason,
    }
    d.update(extra)
    return d


# ============================================================
# core
# ============================================================
def _check_ranking_ready() -> Dict[str, Any]:
    # ========================================================
    # ① global_data
    # ========================================================
    snapshot = getattr(global_data, "latest_ranking_snapshot", None)
    ranking_type = getattr(global_data, "latest_ranking_type", None)

    if isinstance(snapshot, list) and len(snapshot) > 0:
        return {
            "is_ready": True,
            "explicit_ready": True,
            "derived_ready": True,
            "has_snapshot": True,
            "snapshot_count": len(snapshot),
            "ranking_type": ranking_type,
            "source": "cache",
            "reason": "cache_snapshot",
        }

    # ========================================================
    # ② DB fallback
    # ========================================================
    ranking_db_path = _today_ranking_db_path()

    if not ranking_db_path:
        return _not_ready("ranking_db_path_unresolved")

    if not Path(ranking_db_path).exists():
        return _not_ready("ranking_db_not_found", db=ranking_db_path)

    try:
        conn = _connect_db(ranking_db_path)
        cur = conn.cursor()

        if not _table_exists(cur, SNAPSHOT_TABLE):
            conn.close()
            return _not_ready("snapshot_table_missing", db=ranking_db_path, table=SNAPSHOT_TABLE)

        total_rows = _count_rows(cur, SNAPSHOT_TABLE)
        if total_rows < max(1, MIN_ROWS_READY):
            conn.close()
            return _not_ready("snapshot_table_empty", db=ranking_db_path, table=SNAPSHOT_TABLE, total_rows=total_rows)

        cols = _table_columns(cur, SNAPSHOT_TABLE)
        time_col = _pick_time_col(cols)
        latest_raw, latest_dt = _load_latest_snapshot_time(cur, SNAPSHOT_TABLE, time_col)

        # ----------------------------------------------------
        # 鮮度チェック
        # ----------------------------------------------------
        if not _is_snapshot_fresh(latest_dt):
            age_sec = None
            try:
                age_sec = (datetime.now() - latest_dt).total_seconds() if latest_dt else None
            except Exception:
                pass
            logger.warning(
                "[RANKING STALE] db=%s table=%s time_col=%s latest=%s parsed=%s age_sec=%s max_age_sec=%s total_rows=%s",
                ranking_db_path,
                SNAPSHOT_TABLE,
                time_col,
                latest_raw,
                latest_dt,
                age_sec,
                MAX_SNAPSHOT_AGE_SEC,
                total_rows,
            )
            conn.close()
            return _not_ready(
                "snapshot_stale",
                db=ranking_db_path,
                table=SNAPSHOT_TABLE,
                time_col=time_col,
                latest=latest_raw,
                parsed=str(latest_dt) if latest_dt else None,
                age_sec=age_sec,
                max_age_sec=MAX_SNAPSHOT_AGE_SEC,
                total_rows=total_rows,
            )

        snapshot_rows = _load_snapshot_rows(cur, SNAPSHOT_TABLE, time_col, latest_raw)
        conn.close()

        if not snapshot_rows:
            return _not_ready(
                "snapshot_rows_empty_for_latest",
                db=ranking_db_path,
                table=SNAPSHOT_TABLE,
                time_col=time_col,
                latest=latest_raw,
                total_rows=total_rows,
            )

        # ----------------------------------------------------
        # type抽出
        # ----------------------------------------------------
        ranking_types = {r.get("rank_type") or r.get("ranking_type") or r.get("type") for r in snapshot_rows if r.get("rank_type") or r.get("ranking_type") or r.get("type")}

        resolved_type = ranking_types.pop() if len(ranking_types) == 1 else None

        # ----------------------------------------------------
        # global_data復元（重要）
        # ----------------------------------------------------
        try:
            global_data.latest_ranking_snapshot = snapshot_rows
            global_data.latest_ranking_type = resolved_type
            global_data.latest_ranking_snapshot_time = latest_raw
            global_data.latest_ranking_db_path = ranking_db_path
        except Exception:
            logger.warning("[RANKING CACHE RESTORE FAILED]", exc_info=True)

        logger.info(
            "[RANKING READY][DB] snapshot=%d total_rows=%d type=%s time_col=%s latest=%s parsed=%s db=%s",
            len(snapshot_rows),
            total_rows,
            resolved_type,
            time_col,
            latest_raw,
            latest_dt,
            ranking_db_path,
        )

        return {
            "is_ready": True,
            "explicit_ready": True,
            "derived_ready": True,
            "has_snapshot": True,
            "snapshot_count": len(snapshot_rows),
            "ranking_type": resolved_type,
            "source": "db",
            "reason": "db_snapshot",
            "db": ranking_db_path,
            "table": SNAPSHOT_TABLE,
            "time_col": time_col,
            "latest": latest_raw,
            "parsed": str(latest_dt) if latest_dt else None,
            "total_rows": total_rows,
        }

    except Exception as e:
        logger.exception("[RANKING PRECHECK ERROR] %s", e)
        return _not_ready("exception", db=ranking_db_path, error=str(e))


# ============================================================
# public API
# ============================================================
def precheck_ranking_entry() -> Dict[str, Any]:
    result = _check_ranking_ready()

    if not result["is_ready"]:
        logger.warning("[RANKING PRECHECK NG] %s", result)

    return result


# ============================================================
# log helper
# ============================================================
def log_precheck_result(result: Dict[str, Any]):
    if result.get("is_ready"):
        logger.info(
            "[RANKING PRECHECK OK] type=%s snapshot=%s source=%s db=%s time_col=%s latest=%s",
            result.get("ranking_type"),
            result.get("snapshot_count"),
            result.get("source"),
            result.get("db"),
            result.get("time_col"),
            result.get("latest"),
        )
    else:
        logger.warning("[RANKING PRECHECK NG] %s", result)
