# ============================================================
# File   : trading/summary/persistence/ma_cross_state_summary_patch.py
# Version: Ver01-SAVE-MA-CROSS-STATE-TO-SUMMARY-DB
# ------------------------------------------------------------
# summary DB の stock_summary_1min / 3min / 5min へ、
# ゴールデンクロス/デッドクロス後のMA状態を保存する runtime patch。
#
# 目的:
#   - エントリー直前だけでなく、summary DB にMAクロス状態を残す
#   - 後日の検証/バックテストで、勝敗とMA状態を分析できるようにする
#
# 保存列:
#   ma_cross_state
#   ma_cross_score_delta
#   ma_cross_reasons
#   ma5_above_ma25
#   ma5_below_ma25
#   ma5_above_ma25_bars
#   ma5_below_ma25_bars
#   ma5_ma25_gap_pct
#   ma5_ma25_gap_pct_prev
#   ma5_ma25_gap_widening
#   ma5_ma25_gap_shrinking
#   ma_stack_bullish
#   ma_stack_bearish
#   golden_cross_recent / continuation / mature / exhaustion
#   dead_cross_recent / continuation / mature / exhaustion
#
# 方針:
#   - bulk_upsert_summary() の直前に DataFrame へ列を追加
#   - DBに列が無ければ ALTER TABLE ADD COLUMN で自動追加
#   - 既存保存処理は壊さない
#   - 失敗しても元の保存処理を継続する
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


MA_CROSS_SUMMARY_COLUMNS: dict[str, str] = {
    "ma_cross_state": "TEXT",
    "ma_cross_score_delta": "REAL DEFAULT 0",
    "ma_cross_reasons": "TEXT",
    "ma5_above_ma25": "INTEGER DEFAULT 0",
    "ma5_below_ma25": "INTEGER DEFAULT 0",
    "ma5_above_ma25_bars": "INTEGER DEFAULT 0",
    "ma5_below_ma25_bars": "INTEGER DEFAULT 0",
    "ma5_ma25_gap_pct": "REAL DEFAULT 0",
    "ma5_ma25_gap_pct_prev": "REAL DEFAULT 0",
    "ma5_ma25_gap_widening": "INTEGER DEFAULT 0",
    "ma5_ma25_gap_shrinking": "INTEGER DEFAULT 0",
    "ma25_ma75_gap_pct": "REAL DEFAULT 0",
    "ma25_ma75_gap_pct_prev": "REAL DEFAULT 0",
    "ma25_ma75_gap_widening": "INTEGER DEFAULT 0",
    "ma25_ma75_gap_shrinking": "INTEGER DEFAULT 0",
    "ma_stack_bullish": "INTEGER DEFAULT 0",
    "ma_stack_bearish": "INTEGER DEFAULT 0",
    "golden_cross_recent": "INTEGER DEFAULT 0",
    "golden_cross_continuation": "INTEGER DEFAULT 0",
    "golden_cross_mature": "INTEGER DEFAULT 0",
    "golden_cross_exhaustion": "INTEGER DEFAULT 0",
    "dead_cross_recent": "INTEGER DEFAULT 0",
    "dead_cross_continuation": "INTEGER DEFAULT 0",
    "dead_cross_mature": "INTEGER DEFAULT 0",
    "dead_cross_exhaustion": "INTEGER DEFAULT 0",
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


def _ensure_summary_ma_cross_columns(interval: int) -> None:
    if not _env_bool("SUMMARY_SAVE_MA_CROSS_STATE_SCHEMA_ENABLED", True):
        return

    interval = int(interval)
    table = _summary_table_name(interval)
    schema_key = (interval, table)

    if schema_key in _SCHEMA_DONE:
        return

    engine = _resolve_summary_engine()
    if engine is None:
        logger.warning("[MA CROSS SUMMARY DB] schema skip interval=%s reason=no_engine", interval)
        return

    with _PATCH_LOCK:
        if schema_key in _SCHEMA_DONE:
            return
        try:
            with engine.begin() as conn:
                rows = conn.exec_driver_sql(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
                existing = {str(r[1]).strip() for r in rows if len(r) > 1 and r[1] is not None}
                if not existing:
                    logger.warning("[MA CROSS SUMMARY DB] schema skip interval=%s table=%s reason=table_missing", interval, table)
                    return

                added: list[str] = []
                for col, typ in MA_CROSS_SUMMARY_COLUMNS.items():
                    if col in existing:
                        continue
                    conn.exec_driver_sql(f"ALTER TABLE {_quote_ident(table)} ADD COLUMN {_quote_ident(col)} {typ}")
                    added.append(col)

                if added:
                    logger.warning("[MA CROSS SUMMARY DB] added columns interval=%s table=%s columns=%s", interval, table, added)
                else:
                    logger.debug("[MA CROSS SUMMARY DB] columns already exist interval=%s table=%s", interval, table)

                _SCHEMA_DONE.add(schema_key)

        except Exception:
            logger.exception("[MA CROSS SUMMARY DB] ensure columns failed interval=%s table=%s", interval, table)


def _compute_run_lengths(mask: pd.Series, symbols: pd.Series, datetimes: pd.Series | None) -> pd.Series:
    try:
        tmp = pd.DataFrame({"_symbol": symbols.astype(str), "_mask": mask.fillna(False).astype(bool)})
        if datetimes is not None:
            tmp["_datetime"] = pd.to_datetime(datetimes, errors="coerce")
            tmp["_orig_index"] = mask.index
            tmp = tmp.sort_values(["_symbol", "_datetime", "_orig_index"], kind="stable")
        else:
            tmp["_orig_index"] = mask.index

        out_values: dict[Any, int] = {}
        for _sym, g in tmp.groupby("_symbol", sort=False):
            run = 0
            for idx, val in zip(g["_orig_index"], g["_mask"]):
                if bool(val):
                    run += 1
                else:
                    run = 0
                out_values[idx] = run

        return pd.Series(out_values).reindex(mask.index).fillna(0).astype(int)
    except Exception:
        logger.debug("[MA CROSS SUMMARY DB] run length calc failed", exc_info=True)
        return pd.Series([0] * len(mask), index=mask.index, dtype="int64")


def _shift_prev_by_symbol(values: pd.Series, symbols: pd.Series, datetimes: pd.Series | None) -> pd.Series:
    try:
        tmp = pd.DataFrame({"_symbol": symbols.astype(str), "_value": values})
        if datetimes is not None:
            tmp["_datetime"] = pd.to_datetime(datetimes, errors="coerce")
            tmp["_orig_index"] = values.index
            tmp = tmp.sort_values(["_symbol", "_datetime", "_orig_index"], kind="stable")
        else:
            tmp["_orig_index"] = values.index

        tmp["_prev"] = tmp.groupby("_symbol", sort=False)["_value"].shift(1)
        return tmp.set_index("_orig_index")["_prev"].reindex(values.index).fillna(0.0)
    except Exception:
        logger.debug("[MA CROSS SUMMARY DB] prev calc failed", exc_info=True)
        return pd.Series([0.0] * len(values), index=values.index, dtype="float64")


def _int_bool(mask: pd.Series) -> pd.Series:
    try:
        return mask.fillna(False).astype(bool).astype(int)
    except Exception:
        return pd.Series([0] * len(mask), index=mask.index, dtype="int64")


def attach_ma_cross_state_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or getattr(df, "empty", True):
        return df
    if not _env_bool("SUMMARY_SAVE_MA_CROSS_STATE_ENABLED", True):
        return df

    try:
        out = df.copy()
        idx = out.index

        close = _first_col(out, ("close", "close_price", "price", "current_price", "daily_close"), 0.0)
        ma5 = _first_col(out, ("ma5", "MA5", "ma_5", "daily_ma5", "MA_5"), 0.0)
        ma25 = _first_col(out, ("ma25", "MA25", "ma_25", "daily_ma25", "MA_25"), 0.0)
        ma75 = _first_col(out, ("ma75", "MA75", "ma_75", "daily_ma75", "MA_75"), 0.0)

        valid = (close > 0) & (ma5 > 0) & (ma25 > 0) & (ma75 > 0)
        ma5_above = valid & (ma5 > ma25)
        ma5_below = valid & (ma5 < ma25)
        bullish_stack = valid & (close > ma5) & (ma5 > ma25) & (ma25 > ma75)
        bearish_stack = valid & (close < ma5) & (ma5 < ma25) & (ma25 < ma75)

        symbols = _safe_str_series(out["symbol"] if "symbol" in out.columns else "", idx)
        datetimes = pd.to_datetime(out["datetime"], errors="coerce") if "datetime" in out.columns else None

        if "ma5_above_ma25_bars" in out.columns:
            above_bars = _to_num(out["ma5_above_ma25_bars"]).fillna(0).astype(int)
        else:
            above_bars = _compute_run_lengths(ma5_above, symbols, datetimes)

        if "ma5_below_ma25_bars" in out.columns:
            below_bars = _to_num(out["ma5_below_ma25_bars"]).fillna(0).astype(int)
        else:
            below_bars = _compute_run_lengths(ma5_below, symbols, datetimes)

        gap_5_25 = ((ma5 - ma25).abs() / ma25.replace(0, pd.NA) * 100.0).fillna(0.0)
        gap_25_75 = ((ma25 - ma75).abs() / ma75.replace(0, pd.NA) * 100.0).fillna(0.0)

        if "ma5_ma25_gap_pct_prev" in out.columns:
            gap_5_25_prev = _to_num(out["ma5_ma25_gap_pct_prev"]).fillna(0.0)
        else:
            gap_5_25_prev = _shift_prev_by_symbol(gap_5_25, symbols, datetimes)

        if "ma25_ma75_gap_pct_prev" in out.columns:
            gap_25_75_prev = _to_num(out["ma25_ma75_gap_pct_prev"]).fillna(0.0)
        else:
            gap_25_75_prev = _shift_prev_by_symbol(gap_25_75, symbols, datetimes)

        min_expand = _env_float("ENTRY_MA_CROSS_MIN_GAP_EXPAND_PCT", 0.10)
        min_shrink = _env_float("ENTRY_MA_CROSS_MIN_GAP_SHRINK_PCT", 0.10)
        min_bars = int(_env_float("ENTRY_MA_CROSS_CONTINUATION_MIN_BARS", 3.0))
        mature_bars = int(_env_float("ENTRY_MA_CROSS_MATURE_BARS", 30.0))
        max_gap = _env_float("ENTRY_MA_CROSS_MAX_GAP_PCT", 2.5)

        gap_5_25_widening = (gap_5_25_prev > 0) & (gap_5_25 >= gap_5_25_prev * (1.0 + min_expand))
        gap_5_25_shrinking = (gap_5_25_prev > 0) & (gap_5_25 <= gap_5_25_prev * (1.0 - min_shrink))
        gap_25_75_widening = (gap_25_75_prev > 0) & (gap_25_75 >= gap_25_75_prev * (1.0 + min_expand))
        gap_25_75_shrinking = (gap_25_75_prev > 0) & (gap_25_75 <= gap_25_75_prev * (1.0 - min_shrink))

        golden_recent = ma5_above & (above_bars > 0) & (above_bars < min_bars)
        golden_cont = ma5_above & (above_bars >= min_bars)
        golden_mature = ma5_above & ((above_bars >= mature_bars) | (gap_5_25 >= max_gap))
        golden_exhaust = ma5_above & gap_5_25_shrinking

        dead_recent = ma5_below & (below_bars > 0) & (below_bars < min_bars)
        dead_cont = ma5_below & (below_bars >= min_bars)
        dead_mature = ma5_below & ((below_bars >= mature_bars) | (gap_5_25 >= max_gap))
        dead_exhaust = ma5_below & gap_5_25_shrinking

        state = pd.Series("neutral", index=idx, dtype="object")
        state = state.mask(golden_recent, "golden_cross_recent")
        state = state.mask(golden_cont, "golden_cross_continuation")
        state = state.mask(bullish_stack & golden_cont, "bullish_stack_continuation")
        state = state.mask(golden_mature, "golden_cross_mature")
        state = state.mask(golden_exhaust, "golden_cross_exhaustion")
        state = state.mask(dead_recent, "dead_cross_recent")
        state = state.mask(dead_cont, "dead_cross_continuation")
        state = state.mask(bearish_stack & dead_cont, "bearish_stack_continuation")
        state = state.mask(dead_mature, "dead_cross_mature")
        state = state.mask(dead_exhaust, "dead_cross_exhaustion")
        state = state.mask(~valid, "ma_missing")

        score_delta = pd.Series(0.0, index=idx, dtype="float64")
        score_delta += golden_recent.astype(float) * 0.5
        score_delta += golden_cont.astype(float) * 0.8
        score_delta += bullish_stack.astype(float) * 0.9
        score_delta += gap_5_25_widening.astype(float) * 0.4
        score_delta -= golden_exhaust.astype(float) * 0.6
        score_delta -= golden_mature.astype(float) * 0.5

        score_delta -= dead_recent.astype(float) * 0.5
        score_delta -= dead_cont.astype(float) * 0.8
        score_delta -= bearish_stack.astype(float) * 0.9
        score_delta -= gap_5_25_widening.astype(float) * ma5_below.astype(float) * 0.4
        score_delta += dead_exhaust.astype(float) * 0.6
        score_delta += dead_mature.astype(float) * 0.5

        cap = abs(_env_float("ENTRY_MA_CROSS_STATE_MAX_SCORE_DELTA", 2.0))
        score_delta = score_delta.clip(lower=-cap, upper=cap)

        reasons = pd.Series("", index=idx, dtype="object")
        reasons = reasons.mask(golden_recent, reasons + ",golden_recent")
        reasons = reasons.mask(golden_cont, reasons + ",golden_continuation")
        reasons = reasons.mask(bullish_stack, reasons + ",bullish_stack")
        reasons = reasons.mask(golden_mature, reasons + ",golden_mature")
        reasons = reasons.mask(golden_exhaust, reasons + ",golden_exhaustion")
        reasons = reasons.mask(dead_recent, reasons + ",dead_recent")
        reasons = reasons.mask(dead_cont, reasons + ",dead_continuation")
        reasons = reasons.mask(bearish_stack, reasons + ",bearish_stack")
        reasons = reasons.mask(dead_mature, reasons + ",dead_mature")
        reasons = reasons.mask(dead_exhaust, reasons + ",dead_exhaustion")
        reasons = reasons.str.strip(",")

        out["ma_cross_state"] = state
        out["ma_cross_score_delta"] = score_delta
        out["ma_cross_reasons"] = reasons
        out["ma5_above_ma25"] = _int_bool(ma5_above)
        out["ma5_below_ma25"] = _int_bool(ma5_below)
        out["ma5_above_ma25_bars"] = above_bars.astype(int)
        out["ma5_below_ma25_bars"] = below_bars.astype(int)
        out["ma5_ma25_gap_pct"] = gap_5_25.astype(float)
        out["ma5_ma25_gap_pct_prev"] = gap_5_25_prev.astype(float)
        out["ma5_ma25_gap_widening"] = _int_bool(gap_5_25_widening)
        out["ma5_ma25_gap_shrinking"] = _int_bool(gap_5_25_shrinking)
        out["ma25_ma75_gap_pct"] = gap_25_75.astype(float)
        out["ma25_ma75_gap_pct_prev"] = gap_25_75_prev.astype(float)
        out["ma25_ma75_gap_widening"] = _int_bool(gap_25_75_widening)
        out["ma25_ma75_gap_shrinking"] = _int_bool(gap_25_75_shrinking)
        out["ma_stack_bullish"] = _int_bool(bullish_stack)
        out["ma_stack_bearish"] = _int_bool(bearish_stack)
        out["golden_cross_recent"] = _int_bool(golden_recent)
        out["golden_cross_continuation"] = _int_bool(golden_cont)
        out["golden_cross_mature"] = _int_bool(golden_mature)
        out["golden_cross_exhaustion"] = _int_bool(golden_exhaust)
        out["dead_cross_recent"] = _int_bool(dead_recent)
        out["dead_cross_continuation"] = _int_bool(dead_cont)
        out["dead_cross_mature"] = _int_bool(dead_mature)
        out["dead_cross_exhaustion"] = _int_bool(dead_exhaust)

        logger.info(
            "[MA CROSS SUMMARY DB] attached rows=%s valid=%s golden_cont=%s dead_cont=%s bullish_stack=%s bearish_stack=%s",
            len(out),
            int(valid.sum()),
            int(golden_cont.sum()),
            int(dead_cont.sum()),
            int(bullish_stack.sum()),
            int(bearish_stack.sum()),
        )
        return out

    except Exception:
        logger.exception("[MA CROSS SUMMARY DB] attach failed -> keep original df")
        return df


def _prepare_df(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    try:
        _ensure_summary_ma_cross_columns(int(interval))
    except Exception:
        logger.debug("[MA CROSS SUMMARY DB] schema ensure wrapper failed", exc_info=True)
    return attach_ma_cross_state_columns(df)


def _patched_bulk_upsert_summary(df: pd.DataFrame, interval: int, *args, **kwargs):
    if not callable(_ORIG_BULK_UPSERT_SUMMARY):
        return 0
    try:
        df2 = _prepare_df(df, int(interval))
    except Exception:
        logger.exception("[MA CROSS SUMMARY DB] prepare failed bulk -> keep original df")
        df2 = df
    return _ORIG_BULK_UPSERT_SUMMARY(df2, interval, *args, **kwargs)


def _patched_save_summary_bulk(df: pd.DataFrame, interval: int, *args, **kwargs):
    if not callable(_ORIG_SAVE_SUMMARY_BULK):
        return _patched_bulk_upsert_summary(df, interval, *args, **kwargs)
    try:
        df2 = _prepare_df(df, int(interval))
    except Exception:
        logger.exception("[MA CROSS SUMMARY DB] prepare failed save_bulk -> keep original df")
        df2 = df
    return _ORIG_SAVE_SUMMARY_BULK(df2, interval, *args, **kwargs)


def _patched_save_summary_df(df: pd.DataFrame, interval: int, *args, **kwargs):
    if not callable(_ORIG_SAVE_SUMMARY_DF):
        return _patched_bulk_upsert_summary(df, interval, *args, **kwargs)
    try:
        df2 = _prepare_df(df, int(interval))
    except Exception:
        logger.exception("[MA CROSS SUMMARY DB] prepare failed save_df -> keep original df")
        df2 = df
    return _ORIG_SAVE_SUMMARY_DF(df2, interval, *args, **kwargs)


def install_ma_cross_state_summary_patch() -> bool:
    global _INSTALLED, _ORIG_BULK_UPSERT_SUMMARY, _ORIG_SAVE_SUMMARY_BULK, _ORIG_SAVE_SUMMARY_DF
    with _PATCH_LOCK:
        try:
            import trading.summary.persistence.summary_saver_bulk as saver

            cur = getattr(saver, "bulk_upsert_summary", None)
            if getattr(cur, "_ma_cross_state_summary_patch", False):
                _INSTALLED = True
                return True
            if not callable(cur):
                logger.error("[MA CROSS SUMMARY DB] target bulk_upsert_summary unavailable")
                return False

            _ORIG_BULK_UPSERT_SUMMARY = cur
            _ORIG_SAVE_SUMMARY_BULK = getattr(saver, "save_summary_bulk", None)
            _ORIG_SAVE_SUMMARY_DF = getattr(saver, "save_summary_df", None)

            _patched_bulk_upsert_summary._ma_cross_state_summary_patch = True  # type: ignore[attr-defined]
            _patched_save_summary_bulk._ma_cross_state_summary_patch = True  # type: ignore[attr-defined]
            _patched_save_summary_df._ma_cross_state_summary_patch = True  # type: ignore[attr-defined]

            saver.bulk_upsert_summary = _patched_bulk_upsert_summary
            saver.save_summary_bulk = _patched_save_summary_bulk
            saver.save_summary_df = _patched_save_summary_df
            _INSTALLED = True

            logger.warning(
                "[MA CROSS SUMMARY DB] patch installed enabled=%s schema_enabled=%s columns=%s",
                _env_bool("SUMMARY_SAVE_MA_CROSS_STATE_ENABLED", True),
                _env_bool("SUMMARY_SAVE_MA_CROSS_STATE_SCHEMA_ENABLED", True),
                list(MA_CROSS_SUMMARY_COLUMNS.keys()),
            )
            return True

        except Exception:
            logger.exception("[MA CROSS SUMMARY DB] install failed")
            return False


try:
    install_ma_cross_state_summary_patch()
except Exception:
    logger.exception("[MA CROSS SUMMARY DB] auto install failed")


__all__ = [
    "install_ma_cross_state_summary_patch",
    "attach_ma_cross_state_columns",
    "MA_CROSS_SUMMARY_COLUMNS",
]
