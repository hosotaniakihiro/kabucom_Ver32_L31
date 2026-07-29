# ============================================================
# File   : core/global_context/context.py
# Version: PRODUCTION-STABLE-REV10.3-LIGHTWEIGHT-SUMMARY-LOG
#          -SUMMARY-INTEGRITY-GUARD
#          -COMPLETED-SUMMARY-RELAXED
#          -PUSH-PUBLISH-COMPAT
#          -MERGED-FALLBACK-STRENGTHENED
#          -PUSH-SUMMARY-CACHE-FALLBACK
#          -HISTORY-CACHE-NO-LATEST-COMPRESSION
# ------------------------------------------------------------
# 【概要】
#   GlobalContext / global_data の中核管理モジュール
#
# 【主な機能】
#   - merged summary 管理
#   - push / ranking / legacy summary cache 管理
#   - symbol_name_map 管理
#   - push_df / ranking_df 管理
#   - runtime flags 管理
#
# 【REV10.3 修正】
#   - SUMMARY HISTORY GET/SET / MERGED GET/SET の詳細表ログを通常OFF化。
#   - 1分PUSHサマリー更新中に20行テーブルを大量出力し、PUSH-1m / unified-parent
#     が 12〜25秒でタイムアウトする症状を軽減する。
#   - 件数・非ゼロ数ログは残す。表ログは SUMMARY_CONTEXT_VERBOSE_TABLE_LOG=1 の時だけ出す。
# ============================================================

from __future__ import annotations

import inspect
import logging
import os
import threading
import warnings
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_ALLOW_ORDERS_TRUE_VALUES = {"1", "true", "yes", "on", "y", "live", "real"}
_ALLOW_ORDERS_FALSE_VALUES = {"0", "false", "no", "off", "n", "dry", "dryrun", "paper"}


def _env_bool_tri(name: str) -> Optional[bool]:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return None
    s = str(v).strip().lower()
    if s in _ALLOW_ORDERS_TRUE_VALUES:
        return True
    if s in _ALLOW_ORDERS_FALSE_VALUES:
        return False
    return None


def _resolve_allow_orders() -> tuple[bool, str]:
    """Resolve initial allow_orders from ALLOW_ORDERS / DRY_RUN style env vars.

    Defaults to True (live) when nothing is set, matching production operation.
    """
    for key in ("DRY_RUN", "ENTRY_DRY_RUN", "ORDER_DRY_RUN", "KABU_DRY_RUN", "SUMMARY_ENTRY_DRY_RUN", "PAPER_TRADE"):
        if _env_bool_tri(key) is True:
            return False, f"{key}=true"

    for key in ("ALLOW_ORDERS", "ENTRY_ALLOW_ORDERS", "KABU_ALLOW_ORDERS", "LIVE_ORDERS", "ORDER_LIVE"):
        v = _env_bool_tri(key)
        if v is True:
            return True, f"{key}=true"
        if v is False:
            return False, f"{key}=false"

    return True, "default_live_true"

SUMMARY_REQUIRED_COLS = (
    "symbol",
    "score",
)

SUMMARY_OPTIONAL_SCORE_COLS = (
    "score_buy",
    "score_sell",
)

SUMMARY_PREFERRED_COLS = (
    "final_score",
    "display_score",
    "datetime",
)

SUMMARY_TECHNICAL_COLS = (
    "slope",
    "score_slope",
    "mtf",
    "score_mtf",
    "mtf_score",
    "rsi",
    "macd",
    "signal",
)

_NUMERIC_SUMMARY_COLS = (
    "score",
    "score_total",
    "final_score",
    "display_score",
    "score_buy",
    "score_sell",
    "slope",
    "slope_atr_scaled",
    "score_slope",
    "mtf",
    "mtf_alignment",
    "score_mtf",
    "mtf_score",
    "open",
    "high",
    "low",
    "close",
    "rsi",
    "macd",
    "signal",
)

_TIME_CANDIDATE_COLS = (
    "datetime",
    "end_time",
    "start_time",
    "time",
)


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _safe_to_datetime_naive_series(s, *, base_date=None) -> pd.Series:
    """
    UserWarning: Could not infer format... を出さずに datetime 化する。
    timezone 付きの場合も、壁時計時刻を維持して tz だけ外す。
    """
    try:
        if s is None:
            return pd.Series(dtype="datetime64[ns]")

        if not isinstance(s, pd.Series):
            s = pd.Series(s)

        if pd.api.types.is_datetime64_any_dtype(s):
            out = pd.to_datetime(s, errors="coerce")
            try:
                out = out.dt.tz_localize(None)
            except Exception:
                pass
            return out

        raw = s.astype(str).str.strip()
        raw = raw.replace(
            {
                "": None,
                "nan": None,
                "NaN": None,
                "None": None,
                "NaT": None,
                "<NA>": None,
                "null": None,
                "NULL": None,
            }
        )

        out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")

        patterns = [
            (raw.str.match(r"^\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}:\d{2}$", na=False), "%Y-%m-%d %H:%M:%S"),
            (raw.str.match(r"^\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}$", na=False), "%Y-%m-%d %H:%M"),
            (raw.str.match(r"^\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2}$", na=False), "%Y/%m/%d %H:%M:%S"),
            (raw.str.match(r"^\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}$", na=False), "%Y/%m/%d %H:%M"),
            (raw.str.match(r"^\d{4}-\d{1,2}-\d{1,2}$", na=False), "%Y-%m-%d"),
            (raw.str.match(r"^\d{4}/\d{1,2}/\d{1,2}$", na=False), "%Y/%m/%d"),
        ]

        for mask, fmt in patterns:
            if mask.any():
                idx = mask[mask].index
                out.loc[idx] = pd.to_datetime(raw.loc[idx], errors="coerce", format=fmt)

        time_hms = raw.str.match(r"^\d{1,2}:\d{2}:\d{2}$", na=False)
        time_hm = raw.str.match(r"^\d{1,2}:\d{2}$", na=False)

        if time_hms.any() or time_hm.any():
            today = pd.Timestamp.now().strftime("%Y-%m-%d")
            if isinstance(base_date, pd.Series):
                base = _safe_to_datetime_naive_series(base_date, base_date=None)
                base_s = base.dt.strftime("%Y-%m-%d").fillna(today)
            elif base_date is not None:
                base_s = pd.Series(str(base_date), index=s.index)
            else:
                base_s = pd.Series(today, index=s.index)
            combined = base_s.astype(str) + " " + raw.astype(str)
            if time_hms.any():
                idx = time_hms[time_hms].index
                out.loc[idx] = pd.to_datetime(combined.loc[idx], errors="coerce", format="%Y-%m-%d %H:%M:%S")
            if time_hm.any():
                idx = time_hm[time_hm].index
                out.loc[idx] = pd.to_datetime(combined.loc[idx], errors="coerce", format="%Y-%m-%d %H:%M")

        remaining = out.isna() & raw.notna()
        if remaining.any():
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                parsed = raw.loc[remaining].map(_strip_tz_keep_wallclock)
                out.loc[remaining] = pd.to_datetime(parsed, errors="coerce")

        try:
            out = out.dt.tz_localize(None)
        except Exception:
            pass
        return out
    except Exception:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            return pd.to_datetime(pd.Series(s), errors="coerce")


def _strip_tz_keep_wallclock(v):
    try:
        if v is None:
            return pd.NaT
        ts = pd.Timestamp(v)
        if pd.isna(ts):
            return pd.NaT
        if ts.tzinfo is not None:
            ts = ts.tz_localize(None)
        return ts
    except Exception:
        return pd.NaT


def _caller_name(depth: int = 2) -> str:
    try:
        frame = inspect.stack()[depth]
        mod = inspect.getmodule(frame.frame)
        mod_name = mod.__name__ if mod else "unknown_module"
        return f"{mod_name}.{frame.function}"
    except Exception:
        return "unknown_caller"


def _safe_df(df: Any) -> pd.DataFrame:
    try:
        if df is None:
            return pd.DataFrame()
        if not isinstance(df, pd.DataFrame):
            return pd.DataFrame()
        return df.copy()
    except Exception:
        logger.exception("[GlobalContext] _safe_df failed")
        return pd.DataFrame()


def _to_numeric_series(df: pd.DataFrame, col: str) -> pd.Series:
    try:
        if col not in df.columns:
            return pd.Series(dtype="float64")
        return pd.to_numeric(df[col], errors="coerce")
    except Exception:
        return pd.Series(dtype="float64")


def _nonzero_count(df: pd.DataFrame, col: str) -> int:
    try:
        if col not in df.columns:
            return -1
        s = _to_numeric_series(df, col)
        if s.empty:
            return 0
        return int((s.fillna(0) != 0).sum())
    except Exception:
        return -1


def _nonnull_count(df: pd.DataFrame, col: str) -> int:
    try:
        if col not in df.columns:
            return -1
        return int(df[col].notna().sum())
    except Exception:
        return -1


def _normalize_symbol_value(v: Any) -> str:
    try:
        if pd.isna(v):
            return ""
        s = str(v).strip()
        if not s:
            return ""
        if s.endswith(".0"):
            s2 = s[:-2]
            if s2.isdigit():
                return s2
        return s
    except Exception:
        return ""


def _normalize_symbol_series(sr: pd.Series) -> pd.Series:
    try:
        return sr.map(_normalize_symbol_value)
    except Exception:
        return sr.astype(str).str.strip()


def _coerce_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if df.empty:
            return df
        out = df.copy()
        for col in _NUMERIC_SUMMARY_COLS:
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")
        return out
    except Exception:
        logger.exception("[GlobalContext] _coerce_numeric_columns failed")
        return df


def _coerce_datetime_columns(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if df.empty:
            return df
        out = df.copy()
        for col in _TIME_CANDIDATE_COLS:
            if col in out.columns:
                base_date = out["date"] if "date" in out.columns else None
                out[col] = _safe_to_datetime_naive_series(out[col], base_date=base_date)
        return out
    except Exception:
        logger.exception("[GlobalContext] _coerce_datetime_columns failed")
        return df


def _drop_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if df.empty:
            return df
        return df.loc[:, ~df.columns.duplicated()].copy()
    except Exception:
        logger.exception("[GlobalContext] _drop_duplicate_columns failed")
        return df


def _ensure_symbol_column(df: pd.DataFrame) -> pd.DataFrame:
    try:
        out = df.copy()
        if "symbol" in out.columns:
            out["symbol"] = _normalize_symbol_series(out["symbol"])
        return out
    except Exception:
        logger.exception("[GlobalContext] _ensure_symbol_column failed")
        return df


def _best_time_col(df: pd.DataFrame) -> Optional[str]:
    try:
        for col in ("datetime", "end_time", "start_time", "time"):
            if col in df.columns:
                return col
    except Exception:
        pass
    return None


def _latest_one_row_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if df.empty or "symbol" not in df.columns:
            return df
        out = df.copy()
        time_col = _best_time_col(out)
        if time_col and time_col in out.columns:
            out = out.sort_values(["symbol", time_col], ascending=[True, False], na_position="last")
        return out.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)
    except Exception:
        logger.exception("[GlobalContext] _latest_one_row_per_symbol failed")
        return df


def _enrich_symbolname(df: pd.DataFrame, mp: Optional[Dict[str, str]]) -> pd.DataFrame:
    try:
        if df.empty or "symbol" not in df.columns:
            return df
        out = df.copy()
        if "symbolname" not in out.columns:
            out["symbolname"] = ""
        out["symbol"] = _normalize_symbol_series(out["symbol"])
        out["symbolname"] = out["symbolname"].fillna("").astype(str).str.strip()
        mp2 = {str(k).strip(): str(v).strip() for k, v in dict(mp or {}).items() if str(k).strip()}
        if mp2:
            blank_mask = out["symbolname"].eq("")
            if blank_mask.any():
                out.loc[blank_mask, "symbolname"] = out.loc[blank_mask, "symbol"].map(mp2).fillna("")
        if "name" in out.columns:
            name_series = out["name"].fillna("").astype(str).str.strip()
            blank_mask = out["symbolname"].eq("")
            if blank_mask.any():
                out.loc[blank_mask, "symbolname"] = name_series[blank_mask]
        return out
    except Exception:
        logger.exception("[GlobalContext] _enrich_symbolname failed")
        return df


def _repair_mtf_consistency(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if df.empty:
            return df
        out = df.copy()
        mtf = pd.to_numeric(out["mtf"], errors="coerce") if "mtf" in out.columns else None
        score_mtf = pd.to_numeric(out["score_mtf"], errors="coerce") if "score_mtf" in out.columns else None
        mtf_score = pd.to_numeric(out["mtf_score"], errors="coerce") if "mtf_score" in out.columns else None
        final_score = pd.to_numeric(out["final_score"], errors="coerce") if "final_score" in out.columns else None
        if mtf is not None:
            bad_mask = mtf.fillna(0).eq(0)
            if score_mtf is not None:
                pos_mask = bad_mask & score_mtf.fillna(0).gt(0)
                if pos_mask.any():
                    out.loc[pos_mask, "score_mtf"] = 0.0
            if mtf_score is not None:
                pos_mask = bad_mask & mtf_score.fillna(0).gt(0)
                if pos_mask.any():
                    out.loc[pos_mask, "mtf_score"] = 0.0
            if final_score is not None:
                if "score_mtf" in out.columns:
                    score_mtf2 = pd.to_numeric(out["score_mtf"], errors="coerce").fillna(0)
                    same_mask = bad_mask & final_score.fillna(0).eq(score_mtf2)
                    if "score" in out.columns:
                        base_score = pd.to_numeric(out["score"], errors="coerce")
                        same_mask = same_mask & base_score.notna()
                        if same_mask.any():
                            out.loc[same_mask, "final_score"] = base_score[same_mask]
                if "mtf_score" in out.columns:
                    mtf_score2 = pd.to_numeric(out["mtf_score"], errors="coerce").fillna(0)
                    same_mask = bad_mask & pd.to_numeric(out["final_score"], errors="coerce").fillna(0).eq(mtf_score2)
                    if "score" in out.columns:
                        base_score = pd.to_numeric(out["score"], errors="coerce")
                        same_mask = same_mask & base_score.notna()
                        if same_mask.any():
                            out.loc[same_mask, "final_score"] = base_score[same_mask]
        return out
    except Exception:
        logger.exception("[GlobalContext] _repair_mtf_consistency failed")
        return df


def _is_completed_summary_df(df: pd.DataFrame) -> bool:
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return False
        cols = set(df.columns)
        if not all(col in cols for col in SUMMARY_REQUIRED_COLS):
            return False
        sym = df["symbol"].fillna("").astype(str).str.strip()
        if sym.eq("").all():
            return False
        score = pd.to_numeric(df["score"], errors="coerce") if "score" in df.columns else pd.Series(dtype="float64")
        if score.notna().sum() == 0:
            return False
        if "score_buy" in df.columns or "score_sell" in df.columns:
            score_buy = pd.to_numeric(df["score_buy"], errors="coerce") if "score_buy" in df.columns else pd.Series(dtype="float64")
            score_sell = pd.to_numeric(df["score_sell"], errors="coerce") if "score_sell" in df.columns else pd.Series(dtype="float64")
            if score_buy.notna().sum() == 0 and score_sell.notna().sum() == 0 and score.notna().sum() == 0:
                return False
        return True
    except Exception:
        return False


def _profile_df(df: pd.DataFrame) -> Dict[str, Any]:
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return {"rows": 0, "cols": 0, "sample_cols": [], "score_nonzero": 0, "slope_nonzero": 0, "slope_atr_scaled_nonzero": 0, "score_slope_nonzero": 0, "mtf_nonzero": 0, "score_mtf_nonzero": 0, "mtf_score_nonzero": 0, "rsi_nonzero": 0, "macd_nonzero": 0, "signal_nonzero": 0, "close_nonnull": 0, "datetime_nonnull": 0, "blank_symbolname": 0, "unique_symbols": 0, "completed_summary": False}
        blank_symbolname = -1
        if "symbolname" in df.columns:
            blank_symbolname = int(df["symbolname"].fillna("").astype(str).str.strip().eq("").sum())
        unique_symbols = -1
        if "symbol" in df.columns:
            unique_symbols = int(df["symbol"].fillna("").astype(str).str.strip().nunique())
        return {
            "rows": int(len(df)),
            "cols": int(len(df.columns)),
            "sample_cols": list(df.columns[:25]),
            "score_nonzero": _nonzero_count(df, "score"),
            "slope_nonzero": _nonzero_count(df, "slope"),
            "slope_atr_scaled_nonzero": _nonzero_count(df, "slope_atr_scaled"),
            "score_slope_nonzero": _nonzero_count(df, "score_slope"),
            "mtf_nonzero": _nonzero_count(df, "mtf"),
            "score_mtf_nonzero": _nonzero_count(df, "score_mtf"),
            "mtf_score_nonzero": _nonzero_count(df, "mtf_score"),
            "rsi_nonzero": _nonzero_count(df, "rsi"),
            "macd_nonzero": _nonzero_count(df, "macd"),
            "signal_nonzero": _nonzero_count(df, "signal"),
            "close_nonnull": _nonnull_count(df, "close"),
            "datetime_nonnull": _nonnull_count(df, "datetime"),
            "blank_symbolname": blank_symbolname,
            "unique_symbols": unique_symbols,
            "completed_summary": _is_completed_summary_df(df),
        }
    except Exception:
        logger.exception("[GlobalContext] _profile_df failed")
        return {"rows": -1, "cols": -1, "sample_cols": []}


def _log_df_profile(prefix: str, tf: Any, source: Optional[str], df: pd.DataFrame) -> None:
    try:
        prof = _profile_df(df)
        logger.info(
            "%s tf=%s source=%s rows=%s cols=%s unique_symbols=%s blank_symbolname=%s completed_summary=%s sample_cols=%s",
            prefix, tf, source, prof.get("rows"), prof.get("cols"), prof.get("unique_symbols"), prof.get("blank_symbolname"), prof.get("completed_summary"), prof.get("sample_cols"),
        )
        logger.info(
            "%s tf=%s source=%s nonzero score=%s slope=%s slope_atr_scaled=%s score_slope=%s mtf=%s score_mtf=%s mtf_score=%s rsi=%s macd=%s signal=%s close_nonnull=%s datetime_nonnull=%s",
            prefix, tf, source, prof.get("score_nonzero"), prof.get("slope_nonzero"), prof.get("slope_atr_scaled_nonzero"), prof.get("score_slope_nonzero"), prof.get("mtf_nonzero"), prof.get("score_mtf_nonzero"), prof.get("mtf_score_nonzero"), prof.get("rsi_nonzero"), prof.get("macd_nonzero"), prof.get("signal_nonzero"), prof.get("close_nonnull"), prof.get("datetime_nonnull"),
        )

        if not _env_bool("SUMMARY_CONTEXT_VERBOSE_TABLE_LOG", False):
            return

        show_cols = [
            c for c in [
                "symbol", "symbolname", "score", "score_total", "final_score", "display_score",
                "score_buy", "score_sell", "slope", "slope_atr_scaled", "score_slope",
                "mtf", "score_mtf", "mtf_score",
                "open", "high", "low", "close", "rsi", "macd", "signal", "datetime",
            ] if c in df.columns
        ]
        if show_cols and not df.empty:
            head_n = 20
            try:
                head_n = max(1, int(float(os.getenv("SUMMARY_CONTEXT_VERBOSE_TABLE_ROWS", "20"))))
            except Exception:
                head_n = 20
            logger.info("%s tf=%s source=%s\n%s", prefix, tf, source, df[show_cols].head(head_n).to_string(index=False))
    except Exception:
        logger.exception("[GlobalContext] _log_df_profile failed prefix=%s tf=%s source=%s", prefix, tf, source)


# ============================================================
# STALE SUMMARY ROW GUARD
# (旧 core/startup/summary_stale_guard_patch.py から移設)
#
# PUSH / ranking summary が古いまま merged summary に残り、古い価格・
# 古い slope・古い RSI/MACD で表示・AI・発注候補になる問題を防ぐ。
# 同日データでも soft-keep を無制限にせず、1分足は既定300秒/3分足480秒/
# 5分足900秒を下限として扱う。latest_dt が max_age+grace を超えたら
# 同日でも hard-drop する。
# ============================================================

_STALE_GUARD_VERSION = "REV9-SUMMARY-STALE-GUARD-BOUNDED-SAME-DAY"


def _stale_env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _stale_normalize_source(source: Any) -> str:
    try:
        return str(source or "push").strip().lower()
    except Exception:
        return "push"


def _stale_normalize_tf(tf: Any) -> Any:
    try:
        if tf in ("1", "1m", "1min"):
            return 1
        if tf in ("3", "3m", "3min"):
            return 3
        if tf in ("5", "5m", "5min"):
            return 5
        if tf in ("10", "10m", "10min"):
            return 10
        if tf in ("15", "15m", "15min"):
            return 15
        if tf in ("30", "30m", "30min"):
            return 30
        if tf in ("60", "60m", "60min"):
            return 60
        if tf in ("d", "1d", "day", "daily"):
            return "daily"
        return int(tf)
    except Exception:
        return tf


def _stale_is_intraday_live_source(source: Any) -> bool:
    src = _stale_normalize_source(source)
    return src.startswith("push") or src.startswith("ranking") or src in {"legacy", "summary"}


def _stale_bounded_env_int(name: str, default: int, *, min_value: Optional[int] = None) -> int:
    value = _stale_env_int(name, default)
    if min_value is not None:
        value = max(int(value), int(min_value))
    return int(value)


def _stale_max_age_sec(source: Any, tf: Any) -> Optional[int]:
    source = _stale_normalize_source(source)
    tf = _stale_normalize_tf(tf)

    if not _env_bool("SUMMARY_STALE_GUARD_ENABLED", True):
        return None
    if tf == "daily":
        return None

    if source.startswith("push"):
        defaults = {1: 300, 3: 480, 5: 900, 10: 1200, 15: 1500, 30: 2400, 60: 4800}
        default = int(defaults.get(tf, 600))
        return _stale_bounded_env_int(f"PUSH_SUMMARY_{tf}MIN_MAX_AGE_SEC", default, min_value=default)

    if source.startswith("ranking"):
        defaults = {1: 300, 3: 480, 5: 900, 10: 1500, 15: 1800, 30: 3600, 60: 7200}
        default = int(defaults.get(tf, 900))
        return _stale_bounded_env_int(f"RANKING_SUMMARY_{tf}MIN_MAX_AGE_SEC", default, min_value=default)

    if source == "legacy" or source == "summary":
        default = 300 if tf == 1 else 900
        return _stale_bounded_env_int(f"LEGACY_SUMMARY_{tf}MIN_MAX_AGE_SEC", default, min_value=default)

    return None


def _stale_relative_lag_sec(source: Any, tf: Any) -> Optional[int]:
    source = _stale_normalize_source(source)
    tf = _stale_normalize_tf(tf)

    if not _env_bool("SUMMARY_STALE_RELATIVE_GUARD_ENABLED", True):
        return None
    if tf == "daily":
        return None

    try:
        tf_int = int(tf)
    except Exception:
        tf_int = 1

    defaults = {1: 300, 3: 720, 5: 1200}
    default = int(defaults.get(tf_int, max(300, tf_int * 180 + 300)))

    if source.startswith("push"):
        return _stale_bounded_env_int(f"PUSH_SUMMARY_{tf_int}MIN_RELATIVE_LAG_SEC", default, min_value=default)
    if source.startswith("ranking"):
        return _stale_bounded_env_int(f"RANKING_SUMMARY_{tf_int}MIN_RELATIVE_LAG_SEC", default, min_value=default)
    if source in {"legacy", "summary"}:
        return _stale_bounded_env_int(f"LEGACY_SUMMARY_{tf_int}MIN_RELATIVE_LAG_SEC", default, min_value=default)
    return None


def _stale_min_keep_rows(source: Any, tf: Any) -> int:
    tf_n = _stale_normalize_tf(tf)
    if tf_n == "daily":
        return 0
    try:
        tf_int = int(tf_n)
    except Exception:
        tf_int = 1
    src = _stale_normalize_source(source).upper()
    return _stale_env_int(f"{src}_SUMMARY_{tf_int}MIN_KEEP_ROWS", _stale_env_int(f"SUMMARY_{tf_int}MIN_KEEP_ROWS", 0))


def _stale_future_allow_sec(source: Any, tf: Any) -> int:
    try:
        tf_n = _stale_normalize_tf(tf)
        tf_int = int(tf_n) if tf_n != "daily" else 1
    except Exception:
        tf_int = 1
    default = max(10, min(60, tf_int * 12))
    src = _stale_normalize_source(source).upper()
    return _stale_env_int(f"{src}_SUMMARY_{tf_int}MIN_FUTURE_ALLOW_SEC", _stale_env_int("SUMMARY_FUTURE_ALLOW_SEC", default))


def _stale_latest_fallback_rows(out: pd.DataFrame, *, time_col: str, min_keep: int) -> pd.DataFrame:
    if min_keep <= 0 or out.empty:
        return out.iloc[0:0].copy()
    try:
        work = out[out[time_col].notna()].copy()
        if work.empty:
            return out.iloc[0:0].copy()
        work = work.sort_values(time_col, ascending=False)
        if "symbol" in work.columns:
            work = work.drop_duplicates(subset=["symbol"], keep="first")
        return work.head(int(min_keep)).copy().reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY STALE DROP] latest fallback failed", exc_info=True)
        return out.iloc[0:0].copy()


def _stale_to_naive_datetime_series(s: pd.Series) -> pd.Series:
    out = pd.to_datetime(s, errors="coerce")
    try:
        return out.dt.tz_localize(None)
    except Exception:
        return out


def drop_stale_summary_rows(df: Any, *, source: Any, tf: Any, label: str = "sanitize") -> pd.DataFrame:
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame() if df is None else df

        max_age = _stale_max_age_sec(source, tf)
        relative_lag = _stale_relative_lag_sec(source, tf)
        min_keep = _stale_min_keep_rows(source, tf)
        if (max_age is None or max_age <= 0) and (relative_lag is None or relative_lag <= 0):
            return df

        time_col = _best_time_col(df)
        if not time_col:
            logger.warning(
                "[SUMMARY STALE DROP] source=%s tf=%s label=%s before=%s after=0 reason=time_col_missing max_age_sec=%s relative_lag_sec=%s version=%s",
                source, tf, label, len(df), max_age, relative_lag, _STALE_GUARD_VERSION,
            )
            return df.iloc[0:0].copy()

        out = df.copy()
        out[time_col] = _stale_to_naive_datetime_series(out[time_col])
        now = pd.Timestamp.now().tz_localize(None)
        before_original = int(len(out))

        today_str = now.strftime("%Y-%m-%d")
        strict_today = _env_bool("SUMMARY_STALE_STRICT_TODAY_ONLY", True)
        if strict_today and _stale_is_intraday_live_source(source) and _stale_normalize_tf(tf) != "daily":
            dt_str = out[time_col].dt.strftime("%Y-%m-%d")
            non_today_mask = out[time_col].notna() & dt_str.ne(today_str)
            non_today_count = int(non_today_mask.sum()) if len(out) else 0
            if non_today_count > 0:
                non_today_latest = None
                try:
                    non_today_latest = out.loc[non_today_mask, time_col].max()
                except Exception:
                    non_today_latest = None
                out = out.loc[~non_today_mask].copy().reset_index(drop=True)
                logger.warning(
                    "[SUMMARY NON-TODAY DROP] source=%s tf=%s label=%s before=%s non_today_removed=%s after=%s today=%s non_today_latest=%s version=%s",
                    source, tf, label, before_original, non_today_count, len(out), today_str, non_today_latest, _STALE_GUARD_VERSION,
                )
                if out.empty:
                    return out

        allow_sec = _stale_future_allow_sec(source, tf)
        future_cutoff = now + pd.Timedelta(seconds=float(allow_sec))
        dt_series0 = out[time_col]
        future_mask = dt_series0.notna() & dt_series0.gt(future_cutoff)
        future_count = int(future_mask.sum()) if len(out) else 0
        if future_count > 0:
            future_latest = None
            try:
                future_latest = dt_series0.loc[future_mask].max()
            except Exception:
                future_latest = None
            out = out.loc[~future_mask].copy().reset_index(drop=True)
            logger.warning(
                "[SUMMARY FUTURE DROP] source=%s tf=%s label=%s before=%s future_removed=%s after=%s now=%s allow_sec=%s future_latest=%s version=%s",
                source, tf, label, before_original, future_count, len(out), now, allow_sec, future_latest, _STALE_GUARD_VERSION,
            )
            if out.empty:
                return out

        dt_series = out[time_col]
        age_sec = (now - dt_series).dt.total_seconds()
        before = int(len(out))
        valid_mask = dt_series.notna() & age_sec.ge(0)

        latest_dt = None
        latest_age_sec = None
        try:
            latest_dt = dt_series.max()
            if pd.notna(latest_dt):
                latest_age_sec = float((now - latest_dt).total_seconds())
        except Exception:
            latest_dt = None
            latest_age_sec = None

        # REV9: same-day soft keep is bounded. 10:25時点で10:11を残すような
        # 無制限soft-keepはライブ候補に古いPUSHを混ぜるため禁止する。
        global_lag_enabled = _env_bool("SUMMARY_STALE_KEEP_LATEST_PER_SYMBOL_ON_GLOBAL_LAG", False)
        global_lag_grace = _stale_env_int("SUMMARY_STALE_GLOBAL_LAG_GRACE_SEC", 60)
        if (
            not global_lag_enabled
            and max_age is not None
            and max_age > 0
            and latest_age_sec is not None
            and latest_age_sec > float(max_age + global_lag_grace)
        ):
            logger.warning(
                "[SUMMARY STALE DROP] global lag hard-drop bounded source=%s tf=%s label=%s before=%s after=0 max_age_sec=%s grace_sec=%s latest_age_sec=%.1f latest_dt=%s now=%s version=%s",
                source, tf, label, before, max_age, global_lag_grace, latest_age_sec, latest_dt, now, _STALE_GUARD_VERSION,
            )
            return out.iloc[0:0].copy()

        absolute_mask = pd.Series(False, index=out.index)
        if max_age is not None and max_age > 0:
            absolute_mask = age_sec.le(float(max_age))

        relative_mask = pd.Series(False, index=out.index)
        if relative_lag is not None and relative_lag > 0:
            try:
                if pd.notna(latest_dt):
                    cutoff = latest_dt - pd.Timedelta(seconds=float(relative_lag))
                    relative_mask = dt_series.ge(cutoff)
            except Exception:
                latest_dt = None

        valid_mask = valid_mask & (absolute_mask | relative_mask)
        out2 = out.loc[valid_mask].copy().reset_index(drop=True)
        after = int(len(out2))

        if min_keep > 0 and before >= min_keep and after < min_keep:
            fallback = _stale_latest_fallback_rows(out, time_col=time_col, min_keep=min_keep)
            if len(fallback) > after:
                logger.warning(
                    "[SUMMARY STALE DROP] min_keep fallback source=%s tf=%s label=%s before=%s stale_after=%s fallback_after=%s min_keep=%s latest_dt=%s now=%s version=%s",
                    source, tf, label, before, after, len(fallback), min_keep, latest_dt, now, _STALE_GUARD_VERSION,
                )
                out2 = fallback
                after = int(len(out2))

        if after != before or future_count > 0 or before_original != before:
            oldest_kept = None
            newest_kept = None
            try:
                oldest_kept = out2[time_col].min() if after else None
                newest_kept = out2[time_col].max() if after else None
            except Exception:
                pass
            logger.warning(
                "[SUMMARY STALE DROP] source=%s tf=%s label=%s before=%s after=%s max_age_sec=%s relative_lag_sec=%s min_keep=%s latest_dt=%s oldest_kept=%s newest_kept=%s now=%s future_removed=%s version=%s",
                source, tf, label, before_original, after, max_age, relative_lag, min_keep, latest_dt, oldest_kept, newest_kept, now, future_count, _STALE_GUARD_VERSION,
            )

        return out2
    except Exception:
        logger.exception("[SUMMARY STALE DROP] failed source=%s tf=%s label=%s", source, tf, label)
        return df if isinstance(df, pd.DataFrame) else pd.DataFrame()


def _sanitize_summary_df(df: Any, tf: Any, source: str, symbol_name_map: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    out = _sanitize_summary_df_impl(df, tf, source, symbol_name_map)
    return drop_stale_summary_rows(out, source=source, tf=tf, label="merged_sanitize")


def _sanitize_summary_df_impl(df: Any, tf: Any, source: str, symbol_name_map: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    try:
        out = _safe_df(df)
        if out.empty:
            return out
        out = _drop_duplicate_columns(out)
        out = _ensure_symbol_column(out)
        out = _coerce_numeric_columns(out)
        out = _coerce_datetime_columns(out)
        out = _latest_one_row_per_symbol(out)
        out = _enrich_symbolname(out, symbol_name_map)
        out = _repair_mtf_consistency(out)
        if "display_score" not in out.columns and "score" in out.columns:
            out["display_score"] = pd.to_numeric(out["score"], errors="coerce")
        if "final_score" not in out.columns and "score" in out.columns:
            out["final_score"] = pd.to_numeric(out["score"], errors="coerce")
        if "score_buy" not in out.columns and "score" in out.columns:
            out["score_buy"] = pd.to_numeric(out["score"], errors="coerce")
        if "score_sell" not in out.columns:
            out["score_sell"] = 0.0
        if "symbolname" not in out.columns and "symbol" in out.columns:
            out["symbolname"] = ""
        if "symbol" in out.columns:
            out = out[out["symbol"].fillna("").astype(str).str.strip() != ""].copy()
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[GlobalContext] _sanitize_summary_df failed tf=%s source=%s", tf, source)
        return _safe_df(df)


def _sanitize_summary_history_df(df: Any, tf: Any, source: str = "history", symbol_name_map: Optional[Dict[str, str]] = None) -> pd.DataFrame:
    try:
        out = _safe_df(df)
        if out.empty:
            return out
        out = _drop_duplicate_columns(out)
        out = _ensure_symbol_column(out)
        out = _coerce_numeric_columns(out)
        out = _coerce_datetime_columns(out)
        out = _enrich_symbolname(out, symbol_name_map)
        out = _repair_mtf_consistency(out)
        if "display_score" not in out.columns and "score" in out.columns:
            out["display_score"] = pd.to_numeric(out["score"], errors="coerce")
        if "final_score" not in out.columns and "score" in out.columns:
            out["final_score"] = pd.to_numeric(out["score"], errors="coerce")
        if "score_buy" not in out.columns and "score" in out.columns:
            out["score_buy"] = pd.to_numeric(out["score"], errors="coerce")
        if "score_sell" not in out.columns:
            out["score_sell"] = 0.0
        if "symbolname" not in out.columns and "symbol" in out.columns:
            out["symbolname"] = ""
        if "symbol" in out.columns:
            out = out[out["symbol"].fillna("").astype(str).str.strip() != ""].copy()
        time_col = _best_time_col(out)
        if "symbol" in out.columns and time_col and time_col in out.columns:
            out = out.sort_values(["symbol", time_col], ascending=[True, True], na_position="last")
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[GlobalContext] _sanitize_summary_history_df failed tf=%s source=%s", tf, source)
        return _safe_df(df)


class GlobalContext:
    def __init__(self):
        self._lock = threading.RLock()
        self.symbol_name_map: Dict[str, str] = {}
        self._summary_history: Dict[Any, Dict[str, pd.DataFrame]] = {}
        self._merged_summary: Dict[Any, Dict[str, pd.DataFrame]] = {}
        self._push_df = pd.DataFrame()
        self._ranking_df = pd.DataFrame()
        self.runtime: Dict[str, Any] = {}
        # pending_entries も allow_orders と同様、属性未初期化のまま
        # global_data.pending_entries に直接アクセスされると AttributeError になる。
        # 実体の一元管理は trading/entry/pending_manager.py の _ensure_root() だが、
        # それが呼ばれる前でも安全な dict を返せるよう、ここでも空 dict を保証する。
        self.pending_entries: Dict[str, list] = {}
        # allow_orders 未設定のまま position_filter 等が getattr(..., False) で
        # 全件 ENTRY禁止と誤判定するのを防ぐため、起動時パッチが走る前から
        # env ベースの妥当な初期値を持たせる。
        self.allow_orders, self._allow_orders_init_reason = _resolve_allow_orders()
        logger.info(
            "[GlobalContext] allow_orders initial=%s reason=%s",
            self.allow_orders,
            self._allow_orders_init_reason,
        )

    def set_symbol_name_map(self, mp: Dict[str, str]) -> None:
        with self._lock:
            self.symbol_name_map = {str(k).strip(): str(v).strip() for k, v in dict(mp or {}).items() if str(k).strip()}

    def get_symbol_name_map(self) -> Dict[str, str]:
        with self._lock:
            return dict(self.symbol_name_map)

    def set_push_df(self, df: Any, caller: Optional[str] = None) -> pd.DataFrame:
        with self._lock:
            out = _safe_df(df)
            self._push_df = out
            logger.info("[PUSH DF SET] caller=%s rows=%s cols=%s", caller or _caller_name(2), len(out), len(out.columns) if isinstance(out, pd.DataFrame) else 0)
            return out

    def get_push_df(self) -> pd.DataFrame:
        with self._lock:
            return self._push_df.copy()

    def set_ranking_df(self, df: Any, caller: Optional[str] = None) -> pd.DataFrame:
        with self._lock:
            out = _safe_df(df)
            self._ranking_df = out
            logger.info("[RANKING DF SET] caller=%s rows=%s cols=%s", caller or _caller_name(2), len(out), len(out.columns) if isinstance(out, pd.DataFrame) else 0)
            return out

    def get_ranking_df(self) -> pd.DataFrame:
        with self._lock:
            return self._ranking_df.copy()

    def set_summary_history(self, tf: Any, df: Any, source: str = "legacy", caller: Optional[str] = None) -> pd.DataFrame:
        with self._lock:
            src = str(source or "legacy").lower()
            out = _sanitize_summary_history_df(df, tf=tf, source=src, symbol_name_map=self.symbol_name_map)
            self._summary_history.setdefault(tf, {})[src] = out
            latest = None
            try:
                if "datetime" in out.columns and not out.empty:
                    latest = out["datetime"].max()
            except Exception:
                pass
            logger.info("[SUMMARY HISTORY SET] tf=%s source=%s caller=%s rows=%s symbols=%s latest_dt=%s completed=%s", tf, src, caller or _caller_name(2), len(out), out["symbol"].nunique() if "symbol" in out.columns and not out.empty else 0, latest, _is_completed_summary_df(out))
            _log_df_profile("[SUMMARY HISTORY STORED]", tf, src, out)
            return out

    def get_summary_history(self, tf: Any, source: str = "legacy") -> pd.DataFrame:
        with self._lock:
            src = str(source or "legacy").lower()
            df = self._summary_history.get(tf, {}).get(src)
            if df is None:
                return pd.DataFrame()
            out = df.copy()
            _log_df_profile("[SUMMARY HISTORY GET]", tf, src, out)
            return out

    def set_merged_summary(self, tf: Any, df: Any, source: str = "legacy", caller: Optional[str] = None) -> pd.DataFrame:
        with self._lock:
            src = str(source or "legacy").lower()
            out = _sanitize_summary_df(df, tf=tf, source=src, symbol_name_map=self.symbol_name_map)
            self._merged_summary.setdefault(tf, {})[src] = out
            latest = None
            try:
                if "datetime" in out.columns and not out.empty:
                    latest = out["datetime"].max()
            except Exception:
                pass
            logger.info("[MERGED SET] tf=%s source=%s caller=%s rows=%s symbols=%s latest_dt=%s completed=%s", tf, src, caller or _caller_name(2), len(out), out["symbol"].nunique() if "symbol" in out.columns and not out.empty else 0, latest, _is_completed_summary_df(out))
            _log_df_profile("[MERGED STORED]", tf, src, out)
            return out

    def get_merged_summary(self, tf: Any, source: Optional[str] = None) -> pd.DataFrame:
        # stale-guard は旧 core/startup/summary_stale_guard_patch.py から移設。
        # ロック保持時間を変えないため、_get_merged_summary_impl の外側で
        # 適用する (元のmonkeypatchと同じ位置関係)。
        df = self._get_merged_summary_impl(tf, source=source)
        src = source if source is not None else "push"
        return drop_stale_summary_rows(df, source=src, tf=tf, label="merged_get")

    def _get_merged_summary_impl(self, tf: Any, source: Optional[str] = None) -> pd.DataFrame:
        with self._lock:
            if source is not None:
                src = str(source or "legacy").lower()
                df = self._merged_summary.get(tf, {}).get(src)
                if df is None:
                    return pd.DataFrame()
                out = df.copy()
                _log_df_profile("[MERGED GET]", tf, src, out)
                return out
            order = ("push", "legacy", "ranking", "push-cache")
            logger.warning("[MERGED GET] source unspecified -> completed-only fallback order %s tf=%s", " -> ".join(order), tf)
            for src in order:
                df = self._merged_summary.get(tf, {}).get(src)
                if df is None:
                    continue
                out = df.copy()
                if _is_completed_summary_df(out):
                    _log_df_profile("[MERGED GET FALLBACK]", tf, src, out)
                    return out
            return pd.DataFrame()

    def set_runtime(self, key: str, value: Any) -> None:
        with self._lock:
            self.runtime[str(key)] = value

    def get_runtime(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self.runtime.get(str(key), default)


global_context = GlobalContext()


def set_symbol_name_map(mp: Dict[str, str]) -> None:
    return global_context.set_symbol_name_map(mp)


def get_symbol_name_map() -> Dict[str, str]:
    return global_context.get_symbol_name_map()


def set_push_df(df: Any, caller: Optional[str] = None) -> pd.DataFrame:
    return global_context.set_push_df(df, caller=caller)


def get_push_df() -> pd.DataFrame:
    return global_context.get_push_df()


def set_ranking_df(df: Any, caller: Optional[str] = None) -> pd.DataFrame:
    return global_context.set_ranking_df(df, caller=caller)


def get_ranking_df() -> pd.DataFrame:
    return global_context.get_ranking_df()


def set_summary_history(tf: Any = 1, df: Any = None, source: str = "legacy", caller: Optional[str] = None, **kwargs) -> pd.DataFrame:
    if df is None:
        df = kwargs.get("summary_df")
    return global_context.set_summary_history(tf, df, source=source, caller=caller)


def get_summary_history(tf: Any = 1, source: str = "legacy", **kwargs) -> pd.DataFrame:
    return global_context.get_summary_history(tf, source=source)


def set_merged_summary(tf: Any = 1, df: Any = None, source: str = "legacy", caller: Optional[str] = None, **kwargs) -> pd.DataFrame:
    if df is None:
        df = kwargs.get("summary_df")
    return global_context.set_merged_summary(tf, df, source=source, caller=caller)


def get_merged_summary(tf: Any = 1, source: Optional[str] = None, **kwargs) -> pd.DataFrame:
    return global_context.get_merged_summary(tf, source=source)


# PUSH互換 alias
set_push_summary = set_merged_summary
get_push_summary = get_merged_summary
set_push_merged_summary = set_merged_summary
get_push_merged_summary = get_merged_summary


def set_runtime(key: str, value: Any) -> None:
    return global_context.set_runtime(key, value)


def get_runtime(key: str, default: Any = None) -> Any:
    return global_context.get_runtime(key, default)


__all__ = [
    "global_context",
    "set_symbol_name_map",
    "get_symbol_name_map",
    "set_push_df",
    "get_push_df",
    "set_ranking_df",
    "get_ranking_df",
    "set_summary_history",
    "get_summary_history",
    "set_merged_summary",
    "get_merged_summary",
    "set_push_summary",
    "get_push_summary",
    "set_push_merged_summary",
    "get_push_merged_summary",
    "set_runtime",
    "get_runtime",
]
