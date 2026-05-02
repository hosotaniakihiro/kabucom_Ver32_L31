# ============================================================
# File   : scheduler_jobs/summary/display_prepare.py
# Function:
#   - summary / ranking 表示前の DataFrame 正規化を担当する
#   - 列揺れ吸収、duplicate column 解消、symbol 正規化、datetime 正規化を行う
#   - 1銘柄1行へ寄せ、表示に使える品質へ整形する
#   - latest_dt 抽出、symbols_count 集計、future row 除去を行う
#   - prepare_display_df() により display_runner / runners から使える
#     安定した表示用 DataFrame を返す
# ------------------------------------------------------------
# Version: Ver1.2-PRODUCTION-SUMMARY-DISPLAY-PREPARE-LATEST-FIRST
# ------------------------------------------------------------
# ✔ normalize_df
# ✔ prepare_display_df
# ✔ latest_dt_str
# ✔ symbols_count
# ✔ extract_latest_timestamp
# ✔ clamp_future_rows
# ✔ ensure_df 互換公開
# ✔ duplicate columns / MultiIndex 対応
# ✔ symbol / symbolname / datetime 列揺れ吸収
# ✔ score / buy / sell / slope / mtf / rsi / macd 系の欠損耐性
# ✔ 1銘柄1行へ正規化
# ✔ future row 除去
# ✔ scheduler safe
# ✔ scheduler_jobs.summary.__init__ import compatibility
#
# 【REV1.2 修正】
#   - symbolごと1行化で complete_score より datetime を優先
#   - 1分前の古い行が残る問題を修正
#   - clamp_future_rows を dedupe 前にも実行
#   - latest-priority dedupe ログを追加
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# basic helpers
# ============================================================

def _safe_df(df: Any) -> pd.DataFrame:
    """
    任意オブジェクトを安全に DataFrame 化する内部 helper。

    対応:
      - None
      - DataFrame
      - Series
      - dict
      - list[dict]
      - その他 pandas.DataFrame に渡せる object

    追加処理:
      - MultiIndex column flatten
      - column name string 化
      - inf / -inf を NaN 化
      - index reset
    """
    try:
        if df is None:
            return pd.DataFrame()

        if isinstance(df, pd.DataFrame):
            out = df.copy()
        elif isinstance(df, pd.Series):
            out = pd.DataFrame([df.to_dict()])
        elif isinstance(df, dict):
            out = pd.DataFrame([df])
        else:
            out = pd.DataFrame(df).copy()

        if out.empty:
            return out

        try:
            if isinstance(out.columns, pd.MultiIndex):
                out.columns = [
                    "_".join([str(x) for x in col if x not in ("", None)])
                    for col in out.columns.to_flat_index()
                ]
        except Exception:
            logger.debug(
                "[summary.display_prepare] multiindex flatten failed",
                exc_info=True,
            )

        try:
            out.columns = [str(c) for c in out.columns]
        except Exception:
            logger.debug(
                "[summary.display_prepare] column stringify failed",
                exc_info=True,
            )

        try:
            out.replace([np.inf, -np.inf], np.nan, inplace=True)
        except Exception:
            logger.debug(
                "[summary.display_prepare] inf replace failed",
                exc_info=True,
            )

        return out.reset_index(drop=True)

    except Exception:
        logger.exception("[summary.display_prepare] _safe_df failed")
        return pd.DataFrame()


def ensure_df(df: Any = None) -> pd.DataFrame:
    """
    互換用 DataFrame 正規化 helper。
    """
    return _safe_df(df)


def _coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = _safe_df(df)
    if df.empty:
        return df

    try:
        cols = list(df.columns)
        if len(cols) == len(set(cols)):
            return df
    except Exception:
        return df

    try:
        unique_cols = []
        seen = set()
        for c in df.columns:
            if c not in seen:
                unique_cols.append(c)
                seen.add(c)

        out = {}
        for c in unique_cols:
            idxs = [i for i, name in enumerate(df.columns) if name == c]
            if len(idxs) == 1:
                out[c] = df.iloc[:, idxs[0]]
                continue

            s = df.iloc[:, idxs[0]]
            for j in idxs[1:]:
                try:
                    s = s.combine_first(df.iloc[:, j])
                except Exception:
                    try:
                        s = s.where(s.notna(), df.iloc[:, j])
                    except Exception:
                        pass
            out[c] = s

        return pd.DataFrame(out).reset_index(drop=True)

    except Exception:
        logger.debug(
            "[summary.display_prepare] duplicate column coalesce failed",
            exc_info=True,
        )
        try:
            return df.loc[:, ~df.columns.duplicated(keep="last")].copy().reset_index(drop=True)
        except Exception:
            return df


def _normalize_symbol_value(v: Any) -> str:
    try:
        if pd.isna(v):
            return ""
        s = str(v).strip()
    except Exception:
        return ""

    if not s:
        return ""

    if s.endswith(".0"):
        s2 = s[:-2]
        if s2.isdigit():
            return s2

    return s


def _pick_text_series(df: pd.DataFrame, candidates, default="") -> pd.Series:
    for col in candidates:
        if col in df.columns:
            try:
                return df[col].fillna(default).astype(str)
            except Exception:
                continue
    return pd.Series(default, index=df.index, dtype="object")


def _pick_numeric_series(df: pd.DataFrame, candidates, default=np.nan) -> pd.Series:
    for col in candidates:
        if col in df.columns:
            try:
                s = pd.to_numeric(df[col], errors="coerce")
                s = s.replace([np.inf, -np.inf], np.nan)
                return s.fillna(default) if not pd.isna(default) else s
            except Exception:
                continue
    return pd.Series(default, index=df.index, dtype="float64")


def _resolve_symbolname_series(df: pd.DataFrame) -> pd.Series:
    symbol_s = _pick_text_series(df, ["symbol"], default="").astype(str).str.strip()
    symbolname_s = _pick_text_series(df, ["symbolname"], default="").astype(str).str.strip()
    name_s = _pick_text_series(df, ["name"], default="").astype(str).str.strip()

    out = symbolname_s.copy()
    out = out.mask(out.eq(""), name_s)

    try:
        from global_state import global_data

        mp = getattr(global_data, "symbol_name_map", {})
        if isinstance(mp, dict) and mp:
            mapped = symbol_s.map(lambda x: str(mp.get(str(x).strip(), "")).strip())
            out = out.mask(out.eq(""), mapped)
    except Exception:
        logger.debug(
            "[summary.display_prepare] symbolname map complement failed",
            exc_info=True,
        )

    out = out.mask(out.eq(""), symbol_s)
    out = out.fillna("").astype(str).str.strip()
    out = out.mask(out.eq(""), symbol_s)
    return out


def _coerce_datetime_series(df: pd.DataFrame, candidates) -> pd.Series:
    for col in candidates:
        if col in df.columns:
            try:
                s = pd.to_datetime(df[col], errors="coerce")
                try:
                    s = s.dt.tz_localize(None)
                except Exception:
                    pass
                return s
            except Exception:
                continue
    return pd.Series(pd.NaT, index=df.index)


def _ensure_display_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty:
        return out

    out = _coalesce_duplicate_columns(out)

    if "symbol" not in out.columns:
        return pd.DataFrame()

    out["symbol"] = out["symbol"].map(_normalize_symbol_value)
    out = out[out["symbol"] != ""].copy()
    if out.empty:
        return out

    out["symbolname_view"] = _resolve_symbolname_series(out)

    # datetime candidates
    out["__display_dt__"] = _coerce_datetime_series(
        out,
        ["datetime", "end_time", "start_time", "time"],
    )

    # display numeric columns
    out["disp_score"] = _pick_numeric_series(
        out,
        ["score", "display_score", "final_score"],
        default=0.0,
    )
    out["disp_buy_score"] = _pick_numeric_series(
        out,
        ["score_buy", "buy_score", "buy"],
        default=0.0,
    )
    out["disp_sell_score"] = _pick_numeric_series(
        out,
        ["score_sell", "sell_score", "sell"],
        default=0.0,
    ).abs()

    out["disp_total_score"] = _pick_numeric_series(
        out,
        ["score_total", "total_score", "combined_score", "display_score", "score", "final_score"],
        default=0.0,
    )
    if float(out["disp_total_score"].abs().sum()) == 0.0:
        out["disp_total_score"] = out["disp_buy_score"] - out["disp_sell_score"]

    out["disp_final_score"] = _pick_numeric_series(
        out,
        ["final_score", "display_score", "score_total", "score"],
        default=0.0,
    )
    if float(out["disp_final_score"].abs().sum()) == 0.0:
        out["disp_final_score"] = out["disp_total_score"]

    out["disp_slope"] = _pick_numeric_series(
        out,
        ["slope", "score_slope", "slope_atr_scaled", "ma75_slope"],
        default=0.0,
    )
    out["disp_score_slope"] = _pick_numeric_series(
        out,
        ["score_slope", "slope_atr_scaled", "slope"],
        default=0.0,
    )

    out["disp_mtf"] = _pick_numeric_series(
        out,
        ["mtf", "score_mtf", "mtf_score", "mtf_alignment"],
        default=0.0,
    )
    out["disp_score_mtf"] = _pick_numeric_series(
        out,
        ["score_mtf", "mtf_score", "mtf"],
        default=0.0,
    )

    out["disp_rsi"] = _pick_numeric_series(out, ["rsi", "RSI"], default=np.nan)
    out["disp_macd"] = _pick_numeric_series(out, ["macd", "MACD"], default=np.nan)
    out["disp_signal"] = _pick_numeric_series(out, ["signal", "macd_signal", "SIGNAL"], default=np.nan)

    out["disp_base"] = _pick_numeric_series(
        out,
        ["base", "score_base", "_score_base"],
        default=np.nan,
    )
    out["disp_trend"] = _pick_numeric_series(
        out,
        ["trend", "score_trend", "_score_trend"],
        default=np.nan,
    )
    out["disp_mom"] = _pick_numeric_series(
        out,
        ["mom", "momentum", "score_momentum", "_score_momentum"],
        default=np.nan,
    )
    out["disp_vel"] = _pick_numeric_series(
        out,
        ["vel", "velocity", "score_velocity", "_score_velocity"],
        default=np.nan,
    )
    out["disp_pen"] = _pick_numeric_series(
        out,
        ["pen", "penalty", "penalty_score", "direction_penalty", "direction_penalty_score"],
        default=np.nan,
    )

    out["disp_close"] = _pick_numeric_series(
        out,
        ["close", "close_price", "current_price", "price"],
        default=np.nan,
    )

    return out.reset_index(drop=True)


# ============================================================
# main normalization
# ============================================================

def normalize_df(df: Any) -> pd.DataFrame:
    """
    summary / ranking 表示用の基本正規化。
    """
    try:
        out = _ensure_display_columns(df)
        if out.empty:
            return out

        # mtf inconsistency repair
        try:
            bad_mask = out["disp_mtf"].fillna(0).eq(0)

            if "score_mtf" in out.columns:
                raw_score_mtf = pd.to_numeric(out["score_mtf"], errors="coerce").fillna(0)
                out.loc[bad_mask & raw_score_mtf.gt(0), "score_mtf"] = 0.0

            if "mtf_score" in out.columns:
                raw_mtf_score = pd.to_numeric(out["mtf_score"], errors="coerce").fillna(0)
                out.loc[bad_mask & raw_mtf_score.gt(0), "mtf_score"] = 0.0

            if "final_score" in out.columns:
                raw_final = pd.to_numeric(out["final_score"], errors="coerce").fillna(0)
                repl_mask = bad_mask & raw_final.gt(0) & out["disp_total_score"].notna()
                if repl_mask.any():
                    out.loc[repl_mask, "final_score"] = out.loc[repl_mask, "disp_total_score"]
        except Exception:
            logger.debug(
                "[summary.display_prepare] mtf consistency repair failed",
                exc_info=True,
            )

        return out.reset_index(drop=True)

    except Exception:
        logger.exception("[summary.display_prepare] normalize_df failed")
        return _safe_df(df)


def _dedupe_one_row_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    """
    symbolごとに1行へ正規化する。

    重要:
      最新表示では「完全度」より「datetimeの新しさ」を優先する。

    旧仕様:
      symbol → _complete_score → __display_dt__

    問題:
      1分前の行のほうが指標列が揃っていると、
      最新行ではなく1分前の行が選ばれてしまう。

    新仕様:
      symbol → __display_dt__ → _complete_score

    これにより、同一symbolでは最新datetimeの行を優先し、
    同じdatetime内でだけ complete_score を使う。
    """
    try:
        out = normalize_df(df)
        if out.empty:
            return out

        complete_score = pd.Series(0, index=out.index, dtype="int64")

        for c, w in [
            ("symbolname_view", 10),
            ("disp_total_score", 8),
            ("disp_buy_score", 6),
            ("disp_sell_score", 6),
            ("disp_final_score", 6),
            ("disp_slope", 4),
            ("disp_score_slope", 4),
            ("disp_mtf", 4),
            ("disp_score_mtf", 4),
            ("disp_rsi", 3),
            ("disp_macd", 3),
            ("disp_signal", 3),
            ("disp_close", 1),
        ]:
            try:
                if c == "symbolname_view":
                    s = out[c].fillna("").astype(str).str.strip().ne("")
                else:
                    s = pd.to_numeric(out[c], errors="coerce").notna()
                complete_score += s.astype(int) * w
            except Exception:
                continue

        out["_complete_score"] = complete_score

        # ----------------------------------------------------
        # datetime 正規化
        # ----------------------------------------------------
        if "__display_dt__" in out.columns:
            out["__display_dt__"] = pd.to_datetime(out["__display_dt__"], errors="coerce")
            try:
                out["__display_dt__"] = out["__display_dt__"].dt.tz_localize(None)
            except Exception:
                pass

        before = len(out)

        # ----------------------------------------------------
        # 重要:
        # 最新時刻を complete_score より優先する。
        # ----------------------------------------------------
        if "__display_dt__" in out.columns:
            out = out.sort_values(
                ["symbol", "__display_dt__", "_complete_score"],
                ascending=[True, False, False],
                na_position="last",
                kind="mergesort",
            )
        else:
            out = out.sort_values(
                ["symbol", "_complete_score"],
                ascending=[True, False],
                na_position="last",
                kind="mergesort",
            )

        out = out.drop_duplicates(subset=["symbol"], keep="first").copy()
        after = len(out)

        try:
            logger.info(
                "[summary.display_prepare] dedupe latest-priority before=%s after=%s "
                "symbols=%s dt_min=%s dt_max=%s",
                before,
                after,
                int(out["symbol"].nunique()) if "symbol" in out.columns else 0,
                out["__display_dt__"].min() if "__display_dt__" in out.columns else None,
                out["__display_dt__"].max() if "__display_dt__" in out.columns else None,
            )
        except Exception:
            pass

        return out.drop(columns=["_complete_score"], errors="ignore").reset_index(drop=True)

    except Exception:
        logger.exception("[summary.display_prepare] dedupe one row per symbol failed")
        return normalize_df(df)


def prepare_display_df(
    df: Any,
    interval: int = 1,
    now: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    """
    display_runner から呼ばれる表示直前整形。

    重要:
      future row を先に除外してから、
      symbolごとに最新行を選ぶ。
    """
    try:
        out0 = normalize_df(df)
        if out0.empty:
            return out0

        # ----------------------------------------------------
        # future row を先に落とす
        # ----------------------------------------------------
        out0 = clamp_future_rows(out0, interval=interval, now=now)
        if out0.empty:
            return out0.reset_index(drop=True)

        # ----------------------------------------------------
        # その後、symbolごとに最新行を選ぶ
        # ----------------------------------------------------
        out = _dedupe_one_row_per_symbol(out0)

        # 念のため再度 future clamp
        out = clamp_future_rows(out, interval=interval, now=now)

        # 明らかに無効な symbol 行を除外
        if not out.empty and "symbol" in out.columns:
            out["symbol"] = out["symbol"].map(_normalize_symbol_value)
            out = out[out["symbol"] != ""].copy()

        try:
            logger.info(
                "[summary.display_prepare] prepare_display_df done interval=%s rows=%s symbols=%s dt_min=%s dt_max=%s",
                interval,
                len(out),
                int(out["symbol"].nunique()) if "symbol" in out.columns and not out.empty else 0,
                out["__display_dt__"].min() if "__display_dt__" in out.columns and not out.empty else None,
                out["__display_dt__"].max() if "__display_dt__" in out.columns and not out.empty else None,
            )
        except Exception:
            pass

        return out.reset_index(drop=True)

    except Exception:
        logger.exception(
            "[summary.display_prepare] prepare_display_df failed interval=%s",
            interval,
        )
        return normalize_df(df)


# ============================================================
# metadata helpers
# ============================================================

def extract_latest_timestamp(df: Any) -> Optional[dt.datetime]:
    try:
        work = _safe_df(df)
        if work.empty:
            return None

        if "__display_dt__" in work.columns:
            s = pd.to_datetime(work["__display_dt__"], errors="coerce").dropna()
            if not s.empty:
                ts = s.max()
                if pd.isna(ts):
                    return None
                return ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts

        for col in ("datetime", "end_time", "start_time", "time"):
            if col in work.columns:
                s = pd.to_datetime(work[col], errors="coerce").dropna()
                if not s.empty:
                    ts = s.max()
                    if pd.isna(ts):
                        return None
                    try:
                        ts = ts.tz_localize(None)
                    except Exception:
                        pass
                    return ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts

        return None

    except Exception:
        logger.debug(
            "[summary.display_prepare] extract_latest_timestamp failed",
            exc_info=True,
        )
        return None


def latest_dt_str(df: Any) -> Optional[str]:
    try:
        ts = extract_latest_timestamp(df)
        if ts is None:
            return None
        return str(ts)
    except Exception:
        return None


def symbols_count(df: Any) -> int:
    try:
        work = _safe_df(df)
        if work.empty or "symbol" not in work.columns:
            return 0
        s = work["symbol"].map(_normalize_symbol_value)
        s = s[s != ""]
        return int(s.nunique())
    except Exception:
        return 0


# ============================================================
# future row clamp
# ============================================================

def _floor_to_interval(now: dt.datetime, interval: int) -> dt.datetime:
    try:
        minute = (now.minute // int(interval)) * int(interval)
        return now.replace(minute=minute, second=0, microsecond=0)
    except Exception:
        return now.replace(second=0, microsecond=0)


def clamp_future_rows(
    df: Any,
    interval: int = 1,
    now: Optional[dt.datetime] = None,
) -> pd.DataFrame:
    """
    表示時点より未来の row を除去する。
    """
    try:
        out = _safe_df(df)
        if out.empty:
            return out

        now2 = (now or dt.datetime.now()).replace(microsecond=0)
        cutoff = _floor_to_interval(now2, int(interval))

        dt_col = None
        if "__display_dt__" in out.columns:
            dt_col = "__display_dt__"
        else:
            for c in ("datetime", "end_time", "start_time", "time"):
                if c in out.columns:
                    dt_col = c
                    break

        if dt_col is None:
            return out.reset_index(drop=True)

        s = pd.to_datetime(out[dt_col], errors="coerce")
        try:
            s = s.dt.tz_localize(None)
        except Exception:
            pass

        before = len(out)
        out = out[(s.isna()) | (s <= cutoff)].copy()
        after = len(out)

        logger.info(
            "[summary.display_prepare] clamp_future_rows interval=%s before=%d after=%d cutoff=%s",
            interval,
            before,
            after,
            cutoff,
        )

        return out.reset_index(drop=True)

    except Exception:
        logger.exception(
            "[summary.display_prepare] clamp_future_rows failed interval=%s",
            interval,
        )
        return _safe_df(df)


def get_primary_dt_col(df: Any) -> Optional[str]:
    """
    表示・ソート・future clamp 用の主要 datetime カラム名を返す互換関数。

    優先順:
      1. __display_dt__
      2. datetime
      3. end_time
      4. start_time
      5. tick_time
      6. time
      7. inserted_at
      8. received_at
      9. created_at
    """
    try:
        work = _safe_df(df)
        if work.empty:
            return None

        for col in (
            "__display_dt__",
            "datetime",
            "end_time",
            "start_time",
            "tick_time",
            "time",
            "inserted_at",
            "received_at",
            "created_at",
        ):
            if col in work.columns:
                return col

        return None

    except Exception:
        logger.exception("[summary.display_prepare] get_primary_dt_col failed")
        return None


def select_latest_slot_rows(
    df: Any,
    *,
    interval: int = 1,
    now: Optional[dt.datetime] = None,
    dt_col: Optional[str] = None,
    per_symbol: bool = True,
) -> pd.DataFrame:
    """
    表示用 DataFrame から最新スロットの行を選択する互換関数。

    目的:
      - scheduler_jobs.summary.__init__ から import される旧公開APIを復旧する
      - summary / ranking 表示で、最新時刻の行だけを選ぶ
      - future row を除外したうえで最新 slot を採用する
    """
    try:
        out = _safe_df(df)
        if out.empty:
            return out

        out = _coalesce_duplicate_columns(out)

        # future row を先に落とす
        out = clamp_future_rows(out, interval=interval, now=now)
        if out.empty:
            return out

        use_dt_col = dt_col or get_primary_dt_col(out)

        if use_dt_col is None or use_dt_col not in out.columns:
            logger.info(
                "[summary.display_prepare] select_latest_slot_rows no dt col -> return as-is rows=%d",
                len(out),
            )
            return out.reset_index(drop=True)

        dt_s = pd.to_datetime(out[use_dt_col], errors="coerce")
        try:
            dt_s = dt_s.dt.tz_localize(None)
        except Exception:
            pass

        out = out.loc[dt_s.notna()].copy()
        if out.empty:
            return out.reset_index(drop=True)

        out["__slot_dt__"] = pd.to_datetime(out[use_dt_col], errors="coerce")
        try:
            out["__slot_dt__"] = out["__slot_dt__"].dt.tz_localize(None)
        except Exception:
            pass

        if per_symbol and "symbol" in out.columns:
            out["symbol"] = out["symbol"].map(_normalize_symbol_value)
            out = out[out["symbol"] != ""].copy()
            if out.empty:
                return out.drop(columns=["__slot_dt__"], errors="ignore").reset_index(drop=True)

            out = out.sort_values(
                ["symbol", "__slot_dt__"],
                ascending=[True, False],
                na_position="last",
                kind="mergesort",
            )
            out = out.drop_duplicates(subset=["symbol"], keep="first").copy()

            logger.info(
                "[summary.display_prepare] select_latest_slot_rows per_symbol interval=%s rows=%d symbols=%d dt_min=%s dt_max=%s",
                interval,
                len(out),
                int(out["symbol"].nunique()) if "symbol" in out.columns else 0,
                out["__slot_dt__"].min() if "__slot_dt__" in out.columns else None,
                out["__slot_dt__"].max() if "__slot_dt__" in out.columns else None,
            )

            return out.drop(columns=["__slot_dt__"], errors="ignore").reset_index(drop=True)

        latest_dt = out["__slot_dt__"].max()
        selected = out.loc[out["__slot_dt__"].eq(latest_dt)].copy()

        logger.info(
            "[summary.display_prepare] select_latest_slot_rows latest_slot interval=%s rows=%d latest_dt=%s",
            interval,
            len(selected),
            latest_dt,
        )

        return selected.drop(columns=["__slot_dt__"], errors="ignore").reset_index(drop=True)

    except Exception:
        logger.exception(
            "[summary.display_prepare] select_latest_slot_rows failed interval=%s",
            interval,
        )
        return _safe_df(df)


# ============================================================
# public exports
# ============================================================

__all__ = [
    "ensure_df",
    "normalize_df",
    "prepare_display_df",
    "latest_dt_str",
    "symbols_count",
    "extract_latest_timestamp",
    "clamp_future_rows",
    "get_primary_dt_col",
    "select_latest_slot_rows",
]