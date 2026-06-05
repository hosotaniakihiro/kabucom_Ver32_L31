# ============================================================
# File   : core/startup/pullback_entry_pipeline_patch.py
# Version: V1-PULLBACK-ENTRY-PIPELINE
# ------------------------------------------------------------
# 目的:
#   Summary AI / Ranking だけでは拾いにくい押し目買い・戻り売りを、
#   PUSH 1m/3m/5m summary から検出し、既存 entry_pipeline へ合流させる。
#
# 方針:
#   - run_entry_pipeline() 呼び出し時に PULLBACK_ENTRY 候補を追加。
#   - 既存の liquidity / sell credit / position / board guard はそのまま通す。
#   - source は SUMMARY_AI、entry_type は PULLBACK_ENTRY として扱う。
#   - 低流動性銘柄は detector と entry_pipeline の二段で除外。
# ============================================================
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_RUN_ENTRY_PIPELINE = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ""


def _rows_to_list(rows: Any) -> list[Any]:
    try:
        if rows is None:
            return []
        if isinstance(rows, pd.DataFrame):
            return [r for _, r in rows.iterrows()]
        if isinstance(rows, pd.Series):
            return [rows]
        if isinstance(rows, list):
            return list(rows)
        if isinstance(rows, tuple):
            return list(rows)
        if isinstance(rows, dict):
            return [rows]
    except Exception:
        pass
    return []


def _row_symbol(row: Any) -> str:
    try:
        if isinstance(row, dict):
            return _norm_symbol(row.get("symbol"))
        if isinstance(row, pd.Series):
            return _norm_symbol(row.get("symbol"))
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return _norm_symbol(d.get("symbol"))
    except Exception:
        pass
    return ""


def _merge_rows(base_rows: Any, pullback_rows: list[dict[str, Any]]) -> list[Any]:
    rows = _rows_to_list(base_rows)
    seen = {_row_symbol(r) for r in rows if _row_symbol(r)}
    added = []
    for r in pullback_rows:
        sym = _row_symbol(r)
        if not sym or sym in seen:
            continue
        rows.append(r)
        added.append(sym)
        seen.add(sym)
    if added:
        logger.warning("[PULLBACK ENTRY PIPELINE] merged added=%s symbols=%s", len(added), added)
    return rows


def _patched_run_entry_pipeline(approved_rows: Any, df_summary: pd.DataFrame | None, interval: int):
    try:
        if _env_bool("PULLBACK_ENTRY_ENABLED", True) and int(float(interval or 1)) == 1:
            from trading.summary.pipeline.pullback_entry_detector import detect_pullback_entries
            max_rows = _env_int("PULLBACK_ENTRY_MAX_CANDIDATES", 5)
            pb_rows = detect_pullback_entries(max_rows=max_rows)
            if pb_rows:
                approved_rows = _merge_rows(approved_rows, pb_rows)
    except Exception:
        logger.exception("[PULLBACK ENTRY PIPELINE] merge failed; continue original rows")
    return _ORIG_RUN_ENTRY_PIPELINE(approved_rows, df_summary, interval)  # type: ignore[misc]


def install() -> bool:
    global _INSTALLED, _ORIG_RUN_ENTRY_PIPELINE
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("PULLBACK_ENTRY_ENABLED", "1")
        os.environ.setdefault("PULLBACK_ENTRY_MAX_CANDIDATES", "5")
        os.environ.setdefault("PULLBACK_ENTRY_LOT_RATIO", "0.5")
        os.environ.setdefault("PULLBACK_ENTRY_MIN_PULLBACK_PCT", "0.25")
        os.environ.setdefault("PULLBACK_ENTRY_MAX_PULLBACK_PCT", "1.50")
        os.environ.setdefault("PULLBACK_ENTRY_NEAR_MA_PCT", "0.35")
        os.environ.setdefault("PULLBACK_ENTRY_MIN_REBOUND_VOL_RATIO", "0.80")
        os.environ.setdefault("PULLBACK_ENTRY_MIN_VOLUME", os.getenv("ENTRY_STRICT_MIN_VOLUME", "30000"))
        os.environ.setdefault("PULLBACK_ENTRY_MIN_TURNOVER", os.getenv("ENTRY_STRICT_MIN_TURNOVER", "10000000"))

        import trading.summary.pipeline.entry_pipeline as ep
        cur = getattr(ep, "run_entry_pipeline", None)
        if getattr(cur, "_pullback_entry_pipeline_v1", False):
            _INSTALLED = True
            return True
        if not callable(cur):
            logger.warning("[PULLBACK ENTRY PIPELINE] target missing")
            return False
        _ORIG_RUN_ENTRY_PIPELINE = getattr(cur, "_original", cur)
        wrapped = wraps(_ORIG_RUN_ENTRY_PIPELINE)(_patched_run_entry_pipeline)
        wrapped._pullback_entry_pipeline_v1 = True  # type: ignore[attr-defined]
        wrapped._original = _ORIG_RUN_ENTRY_PIPELINE  # type: ignore[attr-defined]
        ep.run_entry_pipeline = wrapped
        _INSTALLED = True
        logger.warning(
            "[PULLBACK ENTRY PIPELINE] installed v1 enabled=%s max=%s lot_ratio=%s pb_range=%s-%s near_ma=%s vol_ratio=%s",
            os.environ.get("PULLBACK_ENTRY_ENABLED"),
            os.environ.get("PULLBACK_ENTRY_MAX_CANDIDATES"),
            os.environ.get("PULLBACK_ENTRY_LOT_RATIO"),
            os.environ.get("PULLBACK_ENTRY_MIN_PULLBACK_PCT"),
            os.environ.get("PULLBACK_ENTRY_MAX_PULLBACK_PCT"),
            os.environ.get("PULLBACK_ENTRY_NEAR_MA_PCT"),
            os.environ.get("PULLBACK_ENTRY_MIN_REBOUND_VOL_RATIO"),
        )
        return True
    except Exception:
        logger.exception("[PULLBACK ENTRY PIPELINE] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[PULLBACK ENTRY PIPELINE] auto install failed")

__all__ = ["install"]
