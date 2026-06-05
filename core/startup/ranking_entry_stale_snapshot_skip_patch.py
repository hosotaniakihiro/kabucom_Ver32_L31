# ============================================================
# File   : core/startup/ranking_entry_stale_snapshot_skip_patch.py
# Version: V3-STALE-SNAPSHOT-WARN-ONLY-FAILOPEN
# ------------------------------------------------------------
# 目的:
#   ranking DB / ranking_snapshot_1min が stale の時に診断ログを出す。
#
# V3:
#   - 2026-06-05 実運用ログで ranking_snapshot_1min.updated_at が前日
#     のままでも、ranking raw / push / runtime summary は当日更新され、
#     ranking entry 候補を作れる状態だった。
#   - V2 の fail-closed は pending作成前に ranking entry 自体を止め、
#     「エントリーされない」原因になったため、デフォルトを warn-only に変更。
#   - stale 時も原則 orig を実行する。明示的に
#       RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE=1
#     を設定した場合だけ旧fail-closed動作に戻せる。
#   - stale 時の pending clear もデフォルトOFF。
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
    cols = ["updated_at", "datetime", "snapshot_time", "received_at", "inserted_at", "created_at", "time"]
    # ranking_snapshot_1min が古く残るケースがあるため、raw/summary系も見る。
    tables = [
        "ranking_snapshot_1min",
        "ranking_raw_1min",
        "ranking_summary_1min",
        "ranking_snapshot",
        "ranking_raw",
    ]
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
    max_age = _env_float("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC", _env_float("RANKING_PRECHECK_MAX_AGE_SEC", 300.0))
    now = dt.datetime.now()
    age = None if latest is None else (now - latest).total_seconds()
    require_today = _env_bool("RANKING_ENTRY_REQUIRE_TODAY", True)
    same_day = latest is not None and latest.date() == now.date()
    ok = latest is not None and age is not None and age <= max_age and (same_day or not require_today)
    diag = {
        "ok": bool(ok),
        "db": str(db_path or ""),
        "latest": latest.isoformat(sep=" ") if latest else None,
        "source": src,
        "rows": rows,
        "age_sec": None if age is None else round(float(age), 3),
        "max_age_sec": max_age,
        "require_today": require_today,
        "same_day": bool(same_day),
    }
    return bool(ok), diag


def _clear_ranking_pending(reason: str, diag: dict[str, Any]) -> None:
    if not _env_bool("RANKING_ENTRY_CLEAR_PENDING_ON_STALE", False):
        return
    try:
        from trading.entry import pending_manager
        root = getattr(pending_manager, "pending_entries", None)
        if isinstance(root, dict):
            before = {str(k): len(v) if hasattr(v, "__len__") else 1 for k, v in root.items()}
            root.clear()
            logger.warning("[RANKING STALE SNAPSHOT SKIP] cleared pending_manager pending reason=%s before=%s diag=%s", reason, before, diag)
    except Exception:
        logger.debug("[RANKING STALE SNAPSHOT SKIP] pending_manager clear skipped", exc_info=True)

    try:
        from global_state import global_data
        root = getattr(global_data, "pending_entries", None)
        if isinstance(root, dict):
            before = {str(k): len(v) if hasattr(v, "__len__") else 1 for k, v in root.items()}
            root.clear()
            logger.warning("[RANKING STALE SNAPSHOT SKIP] cleared global pending reason=%s before=%s diag=%s", reason, before, diag)
    except Exception:
        logger.debug("[RANKING STALE SNAPSHOT SKIP] global pending clear skipped", exc_info=True)


def _make_wrapper(orig):
    @wraps(orig)
    def wrapped_run_ranking_entry_safe(*args: Any, **kwargs: Any):
        try:
            ok, diag = _ranking_snapshot_fresh()
            if not ok:
                if _env_bool("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE", False):
                    logger.warning("[RANKING STALE SNAPSHOT SKIP] skip ranking entry before pending diag=%s", diag)
                    _clear_ranking_pending("ranking_snapshot_stale", diag)
                    return 0
                logger.warning("[RANKING STALE SNAPSHOT SKIP] stale but fail-open continue ranking entry diag=%s", diag)
            else:
                logger.info("[RANKING STALE SNAPSHOT SKIP] ranking snapshot fresh diag=%s", diag)
        except Exception:
            if _env_bool("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE", False):
                logger.exception("[RANKING STALE SNAPSHOT SKIP] precheck failed -> fail closed ranking entry")
                return 0
            logger.exception("[RANKING STALE SNAPSHOT SKIP] precheck failed -> fail-open ranking entry")
        return orig(*args, **kwargs)

    wrapped_run_ranking_entry_safe._ranking_stale_snapshot_skip_v3 = True  # type: ignore[attr-defined]
    wrapped_run_ranking_entry_safe._original = orig  # type: ignore[attr-defined]
    return wrapped_run_ranking_entry_safe


def _unwrap_old_stale_guard(cur):
    # V2 wrapper が既に入っている場合は、その内側の original へ戻してからV3を被せる。
    try:
        if getattr(cur, "_ranking_stale_snapshot_skip_v3", False):
            return cur
        if getattr(cur, "_ranking_stale_snapshot_skip_v2", False):
            orig = getattr(cur, "_original", None)
            if callable(orig):
                return orig
    except Exception:
        pass
    return cur


def _patch_once() -> bool:
    global _ORIG_RUN
    try:
        import trading.entry_exit.tasks as tasks
        cur = getattr(tasks, "_run_ranking_entry_safe", None)
        if not callable(cur):
            logger.warning("[RANKING STALE SNAPSHOT SKIP] target missing")
            return False
        if getattr(cur, "_ranking_stale_snapshot_skip_v3", False):
            return True
        base = _unwrap_old_stale_guard(cur)
        _ORIG_RUN = base
        tasks._run_ranking_entry_safe = _make_wrapper(base)
        logger.warning(
            "[RANKING STALE SNAPSHOT SKIP] patched outermost v3 target=%s skip_if_stale=%s clear_pending=%s",
            getattr(base, "__name__", type(base)),
            os.getenv("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE", "0"),
            os.getenv("RANKING_ENTRY_CLEAR_PENDING_ON_STALE", "0"),
        )
        return True
    except Exception:
        logger.exception("[RANKING STALE SNAPSHOT SKIP] patch_once failed")
        return False


def _watch() -> None:
    # Other startup patches can re-wrap _run_ranking_entry_safe repeatedly.
    # Re-apply this guard as the outermost wrapper for the first two minutes.
    for i in range(240):
        ok = _patch_once()
        if i in (0, 1, 5, 15, 30, 60, 120, 239):
            logger.warning("[RANKING STALE SNAPSHOT SKIP] enforce v3 ok=%s i=%s", ok, i)
        time.sleep(0.5)


def install() -> bool:
    global _INSTALLED
    try:
        # V3はデフォルトでfail-open。旧挙動が必要な時だけ環境変数で明示する。
        os.environ.setdefault("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE", "0")
        os.environ.setdefault("RANKING_ENTRY_REQUIRE_TODAY", "1")
        os.environ.setdefault("RANKING_ENTRY_CLEAR_PENDING_ON_STALE", "0")
        os.environ.setdefault("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC", "300")
        ok = _patch_once()
        if not _INSTALLED:
            threading.Thread(target=_watch, name="ranking-stale-snapshot-skip-enforcer", daemon=True).start()
            _INSTALLED = True
        logger.warning(
            "[RANKING STALE SNAPSHOT SKIP] installed v3 ok=%s skip_if_stale=%s max_age=%s require_today=%s clear_pending=%s",
            ok,
            os.getenv("RANKING_ENTRY_SKIP_IF_SNAPSHOT_STALE", "0"),
            os.getenv("RANKING_ENTRY_SNAPSHOT_MAX_AGE_SEC"),
            os.getenv("RANKING_ENTRY_REQUIRE_TODAY"),
            os.getenv("RANKING_ENTRY_CLEAR_PENDING_ON_STALE", "0"),
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
