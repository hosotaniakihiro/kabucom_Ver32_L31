# ============================================================
# File   : trading/ranking/summary/status.py
# Version: Ver1.0-PRODUCTION-RANKING-SUMMARY-STATUS
# ------------------------------------------------------------
# ranking summary 用 status 取得
# ranking_summary_engine.py から安全に切り出すためのモジュール
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from trading.ranking.summary.cache_store import (
    _ensure_global_slots,
    get_ranking_summary,
    get_latest_ranking_summary,
    get_ranking_summary_initialized,
    get_ranking_summary_status_meta,
)
from trading.ranking.summary.filters import (
    get_ranking_summary_universe,
    get_ranking_summary_runtime_filter_enabled,
    get_ranking_summary_use_universe_filter,
    get_last_runtime_symbols,
)
from trading.ranking.summary.announce import (
    get_indicator_mode,
)

logger = logging.getLogger(__name__)


def _safe_len_df(df: Any) -> int:
    try:
        if isinstance(df, pd.DataFrame):
            return int(len(df))
    except Exception:
        pass
    return 0


def _safe_latest_dt(df: Any, candidates: list[str]) -> str:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return ""
        for c in candidates:
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce").dropna()
                if not s.empty:
                    return str(s.max())
        return ""
    except Exception:
        logger.exception("[RANKING SUMMARY] latest dt extraction failed")
        return ""


def _safe_symbol_count(df: Any) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return 0
        if "symbol" not in df.columns:
            return 0
        return int(df["symbol"].astype(str).nunique())
    except Exception:
        logger.exception("[RANKING SUMMARY] symbol count failed")
        return 0


def get_ranking_summary_status() -> dict[str, Any]:
    """
    ranking summary の現在状態を返す。
    UI / debug / health-check 用。
    """
    _ensure_global_slots()

    try:
        df_1m = get_ranking_summary(1)
        df_3m = get_ranking_summary(3)
        df_5m = get_ranking_summary(5)

        latest_1m = get_latest_ranking_summary(1)
        latest_3m = get_latest_ranking_summary(3)
        latest_5m = get_latest_ranking_summary(5)

        universe = get_ranking_summary_universe()
        runtime_symbols = get_last_runtime_symbols()
        meta = get_ranking_summary_status_meta()

        out = {
            "initialized": bool(get_ranking_summary_initialized()),
            "indicator_mode": get_indicator_mode(),
            "runtime_filter_enabled": bool(get_ranking_summary_runtime_filter_enabled()),
            "universe_filter_enabled": bool(get_ranking_summary_use_universe_filter()),
            "universe_count": int(len(universe)),
            "runtime_symbol_count": int(len(runtime_symbols)),
            "rows_1m": _safe_len_df(df_1m),
            "rows_3m": _safe_len_df(df_3m),
            "rows_5m": _safe_len_df(df_5m),
            "latest_rows_1m": _safe_len_df(latest_1m),
            "latest_rows_3m": _safe_len_df(latest_3m),
            "latest_rows_5m": _safe_len_df(latest_5m),
            "symbols_1m": _safe_symbol_count(df_1m),
            "symbols_3m": _safe_symbol_count(df_3m),
            "symbols_5m": _safe_symbol_count(df_5m),
            "latest_symbols_1m": _safe_symbol_count(latest_1m),
            "latest_symbols_3m": _safe_symbol_count(latest_3m),
            "latest_symbols_5m": _safe_symbol_count(latest_5m),
            "latest_dt_1m": _safe_latest_dt(df_1m, ["snapshot_time", "end_time", "datetime"]),
            "latest_dt_3m": _safe_latest_dt(df_3m, ["end_time", "datetime", "snapshot_time"]),
            "latest_dt_5m": _safe_latest_dt(df_5m, ["end_time", "datetime", "snapshot_time"]),
            "latest_bar_dt_1m": _safe_latest_dt(latest_1m, ["snapshot_time", "end_time", "datetime"]),
            "latest_bar_dt_3m": _safe_latest_dt(latest_3m, ["end_time", "datetime", "snapshot_time"]),
            "latest_bar_dt_5m": _safe_latest_dt(latest_5m, ["end_time", "datetime", "snapshot_time"]),
            "meta": dict(meta or {}),
        }

        return out

    except Exception:
        logger.exception("[RANKING SUMMARY] get status failed")
        return {
            "initialized": False,
            "indicator_mode": "unknown",
            "runtime_filter_enabled": False,
            "universe_filter_enabled": False,
            "universe_count": 0,
            "runtime_symbol_count": 0,
            "rows_1m": 0,
            "rows_3m": 0,
            "rows_5m": 0,
            "latest_rows_1m": 0,
            "latest_rows_3m": 0,
            "latest_rows_5m": 0,
            "symbols_1m": 0,
            "symbols_3m": 0,
            "symbols_5m": 0,
            "latest_symbols_1m": 0,
            "latest_symbols_3m": 0,
            "latest_symbols_5m": 0,
            "latest_dt_1m": "",
            "latest_dt_3m": "",
            "latest_dt_5m": "",
            "latest_bar_dt_1m": "",
            "latest_bar_dt_3m": "",
            "latest_bar_dt_5m": "",
            "meta": {},
        }


__all__ = [
    "get_ranking_summary_status",
]