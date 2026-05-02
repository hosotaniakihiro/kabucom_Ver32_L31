# ============================================================
# File   : trading/summary/engine/incremental/pipeline.py
# Version: Ver2.0-INCREMENTAL-PIPELINE-MODULARIZED
# ------------------------------------------------------------
# ✔ builders / enrich / runner に責務分離
# ✔ 既存 import 互換維持
# ✔ process_single_interval を公開
# ============================================================

from __future__ import annotations

from .builders import build_1m_from_push, build_target_interval_df
from .enrich import (
    ensure_df as _ensure_df,
    ensure_datetime as _ensure_datetime,
    force_enrich_indicators as _force_enrich_indicators,
    has_ohlc as _has_ohlc,
    log_indicator_profile as _log_indicator_profile,
    needs_indicator_rescue as _needs_indicator_rescue,
    rescue_if_needed as _rescue_if_needed,
    safe_non_null as _safe_non_null,
    safe_non_zero as _safe_non_zero,
    sort_symbol_dt as _sort_symbol_dt,
    to_numeric as _to_numeric,
)
from .runner import process_single_interval

__all__ = [
    "build_1m_from_push",
    "build_target_interval_df",
    "process_single_interval",
    "_ensure_df",
    "_to_numeric",
    "_ensure_datetime",
    "_sort_symbol_dt",
    "_safe_non_null",
    "_safe_non_zero",
    "_log_indicator_profile",
    "_has_ohlc",
    "_needs_indicator_rescue",
    "_force_enrich_indicators",
    "_rescue_if_needed",
]