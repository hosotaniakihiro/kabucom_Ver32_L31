# ============================================================
# File   : trading/summary/pipeline/indicator_short_history.py
# Version: PRODUCTION-STABLE-INDICATOR-SHORT-HISTORY-PATCH-V4-NAN-HIST-GUARD
# ------------------------------------------------------------
# Purpose:
#   indicator_pipeline の短履歴問題を補正する。
#
# Why:
#   PUSH登録ローテーション直後や起動直後は symbol_hist_len が 1〜3本しかなく、
#   RSI / MACD / signal / slope / ATR が NaN のまま残りやすい。
#   その結果、表示では score が出ていても technical_ready=False になり、
#   SUMMARY AI 側で technical_not_ready / MTF fail-open が多発する。
#
# V2:
#   - hist_len=1 でも rsi=50 / macd=0 / signal=0 / hist=0 を明示補完する
#   - score_slope がある場合は slope / slope_atr_scaled へ逆補完する
#   - close + score + slope系がある行は technical_ready=True に補正する
#   - usable_technical_ready も維持する
#
# V3:
#   - hist_len < 3 かつ rsi=50/macd=0/signal=0/slope=0 の中立行で、
#     score=-1 / score_sell=1 のようなデフォルト売りスコアを0に抑制する。
#   - 本当に下向きの行、MACD/RSI/傾きが出ている行は抑制しない。
#
# V4:
#   - symbol_hist_len 列が無い/全NaNの短履歴DFで int(NaN) にならないよう
#     _profile() を防御化する。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

import numpy as np
import pandas as pd
from trading.summary.bool_series import na_false_bool, na_false_bool_count

logger = logging.getLogger(__name__)
_PATCHED = False


def _num(s: Any, index=None, default=np.nan) -> pd.Series:
    try:
        if isinstance(s, pd.Series):
            return pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan)
        return pd.Series(default, index=index, dtype="float64")
    except Exception:
        return pd.Series(default, index=index, dtype="float64")


def _nonnull(df: pd.DataFrame, col: str) -> int:
    try:
        if col not in df.columns:
            return 0
        return int(pd.to_numeric(df[col], errors="coerce").notna().sum())
    except Exception:
        return 0


def _nonzero(df: pd.DataFrame, col: str) -> int:
    try:
        if col not in df.columns:
            return 0
        s = pd.to_numeric(df[col], errors="coerce").fillna(0)
        return int((s != 0).sum())
    except Exception:
        return 0


def _hist_max(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return 0
        if "symbol_hist_len" not in df.columns:
            return 0
        s = pd.to_numeric(df["symbol_hist_len"], errors="coerce").replace([np.inf, -np.inf], np.nan)
        if s.dropna().empty:
            return 0
        v = s.fillna(0).max()
        if pd.isna(v) or not np.isfinite(float(v)):
            return 0
        return int(float(v))
    except Exception:
        return 0


def _bool_sum(value: Any, index: Any) -> int:
    try:
        if isinstance(value, pd.Series):
            return na_false_bool_count(value, index=index)
        return na_false_bool_count(value, index=index)
    except Exception:
        return 0


def _profile(df: pd.DataFrame) -> dict[str, int]:
    """
    symbol_hist_len が全NaNの短履歴DF (起動直後のPUSH raw DB fallback等) でも
    int(NaN) で落ちないように防御的に集計する。
    """
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return {
                "rows": 0,
                "symbols": 0,
                "hist_max": 0,
                "technical_ready": 0,
                "usable_ready": 0,
                "slope_nonnull": 0,
                "slope_nonzero": 0,
                "score_slope_nonzero": 0,
                "atr_nonnull": 0,
                "rsi_nonnull": 0,
                "macd_nonnull": 0,
                "macd_nonzero": 0,
                "signal_nonnull": 0,
            }
        idx = df.index
        return {
            "rows": int(len(df)),
            "symbols": int(df["symbol"].astype(str).nunique()) if "symbol" in df.columns else 0,
            "hist_max": _hist_max(df),
            "technical_ready": _bool_sum(df.get("technical_ready", False), idx),
            "usable_ready": _bool_sum(df.get("usable_technical_ready", False), idx),
            "slope_nonnull": _nonnull(df, "slope"),
            "slope_nonzero": _nonzero(df, "slope"),
            "score_slope_nonzero": _nonzero(df, "score_slope"),
            "atr_nonnull": _nonnull(df, "atr"),
            "rsi_nonnull": _nonnull(df, "rsi"),
            "macd_nonnull": _nonnull(df, "macd"),
            "macd_nonzero": _nonzero(df, "macd"),
            "signal_nonnull": _nonnull(df, "signal"),
        }
    except Exception:
        logger.debug("[indicator_short_history_patch] profile fallback failed", exc_info=True)
        return {"rows": int(len(df)) if isinstance(df, pd.DataFrame) else 0, "symbols": 0, "hist_max": 0}


def _ensure_sorted(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    try:
        out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    except Exception:
        pass
    try:
        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            out = out.sort_values(["symbol", "datetime"], kind="mergesort").reset_index(drop=True)
        else:
            out = out.sort_values(["symbol"], kind="mergesort").reset_index(drop=True)
    except Exception:
        pass
    return out


def _first_price(df: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    base = pd.Series(np.nan, index=df.index, dtype="float64")
    for c in names:
        if c in df.columns:
            s = pd.to_numeric(df[c], errors="coerce").replace([np.inf, -np.inf], np.nan)
            s = s.mask(s <= 0, np.nan)
            base = base.combine_first(s)
    return base


def _fill_missing_numeric(
    out: pd.DataFrame,
    col: str,
    values: pd.Series,
    *,
    zero_is_missing: bool = False,
    allow_zero_value: bool = True,
) -> tuple[pd.DataFrame, int]:
    if col not in out.columns:
        out[col] = np.nan
    cur = pd.to_numeric(out[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
    val = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan).reindex(out.index)
    mask = cur.isna() & val.notna()
    if zero_is_missing:
        mask = (cur.isna() | cur.fillna(0).eq(0)) & val.notna()
        if not allow_zero_value:
            mask = mask & val.fillna(0).ne(0)
    n = int(mask.sum())
    if n > 0:
        out.loc[mask, col] = val.loc[mask]
    return out, n


def _safe_bool_series(s: Any, index) -> pd.Series:
    try:
        if isinstance(s, pd.Series):
            if s.dtype == bool:
                return na_false_bool(s, index=index)
            txt = s.reindex(index).astype(str).str.lower().str.strip()
            return txt.isin(["1", "true", "yes", "on"])
    except Exception:
        pass
    return pd.Series(False, index=index, dtype="bool")


def _series_from_col(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(default)


def _suppress_neutral_default_scores(out: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """短履歴で全指標が中立なのに付いたデフォルト売りスコアを消す。"""
    try:
        if out.empty:
            return out, 0
        hist_len = _series_from_col(out, "symbol_hist_len", 0.0)
        score = _series_from_col(out, "score", 0.0)
        score_sell = _series_from_col(out, "score_sell", 0.0)
        score_buy = _series_from_col(out, "score_buy", 0.0)
        slope = _series_from_col(out, "slope", 0.0).abs()
        slope_atr = _series_from_col(out, "slope_atr_scaled", 0.0).abs()
        score_slope = _series_from_col(out, "score_slope", 0.0).abs()
        rsi = _series_from_col(out, "rsi", 50.0)
        macd = _series_from_col(out, "macd", 0.0).abs()
        signal = _series_from_col(out, "signal", 0.0).abs()

        short_hist = hist_len < 3
        neutral = (
            (slope <= 1e-12)
            & (slope_atr <= 1e-12)
            & (score_slope <= 1e-12)
            & ((rsi - 50.0).abs() <= 1e-9)
            & (macd <= 1e-12)
            & (signal <= 1e-12)
        )
        default_sell = (score < 0) & (score_sell > 0) & (score_buy <= 0)
        mask = short_hist & neutral & default_sell
        n = int(mask.sum())
        if n:
            for col in ("score", "score_total", "final_score", "display_score", "score_sell"):
                if col in out.columns:
                    out.loc[mask, col] = 0.0
            if "score_buy" in out.columns:
                out.loc[mask, "score_buy"] = 0.0
            # 既存列が float64 のことがあり、True を入れると将来の pandas で
            # エラーになる。今の暗黙変換 (1.0) と同じ値を明示して入れる。
            if "neutral_default_score_suppressed" not in out.columns:
                out["neutral_default_score_suppressed"] = False
            _ns = out["neutral_default_score_suppressed"]
            if pd.api.types.is_bool_dtype(_ns):
                _sup_val = True
            elif pd.api.types.is_integer_dtype(_ns):
                _sup_val = 1
            elif pd.api.types.is_float_dtype(_ns):
                _sup_val = 1.0
            else:
                _sup_val = True
            out.loc[mask, "neutral_default_score_suppressed"] = _sup_val
        return out, n
    except Exception:
        logger.exception("[IND SHORT PATCH] neutral score suppress failed")
        return out, 0


def _is_market_hours_now() -> bool:
    """東証の通常立会時間帯か (SHORT_PATCH 依存の WARNING 判定用)。"""
    try:
        import datetime as _dt

        now = _dt.datetime.now()
        if now.weekday() >= 5:
            return False
        hhmm = now.hour * 100 + now.minute
        return (900 <= hhmm <= 1130) or (1230 <= hhmm <= 1530)
    except Exception:
        return False


def add_short_history_indicators(df: pd.DataFrame, *, interval: Any = None) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return df

    out = _ensure_sorted(df)
    before = _profile(out)
    _ready_before_patch = _safe_bool_series(
        out.get("technical_ready", pd.Series(False, index=out.index)), out.index
    )

    close = _first_price(out, ("close", "close_price", "price", "current_price", "last_price"))
    high = _first_price(out, ("high", "high_price")).combine_first(close)
    low = _first_price(out, ("low", "low_price")).combine_first(close)

    try:
        grouped_close = close.groupby(out["symbol"], sort=False)
        diff = grouped_close.diff().fillna(0.0)
        prev_close = grouped_close.shift(1)
        pct = (diff / prev_close.replace(0, np.nan)).replace([np.inf, -np.inf], np.nan)
    except Exception:
        diff = pd.Series(0.0, index=out.index, dtype="float64")
        pct = pd.Series(np.nan, index=out.index, dtype="float64")
        prev_close = pd.Series(np.nan, index=out.index, dtype="float64")

    score_slope = pd.to_numeric(out.get("score_slope", pd.Series(np.nan, index=out.index)), errors="coerce").replace([np.inf, -np.inf], np.nan)
    score_slope_scaled = (score_slope / 100.0).clip(-0.2, 0.2)

    try:
        slope_raw = pct.groupby(out["symbol"], sort=False).transform(lambda x: x.rolling(3, min_periods=1).mean())
    except Exception:
        slope_raw = pct
    slope_raw = slope_raw.replace([np.inf, -np.inf], np.nan).clip(-0.2, 0.2).combine_first(score_slope_scaled)

    out, n_slope = _fill_missing_numeric(out, "slope", slope_raw, zero_is_missing=True, allow_zero_value=False)
    out, n_slope_atr = _fill_missing_numeric(out, "slope_atr_scaled", slope_raw, zero_is_missing=True, allow_zero_value=False)

    try:
        tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        atr_light = tr.groupby(out["symbol"], sort=False).transform(lambda x: x.rolling(3, min_periods=1).mean())
        atr_light = atr_light.replace([np.inf, -np.inf], np.nan)
        atr_light = atr_light.where(close.notna(), np.nan).fillna(0.0)
    except Exception:
        atr_light = pd.Series(0.0, index=out.index, dtype="float64")

    out, n_atr = _fill_missing_numeric(out, "atr", atr_light, zero_is_missing=False)
    out, n_atr1 = _fill_missing_numeric(out, "atr_1m", atr_light, zero_is_missing=False)
    try:
        out, _ = _fill_missing_numeric(out, f"atr_{int(interval)}m", atr_light, zero_is_missing=False)
    except Exception:
        pass

    try:
        gain = diff.clip(lower=0)
        loss = (-diff.clip(upper=0))
        avg_gain = gain.groupby(out["symbol"], sort=False).transform(lambda x: x.rolling(3, min_periods=1).mean())
        avg_loss = loss.groupby(out["symbol"], sort=False).transform(lambda x: x.rolling(3, min_periods=1).mean())
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi_light = 100.0 - (100.0 / (1.0 + rs))
        rsi_light = rsi_light.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
        rsi_light = rsi_light.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
        rsi_light = rsi_light.where(close.notna(), np.nan).fillna(50.0).clip(0, 100)
    except Exception:
        rsi_light = pd.Series(50.0, index=out.index, dtype="float64")

    out, n_rsi = _fill_missing_numeric(out, "rsi", rsi_light, zero_is_missing=False)

    try:
        ema_fast = close.groupby(out["symbol"], sort=False).transform(lambda x: x.ewm(span=3, adjust=False, min_periods=1).mean())
        ema_slow = close.groupby(out["symbol"], sort=False).transform(lambda x: x.ewm(span=6, adjust=False, min_periods=1).mean())
        macd_light = (ema_fast - ema_slow).replace([np.inf, -np.inf], np.nan).where(close.notna(), np.nan).fillna(0.0)
        signal_light = macd_light.groupby(out["symbol"], sort=False).transform(lambda x: x.ewm(span=3, adjust=False, min_periods=1).mean()).fillna(0.0)
        hist_light = (macd_light - signal_light).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    except Exception:
        macd_light = pd.Series(0.0, index=out.index, dtype="float64")
        signal_light = pd.Series(0.0, index=out.index, dtype="float64")
        hist_light = pd.Series(0.0, index=out.index, dtype="float64")

    out, n_macd = _fill_missing_numeric(out, "macd", macd_light, zero_is_missing=True, allow_zero_value=True)
    out, n_signal = _fill_missing_numeric(out, "signal", signal_light, zero_is_missing=True, allow_zero_value=True)
    out, n_hist = _fill_missing_numeric(out, "hist", hist_light, zero_is_missing=True, allow_zero_value=True)

    slope_for_score = pd.to_numeric(out.get("slope", pd.Series(np.nan, index=out.index)), errors="coerce")
    score_slope_light = (slope_for_score * 100.0).clip(-20, 20)
    out, n_score_slope = _fill_missing_numeric(out, "score_slope", score_slope_light, zero_is_missing=True, allow_zero_value=False)

    zero_mtf = pd.Series(0.0, index=out.index, dtype="float64")
    out, n_mtf = _fill_missing_numeric(out, "mtf", zero_mtf, zero_is_missing=False)
    out, n_score_mtf = _fill_missing_numeric(out, "score_mtf", zero_mtf, zero_is_missing=False)
    out, n_mtf_score = _fill_missing_numeric(out, "mtf_score", zero_mtf, zero_is_missing=False)

    suppressed_neutral_scores = 0
    try:
        out, suppressed_neutral_scores = _suppress_neutral_default_scores(out)
    except Exception:
        logger.exception("[IND SHORT PATCH] neutral default score suppress wrapper failed")

    try:
        score_any = pd.to_numeric(out.get("score", out.get("final_score", pd.Series(np.nan, index=out.index))), errors="coerce").notna()
        slope_any = (
            pd.to_numeric(out.get("slope", pd.Series(np.nan, index=out.index)), errors="coerce").notna()
            | pd.to_numeric(out.get("score_slope", pd.Series(np.nan, index=out.index)), errors="coerce").notna()
            | pd.to_numeric(out.get("slope_atr_scaled", pd.Series(np.nan, index=out.index)), errors="coerce").notna()
        )
        indicator_any = (
            pd.to_numeric(out.get("rsi", pd.Series(np.nan, index=out.index)), errors="coerce").notna()
            & pd.to_numeric(out.get("macd", pd.Series(np.nan, index=out.index)), errors="coerce").notna()
        )
        usable = close.notna() & score_any & (slope_any | indicator_any)
        old_ready = _safe_bool_series(out.get("technical_ready", pd.Series(False, index=out.index)), out.index)
        # SHORT PATCH は補完値なので technical_ready(strict) を True にしない。
        # ENTRY 可否は実履歴から計算できた行だけで判断する (2026-09-02)。
        out["usable_technical_ready"] = na_false_bool(usable)
        out["soft_technical_ready"] = na_false_bool(usable)
        out["technical_ready"] = na_false_bool(old_ready)
        out["display_ready"] = pd.Series(out.get("display_ready", True), index=out.index).fillna(True).astype(bool)
    except Exception:
        out["usable_technical_ready"] = False

    # technical_source: native で ready だった行はそのまま、
    # このパッチで初めて ready になった行だけ SHORT_PATCH にする。
    try:
        ready_after = _safe_bool_series(out.get("technical_ready", pd.Series(False, index=out.index)), out.index)
        source = pd.Series(out.get("technical_source", "NONE"), index=out.index).astype(str)
        source = source.where(source.isin(["NATIVE_HISTORY", "SHORT_PATCH"]), "NONE")
        patched = ready_after & (~_ready_before_patch)
        source = source.mask(patched, "SHORT_PATCH")
        out["technical_source"] = source

        native_n = int((source == "NATIVE_HISTORY").sum())
        patch_n = int((source == "SHORT_PATCH").sum())
        total_ready = native_n + patch_n
        ratio = (patch_n / total_ready) if total_ready else 0.0
        msg = (
            "[IND SHORT PATCH][TECHNICAL SOURCE] interval=%s rows=%s "
            "NATIVE_HISTORY=%s SHORT_PATCH=%s short_patch_ratio=%.2f"
        )
        warn_ratio = float(os.getenv("IND_SHORT_PATCH_WARN_RATIO", "0.30") or 0.30)
        if total_ready and ratio >= warn_ratio and _is_market_hours_now():
            logger.warning(
                msg + " (通常市場時間に SHORT_PATCH 依存が高い。warmup history の供給を確認する)",
                interval, len(out), native_n, patch_n, ratio,
            )
        else:
            logger.info(msg, interval, len(out), native_n, patch_n, ratio)
    except Exception:
        logger.debug("[IND SHORT PATCH] technical source tagging failed", exc_info=True)

    after = _profile(out)
    logger.warning(
        "[IND SHORT PATCH] applied interval=%s filled=%s before=%s after=%s",
        interval,
        {
            "slope": n_slope,
            "slope_atr_scaled": n_slope_atr,
            "atr": n_atr,
            "atr_1m": n_atr1,
            "rsi": n_rsi,
            "macd": n_macd,
            "signal": n_signal,
            "hist": n_hist,
            "score_slope": n_score_slope,
            "mtf": n_mtf,
            "score_mtf": n_score_mtf,
            "mtf_score": n_mtf_score,
            "neutral_default_score_suppressed": suppressed_neutral_scores,
            "usable_ready": na_false_bool_count(out.get("usable_technical_ready", False)),
            "technical_ready": na_false_bool_count(out.get("technical_ready", False)),
        },
        before,
        after,
    )
    return out


__all__ = ["add_short_history_indicators"]
