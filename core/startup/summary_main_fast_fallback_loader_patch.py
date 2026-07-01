# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_main_fast_fallback_loader_patch.py
# Version: V1-MAIN-FAST-PUSH-FALLBACK-NO-NAS-DB
# ------------------------------------------------------------
# main.py の summary 1m tick 用。
#
# 問題:
#   PUSH WS が一瞬不安定になると fallback_loader が NAS SQLite の
#   summary/push raw DB fallback を読みに行き、1分足だけでも 17〜25秒
#   詰まることがある。
#
# 方針:
#   main.py + interval=1 では、global_data 上のメモリ/前回mergedを最優先。
#   取れなければ既定では空で即返す。NAS DB fallback は main.py では
#   明示許可時だけ使う。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import time
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V1-MAIN-FAST-PUSH-FALLBACK-NO-NAS-DB"
_INSTALLED = False


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


def _argv_text() -> str:
    try:
        return " ".join(str(x).replace("\\", "/").lower() for x in (sys.argv or []))
    except Exception:
        return ""


def _is_main_py() -> bool:
    argv = _argv_text()
    if any(x in argv for x in ("main_database.py", "data_collectors_runner.py", "summary_database_runner.py", "push_receiver_runner.py")):
        return False
    return "main.py" in argv


def _is_main_entry_context() -> bool:
    try:
        role = str(os.getenv("SUMMARY_DB_WRITER_ROLE") or "").strip().lower()
        return _is_main_py() or _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False) or role in {"entry_only", "main_entry_only", "read_only", "no_save"}
    except Exception:
        return _is_main_py()


def _normalize_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _safe_df(value: Any) -> pd.DataFrame:
    try:
        if isinstance(value, pd.DataFrame):
            return value.copy()
        if isinstance(value, tuple) and value and isinstance(value[0], pd.DataFrame):
            return value[0].copy()
        if isinstance(value, dict):
            for k in ("df", "summary_df", "merged_df", "result_df", "latest_df", "output_df"):
                v = value.get(k)
                if isinstance(v, pd.DataFrame):
                    return v.copy()
        return pd.DataFrame(value).copy()
    except Exception:
        return pd.DataFrame()


def _normalize_df(df: pd.DataFrame, *, now: Optional[dt.datetime], interval: int) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    try:
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
        x["symbol"] = x["symbol"].map(_normalize_symbol)
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        try:
            x["datetime"] = x["datetime"].dt.tz_localize(None)
        except Exception:
            pass
        x = x.dropna(subset=["symbol", "datetime"])
        x = x[x["symbol"].astype(str).str.strip().ne("")]
        if x.empty:
            return pd.DataFrame()
        if now is not None:
            try:
                expected = pd.Timestamp(now.replace(tzinfo=None, microsecond=0)).floor(f"{int(interval)}min")
                x["_slot"] = x["datetime"].dt.floor(f"{int(interval)}min")
                x = x[x["_slot"] <= expected]
                if not x.empty:
                    latest_slot = x["_slot"].max()
                    # summary_df/merged_df は最新slotのみ、push_dfはそのままでもよいが
                    # fallbackとしては最新slotに揃える。
                    x = x[x["_slot"] == latest_slot]
                x = x.drop(columns=["_slot"], errors="ignore")
            except Exception:
                pass
        return x.reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY MAIN FAST FALLBACK] normalize failed", exc_info=True)
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


def _get_global_data() -> Any:
    try:
        from global_state import global_data
        return global_data
    except Exception:
        try:
            from core.global_context.context import global_data  # type: ignore
            return global_data
        except Exception:
            return None


def _memory_candidates(interval: int, now: Optional[dt.datetime]) -> list[tuple[str, pd.DataFrame]]:
    gd = _get_global_data()
    if gd is None:
        return []
    candidates: list[tuple[str, Any]] = []

    # 1. 直近PUSH/streamメモリ
    for name in ("push_df", "stream_data", "latest_push_df", "push_data", "push_snapshot_df"):
        try:
            candidates.append((f"global_data.{name}", getattr(gd, name, None)))
        except Exception:
            pass
    try:
        fn = getattr(gd, "get_push_df", None)
        if callable(fn):
            candidates.append(("global_data.get_push_df()", fn()))
    except Exception:
        pass

    # 2. 既存summary/merged cache
    for name in (
        f"push_merged_summary_{interval}min", f"push_merged_summary_{interval}",
        f"merged_summary_{interval}min", f"merged_summary_{interval}",
        f"push_summary_{interval}min", f"push_summary_{interval}",
        f"latest_push_summary_{interval}min", f"latest_push_summary_{interval}",
        f"summary_{interval}m_df", f"latest_summary_{interval}m_df",
        "merged_summary",
    ):
        try:
            candidates.append((f"global_data.{name}", getattr(gd, name, None)))
        except Exception:
            pass
    for method_name in ("get_merged_summary", "get_push_summary", "get_summary_history", "get_latest_summary"):
        try:
            fn = getattr(gd, method_name, None)
            if callable(fn):
                try:
                    if method_name == "get_merged_summary":
                        candidates.append((f"global_data.{method_name}({interval},source=push)", fn(interval, source="push")))
                    else:
                        candidates.append((f"global_data.{method_name}({interval})", fn(interval)))
                except TypeError:
                    candidates.append((f"global_data.{method_name}({interval})", fn(interval)))
        except Exception:
            pass

    out: list[tuple[str, pd.DataFrame]] = []
    for name, value in candidates:
        df = _normalize_df(_safe_df(value), now=now, interval=interval)
        if not df.empty:
            out.append((name, df))
    return out


def _choose(candidates: list[tuple[str, pd.DataFrame]]) -> pd.DataFrame:
    if not candidates:
        return pd.DataFrame()
    def key(item: tuple[str, pd.DataFrame]):
        _name, df = item
        ts = _latest_dt(df)
        return (pd.Timestamp.min if ts is None or pd.isna(ts) else ts, len(df), _symbols_count(df))
    candidates.sort(key=key, reverse=True)
    name, df = candidates[0]
    logger.warning(
        "[SUMMARY MAIN FAST FALLBACK] selected memory candidate name=%s rows=%s symbols=%s latest_dt=%s",
        name,
        len(df),
        _symbols_count(df),
        _latest_dt(df),
    )
    return df.reset_index(drop=True)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if not _is_main_entry_context():
        logger.warning("[SUMMARY MAIN FAST FALLBACK] skipped non-main context version=%s", VERSION)
        return False
    try:
        import scheduler_jobs.summary.fallback_loader as fl
        orig = getattr(fl, "fallback_push_summary_df", None)
        if not callable(orig):
            logger.warning("[SUMMARY MAIN FAST FALLBACK] target missing")
            return False
        if getattr(orig, "_summary_main_fast_fallback_wrapped", False):
            _INSTALLED = True
            return True

        os.environ.setdefault("SUMMARY_MAIN_SKIP_PUSH_DB_FALLBACK", "1")
        os.environ.setdefault("SUMMARY_MAIN_FAST_FALLBACK_ENABLED", "1")

        def _patched_fallback_push_summary_df(interval: int, *, now: Optional[dt.datetime] = None) -> pd.DataFrame:
            interval_i = int(interval)
            if not (_is_main_entry_context() and interval_i == 1 and _env_bool("SUMMARY_MAIN_FAST_FALLBACK_ENABLED", True)):
                return orig(interval_i, now=now)
            t0 = time.perf_counter()
            now_i = (now or dt.datetime.now()).replace(tzinfo=None, microsecond=0)
            try:
                mem = _choose(_memory_candidates(interval_i, now_i))
                if not mem.empty:
                    logger.warning(
                        "[SUMMARY MAIN FAST FALLBACK] return memory interval=1 rows=%s elapsed=%.3fs skip_db=%s",
                        len(mem),
                        time.perf_counter() - t0,
                        os.getenv("SUMMARY_MAIN_SKIP_PUSH_DB_FALLBACK"),
                    )
                    return mem
                if _env_bool("SUMMARY_MAIN_SKIP_PUSH_DB_FALLBACK", True):
                    logger.warning(
                        "[SUMMARY MAIN FAST FALLBACK] memory empty -> skip NAS DB fallback interval=1 elapsed=%.3fs",
                        time.perf_counter() - t0,
                    )
                    return pd.DataFrame()
            except Exception:
                logger.exception("[SUMMARY MAIN FAST FALLBACK] memory fallback failed interval=1")
                if _env_bool("SUMMARY_MAIN_SKIP_PUSH_DB_FALLBACK", True):
                    return pd.DataFrame()
            return orig(interval_i, now=now_i)

        _patched_fallback_push_summary_df._summary_main_fast_fallback_wrapped = True  # type: ignore[attr-defined]
        _patched_fallback_push_summary_df._original = orig  # type: ignore[attr-defined]
        fl.fallback_push_summary_df = _patched_fallback_push_summary_df
        _INSTALLED = True
        logger.warning(
            "[SUMMARY MAIN FAST FALLBACK] installed version=%s skip_db=%s main=%s",
            VERSION,
            os.getenv("SUMMARY_MAIN_SKIP_PUSH_DB_FALLBACK"),
            _is_main_py(),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY MAIN FAST FALLBACK] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MAIN FAST FALLBACK] auto install failed")

__all__ = ["VERSION", "install"]
