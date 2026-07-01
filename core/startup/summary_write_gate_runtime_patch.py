# ============================================================
# File   : core/startup/summary_write_gate_runtime_patch.py
# Version: PRODUCTION-STABLE-REV3-SUMMARY-WRITE-GATE-PROCESS-AWARE
# ------------------------------------------------------------
# Purpose:
#   - summary DB の write gate が無制限待ちになり、1m/5m/recovery が
#     互いに詰まる問題を起動時に緩和する。
#   - main.py / entry-only runtime では、起動時/復旧時の巨大UPSERTを
#     同期実行せず、リアルタイム発注処理を守る。
#   - main_database.py / yahoo_complement / summary_database_runner などの
#     保存担当プロセスでは巨大UPSERTをスキップしない。
#
# REV3:
#   - data_collectors_runner 配下の yahoo_complement で rows=57028 の
#     1m補完保存が huge upsert skipped になっていたため、プロセス判定を追加。
#   - DB保存担当では huge_skip を無効化し、chunk/retry で保存を続ける。
#
# REV2:
#   - rows=93165 / chunk=1243 の recovery UPSERT が
#     BEGIN IMMEDIATE で database is locked になっていた。
#   - retryを増やしても、他プロセスがsummary DBを触る限り詰まりやすい。
#   - main.pyのリアルタイム処理では巨大復旧保存を捨て、
#     夜間バッチ/Yahoo補完/次回差分に任せる方が安全。
# ============================================================

from __future__ import annotations

import logging
import os
import sys
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
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _argv_text() -> str:
    try:
        return " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
    except Exception:
        return ""


def _is_summary_save_owner_process() -> bool:
    """summary DB保存を担うプロセスでは巨大UPSERTを捨てない。"""
    argv = _argv_text()
    save_owner_markers = (
        "main_database.py",
        "data_collectors_runner.py",
        "summary_database_runner.py",
        "yahoo_complement",
        "yahoo_complement_runner",
        "run_night_yahoo",
        "run_update_daily_data_from_yahoo",
        "run_night_yahoo_full_summary",
        "summary_recovery",
    )
    if any(x in argv for x in save_owner_markers):
        return True
    return any(
        _env_bool(x, False)
        for x in (
            "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
            "AUTOSTOCK_MAIN_DATABASE_PROCESS",
            "AUTOSTOCK_SUMMARY_DB_WRITER",
            "AUTOSTOCK_YAHOO_COMPLEMENT_PROCESS",
            "SUMMARY_DB_SAVE_OWNER_PROCESS",
        )
    )


def _is_entry_only_process() -> bool:
    if _env_bool("SUMMARY_MAIN_ENTRY_ONLY", False):
        return True
    argv = _argv_text()
    if "main.py" in argv and not _is_summary_save_owner_process():
        return True
    return False


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
        save_owner = _is_summary_save_owner_process()
        entry_only = _is_entry_only_process()

        # ----------------------------------------------------
        # 巨大recovery/closed-market rebuild系UPSERTの同期実行制御。
        # main.py はリアルタイム発注を守るため skip 可。
        # DB保存担当プロセスは skip せず保存する。
        # ----------------------------------------------------
        huge_skip_default = bool(entry_only and not save_owner)
        huge_skip_enabled = _env_bool("SUMMARY_SKIP_HUGE_UPSERT", huge_skip_default)
        huge_threshold = _env_int("SUMMARY_HUGE_UPSERT_SKIP_ROWS", 50000)

        if save_owner and _env_bool("SUMMARY_SAVE_OWNER_DISABLE_HUGE_SKIP", True):
            huge_skip_enabled = False

        if huge_skip_enabled and row_count >= huge_threshold:
            logger.error(
                "[SUMMARY WRITE GATE PATCH] huge upsert skipped to avoid sqlite lock "
                "interval=%s table=%s rows=%s threshold=%s save_owner=%s entry_only=%s "
                "hint=set SUMMARY_SKIP_HUGE_UPSERT=0 to force, or run recovery/night batch separately",
                iv,
                table,
                row_count,
                huge_threshold,
                save_owner,
                entry_only,
            )
            return 0

        if chunk_size is None:
            chunk_size = int(os.environ.get("SUMMARY_UPSERT_CHUNK_SIZE", "75"))
        if retry is None:
            retry = int(os.environ.get("SUMMARY_UPSERT_RETRY", "12"))
        if sleep_base is None:
            sleep_base = float(os.environ.get("SUMMARY_UPSERT_SLEEP_BASE", "0.45"))

        # 中規模以上はロックを取りに行く時間を短くする。
        # 保存担当プロセスでは skip_if_busy を既定で False にして、補完保存を落とさない。
        medium_threshold = _env_int("SUMMARY_MEDIUM_UPSERT_FAST_SKIP_ROWS", 5000)
        if row_count >= medium_threshold:
            if save_owner:
                write_gate_timeout = _env_float("SUMMARY_WRITE_GATE_TIMEOUT_SAVE_OWNER_HUGE", 20.0)
                skip_if_busy = _env_bool("SUMMARY_SAVE_OWNER_HUGE_SKIP_IF_BUSY", False)
                retry = max(int(retry), _env_int("SUMMARY_UPSERT_RETRY_SAVE_OWNER_HUGE_MIN", 8))
                sleep_base = min(float(sleep_base), _env_float("SUMMARY_UPSERT_SLEEP_SAVE_OWNER_HUGE_MAX", 0.20))
                if chunk_size is None:
                    chunk_size = _env_int("SUMMARY_UPSERT_CHUNK_SAVE_OWNER_HUGE", 250)
            else:
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
            "[SUMMARY WRITE GATE PATCH] execute_upsert interval=%s table=%s rows=%s timeout=%s skip_if_busy=%s retry=%s sleep=%.3f save_owner=%s entry_only=%s",
            iv,
            table,
            row_count,
            write_gate_timeout,
            skip_if_busy,
            int(retry),
            float(sleep_base),
            save_owner,
            entry_only,
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
        "[SUMMARY WRITE GATE PATCH] installed rev3 1m_timeout=%.2fs other_timeout=%.2fs other_skip_if_busy=True huge_skip_default_entry_only=%s huge_threshold=%s save_owner=%s argv=%s",
        _env_float("SUMMARY_WRITE_GATE_TIMEOUT_1M", 8.0),
        _env_float("SUMMARY_WRITE_GATE_TIMEOUT_OTHER", 0.25),
        _is_entry_only_process() and not _is_summary_save_owner_process(),
        _env_int("SUMMARY_HUGE_UPSERT_SKIP_ROWS", 50000),
        _is_summary_save_owner_process(),
        sys.argv,
    )


def install() -> bool:
    install_summary_write_gate_runtime_patch()
    return True


__all__ = ["install_summary_write_gate_runtime_patch", "install"]
