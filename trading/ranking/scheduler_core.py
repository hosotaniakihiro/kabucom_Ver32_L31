# ============================================================
# File   : trading/ranking/scheduler_core.py
# Version: Ver1.6-RANKING-SCHEDULER-CORE-PUSH-STYLE-WRITER
# ------------------------------------------------------------
# ✔ ranking scheduler 本体
# ✔ PUSH DB WRITER と同じ buffer writer 方式に対応
# ✔ collect -> writer queue -> return
# ✔ 毎分jobがDB書き込み待ちで詰まらない
# ✔ FAST/FULL モード分離
#   - fast: writerへ raw/snapshot をqueueしてすぐ戻る
#   - full: legacy保存もwriterへqueueし、必要なら重い後処理を実行
# ✔ 同一分ガード
# ✔ 市場時間判定
# ✔ ranking snapshot 取得時に当日銘柄を global_data へ共有保存
# ✔ trading.ranking.runtime_symbols と連携
# ✔ 既存 direct 保存への fallback も保持
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any, Optional

from trading.ranking.ranking_snapshot_manager import RankingSnapshotManager

from .normalizers import floor_to_minute
from .runtime_state import (
    ensure_global_defaults,
    get_global_data,
    update_runtime_state,
    save_snapshot_to_global,
    get_runtime_symbol_selector_status_safe,
)
from .collectors import collect_ranking_rows
from .persistence import save_raw_rows, save_snapshot_and_build
from .postprocess import (
    update_ranking_summary_cache,
    build_ranking_ma,
    process_entry_candidates,
    run_followup_pipelines,
    run_closed_day_reuse_mode,
)
from trading.ranking.ranking_summary_engine import get_ranking_summary_status
from database.crud.crud_ranking_snapshot import insert_ranking_snapshot_1min

try:
    from trading.ranking.ranking_db_writer import (
        add_ranking_rows_async,
        ensure_ranking_writer_started,
    )
    _HAS_RANKING_WRITER = True
except Exception:
    add_ranking_rows_async = None  # type: ignore
    ensure_ranking_writer_started = None  # type: ignore
    _HAS_RANKING_WRITER = False


# ------------------------------------------------------------
# ranking runtime shared symbols
# ------------------------------------------------------------

try:
    from trading.ranking.runtime_symbols import (
        add_ranking_symbols,
        add_filtered_ranking_symbols,
        log_ranking_symbol_cache_snapshot,
    )
    _HAS_RUNTIME_SYMBOLS = True
except Exception:
    _HAS_RUNTIME_SYMBOLS = False

    def add_ranking_symbols(*args, **kwargs):
        return set()

    def add_filtered_ranking_symbols(*args, **kwargs):
        return set()

    def log_ranking_symbol_cache_snapshot(*args, **kwargs):
        return None


logger = logging.getLogger(__name__)

ensure_global_defaults()
global_data = get_global_data()

_job_lock = threading.Lock()
_is_running = False
FORCE_RANKING_SAVE = False

_last_job_started_at: float = 0.0
_last_job_finished_at: float = 0.0
_last_job_duration_sec: float = 0.0

MIN_JOB_INTERVAL_SEC = 2.0

_last_completed_minute: Optional[dt.datetime] = None
_last_started_minute: Optional[dt.datetime] = None

_snapshot_mgr = RankingSnapshotManager(maxlen=5)


# ============================================================
# basic helpers
# ============================================================

def now_in_market_hours(now_t: dt.time) -> bool:
    return (
        dt.time(9, 0) <= now_t <= dt.time(11, 30)
        or dt.time(12, 30) <= now_t <= dt.time(15, 30)
    )


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y"}


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v or 0)
    except Exception:
        return int(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v or 0.0)
    except Exception:
        return float(default)


def _safe_set_global_attr(name: str, value: Any) -> None:
    try:
        setattr(global_data, name, value)
    except Exception:
        logger.debug(
            "[RANKING JOB] set global attr failed name=%s",
            name,
            exc_info=True,
        )


def _normalize_mode(mode: Optional[str]) -> str:
    m = str(mode or "fast").lower().strip()
    if m not in {"fast", "full"}:
        return "fast"
    return m


def _build_result(
    *,
    ok: bool,
    mode: str,
    started_minute: dt.datetime,
    raw_rows: int = 0,
    snapshot_rows: int = 0,
    saved_raw_rows: int = 0,
    saved_snapshot_rows: int = 0,
    queued_raw_rows: int = 0,
    queued_snapshot_rows: int = 0,
    queued_legacy_rows: int = 0,
    duration_sec: float = 0.0,
    skip_reason: Optional[str] = None,
    error: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "mode": mode,
        "minute": started_minute,
        "raw_rows": _safe_int(raw_rows),
        "snapshot_rows": _safe_int(snapshot_rows),
        "saved_raw_rows": _safe_int(saved_raw_rows),
        "saved_snapshot_rows": _safe_int(saved_snapshot_rows),
        "queued_raw_rows": _safe_int(queued_raw_rows),
        "queued_snapshot_rows": _safe_int(queued_snapshot_rows),
        "queued_legacy_rows": _safe_int(queued_legacy_rows),
        "duration_sec": _safe_float(duration_sec),
        "skip_reason": skip_reason,
        "error": error,
    }


# ============================================================
# runtime symbols
# ============================================================

def _extract_snapshot_symbols(snapshot_rows: list[dict]) -> list[str]:
    """
    snapshot_rows から symbol を順序保持で抽出する。
    """
    out: list[str] = []
    seen: set[str] = set()

    try:
        for row in snapshot_rows or []:
            if not isinstance(row, dict):
                continue

            symbol = row.get("symbol")
            if symbol is None:
                continue

            s = str(symbol).strip()
            if not s:
                continue

            if s.endswith(".0"):
                s = s[:-2]

            if not s or s in seen:
                continue

            seen.add(s)
            out.append(s)

    except Exception:
        logger.exception("[RANKING SNAPSHOT] extract symbols failed")

    return out


def _update_runtime_symbols_from_snapshot(
    snapshot_rows: list[dict],
    *,
    target_date: dt.date,
) -> None:
    """
    ranking snapshot 取得結果を trading.ranking.runtime_symbols 経由で
    global_data に共有保存する。
    """
    if not _HAS_RUNTIME_SYMBOLS:
        logger.debug("[RANKING SNAPSHOT] runtime_symbols unavailable -> skip")
        return

    try:
        snapshot_symbols = _extract_snapshot_symbols(snapshot_rows)

        if not snapshot_symbols:
            logger.info("[RANKING SNAPSHOT] no symbols to update runtime cache")
            return

        add_ranking_symbols(
            snapshot_symbols,
            filtered=False,
            target_date=target_date,
        )

        add_filtered_ranking_symbols(
            snapshot_symbols,
            target_date=target_date,
        )

        try:
            global_data.ranking_today_symbols_last_source = "ranking_snapshot_1min"
            global_data.ranking_today_symbols_last_count = len(snapshot_symbols)
            global_data.ranking_today_symbols_last_updated_minute = dt.datetime.combine(
                target_date,
                dt.time.min,
            )
        except Exception:
            logger.debug(
                "[RANKING SNAPSHOT] save extra global attrs failed",
                exc_info=True,
            )

        log_ranking_symbol_cache_snapshot("[RANKING SNAPSHOT -> GLOBAL]")

        logger.info(
            "[RANKING SNAPSHOT] runtime symbols updated symbols=%d date=%s",
            len(snapshot_symbols),
            target_date,
        )

    except Exception:
        logger.exception("[RANKING SNAPSHOT] runtime symbol update failed")


# ============================================================
# save helpers
# ============================================================

def _queue_to_ranking_writer(
    *,
    raw_rows: list[dict],
    snapshot_rows: list[dict],
    started_minute: dt.datetime,
    save_legacy: bool,
    mode: str,
) -> dict[str, Any]:
    """
    PUSH DB WRITER と同じ方式:
      scheduler側ではDB保存せず、writer bufferへ積むだけ。
    """
    if not _HAS_RANKING_WRITER or add_ranking_rows_async is None:
        raise RuntimeError("ranking_db_writer unavailable")

    if ensure_ranking_writer_started is not None:
        try:
            ensure_ranking_writer_started()
        except Exception:
            logger.debug("[RANKING WRITER] ensure start failed", exc_info=True)

    ret = add_ranking_rows_async(
        raw_rows=raw_rows,
        snapshot_rows=snapshot_rows,
        save_legacy=save_legacy,
        now_dt=started_minute,
        source=f"scheduler_core:{mode}",
    )

    logger.info(
        "[RANKING WRITER] queued mode=%s minute=%s raw=%s snapshot=%s legacy=%s ret=%s",
        mode,
        started_minute,
        len(raw_rows or []),
        len(snapshot_rows or []),
        save_legacy,
        ret,
    )

    return ret if isinstance(ret, dict) else {"ok": True}


def _save_direct_fallback(
    *,
    raw_rows: list[dict],
    snapshot_rows: list[dict],
    started_minute: dt.datetime,
    save_legacy: bool,
    mode: str,
) -> tuple[int, int]:
    """
    writerが使えない場合の直接保存fallback。
    """
    saved_raw = 0
    saved_snapshot = 0

    if raw_rows:
        try:
            try:
                saved_raw = int(
                    save_raw_rows(
                        raw_rows,
                        now_dt=started_minute,
                        save_legacy=save_legacy,
                    ) or 0
                )
            except TypeError:
                saved_raw = int(save_raw_rows(raw_rows, now_dt=started_minute) or 0)
        except Exception:
            logger.exception("[RANKING RAW] direct fallback insert failed")

    if snapshot_rows:
        try:
            if mode == "fast":
                saved_snapshot = int(insert_ranking_snapshot_1min(snapshot_rows) or 0)
                logger.info(
                    "[RANKING FAST] snapshot saved directly fallback rows=%d minute=%s",
                    saved_snapshot,
                    started_minute,
                )
            else:
                saved_snapshot = int(save_snapshot_and_build(snapshot_rows, now_dt=started_minute) or 0)
                logger.info(
                    "[RANKING FULL] snapshot saved via build fallback rows=%d minute=%s",
                    saved_snapshot,
                    started_minute,
                )
        except Exception:
            logger.exception("[RANKING SNAPSHOT] direct fallback DB insert/build failed")

    return saved_raw, saved_snapshot


# ============================================================
# main job
# ============================================================

def job_save_ranking(
    mode: str = "fast",
    run_full_postprocess: Optional[bool] = None,
    save_legacy: bool = False,
    force: bool = False,
) -> dict[str, Any] | None:
    """
    ランキング保存ジョブ。

    mode="fast":
      collect -> writer queue -> runtime symbol update -> return

    mode="full":
      collect -> writer queue(save_legacy=True) -> optional postprocess
    """
    global _is_running
    global _last_job_started_at
    global _last_job_finished_at
    global _last_job_duration_sec
    global _last_completed_minute
    global _last_started_minute

    now_ts = time.time()
    started_wallclock = dt.datetime.now()
    started_minute = floor_to_minute(started_wallclock)

    mode = _normalize_mode(mode)

    if run_full_postprocess is None:
        run_full_postprocess = mode == "full"

    save_legacy = bool(
        save_legacy
        or mode == "full"
        or _env_bool("RANKING_SAVE_LEGACY_EVERY_MINUTE", False)
    )

    if not _job_lock.acquire(blocking=False):
        logger.info("⏭ [RANKING] skipped (job lock busy)")
        update_runtime_state(
            last_skip_reason="job_lock_busy",
            is_running=_is_running,
            last_attempt_at=started_wallclock,
        )
        return _build_result(
            ok=False,
            mode=mode,
            started_minute=started_minute,
            skip_reason="job_lock_busy",
        )

    try:
        if _is_running:
            logger.info("⏭ [RANKING] skipped (_is_running=True)")
            update_runtime_state(
                last_skip_reason="already_running_flag",
                is_running=True,
                last_attempt_at=started_wallclock,
            )
            return _build_result(
                ok=False,
                mode=mode,
                started_minute=started_minute,
                skip_reason="already_running_flag",
            )

        if _last_job_started_at > 0 and (now_ts - _last_job_started_at) < MIN_JOB_INTERVAL_SEC:
            logger.info(
                "⏭ [RANKING] skipped (too frequent %.2fs < %.2fs)",
                now_ts - _last_job_started_at,
                MIN_JOB_INTERVAL_SEC,
            )
            update_runtime_state(
                last_skip_reason="min_interval_guard",
                is_running=False,
                last_attempt_at=started_wallclock,
            )
            return _build_result(
                ok=False,
                mode=mode,
                started_minute=started_minute,
                skip_reason="min_interval_guard",
            )

        if not force and _last_started_minute is not None and _last_started_minute == started_minute:
            logger.info("⏭ [RANKING] skipped same started minute=%s", started_minute)
            update_runtime_state(
                last_skip_reason="same_started_minute",
                is_running=False,
                last_attempt_at=started_wallclock,
            )
            return _build_result(
                ok=False,
                mode=mode,
                started_minute=started_minute,
                skip_reason="same_started_minute",
            )

        if not force and _last_completed_minute is not None and _last_completed_minute == started_minute:
            logger.info("⏭ [RANKING] skipped same completed minute=%s", started_minute)
            update_runtime_state(
                last_skip_reason="same_completed_minute",
                is_running=False,
                last_attempt_at=started_wallclock,
            )
            return _build_result(
                ok=False,
                mode=mode,
                started_minute=started_minute,
                skip_reason="same_completed_minute",
            )

        _is_running = True
        _last_job_started_at = now_ts
        _last_started_minute = started_minute
        _safe_set_global_attr("ranking_scheduler_running", True)

        now_t = started_wallclock.time()

        logger.info(
            "🚀 [RANKING] start mode=%s full_postprocess=%s save_legacy=%s force=%s writer=%s at=%s minute=%s",
            mode,
            run_full_postprocess,
            save_legacy,
            force,
            _HAS_RANKING_WRITER,
            started_wallclock,
            started_minute,
        )

        update_runtime_state(
            is_running=True,
            started_at=started_wallclock,
            started_minute=started_minute,
            finished_at=None,
            last_skip_reason=None,
        )

        raw_rows: list[dict] = []
        snapshot_rows: list[dict] = []

        if not force and not FORCE_RANKING_SAVE and not now_in_market_hours(now_t):
            logger.info("⏸ [RANKING] out of market hours -> closed-day reuse mode")
            raw_cnt, snapshot_cnt = run_closed_day_reuse_mode(started_minute)

            duration = time.time() - now_ts

            try:
                global_data.ranking_last_job_status = {
                    "ok": True,
                    "mode": "closed_day_reuse",
                    "requested_mode": mode,
                    "started_at": started_wallclock,
                    "started_minute": started_minute,
                    "raw_rows": raw_cnt,
                    "snapshot_rows": snapshot_cnt,
                    "duration_sec": duration,
                    "ranking_summary_status": get_ranking_summary_status(),
                    "runtime_symbol_selector_status": get_runtime_symbol_selector_status_safe(),
                }
            except Exception:
                logger.exception("[RANKING JOB] save closed-day last_job_status failed")

            _last_completed_minute = started_minute

            logger.info(
                "🏁 [RANKING] closed-day done raw=%d snapshot=%d duration=%.3fs",
                raw_cnt,
                snapshot_cnt,
                duration,
            )

            return _build_result(
                ok=True,
                mode="closed_day_reuse",
                started_minute=started_minute,
                raw_rows=raw_cnt,
                snapshot_rows=snapshot_cnt,
                saved_raw_rows=raw_cnt,
                saved_snapshot_rows=snapshot_cnt,
                duration_sec=duration,
            )

        try:
            raw_rows, snapshot_rows = collect_ranking_rows(started_minute, _snapshot_mgr)
            logger.info(
                "[RANKING] collected raw=%s snapshot=%s minute=%s",
                len(raw_rows),
                len(snapshot_rows),
                started_minute,
            )
        except Exception:
            logger.exception("[RANKING] collect phase failed")
            raise

        saved_raw = 0
        saved_snapshot = 0
        queued_raw = 0
        queued_snapshot = 0
        queued_legacy = 0

        try:
            if _HAS_RANKING_WRITER:
                qret = _queue_to_ranking_writer(
                    raw_rows=raw_rows,
                    snapshot_rows=snapshot_rows,
                    started_minute=started_minute,
                    save_legacy=save_legacy,
                    mode=mode,
                )
                queued_raw = int(qret.get("queued_raw", len(raw_rows)) or 0)
                queued_snapshot = int(qret.get("queued_snapshot", len(snapshot_rows)) or 0)
                queued_legacy = int(qret.get("queued_legacy", len(raw_rows) if save_legacy else 0) or 0)
                saved_raw = 0
                saved_snapshot = 0
            else:
                saved_raw, saved_snapshot = _save_direct_fallback(
                    raw_rows=raw_rows,
                    snapshot_rows=snapshot_rows,
                    started_minute=started_minute,
                    save_legacy=save_legacy,
                    mode=mode,
                )

        except Exception:
            logger.exception("[RANKING WRITER] queue failed -> direct fallback")
            saved_raw, saved_snapshot = _save_direct_fallback(
                raw_rows=raw_rows,
                snapshot_rows=snapshot_rows,
                started_minute=started_minute,
                save_legacy=save_legacy,
                mode=mode,
            )

        if snapshot_rows:
            try:
                save_snapshot_to_global(snapshot_rows)
            except Exception:
                logger.exception("[RANKING SNAPSHOT] save_snapshot_to_global failed")

            try:
                _update_runtime_symbols_from_snapshot(
                    snapshot_rows,
                    target_date=started_minute.date(),
                )
            except Exception:
                logger.exception("[RANKING SNAPSHOT] runtime symbol sync failed")

            if run_full_postprocess:
                try:
                    update_ranking_summary_cache(snapshot_rows)
                except Exception:
                    logger.exception("[RANKING SUMMARY] post snapshot update failed")
            else:
                logger.info(
                    "[RANKING FAST] skipped update_ranking_summary_cache minute=%s",
                    started_minute,
                )
        else:
            logger.warning("[RANKING SNAPSHOT] no snapshot rows collected")

        if run_full_postprocess:
            try:
                build_ranking_ma(snapshot_rows, started_minute)
            except Exception:
                logger.exception("[RANKING FULL] build_ranking_ma failed")

            try:
                process_entry_candidates(snapshot_rows)
            except Exception:
                logger.exception("[RANKING FULL] process_entry_candidates failed")

            try:
                run_followup_pipelines()
            except Exception:
                logger.exception("[RANKING FULL] run_followup_pipelines failed")
        else:
            logger.info(
                "[RANKING FAST] skipped heavy postprocess build_ma/entry_candidates/followup minute=%s",
                started_minute,
            )

        duration = time.time() - now_ts

        try:
            global_data.ranking_last_saved_minute = started_minute
            global_data.ranking_last_raw_rows = saved_raw
            global_data.ranking_last_snapshot_rows = saved_snapshot
            global_data.ranking_last_queued_raw_rows = queued_raw
            global_data.ranking_last_queued_snapshot_rows = queued_snapshot
            global_data.ranking_last_queued_legacy_rows = queued_legacy
            global_data.ranking_last_job_status = {
                "ok": True,
                "mode": "market_live_fast" if mode == "fast" else "market_live_full",
                "requested_mode": mode,
                "writer_enabled": bool(_HAS_RANKING_WRITER),
                "run_full_postprocess": bool(run_full_postprocess),
                "save_legacy": bool(save_legacy),
                "started_at": started_wallclock,
                "started_minute": started_minute,
                "raw_rows": len(raw_rows),
                "snapshot_rows": len(snapshot_rows),
                "saved_raw_rows": saved_raw,
                "saved_snapshot_rows": saved_snapshot,
                "queued_raw_rows": queued_raw,
                "queued_snapshot_rows": queued_snapshot,
                "queued_legacy_rows": queued_legacy,
                "duration_sec": duration,
                "ranking_summary_status": get_ranking_summary_status(),
                "runtime_symbol_selector_status": get_runtime_symbol_selector_status_safe(),
            }
        except Exception:
            logger.exception("[RANKING JOB] save last_job_status failed")

        _last_completed_minute = started_minute

        logger.info(
            "🏁 [RANKING] done mode=%s minute=%s raw=%d snapshot=%d queued_raw=%d queued_snapshot=%d queued_legacy=%d saved_raw=%d saved_snapshot=%d duration=%.3fs",
            mode,
            started_minute,
            len(raw_rows),
            len(snapshot_rows),
            queued_raw,
            queued_snapshot,
            queued_legacy,
            saved_raw,
            saved_snapshot,
            duration,
        )

        return _build_result(
            ok=True,
            mode=mode,
            started_minute=started_minute,
            raw_rows=len(raw_rows),
            snapshot_rows=len(snapshot_rows),
            saved_raw_rows=saved_raw,
            saved_snapshot_rows=saved_snapshot,
            queued_raw_rows=queued_raw,
            queued_snapshot_rows=queued_snapshot,
            queued_legacy_rows=queued_legacy,
            duration_sec=duration,
        )

    except Exception as e:
        duration = time.time() - now_ts

        logger.exception("[RANKING JOB] fatal error")

        try:
            global_data.ranking_last_job_status = {
                "ok": False,
                "mode": mode,
                "finished_at": dt.datetime.now(),
                "started_minute": started_minute,
                "duration_sec": duration,
                "error": str(e),
            }
        except Exception:
            pass

        return _build_result(
            ok=False,
            mode=mode,
            started_minute=started_minute,
            duration_sec=duration,
            error=str(e),
        )

    finally:
        _last_job_finished_at = time.time()
        _last_job_duration_sec = max(_last_job_finished_at - _last_job_started_at, 0.0)

        update_runtime_state(
            is_running=False,
            finished_at=dt.datetime.now(),
            last_duration_sec=_last_job_duration_sec,
            last_started_at_epoch=_last_job_started_at,
            last_finished_at_epoch=_last_job_finished_at,
            last_completed_minute=_last_completed_minute,
        )

        try:
            global_data.ranking_scheduler_running = False
        except Exception:
            pass

        _is_running = False

        try:
            _job_lock.release()
        except Exception:
            logger.exception("[RANKING JOB] job_lock release failed")


# ============================================================
# compatibility wrappers
# ============================================================

def save_ranking_data_loop(
    mode: str = "fast",
    run_full_postprocess: Optional[bool] = None,
    save_legacy: bool = False,
    force: bool = False,
) -> dict[str, Any] | None:
    logger.warning("🔥 RANKING LOOP EXECUTED mode=%s", mode)

    return job_save_ranking(
        mode=mode,
        run_full_postprocess=run_full_postprocess,
        save_legacy=save_legacy,
        force=force,
    )


def force_save_ranking_full_once() -> dict[str, Any] | None:
    return job_save_ranking(
        mode="full",
        run_full_postprocess=True,
        save_legacy=True,
        force=True,
    )


def force_save_ranking_fast_once() -> dict[str, Any] | None:
    return job_save_ranking(
        mode="fast",
        run_full_postprocess=False,
        save_legacy=False,
        force=True,
    )


__all__ = [
    "job_save_ranking",
    "save_ranking_data_loop",
    "force_save_ranking_full_once",
    "force_save_ranking_fast_once",
    "now_in_market_hours",
]
