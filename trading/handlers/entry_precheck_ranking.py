# ============================================================
# trading/handlers/entry_precheck_ranking.py
# Ver: FINAL-RANKING-PRECHECK-RAW-FALLBACK
# ------------------------------------------------------------
# ✔ DB WAL / timeout対応
# ✔ snapshot鮮度チェック
# ✔ global_data復元
# ✔ ロック耐性
# ✔ 異常データ防止
# ✔ production hardened
#
# Fix:
#   - ranking_snapshot_1min が古いままでも、ranking_raw_1min 等には
#     当日最新データが保存されているケースがある。
#   - 2026-06-05ログでは ranking collector は api_success=6 で成功しているが、
#     precheck は ranking_snapshot_1min の 2026-06-04 16:22:14 を見て
#     RANKING_PRECHECK_NG snapshot_stale になり、pendingを全削除していた。
#   - snapshot stale 時は fresh な raw/summary 系ランキングテーブルへfallbackし、
#     latest rows を global_data.latest_ranking_snapshot に復元する。
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime
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
RAW_FALLBACK_TABLES = (
    "ranking_raw_1min",
    "ranking_summary_1min",
    "ranking_raw",
    "ranking_snapshot",
)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


MAX_SNAPSHOT_AGE_SEC = _env_int("RANKING_PRECHECK_MAX_SNAPSHOT_AGE_SEC", 300)
MIN_ROWS_READY = _env_int("RANKING_PRECHECK_MIN_ROWS", 1)
FAST_SCAN_ROWS = _env_int("RANKING_PRECHECK_FAST_SCAN_ROWS", 3000)
RAW_FALLBACK_MAX_ROWS = _env_int("RANKING_PRECHECK_RAW_FALLBACK_MAX_ROWS", 2000)


print(
    "### ENTRY_PRECHECK_RANKING LOADED FROM:",
    __file__,
    "PID:",
    __import__("os").getpid(),
    flush=True,
)


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
        conn.execute("PRAGMA query_only=ON;")
    except Exception:
        logger.debug("[RANKING PRECHECK] pragma setup failed path=%s", path, exc_info=True)
    conn.row_factory = sqlite3.Row
    return conn


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _table_exists(cur: sqlite3.Cursor, table: str) -> bool:
    try:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,))
        return cur.fetchone() is not None
    except Exception:
        return False


def _table_columns(cur: sqlite3.Cursor, table: str) -> list[str]:
    try:
        cur.execute(f"PRAGMA table_info({_quote_ident(table)})")
        return [str(r[1]) for r in cur.fetchall()]
    except Exception:
        return []


def _count_rows(cur: sqlite3.Cursor, table: str) -> int:
    try:
        cur.execute(f"SELECT COUNT(*) FROM {_quote_ident(table)}")
        row = cur.fetchone()
        return int(row[0] if row else 0)
    except Exception:
        return 0


def _candidate_time_cols(cols: list[str]) -> list[str]:
    colset = {str(c).lower(): c for c in cols}
    priority = ["datetime", "snapshot_time", "timestamp", "received_at", "inserted_at", "created_at", "updated_at", "date_time", "dt", "time"]
    out: list[str] = []
    for p in priority:
        if p in colset and colset[p] not in out:
            out.append(colset[p])
    for c in cols:
        lc = str(c).lower()
        if ("time" in lc or lc in {"date", "dt"}) and c not in out:
            out.append(c)
    return out


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


def _load_latest_for_col(cur: sqlite3.Cursor, table: str, col: str) -> tuple[Optional[Any], Optional[datetime]]:
    try:
        qcol = _quote_ident(col)
        cur.execute(f"SELECT MAX({qcol}) FROM {_quote_ident(table)} WHERE {qcol} IS NOT NULL AND TRIM(CAST({qcol} AS TEXT)) <> ''")
        row = cur.fetchone()
        raw = row[0] if row and row[0] is not None else None
        return raw, _parse_dt(raw)
    except Exception:
        logger.debug("[RANKING PRECHECK] latest time load failed table=%s col=%s", table, col, exc_info=True)
        return None, None


def _pick_latest_time_col(cur: sqlite3.Cursor, table: str, cols: list[str]) -> tuple[Optional[str], Optional[Any], Optional[datetime], list[dict[str, Any]]]:
    diagnostics: list[dict[str, Any]] = []
    best_col: Optional[str] = None
    best_raw: Optional[Any] = None
    best_dt: Optional[datetime] = None

    for col in _candidate_time_cols(cols):
        raw, parsed = _load_latest_for_col(cur, table, col)
        diagnostics.append({"col": col, "raw": raw, "parsed": str(parsed) if parsed else None})
        if parsed is None:
            continue
        if best_dt is None or parsed > best_dt:
            best_col = col
            best_raw = raw
            best_dt = parsed

    if best_col is not None:
        return best_col, best_raw, best_dt, diagnostics

    return None, None, None, diagnostics


def _load_snapshot_rows(cur: sqlite3.Cursor, table: str, time_col: Optional[str], latest_raw: Optional[Any], *, limit: int = 2000) -> List[Dict[str, Any]]:
    try:
        if time_col and latest_raw is not None:
            qcol = _quote_ident(time_col)
            cur.execute(f"SELECT * FROM {_quote_ident(table)} WHERE {qcol} = ? LIMIT ?", (latest_raw, int(limit)))
        else:
            cur.execute(f"SELECT * FROM {_quote_ident(table)} ORDER BY rowid DESC LIMIT ?", (int(min(max(limit, 1), FAST_SCAN_ROWS)),))
        rows = cur.fetchall()
        return [dict(r) for r in rows]
    except Exception:
        logger.debug("[RANKING PRECHECK] snapshot rows load failed table=%s col=%s", table, time_col, exc_info=True)
        return []


def _is_snapshot_fresh(latest_dt: Optional[datetime]) -> bool:
    if latest_dt is None:
        return True
    try:
        age = (datetime.now() - latest_dt).total_seconds()
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


def _resolve_type(rows: list[dict[str, Any]]) -> Any:
    try:
        ranking_types = {r.get("rank_type") or r.get("ranking_type") or r.get("type") or r.get("category") for r in rows if r.get("rank_type") or r.get("ranking_type") or r.get("type") or r.get("category")}
        return ranking_types.pop() if len(ranking_types) == 1 else None
    except Exception:
        return None


def _cache_snapshot(rows: list[dict[str, Any]], ranking_type: Any, latest_raw: Any, db_path: str, table: str) -> None:
    try:
        global_data.latest_ranking_snapshot = rows
        global_data.latest_ranking_type = ranking_type
        global_data.latest_ranking_snapshot_time = latest_raw
        global_data.latest_ranking_db_path = db_path
        global_data.latest_ranking_snapshot_table = table
    except Exception:
        logger.warning("[RANKING CACHE RESTORE FAILED]", exc_info=True)


def _try_fresh_raw_fallback(cur: sqlite3.Cursor, db_path: str, *, stale_detail: dict[str, Any] | None = None) -> Dict[str, Any] | None:
    if not _env_bool("RANKING_PRECHECK_RAW_FALLBACK_ENABLED", True):
        return None

    diag_all: list[dict[str, Any]] = []
    for table in RAW_FALLBACK_TABLES:
        try:
            if not _table_exists(cur, table):
                continue
            total_rows = _count_rows(cur, table)
            if total_rows < max(1, MIN_ROWS_READY):
                continue
            cols = _table_columns(cur, table)
            if not cols:
                continue
            time_col, latest_raw, latest_dt, time_diag = _pick_latest_time_col(cur, table, cols)
            diag_all.append({"table": table, "rows": total_rows, "time_col": time_col, "latest": latest_raw, "parsed": str(latest_dt) if latest_dt else None, "diag": time_diag})
            if not _is_snapshot_fresh(latest_dt):
                continue
            rows = _load_snapshot_rows(cur, table, time_col, latest_raw, limit=RAW_FALLBACK_MAX_ROWS)
            if not rows:
                continue
            resolved_type = _resolve_type(rows)
            _cache_snapshot(rows, resolved_type, latest_raw, db_path, table)
            logger.warning(
                "[RANKING READY][RAW_FALLBACK] rows=%d total_rows=%d type=%s table=%s time_col=%s latest=%s parsed=%s db=%s stale_detail=%s",
                len(rows),
                total_rows,
                resolved_type,
                table,
                time_col,
                latest_raw,
                latest_dt,
                db_path,
                stale_detail,
            )
            return {
                "is_ready": True,
                "explicit_ready": False,
                "derived_ready": True,
                "has_snapshot": True,
                "snapshot_count": len(rows),
                "ranking_type": resolved_type,
                "source": "db_raw_fallback",
                "reason": "fresh_raw_fallback",
                "db": db_path,
                "table": table,
                "time_col": time_col,
                "latest": latest_raw,
                "parsed": str(latest_dt) if latest_dt else None,
                "total_rows": total_rows,
                "raw_fallback_diag": diag_all,
                "stale_detail": stale_detail or {},
            }
        except Exception:
            logger.debug("[RANKING PRECHECK] raw fallback failed table=%s", table, exc_info=True)
            continue
    if diag_all:
        logger.warning("[RANKING PRECHECK] raw fallback unavailable diag=%s stale_detail=%s", diag_all, stale_detail)
    return None


def _check_ranking_ready() -> Dict[str, Any]:
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

    ranking_db_path = _today_ranking_db_path()
    if not ranking_db_path:
        return _not_ready("ranking_db_path_unresolved")
    if not Path(ranking_db_path).exists():
        return _not_ready("ranking_db_not_found", db=ranking_db_path)

    conn = None
    try:
        conn = _connect_db(ranking_db_path)
        cur = conn.cursor()

        if not _table_exists(cur, SNAPSHOT_TABLE):
            fallback = _try_fresh_raw_fallback(cur, ranking_db_path, stale_detail={"reason": "snapshot_table_missing", "table": SNAPSHOT_TABLE})
            if fallback:
                return fallback
            return _not_ready("snapshot_table_missing", db=ranking_db_path, table=SNAPSHOT_TABLE)

        total_rows = _count_rows(cur, SNAPSHOT_TABLE)
        if total_rows < max(1, MIN_ROWS_READY):
            fallback = _try_fresh_raw_fallback(cur, ranking_db_path, stale_detail={"reason": "snapshot_table_empty", "table": SNAPSHOT_TABLE, "total_rows": total_rows})
            if fallback:
                return fallback
            return _not_ready("snapshot_table_empty", db=ranking_db_path, table=SNAPSHOT_TABLE, total_rows=total_rows)

        cols = _table_columns(cur, SNAPSHOT_TABLE)
        time_col, latest_raw, latest_dt, time_diag = _pick_latest_time_col(cur, SNAPSHOT_TABLE, cols)

        if not _is_snapshot_fresh(latest_dt):
            age_sec = None
            try:
                age_sec = (datetime.now() - latest_dt).total_seconds() if latest_dt else None
            except Exception:
                pass
            stale_detail = {
                "db": ranking_db_path,
                "table": SNAPSHOT_TABLE,
                "time_col": time_col,
                "latest": latest_raw,
                "parsed": str(latest_dt) if latest_dt else None,
                "age_sec": age_sec,
                "max_age_sec": MAX_SNAPSHOT_AGE_SEC,
                "total_rows": total_rows,
                "time_diag": time_diag,
            }
            logger.warning(
                "[RANKING STALE] db=%s table=%s time_col=%s latest=%s parsed=%s age_sec=%s max_age_sec=%s total_rows=%s time_diag=%s -> try raw fallback",
                ranking_db_path,
                SNAPSHOT_TABLE,
                time_col,
                latest_raw,
                latest_dt,
                age_sec,
                MAX_SNAPSHOT_AGE_SEC,
                total_rows,
                time_diag,
            )
            fallback = _try_fresh_raw_fallback(cur, ranking_db_path, stale_detail=stale_detail)
            if fallback:
                return fallback
            return _not_ready("snapshot_stale", **stale_detail)

        snapshot_rows = _load_snapshot_rows(cur, SNAPSHOT_TABLE, time_col, latest_raw)
        if not snapshot_rows:
            fallback = _try_fresh_raw_fallback(cur, ranking_db_path, stale_detail={"reason": "snapshot_rows_empty_for_latest", "table": SNAPSHOT_TABLE, "time_col": time_col, "latest": latest_raw, "total_rows": total_rows, "time_diag": time_diag})
            if fallback:
                return fallback
            return _not_ready(
                "snapshot_rows_empty_for_latest",
                db=ranking_db_path,
                table=SNAPSHOT_TABLE,
                time_col=time_col,
                latest=latest_raw,
                total_rows=total_rows,
                time_diag=time_diag,
            )

        resolved_type = _resolve_type(snapshot_rows)
        _cache_snapshot(snapshot_rows, resolved_type, latest_raw, ranking_db_path, SNAPSHOT_TABLE)

        logger.info(
            "[RANKING READY][DB] snapshot=%d total_rows=%d type=%s time_col=%s latest=%s parsed=%s db=%s time_diag=%s",
            len(snapshot_rows),
            total_rows,
            resolved_type,
            time_col,
            latest_raw,
            latest_dt,
            ranking_db_path,
            time_diag,
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
            "time_diag": time_diag,
        }

    except Exception as e:
        logger.exception("[RANKING PRECHECK ERROR] %s", e)
        return _not_ready("exception", db=ranking_db_path, error=str(e))
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _ranking_pending_count() -> int:
    try:
        import trading.entry_exit.tasks as tasks
        return int(tasks._pending_count_for_source("RANKING") or 0)
    except Exception:
        logger.debug("[RANKING PRECHECK BYPASS] pending count failed", exc_info=True)
        return 0


def precheck_ranking_entry() -> Dict[str, Any]:
    result = _check_ranking_ready()
    if not result["is_ready"]:
        # 旧 core/startup/ranking_entry_controller_timeout_patch.py (V1.9) の
        # precheck bypass-when-pending をインライン化。
        # snapshotが古い/未整備でも、既にRANKING pendingが存在するなら
        # entry_controller側の処理は進めさせる (詰まったpendingを掃除する機会を潰さない)。
        if _env_bool("RANKING_ENTRY_BYPASS_PRECHECK_WHEN_PENDING", True):
            cnt = _ranking_pending_count()
            if cnt > 0:
                bypassed = dict(result)
                bypassed["is_ready"] = True
                bypassed["explicit_ready"] = True
                bypassed["derived_ready"] = True
                bypassed["bypass_reason"] = "RANKING_PENDING_EXISTS"
                bypassed["pending_count"] = cnt
                bypassed["original_detail_reason"] = result.get("detail_reason") or result.get("reason")
                logger.warning("[RANKING PRECHECK BYPASS] pending exists -> allow entry_controller pending_count=%s original=%s", cnt, result)
                return bypassed
        logger.warning("[RANKING PRECHECK NG] %s", result)
    return result


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


__all__ = ["precheck_ranking_entry", "log_precheck_result"]
