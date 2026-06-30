# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_mtf_duplicate_datetime_guard_patch.py
# Version: V1.1-DUPLICATE-DATETIME-MERGE-GUARD-MAIN-WRITER-OPTIONAL
# ------------------------------------------------------------
# Purpose:
#   1) Prevent pandas merge failure in summary_mtf_diff_from_1m_patch:
#          ValueError: The column label 'datetime' is not unique.
#
#      This happens after _bucket_dt is renamed to datetime while the original
#      1m datetime column is still present.  For repair-source rows, the merge
#      key must be the bucket-end datetime, so duplicated labels keep the last
#      datetime column.
#
#   2) In main.py, do not require the PUSH DB writer to be alive for Summary-AI.
#      main.py is the entry/judgement process; main_database.py owns DB saving.
#      Safety still requires fresh 1m PUSH context in main.py.
# ============================================================
from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1.1-DUPLICATE-DATETIME-MERGE-GUARD-MAIN-WRITER-OPTIONAL"
_INSTALLED = False
_SUMMARY_AI_MAIN_WRITER_OPTIONAL_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _is_database_collector_context() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(
            x in argv
            for x in (
                "main_database.py",
                "db_prepare_runner.py",
                "ranking_collector_runner.py",
                "push_receiver_runner.py",
                "yahoo_complement_runner.py",
                "summary_database_runner.py",
                "data_collectors_runner.py",
            )
        ):
            return True
        return any(
            os.getenv(k) == "1"
            for k in (
                "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
                "AUTOSTOCK_MAIN_DATABASE_PROCESS",
                "AUTOSTOCK_SUMMARY_DB_WRITER",
                "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
            )
        )
    except Exception:
        return False


def _is_main_py_process() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        return "main.py" in argv and not _is_database_collector_context()
    except Exception:
        return False


def _dedupe_columns(df: Any, *, keep: str = "last") -> Any:
    try:
        import pandas as pd
        if not isinstance(df, pd.DataFrame) or df.empty:
            return df
        if not df.columns.has_duplicates:
            return df
        dupes = [str(c) for c in df.columns[df.columns.duplicated(keep=False)].unique()]
        out = df.loc[:, ~df.columns.duplicated(keep=keep)].copy()
        logger.warning(
            "[SUMMARY MTF DUPLICATE DATETIME GUARD] duplicate columns dropped keep=%s dupes=%s before_cols=%s after_cols=%s",
            keep,
            dupes,
            len(df.columns),
            len(out.columns),
        )
        return out
    except Exception:
        logger.exception("[SUMMARY MTF DUPLICATE DATETIME GUARD] dedupe failed")
        return df


def _safe_repair_mtf_from_1m(hist: Any, one: Any, *, interval: int):
    import pandas as pd
    import core.startup.summary_mtf_diff_from_1m_patch as target

    out = hist.copy() if isinstance(hist, pd.DataFrame) else pd.DataFrame()
    one_df = one.copy() if isinstance(one, pd.DataFrame) else pd.DataFrame()
    out = _dedupe_columns(out, keep="last")
    one_df = _dedupe_columns(one_df, keep="last")
    if out.empty or one_df.empty:
        return out
    try:
        if "symbol" not in out.columns or "datetime" not in out.columns or "symbol" not in one_df.columns or "datetime" not in one_df.columns:
            return out

        interval_i = int(interval)
        one2 = one_df.copy()
        one2["datetime"] = pd.to_datetime(one2["datetime"], errors="coerce")
        one2 = one2.dropna(subset=["datetime", "symbol"])
        if one2.empty:
            return out

        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out.dropna(subset=["datetime", "symbol"])
        if out.empty:
            return out

        one2["_bucket_dt"] = one2["datetime"].dt.floor(f"{interval_i}min") + pd.Timedelta(minutes=interval_i)
        one2 = one2.sort_values(["symbol", "_bucket_dt", "datetime"], kind="stable")
        latest_1m = one2.groupby(["symbol", "_bucket_dt"], as_index=False).tail(1).copy()
        latest_1m = latest_1m.rename(columns={"_bucket_dt": "datetime"})
        latest_1m = _dedupe_columns(latest_1m, keep="last")

        repair_cols = [
            "score", "score_total", "final_score", "display_score", "combined_score",
            "score_buy", "buy_score", "score_sell", "sell_score",
            "slope", "slope_atr_scaled", "score_slope",
            "rsi", "macd", "signal", "hist",
            "mtf", "score_mtf", "mtf_score",
            "atr", "atr_1m", "atr_3m", "atr_5m",
            "technical_ready", "display_ready", "usable_ready",
        ]
        repair_cols = [c for c in repair_cols if c in latest_1m.columns]
        if not repair_cols:
            return out

        src = latest_1m[["symbol", "datetime"] + repair_cols].copy()
        src = _dedupe_columns(src, keep="last")
        if src.columns.has_duplicates or out.columns.has_duplicates:
            logger.warning(
                "[SUMMARY MTF DUPLICATE DATETIME GUARD] merge skipped because duplicate columns remain out_dupes=%s src_dupes=%s",
                bool(out.columns.has_duplicates),
                bool(src.columns.has_duplicates),
            )
            return out

        merged = out.merge(src, on=["symbol", "datetime"], how="left", suffixes=("", "_1mrepair"))

        repaired_cols: dict[str, int] = {}
        for col in repair_cols:
            rcol = f"{col}_1mrepair"
            if rcol not in merged.columns:
                continue
            before_nonzero = target._nonzero_count(merged, col) if col in merged.columns else 0
            if col not in merged.columns:
                merged[col] = merged[rcol]
            else:
                if col in {"technical_ready", "display_ready", "usable_ready"}:
                    mask = merged[col].isna() | (merged[col].astype(str).str.lower().isin({"false", "0", "nan", "none", ""}))
                    merged.loc[mask, col] = merged.loc[mask, rcol]
                else:
                    cur = pd.to_numeric(merged[col], errors="coerce")
                    rep = pd.to_numeric(merged[rcol], errors="coerce")
                    mask = (cur.isna() | (cur.fillna(0.0).abs() == 0.0)) & rep.notna() & (rep.fillna(0.0).abs() > 0.0)
                    merged.loc[mask, col] = rep[mask]
            after_nonzero = target._nonzero_count(merged, col)
            if after_nonzero > before_nonzero:
                repaired_cols[col] = after_nonzero - before_nonzero
            merged = merged.drop(columns=[rcol], errors="ignore")

        if "score_total" in merged.columns:
            for alias in ("score", "final_score", "display_score", "combined_score"):
                if alias not in merged.columns or target._nonzero_count(merged, alias) == 0:
                    merged[alias] = merged["score_total"]
        if "score_buy" in merged.columns and ("buy_score" not in merged.columns or target._nonzero_count(merged, "buy_score") == 0):
            merged["buy_score"] = merged["score_buy"]
        if "score_sell" in merged.columns and ("sell_score" not in merged.columns or target._nonzero_count(merged, "sell_score") == 0):
            merged["sell_score"] = merged["score_sell"]

        vol_source = target._best_numeric_column(one2, ["volume", "vol", "trading_volume", "出来高"])
        if vol_source is not None:
            vol_sum = one2.groupby(["symbol", "_bucket_dt"], as_index=False)[vol_source].sum().rename(columns={"_bucket_dt": "datetime", vol_source: "_bucket_volume_repair"})
            vol_sum = _dedupe_columns(vol_sum, keep="last")
            merged = merged.merge(vol_sum, on=["symbol", "datetime"], how="left")
            cur_vol = target._numeric_series(merged, "volume", 0.0)
            rep_vol = pd.to_numeric(merged["_bucket_volume_repair"], errors="coerce")
            mask = (cur_vol.abs() == 0.0) & rep_vol.notna() & (rep_vol.fillna(0.0).abs() > 0.0)
            if "volume" not in merged.columns:
                merged["volume"] = 0.0
            merged.loc[mask, "volume"] = rep_vol[mask]
            merged = merged.drop(columns=["_bucket_volume_repair"], errors="ignore")

        if repaired_cols or target._nonzero_count(out, "volume") == 0 < target._nonzero_count(merged, "volume"):
            logger.warning(
                "[SUMMARY MTF DUPLICATE DATETIME GUARD] mtf repaired from 1m interval=%s rows=%s repaired_cols=%s score_nonzero=%s volume_nonzero=%s",
                interval_i,
                len(merged),
                repaired_cols,
                target._nonzero_count(merged, "score_total"),
                target._nonzero_count(merged, "volume"),
            )
        return merged.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY MTF DUPLICATE DATETIME GUARD] safe repair failed interval=%s", interval)
        return out


def _install_summary_ai_main_writer_optional() -> bool:
    global _SUMMARY_AI_MAIN_WRITER_OPTIONAL_INSTALLED
    if _SUMMARY_AI_MAIN_WRITER_OPTIONAL_INSTALLED:
        return True
    if not _env_bool("SUMMARY_AI_MAIN_PUSH_WRITER_OPTIONAL", True):
        logger.warning("[SUMMARY AI MAIN WRITER OPTIONAL] disabled by env")
        return False
    try:
        import core.startup.summary_ai_candidate_refill_patch as guard
        cur = getattr(guard, "_push_writer_state", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI MAIN WRITER OPTIONAL] target missing")
            return False
        if getattr(cur, "_main_writer_optional_v1", False):
            _SUMMARY_AI_MAIN_WRITER_OPTIONAL_INSTALLED = True
            return True
        orig = getattr(cur, "_original", cur)

        def _patched_push_writer_state():
            if _is_main_py_process():
                return True, "writer_check_skipped_in_main_py_db_saved_by_main_database"
            return orig()

        _patched_push_writer_state._main_writer_optional_v1 = True  # type: ignore[attr-defined]
        _patched_push_writer_state._original = orig  # type: ignore[attr-defined]
        guard._push_writer_state = _patched_push_writer_state
        _SUMMARY_AI_MAIN_WRITER_OPTIONAL_INSTALLED = True
        logger.warning("[SUMMARY AI MAIN WRITER OPTIONAL] installed main_py=%s", _is_main_py_process())
        return True
    except Exception:
        logger.exception("[SUMMARY AI MAIN WRITER OPTIONAL] install failed")
        return False


def install() -> bool:
    global _INSTALLED
    mtf_ok = False
    if not _env_bool("SUMMARY_MTF_DUPLICATE_DATETIME_GUARD", True):
        logger.warning("[SUMMARY MTF DUPLICATE DATETIME GUARD] disabled by env")
    else:
        try:
            import core.startup.summary_mtf_diff_from_1m_patch as target
            cur = getattr(target, "_repair_mtf_from_1m", None)
            if not callable(cur):
                logger.warning("[SUMMARY MTF DUPLICATE DATETIME GUARD] target missing")
            elif getattr(cur, "_duplicate_datetime_guard_v1", False):
                mtf_ok = True
            else:
                _safe_repair_mtf_from_1m._duplicate_datetime_guard_v1 = True  # type: ignore[attr-defined]
                _safe_repair_mtf_from_1m._original = cur  # type: ignore[attr-defined]
                target._repair_mtf_from_1m = _safe_repair_mtf_from_1m
                mtf_ok = True
                logger.warning("[SUMMARY MTF DUPLICATE DATETIME GUARD] installed version=%s", VERSION)
        except Exception:
            logger.exception("[SUMMARY MTF DUPLICATE DATETIME GUARD] install failed")
    writer_optional_ok = _install_summary_ai_main_writer_optional()
    _INSTALLED = bool(mtf_ok or writer_optional_ok)
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[SUMMARY MTF DUPLICATE DATETIME GUARD] auto install failed")

__all__ = ["VERSION", "install"]
