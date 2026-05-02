# ============================================================
# File   : trading/summary/pipeline/entry_pipeline.py
# Version: Ver2.1-PRODUCTION-HARDENED-ENTRY-PIPELINE-INTEGRATED
# ------------------------------------------------------------
# ✔ AI approved rows → entry execution
# ✔ DataFrame / list / dict / Series 両対応
# ✔ 重複エントリー防止
# ✔ positionチェック
# ✔ blowoff top filter
# ✔ run_summary_entry_executor 呼び出し
# ✔ NaN / None 防御
# ✔ source / interval 補完
# ✔ summary→pending→entry_controller 整合
# ✔ skip理由の件数可視化
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
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


# ============================================================
# safe symbol
# ============================================================

def _get_symbol(row: Any) -> str:
    try:
        d = _row_to_dict(row)
        symbol = str(d.get("symbol", "")).strip()
        if symbol.endswith(".0"):
            ss = symbol[:-2]
            if ss.isdigit():
                return ss
        return symbol
    except Exception:
        return ""


# ============================================================
# duplicate / position check
# ============================================================

def _already_in_position(symbol: str) -> bool:
    try:
        positions = global_data.get_positions()

        if not positions:
            return False

        for p in positions:
            try:
                if str(p.get("symbol")).strip() == str(symbol).strip():
                    return True
            except Exception:
                continue

        return False

    except Exception:
        logger.exception("[entry_pipeline] position check failed")
        return False


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

                if not allow_liquidity_entry(row_dict):
                    skipped_liquidity += 1
                    logger.info(
                        "[entry_pipeline] skip liquidity symbol=%s interval=%s",
                        symbol,
                        interval,
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