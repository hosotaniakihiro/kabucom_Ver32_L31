# ============================================================
# File   : core/startup/ranking_entry_stale_failclosed_final_patch.py
# Version: V1-FINAL-RANKING-STALE-FAIL-CLOSED
# ------------------------------------------------------------
# Final wrapper for ranking entry scheduler.
# If today's ranking snapshot is missing, old, or not today, ranking entry
# is skipped before new pending rows are created.
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
import time
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _b(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _f(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return default if v is None or str(v).strip() == "" else float(v)
    except Exception:
        return default


def _parse_ts(v: Any):
    try:
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() in {"none", "nan", "nat"}:
            return None
        return dt.datetime.fromisoformat(s.replace("T", " ")).replace(tzinfo=None)
    except Exception:
        try:
            import pandas as pd
            x = pd.to_datetime(v, errors="coerce")
            if pd.isna(x):
                return None
            return x.to_pydatetime().replace(tzinfo=None)
        except Exception:
            return None


def _fresh_diag() -> tuple[bool, dict[str, Any]]:
    try:
        from ats.ats_ranking.db_path import get_today_ranking_db_path
        db_path = str(get_today_ranking_db_path())
    except Exception:
        db_path = ""
    now = dt.datetime.now().replace(microsecond=0)
    best = None
    source = "none"
    rows = 0
    tables = ("ranking_snapshot_1min", "ranking_raw_1min", "ranking_summary_1min", "ranking_snapshot", "ranking_raw")
    cols = ("datetime", "updated_at", "snapshot_time", "received_at", "inserted_at", "created_at", "time")
    try:
        with sqlite3.connect(db_path, timeout=2.0) as conn:
            cur = conn.cursor()
            existing = {r[0] for r in cur.execute("select name from sqlite_master where type='table'").fetchall()}
            for table in tables:
                if table not in existing:
                    continue
                try:
                    cnt = int(cur.execute(f"select count(*) from {table}").fetchone()[0] or 0)
                    rows = max(rows, cnt)
                except Exception:
                    cnt = 0
                if cnt <= 0:
                    continue
                try:
                    table_cols = {r[1] for r in cur.execute(f"pragma table_info({table})").fetchall()}
                except Exception:
                    table_cols = set()
                for col in cols:
                    if col not in table_cols:
                        continue
                    try:
                        ts = _parse_ts(cur.execute(f"select max({col}) from {table}").fetchone()[0])
                        if ts is not None and (best is None or ts > best):
                            best = ts
                            source = f"{table}.{col}"
                    except Exception:
                        pass
    except Exception:
        logger.exception("[RANKING STALE FINAL] inspect failed db=%s", db_path)

    age = None if best is None else (now - best).total_seconds()
    max_age = _f("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC", 300.0)
    ok = bool(best is not None and age is not None and age <= max_age and best.date() == now.date())
    return ok, {"db": db_path, "latest": None if best is None else str(best), "source": source, "rows": rows, "age_sec": age, "max_age_sec": max_age, "today": str(now.date())}


def _clear_pending(diag: dict[str, Any]) -> None:
    if not _b("RANKING_ENTRY_CLEAR_PENDING_ON_STALE", True):
        return
    for mod_name in ("trading.entry.pending_manager", "global_state"):
        try:
            mod = __import__(mod_name, fromlist=["x"])
            root = getattr(mod, "pending_entries", None)
            if root is None and hasattr(mod, "global_data"):
                root = getattr(mod.global_data, "pending_entries", None)
            if isinstance(root, dict):
                before = {str(k): len(v) if hasattr(v, "__len__") else 1 for k, v in root.items()}
                root.clear()
                logger.warning("[RANKING STALE FINAL] cleared pending before=%s diag=%s", before, diag)
        except Exception:
            pass


def _wrap(orig):
    @wraps(orig)
    def wrapped(*args, **kwargs):
        if _b("RANKING_ENTRY_STALE_FAILOPEN_ENABLED", False):
            return orig(*args, **kwargs)
        ok, diag = _fresh_diag()
        if not ok:
            logger.warning("[RANKING STALE FINAL] skip ranking entry before pending diag=%s", diag)
            _clear_pending(diag)
            return 0
        logger.info("[RANKING STALE FINAL] ranking source fresh diag=%s", diag)
        return orig(*args, **kwargs)
    wrapped._ranking_stale_final_v1 = True  # type: ignore[attr-defined]
    wrapped._original = orig  # type: ignore[attr-defined]
    return wrapped


def _patch_once() -> bool:
    try:
        import trading.entry_exit.tasks as tasks
        cur = getattr(tasks, "_run_ranking_entry_safe", None)
        if not callable(cur):
            return False
        if getattr(cur, "_ranking_stale_final_v1", False):
            return True
        tasks._run_ranking_entry_safe = _wrap(cur)
        logger.warning("[RANKING STALE FINAL] patched outermost target=%s", getattr(cur, "__name__", type(cur)))
        return True
    except Exception:
        logger.exception("[RANKING STALE FINAL] patch failed")
        return False


def _watch():
    for i in range(360):
        ok = _patch_once()
        if i in (0, 1, 5, 15, 60, 180, 359):
            logger.warning("[RANKING STALE FINAL] enforce ok=%s i=%s", ok, i)
        time.sleep(0.5)


def install() -> bool:
    global _INSTALLED
    os.environ.setdefault("RANKING_ENTRY_STALE_FAILOPEN_ENABLED", "0")
    os.environ.setdefault("RANKING_ENTRY_CLEAR_PENDING_ON_STALE", "1")
    os.environ.setdefault("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC", "300")
    ok = _patch_once()
    if not _INSTALLED:
        _INSTALLED = True
        threading.Thread(target=_watch, name="ranking-stale-final-watch", daemon=True).start()
    logger.warning("[RANKING STALE FINAL] installed ok=%s failopen=%s", ok, os.getenv("RANKING_ENTRY_STALE_FAILOPEN_ENABLED"))
    return ok


try:
    install()
except Exception:
    logger.exception("[RANKING STALE FINAL] auto install failed")

__all__ = ["install"]
