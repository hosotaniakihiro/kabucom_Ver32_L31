# ============================================================
# File   : core/startup/summary_write_gate_runtime_patch.py
# Version: PRODUCTION-STABLE-REV1-SUMMARY-WRITE-GATE-FAST-SKIP
# ------------------------------------------------------------
# Purpose:
#   - summary DB の write gate が無制限待ちになり、1m/5m/recovery が
#     互いに詰まる問題を起動時に緩和する。
#   - 1分足は短時間だけ待つ。
#   - 3分/5分/その他は busy 時に長時間待たず skip して次回へ回す。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_EXECUTE_UPSERT = None


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _interval_int(v: Any, default: int = 1) -> int:
    try:
        return int(float(str(v).replace("min", "").strip()))
    except Exception:
        return int(default)


def install_summary_write_gate_runtime_patch() -> None:
    global _INSTALLED, _ORIGINAL_EXECUTE_UPSERT
    if _INSTALLED:
        return

    try:
        from trading.summary.persistence.core import upsert_executor as ue
    except Exception:
        logger.exception("[SUMMARY WRITE GATE PATCH] import upsert_executor failed")
        return

    original = getattr(ue, "execute_upsert", None)
    if not callable(original):
        logger.warning("[SUMMARY WRITE GATE PATCH] execute_upsert missing")
        return

    _ORIGINAL_EXECUTE_UPSERT = original

    def patched_execute_upsert(
        rows,
        interval,
        engine=None,
        table_name=None,
        *,
        chunk_size=None,
        retry=None,
        sleep_base=None,
        skip_if_busy=False,
        write_gate_timeout=None,
    ):
        iv = _interval_int(interval, 1)

        if chunk_size is None:
            chunk_size = int(os.environ.get("SUMMARY_UPSERT_CHUNK_SIZE", "75"))
        if retry is None:
            retry = int(os.environ.get("SUMMARY_UPSERT_RETRY", "12"))
        if sleep_base is None:
            sleep_base = float(os.environ.get("SUMMARY_UPSERT_SLEEP_BASE", "0.45"))

        if write_gate_timeout is None:
            if iv == 1:
                write_gate_timeout = _env_float("SUMMARY_WRITE_GATE_TIMEOUT_1M", 8.0)
                skip_if_busy = bool(skip_if_busy)
            else:
                write_gate_timeout = _env_float("SUMMARY_WRITE_GATE_TIMEOUT_OTHER", 0.25)
                skip_if_busy = True
                retry = min(int(retry), int(os.environ.get("SUMMARY_UPSERT_RETRY_OTHER_MAX", "2")))
                sleep_base = min(float(sleep_base), float(os.environ.get("SUMMARY_UPSERT_SLEEP_OTHER_MAX", "0.15")))

        logger.debug(
            "[SUMMARY WRITE GATE PATCH] execute_upsert interval=%s timeout=%s skip_if_busy=%s retry=%s sleep=%.3f",
            iv,
            write_gate_timeout,
            skip_if_busy,
            int(retry),
            float(sleep_base),
        )

        return original(
            rows,
            interval,
            engine=engine,
            table_name=table_name,
            chunk_size=int(chunk_size),
            retry=int(retry),
            sleep_base=float(sleep_base),
            skip_if_busy=bool(skip_if_busy),
            write_gate_timeout=float(write_gate_timeout),
        )

    ue.execute_upsert = patched_execute_upsert

    _INSTALLED = True
    logger.warning(
        "[SUMMARY WRITE GATE PATCH] installed 1m_timeout=%.2fs other_timeout=%.2fs other_skip_if_busy=True",
        _env_float("SUMMARY_WRITE_GATE_TIMEOUT_1M", 8.0),
        _env_float("SUMMARY_WRITE_GATE_TIMEOUT_OTHER", 0.25),
    )


__all__ = ["install_summary_write_gate_runtime_patch"]
