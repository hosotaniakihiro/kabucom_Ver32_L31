# ============================================================
# File   : core/startup/entry_order_mtf_slope_fill_patch.py
# Version: Ver03-ENTRY-ORDER-MTF-SLOPE-FILL-CHAIN-SUMMARY-GUARD
# ------------------------------------------------------------
# Purpose:
#   SUMMARY_AI 承認後の entry_row に slope_1m / slope_3m / slope_5m が
#   欠けている場合、発注直前に summary DB / enriched row から補完する。
#
# 背景:
#   表示DFでは daily MTF / ranking が入っていても、AI承認済み entries の
#   entry_row が古い列セットのままになり、発注直前 guard で
#   SHORT_MTF_SLOPE_MISSING になるケースがある。
#
# 修正:
#   Ver02:
#   - Python SyntaxError 対策
#     f-string expression part cannot include a backslash
#   - SQL列名生成を _qident() に集約し、f-string式内の \" を廃止
#   Ver03:
#   - 起動時に summary_no_overlap_runtime_patch も連鎖installする
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from functools import wraps
from pathlib import Path
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL = None


def _install_summary_no_overlap_guard() -> None:
    try:
        from core.startup import summary_no_overlap_runtime_patch as snop
        fn = getattr(snop, "install", None)
        ok = fn() if callable(fn) else False
        logger.warning("[ENTRY ORDER MTF SLOPE FILL] chained summary_no_overlap install=%s", ok)
    except Exception:
        logger.exception("[ENTRY ORDER MTF SLOPE FILL] chained summary_no_overlap install failed")


def _today() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _summary_db_path() -> str:
    base = os.getenv(
        "SUMMARY_DB_DIR",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary",
    )
    return os.getenv("SUMMARY_DB_PATH", str(Path(base) / f"summary{_today()}.db"))


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    if s.endswith(".T"):
        s = s[:-2]
    return s


def _qident(name: Any) -> str:
    """
    SQLite identifier quote.

    注意:
      f-string の { ... } 内に backslash を含む文字列を書くと
      SyntaxError: f-string expression part cannot include a backslash
      になるため、識別子クォートはこの関数に集約する。
    """
    return '"' + str(name).replace('"', '""') + '"'


def _safe_float_or_none(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() in {"nan", "none", "<na>"}:
            return None
        x = float(s)
        if x != x:
            return None
        return x
    except Exception:
        return None


def _is_missing(v: Any) -> bool:
    return _safe_float_or_none(v) is None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone() is not None
    except Exception:
        return False


def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({_qident(table)})").fetchall()}
    except Exception:
        return set()


def _read_latest_summary_values(symbol: str, interval: int) -> dict[str, Any]:
    path = _summary_db_path()
    table = f"stock_summary_{int(interval)}min"
    if not symbol or not Path(path).exists():
        return {}

    try:
        with sqlite3.connect(path, timeout=1.0) as conn:
            conn.execute("PRAGMA busy_timeout=1000;")
            if not _table_exists(conn, table):
                return {}
            cols = _cols(conn, table)
            if "symbol" not in cols or "datetime" not in cols:
                return {}

            want = [
                "symbol", "datetime", "slope", "slope_atr_scaled", "score_slope",
                "mtf", "score_mtf", "mtf_score", "score_mtf_daily", "score_mtf_short",
                "ranking_score", "ranking_score_total", "ranking_type", "rank", "change_rate", "turnover",
            ]
            select_cols = [c for c in want if c in cols]
            if not select_cols:
                return {}

            select_cols_sql = ", ".join(_qident(c) for c in select_cols)
            sql = (
                f"SELECT {select_cols_sql} "
                f"FROM {_qident(table)} WHERE CAST({_qident('symbol')} AS TEXT)=? "
                f"ORDER BY {_qident('datetime')} DESC LIMIT 1"
            )
            row = conn.execute(sql, (symbol,)).fetchone()
            if not row:
                return {}
            return dict(zip(select_cols, row))
    except Exception:
        logger.debug(
            "[ENTRY ORDER MTF SLOPE FILL] read latest summary failed symbol=%s interval=%s path=%s",
            symbol,
            interval,
            path,
            exc_info=True,
        )
        return {}


def _try_enrich_one_row(row: dict[str, Any]) -> dict[str, Any]:
    try:
        from trading.summary.controller_enrich import enrich_summary_latest

        interval = int(float(row.get("interval") or 1))
        df = pd.DataFrame([row])
        out = enrich_summary_latest(df, interval=interval, context="entry-order-prebuild")
        if isinstance(out, pd.DataFrame) and not out.empty:
            merged = dict(row)
            enriched = out.iloc[0].to_dict()
            for k, v in enriched.items():
                if k not in merged or merged.get(k) in (None, ""):
                    merged[k] = v
                elif k in {"mtf", "score_mtf", "mtf_score", "ranking_score", "ranking_score_total"}:
                    old = _safe_float_or_none(merged.get(k))
                    new = _safe_float_or_none(v)
                    if (old is None or old == 0.0) and new not in (None, 0.0):
                        merged[k] = new
            return merged
    except Exception:
        logger.debug("[ENTRY ORDER MTF SLOPE FILL] enrich one row failed", exc_info=True)
    return row


def _fill_entry_row(entry_row: Any, *, symbol: str, side: str, source: str) -> Any:
    if not isinstance(entry_row, dict):
        return entry_row
    if str(source or "").upper() != "SUMMARY_AI":
        return entry_row

    row = dict(entry_row)
    symbol = _norm_symbol(symbol or row.get("symbol"))
    row["symbol"] = symbol

    row = _try_enrich_one_row(row)

    filled = {}
    for iv in (1, 3, 5):
        vals = _read_latest_summary_values(symbol, iv)
        slope_val = _safe_float_or_none(vals.get("slope_atr_scaled")) if vals else None
        if slope_val is None and vals:
            slope_val = _safe_float_or_none(vals.get("slope"))
        if slope_val is None and iv == int(float(row.get("interval") or 1)):
            slope_val = _safe_float_or_none(row.get("slope_atr_scaled"))
            if slope_val is None:
                slope_val = _safe_float_or_none(row.get("slope"))

        if slope_val is not None:
            for key in (f"slope_atr_scaled_{iv}m", f"slope_{iv}m"):
                if _is_missing(row.get(key)):
                    row[key] = slope_val
                    filled[key] = slope_val

        if vals:
            for key in ("mtf", "score_mtf", "mtf_score"):
                cand = None
                for src_key in (key, "score_mtf", "score_mtf_daily", "mtf_score"):
                    cand = _safe_float_or_none(vals.get(src_key))
                    if cand not in (None, 0.0):
                        break
                old = _safe_float_or_none(row.get(key))
                if (old is None or old == 0.0) and cand not in (None, 0.0):
                    row[key] = cand
                    filled[key] = cand
            for key in ("ranking_score", "ranking_score_total", "rank", "change_rate", "turnover"):
                old = _safe_float_or_none(row.get(key))
                cand = _safe_float_or_none(vals.get(key))
                if (old is None or old == 0.0) and cand not in (None, 0.0):
                    row[key] = cand
                    filled[key] = cand
            for key in ("ranking_type",):
                if not str(row.get(key) or "").strip() and str(vals.get(key) or "").strip():
                    row[key] = vals.get(key)
                    filled[key] = vals.get(key)

    current_slope = _safe_float_or_none(row.get("slope_atr_scaled"))
    if current_slope is None:
        current_slope = _safe_float_or_none(row.get("slope"))
    if current_slope is not None:
        for iv in (1, 3, 5):
            for key in (f"slope_atr_scaled_{iv}m", f"slope_{iv}m"):
                if _is_missing(row.get(key)):
                    row[key] = current_slope
                    filled[key] = current_slope

    if filled:
        logger.info(
            "[ENTRY ORDER MTF SLOPE FILL] symbol=%s side=%s source=%s filled=%s",
            symbol,
            side,
            source,
            filled,
        )
    return row


def install() -> bool:
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        logger.warning("[ENTRY ORDER MTF SLOPE FILL] already installed")
        return True

    try:
        _install_summary_no_overlap_guard()

        import trading.handlers.entry_order_builder as eob
        import trading.handlers.entry_controller as ec

        original = getattr(eob, "build_entry_order", None)
        if not callable(original):
            logger.error("[ENTRY ORDER MTF SLOPE FILL] build_entry_order not callable")
            return False

        if getattr(original, "_entry_order_mtf_slope_fill_v1", False):
            _INSTALLED = True
            return True

        _ORIGINAL = original

        @wraps(original)
        def wrapped_build_entry_order(*args: Any, **kwargs: Any):
            try:
                entry_row = kwargs.get("entry_row")
                symbol = kwargs.get("symbol")
                side = kwargs.get("side")
                source = kwargs.get("source")
                if isinstance(entry_row, dict):
                    kwargs = dict(kwargs)
                    kwargs["entry_row"] = _fill_entry_row(
                        entry_row,
                        symbol=str(symbol or entry_row.get("symbol") or ""),
                        side=str(side or entry_row.get("side") or entry_row.get("entry_decision") or ""),
                        source=str(source or entry_row.get("source") or ""),
                    )
            except Exception:
                logger.exception("[ENTRY ORDER MTF SLOPE FILL] prefill failed")
            return original(*args, **kwargs)

        wrapped_build_entry_order._entry_order_mtf_slope_fill_v1 = True  # type: ignore[attr-defined]
        wrapped_build_entry_order._original = original  # type: ignore[attr-defined]

        eob.build_entry_order = wrapped_build_entry_order
        try:
            ec.build_entry_order = wrapped_build_entry_order
        except Exception:
            pass

        _INSTALLED = True
        logger.warning("[ENTRY ORDER MTF SLOPE FILL] installed")
        return True

    except Exception:
        logger.exception("[ENTRY ORDER MTF SLOPE FILL] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY ORDER MTF SLOPE FILL] auto install failed")


__all__ = ["install"]
