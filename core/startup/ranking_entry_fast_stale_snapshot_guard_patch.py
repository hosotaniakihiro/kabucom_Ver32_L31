# ============================================================
# File   : core/startup/ranking_entry_fast_stale_snapshot_guard_patch.py
# Version: V1-FAST-RANKING-STALE-SNAPSHOT-GUARD
# ------------------------------------------------------------
# 目的:
#   ranking_entry_fast_runtime_patch が run_ranking_entry_pipeline を直接
#   置き換えた後でも、古い ranking_snapshot_1min から pending を作らない。
#
# ログ上の問題:
#   pending added symbol=4095 source=RANKING ...
#   その後 entry_precheck_ranking で
#   RANKING STALE latest=2026-06-04 16:22:14 age_sec=80780 max_age=300
#
# 方針:
#   - trading.ranking.entry_from_ranking.run_ranking_entry_pipeline
#     と entry_from_ranking を直接 wrap。
#   - stale なら build前に return 0。
#   - Summary AI / PULLBACK / TONOSAMA は触らない。
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
        if not s or s.lower() in {"none", "nan", "nat", "<na>"}:
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


def _ranking_db_path() -> str:
    try:
        from ats.ats_ranking.db_path import get_usable_ranking_db_path
        p = get_usable_ranking_db_path(force_refresh=True, allow_fallback=False, prefer_today_even_if_empty=True)
        if p:
            return str(p)
    except Exception:
        logger.debug("[FAST RANKING STALE GUARD] db resolver failed", exc_info=True)
    try:
        root = os.getenv("AUTOSTOCK_ROOT", r"\\192.168.0.22\AutoStockBuyAndSell")
        today = dt.datetime.now().strftime("%Y%m%d")
        return os.path.join(root, "raw_data", "kabu_station", "ranking", f"ranking{today}.db")
    except Exception:
        return ""


def _latest_snapshot_diag() -> dict[str, Any]:
    db = _ranking_db_path()
    tables = ["ranking_snapshot_1min", "ranking_snapshot", "ranking_raw"]
    cols = ["updated_at", "datetime", "snapshot_time", "received_at", "inserted_at", "created_at", "time"]
    best_dt = None
    best_src = "no_time"
    rows = 0
    diag_cols = []
    try:
        if not db or not os.path.exists(db):
            return {"ok": False, "reason": "db_missing", "db": db, "latest": None, "rows": 0}
        with sqlite3.connect(db, timeout=2.0) as conn:
            cur = conn.cursor()
            existing = {r[0] for r in cur.execute("select name from sqlite_master where type='table'").fetchall()}
            for table in tables:
                if table not in existing:
                    continue
                try:
                    cnt = int(cur.execute(f"select count(*) from {table}").fetchone()[0] or 0)
                except Exception:
                    cnt = 0
                rows = max(rows, cnt)
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
                        diag_cols.append({"table": table, "col": col, "raw": raw, "parsed": parsed.isoformat(sep=" ") if parsed else None})
                        if parsed is not None and (best_dt is None or parsed > best_dt):
                            best_dt = parsed
                            best_src = f"{table}.{col}"
                    except Exception:
                        continue
        now = dt.datetime.now()
        max_age = _env_float("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC", 300.0)
        age = None if best_dt is None else (now - best_dt).total_seconds()
        ok = best_dt is not None and age is not None and age <= max_age
        return {
            "ok": bool(ok),
            "db": db,
            "latest": best_dt.isoformat(sep=" ") if best_dt else None,
            "source": best_src,
            "rows": rows,
            "age_sec": None if age is None else round(float(age), 3),
            "max_age_sec": max_age,
            "time_diag": diag_cols[:12],
        }
    except Exception:
        logger.exception("[FAST RANKING STALE GUARD] inspect failed db=%s", db)
        return {"ok": False, "reason": "inspect_error", "db": db, "latest": None, "rows": rows}


def _should_skip() -> tuple[bool, dict[str, Any]]:
    if not _env_bool("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE", True):
        return False, {"reason": "disabled"}
    diag = _latest_snapshot_diag()
    return not bool(diag.get("ok")), diag


def _wrap_func(fn, label: str):
    base = getattr(fn, "_original", fn)

    @wraps(base)
    def wrapper(*args, **kwargs):
        skip, diag = _should_skip()
        if skip:
            logger.warning("[FAST RANKING STALE GUARD] skip before pending label=%s diag=%s", label, diag)
            return 0
        logger.info("[FAST RANKING STALE GUARD] fresh label=%s diag=%s", label, diag)
        return base(*args, **kwargs)

    wrapper._fast_ranking_stale_guard_v1 = True  # type: ignore[attr-defined]
    wrapper._original = base  # type: ignore[attr-defined]
    return wrapper


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE", "1")
        os.environ.setdefault("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC", "300")
        import trading.ranking.entry_from_ranking as efr
        patched = []
        for name in ("run_ranking_entry_pipeline", "entry_from_ranking"):
            cur = getattr(efr, name, None)
            if callable(cur) and not getattr(cur, "_fast_ranking_stale_guard_v1", False):
                setattr(efr, name, _wrap_func(cur, name))
                patched.append(name)
        _INSTALLED = True
        logger.warning(
            "[FAST RANKING STALE GUARD] installed v1 patched=%s enabled=%s max_age=%s",
            patched,
            os.environ.get("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE"),
            os.environ.get("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC"),
        )
        return True
    except Exception:
        logger.exception("[FAST RANKING STALE GUARD] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[FAST RANKING STALE GUARD] auto install failed")

__all__ = ["install"]
