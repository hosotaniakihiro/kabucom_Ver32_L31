# ============================================================
# File   : core/startup/push_summary_realtime_patch.py
# Version: REV5-PUSH-WRITER-FORCE-AND-NO-RUNTIME-TOKEN-REFRESH
# ------------------------------------------------------------
# PUSH DB flush後にsummaryを軽く再計算するruntime patch。
#
# REV5:
#   - push_receiver_runner / main_database 側では PUSH DB writer を必ず有効化し、
#     memory_only=True / writer_ready=False / total_flushed=0 のまま進む状態を防ぐ。
#   - start_push_stream 呼び出し時に StreamDBWriter singleton を明示注入する。
#   - 401時の runtime refresh を抑止し、settings.ini / 既存token再利用だけに寄せる。
#
# REV4:
#   - 起動直後 bootstrap で 3m/5m まで再計算すると、PUSH履歴不足時に
#     latest_dt=11:35 の古い5分足が global cache に入ることがある。
#   - bootstrap は既定で 1m のみに限定する。
#   - 通常の push_flush 後 rebuild は従来通り 1,3,5 を対象にする。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sys
import threading
import time
from typing import Any, Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_STREAM_WRITER_FLUSH = None
_ORIGINAL_PUSH_START = None
_ORIGINAL_RUNNER_PUSH_START = None
_TRIGGER_LOCK = threading.RLock()
_TRIGGER_RUNNING = False
_LAST_TRIGGER_AT = 0.0
_DEFAULT_INTERVALS = (1, 3, 5)
_BOOTSTRAP_DEFAULT_INTERVALS = (1,)


def _env_bool(name: str, default: bool) -> bool:
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
        return float(str(v).strip())
    except Exception:
        return float(default)


def _argv_text() -> str:
    try:
        return " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
    except Exception:
        return ""


def _is_database_or_push_receiver_process() -> bool:
    argv = _argv_text()
    if any(x in argv for x in (
        "main_database.py",
        "data_collectors_runner.py",
        "push_receiver_runner.py",
        "summary_database_runner.py",
        "ranking_collector_runner.py",
    )):
        return True
    return any(_env_bool(x, False) for x in (
        "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
        "AUTOSTOCK_MAIN_DATABASE_PROCESS",
        "AUTOSTOCK_SUMMARY_DB_WRITER",
        "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
    ))


def _force_push_writer_env_if_database_process() -> None:
    """DB/data-collector側では PUSH DB writer を必ず有効にする。"""
    if not _is_database_or_push_receiver_process():
        return
    os.environ["AUTOSTOCK_DATA_COLLECTORS_PROCESS"] = "1"
    os.environ["AUTOSTOCK_MAIN_MEMORY_ONLY"] = "0"
    os.environ["AUTOSTOCK_SKIP_DATA_COLLECTOR_WORK_IN_MAIN"] = "0"
    os.environ["PUSH_STREAM_DB_WRITE"] = "1"
    os.environ.setdefault("PUSH_STREAM_ORDER_BOOK_WRITE", "1")


def _parse_intervals(value: str | None, default: Iterable[int] = _DEFAULT_INTERVALS) -> list[int]:
    if not value or not str(value).strip():
        return [int(x) for x in default]
    out: list[int] = []
    for part in str(value).replace(";", ",").split(","):
        s = part.strip().lower().replace("min", "").replace("m", "")
        if not s:
            continue
        try:
            n = int(float(s))
            if n > 0 and n not in out:
                out.append(n)
        except Exception:
            pass
    return out or [int(x) for x in default]


def _default_intervals() -> list[int]:
    return _parse_intervals(os.getenv("PUSH_SUMMARY_REALTIME_DEFAULT_INTERVALS"), default=_DEFAULT_INTERVALS)


def _bootstrap_intervals() -> list[int]:
    return _parse_intervals(os.getenv("PUSH_SUMMARY_REALTIME_BOOTSTRAP_INTERVALS"), default=_BOOTSTRAP_DEFAULT_INTERVALS)


def _should_bootstrap_rebuild_here() -> bool:
    if not _env_bool("PUSH_SUMMARY_REALTIME_BOOTSTRAP_REBUILD_ENABLED", True):
        return False
    if _env_bool("PUSH_SUMMARY_REALTIME_BOOTSTRAP_FORCE", False):
        return True
    argv = _argv_text()
    return any(x in argv for x in (
        "main_database.py",
        "push_receiver_runner.py",
        "summary_database_runner.py",
    ))


def _safe_latest_dt(df: Any) -> Optional[pd.Timestamp]:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        for col in ("datetime", "end_time", "start_time", "received_at", "time"):
            if col not in df.columns:
                continue
            s = pd.to_datetime(df[col], errors="coerce")
            if s.notna().any():
                ts = s.max()
                try:
                    ts = ts.tz_localize(None)
                except Exception:
                    pass
                return ts
    except Exception:
        pass
    return None


def _safe_symbol_count(df: Any) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return 0
        return int(df["symbol"].astype(str).nunique())
    except Exception:
        return 0


def _build_summary(interval: int) -> pd.DataFrame:
    try:
        from scheduler_jobs.summary.runner_core import job_summary
        n = dt.datetime.now().replace(second=0, microsecond=0)
        df = job_summary(int(interval), display=False, now=n, run_entry=False)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    except Exception:
        logger.exception("[PUSH SUMMARY REALTIME] runner_core job_summary failed interval=%s", interval)
        return pd.DataFrame()


def _summary_rebuild_worker(intervals: list[int], reason: str) -> None:
    global _TRIGGER_RUNNING, _LAST_TRIGGER_AT
    try:
        for interval in intervals:
            try:
                logger.warning("[PUSH SUMMARY REALTIME] rebuild start interval=%s reason=%s", interval, reason)
                df = _build_summary(int(interval))
                logger.warning(
                    "[PUSH SUMMARY REALTIME] rebuild done interval=%s rows=%s symbols=%s latest_dt=%s reason=%s",
                    interval,
                    len(df) if isinstance(df, pd.DataFrame) else 0,
                    _safe_symbol_count(df),
                    _safe_latest_dt(df),
                    reason,
                )
            except Exception:
                logger.exception("[PUSH SUMMARY REALTIME] rebuild failed interval=%s reason=%s", interval, reason)
    finally:
        with _TRIGGER_LOCK:
            _TRIGGER_RUNNING = False
            _LAST_TRIGGER_AT = time.time()


def _trigger_summary_rebuild(reason: str, *, ignore_cooldown: bool = False, intervals_override: list[int] | None = None) -> None:
    global _TRIGGER_RUNNING, _LAST_TRIGGER_AT
    if not _env_bool("PUSH_SUMMARY_REALTIME_REBUILD_ENABLED", True):
        return
    cooldown = _env_float("PUSH_SUMMARY_REALTIME_COOLDOWN_SEC", 20.0)
    now = time.time()
    with _TRIGGER_LOCK:
        if _TRIGGER_RUNNING:
            logger.debug("[PUSH SUMMARY REALTIME] rebuild skipped already running reason=%s", reason)
            return
        if not ignore_cooldown and now - float(_LAST_TRIGGER_AT or 0.0) < cooldown:
            logger.debug("[PUSH SUMMARY REALTIME] rebuild skipped cooldown reason=%s", reason)
            return
        _TRIGGER_RUNNING = True

    intervals = list(intervals_override or _parse_intervals(os.getenv("PUSH_SUMMARY_REALTIME_INTERVALS"), default=_default_intervals()))
    logger.warning("[PUSH SUMMARY REALTIME] rebuild queued intervals=%s reason=%s", intervals, reason)
    th = threading.Thread(target=_summary_rebuild_worker, args=(intervals, reason), daemon=True, name="PushSummaryRealtimeRebuild")
    th.start()


def _patch_push_db_writer() -> bool:
    global _ORIGINAL_STREAM_WRITER_FLUSH
    try:
        import trading.push.push_db_writer as writer_mod
        cls = getattr(writer_mod, "StreamDBWriter", None)
        original = getattr(cls, "flush", None) if cls is not None else None
        if not callable(original):
            logger.warning("[PUSH SUMMARY REALTIME] StreamDBWriter.flush not found")
            return False
        if getattr(original, "_push_summary_realtime_patched_v5", False):
            return True
        _ORIGINAL_STREAM_WRITER_FLUSH = original

        def _flush_with_realtime_summary(self, *args, **kwargs):
            ok = original(self, *args, **kwargs)
            try:
                if bool(ok):
                    delta = int(getattr(writer_mod.global_data, "last_flush_delta", 0) or 0)
                    rows = int(getattr(writer_mod.global_data, "last_flush_rows", 0) or 0)
                    if delta > 0 or rows > 0:
                        _trigger_summary_rebuild(reason=f"push_flush rows={rows} delta={delta}")
                    else:
                        logger.debug("[PUSH SUMMARY REALTIME] flush ok but no row delta rows=%s delta=%s", rows, delta)
            except Exception:
                logger.debug("[PUSH SUMMARY REALTIME] flush post-trigger failed", exc_info=True)
            return ok

        _flush_with_realtime_summary._push_summary_realtime_patched_v5 = True  # type: ignore[attr-defined]
        _flush_with_realtime_summary._push_summary_realtime_patched_v4 = True  # type: ignore[attr-defined]
        _flush_with_realtime_summary._push_summary_realtime_patched_v3 = True  # type: ignore[attr-defined]
        setattr(cls, "flush", _flush_with_realtime_summary)
        return True
    except Exception:
        logger.exception("[PUSH SUMMARY REALTIME] patch push_db_writer failed")
        return False


def _ensure_stream_writer_singleton_started() -> Any:
    """PUSH保存用 singleton を開始して返す。失敗時は None。"""
    try:
        _force_push_writer_env_if_database_process()
        import trading.push.push_db_writer as writer_mod
        writer = getattr(writer_mod, "stream_writer", None)
        if writer is None:
            cls = getattr(writer_mod, "StreamDBWriter", None)
            writer = cls(enable_raw_save=True) if callable(cls) else None
            if writer is not None:
                setattr(writer_mod, "stream_writer", writer)
        if writer is not None:
            try:
                start = getattr(writer, "start", None)
                if callable(start):
                    start()
            except Exception:
                logger.debug("[PUSH SUMMARY REALTIME] stream_writer.start skipped/failed", exc_info=True)
        return writer
    except Exception:
        logger.exception("[PUSH SUMMARY REALTIME] ensure stream_writer singleton failed")
        return None


def _patch_push_stream_start_force_writer() -> bool:
    """push_receiver/main_database側の start_push_stream に writer を明示注入する。"""
    global _ORIGINAL_PUSH_START, _ORIGINAL_RUNNER_PUSH_START
    if not _is_database_or_push_receiver_process():
        return True

    try:
        import trading.push.push_stream.runner as runner_mod
        import trading.push.push_stream as pkg_mod

        original = getattr(runner_mod, "start_push_stream", None)
        if not callable(original):
            logger.warning("[PUSH SUMMARY REALTIME] runner.start_push_stream not found")
            return False
        if getattr(original, "_push_writer_force_patched_v5", False):
            return True

        _ORIGINAL_RUNNER_PUSH_START = original
        _ORIGINAL_PUSH_START = getattr(pkg_mod, "_runner_start_push_stream", None)

        def _start_push_stream_with_forced_writer(*args, **kwargs):
            _force_push_writer_env_if_database_process()
            if kwargs.get("stream_writer", None) is None:
                writer = _ensure_stream_writer_singleton_started()
                if writer is not None:
                    kwargs["stream_writer"] = writer
            # data_collector側では False を明示されても保存を優先する。
            if kwargs.get("stream_writer") is False and _is_database_or_push_receiver_process():
                writer = _ensure_stream_writer_singleton_started()
                if writer is not None:
                    kwargs["stream_writer"] = writer
            logger.warning(
                "[PUSH SUMMARY REALTIME] start_push_stream forced writer process_db=%s writer_injected=%s db_write_env=%s argv=%s",
                _is_database_or_push_receiver_process(),
                kwargs.get("stream_writer") is not None and kwargs.get("stream_writer") is not False,
                os.getenv("PUSH_STREAM_DB_WRITE"),
                sys.argv,
            )
            return original(*args, **kwargs)

        _start_push_stream_with_forced_writer._push_writer_force_patched_v5 = True  # type: ignore[attr-defined]
        setattr(runner_mod, "start_push_stream", _start_push_stream_with_forced_writer)
        # package __init__.py は runner.start_push_stream を _runner_start_push_stream に束縛しているため、ここも差し替える。
        try:
            setattr(pkg_mod, "_runner_start_push_stream", _start_push_stream_with_forced_writer)
        except Exception:
            pass
        return True
    except Exception:
        logger.exception("[PUSH SUMMARY REALTIME] patch push_stream start force writer failed")
        return False


def _patch_runtime_token_refresh_suppression() -> bool:
    """401時は runtime refresh を呼ばず、既存/settings token 再利用またはそのcycle skipに統一する。"""
    ok = True
    try:
        import force_cancel_loop as fcl

        def _no_refresh_headers_after_401(context: str):
            logger.warning("[FORCE_CANCEL] 401 received; runtime refresh disabled context=%s -> reuse settings/runtime token only", context)
            try:
                return fcl._direct_token_fallback(context)
            except Exception:
                logger.debug("[FORCE_CANCEL] direct token fallback failed after 401 context=%s", context, exc_info=True)
                return None

        setattr(fcl, "_refresh_headers_after_401", _no_refresh_headers_after_401)
    except Exception:
        ok = False
        logger.debug("[PUSH SUMMARY REALTIME] force_cancel_loop token refresh suppression skipped", exc_info=True)

    try:
        import trading.position.kabu_position_reader as kpr

        def _no_refresh_token_after_401() -> str:
            logger.warning("[KABU POSITION READER] 401 received; runtime refresh disabled -> skip this cycle")
            return ""

        setattr(kpr, "_refresh_token_after_401", _no_refresh_token_after_401)
    except Exception:
        ok = False
        logger.debug("[PUSH SUMMARY REALTIME] kabu_position_reader token refresh suppression skipped", exc_info=True)

    try:
        import kabu_api.positions as pos

        # このモジュールは通常refreshしないが、401時に長いtracebackを出さないよう requests.HTTPError を明示処理。
        original_get_positions = getattr(pos, "get_positions", None)
        if callable(original_get_positions) and not getattr(original_get_positions, "_no_runtime_refresh_guard_v5", False):
            def _get_positions_no_401_traceback(*args, **kwargs):
                try:
                    return original_get_positions(*args, **kwargs)
                except Exception as e:
                    logger.warning("[kabu_api.positions] positions read skipped err=%s", e)
                    try:
                        return getattr(pos, "_POS_CACHE", None) or []
                    except Exception:
                        return []

            _get_positions_no_401_traceback._no_runtime_refresh_guard_v5 = True  # type: ignore[attr-defined]
            setattr(pos, "get_positions", _get_positions_no_401_traceback)
    except Exception:
        ok = False
        logger.debug("[PUSH SUMMARY REALTIME] kabu_api.positions guard skipped", exc_info=True)

    return ok


def _schedule_bootstrap_rebuild() -> None:
    if not _should_bootstrap_rebuild_here():
        return
    delay = _env_float("PUSH_SUMMARY_REALTIME_BOOTSTRAP_DELAY_SEC", 3.0)
    intervals = _bootstrap_intervals()

    def _delayed_bootstrap() -> None:
        try:
            if delay > 0:
                time.sleep(delay)
            _trigger_summary_rebuild(reason="install_bootstrap", ignore_cooldown=True, intervals_override=intervals)
        except Exception:
            logger.debug("[PUSH SUMMARY REALTIME] bootstrap trigger failed", exc_info=True)

    logger.warning("[PUSH SUMMARY REALTIME] bootstrap scheduled intervals=%s delay=%.1fs", intervals, delay)
    threading.Thread(target=_delayed_bootstrap, daemon=True, name="PushSummaryRealtimeBootstrap").start()


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    if not _env_bool("PUSH_SUMMARY_REALTIME_PATCH_ENABLED", True):
        logger.warning("[PUSH SUMMARY REALTIME] disabled by env")
        return False

    _force_push_writer_env_if_database_process()

    try:
        from core.startup import push_summary_realtime_callable_fix_patch
        push_summary_realtime_callable_fix_patch.install()
    except Exception:
        logger.debug("[PUSH SUMMARY REALTIME] callable fix optional install failed", exc_info=True)

    ok_writer = _patch_push_db_writer()
    ok_start = _patch_push_stream_start_force_writer()
    ok_token = _patch_runtime_token_refresh_suppression()
    if _is_database_or_push_receiver_process():
        _ensure_stream_writer_singleton_started()

    _PATCHED = bool(ok_writer and ok_start)
    intervals = _parse_intervals(os.getenv("PUSH_SUMMARY_REALTIME_INTERVALS"), default=_default_intervals())
    bootstrap = _should_bootstrap_rebuild_here()
    logger.warning(
        "[PUSH SUMMARY REALTIME] installed ok=%s writer_patch=%s start_patch=%s token_suppression=%s intervals=%s bootstrap=%s bootstrap_intervals=%s version=REV5 argv=%s",
        _PATCHED,
        ok_writer,
        ok_start,
        ok_token,
        intervals,
        bootstrap,
        _bootstrap_intervals(),
        sys.argv,
    )
    if _PATCHED and bootstrap:
        _schedule_bootstrap_rebuild()
    return _PATCHED


__all__ = ["install"]
