# ============================================================
# File   : core/startup/summary_ai_async_entry_patch.py
# Version: Ver02-SUMMARY-AI-ASYNC-ENTRY-QUEUE
# ------------------------------------------------------------
# SUMMARY AI の実発注部分を、1分サマリー本体から切り離す runtime patch。
#
# Ver02 重要修正:
#   - 前回実発注中でも async_entry_busy で候補を捨てない
#   - approved_rows をキューに積み、1本の worker が順番に処理する
#   - 並列サマリー化後、1m/3m/5m の候補が同時に来ても取りこぼさない
#
# ENV:
#   SUMMARY_AI_ASYNC_ENTRY=1
#   SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY=0      # Ver02 default: 捨てない
#   SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX=20
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_EXECUTE = None
_ASYNC_LOCK = threading.Lock()
_QUEUE: deque[dict[str, Any]] = deque()
_WORKER_RUNNING = False
_SEQ = 0


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return max(1, int(float(v)))
    except Exception:
        return int(default)


def _safe_len(v: Any) -> int:
    try:
        return len(v) if v is not None else 0
    except Exception:
        return 0


def _symbols(rows: Any, limit: int = 30) -> list[str]:
    out: list[str] = []
    try:
        for r in list(rows or [])[:limit]:
            if isinstance(r, dict):
                out.append(str(r.get("symbol") or ""))
            else:
                out.append(str(getattr(r, "symbol", "")))
    except Exception:
        pass
    return [x for x in out if x]


def _summarize_result(result: Any) -> dict[str, Any]:
    try:
        if isinstance(result, dict):
            return {
                "executed": bool(result.get("executed")),
                "submitted_async": bool(result.get("submitted_async")),
                "skip_reason": result.get("skip_reason"),
                "approved": _safe_len(result.get("approved_rows")),
                "result_type": type(result.get("result")).__name__,
                "result": result.get("result"),
            }
        return {"result_type": type(result).__name__, "result": result}
    except Exception as e:
        return {"summary_error": str(e), "result_type": type(result).__name__}


def _run_worker_loop() -> None:
    global _WORKER_RUNNING
    while True:
        with _ASYNC_LOCK:
            if not _QUEUE:
                _WORKER_RUNNING = False
                logger.info("[SUMMARY AI ASYNC ENTRY] queue worker idle")
                return
            item = _QUEUE.popleft()
            q_left = len(_QUEUE)

        seq = item["seq"]
        interval = item["interval"]
        approved_rows = item["approved_rows"]
        started = time.time()
        try:
            logger.warning(
                "[SUMMARY AI ASYNC ENTRY] worker start seq=%s interval=%s approved=%s symbols=%s queue_left=%s",
                seq,
                interval,
                len(approved_rows),
                _symbols(approved_rows),
                q_left,
            )
            result = _ORIG_EXECUTE(
                item["ai_results"],
                df_summary=item["df_summary"],
                interval=interval,
                max_entries=item["max_entries"],
                dry_run=False,
                require_market_open=item["require_market_open"],
                entry_pipeline=item["entry_pipeline"],
            )
            logger.warning(
                "[SUMMARY AI ASYNC ENTRY] worker done seq=%s interval=%s elapsed=%.3fs summary=%s",
                seq,
                interval,
                time.time() - started,
                _summarize_result(result),
            )
        except Exception as e:
            logger.exception("[SUMMARY AI ASYNC ENTRY] worker failed seq=%s interval=%s err=%s", seq, interval, e)


def _ensure_worker_started() -> None:
    global _WORKER_RUNNING
    with _ASYNC_LOCK:
        if _WORKER_RUNNING:
            return
        _WORKER_RUNNING = True
        t = threading.Thread(
            target=_run_worker_loop,
            daemon=True,
            name="summary-ai-entry-async-queue-worker",
        )
        t.start()


def _patched_execute_ai_ok_entries_bulk(
    ai_results,
    *,
    df_summary,
    interval=1,
    max_entries=10,
    dry_run=True,
    require_market_open=True,
    entry_pipeline=None,
):
    """execute_ai_ok_entries_bulk の差し替え。approved_rows 作成後、実発注をキュー投入する。"""
    global _SEQ

    if not callable(_ORIG_EXECUTE):
        logger.warning("[SUMMARY AI ASYNC ENTRY] original executor missing -> no-op")
        return {
            "executed": False,
            "submitted_async": False,
            "dry_run": dry_run,
            "approved_rows": [],
            "result": None,
            "skip_reason": "original_executor_missing",
        }

    if dry_run or not _env_bool("SUMMARY_AI_ASYNC_ENTRY", True):
        return _ORIG_EXECUTE(
            ai_results,
            df_summary=df_summary,
            interval=interval,
            max_entries=max_entries,
            dry_run=dry_run,
            require_market_open=require_market_open,
            entry_pipeline=entry_pipeline,
        )

    try:
        from trading.entry.summary_ai import executor as exec_mod

        approved_rows = exec_mod.build_ai_ok_approved_rows(
            ai_results,
            max_entries=max_entries,
        )

        if not approved_rows:
            logger.info("[SUMMARY AI ASYNC ENTRY] no approved rows; skip async interval=%s", interval)
            return {
                "executed": False,
                "submitted_async": False,
                "dry_run": False,
                "approved_rows": [],
                "result": None,
                "skip_reason": "no_ai_ok",
            }

        if require_market_open:
            is_open = getattr(exec_mod, "is_market_open", None)
            if callable(is_open) and not bool(is_open()):
                logger.warning(
                    "[SUMMARY AI ASYNC ENTRY] market closed; async entry skipped approved=%s symbols=%s",
                    len(approved_rows),
                    _symbols(approved_rows),
                )
                return {
                    "executed": False,
                    "submitted_async": False,
                    "dry_run": False,
                    "approved_rows": approved_rows,
                    "result": None,
                    "skip_reason": "market_closed",
                }

        queue_max = _env_int("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", 20)
        drop_busy = _env_bool("SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY", False)

        with _ASYNC_LOCK:
            if len(_QUEUE) >= queue_max:
                if drop_busy:
                    logger.warning(
                        "[SUMMARY AI ASYNC ENTRY] queue full; drop submit interval=%s approved=%s symbols=%s queue=%s max=%s",
                        interval,
                        len(approved_rows),
                        _symbols(approved_rows),
                        len(_QUEUE),
                        queue_max,
                    )
                    return {
                        "executed": False,
                        "submitted_async": False,
                        "async_busy": True,
                        "queue_full": True,
                        "dry_run": False,
                        "approved_rows": approved_rows,
                        "result": None,
                        "skip_reason": "async_entry_queue_full",
                    }
                # 捨てない設定では古いものを1つ落として最新を残す。完全停止防止。
                dropped = _QUEUE.popleft()
                logger.warning(
                    "[SUMMARY AI ASYNC ENTRY] queue full; drop oldest seq=%s interval=%s to keep latest",
                    dropped.get("seq"),
                    dropped.get("interval"),
                )

            _SEQ += 1
            seq = _SEQ
            _QUEUE.append(
                {
                    "seq": seq,
                    "ai_results": ai_results,
                    "df_summary": df_summary,
                    "interval": interval,
                    "max_entries": max_entries,
                    "require_market_open": require_market_open,
                    "entry_pipeline": entry_pipeline,
                    "approved_rows": approved_rows,
                }
            )
            q_size = len(_QUEUE)

        _ensure_worker_started()

        logger.warning(
            "[SUMMARY AI ASYNC ENTRY] queued seq=%s interval=%s approved=%s symbols=%s queue_size=%s",
            seq,
            interval,
            len(approved_rows),
            _symbols(approved_rows),
            q_size,
        )

        return {
            "executed": True,
            "submitted_async": True,
            "queued_async": True,
            "async_seq": seq,
            "queue_size": q_size,
            "dry_run": False,
            "approved_rows": approved_rows,
            "result": {"status": "queued_async", "seq": seq, "queue_size": q_size},
            "skip_reason": "queued_async",
        }

    except Exception as e:
        logger.exception("[SUMMARY AI ASYNC ENTRY] submit failed err=%s -> fallback sync", e)
        return _ORIG_EXECUTE(
            ai_results,
            df_summary=df_summary,
            interval=interval,
            max_entries=max_entries,
            dry_run=False,
            require_market_open=require_market_open,
            entry_pipeline=entry_pipeline,
        )


def install() -> bool:
    global _INSTALLED, _ORIG_EXECUTE
    if _INSTALLED:
        return True

    try:
        from trading.entry.summary_ai import executor as exec_mod
        from trading.entry.summary_ai import runner as runner_mod

        current = getattr(exec_mod, "execute_ai_ok_entries_bulk", None)
        if getattr(current, "_summary_ai_async_entry_patch_v2", False):
            _INSTALLED = True
            logger.warning("[SUMMARY AI ASYNC ENTRY] already installed v2")
            return True

        _ORIG_EXECUTE = current
        _patched_execute_ai_ok_entries_bulk._summary_ai_async_entry_patch_v2 = True  # type: ignore[attr-defined]

        exec_mod.execute_ai_ok_entries_bulk = _patched_execute_ai_ok_entries_bulk
        runner_mod.execute_ai_ok_entries_bulk = _patched_execute_ai_ok_entries_bulk

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI ASYNC ENTRY] installed v2 enabled=%s drop_busy=%s queue_max=%s",
            _env_bool("SUMMARY_AI_ASYNC_ENTRY", True),
            _env_bool("SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY", False),
            _env_int("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", 20),
        )
        return True
    except Exception as e:
        logger.exception("[SUMMARY AI ASYNC ENTRY] install failed err=%s", e)
        return False


try:
    install()
except Exception as e:
    logger.exception("[SUMMARY AI ASYNC ENTRY] auto install failed err=%s", e)

__all__ = ["install"]