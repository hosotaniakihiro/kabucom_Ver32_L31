# ============================================================
# File   : trading/filters/summary_ai_range_bridge.py
# ------------------------------------------------------------
# SUMMARY_AI の 1分足即時候補が、5分足レンジ未成熟のせいで
# LOW_MOVE_RANGE_TOO_SMALL で落ちるのを防ぐブリッジ判定。
#
# 旧 core/startup/summary_ai_low_move_softpass_patch.py V3.5 が
# trading/filters/volatility_filter._range_5m_filter_from_entry_row を
# ラップして行っていた判定を本文側へ移設した。
#
# ガードは緩和しない。entry_row 自体のレンジが十分 (min_ok) かつ
# ranking snapshot の実変動も十分 (rescue_ok) の場合だけ、
# 5m代替レンジによる二重ブロックを回避する。
#
# 旧パッチは sitecustomize の BACKGROUND_MAIN_PATCHES 経由 = main.py 専用
# だったため、range_bridge_enabled() で同じスコープを保つ。
# ============================================================

from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V3.5-SUMMARY-AI-1M-RANGE-BRIDGE"

_RANGE_BRIDGE_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)

def _env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in _TRUE
    except Exception:
        return bool(default)

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if math.isnan(x) or math.isinf(x):
            return float(default)
        return x
    except Exception:
        return float(default)

def _row_dict(entry_row: Any) -> dict:
    try:
        if isinstance(entry_row, dict):
            return dict(entry_row)
        if hasattr(entry_row, "to_dict"):
            d = entry_row.to_dict()
            return dict(d) if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}

def _source_is_summary_ai(source: Any, row: dict) -> bool:
    src = str(source or row.get("source") or row.get("entry_type") or row.get("pipeline_source") or "").strip().upper()
    text = " ".join(str(row.get(k) or "") for k in ("entry_type", "source", "reason", "ai_reason", "model_used")).upper()
    return src in {"SUMMARY_AI", "SUMMARY", "PUSH", "PUSH_SUMMARY"} or "SUMMARY_AI" in src or "SRC=SUMMARY" in text or "SUMMARY_AI" in text

def _entry_row_min_range_ok(vf: Any, entry_row: Any) -> tuple[bool, dict]:
    try:
        row = _row_dict(entry_row)
        symbol = str(row.get("symbol") or row.get("Symbol") or "")
        close = _safe_float(_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
        high = _safe_float(_first(row, ("high_price", "high"), 0.0), 0.0)
        low = _safe_float(_first(row, ("low_price", "low"), 0.0), 0.0)
        min_pct = _safe_float(getattr(vf, "DEFAULT_ENTRY_ROW_RANGE_MIN_PCT", 0.006), 0.006)
        ratio = ((high - low) / close) if close > 0 and high >= low and high > 0 and low > 0 else 0.0
        return bool(ratio >= min_pct), {"symbol": symbol, "close": close, "high": high, "low": low, "ratio": ratio, "min_pct": min_pct}
    except Exception:
        return False, {"error": "entry_row_min_range_check_failed"}

def range_bridge_enabled() -> bool:
    """このプロセスで SUMMARY_AI 1m レンジブリッジを使うか。

    SUMMARY_AI_1M_RANGE_BRIDGE_ENABLED=1/0 で明示的に上書きできる。
    """
    try:
        raw = os.getenv("SUMMARY_AI_1M_RANGE_BRIDGE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return _env_bool("SUMMARY_AI_1M_RANGE_BRIDGE_ENABLED", True)
        if _env_bool("CORE_STARTUP_FORCE_FULL_INIT_PATCHES", False):
            return True
        argv_text = " ".join(str(x) for x in sys.argv).replace("\\", "/").lower()
        return not any(name in argv_text for name in _RANGE_BRIDGE_DB_PROCESS_NAMES)
    except Exception:
        return False


def allow_by_1m_range_bridge(vf: Any, entry_row: Any) -> bool:
    """True を返したら 5m レンジ判定をスキップして通してよい。"""
    if not range_bridge_enabled():
        return False
    row = _row_dict(entry_row)
    src = str(row.get("source") or row.get("entry_type") or row.get("pipeline_source") or "")
    try:
        if not _source_is_summary_ai(src, row):
            return False
        min_ok, diag = _entry_row_min_range_ok(vf, entry_row)
        rescue_ok = False
        try:
            rescue_min = _safe_float(getattr(vf, "DEFAULT_RANKING_RESCUE_MIN_PCT", 0.008), 0.008)
            rescue_ok = bool(vf._ranking_move_rescue(entry_row, min_pct=rescue_min, label="summary_ai_1m_range_bridge"))
        except Exception:
            rescue_ok = False
        if min_ok and rescue_ok:
            logger.warning(
                "[LOW MOVE GUARD] SUMMARY_AI 1m range bridge allow symbol=%s ratio=%.6f min_pct=%.6f "
                "rescue_ok=%s version=%s",
                diag.get("symbol"), float(diag.get("ratio") or 0.0),
                float(diag.get("min_pct") or 0.0), rescue_ok, VERSION,
            )
            return True
    except Exception:
        logger.debug("[LOW MOVE GUARD] SUMMARY_AI range bridge precheck failed", exc_info=True)
    return False


def apply_env_defaults() -> None:
    """旧 install() が設定していた env 既定値。"""
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS", "0")
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCHER", "0")
    os.environ.setdefault("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED", "1")
    os.environ.setdefault("ENTRY_EXECUTE_ORIG_TIMEOUT_SEC", "15")
    os.environ.setdefault("SUMMARY_AI_EXECUTOR_ROLLING_RETRY", "1")
    os.environ.setdefault("SUMMARY_AI_EXECUTOR_CANDIDATE_SCAN_LIMIT", "12")
    os.environ.setdefault("SUMMARY_AI_EXECUTOR_BATCH_SIZE", "3")


def install() -> bool:
    """sitecustomize から呼ばれる互換入口。env 既定値の適用だけ行う。"""
    apply_env_defaults()
    logger.warning(
        "[LOW MOVE GUARD] env defaults applied bridge_scope=%s softpass=%s blowoff=%s rolling_retry=%s",
        range_bridge_enabled(),
        os.getenv("SUMMARY_AI_LOW_MOVE_SOFTPASS"),
        os.getenv("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED"),
        os.getenv("SUMMARY_AI_EXECUTOR_ROLLING_RETRY"),
    )
    return True


__all__ = ["install", "apply_env_defaults", "range_bridge_enabled", "allow_by_1m_range_bridge"]
