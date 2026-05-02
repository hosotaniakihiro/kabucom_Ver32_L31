# ============================================================
# File   : core/startup/startup_status.py
# Version: FINAL-PRODUCTION-REV23.1-STARTUP-STATUS
#          -RANKING-SUMMARY-BOOTSTRAP-STATUS
# ------------------------------------------------------------
# 【概要】
#   startup 完了時の状態収集・ログ出力を担当
#
# 【機能】
#   ✔ push status
#   ✔ scheduler status
#   ✔ schedule loop status
#   ✔ startup summary restore status
#   ✔ ranking summary bootstrap status
#   ✔ summary bootstrap status
#   ✔ MTF bootstrap status
#   ✔ final STARTUP COMPLETE log
#
# 【REV23.1 変更点】
#   ✔ ranking_summary_bootstrap_* を final status に追加
#   ✔ ranking summary bootstrap の保存件数 / snapshot行数 / DB path / message を表示
#   ✔ ranking summary cache の行数・最新datetimeを簡易収集
#   ✔ 既存機能削除ゼロ
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from global_state import global_data

from core.startup.push_storage_bootstrap import is_push_storage_running
from core.startup.summary_runtime import get_summary_bootstrap_state
from core.startup.schedule_loop import get_schedule_loop_status
from core.startup.scheduler_startup import log_scheduler_snapshot

logger = logging.getLogger(__name__)

VERSION = "FINAL-PRODUCTION-REV23.1-STARTUP-STATUS-RANKING-SUMMARY-BOOTSTRAP"


# ============================================================
# small helpers
# ============================================================

def _safe_getattr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _safe_len(x: Any) -> int:
    try:
        return len(x)
    except Exception:
        return 0


def _safe_head(x: Any, n: int = 10) -> list[Any]:
    try:
        return list(x)[:n]  # type: ignore[arg-type]
    except Exception:
        return []


def _latest_dt_from_df(df: Any) -> str | None:
    """
    DataFrame から datetime の最大値を安全に文字列で返す。
    """
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None

        if "datetime" not in df.columns:
            return None

        s = pd.to_datetime(df["datetime"], errors="coerce").dropna()
        if s.empty:
            return None

        return str(s.max())
    except Exception:
        return None


def _nunique_symbol_from_df(df: Any) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return 0
        if "symbol" not in df.columns:
            return 0
        return int(df["symbol"].astype(str).nunique())
    except Exception:
        return 0


def _result_to_dict(result: Any) -> Any:
    """
    dataclass / object / dict の bootstrap result をログに出しやすい形へ変換する。
    """
    if result is None:
        return None

    if isinstance(result, dict):
        return result

    out: dict[str, Any] = {}

    for name in [
        "ok",
        "intervals",
        "db_path",
        "snapshot_rows",
        "message",
    ]:
        try:
            out[name] = getattr(result, name)
        except Exception:
            pass

    return out or str(result)


def _collect_ranking_summary_cache_status() -> dict[str, Any]:
    """
    global_data に入っている ranking summary cache の状態を簡易収集する。

    bootstrap_cache.py では以下のような属性へ反映する想定:
      - ranking_summary_1min
      - ranking_summary_3min
      - ranking_summary_5min
      - ranking_summary_1
      - ranking_summary_3
      - ranking_summary_5
      - ranking_summary_df_1
      - ranking_summary_df_3
      - ranking_summary_df_5
      - ranking_merged_summary_1min
      - ranking_merged_summary_3min
      - ranking_merged_summary_5min
    """
    status: dict[str, Any] = {}

    for interval in (1, 3, 5):
        candidates = [
            f"ranking_summary_{interval}min",
            f"ranking_summary_{interval}",
            f"ranking_summary_df_{interval}",
            f"ranking_merged_summary_{interval}min",
        ]

        df = None
        used_attr = None

        for attr in candidates:
            val = _safe_getattr(global_data, attr, None)
            if isinstance(val, pd.DataFrame):
                df = val
                used_attr = attr
                break

        key = f"{interval}min"
        status[key] = {
            "attr": used_attr,
            "rows": _safe_len(df) if isinstance(df, pd.DataFrame) else 0,
            "symbols": _nunique_symbol_from_df(df),
            "latest_dt": _latest_dt_from_df(df),
        }

    return status


def _collect_ranking_summary_bootstrap_status() -> dict[str, Any]:
    """
    ranking summary bootstrap の状態を global_data から収集する。
    """
    result = _safe_getattr(global_data, "ranking_summary_bootstrap_result", None)

    # result に入っていればそれを優先し、なければ個別flagを見る
    result_dict = _result_to_dict(result)

    saved = _safe_getattr(global_data, "ranking_summary_bootstrap_saved", {})
    snapshot_rows = _safe_getattr(global_data, "ranking_summary_bootstrap_snapshot_rows", 0)
    db_path = _safe_getattr(global_data, "ranking_summary_bootstrap_db_path", None)
    message = _safe_getattr(global_data, "ranking_summary_bootstrap_message", "")

    if isinstance(result_dict, dict):
        saved = result_dict.get("intervals", saved)
        snapshot_rows = result_dict.get("snapshot_rows", snapshot_rows)
        db_path = result_dict.get("db_path", db_path)
        message = result_dict.get("message", message)

    return {
        "started": _safe_getattr(global_data, "ranking_summary_bootstrap_started", False),
        "done": _safe_getattr(global_data, "ranking_summary_bootstrap_done", False),
        "failed": _safe_getattr(global_data, "ranking_summary_bootstrap_failed", False),
        "saved": saved,
        "snapshot_rows": snapshot_rows,
        "db_path": db_path,
        "message": message,
        "result": result_dict,
        "cache": _collect_ranking_summary_cache_status(),
    }


# ============================================================
# push status
# ============================================================

def refresh_final_push_flags() -> tuple[bool, bool, bool]:
    push_storage_running = False
    push_stream_running = False
    ws_connected = False

    try:
        push_storage_running = bool(is_push_storage_running())
        global_data.push_storage_running = push_storage_running
        global_data.push_writer_running = push_storage_running
    except Exception:
        logger.debug("[startup_status] final push storage status check failed", exc_info=True)

    try:
        push_stream_running = bool(getattr(global_data, "push_stream_running", False))
        ws_connected = bool(getattr(global_data, "ws_connected", False))
    except Exception:
        logger.debug("[startup_status] final push stream status check failed", exc_info=True)

    return push_storage_running, push_stream_running, ws_connected


# ============================================================
# collect status
# ============================================================

def collect_startup_status() -> dict[str, Any]:
    push_storage_running, push_stream_running, ws_connected = refresh_final_push_flags()

    st = get_summary_bootstrap_state()

    bridge_count = 0
    bridge_head = []
    try:
        bridge_count = int(getattr(global_data, "push_symbol_bridge_count", 0) or 0)
        bridge_head = _safe_head(getattr(global_data, "push_symbol_bridge_symbols", []) or [], 10)
    except Exception:
        pass

    ranking_summary_status = _collect_ranking_summary_bootstrap_status()

    status: dict[str, Any] = {
        # ----------------------------------------------------
        # PUSH
        # ----------------------------------------------------
        "push_storage_running": push_storage_running,
        "push_stream_running": push_stream_running,
        "ws_connected": ws_connected,

        "push_stream_early_start_done": getattr(global_data, "push_stream_early_start_done", False),
        "push_stream_early_start_failed": getattr(global_data, "push_stream_early_start_failed", False),
        "push_stream_early_start_result": getattr(global_data, "push_stream_early_start_result", None),

        # ----------------------------------------------------
        # scheduler
        # ----------------------------------------------------
        "scheduler_registered": getattr(global_data, "scheduler_bootstrap_registered", False),
        "scheduler_failed": getattr(global_data, "scheduler_bootstrap_failed", False),
        "scheduler_result": getattr(global_data, "scheduler_bootstrap_result", None),

        "schedule_loop_status": get_schedule_loop_status(),

        # ----------------------------------------------------
        # PUSH symbol bridge
        # ----------------------------------------------------
        "push_symbol_bridge_count": bridge_count,
        "push_symbol_bridge_head": bridge_head,

        # ----------------------------------------------------
        # startup summary restore - PUSH由来
        # ----------------------------------------------------
        "startup_summary_restore_done": getattr(global_data, "startup_summary_restore_done", False),
        "startup_summary_restore_failed": getattr(global_data, "startup_summary_restore_failed", False),
        "startup_summary_restore_result": getattr(global_data, "startup_summary_restore_result", None),

        # ----------------------------------------------------
        # ranking summary bootstrap - ランキング由来
        # ----------------------------------------------------
        "ranking_summary_bootstrap": ranking_summary_status,

        # 個別キーでも見やすいように残す
        "ranking_summary_bootstrap_started": ranking_summary_status.get("started"),
        "ranking_summary_bootstrap_done": ranking_summary_status.get("done"),
        "ranking_summary_bootstrap_failed": ranking_summary_status.get("failed"),
        "ranking_summary_bootstrap_saved": ranking_summary_status.get("saved"),
        "ranking_summary_bootstrap_snapshot_rows": ranking_summary_status.get("snapshot_rows"),
        "ranking_summary_bootstrap_db_path": ranking_summary_status.get("db_path"),
        "ranking_summary_bootstrap_message": ranking_summary_status.get("message"),

        # ----------------------------------------------------
        # summary unique index
        # ----------------------------------------------------
        "summary_unique_index_bootstrap_started": getattr(global_data, "summary_unique_index_bootstrap_started", False),
        "summary_unique_index_bootstrap_done": getattr(global_data, "summary_unique_index_bootstrap_done", False),
        "summary_unique_index_bootstrap_failed": getattr(global_data, "summary_unique_index_bootstrap_failed", False),
        "summary_unique_index_bootstrap_results": getattr(global_data, "summary_unique_index_bootstrap_results", None),

        # ----------------------------------------------------
        # MTF history
        # ----------------------------------------------------
        "mtf_history_bootstrap_started": getattr(global_data, "mtf_history_bootstrap_started", False),
        "mtf_history_bootstrap_done": getattr(global_data, "mtf_history_bootstrap_done", False),
        "mtf_history_bootstrap_failed": getattr(global_data, "mtf_history_bootstrap_failed", False),
        "mtf_history_bootstrap_results": getattr(global_data, "mtf_history_bootstrap_results", None),

        # ----------------------------------------------------
        # summary bootstrap state
        # ----------------------------------------------------
        "summary_bootstrap_started": st.get("started"),
        "summary_bootstrap_done": st.get("done"),
        "summary_bootstrap_failed": st.get("failed"),
    }

    return status


# ============================================================
# final log
# ============================================================

def log_startup_complete() -> dict[str, Any]:
    status = collect_startup_status()

    log_scheduler_snapshot("startup complete")

    logger.info(
        "🚀 STARTUP COMPLETE "
        "(REV23.1 THIN-ORCHESTRATOR "
        "PUSH-STARTUP "
        "SCHEDULER-STARTUP "
        "SCHEDULE-RUN-PENDING-LOOP "
        "SUMMARY-STARTUP "
        "RANKING-SUMMARY-BOOTSTRAP) "
        "status=%s",
        status,
    )

    return status


__all__ = [
    "VERSION",
    "refresh_final_push_flags",
    "collect_startup_status",
    "log_startup_complete",
]