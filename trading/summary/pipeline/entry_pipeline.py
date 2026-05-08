# ============================================================
# File   : trading/summary/pipeline/entry_pipeline.py
# Version: Ver2.3-PRODUCTION-HARDENED-POSITION-COMPAT-FIX
# ------------------------------------------------------------
# ✔ AI approved rows → entry execution
# ✔ DataFrame / list / dict / Series 両対応
# ✔ 重複エントリー防止
# ✔ positionチェック
# ✔ positionチェック dict/list/DataFrame/Series 互換
# ✔ global_data.get_positions 不在でも落とさない fallback
# ✔ blowoff top filter
# ✔ run_summary_entry_executor 呼び出し
# ✔ NaN / None 防御
# ✔ source / interval 補完
# ✔ summary→pending→entry_controller 整合
# ✔ skip理由の件数可視化
# ✔ production hardened
# ✔ SUMMARY AI通常エントリーとイナゴ liquidity_shock 条件を分離
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

logger = logging.getLogger(__name__)


DEFAULT_SOURCE = "SUMMARY"


# ============================================================
# normalize rows
# ============================================================

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


# ============================================================
# row helper
# ============================================================

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


# ============================================================
# safe symbol
# ============================================================

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


# ============================================================
# duplicate / position check
# ============================================================

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
            # {symbol: position} 形式
            if target in {_normalize_symbol(k) for k in positions.keys()}:
                return True
            # 1ポジションdict、または values に position dict が入る形式
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
        positions_getter = getattr(global_data, "get_positions", None)

        if callable(positions_getter):
            positions = positions_getter()
        else:
            logger.warning(
                "[entry_pipeline] global_data.get_positions not found; fallback open_positions symbol=%s",
                symbol,
            )
            positions = getattr(global_data, "open_positions", None)

        return _positions_contains_symbol(positions, symbol)

    except Exception:
        logger.exception("[entry_pipeline] position check failed symbol=%s", symbol)
        return False


# ============================================================
# liquidity filters
# ============================================================

def _is_inago_source(row: dict) -> bool:
    source = str(row.get("source") or "").upper()
    strategy = str(row.get("strategy") or row.get("entry_strategy") or "").upper()
    reason = str(row.get("reason") or row.get("ai_reason") or "").upper()
    return (
        "INAGO" in source
        or "TONOSAMA" in source
        or "LIQUIDITY_SHOCK" in source
        or "INAGO" in strategy
        or "TONOSAMA" in strategy
        or "LIQUIDITY_SHOCK" in strategy
        or "LIQUIDITY" in reason and "SHOCK" in reason
    )


def _allow_summary_ai_liquidity(row: dict, *, symbol: str, interval: int) -> bool:
    close = _safe_float(_first(row, ["close", "close_price", "price", "current_price"], 0.0), 0.0)
    volume = _safe_float(_first(row, ["volume", "trading_volume", "出来高"], 0.0), 0.0)
    turnover = _safe_float(_first(row, ["turnover", "trading_value", "売買代金"], 0.0), 0.0)
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume

    score = abs(_safe_float(_first(row, ["score", "score_total", "final_score", "display_score"], 0.0), 0.0))
    buy_score = _safe_float(_first(row, ["buy_score", "score_buy"], 0.0), 0.0)
    sell_score = _safe_float(_first(row, ["sell_score", "score_sell"], 0.0), 0.0)
    effective_score = max(score, buy_score, sell_score)

    min_price = _env_float("SUMMARY_ENTRY_MIN_PRICE", _env_float("ENTRY_MIN_PRICE", 200.0))
    min_volume = _env_float("SUMMARY_ENTRY_MIN_VOLUME", _env_float("ENTRY_MIN_VOLUME", 3000.0))
    min_turnover = _env_float("SUMMARY_ENTRY_MIN_TURNOVER", _env_float("ENTRY_MIN_TURNOVER", 3_000_000.0))
    min_score = _env_float("SUMMARY_ENTRY_MIN_LIQUIDITY_SCORE", 3.0)

    ok = (
        close > min_price
        and volume >= min_volume
        and turnover >= min_turnover
        and effective_score >= min_score
    )

    if ok:
        logger.info(
            "[entry_pipeline] summary liquidity allow symbol=%s interval=%s close=%.2f volume=%.0f turnover=%.0f score=%.3f",
            symbol,
            interval,
            close,
            volume,
            turnover,
            effective_score,
        )
        return True

    logger.info(
        "[entry_pipeline] summary liquidity deny symbol=%s interval=%s close=%.2f volume=%.0f turnover=%.0f score=%.3f min_price=%.1f min_volume=%.0f min_turnover=%.0f min_score=%.2f",
        symbol,
        interval,
        close,
        volume,
        turnover,
        effective_score,
        min_price,
        min_volume,
        min_turnover,
        min_score,
    )
    return False


def _allow_entry_liquidity(row: dict, *, symbol: str, interval: int) -> bool:
    if _is_inago_source(row):
        ok = bool(allow_liquidity_entry(row))
        if not ok:
            logger.info("[entry_pipeline] inago liquidity deny symbol=%s interval=%s", symbol, interval)
        return ok

    return _allow_summary_ai_liquidity(row, symbol=symbol, interval=interval)


# ============================================================
# blowoff filter
# ============================================================

def _filter_blowoff(rows: List[Any], df_summary: pd.DataFrame | None) -> List[Any]:
    try:
        if df_summary is None:
            return rows

        if not isinstance(df_summary, pd.DataFrame):
            return rows

        if df_summary.empty:
            return rows

        tops = detect_blowoff_top(df_summary)

        if tops is None or tops.empty:
            return rows

        if "symbol" not in tops.columns:
            return rows

        top_symbols = set(tops["symbol"].astype(str))

        filtered = []

        for r in rows:
            symbol = _get_symbol(r)

            if symbol in top_symbols:
                logger.info(
                    "[entry_pipeline] skip blowoff top symbol=%s",
                    symbol,
                )
                continue

            filtered.append(r)

        return filtered

    except Exception:
        logger.exception("[entry_pipeline] blowoff filter failed")
        return rows


# ============================================================
# dataframe build
# ============================================================

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
            df_exec["interval"] = df_exec["interval"].apply(
                lambda x: interval if _safe_interval(x) is None else _safe_interval(x)
            )

        if "symbol" in df_exec.columns:
            df_exec["symbol"] = df_exec["symbol"].astype(str).str.replace(r"\.0$", "", regex=True)

        return df_exec

    except Exception:
        logger.exception("[entry_pipeline] build exec dataframe failed interval=%s", interval)
        return pd.DataFrame()


# ============================================================
# entry pipeline
# ============================================================

def run_entry_pipeline(approved_rows: Any, df_summary: pd.DataFrame | None, interval: int):
    try:
        interval = _safe_interval(interval) or interval
        rows = _normalize_rows(approved_rows)

        if not rows:
            logger.info("[entry_pipeline] no approved rows interval=%s", interval)
            return

        total_in = len(rows)
        skipped_no_symbol = 0
        skipped_liquidity = 0
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
                    logger.info(
                        "[entry_pipeline] skip liquidity symbol=%s interval=%s source=%s",
                        symbol,
                        interval,
                        row_dict.get("source"),
                    )
                    continue

            except Exception:
                logger.exception("[entry_pipeline] liquidity filter failed symbol=%s", symbol)
                skipped_liquidity += 1
                continue

            if _already_in_position(symbol):
                skipped_position += 1
                logger.info(
                    "[entry_pipeline] skip position symbol=%s interval=%s",
                    symbol,
                    interval,
                )
                continue

            filtered.append(row_dict)

        before_blowoff = len(filtered)
        filtered = _filter_blowoff(filtered, df_summary)
        after_blowoff = len(filtered)
        skipped_blowoff = before_blowoff - after_blowoff

        logger.info(
            "[entry_pipeline] summary interval=%s approved=%s no_symbol=%s liquidity_skip=%s "
            "position_skip=%s blowoff_skip=%s executable=%s",
            interval,
            total_in,
            skipped_no_symbol,
            skipped_liquidity,
            skipped_position,
            skipped_blowoff,
            len(filtered),
        )

        if not filtered:
            logger.info("[entry_pipeline] no tradable rows after filters interval=%s", interval)
            return

        df_exec = _build_exec_dataframe(filtered, interval)

        if df_exec.empty:
            logger.info("[entry_pipeline] df_exec empty interval=%s", interval)
            return

        logger.info(
            "[entry_pipeline] calling executor interval=%s symbols=%s source_counts=%s",
            interval,
            ",".join(df_exec["symbol"].astype(str).tolist()) if "symbol" in df_exec.columns else "N/A",
            df_exec["source"].value_counts(dropna=False).to_dict() if "source" in df_exec.columns else {},
        )

        run_summary_entry_executor(df_exec, df_summary, interval)

        logger.info(
            "[entry_pipeline] executed entries=%s interval=%s",
            len(df_exec),
            interval,
        )

    except Exception:
        logger.exception("[entry_pipeline] failed interval=%s", interval)
