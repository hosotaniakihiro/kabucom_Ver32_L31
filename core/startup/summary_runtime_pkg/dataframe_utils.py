# ============================================================
# File   : core/startup/summary_runtime_pkg/dataframe_utils.py
# Version: REV3.0-SUMMARY-RUNTIME-DATAFRAME-UTILS
# ------------------------------------------------------------
# 【概要】
#   summary runtime 用 DataFrame utility
#
# 【主な機能】
#   - datetime 正規化
#   - symbol/datetime 重複除去
#   - symbolごとの tail 抽出
#   - summary profile log
#   - ready score 判定
#   - existing + seed merge
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from .state import SUMMARY_DB_SEED_BARS_PER_SYMBOL

logger = logging.getLogger(__name__)


def is_nonempty_df(df: Any) -> bool:
    return isinstance(df, pd.DataFrame) and not df.empty


def symbols_count(df: Any) -> int:
    try:
        if isinstance(df, pd.DataFrame) and not df.empty and "symbol" in df.columns:
            return int(
                df["symbol"]
                .fillna("")
                .astype(str)
                .str.strip()
                .replace("", pd.NA)
                .dropna()
                .nunique()
            )
    except Exception:
        pass
    return 0


def latest_dt_str(df: Any) -> str | None:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None

        for c in ("datetime", "end_time", "time", "start_time"):
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce").dropna()
                if not s.empty:
                    return str(s.max())
    except Exception:
        pass
    return None


def nonzero_count(df: pd.DataFrame, col: str) -> int:
    try:
        if col not in df.columns:
            return -1
        s = pd.to_numeric(df[col], errors="coerce").fillna(0)
        return int((s != 0).sum())
    except Exception:
        return -1


def nonnull_count(df: pd.DataFrame, col: str) -> int:
    try:
        if col not in df.columns:
            return -1
        return int(pd.to_numeric(df[col], errors="coerce").notna().sum())
    except Exception:
        return -1


def normalize_datetime_for_tf(df: pd.DataFrame, tf: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    try:
        from trading.summary.recovery.helpers import normalize_datetime_columns

        out = normalize_datetime_columns(df, interval=int(tf))
        if isinstance(out, pd.DataFrame):
            return out
    except Exception:
        pass

    try:
        x = df.copy()

        if "datetime" not in x.columns:
            for c in ("end_time", "time", "start_time"):
                if c in x.columns:
                    x["datetime"] = x[c]
                    break

        if "datetime" not in x.columns:
            if "date" in x.columns and "time_range" in x.columns:
                x["datetime"] = (
                    x["date"].astype(str).str.slice(0, 10)
                    + " "
                    + x["time_range"].astype(str).str.slice(0, 5)
                    + ":00"
                )
            elif "date" in x.columns and "time" in x.columns:
                x["datetime"] = (
                    x["date"].astype(str).str.slice(0, 10)
                    + " "
                    + x["time"].astype(str).str.slice(0, 8)
                )

        if "datetime" in x.columns:
            x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
            x = x.dropna(subset=["datetime"])

            if "date" not in x.columns:
                x["date"] = x["datetime"].dt.strftime("%Y-%m-%d")

            if "time" not in x.columns:
                x["time"] = x["datetime"].dt.strftime("%H:%M:%S")

            if "time_range" not in x.columns:
                x["time_range"] = x["datetime"].dt.strftime("%H:%M")

            if "start_time" not in x.columns:
                x["start_time"] = x["datetime"]

            if "end_time" not in x.columns:
                x["end_time"] = x["datetime"]

        return x.reset_index(drop=True)
    except Exception:
        logger.debug("[summary_runtime] normalize datetime failed tf=%s", tf, exc_info=True)
        return df


def dedupe_symbol_datetime(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if df is None or df.empty:
            return pd.DataFrame()

        if "symbol" not in df.columns or "datetime" not in df.columns:
            return df.copy().reset_index(drop=True)

        x = df.copy()
        x["symbol"] = x["symbol"].astype(str)
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x.dropna(subset=["symbol", "datetime"])

        if x.empty:
            return pd.DataFrame()

        x = (
            x.sort_values(["symbol", "datetime"], kind="stable")
            .drop_duplicates(subset=["symbol", "datetime"], keep="last")
            .reset_index(drop=True)
        )
        return x
    except Exception:
        logger.debug("[summary_runtime] dedupe symbol/datetime failed", exc_info=True)
        return df


def tail_per_symbol(df: pd.DataFrame, bars_per_symbol: int) -> pd.DataFrame:
    try:
        if df is None or df.empty:
            return pd.DataFrame()

        if "symbol" not in df.columns or "datetime" not in df.columns:
            return df.copy().reset_index(drop=True)

        x = df.copy()
        x["symbol"] = x["symbol"].astype(str)
        x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
        x = x.dropna(subset=["symbol", "datetime"])

        if x.empty:
            return pd.DataFrame()

        x = (
            x.sort_values(["symbol", "datetime"], kind="stable")
            .groupby("symbol", group_keys=False)
            .tail(max(int(bars_per_symbol), 1))
            .reset_index(drop=True)
        )
        return x
    except Exception:
        logger.debug("[summary_runtime] tail per symbol failed", exc_info=True)
        return df


def log_summary_profile(label: str, tf: int, df: Any) -> None:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            logger.info(
                "[summary_runtime] %s tf=%s rows=0 symbols=0 latest_dt=None",
                label,
                tf,
            )
            return

        logger.info(
            "[summary_runtime] %s tf=%s rows=%d symbols=%d latest_dt=%s cols=%s",
            label,
            tf,
            len(df),
            symbols_count(df),
            latest_dt_str(df),
            list(df.columns[:30]),
        )
        logger.info(
            "[summary_runtime] %s tf=%s nonzero score=%s final_score=%s display_score=%s score_buy=%s score_sell=%s slope=%s score_slope=%s mtf=%s score_mtf=%s rsi_nonnull=%s macd_nonnull=%s close_nonnull=%s",
            label,
            tf,
            nonzero_count(df, "score"),
            nonzero_count(df, "final_score"),
            nonzero_count(df, "display_score"),
            nonzero_count(df, "score_buy"),
            nonzero_count(df, "score_sell"),
            nonzero_count(df, "slope"),
            nonzero_count(df, "score_slope"),
            nonzero_count(df, "mtf"),
            nonzero_count(df, "score_mtf"),
            nonnull_count(df, "rsi"),
            nonnull_count(df, "macd"),
            nonnull_count(df, "close"),
        )
    except Exception:
        logger.exception("[summary_runtime] profile log failed label=%s tf=%s", label, tf)


def summary_has_ready_scores(df) -> bool:
    try:
        if df is None or df.empty:
            return False

        for c in (
            "score",
            "final_score",
            "display_score",
            "score_buy",
            "score_sell",
            "slope_atr_scaled",
            "slope",
            "mtf",
            "score_mtf",
            "macd",
            "signal",
            "rsi",
        ):
            if c in df.columns:
                s = pd.to_numeric(df[c], errors="coerce").fillna(0)
                if int((s != 0).sum()) > 0:
                    return True

        return False
    except Exception:
        return False


def can_skip_closed_day_recalc(df1, df3, df5) -> bool:
    return bool(
        summary_has_ready_scores(df1)
        and summary_has_ready_scores(df3)
        and summary_has_ready_scores(df5)
    )


def merge_existing_and_seed(existing: Any, seed: Any, tf: int, *, label: str) -> pd.DataFrame:
    try:
        if not isinstance(existing, pd.DataFrame) or existing.empty:
            if isinstance(seed, pd.DataFrame) and not seed.empty:
                return seed.copy().reset_index(drop=True)
            return pd.DataFrame()

        if not isinstance(seed, pd.DataFrame) or seed.empty:
            return existing.copy().reset_index(drop=True)

        try:
            from trading.summary.recovery.helpers import merge_summary_frames_with_priority

            merged = merge_summary_frames_with_priority(existing, seed, interval=int(tf))
        except Exception:
            merged = pd.concat([existing, seed], ignore_index=True, sort=False)

        merged = normalize_datetime_for_tf(merged, tf)
        merged = dedupe_symbol_datetime(merged)
        merged = tail_per_symbol(merged, SUMMARY_DB_SEED_BARS_PER_SYMBOL.get(int(tf), 150))

        logger.info(
            "[summary_runtime] merged existing+seed label=%s tf=%s existing_rows=%d seed_rows=%d merged_rows=%d symbols=%d latest_dt=%s",
            label,
            tf,
            len(existing),
            len(seed),
            len(merged),
            symbols_count(merged),
            latest_dt_str(merged),
        )
        return merged
    except Exception:
        logger.exception("[summary_runtime] merge existing+seed failed label=%s tf=%s", label, tf)
        if isinstance(seed, pd.DataFrame) and not seed.empty:
            return seed
        if isinstance(existing, pd.DataFrame) and not existing.empty:
            return existing
        return pd.DataFrame()


def normalize_symbol_local(v) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return ""
        s = s.replace(".0", "")
        s = s.replace("　", "").replace(" ", "")
        s = s.upper()
        return s
    except Exception:
        return ""


def iter_symbols_from_any(values):
    try:
        import pandas as pd  # local import for compatibility
    except Exception:
        pd = None

    if values is None:
        return

    if pd is not None and isinstance(values, pd.DataFrame):
        for col in ("symbol", "code", "Code", "ticker"):
            if col in values.columns:
                for x in values[col].tolist():
                    yield normalize_symbol_local(x)
                return
        return

    if pd is not None and isinstance(values, pd.Series):
        for x in values.tolist():
            yield normalize_symbol_local(x)
        return

    if isinstance(values, dict):
        for k in ("symbol", "code", "Code", "ticker"):
            if k in values:
                yield normalize_symbol_local(values.get(k))
                return
        for _, v in values.items():
            s = normalize_symbol_local(v)
            if s:
                yield s
        return

    if isinstance(values, (list, tuple, set)):
        for x in values:
            if isinstance(x, dict):
                if "symbol" in x:
                    yield normalize_symbol_local(x.get("symbol"))
                    continue
                if "code" in x:
                    yield normalize_symbol_local(x.get("code"))
                    continue
                if "Code" in x:
                    yield normalize_symbol_local(x.get("Code"))
                    continue
                if "ticker" in x:
                    yield normalize_symbol_local(x.get("ticker"))
                    continue
            yield normalize_symbol_local(x)
        return

    yield normalize_symbol_local(values)


__all__ = [
    "is_nonempty_df",
    "symbols_count",
    "latest_dt_str",
    "nonzero_count",
    "nonnull_count",
    "normalize_datetime_for_tf",
    "dedupe_symbol_datetime",
    "tail_per_symbol",
    "log_summary_profile",
    "summary_has_ready_scores",
    "can_skip_closed_day_recalc",
    "merge_existing_and_seed",
    "normalize_symbol_local",
    "iter_symbols_from_any",
]