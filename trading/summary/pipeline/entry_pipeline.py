# ============================================================
# File   : trading/summary/pipeline/entry_pipeline.py
# Version: Ver3.0-SUMMARY-AI-BLOWOFF-ALLOW
# ------------------------------------------------------------
# ✔ AI approved rows → entry execution
# ✔ SUMMARY AI通常エントリーとイナゴ liquidity_shock 条件を分離
# ✔ SUMMARY liquidity の min_score を BUY / SELL で分離
# ✔ Ver2.8: Summary AI承認済み候補が liquidity だけで全落ちする問題を救済
# ✔ Ver2.9: 低流動性銘柄へのエントリーを防ぐため、rescueでも
#            出来高3万株・売買代金1000万円を必須化
# ✔ Ver3.0: SUMMARY/PUSH/AI_OK 候補は blowoff top だけで全落ちさせない
# ============================================================

from __future__ import annotations

import logging
import math
import os
from typing import Any, List

import pandas as pd

from global_state import global_data

from trading.summary.summary_entry import run_summary_entry_executor
from trading.ai.blowoff_top_detector import detect_blowoff_top
from trading.ai.liquidity_shock_detector import allow_liquidity_entry
from AI.sell_credit_guard import can_sell_symbol

logger = logging.getLogger(__name__)

DEFAULT_SOURCE = "SUMMARY"


def _normalize_rows(rows: Any) -> List[Any]:
    try:
        if rows is None:
            return []
        if isinstance(rows, pd.DataFrame):
            if rows.empty:
                return []
            return [r for _, r in rows.iterrows()]
        if isinstance(rows, pd.Series):
            return [rows]
        if isinstance(rows, list):
            return rows
        if isinstance(rows, tuple):
            return list(rows)
        if isinstance(rows, dict):
            return [rows]
        return []
    except Exception:
        logger.exception("[entry_pipeline] normalize rows failed")
        return []


def _row_to_dict(row: Any) -> dict:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            return dict(row)
        if isinstance(row, pd.Series):
            return row.to_dict()
        if hasattr(row, "to_dict"):
            v = row.to_dict()
            if isinstance(v, dict):
                return dict(v)
        return {}
    except Exception:
        logger.exception("[entry_pipeline] row_to_dict failed")
        return {}


def _safe_interval(v: Any) -> int | None:
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except Exception:
        return None


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _clean_nan_dict(d: dict) -> dict:
    out = {}
    try:
        for k, v in d.items():
            try:
                if pd.isna(v):
                    out[k] = None
                else:
                    out[k] = v
            except Exception:
                out[k] = v
        return out
    except Exception:
        logger.exception("[entry_pipeline] clean_nan_dict failed")
        return d


def _first(row: dict, keys: list[str], default: Any = None) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _norm_side(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip().upper()
        return s if s in ("BUY", "SELL") else ""
    except Exception:
        return ""


def _resolve_side(row: dict, *, buy_score: float, sell_score: float, raw_score: float) -> str:
    side = _norm_side(row.get("side") or row.get("entry_decision") or row.get("ai_side") or row.get("decision"))
    if side:
        return side
    if sell_score > buy_score and sell_score > 0:
        return "SELL"
    if buy_score > sell_score and buy_score > 0:
        return "BUY"
    if raw_score < 0:
        return "SELL"
    if raw_score > 0:
        return "BUY"
    return ""


def _resolve_side_from_row(row: dict) -> str:
    raw_score = _safe_float(_first(row, ["score", "score_total", "final_score", "display_score"], 0.0), 0.0)
    buy_score = _safe_float(_first(row, ["buy_score", "score_buy"], 0.0), 0.0)
    sell_score = _safe_float(_first(row, ["sell_score", "score_sell"], 0.0), 0.0)
    return _resolve_side(row, buy_score=buy_score, sell_score=sell_score, raw_score=raw_score)


def _resolve_summary_liquidity_min_score(row: dict, *, side: str) -> float:
    if side == "SELL":
        return _env_float("SUMMARY_ENTRY_MIN_LIQUIDITY_SCORE_SELL", _env_float("SUMMARY_ENTRY_MIN_SCORE_SELL", _env_float("MIN_ENTRY_SCORE_SELL_SUMMARY", 1.0)))
    if side == "BUY":
        return _env_float("SUMMARY_ENTRY_MIN_LIQUIDITY_SCORE_BUY", _env_float("SUMMARY_ENTRY_MIN_SCORE_BUY", _env_float("SUMMARY_ENTRY_MIN_LIQUIDITY_SCORE", 3.0)))
    return _env_float("SUMMARY_ENTRY_MIN_LIQUIDITY_SCORE", 3.0)


def _normalize_symbol(value: Any) -> str:
    try:
        s = str(value).strip()
        if not s or s.lower() in ("none", "nan", "nat"):
            return ""
        if s.endswith(".0"):
            ss = s[:-2]
            if ss.isdigit():
                return ss
        return s
    except Exception:
        return ""


def _get_symbol(row: Any) -> str:
    try:
        d = _row_to_dict(row)
        return _normalize_symbol(d.get("symbol", ""))
    except Exception:
        return ""


def _extract_position_symbol(position: Any) -> str:
    try:
        if position is None:
            return ""
        if isinstance(position, pd.Series):
            position = position.to_dict()
        if isinstance(position, dict):
            for key in ("symbol", "Symbol", "銘柄コード", "code", "stock_code", "StockCode"):
                if key in position:
                    sym = _normalize_symbol(position.get(key))
                    if sym:
                        return sym
            return ""
        return _normalize_symbol(position)
    except Exception:
        return ""


def _positions_contains_symbol(positions: Any, symbol: str) -> bool:
    target = _normalize_symbol(symbol)
    if not target:
        return False
    try:
        if positions is None:
            return False
        if isinstance(positions, pd.DataFrame):
            if positions.empty:
                return False
            for col in ("symbol", "Symbol", "銘柄コード", "code", "stock_code", "StockCode"):
                if col in positions.columns:
                    s = positions[col].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
                    return bool((s == target).any())
            return False
        if isinstance(positions, pd.Series):
            return _extract_position_symbol(positions) == target
        if isinstance(positions, dict):
            if target in {_normalize_symbol(k) for k in positions.keys()}:
                return True
            if _extract_position_symbol(positions) == target:
                return True
            for v in positions.values():
                if _extract_position_symbol(v) == target:
                    return True
            return False
        if isinstance(positions, (list, tuple, set)):
            for p in positions:
                if _extract_position_symbol(p) == target:
                    return True
            return False
        return False
    except Exception:
        logger.exception("[entry_pipeline] positions contains check failed symbol=%s", symbol)
        return False


def _already_in_position(symbol: str) -> bool:
    try:
        try:
            from trading.position.open_position_sync import is_symbol_in_open_position
            if is_symbol_in_open_position(symbol, sync=True):
                logger.info("[entry_pipeline] position found by DB sync symbol=%s", symbol)
                return True
        except Exception:
            logger.debug("[entry_pipeline] DB position sync check failed symbol=%s", symbol, exc_info=True)
        positions_getter = getattr(global_data, "get_positions", None)
        if callable(positions_getter):
            positions = positions_getter()
        else:
            logger.warning("[entry_pipeline] global_data.get_positions not found; fallback open_positions symbol=%s", symbol)
            positions = getattr(global_data, "open_positions", None)
        return _positions_contains_symbol(positions, symbol)
    except Exception:
        logger.exception("[entry_pipeline] position check failed symbol=%s", symbol)
        return False


def _is_inago_source(row: dict) -> bool:
    source = str(row.get("source") or "").upper()
    strategy = str(row.get("strategy") or row.get("entry_strategy") or "").upper()
    reason = str(row.get("reason") or row.get("ai_reason") or "").upper()
    return ("INAGO" in source or "TONOSAMA" in source or "LIQUIDITY_SHOCK" in source or "INAGO" in strategy or "TONOSAMA" in strategy or "LIQUIDITY_SHOCK" in strategy or "LIQUIDITY" in reason and "SHOCK" in reason)


def _is_summary_ai_source(row: dict) -> bool:
    try:
        if _is_inago_source(row):
            return False
        source = str(row.get("source") or "").strip().upper()
        entry_type = str(row.get("entry_type") or row.get("type") or "").strip().upper()
        strategy = str(row.get("strategy") or row.get("entry_strategy") or "").strip().upper()
        reason = str(row.get("reason") or row.get("ai_reason") or "").strip().upper()
        decision = str(row.get("decision") or row.get("entry_decision") or row.get("ai_decision") or "").strip().upper()
        return (
            source in {"SUMMARY", "SUMMARY_AI", "PUSH"}
            or entry_type == "SUMMARY_AI"
            or strategy == "SUMMARY_AI"
            or "SUMMARY_AI" in reason
            or "SRC=SUMMARY" in reason
            or decision == "AI_OK"
            or bool(row.get("ai_ok"))
        )
    except Exception:
        return False


def _range_pct(row: dict, close: float) -> float:
    high = _safe_float(_first(row, ["high", "high_price"], 0.0), 0.0)
    low = _safe_float(_first(row, ["low", "low_price"], 0.0), 0.0)
    base = close if close > 0 else max(high, low, 1.0)
    if high > 0 and low > 0 and high >= low and base > 0:
        return (high - low) / base * 100.0
    return 0.0


def _summary_ai_liquidity_rescue(row: dict, *, symbol: str, side: str, close: float, volume: float, turnover: float, effective_score: float) -> bool:
    if not _env_bool("SUMMARY_AI_ENTRY_LIQUIDITY_RESCUE_ENABLED", True):
        return False
    source = str(row.get("source") or "").upper()
    entry_type = str(row.get("entry_type") or "").upper()
    reason = str(row.get("reason") or row.get("ai_reason") or "")
    is_summary_ai = source in {"SUMMARY", "SUMMARY_AI", "PUSH"} or entry_type == "SUMMARY_AI" or "src=SUMMARY" in reason
    if not is_summary_ai:
        return False
    range_pct = _range_pct(row, close)
    mtf = max(
        _safe_float(_first(row, ["mtf", "score_mtf", "mtf_score"], 0.0), 0.0),
        _safe_float(_first(row, ["score_mtf_short", "score_mtf_daily"], 0.0), 0.0),
    )
    slope_abs = abs(_safe_float(_first(row, ["slope_atr_scaled", "slope", "score_slope"], 0.0), 0.0))
    min_price = _env_float("SUMMARY_AI_RESCUE_MIN_PRICE", 300.0)
    min_score = _env_float("SUMMARY_AI_RESCUE_MIN_SCORE", 1.0)
    min_range = _env_float("SUMMARY_AI_RESCUE_MIN_RANGE_PCT", 1.5)
    min_mtf = _env_float("SUMMARY_AI_RESCUE_MIN_MTF", 5.0)
    min_volume = _env_float("SUMMARY_AI_RESCUE_MIN_VOLUME", _env_float("ENTRY_STRICT_MIN_VOLUME", 30000.0))
    min_turnover = _env_float("SUMMARY_AI_RESCUE_MIN_TURNOVER", _env_float("ENTRY_STRICT_MIN_TURNOVER", 10_000_000.0))
    ok = close >= min_price and effective_score >= min_score and range_pct >= min_range and mtf >= min_mtf and volume >= min_volume and turnover >= min_turnover
    if ok:
        logger.warning(
            "[entry_pipeline] SUMMARY AI liquidity rescue allow symbol=%s side=%s close=%.2f volume=%.0f turnover=%.0f score=%.3f range=%.3f mtf=%.3f slope_abs=%.6f min_volume=%.0f min_turnover=%.0f",
            symbol, side, close, volume, turnover, effective_score, range_pct, mtf, slope_abs, min_volume, min_turnover,
        )
        return True
    logger.info(
        "[entry_pipeline] SUMMARY AI liquidity rescue no symbol=%s side=%s close=%.2f volume=%.0f turnover=%.0f score=%.3f range=%.3f mtf=%.3f need_price=%.1f need_score=%.2f need_range=%.2f need_mtf=%.2f need_volume=%.0f need_turnover=%.0f",
        symbol, side, close, volume, turnover, effective_score, range_pct, mtf, min_price, min_score, min_range, min_mtf, min_volume, min_turnover,
    )
    return False


def _allow_summary_ai_liquidity(row: dict, *, symbol: str, interval: int) -> bool:
    close = _safe_float(_first(row, ["close", "close_price", "price", "current_price"], 0.0), 0.0)
    volume = _safe_float(_first(row, ["volume", "trading_volume", "出来高"], 0.0), 0.0)
    turnover = _safe_float(_first(row, ["turnover", "trading_value", "売買代金"], 0.0), 0.0)
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume
    raw_score = _safe_float(_first(row, ["score", "score_total", "final_score", "display_score"], 0.0), 0.0)
    score = abs(raw_score)
    buy_score = _safe_float(_first(row, ["buy_score", "score_buy"], 0.0), 0.0)
    sell_score = _safe_float(_first(row, ["sell_score", "score_sell"], 0.0), 0.0)
    effective_score = max(score, buy_score, sell_score)
    side = _resolve_side(row, buy_score=buy_score, sell_score=sell_score, raw_score=raw_score)
    min_price = _env_float("SUMMARY_ENTRY_MIN_PRICE", _env_float("ENTRY_MIN_PRICE", 200.0))
    min_volume = _env_float("SUMMARY_ENTRY_MIN_VOLUME", _env_float("ENTRY_MIN_VOLUME", _env_float("ENTRY_STRICT_MIN_VOLUME", 30000.0)))
    min_turnover = _env_float("SUMMARY_ENTRY_MIN_TURNOVER", _env_float("ENTRY_MIN_TURNOVER", _env_float("ENTRY_STRICT_MIN_TURNOVER", 10_000_000.0)))
    min_score = _resolve_summary_liquidity_min_score(row, side=side)
    ok = close > min_price and volume >= min_volume and turnover >= min_turnover and effective_score >= min_score
    if ok:
        logger.info("[entry_pipeline] summary liquidity allow symbol=%s interval=%s side=%s close=%.2f volume=%.0f turnover=%.0f score=%.3f min_score=%.2f min_volume=%.0f min_turnover=%.0f", symbol, interval, side, close, volume, turnover, effective_score, min_score, min_volume, min_turnover)
        return True
    if _summary_ai_liquidity_rescue(row, symbol=symbol, side=side, close=close, volume=volume, turnover=turnover, effective_score=effective_score):
        return True
    logger.info("[entry_pipeline] summary liquidity deny symbol=%s interval=%s side=%s close=%.2f volume=%.0f turnover=%.0f score=%.3f min_price=%.1f min_volume=%.0f min_turnover=%.0f min_score=%.2f", symbol, interval, side, close, volume, turnover, effective_score, min_price, min_volume, min_turnover, min_score)
    return False


def _allow_entry_liquidity(row: dict, *, symbol: str, interval: int) -> bool:
    if _is_inago_source(row):
        ok = bool(allow_liquidity_entry(row))
        if not ok:
            logger.info("[entry_pipeline] inago liquidity deny symbol=%s interval=%s", symbol, interval)
        return ok
    return _allow_summary_ai_liquidity(row, symbol=symbol, interval=interval)


def _allow_sell_credit_before_pending(row: dict, *, symbol: str, interval: int) -> bool:
    side = _resolve_side_from_row(row)
    if side != "SELL":
        return True
    try:
        ok = bool(can_sell_symbol(symbol))
    except Exception:
        logger.exception("[entry_pipeline] sell credit precheck failed symbol=%s interval=%s -> skip safe", symbol, interval)
        return False
    if ok:
        logger.info("[entry_pipeline] sell credit allow symbol=%s interval=%s side=%s", symbol, interval, side)
        return True
    logger.info("[entry_pipeline] skip sell credit guard symbol=%s interval=%s side=%s reason=not_short_sellable_pre_pending", symbol, interval, side)
    return False


def _filter_blowoff(rows: List[Any], df_summary: pd.DataFrame | None) -> List[Any]:
    try:
        if df_summary is None or not isinstance(df_summary, pd.DataFrame) or df_summary.empty:
            return rows
        tops = detect_blowoff_top(df_summary)
        if tops is None or tops.empty or "symbol" not in tops.columns:
            return rows
        top_symbols = set(tops["symbol"].astype(str))
        allow_summary_ai_blowoff = _env_bool("SUMMARY_AI_ALLOW_BLOWOFF_TOP", True)
        filtered = []
        for r in rows:
            symbol = _get_symbol(r)
            if symbol in top_symbols:
                row_dict = _clean_nan_dict(_row_to_dict(r))
                side = _resolve_side_from_row(row_dict)
                if allow_summary_ai_blowoff and _is_summary_ai_source(row_dict):
                    logger.warning(
                        "[entry_pipeline] blowoff top detected but SUMMARY_AI/PUSH allowed symbol=%s side=%s source=%s entry_type=%s",
                        symbol,
                        side,
                        row_dict.get("source"),
                        row_dict.get("entry_type"),
                    )
                    filtered.append(r)
                    continue
                logger.info("[entry_pipeline] skip blowoff top symbol=%s side=%s source=%s", symbol, side, row_dict.get("source"))
                continue
            filtered.append(r)
        return filtered
    except Exception:
        logger.exception("[entry_pipeline] blowoff filter failed")
        return rows


def _build_exec_dataframe(rows: List[Any], interval: int) -> pd.DataFrame:
    records: List[dict] = []
    try:
        for row in rows:
            d = _clean_nan_dict(_row_to_dict(row))
            if not d:
                continue
            if not d.get("source"):
                d["source"] = DEFAULT_SOURCE
            if _safe_interval(d.get("interval")) is None:
                d["interval"] = interval
            else:
                d["interval"] = _safe_interval(d.get("interval"))
            records.append(d)
        if not records:
            return pd.DataFrame()
        df_exec = pd.DataFrame(records)
        if "source" not in df_exec.columns:
            df_exec["source"] = DEFAULT_SOURCE
        else:
            df_exec["source"] = df_exec["source"].fillna(DEFAULT_SOURCE)
        if "interval" not in df_exec.columns:
            df_exec["interval"] = interval
        else:
            df_exec["interval"] = df_exec["interval"].apply(lambda x: interval if _safe_interval(x) is None else _safe_interval(x))
        if "symbol" in df_exec.columns:
            df_exec["symbol"] = df_exec["symbol"].astype(str).str.replace(r"\.0$", "", regex=True)
        return df_exec
    except Exception:
        logger.exception("[entry_pipeline] build exec dataframe failed interval=%s", interval)
        return pd.DataFrame()


def _result_executed(result: Any) -> bool:
    try:
        if result is None:
            return False
        if isinstance(result, bool):
            return result
        if isinstance(result, dict):
            if bool(result.get("executed")):
                return True
            for key in ("executed_count", "approved_count", "registered"):
                try:
                    if int(result.get(key) or 0) > 0:
                        return True
                except Exception:
                    pass
            entries = result.get("entries")
            if isinstance(entries, list) and entries:
                return True
            return False
        if isinstance(result, (list, tuple, set)):
            return len(result) > 0
        return bool(result)
    except Exception:
        return False


def run_entry_pipeline(approved_rows: Any, df_summary: pd.DataFrame | None, interval: int):
    try:
        interval = _safe_interval(interval) or interval
        rows = _normalize_rows(approved_rows)
        if not rows:
            logger.info("[entry_pipeline] no approved rows interval=%s", interval)
            return {"executed": False, "entries": 0, "interval": interval, "skip_reason": "no_approved_rows"}
        total_in = len(rows)
        skipped_no_symbol = 0
        skipped_liquidity = 0
        skipped_sell_credit = 0
        skipped_position = 0
        filtered: List[Any] = []
        for r in rows:
            symbol = _get_symbol(r)
            if not symbol:
                skipped_no_symbol += 1
                continue
            try:
                row_dict = _clean_nan_dict(_row_to_dict(r))
                if not row_dict.get("source"):
                    row_dict["source"] = DEFAULT_SOURCE
                if _safe_interval(row_dict.get("interval")) is None:
                    row_dict["interval"] = interval
                else:
                    row_dict["interval"] = _safe_interval(row_dict.get("interval"))
                if not _allow_entry_liquidity(row_dict, symbol=symbol, interval=int(interval)):
                    skipped_liquidity += 1
                    logger.info("[entry_pipeline] skip liquidity symbol=%s interval=%s source=%s", symbol, interval, row_dict.get("source"))
                    continue
                if not _allow_sell_credit_before_pending(row_dict, symbol=symbol, interval=int(interval)):
                    skipped_sell_credit += 1
                    continue
            except Exception:
                logger.exception("[entry_pipeline] pre-entry filters failed symbol=%s", symbol)
                skipped_liquidity += 1
                continue
            if _already_in_position(symbol):
                skipped_position += 1
                logger.info("[entry_pipeline] skip position symbol=%s interval=%s", symbol, interval)
                continue
            filtered.append(row_dict)
        before_blowoff = len(filtered)
        filtered = _filter_blowoff(filtered, df_summary)
        after_blowoff = len(filtered)
        skipped_blowoff = before_blowoff - after_blowoff
        logger.info("[entry_pipeline] summary interval=%s approved=%s no_symbol=%s liquidity_skip=%s sell_credit_skip=%s position_skip=%s blowoff_skip=%s executable=%s", interval, total_in, skipped_no_symbol, skipped_liquidity, skipped_sell_credit, skipped_position, skipped_blowoff, len(filtered))
        if not filtered:
            logger.info("[entry_pipeline] no tradable rows after filters interval=%s", interval)
            return {"executed": False, "entries": 0, "approved": total_in, "interval": interval, "skip_reason": "no_tradable_rows_after_filters", "skipped": {"no_symbol": skipped_no_symbol, "liquidity": skipped_liquidity, "sell_credit": skipped_sell_credit, "position": skipped_position, "blowoff": skipped_blowoff}}
        df_exec = _build_exec_dataframe(filtered, interval)
        if df_exec.empty:
            logger.info("[entry_pipeline] df_exec empty interval=%s", interval)
            return {"executed": False, "entries": 0, "approved": total_in, "interval": interval, "skip_reason": "df_exec_empty"}
        logger.info("[entry_pipeline] calling executor interval=%s symbols=%s source_counts=%s", interval, ",".join(df_exec["symbol"].astype(str).tolist()) if "symbol" in df_exec.columns else "N/A", df_exec["source"].value_counts(dropna=False).to_dict() if "source" in df_exec.columns else {})
        result = run_summary_entry_executor(df_exec, df_summary, interval)
        executed = _result_executed(result)
        logger.info("[entry_pipeline] executed entries=%s interval=%s executed=%s result_type=%s result=%s", len(df_exec), interval, executed, type(result).__name__, result)
        return {"executed": executed, "entries": len(df_exec), "approved": total_in, "interval": interval, "result": result, "skip_reason": None if executed else "summary_entry_executor_no_order", "skipped": {"no_symbol": skipped_no_symbol, "liquidity": skipped_liquidity, "sell_credit": skipped_sell_credit, "position": skipped_position, "blowoff": skipped_blowoff}}
    except Exception as e:
        logger.exception("[entry_pipeline] fatal error interval=%s err=%s", interval, e)
        return {"executed": False, "entries": 0, "interval": interval, "skip_reason": "entry_pipeline_exception", "error": str(e)}


__all__ = ["run_entry_pipeline"]