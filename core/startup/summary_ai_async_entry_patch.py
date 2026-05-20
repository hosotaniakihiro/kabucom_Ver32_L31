# ============================================================
# File   : core/startup/summary_ai_async_entry_patch.py
# Version: Ver01-SUMMARY-AI-ASYNC-ENTRY
# ------------------------------------------------------------
# SUMMARY AI の実発注部分を、1分サマリー本体から切り離す runtime patch。
#
# 目的:
#   - PUSH-1m summary job が90秒 timeout して entry まで戻らない問題を緩和
#   - AI_OK / approved_rows 作成後、実発注 pipeline は background thread で実行
#   - runner 側には submitted_async として即時返す
#   - 実発注の成否・skip_reason・skipped 内訳を background 側ログに必ず出す
#
# ENV:
#   SUMMARY_AI_ASYNC_ENTRY=1           # default ON
#   SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY=1 # 前回実発注中なら次回投入を抑止
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_EXECUTE = None
_ASYNC_LOCK = threading.Lock()
_INFLIGHT = False
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
    """
    trading.entry.summary_ai.executor.execute_ai_ok_entries_bulk の差し替え。

    dry_run時やENV無効時は元関数をそのまま実行。
    実発注時は approved_rows だけ同期的に作り、発注pipelineは別スレッドへ渡す。
    """
    global _INFLIGHT, _SEQ

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

        drop_busy = _env_bool("SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY", True)
        with _ASYNC_LOCK:
            if _INFLIGHT and drop_busy:
                logger.warning(
                    "[SUMMARY AI ASYNC ENTRY] previous async entry still running; skip new submit interval=%s approved=%s symbols=%s",
                    interval,
                    len(approved_rows),
                    _symbols(approved_rows),
                )
                return {
                    "executed": False,
                    "submitted_async": False,
                    "async_busy": True,
                    "dry_run": False,
                    "approved_rows": approved_rows,
                    "result": None,
                    "skip_reason": "async_entry_busy",
                }

            _INFLIGHT = True
            _SEQ += 1
            seq = _SEQ

        def _worker():
            global _INFLIGHT
            started = time.time()
            try:
                logger.warning(
                    "[SUMMARY AI ASYNC ENTRY] worker start seq=%s interval=%s approved=%s symbols=%s",
                    seq,
                    interval,
                    len(approved_rows),
                    _symbols(approved_rows),
                )
                result = _ORIG_EXECUTE(
                    ai_results,
                    df_summary=df_summary,
                    interval=interval,
                    max_entries=max_entries,
                    dry_run=False,
                    require_market_open=require_market_open,
                    entry_pipeline=entry_pipeline,
                )
                logger.warning(
                    "[SUMMARY AI ASYNC ENTRY] worker done seq=%s elapsed=%.3fs summary=%s",
                    seq,
                    time.time() - started,
                    _summarize_result(result),
                )
            except Exception as e:
                logger.exception("[SUMMARY AI ASYNC ENTRY] worker failed seq=%s err=%s", seq, e)
            finally:
                with _ASYNC_LOCK:
                    _INFLIGHT = False

        t = threading.Thread(
            target=_worker,
            daemon=True,
            name=f"summary-ai-entry-async-{seq}",
        )
        t.start()

        logger.warning(
            "[SUMMARY AI ASYNC ENTRY] submitted seq=%s interval=%s approved=%s symbols=%s",
            seq,
            interval,
            len(approved_rows),
            _symbols(approved_rows),
        )

        return {
            # サマリー側から見ると「発注処理の投入」は完了。
            # 実際の注文成否は worker done ログで確認する。
            "executed": True,
            "submitted_async": True,
            "async_seq": seq,
            "dry_run": False,
            "approved_rows": approved_rows,
            "result": {"status": "submitted_async", "seq": seq},
            "skip_reason": "submitted_async",
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
        if getattr(current, "_summary_ai_async_entry_patch", False):
            _INSTALLED = True
            logger.warning("[SUMMARY AI ASYNC ENTRY] already installed")
            return True

        _ORIG_EXECUTE = current
        _patched_execute_ai_ok_entries_bulk._summary_ai_async_entry_patch = True  # type: ignore[attr-defined]

        exec_mod.execute_ai_ok_entries_bulk = _patched_execute_ai_ok_entries_bulk
        # runner.py は from .executor import execute_ai_ok_entries_bulk で関数参照を保持しているため、
        # runner 側の参照も差し替える。
        runner_mod.execute_ai_ok_entries_bulk = _patched_execute_ai_ok_entries_bulk

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI ASYNC ENTRY] installed enabled=%s drop_busy=%s",
            _env_bool("SUMMARY_AI_ASYNC_ENTRY", True),
            _env_bool("SUMMARY_AI_ASYNC_ENTRY_DROP_BUSY", True),
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
