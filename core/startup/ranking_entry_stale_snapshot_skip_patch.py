# ============================================================
# File   : core/startup/ranking_entry_stale_snapshot_skip_patch.py
# Version: V1-RANKING-STALE-SNAPSHOT-SKIP
# ------------------------------------------------------------
# 目的:
#   ranking DB / ranking_snapshot_1min が stale の時に、
#   ranking entry が pending だけ作って entry_controller 側で
#   RANKING_PRECHECK_NG になる無駄ループを止める。
#
# 背景:
#   ログ例:
#     RANKING_PRECHECK_NG latest=2026-06-04 16:22:14 age_sec=73801
#     pending_before={'4095':1,'4270':1,'3905':1,'5016':1}
#
# 方針:
#   - _run_ranking_entry_safe() の前に ranking_snapshot_1min の鮮度を見る。
#   - stale なら build_fn を呼ばず、pendingを作らず 0 を返す。
#   - Summary/PULLBACK/TONOSAMA は止めない。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_RUN = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _parse_dt(v: Any) -> dt.datetime | None:
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


def _latest_snapshot_time(db_path: str) -> tuple[dt.datetime | None, str, int]:
    if not db_path:
        return None, "no_db_path", 0
    cols = ["updated_at", "datetime", "snapshot_time", "created_at", "time"]
    tables = ["ranking_snapshot_1min", "ranking_snapshot", "ranking_raw"]
    try:
        with sqlite3.connect(str(db_path), timeout=2.0) as conn:
            cur = conn.cursor()
            existing = {r[0] for r in cur.execute("select name from sqlite_master where type='table'").fetchall()}
            best_dt = None
            best_src = "no_time"
            best_rows = 0
            for table in tables:
                if table not in existing:
                    continue
                try:
                    cnt = int(cur.execute(f"select count(*) from {table}").fetchone()[0] or 0)
                except Exception:
                    cnt = 0
                best_rows = max(best_rows, cnt)
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
                        raw = cur.execute(f"select max({col}) from {table}").fetchone()[0]
                        parsed = _parse_dt(raw)
                        if parsed is not None and (best_dt is None or parsed > best_dt):
                            best_dt = parsed
                            best_src = f"{table}.{col}"
                    except Exception:
                        continue
            return best_dt, best_src, best_rows
    except Exception:
        logger.exception("[RANKING STALE SNAPSHOT SKIP] db inspect failed path=%s", db_path)
        return None, "inspect_error", 0


def _ranking_snapshot_fresh() -> tuple[bool, dict[str, Any]]:
    try:
        from ats.ats_ranking.db_path import get_usable_ranking_db_path
        db_path = get_usable_ranking_db_path(force_refresh=True, allow_fallback=False, prefer_today_even_if_empty=True)
    except Exception:
        logger.exception("[RANKING STALE SNAPSHOT SKIP] resolve db failed")
        db_path = None

    latest, src, rows = _latest_snapshot_time(str(db_path or ""))
    max_age = _env_float("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC", 300.0)
    now = dt.datetime.now()
    age = None if latest is None else (now - latest).total_seconds()
    ok = latest is not None and age is not None and age <= max_age
    diag = {
        "ok": bool(ok),
        "db": str(db_path or ""),
        "latest": latest.isoformat(sep=" ") if latest else None,
        "source": src,
        "rows": rows,
        "age_sec": None if age is None else round(float(age), 3),
        "max_age_sec": max_age,
    }
    return bool(ok), diag


def install() -> bool:
    global _INSTALLED, _ORIG_RUN
    if _INSTALLED:
        return True
    try:
        if not _env_bool("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE", True):
            logger.warning("[RANKING STALE SNAPSHOT SKIP] disabled by env")
            return False
        os.environ.setdefault("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC", "300")
        import trading.entry_exit.tasks as tasks
        cur = getattr(tasks, "_run_ranking_entry_safe", None)
        if getattr(cur, "_ranking_stale_snapshot_skip_v1", False):
            _INSTALLED = True
            return True
        if not callable(cur):
            logger.warning("[RANKING STALE SNAPSHOT SKIP] target missing")
            return False
        _ORIG_RUN = getattr(cur, "_original", cur)

        @wraps(_ORIG_RUN)
        def wrapped_run_ranking_entry_safe(*args: Any, **kwargs: Any):
            try:
                ok, diag = _ranking_snapshot_fresh()
                if not ok:
                    logger.warning("[RANKING STALE SNAPSHOT SKIP] skip ranking entry before pending diag=%s", diag)
                    return 0
                logger.info("[RANKING STALE SNAPSHOT SKIP] ranking snapshot fresh diag=%s", diag)
            except Exception:
                logger.exception("[RANKING STALE SNAPSHOT SKIP] precheck failed -> fail closed ranking entry")
                return 0
            return _ORIG_RUN(*args, **kwargs)

        wrapped_run_ranking_entry_safe._ranking_stale_snapshot_skip_v1 = True  # type: ignore[attr-defined]
        wrapped_run_ranking_entry_safe._original = _ORIG_RUN  # type: ignore[attr-defined]
        tasks._run_ranking_entry_safe = wrapped_run_ranking_entry_safe
        _INSTALLED = True
        logger.warning(
            "[RANKING STALE SNAPSHOT SKIP] installed v1 enabled=%s max_age=%s",
            os.getenv("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE", "1"),
            os.getenv("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC"),
        )
        return True
    except Exception:
        logger.exception("[RANKING STALE SNAPSHOT SKIP] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[RANKING STALE SNAPSHOT SKIP] auto install failed")

__all__ = ["install"]
