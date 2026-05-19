# ============================================================
# File   : trading/summary/persistence/vwap_state_summary_patch.py
# Version: Ver01-SAVE-VWAP-STATE-TO-SUMMARY-DB
# ------------------------------------------------------------
# summary DB の stock_summary_1min / 3min / 5min へ、
# VWAP上/下の継続状態を保存する runtime patch。
#
# 保存列:
#   vwap_state
#   vwap_score_delta
#   vwap_reasons
#   price_above_vwap
#   price_below_vwap
#   price_above_vwap_bars
#   price_below_vwap_bars
#   vwap_gap_pct
#   vwap_gap_pct_prev
#   vwap_gap_widening
#   vwap_gap_shrinking
#   above_vwap_recent / continuation / mature / exhaustion
#   below_vwap_recent / continuation / mature / exhaustion
# ============================================================

from __future__ import annotations

import logging
import os
import threading
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_INSTALLED = False
_PATCH_LOCK = threading.RLock()
_ORIG_BULK_UPSERT_SUMMARY = None
_ORIG_SAVE_SUMMARY_BULK = None
_ORIG_SAVE_SUMMARY_DF = None
_SCHEMA_DONE: set[tuple[int, str]] = set()


VWAP_SUMMARY_COLUMNS: dict[str, str] = {
    "vwap_state": "TEXT",
    "vwap_score_delta": "REAL DEFAULT 0",
    "vwap_reasons": "TEXT",
    "price_above_vwap": "INTEGER DEFAULT 0",
    "price_below_vwap": "INTEGER DEFAULT 0",
    "price_above_vwap_bars": "INTEGER DEFAULT 0",
    "price_below_vwap_bars": "INTEGER DEFAULT 0",
    "vwap_gap_pct": "REAL DEFAULT 0",
    "vwap_gap_pct_prev": "REAL DEFAULT 0",
    "vwap_gap_widening": "INTEGER DEFAULT 0",
    "vwap_gap_shrinking": "INTEGER DEFAULT 0",
    "above_vwap_recent": "INTEGER DEFAULT 0",
    "above_vwap_continuation": "INTEGER DEFAULT 0",
    "above_vwap_mature": "INTEGER DEFAULT 0",
    "above_vwap_exhaustion": "INTEGER DEFAULT 0",
    "below_vwap_recent": "INTEGER DEFAULT 0",
    "below_vwap_continuation": "INTEGER DEFAULT 0",
    "below_vwap_mature": "INTEGER DEFAULT 0",
    "below_vwap_exhaustion": "INTEGER DEFAULT 0",
}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _to_num(s: Any) -> pd.Series:
    try:
        if isinstance(s, pd.Series):
            return pd.to_numeric(s, errors="coerce")
        return pd.to_numeric(pd.Series(s), errors="coerce")
    except Exception:
        return pd.Series(dtype="float64")


def _first_col(df: pd.DataFrame, names: tuple[str, ...], default: float = 0.0) -> pd.Series:
    for name in names:
        if name in df.columns:
            return _to_num(df[name])
    return pd.Series([default] * len(df), index=df.index, dtype="float64")


def _safe_str_series(s: Any, index) -> pd.Series:
    try:
        return pd.Series(s, index=index).astype(str)
    except Exception:
        return pd.Series([""] * len(index), index=index)


def _summary_table_name(interval: int) -> str:
    return f"stock_summary_{int(interval)}min"


def _resolve_summary_engine():
    candidates = []
    try:
        from database.session import get_summary_engine
        candidates.append(get_summary_engine)
    except Exception:
        pass
    try:
        from database.session import summary_engine
        candidates.append(lambda: summary_engine)
    except Exception:
        pass
    try:
        from database.session import Session_summary
        candidates.append(lambda: getattr(Session_summary, "bind", None))
    except Exception:
        pass
    try:
        from trading.summary.persistence.core import upsert_engine as ue
        candidates.append(lambda: getattr(ue, "summary_engine", None))
        candidates.append(lambda: getattr(ue, "engine", None))
    except Exception:
        pass
    for resolver in candidates:
        try:
            engine = resolver()
            if engine is not None:
                return engine
        except Exception:
            continue
    return None


def _quote_ident(name: str) -> str:
    safe = str(name).replace('"', '""')
    return f'"{safe}"'


def _ensure_summary_vwap_columns(interval: int) -> None:
    if not _env_bool("SUMMARY_SAVE_VWAP_STATE_SCHEMA_ENABLED", True):
        return
    interval = int(interval)
    table = _summary_table_name(interval)
    schema_key = (interval, table)
    if schema_key in _SCHEMA_DONE:
        return
    engine = _resolve_summary_engine()
    if engine is None:
        logger.warning("[VWAP SUMMARY DB] schema skip interval=%s reason=no_engine", interval)
        return
    with _PATCH_LOCK:
        if schema_key in _SCHEMA_DONE:
            return
        try:
            with engine.begin() as conn:
                rows = conn.exec_driver_sql(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
                existing = {str(r[1]).strip() for r in rows if len(r) > 1 and r[1] is not None}
                if not existing:
                    logger.warning("[VWAP SUMMARY DB] schema skip interval=%s table=%s reason=table_missing", interval, table)
                    return
                added: list[str] = []
                for col, typ in VWAP_SUMMARY_COLUMNS.items():
                    if col in existing:
                        continue
                    conn.exec_driver_sql(f"ALTER TABLE {_quote_ident(table)} ADD COLUMN {_quote_ident(col)} {typ}")
                    added.append(col)
                if added:
                    logger.warning("[VWAP SUMMARY DB] added columns interval=%s table=%s columns=%s", interval, table, added)
                _SCHEMA_DONE.add(schema_key)
        except Exception:
            logger.exception("[VWAP SUMMARY DB] ensure columns failed interval=%s table=%s", interval, table)


def _compute_run_lengths(mask: pd.Series, symbols: pd.Series, datetimes: pd.Series | None) -> pd.Series:
    try:
        tmp = pd.DataFrame({"_symbol": symbols.astype(str), "_mask": mask.fillna(False).astype(bool), "_orig_index": mask.index})
        if datetimes is not None:
            tmp["_datetime"] = pd.to_datetime(datetimes, errors="coerce")
            tmp = tmp.sort_values(["_symbol", "_datetime", "_orig_index"], kind="stable")
        out_values: dict[Any, int] = {}
        for _sym, g in tmp.groupby("_symbol", sort=False):
            run = 0
            for idx, val in zip(g["_orig_index"], g["_mask"]):
                run = run + 1 if bool(val) else 0
                out_values[idx] = run
        return pd.Series(out_values).reindex(mask.index).fillna(0).astype(int)
    except Exception:
        logger.debug("[VWAP SUMMARY DB] run length calc failed", exc_info=True)
        return pd.Series([0] * len(mask), index=mask.index, dtype="int64")


def _shift_prev_by_symbol(values: pd.Series, symbols: pd.Series, datetimes: pd.Series | None) -> pd.Series:
    try:
        tmp = pd.DataFrame({"_symbol": symbols.astype(str), "_value": values, "_orig_index": values.index})
        if datetimes is not None:
            tmp["_datetime"] = pd.to_datetime(datetimes, errors="coerce")
            tmp = tmp.sort_values(["_symbol", "_datetime", "_orig_index"], kind="stable")
        tmp["_prev"] = tmp.groupby("_symbol", sort=False)["_value"].shift(1)
        return tmp.set_index("_orig_index")["_prev"].reindex(values.index).fillna(0.0)
    except Exception:
        logger.debug("[VWAP SUMMARY DB] prev calc failed", exc_info=True)
        return pd.Series([0.0] * len(values), index=values.index, dtype="float64")


def _int_bool(mask: pd.Series) -> pd.Series:
    try:
        return mask.fillna(False).astype(bool).astype(int)
    except Exception:
        return pd.Series([0] * len(mask), index=mask.index, dtype="int64")


def attach_vwap_state_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or getattr(df, "empty", True):
        return df
    if not _env_bool("SUMMARY_SAVE_VWAP_STATE_ENABLED", True):
        return df
    try:
        out = df.copy()
        idx = out.index
        close = _first_col(out, ("close", "close_price", "price", "current_price"), 0.0)
        vwap = _first_col(out, ("vwap", "VWAP", "vwap_price", "session_vwap"), 0.0)
        valid = (close > 0) & (vwap > 0)
        above = valid & (close > vwap)
        below = valid & (close < vwap)
        symbols = _safe_str_series(out["symbol"] if "symbol" in out.columns else "", idx)
        datetimes = pd.to_datetime(out["datetime"], errors="coerce") if "datetime" in out.columns else None

        if "price_above_vwap_bars" in out.columns:
            above_bars = _to_num(out["price_above_vwap_bars"]).fillna(0).astype(int)
        else:
            above_bars = _compute_run_lengths(above, symbols, datetimes)
        if "price_below_vwap_bars" in out.columns:
            below_bars = _to_num(out["price_below_vwap_bars"]).fillna(0).astype(int)
        else:
            below_bars = _compute_run_lengths(below, symbols, datetimes)

        gap = ((close - vwap).abs() / vwap.replace(0, pd.NA) * 100.0).fillna(0.0)
        gap_prev = _to_num(out["vwap_gap_pct_prev"]).fillna(0.0) if "vwap_gap_pct_prev" in out.columns else _shift_prev_by_symbol(gap, symbols, datetimes)

        min_expand = _env_float("ENTRY_VWAP_MIN_GAP_EXPAND_PCT", 0.08)
        min_shrink = _env_float("ENTRY_VWAP_MIN_GAP_SHRINK_PCT", 0.08)
        min_bars = int(_env_float("ENTRY_VWAP_CONTINUATION_MIN_BARS", 3.0))
        mature_bars = int(_env_float("ENTRY_VWAP_MATURE_BARS", 20.0))
        max_gap = _env_float("ENTRY_VWAP_MAX_GAP_PCT", 1.20)

        widening = (gap_prev > 0) & (gap >= gap_prev * (1.0 + min_expand))
        shrinking = (gap_prev > 0) & (gap <= gap_prev * (1.0 - min_shrink))

        above_recent = above & (above_bars > 0) & (above_bars < min_bars)
        above_cont = above & (above_bars >= min_bars)
        above_mature = above & ((above_bars >= mature_bars) | (gap >= max_gap))
        above_exhaust = above & shrinking
        below_recent = below & (below_bars > 0) & (below_bars < min_bars)
        below_cont = below & (below_bars >= min_bars)
        below_mature = below & ((below_bars >= mature_bars) | (gap >= max_gap))
        below_exhaust = below & shrinking

        state = pd.Series("neutral", index=idx, dtype="object")
        state = state.mask(above_recent, "above_vwap_recent")
        state = state.mask(above_cont, "above_vwap_continuation")
        state = state.mask(above_mature, "above_vwap_mature")
        state = state.mask(above_exhaust, "above_vwap_exhaustion")
        state = state.mask(below_recent, "below_vwap_recent")
        state = state.mask(below_cont, "below_vwap_continuation")
        state = state.mask(below_mature, "below_vwap_mature")
        state = state.mask(below_exhaust, "below_vwap_exhaustion")
        state = state.mask(~valid, "vwap_missing")

        score_delta = pd.Series(0.0, index=idx, dtype="float64")
        score_delta += above_recent.astype(float) * 0.5
        score_delta += above_cont.astype(float) * 0.8
        score_delta += (above & widening).astype(float) * 0.4
        score_delta -= above_exhaust.astype(float) * 0.5
        score_delta -= above_mature.astype(float) * 0.5
        score_delta -= below_recent.astype(float) * 0.5
        score_delta -= below_cont.astype(float) * 0.8
        score_delta -= (below & widening).astype(float) * 0.4
        score_delta += below_exhaust.astype(float) * 0.5
        score_delta += below_mature.astype(float) * 0.5
        cap = abs(_env_float("ENTRY_VWAP_STATE_MAX_SCORE_DELTA", 1.5))
        score_delta = score_delta.clip(lower=-cap, upper=cap)

        reasons = pd.Series("", index=idx, dtype="object")
        reasons = reasons.mask(above_recent, reasons + ",above_recent")
        reasons = reasons.mask(above_cont, reasons + ",above_continuation")
        reasons = reasons.mask(above_mature, reasons + ",above_mature")
        reasons = reasons.mask(above_exhaust, reasons + ",above_exhaustion")
        reasons = reasons.mask(below_recent, reasons + ",below_recent")
        reasons = reasons.mask(below_cont, reasons + ",below_continuation")
        reasons = reasons.mask(below_mature, reasons + ",below_mature")
        reasons = reasons.mask(below_exhaust, reasons + ",below_exhaustion")
        reasons = reasons.mask(widening, reasons + ",vwap_gap_widening")
        reasons = reasons.mask(shrinking, reasons + ",vwap_gap_shrinking")
        reasons = reasons.str.strip(",")

        out["vwap_state"] = state
        out["vwap_score_delta"] = score_delta
        out["vwap_reasons"] = reasons
        out["price_above_vwap"] = _int_bool(above)
        out["price_below_vwap"] = _int_bool(below)
        out["price_above_vwap_bars"] = above_bars.astype(int)
        out["price_below_vwap_bars"] = below_bars.astype(int)
        out["vwap_gap_pct"] = gap.astype(float)
        out["vwap_gap_pct_prev"] = gap_prev.astype(float)
        out["vwap_gap_widening"] = _int_bool(widening)
        out["vwap_gap_shrinking"] = _int_bool(shrinking)
        out["above_vwap_recent"] = _int_bool(above_recent)
        out["above_vwap_continuation"] = _int_bool(above_cont)
        out["above_vwap_mature"] = _int_bool(above_mature)
        out["above_vwap_exhaustion"] = _int_bool(above_exhaust)
        out["below_vwap_recent"] = _int_bool(below_recent)
        out["below_vwap_continuation"] = _int_bool(below_cont)
        out["below_vwap_mature"] = _int_bool(below_mature)
        out["below_vwap_exhaustion"] = _int_bool(below_exhaust)

        logger.info(
            "[VWAP SUMMARY DB] attached rows=%s valid=%s above_cont=%s below_cont=%s",
            len(out), int(valid.sum()), int(above_cont.sum()), int(below_cont.sum()),
        )
        return out
    except Exception:
        logger.exception("[VWAP SUMMARY DB] attach failed -> keep original df")
        return df


def _prepare_df(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    try:
        _ensure_summary_vwap_columns(int(interval))
    except Exception:
        logger.debug("[VWAP SUMMARY DB] schema ensure wrapper failed", exc_info=True)
    return attach_vwap_state_columns(df)


def _patched_bulk_upsert_summary(df: pd.DataFrame, interval: int, *args, **kwargs):
    if not callable(_ORIG_BULK_UPSERT_SUMMARY):
        return 0
    try:
        df2 = _prepare_df(df, int(interval))
    except Exception:
        logger.exception("[VWAP SUMMARY DB] prepare failed bulk -> keep original df")
        df2 = df
    return _ORIG_BULK_UPSERT_SUMMARY(df2, interval, *args, **kwargs)


def _patched_save_summary_bulk(df: pd.DataFrame, interval: int, *args, **kwargs):
    if not callable(_ORIG_SAVE_SUMMARY_BULK):
        return _patched_bulk_upsert_summary(df, interval, *args, **kwargs)
    try:
        df2 = _prepare_df(df, int(interval))
    except Exception:
        logger.exception("[VWAP SUMMARY DB] prepare failed save_bulk -> keep original df")
        df2 = df
    return _ORIG_SAVE_SUMMARY_BULK(df2, interval, *args, **kwargs)


def _patched_save_summary_df(df: pd.DataFrame, interval: int, *args, **kwargs):
    if not callable(_ORIG_SAVE_SUMMARY_DF):
        return _patched_bulk_upsert_summary(df, interval, *args, **kwargs)
    try:
        df2 = _prepare_df(df, int(interval))
    except Exception:
        logger.exception("[VWAP SUMMARY DB] prepare failed save_df -> keep original df")
        df2 = df
    return _ORIG_SAVE_SUMMARY_DF(df2, interval, *args, **kwargs)


def install_vwap_state_summary_patch() -> bool:
    global _INSTALLED, _ORIG_BULK_UPSERT_SUMMARY, _ORIG_SAVE_SUMMARY_BULK, _ORIG_SAVE_SUMMARY_DF
    with _PATCH_LOCK:
        try:
            import trading.summary.persistence.summary_saver_bulk as saver
            cur = getattr(saver, "bulk_upsert_summary", None)
            if getattr(cur, "_vwap_state_summary_patch", False):
                _INSTALLED = True
                return True
            if not callable(cur):
                logger.error("[VWAP SUMMARY DB] target bulk_upsert_summary unavailable")
                return False
            _ORIG_BULK_UPSERT_SUMMARY = cur
            _ORIG_SAVE_SUMMARY_BULK = getattr(saver, "save_summary_bulk", None)
            _ORIG_SAVE_SUMMARY_DF = getattr(saver, "save_summary_df", None)
            _patched_bulk_upsert_summary._vwap_state_summary_patch = True  # type: ignore[attr-defined]
            _patched_save_summary_bulk._vwap_state_summary_patch = True  # type: ignore[attr-defined]
            _patched_save_summary_df._vwap_state_summary_patch = True  # type: ignore[attr-defined]
            saver.bulk_upsert_summary = _patched_bulk_upsert_summary
            saver.save_summary_bulk = _patched_save_summary_bulk
            saver.save_summary_df = _patched_save_summary_df
            _INSTALLED = True
            logger.warning(
                "[VWAP SUMMARY DB] patch installed enabled=%s schema_enabled=%s columns=%s",
                _env_bool("SUMMARY_SAVE_VWAP_STATE_ENABLED", True),
                _env_bool("SUMMARY_SAVE_VWAP_STATE_SCHEMA_ENABLED", True),
                list(VWAP_SUMMARY_COLUMNS.keys()),
            )
            return True
        except Exception:
            logger.exception("[VWAP SUMMARY DB] install failed")
            return False


try:
    install_vwap_state_summary_patch()
except Exception:
    logger.exception("[VWAP SUMMARY DB] auto install failed")


__all__ = [
    "install_vwap_state_summary_patch",
    "attach_vwap_state_columns",
    "VWAP_SUMMARY_COLUMNS",
]
