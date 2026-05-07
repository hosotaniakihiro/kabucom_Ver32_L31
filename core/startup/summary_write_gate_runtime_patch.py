# ============================================================
# File   : core/startup/summary_write_gate_runtime_patch.py
# Version: PRODUCTION-STABLE-REV2-SUMMARY-WRITE-GATE-HUGE-UPsert-SKIP
# ------------------------------------------------------------
# Purpose:
#   - summary DB の write gate が無制限待ちになり、1m/5m/recovery が
#     互いに詰まる問題を起動時に緩和する。
#   - 1分足は短時間だけ待つ。
#   - 3分/5分/その他は busy 時に長時間待たず skip して次回へ回す。
#   - 起動時/復旧時の巨大UPSERT(rows=数万〜十万)は、
#     summary DB を長時間ロックしやすいため同期実行しない。
#
# Why REV2:
#   - rows=93165 / chunk=1243 の recovery UPSERT が
#     BEGIN IMMEDIATE で database is locked になっていた。
#   - retryを増やしても、他プロセスがsummary DBを触る限り詰まりやすい。
#   - main.pyのリアルタイム処理では巨大復旧保存を捨て、
#     夜間バッチ/Yahoo補完/次回差分に任せる方が安全。
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


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _interval_int(v: Any, default: int = 1) -> int:
    try:
        return int(float(str(v).replace("min", "").strip()))
    except Exception:
        return int(default)


def _safe_len_rows(rows: Any) -> int:
    try:
        return int(len(rows))
    except Exception:
        pass

    try:
        if hasattr(rows, "shape"):
            return int(rows.shape[0])
    except Exception:
        pass

    try:
        return int(sum(1 for _ in rows))
    except Exception:
        return -1


def _table_name(interval: Any, table_name: Any = None) -> str:
    if table_name:
        return str(table_name)
    iv = _interval_int(interval, 1)
    return f"stock_summary_{iv}min"


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
        row_count = _safe_len_rows(rows)
        table = _table_name(interval, table_name)

        # ----------------------------------------------------
        # 巨大recovery/closed-market rebuild系UPSERTの同期実行を止める。
        # ----------------------------------------------------
        huge_skip_enabled = _env_bool("SUMMARY_SKIP_HUGE_UPSERT", True)
        huge_threshold = _env_int("SUMMARY_HUGE_UPSERT_SKIP_ROWS", 50000)

        if huge_skip_enabled and row_count >= huge_threshold:
            logger.error(
                "[SUMMARY WRITE GATE PATCH] huge upsert skipped to avoid sqlite lock "
                "interval=%s table=%s rows=%s threshold=%s "
                "hint=set SUMMARY_SKIP_HUGE_UPSERT=0 to force, or run recovery/night batch separately",
                iv,
                table,
                row_count,
                huge_threshold,
            )
            return 0

        if chunk_size is None:
            chunk_size = int(os.environ.get("SUMMARY_UPSERT_CHUNK_SIZE", "75"))
        if retry is None:
            retry = int(os.environ.get("SUMMARY_UPSERT_RETRY", "12"))
        if sleep_base is None:
            sleep_base = float(os.environ.get("SUMMARY_UPSERT_SLEEP_BASE", "0.45"))

        # 中規模以上はロックを取りに行く時間を短くする。
        # ここで失敗しても次回tick/夜間バッチで補完する。
        medium_threshold = _env_int("SUMMARY_MEDIUM_UPSERT_FAST_SKIP_ROWS", 5000)
        if row_count >= medium_threshold:
            write_gate_timeout = _env_float("SUMMARY_WRITE_GATE_TIMEOUT_HUGE", 0.25)
            skip_if_busy = True
            retry = min(int(retry), _env_int("SUMMARY_UPSERT_RETRY_HUGE_MAX", 2))
            sleep_base = min(float(sleep_base), _env_float("SUMMARY_UPSERT_SLEEP_HUGE_MAX", 0.10))
        elif write_gate_timeout is None:
            if iv == 1:
                write_gate_timeout = _env_float("SUMMARY_WRITE_GATE_TIMEOUT_1M", 8.0)
                skip_if_busy = bool(skip_if_busy)
            else:
                write_gate_timeout = _env_float("SUMMARY_WRITE_GATE_TIMEOUT_OTHER", 0.25)
                skip_if_busy = True
                retry = min(int(retry), int(os.environ.get("SUMMARY_UPSERT_RETRY_OTHER_MAX", "2")))
                sleep_base = min(float(sleep_base), float(os.environ.get("SUMMARY_UPSERT_SLEEP_OTHER_MAX", "0.15")))

        logger.debug(
            "[SUMMARY WRITE GATE PATCH] execute_upsert interval=%s table=%s rows=%s timeout=%s skip_if_busy=%s retry=%s sleep=%.3f",
            iv,
            table,
            row_count,
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
        "[SUMMARY WRITE GATE PATCH] installed 1m_timeout=%.2fs other_timeout=%.2fs other_skip_if_busy=True huge_skip=%s huge_threshold=%s",
        _env_float("SUMMARY_WRITE_GATE_TIMEOUT_1M", 8.0),
        _env_float("SUMMARY_WRITE_GATE_TIMEOUT_OTHER", 0.25),
        _env_bool("SUMMARY_SKIP_HUGE_UPSERT", True),
        _env_int("SUMMARY_HUGE_UPSERT_SKIP_ROWS", 50000),
    )


__all__ = ["install_summary_write_gate_runtime_patch"]
