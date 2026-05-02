# ==========================================================
# File   : trading/summary/controller_projection.py
# Version: Ver1.2-PRODUCTION-HARDENED-CONTROLLER-PROJECTION-DISPLAY-READY
#          -TECHNICAL-READY-STRICT
#          -DISPLAY-READY-SHORT-HISTORY
#          -MATURE-FIRST-DATETIME-HARDENED
# ----------------------------------------------------------
# 【概要】
#   summary_controller 用 projection / readiness / diagnostics
#
# 【主な機能】
#   - latest projection
#   - history length attach
#   - technical_ready rebuild
#   - display_ready rebuild
#   - mature-first selection
#   - diagnostics logging
#
# 【今回の修正】
#   - technical_ready は厳格なまま維持
#   - short-history でも score + close がある場合 display_ready=True
#   - TOP10表示用に display_ready を追加
#   - log_df_state / log_scoring_probe に display_ready を追加
#   - latest projection 後に display_ready を再構築
#
# 【重要】
#   technical_ready:
#       RSI/MACD/slope/mtf/MA75 などが十分揃った行だけ True
#
#   display_ready:
#       score / score_buy / score_sell / close があるなら短履歴でも True
#       TOP10表示用の緩い readiness
# ==========================================================

from __future__ import annotations

import logging
import warnings
from typing import Callable

import pandas as pd

logger = logging.getLogger(__name__)

MIN_HISTORY_ROWS_RSI = 14
MIN_HISTORY_ROWS_MACD = 26
MIN_HISTORY_ROWS_MA75 = 75
MIN_HISTORY_ROWS_STRONG = 80


# ==========================================================
# datetime helper
# ==========================================================

def _safe_to_datetime(s) -> pd.Series:
    try:
        if isinstance(s, pd.Series):
            if pd.api.types.is_datetime64_any_dtype(s):
                out = pd.to_datetime(s, errors="coerce")
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    out = pd.to_datetime(s, errors="coerce")
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                out = pd.to_datetime(pd.Series(s), errors="coerce")

        try:
            out = out.dt.tz_localize(None)
        except Exception:
            pass

        return out
    except Exception:
        try:
            return pd.Series(pd.NaT, index=getattr(s, "index", None), dtype="datetime64[ns]")
        except Exception:
            return pd.Series(dtype="datetime64[ns]")


# ==========================================================
# small safe helpers
# ==========================================================

def _safe_len(df) -> int:
    try:
        return 0 if df is None else int(len(df))
    except Exception:
        return 0


def _safe_symbol_count(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return 0
        s = df["symbol"].fillna("").astype(str).str.strip()
        s = s.replace({"": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA})
        return int(s.dropna().nunique())
    except Exception:
        return 0


def _safe_latest_dt(df: pd.DataFrame):
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        for c in ("datetime", "end_time", "snapshot_time", "tick_time"):
            if c in df.columns:
                s = _safe_to_datetime(df[c]).dropna()
                if not s.empty:
                    return s.max()
        return None
    except Exception:
        return None


def _safe_numeric_nonzero(df: pd.DataFrame, cols: tuple[str, ...]) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return 0
        best = 0
        for c in cols:
            if c in df.columns:
                s = pd.to_numeric(df[c], errors="coerce").fillna(0)
                best = max(best, int((s != 0).sum()))
        return best
    except Exception:
        return 0


def _safe_numeric_nonnull(df: pd.DataFrame, cols: tuple[str, ...]) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return 0
        best = 0
        for c in cols:
            if c in df.columns:
                s = pd.to_numeric(df[c], errors="coerce")
                best = max(best, int(s.notna().sum()))
        return best
    except Exception:
        return 0


def _safe_ready_count(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "technical_ready" not in df.columns:
            return 0
        s = pd.Series(df["technical_ready"]).fillna(False).astype(bool)
        return int(s.sum())
    except Exception:
        return 0


def _safe_ready_symbol_count(df: pd.DataFrame) -> int:
    try:
        if (
            not isinstance(df, pd.DataFrame)
            or df.empty
            or "symbol" not in df.columns
            or "technical_ready" not in df.columns
        ):
            return 0
        ready = pd.Series(df["technical_ready"]).fillna(False).astype(bool)
        return int(df.loc[ready, "symbol"].astype(str).nunique())
    except Exception:
        return 0


def _safe_display_ready_count(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return 0
        if "display_ready" in df.columns:
            s = pd.Series(df["display_ready"]).fillna(False).astype(bool)
            return int(s.sum())
        return int(_build_display_ready_mask(df).sum())
    except Exception:
        return 0


def _safe_display_ready_symbol_count(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return 0
        if "display_ready" in df.columns:
            ready = pd.Series(df["display_ready"]).fillna(False).astype(bool)
        else:
            ready = _build_display_ready_mask(df)
        return int(df.loc[ready, "symbol"].astype(str).nunique())
    except Exception:
        return 0


def _profile_numeric_series(df: pd.DataFrame, col: str) -> str:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or col not in df.columns:
            return f"{col}=MISSING"

        if col == "source":
            vc = df[col].fillna("NULL").astype(str).value_counts(dropna=False).to_dict()
            return f"{col}={vc}"

        if col in ("technical_ready", "display_ready"):
            s = pd.Series(df[col]).fillna(False).astype(bool)
            return (
                f"{col}: non_null={int(s.notna().sum())} "
                f"nonzero={int(s.sum())} "
                f"nunique={int(pd.Series(s).nunique(dropna=True))} "
                f"min={s.min()} max={s.max()}"
            )

        s = pd.to_numeric(df[col], errors="coerce")
        return (
            f"{col}: non_null={int(s.notna().sum())} "
            f"nonzero={int((s.fillna(0) != 0).sum())} "
            f"eq_2000={int((s.fillna(0) == 2000).sum())} "
            f"eq_-2000={int((s.fillna(0) == -2000).sum())} "
            f"nunique={int(s.nunique(dropna=True))} "
            f"min={s.min()} max={s.max()}"
        )
    except Exception:
        return f"{col}=PROFILE_FAILED"


def _normalized_symbol_datetime_df(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if out.empty:
        return out

    try:
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip()

        if "datetime" in out.columns:
            out["datetime"] = _safe_to_datetime(out["datetime"])

        if "symbol" in out.columns and "datetime" in out.columns:
            out = out.dropna(subset=["symbol", "datetime"]).copy()
            out = out[out["symbol"] != ""].copy()

        return out
    except Exception:
        logger.exception("[summary_controller] normalize symbol/datetime failed")
        return out


# ==========================================================
# display readiness
# ==========================================================

def _build_display_ready_mask(df: pd.DataFrame) -> pd.Series:
    """
    TOP10表示用 readiness。

    technical_ready ほど厳しくしない。
    short-history でも score と価格があれば表示対象にする。
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.Series(False, index=getattr(df, "index", None))

    idx = df.index

    try:
        if "symbol" in df.columns:
            symbol_ok = df["symbol"].fillna("").astype(str).str.strip().ne("")
        else:
            symbol_ok = pd.Series(False, index=idx)

        close_ok = pd.Series(False, index=idx)
        for c in ("close", "close_price", "price", "current_price", "last_price"):
            if c in df.columns:
                s = pd.to_numeric(df[c], errors="coerce")
                close_ok = close_ok | (s.notna() & s.fillna(0).ne(0))

        score_nonzero = pd.Series(False, index=idx)
        score_nonnull = pd.Series(False, index=idx)
        for c in (
            "score",
            "score_total",
            "final_score",
            "display_score",
            "score_buy",
            "score_sell",
            "buy_score",
            "sell_score",
            "combined_score",
            "absolute_score",
        ):
            if c in df.columns:
                s = pd.to_numeric(df[c], errors="coerce")
                score_nonzero = score_nonzero | s.fillna(0).ne(0)
                score_nonnull = score_nonnull | s.notna()

        return (symbol_ok & close_ok & (score_nonzero | score_nonnull)).fillna(False)

    except Exception:
        logger.debug("[summary_controller] build display_ready mask failed", exc_info=True)
        return pd.Series(False, index=idx)


def rebuild_display_ready(df: pd.DataFrame) -> pd.DataFrame:
    """
    display_ready を再構築する。

    technical_ready=True の行は必ず display_ready=True。
    technical_ready=False でも score/close があれば display_ready=True。
    """
    out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if out.empty:
        return out

    try:
        display_ready = _build_display_ready_mask(out)

        if "technical_ready" in out.columns:
            try:
                tech_ready = pd.Series(out["technical_ready"]).fillna(False).astype(bool)
                display_ready = display_ready | tech_ready
            except Exception:
                pass

        out["display_ready"] = display_ready.fillna(False).astype(bool)

        logger.info(
            "[summary_controller] rebuild_display_ready rows=%s display_rows=%s display_symbols=%s score_nonzero=%s close_nonnull=%s",
            len(out),
            _safe_display_ready_count(out),
            _safe_display_ready_symbol_count(out),
            _safe_numeric_nonzero(out, ("score", "score_buy", "score_sell", "final_score", "display_score")),
            _safe_numeric_nonnull(out, ("close", "close_price", "price", "current_price")),
        )

    except Exception:
        logger.exception("[summary_controller] rebuild_display_ready failed")
        try:
            out["display_ready"] = False
        except Exception:
            pass

    return out


# ==========================================================
# diagnostics
# ==========================================================

def log_df_state(
    label: str,
    interval: int,
    df: pd.DataFrame,
) -> None:
    try:
        rows = _safe_len(df)
        cols = 0 if not isinstance(df, pd.DataFrame) else len(df.columns)
        symbols = _safe_symbol_count(df)
        latest_dt = _safe_latest_dt(df)

        logger.info(
            "[summary_controller] %s interval=%s rows=%s cols=%s symbols=%s latest_dt=%s",
            label,
            interval,
            rows,
            cols,
            symbols,
            latest_dt,
        )

        if isinstance(df, pd.DataFrame) and not df.empty:
            for c in (
                "score",
                "score_buy",
                "score_sell",
                "buy_score",
                "sell_score",
                "slope_atr_scaled",
                "slope",
                "mtf",
                "score_mtf",
                "rsi",
                "macd",
                "signal",
                "close",
                "close_price",
                "price",
                "volume",
                "technical_ready",
                "display_ready",
                "source",
                "symbol_hist_len",
            ):
                if c in df.columns:
                    logger.info(
                        "[summary_controller] %s interval=%s %s",
                        label,
                        interval,
                        _profile_numeric_series(df, c),
                    )

            logger.info(
                "[summary_controller] %s interval=%s ready_rows=%s ready_symbols=%s display_rows=%s display_symbols=%s",
                label,
                interval,
                _safe_ready_count(df),
                _safe_ready_symbol_count(df),
                _safe_display_ready_count(df),
                _safe_display_ready_symbol_count(df),
            )

    except Exception:
        logger.exception("[summary_controller] df state log failed label=%s interval=%s", label, interval)


def log_scoring_probe(
    label: str,
    interval: int,
    df: pd.DataFrame,
) -> None:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            logger.info("[summary_controller] %s interval=%s empty", label, interval)
            return

        logger.info(
            "[summary_controller] %s interval=%s rows=%s cols=%s symbols=%s latest_dt=%s",
            label,
            interval,
            len(df),
            len(df.columns),
            _safe_symbol_count(df),
            _safe_latest_dt(df),
        )

        probe_cols = [
            "score", "score_buy", "score_sell", "buy_score", "sell_score",
            "ranking_score", "slope", "slope_atr_scaled", "mtf", "score_mtf",
            "mtf_score", "mtf_alignment", "rsi", "macd", "signal",
            "close", "close_price", "price", "volume",
            "technical_ready", "display_ready", "symbol_hist_len",
        ]

        for c in probe_cols:
            logger.info(
                "[summary_controller] %s interval=%s %s",
                label,
                interval,
                _profile_numeric_series(df, c),
            )

        logger.info(
            "[summary_controller] %s interval=%s ready_rows=%s ready_symbols=%s display_rows=%s display_symbols=%s",
            label,
            interval,
            _safe_ready_count(df),
            _safe_ready_symbol_count(df),
            _safe_display_ready_count(df),
            _safe_display_ready_symbol_count(df),
        )

    except Exception:
        logger.exception("[summary_controller] scoring probe failed label=%s interval=%s", label, interval)


def log_history_density(
    label: str,
    interval: int,
    df: pd.DataFrame,
) -> None:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns or "datetime" not in df.columns:
            logger.info("[summary_controller] %s interval=%s empty_or_missing_keys", label, interval)
            return

        out = _normalized_symbol_datetime_df(df)
        vc = out.groupby("symbol")["datetime"].nunique().sort_values(ascending=False)
        if vc.empty:
            logger.info("[summary_controller] %s interval=%s no_history_counts", label, interval)
            return

        logger.info(
            "[summary_controller] %s interval=%s symbols=%s rows=%s hist_len[min=%s p25=%.2f med=%.2f p75=%.2f max=%s mean=%.2f] display_rows=%s display_symbols=%s",
            label,
            interval,
            int(vc.shape[0]),
            int(len(out)),
            int(vc.min()),
            float(vc.quantile(0.25)),
            float(vc.quantile(0.50)),
            float(vc.quantile(0.75)),
            int(vc.max()),
            float(vc.mean()),
            _safe_display_ready_count(out),
            _safe_display_ready_symbol_count(out),
        )
    except Exception:
        logger.exception("[summary_controller] history density log failed label=%s interval=%s", label, interval)


# ==========================================================
# history len
# ==========================================================

def history_len_per_symbol(df: pd.DataFrame) -> pd.Series:
    try:
        out = _normalized_symbol_datetime_df(df)
        if out.empty or "symbol" not in out.columns:
            return pd.Series(dtype="int64")
        if "datetime" not in out.columns:
            return out.groupby("symbol")["symbol"].count().astype("int64")
        return out.groupby("symbol")["datetime"].nunique().astype("int64")
    except Exception:
        return pd.Series(dtype="int64")


def attach_history_len(
    latest_df: pd.DataFrame,
    hist_df: pd.DataFrame,
    normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
) -> pd.DataFrame:
    out = normalize_fn(latest_df)
    if out.empty or "symbol" not in out.columns:
        return out

    try:
        out = out.copy()
        out["symbol"] = out["symbol"].astype(str).str.strip()
        vc = history_len_per_symbol(hist_df)
        out["symbol_hist_len"] = out["symbol"].map(vc).fillna(0).astype(int)
        out = rebuild_display_ready(out)
        return out
    except Exception:
        logger.exception("[summary_controller] attach history len failed")
        out["symbol_hist_len"] = 0
        out = rebuild_display_ready(out)
        return out


# ==========================================================
# technical ready rebuild
# ==========================================================

def rebuild_technical_ready(df: pd.DataFrame) -> pd.DataFrame:
    """
    technical_ready は厳格に判定する。

    注意:
      short-history 表示許可は display_ready で行う。
      technical_ready を無理に True にしない。
    """
    out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if out.empty:
        return out

    if "technical_ready" not in out.columns:
        out["technical_ready"] = False

    try:
        hist_len = (
            pd.to_numeric(out["symbol_hist_len"], errors="coerce").fillna(0)
            if "symbol_hist_len" in out.columns
            else pd.Series(0, index=out.index)
        )

        close_ok = (
            pd.to_numeric(out["close"], errors="coerce").notna()
            if "close" in out.columns
            else pd.Series(False, index=out.index)
        )
        volume_ok = (
            pd.to_numeric(out["volume"], errors="coerce").fillna(0).gt(0)
            if "volume" in out.columns
            else pd.Series(False, index=out.index)
        )

        rsi_s = pd.to_numeric(out["rsi"], errors="coerce") if "rsi" in out.columns else pd.Series(index=out.index, dtype="float64")
        macd_s = pd.to_numeric(out["macd"], errors="coerce") if "macd" in out.columns else pd.Series(index=out.index, dtype="float64")
        signal_s = pd.to_numeric(out["signal"], errors="coerce") if "signal" in out.columns else pd.Series(index=out.index, dtype="float64")
        slope_s = pd.to_numeric(out["slope"], errors="coerce") if "slope" in out.columns else pd.Series(index=out.index, dtype="float64")
        slope_atr_s = pd.to_numeric(out["slope_atr_scaled"], errors="coerce") if "slope_atr_scaled" in out.columns else pd.Series(index=out.index, dtype="float64")
        mtf_s = pd.to_numeric(out["mtf"], errors="coerce") if "mtf" in out.columns else pd.Series(index=out.index, dtype="float64")
        score_mtf_s = pd.to_numeric(out["score_mtf"], errors="coerce") if "score_mtf" in out.columns else pd.Series(index=out.index, dtype="float64")
        mtf_score_s = pd.to_numeric(out["mtf_score"], errors="coerce") if "mtf_score" in out.columns else pd.Series(index=out.index, dtype="float64")
        ma75_s = pd.to_numeric(out["ma75"], errors="coerce") if "ma75" in out.columns else pd.Series(index=out.index, dtype="float64")

        rsi_ok = rsi_s.notna()

        # MACD は notna だけだと 0 埋め誤判定しやすいので signal と合わせて厳格化
        macd_ok = (
            macd_s.notna()
            & (
                macd_s.fillna(0).ne(0)
                | signal_s.fillna(0).ne(0)
            )
        )

        slope_ok = slope_s.fillna(0).ne(0) | slope_atr_s.fillna(0).ne(0)
        mtf_ok = mtf_s.fillna(0).ne(0) | score_mtf_s.fillna(0).ne(0) | mtf_score_s.fillna(0).ne(0)
        ma75_ok = ma75_s.notna()

        ready_strong = (
            (hist_len >= MIN_HISTORY_ROWS_MACD)
            & close_ok
            & (macd_ok | rsi_ok | slope_ok | mtf_ok | ma75_ok)
        )

        ready_soft = (
            (hist_len >= MIN_HISTORY_ROWS_RSI)
            & close_ok
            & (rsi_ok | slope_ok | ma75_ok | volume_ok)
        )

        ready = ready_strong | ready_soft
        out["technical_ready"] = ready.fillna(False).astype(bool)

        logger.info(
            "[summary_controller] rebuild_technical_ready rows=%s ready_rows=%s ready_symbols=%s rsi_nonnull=%s macd_nonzero=%s slope_nonzero=%s mtf_nonzero=%s ma75_nonnull=%s hist_ge14=%s hist_ge26=%s",
            len(out),
            _safe_ready_count(out),
            _safe_ready_symbol_count(out),
            int(rsi_ok.sum()),
            int(macd_ok.sum()),
            int(slope_ok.sum()),
            int(mtf_ok.sum()),
            int(ma75_ok.sum()),
            int((hist_len >= MIN_HISTORY_ROWS_RSI).sum()),
            int((hist_len >= MIN_HISTORY_ROWS_MACD).sum()),
        )

    except Exception:
        logger.exception("[summary_controller] rebuild technical_ready failed")

    out = rebuild_display_ready(out)
    return out


# ==========================================================
# source priority
# ==========================================================

def source_priority_series(df: pd.DataFrame) -> pd.Series:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.Series(dtype="int64")

    if "source" not in df.columns:
        return pd.Series(0, index=df.index, dtype="int64")

    try:
        src = df["source"].fillna("").astype(str)
        out = pd.Series(0, index=df.index, dtype="int64")

        out.loc[src.str.startswith("summary_recovery_push_1m", na=False)] = 500
        out.loc[src.str.startswith("summary_recovery_resample_1m", na=False)] = 450
        out.loc[src.str.startswith("summary_recovery_", na=False)] = 400
        out.loc[src.eq("SUMMARY")] = 350
        out.loc[src.str.startswith("ranking_history_", na=False)] = 120
        out.loc[src.str.startswith("RANKING_", na=False)] = 100
        return out
    except Exception:
        logger.exception("[summary_controller] source priority build failed")
        return pd.Series(0, index=df.index, dtype="int64")


# ==========================================================
# latest projection
# ==========================================================

def latest_row_per_symbol(
    df: pd.DataFrame,
    normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
) -> pd.DataFrame:
    out = normalize_fn(df)
    if out.empty or "symbol" not in out.columns or "datetime" not in out.columns:
        return rebuild_display_ready(out)

    try:
        out = _normalized_symbol_datetime_df(out)
        out = (
            out.sort_values(["symbol", "datetime"], kind="mergesort")
               .drop_duplicates(["symbol"], keep="last")
               .reset_index(drop=True)
        )
        out = rebuild_display_ready(out)
        return out
    except Exception:
        logger.exception("[summary_controller] latest row per symbol failed")
        return rebuild_display_ready(out)


def latest_row_per_symbol_mature_first(
    df: pd.DataFrame,
    normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
) -> pd.DataFrame:
    out = normalize_fn(df)
    if out.empty or "symbol" not in out.columns or "datetime" not in out.columns:
        return rebuild_display_ready(out)

    try:
        out = _normalized_symbol_datetime_df(out)

        # 念のため readiness を最新化
        if "technical_ready" not in out.columns:
            out["technical_ready"] = False
        out = rebuild_display_ready(out)

        out["_ready_rank"] = (
            pd.Series(out["technical_ready"]).fillna(False).astype(bool).astype(int)
            if "technical_ready" in out.columns else 0
        )
        out["_display_rank"] = (
            pd.Series(out["display_ready"]).fillna(False).astype(bool).astype(int)
            if "display_ready" in out.columns else 0
        )
        out["_hist_rank"] = (
            pd.to_numeric(out["symbol_hist_len"], errors="coerce").fillna(0)
            if "symbol_hist_len" in out.columns else 0
        )
        out["_source_rank"] = source_priority_series(out)
        out["_score_rank"] = (
            pd.to_numeric(out["score_buy"], errors="coerce").fillna(0)
            if "score_buy" in out.columns
            else (
                pd.to_numeric(out["score"], errors="coerce").fillna(0)
                if "score" in out.columns
                else 0
            )
        )
        out["_rsi_rank"] = (
            pd.to_numeric(out["rsi"], errors="coerce").notna().astype(int)
            if "rsi" in out.columns else 0
        )
        out["_macd_rank"] = (
            (
                pd.to_numeric(out["macd"], errors="coerce").fillna(0).ne(0)
                | pd.to_numeric(out["signal"], errors="coerce").fillna(0).ne(0)
            ).astype(int)
            if "macd" in out.columns or "signal" in out.columns else 0
        )
        out["_slope_rank"] = (
            (
                pd.to_numeric(out["slope"], errors="coerce").fillna(0).ne(0)
                | pd.to_numeric(out["slope_atr_scaled"], errors="coerce").fillna(0).ne(0)
            ).astype(int)
            if "slope" in out.columns or "slope_atr_scaled" in out.columns else 0
        )
        out["_mtf_rank"] = (
            (
                pd.to_numeric(out["mtf"], errors="coerce").fillna(0).ne(0)
                | pd.to_numeric(out["score_mtf"], errors="coerce").fillna(0).ne(0)
                | pd.to_numeric(out["mtf_score"], errors="coerce").fillna(0).ne(0)
            ).astype(int)
            if "mtf" in out.columns or "score_mtf" in out.columns or "mtf_score" in out.columns else 0
        )

        out = (
            out.sort_values(
                [
                    "symbol",
                    "_ready_rank",
                    "_display_rank",
                    "_hist_rank",
                    "_source_rank",
                    "_rsi_rank",
                    "_macd_rank",
                    "_slope_rank",
                    "_mtf_rank",
                    "_score_rank",
                    "datetime",
                ],
                ascending=[True, False, False, False, False, False, False, False, False, False, False],
                kind="mergesort",
            )
            .drop_duplicates(["symbol"], keep="first")
            .reset_index(drop=True)
        )

        out = rebuild_display_ready(out)

        logger.info(
            "[summary_controller] mature-first latest projection rows=%s symbols=%s ready_rows=%s ready_symbols=%s display_rows=%s display_symbols=%s latest_dt=%s",
            len(out),
            _safe_symbol_count(out),
            _safe_ready_count(out),
            _safe_ready_symbol_count(out),
            _safe_display_ready_count(out),
            _safe_display_ready_symbol_count(out),
            _safe_latest_dt(out),
        )

        return out.drop(
            columns=[
                "_ready_rank",
                "_display_rank",
                "_hist_rank",
                "_source_rank",
                "_score_rank",
                "_rsi_rank",
                "_macd_rank",
                "_slope_rank",
                "_mtf_rank",
            ],
            errors="ignore",
        )

    except Exception:
        logger.exception("[summary_controller] mature-first latest projection failed")
        return latest_row_per_symbol(df, normalize_fn)


__all__ = [
    "MIN_HISTORY_ROWS_RSI",
    "MIN_HISTORY_ROWS_MACD",
    "MIN_HISTORY_ROWS_MA75",
    "MIN_HISTORY_ROWS_STRONG",
    "log_df_state",
    "log_scoring_probe",
    "log_history_density",
    "history_len_per_symbol",
    "attach_history_len",
    "rebuild_technical_ready",
    "rebuild_display_ready",
    "source_priority_series",
    "latest_row_per_symbol",
    "latest_row_per_symbol_mature_first",
]