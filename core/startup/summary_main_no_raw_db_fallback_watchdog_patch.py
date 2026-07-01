# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_main_no_raw_db_fallback_watchdog_patch.py
# Version: V1-MAIN-NO-RAW-DB-FALLBACK-WATCHDOG
# ------------------------------------------------------------
# push_summary_fallback_and_active_price_patch(REV4) が後から
# fallback_push_summary_df を上書きしても、main.py + interval=1 では
# pushYYYYMMDD.db の raw DB fallback を使わないように再適用する。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import threading
import time
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V1-MAIN-NO-RAW-DB-FALLBACK-WATCHDOG"
_INSTALLED = False
_THREAD_STARTED = False


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        s = str(raw).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip() != "":
            return int(float(str(raw).strip()))
    except Exception:
        pass
    return int(default)


def _argv_text() -> str:
    try:
        return " ".join(str(x).replace("\\", "/").lower() for x in (sys.argv or []))
    except Exception:
        return ""


def _is_main_context() -> bool:
    try:
        argv = _argv_text()
        if any(x in argv for x in ("main_database.py", "data_collectors_runner.py", "summary_database_runner.py", "push_receiver_runner.py")):
            return False
        role = str(os.getenv("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
        return "main.py" in argv or _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False) or role in {"entry_only", "main_entry_only", "read_only", "no_save"}
    except Exception:
        return False


def _normalize_df(df: Any, *, interval: int, now: Optional[dt.datetime]) -> pd.DataFrame:
    try:
        if isinstance(df, tuple) and df and isinstance(df[0], pd.DataFrame):
            df = df[0]
        elif isinstance(df, dict):
            for k in ("df", "summary_df", "merged_df", "result_df", "latest_df"):
                if isinstance(df.get(k), pd.DataFrame):
                    df = df.get(k)
                    break
        if not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame()
        x = df.copy()
        x = x.loc[:, ~pd.Index(x.columns).duplicated()].copy()
        if "symbol" not in x.columns:
            for c in ("Symbol", "Code", "code", "symbol_code"):
                if c in x.columns:
                    x["symbol"] = x[c]
                    break
        if "datetime" not in x.columns:
            for c in ("end_time", "start_time", "time", "snapshot_time", "received_at"):
                if c in x.columns:
                    x["datetime"] = x[c]
                    break
        if "symbol" not in x.columns or "datetime" not in x.columns:
            return pd.DataFrame()
        x["symbol"] = x["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        try:
            x["datetime"] = x["datetime"].dt.tz_localize(None)
        except Exception:
            pass
        x = x.dropna(subset=["symbol", "datetime"])
        x = x[x["symbol"].ne("")]
        if x.empty:
            return pd.DataFrame()
        if now is not None:
            try:
                expected = pd.Timestamp(now.replace(tzinfo=None, microsecond=0)).floor(f"{int(interval)}min")
                x["_slot"] = x["datetime"].dt.floor(f"{int(interval)}min")
                x = x[x["_slot"] <= expected]
                if not x.empty:
                    latest_slot = x["_slot"].max()
                    x = x[x["_slot"] == latest_slot]
                x = x.drop(columns=["_slot"], errors="ignore")
            except Exception:
                pass
        return x.reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY MAIN NO RAW DB FALLBACK] normalize failed", exc_info=True)
        return pd.DataFrame()


def _symbols_count(df: pd.DataFrame) -> int:
    try:
        return int(df["symbol"].astype(str).nunique()) if isinstance(df, pd.DataFrame) and "symbol" in df.columns else 0
    except Exception:
        return 0


def _latest_dt(df: pd.DataFrame):
    try:
        return pd.to_datetime(df["datetime"], errors="coerce").max() if isinstance(df, pd.DataFrame) and "datetime" in df.columns else None
    except Exception:
        return None


def _global_data() -> Any:
    try:
        from global_state import global_data
        return global_data
    except Exception:
        try:
            from core.global_context.context import global_data  # type: ignore
            return global_data
        except Exception:
            return None


def _memory_candidates(interval: int, now: dt.datetime) -> list[tuple[str, pd.DataFrame]]:
    gd = _global_data()
    if gd is None:
        return []
    raw: list[tuple[str, Any]] = []
    for name in (
        "push_df", "stream_data", "latest_push_df", "push_data", "push_snapshot_df",
        f"push_summary_{interval}min", f"push_summary_{interval}",
        f"latest_push_summary_{interval}min", f"latest_push_summary_{interval}",
        f"push_merged_summary_{interval}min", f"push_merged_summary_{interval}",
        f"merged_summary_{interval}min", f"merged_summary_{interval}",
        f"summary_{interval}m_df", f"latest_summary_{interval}m_df",
        "merged_summary",
    ):
        try:
            raw.append((f"global_data.{name}", getattr(gd, name, None)))
        except Exception:
            pass
    for method in ("get_push_df", "get_merged_summary", "get_push_summary", "get_summary_history", "get_latest_summary"):
        try:
            fn = getattr(gd, method, None)
            if not callable(fn):
                continue
            try:
                if method == "get_push_df":
                    value = fn()
                elif method == "get_merged_summary":
                    value = fn(interval, source="push")
                else:
                    value = fn(interval)
            except TypeError:
                value = fn(interval)
            raw.append((f"global_data.{method}", value))
        except Exception:
            pass
    out: list[tuple[str, pd.DataFrame]] = []
    for name, value in raw:
        df = _normalize_df(value, interval=interval, now=now)
        if not df.empty:
            out.append((name, df))
    return out


def _choose(candidates: list[tuple[str, pd.DataFrame]]) -> tuple[str, pd.DataFrame]:
    if not candidates:
        return "", pd.DataFrame()
    def key(item: tuple[str, pd.DataFrame]):
        _name, df = item
        ts = _latest_dt(df)
        if ts is None or pd.isna(ts):
            ts = pd.Timestamp.min
        return (ts, len(df), _symbols_count(df))
    candidates.sort(key=key, reverse=True)
    return candidates[0][0], candidates[0][1].reset_index(drop=True)


def _patch_loader_once() -> bool:
    if not _is_main_context() or not _env_bool("SUMMARY_MAIN_DISABLE_RAW_DB_FALLBACK", True):
        return False
    try:
        os.environ.setdefault("SUMMARY_MAIN_DISABLE_RAW_DB_FALLBACK", "1")
        os.environ.setdefault("SUMMARY_MAIN_SKIP_PUSH_DB_FALLBACK", "1")
        os.environ.setdefault("PUSH_SUMMARY_RAW_DB_FALLBACK_ENABLED_IN_MAIN", "0")

        import scheduler_jobs.summary.fallback_loader as fl
        cur = getattr(fl, "fallback_push_summary_df", None)
        if getattr(cur, "_summary_main_no_raw_db_watchdog", False):
            return True

        def _fallback_push_summary_df_no_raw(interval: int, *, now=None) -> pd.DataFrame:
            interval_i = int(interval)
            if not (_is_main_context() and interval_i == 1 and _env_bool("SUMMARY_MAIN_DISABLE_RAW_DB_FALLBACK", True)):
                if callable(cur):
                    return cur(interval_i, now=now)
                return pd.DataFrame()
            now_i = (now or dt.datetime.now()).replace(tzinfo=None, microsecond=0)
            name, df = _choose(_memory_candidates(interval_i, now_i))
            if not df.empty:
                logger.warning(
                    "[SUMMARY MAIN NO RAW DB FALLBACK] memory return interval=1 name=%s rows=%s symbols=%s latest_dt=%s version=%s",
                    name,
                    len(df),
                    _symbols_count(df),
                    _latest_dt(df),
                    VERSION,
                )
                return df
            logger.warning(
                "[SUMMARY MAIN NO RAW DB FALLBACK] memory empty -> raw/NAS DB fallback blocked interval=1 version=%s",
                VERSION,
            )
            return pd.DataFrame()

        _fallback_push_summary_df_no_raw._summary_main_no_raw_db_watchdog = True  # type: ignore[attr-defined]
        _fallback_push_summary_df_no_raw._original = cur  # type: ignore[attr-defined]
        fl.fallback_push_summary_df = _fallback_push_summary_df_no_raw

        try:
            import scheduler_jobs.summary.runner_core as rc
            rc.fallback_push_summary_df = _fallback_push_summary_df_no_raw
        except Exception:
            pass

        # REV4 patch itself may call its private raw loader. Replace it too when loaded.
        try:
            import core.startup.push_summary_fallback_and_active_price_patch as rev4
            def _blocked_raw_loader(interval_i: int, *, now_i=None):
                if _is_main_context() and int(interval_i) == 1 and _env_bool("SUMMARY_MAIN_DISABLE_RAW_DB_FALLBACK", True):
                    logger.warning(
                        "[SUMMARY MAIN NO RAW DB FALLBACK] blocked REV4 raw DB loader interval=1 version=%s",
                        VERSION,
                    )
                    return pd.DataFrame()
                return pd.DataFrame()
            rev4._load_recent_push_raw_summary = _blocked_raw_loader
        except Exception:
            pass

        logger.warning("[SUMMARY MAIN NO RAW DB FALLBACK] patched fallback loader version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY MAIN NO RAW DB FALLBACK] patch failed")
        return False


def _watchdog_loop() -> None:
    loops = max(3, _env_int("SUMMARY_MAIN_NO_RAW_DB_WATCHDOG_LOOPS", 45))
    interval = max(0.2, float(os.getenv("SUMMARY_MAIN_NO_RAW_DB_WATCHDOG_INTERVAL_SEC") or "1.0"))
    for i in range(loops):
        try:
            _patch_loader_once()
        except Exception:
            logger.exception("[SUMMARY MAIN NO RAW DB FALLBACK] watchdog iteration failed i=%s", i)
        time.sleep(interval)
    logger.warning("[SUMMARY MAIN NO RAW DB FALLBACK] watchdog done loops=%s", loops)


def install() -> bool:
    global _INSTALLED, _THREAD_STARTED
    if not _is_main_context():
        logger.warning("[SUMMARY MAIN NO RAW DB FALLBACK] skipped non-main context version=%s", VERSION)
        return False
    ok = _patch_loader_once()
    _INSTALLED = bool(ok)
    if not _THREAD_STARTED and _env_bool("SUMMARY_MAIN_NO_RAW_DB_WATCHDOG", True):
        _THREAD_STARTED = True
        threading.Thread(target=_watchdog_loop, name="summary-main-no-raw-db-fallback-watchdog", daemon=True).start()
        logger.warning("[SUMMARY MAIN NO RAW DB FALLBACK] watchdog started version=%s", VERSION)
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[SUMMARY MAIN NO RAW DB FALLBACK] auto install failed")

__all__ = ["VERSION", "install"]
