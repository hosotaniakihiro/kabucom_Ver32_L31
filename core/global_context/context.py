# ============================================================
# File   : core/global_context/context.py
# Version: PRODUCTION-STABLE-REV10.2-SUMMARY-HISTORY-CACHE
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
# 【REV10.2 修正】
#   - 計算用 summary history cache を追加
#
#   - 修正前:
#       set_merged_summary()
#       -> _sanitize_summary_df()
#       -> _latest_one_row_per_symbol()
#       により、DBから読んだ履歴DFが最新1行/銘柄へ圧縮される。
#
#   - 修正後:
#       表示用:
#           set_merged_summary()
#           -> 最新1行/銘柄へ圧縮
#
#       計算用:
#           set_summary_history()
#           -> 履歴DFを圧縮せず保持
#
#   - これにより:
#       DB seed loaded rows=5426
#       SUMMARY HISTORY SET rows=5426
#       MERGED SET rows=313
#     のように、履歴用と表示用を分離できる。
#
# 【重要】
#   - indicator / ranking / scoring が履歴本数を必要とする場合は
#       get_summary_history(tf)
#     を使う。
#
#   - 画面表示 / TOP10 / 最新状態確認は
#       get_merged_summary(tf, source="push")
#     を使う。
# ============================================================

from __future__ import annotations

import inspect
import logging
import threading
import warnings
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

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
                out.loc[idx] = pd.to_datetime(
                    raw.loc[idx],
                    errors="coerce",
                    format=fmt,
                )

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
                out.loc[idx] = pd.to_datetime(
                    combined.loc[idx],
                    errors="coerce",
                    format="%Y-%m-%d %H:%M:%S",
                )

            if time_hm.any():
                idx = time_hm[time_hm].index
                out.loc[idx] = pd.to_datetime(
                    combined.loc[idx],
                    errors="coerce",
                    format="%Y-%m-%d %H:%M",
                )

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
    """
    2026-04-20 10:47:00+09:00 -> 2026-04-20 10:47:00
    UTC変換せず、JSTの壁時計時刻を維持する。
    """
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
        return None
    except Exception:
        return None


def _latest_one_row_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if df.empty or "symbol" not in df.columns:
            return df

        out = df.copy()
        out = out[out["symbol"].astype(str).str.strip() != ""].copy()
        if out.empty:
            return out

        time_col = _best_time_col(out)
        if time_col and time_col in out.columns:
            out[time_col] = pd.to_datetime(out[time_col], errors="coerce")
            out = out.sort_values(
                by=["symbol", time_col],
                ascending=[True, True],
                na_position="last",
            )
        else:
            out = out.reset_index(drop=True)

        out = out.drop_duplicates(subset=["symbol"], keep="last").copy()
        out = out.reset_index(drop=True)
        return out
    except Exception:
        logger.exception("[GlobalContext] _latest_one_row_per_symbol failed")
        return df


def _enrich_symbolname(df: pd.DataFrame, mp: Optional[Dict[str, str]] = None) -> pd.DataFrame:
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
            return {
                "rows": 0,
                "cols": 0,
                "sample_cols": [],
                "score_nonzero": 0,
                "slope_nonzero": 0,
                "slope_atr_scaled_nonzero": 0,
                "score_slope_nonzero": 0,
                "mtf_nonzero": 0,
                "score_mtf_nonzero": 0,
                "mtf_score_nonzero": 0,
                "rsi_nonzero": 0,
                "macd_nonzero": 0,
                "signal_nonzero": 0,
                "close_nonnull": 0,
                "datetime_nonnull": 0,
                "blank_symbolname": 0,
                "unique_symbols": 0,
                "completed_summary": False,
            }

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
        return {
            "rows": -1,
            "cols": -1,
            "sample_cols": [],
        }


def _log_df_profile(prefix: str, tf: Any, source: Optional[str], df: pd.DataFrame) -> None:
    try:
        prof = _profile_df(df)
        logger.info(
            "%s tf=%s source=%s rows=%s cols=%s unique_symbols=%s blank_symbolname=%s completed_summary=%s sample_cols=%s",
            prefix,
            tf,
            source,
            prof.get("rows"),
            prof.get("cols"),
            prof.get("unique_symbols"),
            prof.get("blank_symbolname"),
            prof.get("completed_summary"),
            prof.get("sample_cols"),
        )
        logger.info(
            "%s tf=%s source=%s nonzero score=%s slope=%s slope_atr_scaled=%s score_slope=%s mtf=%s score_mtf=%s mtf_score=%s rsi=%s macd=%s signal=%s close_nonnull=%s datetime_nonnull=%s",
            prefix,
            tf,
            source,
            prof.get("score_nonzero"),
            prof.get("slope_nonzero"),
            prof.get("slope_atr_scaled_nonzero"),
            prof.get("score_slope_nonzero"),
            prof.get("mtf_nonzero"),
            prof.get("score_mtf_nonzero"),
            prof.get("mtf_score_nonzero"),
            prof.get("rsi_nonzero"),
            prof.get("macd_nonzero"),
            prof.get("signal_nonzero"),
            prof.get("close_nonnull"),
            prof.get("datetime_nonnull"),
        )

        show_cols = [
            c for c in [
                "symbol", "symbolname", "score", "score_total", "final_score", "display_score",
                "score_buy", "score_sell", "slope", "slope_atr_scaled", "score_slope",
                "mtf", "score_mtf", "mtf_score",
                "open", "high", "low", "close", "rsi", "macd", "signal", "datetime"
            ] if c in df.columns
        ]
        if show_cols and not df.empty:
            logger.info(
                "%s tf=%s source=%s\n%s",
                prefix,
                tf,
                source,
                df[show_cols].head(20).to_string(index=False),
            )
    except Exception:
        logger.exception(
            "[GlobalContext] _log_df_profile failed prefix=%s tf=%s source=%s",
            prefix,
            tf,
            source,
        )


def _sanitize_summary_df(
    df: Any,
    tf: Any,
    source: str,
    symbol_name_map: Optional[Dict[str, str]] = None,
) -> pd.DataFrame:
    """
    表示用 summary sanitize。

    重要:
      ここでは最新1行/銘柄へ圧縮する。
      履歴を保持したい場合は _sanitize_summary_history_df() を使う。
    """
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

        out = out.reset_index(drop=True)
        return out
    except Exception:
        logger.exception("[GlobalContext] _sanitize_summary_df failed tf=%s source=%s", tf, source)
        return _safe_df(df)


def _sanitize_summary_history_df(
    df: Any,
    tf: Any,
    source: str = "history",
    symbol_name_map: Optional[Dict[str, str]] = None,
) -> pd.DataFrame:
    """
    計算用 summary 履歴 sanitize。

    重要:
      _latest_one_row_per_symbol() を呼ばない。
      そのため、DB seed で読み込んだ履歴を全行保持できる。
    """
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
            out[time_col] = pd.to_datetime(out[time_col], errors="coerce")
            out = (
                out.sort_values(["symbol", time_col], kind="stable")
                .drop_duplicates(subset=["symbol", time_col], keep="last")
                .reset_index(drop=True)
            )
        else:
            out = out.reset_index(drop=True)

        return out

    except Exception:
        logger.exception("[GlobalContext] _sanitize_summary_history_df failed tf=%s source=%s", tf, source)
        return _safe_df(df)


class GlobalContext:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._init_runtime_state()
        logger.info("GlobalContext initialized")

    def _init_runtime_state(self) -> None:
        self.merged_summary_1 = pd.DataFrame()
        self.merged_summary_3 = pd.DataFrame()
        self.merged_summary_5 = pd.DataFrame()
        self.merged_summary_10 = pd.DataFrame()
        self.merged_summary_15 = pd.DataFrame()
        self.merged_summary_30 = pd.DataFrame()
        self.merged_summary_60 = pd.DataFrame()
        self.merged_summary_daily = pd.DataFrame()

        self._merged_by_source: Dict[str, Dict[Any, pd.DataFrame]] = {
            "push": {},
            "legacy": {},
            "ranking": {},
        }

        self._last_rejected_merged_by_source: Dict[str, Dict[Any, pd.DataFrame]] = {
            "push": {},
            "legacy": {},
            "ranking": {},
        }

        self.symbol_name_map: Dict[str, str] = {}
        self.push_df = pd.DataFrame()
        self.ranking_df = pd.DataFrame()

        self.ws_connected = False
        self.push_stream_running = False
        self.subscription_refresh_running = False
        self.push_writer_running = False
        self.summary_running = False
        self.ranking_running = False

        self.last_push_received_at = None
        self.last_push_db_flush_at = None

        self.summary_bootstrap_started = False
        self.summary_bootstrap_done = False
        self.summary_bootstrap_failed = False

        self.allow_orders = False
        self.today_str = ""
        self.ws_url = ""
        self.token_value = None
        self.recent_entry_symbols = []

        self.active_symbols = []
        self.monitor_symbols = []
        self.buy_candidate_symbols = []
        self.sell_candidate_symbols = []
        self.push_symbols = []
        self.runtime_symbols = []
        self.ranking_summary_universe = []
        self.daily_watchlist_symbols = []
        self.daily_watchlist = []
        self.ats_register_targets = []
        self.ats_targets = []
        self.should_register_symbols = []

        self.push_summary_cache = {}
        self.ranking_summary_cache = {}

        # 計算用履歴キャッシュ
        # merged summary は表示用に最新1行/銘柄へ圧縮されるため、
        # indicator / ranking / scoring 用の履歴DFはここに保持する。
        self.summary_history_cache: Dict[Any, pd.DataFrame] = {
            1: pd.DataFrame(),
            3: pd.DataFrame(),
            5: pd.DataFrame(),
        }

    def clear_all(self) -> None:
        try:
            with self._lock:
                self._init_runtime_state()
            logger.info("[GlobalContext] clear_all completed")
        except Exception:
            logger.exception("[GlobalContext] clear_all failed")

    @staticmethod
    def _normalize_tf(tf: Any) -> Any:
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
            return tf
        except Exception:
            return tf

    def _legacy_attr_name(self, tf: Any) -> Optional[str]:
        tf = self._normalize_tf(tf)
        mapping = {
            1: "merged_summary_1",
            3: "merged_summary_3",
            5: "merged_summary_5",
            10: "merged_summary_10",
            15: "merged_summary_15",
            30: "merged_summary_30",
            60: "merged_summary_60",
            "daily": "merged_summary_daily",
        }
        return mapping.get(tf)

    def _set_legacy_push_attr(self, tf: Any, df: pd.DataFrame) -> None:
        try:
            attr = self._legacy_attr_name(tf)
            if not attr:
                return

            prev = getattr(self, attr, None)
            prev_id = hex(id(prev)) if prev is not None else None
            setattr(self, attr, df.copy())
            new_obj = getattr(self, attr, None)
            new_id = hex(id(new_obj)) if new_obj is not None else None
            logger.info(
                "[MERGED SET LEGACY ATTR] tf=%s previous=%s new_id=%s",
                tf,
                prev_id,
                new_id,
            )
        except Exception:
            logger.exception("[GlobalContext] _set_legacy_push_attr failed tf=%s", tf)

    def _legacy_attr_df(self, tf: Any) -> pd.DataFrame:
        try:
            attr = self._legacy_attr_name(tf)
            if not attr:
                return pd.DataFrame()
            return _safe_df(getattr(self, attr, pd.DataFrame()))
        except Exception:
            logger.exception("[GlobalContext] _legacy_attr_df failed tf=%s", tf)
            return pd.DataFrame()

    def _push_summary_cache_df(self, tf: Any) -> pd.DataFrame:
        try:
            tf = int(tf) if tf != "daily" else tf
            if not hasattr(self, "push_summary_cache") or not isinstance(self.push_summary_cache, dict):
                return pd.DataFrame()
            return _safe_df(self.push_summary_cache.get(tf, pd.DataFrame()))
        except Exception:
            logger.exception("[GlobalContext] _push_summary_cache_df failed tf=%s", tf)
            return pd.DataFrame()

    def _get_best_completed_summary(self, tf: Any, source: Optional[str] = None) -> pd.DataFrame:
        try:
            tf = self._normalize_tf(tf)

            if source:
                df = _safe_df(self._merged_by_source.get(source, {}).get(tf, pd.DataFrame()))
                if _is_completed_summary_df(df):
                    return _sanitize_summary_df(df, tf=tf, source=source, symbol_name_map=self.symbol_name_map)

                if source == "push":
                    cache_df = self._push_summary_cache_df(tf)
                    if _is_completed_summary_df(cache_df):
                        return _sanitize_summary_df(cache_df, tf=tf, source="push-cache", symbol_name_map=self.symbol_name_map)

                    legacy_df = self._legacy_attr_df(tf)
                    if _is_completed_summary_df(legacy_df):
                        return _sanitize_summary_df(legacy_df, tf=tf, source="push", symbol_name_map=self.symbol_name_map)

                return pd.DataFrame()

            for src in ("push", "legacy", "ranking"):
                df = _safe_df(self._merged_by_source.get(src, {}).get(tf, pd.DataFrame()))
                if _is_completed_summary_df(df):
                    return _sanitize_summary_df(df, tf=tf, source=src, symbol_name_map=self.symbol_name_map)

            cache_df = self._push_summary_cache_df(tf)
            if _is_completed_summary_df(cache_df):
                return _sanitize_summary_df(cache_df, tf=tf, source="push-cache", symbol_name_map=self.symbol_name_map)

            legacy_push_df = self._legacy_attr_df(tf)
            if _is_completed_summary_df(legacy_push_df):
                return _sanitize_summary_df(legacy_push_df, tf=tf, source="push", symbol_name_map=self.symbol_name_map)

            return pd.DataFrame()
        except Exception:
            logger.exception("[GlobalContext] _get_best_completed_summary failed tf=%s source=%s", tf, source)
            return pd.DataFrame()

    def set_merged_summary(self, tf: Any, df: Any, source: str = "push") -> None:
        tf = self._normalize_tf(tf)
        source = (source or "push").strip().lower()
        caller = _caller_name(2)

        try:
            df_input = _safe_df(df)
            df2 = _sanitize_summary_df(
                df_input,
                tf=tf,
                source=source,
                symbol_name_map=self.symbol_name_map,
            )

            with self._lock:
                if source not in self._merged_by_source:
                    self._merged_by_source[source] = {}
                if source not in self._last_rejected_merged_by_source:
                    self._last_rejected_merged_by_source[source] = {}

                _log_df_profile("[MERGED SET INPUT]", tf, source, df2)

                if not _is_completed_summary_df(df2):
                    self._last_rejected_merged_by_source[source][tf] = df_input.copy()
                    logger.warning(
                        "[MERGED SET REJECTED] tf=%s source=%s caller=%s reason=incomplete_summary rows=%s cols=%s input_cols=%s",
                        tf,
                        source,
                        caller,
                        len(df_input),
                        len(df_input.columns) if isinstance(df_input, pd.DataFrame) else -1,
                        list(df_input.columns) if isinstance(df_input, pd.DataFrame) else [],
                    )
                    return

                self._merged_by_source[source][tf] = df2.copy()

                if source == "push":
                    self._set_legacy_push_attr(tf, df2)

                logger.info(
                    "[MERGED SET] tf=%s source=%s caller=%s rows=%s",
                    tf,
                    source,
                    caller,
                    len(df2),
                )

                _log_df_profile(
                    "[MERGED SET STORED]",
                    tf,
                    source,
                    self._merged_by_source[source][tf],
                )

        except Exception:
            logger.exception(
                "[MERGED SET] failed tf=%s source=%s caller=%s",
                tf,
                source,
                caller,
            )

    def get_merged_summary(self, tf: Any, source: Optional[str] = None) -> pd.DataFrame:
        tf = self._normalize_tf(tf)

        try:
            with self._lock:
                if source:
                    source = source.strip().lower()
                    df = self._get_best_completed_summary(tf=tf, source=source)
                    _log_df_profile("[MERGED GET]", tf, source, df)

                    if df.empty:
                        logger.warning(
                            "[MERGED GET] tf=%s source=%s -> no completed summary available",
                            tf,
                            source,
                        )
                    return df

                logger.warning(
                    "[MERGED GET] source unspecified -> completed-only fallback order push -> legacy -> ranking -> push-cache tf=%s",
                    tf
                )

                df_best = self._get_best_completed_summary(tf=tf, source=None)
                if not df_best.empty:
                    picked_source = None
                    for src in ("push", "legacy", "ranking"):
                        cand = _safe_df(self._merged_by_source.get(src, {}).get(tf, pd.DataFrame()))
                        if _is_completed_summary_df(cand):
                            picked_source = src
                            break

                    if picked_source is None:
                        cache_df = self._push_summary_cache_df(tf)
                        if _is_completed_summary_df(cache_df):
                            picked_source = "push-cache"

                    if picked_source is None:
                        legacy_push_df = self._legacy_attr_df(tf)
                        if _is_completed_summary_df(legacy_push_df):
                            picked_source = "push-legacy-attr"

                    _log_df_profile("[MERGED GET FALLBACK]", tf, picked_source or "completed", df_best)
                    return df_best

                logger.warning(
                    "[MERGED GET] tf=%s source unspecified but no completed summary found; returning empty df",
                    tf
                )
                empty_df = pd.DataFrame()
                _log_df_profile("[MERGED GET FALLBACK]", tf, "empty", empty_df)
                return empty_df

        except Exception:
            logger.exception("[MERGED GET] failed tf=%s source=%s", tf, source)
            return pd.DataFrame()

    def set_push_merged_summary(self, tf: Any, df: Any) -> None:
        self.set_merged_summary(tf=tf, df=df, source="push")

    def get_push_merged_summary(self, tf: Any) -> pd.DataFrame:
        return self.get_merged_summary(tf=tf, source="push")

    def set_ranking_merged_summary(self, tf: Any, df: Any) -> None:
        self.set_merged_summary(tf=tf, df=df, source="ranking")

    def get_ranking_merged_summary(self, tf: Any) -> pd.DataFrame:
        return self.get_merged_summary(tf=tf, source="ranking")

    def set_legacy_merged_summary(self, tf: Any, df: Any) -> None:
        self.set_merged_summary(tf=tf, df=df, source="legacy")

    def get_legacy_merged_summary(self, tf: Any) -> pd.DataFrame:
        return self.get_merged_summary(tf=tf, source="legacy")

    def get_rejected_merged_summary(self, tf: Any, source: str = "push") -> pd.DataFrame:
        try:
            tf = self._normalize_tf(tf)
            source = (source or "push").strip().lower()
            with self._lock:
                df = _safe_df(self._last_rejected_merged_by_source.get(source, {}).get(tf, pd.DataFrame()))
                _log_df_profile("[MERGED GET REJECTED]", tf, source, df)
                return df
        except Exception:
            logger.exception("[GlobalContext] get_rejected_merged_summary failed tf=%s source=%s", tf, source)
            return pd.DataFrame()

    def set_summary_history(self, tf: Any, df: Any, source: str = "push") -> None:
        """
        indicator / ranking / scoring 用の履歴 summary を保存する。

        重要:
          set_merged_summary() は表示用に最新1行/銘柄へ圧縮する。
          こちらは履歴DFを圧縮せず保持する。
        """
        tf = self._normalize_tf(tf)
        source = (source or "push").strip().lower()
        caller = _caller_name(2)

        try:
            df_input = _safe_df(df)
            df2 = _sanitize_summary_history_df(
                df_input,
                tf=tf,
                source=source,
                symbol_name_map=self.symbol_name_map,
            )

            with self._lock:
                if not hasattr(self, "summary_history_cache") or not isinstance(self.summary_history_cache, dict):
                    self.summary_history_cache = {}

                self.summary_history_cache[tf] = df2.copy()

                time_col = _best_time_col(df2)
                latest_dt = None
                if isinstance(df2, pd.DataFrame) and not df2.empty and time_col and time_col in df2.columns:
                    latest_dt = pd.to_datetime(df2[time_col], errors="coerce").max()

                symbol_count = 0
                if isinstance(df2, pd.DataFrame) and "symbol" in df2.columns and not df2.empty:
                    symbol_count = int(df2["symbol"].fillna("").astype(str).str.strip().nunique())

                logger.info(
                    "[SUMMARY HISTORY SET] tf=%s source=%s caller=%s rows=%s symbols=%s latest_dt=%s completed=%s",
                    tf,
                    source,
                    caller,
                    len(df2),
                    symbol_count,
                    latest_dt,
                    _is_completed_summary_df(df2),
                )

                _log_df_profile(
                    "[SUMMARY HISTORY STORED]",
                    tf,
                    source,
                    df2,
                )

        except Exception:
            logger.exception(
                "[SUMMARY HISTORY SET] failed tf=%s source=%s caller=%s",
                tf,
                source,
                caller,
            )

    def get_summary_history(self, tf: Any, source: str = "push") -> pd.DataFrame:
        """
        indicator / ranking / scoring 用の履歴 summary を取得する。
        """
        tf = self._normalize_tf(tf)
        source = (source or "push").strip().lower()

        try:
            with self._lock:
                if not hasattr(self, "summary_history_cache") or not isinstance(self.summary_history_cache, dict):
                    logger.warning("[SUMMARY HISTORY GET] cache missing tf=%s source=%s", tf, source)
                    return pd.DataFrame()

                df = _safe_df(self.summary_history_cache.get(tf, pd.DataFrame()))
                _log_df_profile("[SUMMARY HISTORY GET]", tf, source, df)
                return df

        except Exception:
            logger.exception("[SUMMARY HISTORY GET] failed tf=%s source=%s", tf, source)
            return pd.DataFrame()

    def set_symbol_name_map(self, mp: Dict[str, str]) -> None:
        try:
            with self._lock:
                self.symbol_name_map = {
                    str(k).strip(): str(v).strip()
                    for k, v in dict(mp or {}).items()
                    if str(k).strip()
                }

                for source, per_tf in self._merged_by_source.items():
                    for tf, df in list(per_tf.items()):
                        df2 = _sanitize_summary_df(
                            df,
                            tf=tf,
                            source=source,
                            symbol_name_map=self.symbol_name_map,
                        )
                        per_tf[tf] = df2

                        if source == "push":
                            self._set_legacy_push_attr(tf, df2)

                if hasattr(self, "summary_history_cache") and isinstance(self.summary_history_cache, dict):
                    for tf, df in list(self.summary_history_cache.items()):
                        self.summary_history_cache[tf] = _sanitize_summary_history_df(
                            df,
                            tf=tf,
                            source="history",
                            symbol_name_map=self.symbol_name_map,
                        )

                logger.info(
                    "[GlobalContext] set_symbol_name_map done size=%s",
                    len(self.symbol_name_map),
                )
        except Exception:
            logger.exception("[GlobalContext] set_symbol_name_map failed")

    def get_symbol_name_map(self) -> Dict[str, str]:
        try:
            with self._lock:
                return dict(self.symbol_name_map or {})
        except Exception:
            logger.exception("[GlobalContext] get_symbol_name_map failed")
            return {}

    def set_push_df(self, df: Any) -> None:
        try:
            with self._lock:
                self.push_df = _safe_df(df)
        except Exception:
            logger.exception("[GlobalContext] set_push_df failed")

    def get_push_df(self) -> pd.DataFrame:
        try:
            with self._lock:
                return _safe_df(self.push_df)
        except Exception:
            logger.exception("[GlobalContext] get_push_df failed")
            return pd.DataFrame()

    def set_ranking_df(self, df: Any) -> None:
        try:
            with self._lock:
                self.ranking_df = _safe_df(df)
        except Exception:
            logger.exception("[GlobalContext] set_ranking_df failed")

    def get_ranking_df(self) -> pd.DataFrame:
        try:
            with self._lock:
                return _safe_df(self.ranking_df)
        except Exception:
            logger.exception("[GlobalContext] get_ranking_df failed")
            return pd.DataFrame()

    def set_push_summary(self, tf: int, df):
        try:
            tf = int(tf)
            df2 = _sanitize_summary_df(df, tf=tf, source="push", symbol_name_map=self.symbol_name_map)

            if not hasattr(self, "push_summary_cache") or not isinstance(self.push_summary_cache, dict):
                self.push_summary_cache = {}

            self.push_summary_cache[tf] = df2.copy() if hasattr(df2, "copy") else df2

            logger.info(
                "[GlobalContext] set_push_summary tf=%s rows=%s completed=%s",
                tf,
                len(df2) if isinstance(df2, pd.DataFrame) else -1,
                _is_completed_summary_df(df2) if isinstance(df2, pd.DataFrame) else False,
            )
        except Exception:
            logger.exception("[GlobalContext] set_push_summary failed tf=%s", tf)

    def get_push_summary(self, tf: int):
        try:
            tf = int(tf)
            if not hasattr(self, "push_summary_cache") or not isinstance(self.push_summary_cache, dict):
                return None
            df = self.push_summary_cache.get(tf)
            if isinstance(df, pd.DataFrame):
                return df.copy()
            return df
        except Exception:
            logger.exception("[GlobalContext] get_push_summary failed tf=%s", tf)
            return None

    def set_ranking_summary(self, tf: int, df):
        try:
            tf = int(tf)
            df2 = _sanitize_summary_df(df, tf=tf, source="ranking", symbol_name_map=self.symbol_name_map)

            if not hasattr(self, "ranking_summary_cache") or not isinstance(self.ranking_summary_cache, dict):
                self.ranking_summary_cache = {}

            self.ranking_summary_cache[tf] = df2.copy() if hasattr(df2, "copy") else df2

            logger.info(
                "[GlobalContext] set_ranking_summary tf=%s rows=%s completed=%s",
                tf,
                len(df2) if isinstance(df2, pd.DataFrame) else -1,
                _is_completed_summary_df(df2) if isinstance(df2, pd.DataFrame) else False,
            )
        except Exception:
            logger.exception("[GlobalContext] set_ranking_summary failed tf=%s", tf)

    def get_ranking_summary(self, tf: int):
        try:
            tf = int(tf)
            if not hasattr(self, "ranking_summary_cache") or not isinstance(self.ranking_summary_cache, dict):
                return None
            df = self.ranking_summary_cache.get(tf)
            if isinstance(df, pd.DataFrame):
                return df.copy()
            return df
        except Exception:
            logger.exception("[GlobalContext] get_ranking_summary failed tf=%s", tf)
            return None


global_data = GlobalContext()
global_context = global_data
GC = global_data