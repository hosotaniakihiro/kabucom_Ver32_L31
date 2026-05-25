# ============================================================
# File   : trading/summary/pipeline/indicator_short_history_patch.py
# Version: PRODUCTION-STABLE-INDICATOR-SHORT-HISTORY-PATCH-V2-READY-FILL
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
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import numpy as np
import pandas as pd

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


def _profile(df: pd.DataFrame) -> dict[str, int]:
    return {
        "rows": int(len(df)) if isinstance(df, pd.DataFrame) else 0,
        "symbols": int(df["symbol"].astype(str).nunique()) if isinstance(df, pd.DataFrame) and not df.empty and "symbol" in df.columns else 0,
        "hist_max": int(pd.to_numeric(df.get("symbol_hist_len", pd.Series(dtype=float)), errors="coerce").fillna(0).max()) if isinstance(df, pd.DataFrame) and not df.empty else 0,
        "technical_ready": int(pd.Series(df.get("technical_ready", False)).fillna(False).astype(bool).sum()) if isinstance(df, pd.DataFrame) and not df.empty else 0,
        "usable_ready": int(pd.Series(df.get("usable_technical_ready", False)).fillna(False).astype(bool).sum()) if isinstance(df, pd.DataFrame) and not df.empty else 0,
        "slope_nonnull": _nonnull(df, "slope"),
        "slope_nonzero": _nonzero(df, "slope"),
        "score_slope_nonzero": _nonzero(df, "score_slope"),
        "atr_nonnull": _nonnull(df, "atr"),
        "rsi_nonnull": _nonnull(df, "rsi"),
        "macd_nonnull": _nonnull(df, "macd"),
        "macd_nonzero": _nonzero(df, "macd"),
        "signal_nonnull": _nonnull(df, "signal"),
    }


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
                return s.reindex(index).fillna(False).astype(bool)
            txt = s.reindex(index).astype(str).str.lower().str.strip()
            return txt.isin(["1", "true", "yes", "on"])
    except Exception:
        pass
    return pd.Series(False, index=index, dtype="bool")


def add_short_history_indicators(df: pd.DataFrame, *, interval: Any = None) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return df

    out = _ensure_sorted(df)
    before = _profile(out)

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

    # score_slope が先にある場合、slopeの元として使う。
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
        # high/lowが同値でもATR列自体は残す。0でも「計算済み」として扱う。
        atr_light = atr_light.where(close.notna(), np.nan).fillna(0.0)
    except Exception:
        atr_light = pd.Series(0.0, index=out.index, dtype="float64")

    out, n_atr = _fill_missing_numeric(out, "atr", atr_light, zero_is_missing=False)
    out, n_atr1 = _fill_missing_numeric(out, "atr_1m", atr_light, zero_is_missing=False)
    try:
        out, _ = _fill_missing_numeric(out, f"atr_{int(interval)}m", atr_light, zero_is_missing=False)
    except Exception:
        pass

    # hist_len=1 でも rsi=50 を入れる。
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

    # hist_len=1 でも macd/signal/hist は0として補完する。
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

    # MTF列がNaNの場合は0を入れて欠損表示を防ぐ。
    zero_mtf = pd.Series(0.0, index=out.index, dtype="float64")
    out, n_mtf = _fill_missing_numeric(out, "mtf", zero_mtf, zero_is_missing=False)
    out, n_score_mtf = _fill_missing_numeric(out, "score_mtf", zero_mtf, zero_is_missing=False)
    out, n_mtf_score = _fill_missing_numeric(out, "mtf_score", zero_mtf, zero_is_missing=False)

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
        new_ready = old_ready | usable.fillna(False)
        out["usable_technical_ready"] = usable.fillna(False).astype(bool)
        out["technical_ready"] = new_ready.fillna(False).astype(bool)
        out["display_ready"] = pd.Series(out.get("display_ready", True), index=out.index).fillna(True).astype(bool)
    except Exception:
        out["usable_technical_ready"] = False

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
            "usable_ready": int(pd.Series(out.get("usable_technical_ready", False)).fillna(False).astype(bool).sum()),
            "technical_ready": int(pd.Series(out.get("technical_ready", False)).fillna(False).astype(bool).sum()),
        },
        before,
        after,
    )
    return out


def install_indicator_short_history_patch() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    try:
        from trading.summary.pipeline import indicator_pipeline as target
    except Exception:
        logger.exception("[IND SHORT PATCH] import target failed")
        return False

    orig_run = getattr(target, "run_indicator_pipeline", None)
    if not callable(orig_run):
        logger.warning("[IND SHORT PATCH] run_indicator_pipeline not callable")
        return False

    # 既存patchがある場合は元関数まで戻して二重patchを避ける。
    try:
        seen = set()
        while callable(getattr(orig_run, "_original", None)) and id(orig_run) not in seen:
            seen.add(id(orig_run))
            orig_run = getattr(orig_run, "_original")
    except Exception:
        pass

    def run_indicator_pipeline_patched(df: pd.DataFrame, interval=None, run_downstream_scoring: bool = True) -> pd.DataFrame:
        out = orig_run(df=df, interval=interval, run_downstream_scoring=run_downstream_scoring)
        try:
            return add_short_history_indicators(out, interval=interval)
        except Exception:
            logger.exception("[IND SHORT PATCH] fallback failed interval=%s", interval)
            return out

    run_indicator_pipeline_patched._original = orig_run  # type: ignore[attr-defined]

    def indicator_pipeline_patched(df: pd.DataFrame, interval=None, run_downstream_scoring: bool = True) -> pd.DataFrame:
        return run_indicator_pipeline_patched(df=df, interval=interval, run_downstream_scoring=run_downstream_scoring)

    def apply_indicator_pipeline_patched(df: pd.DataFrame, interval=None, run_downstream_scoring: bool = True) -> pd.DataFrame:
        return run_indicator_pipeline_patched(df=df, interval=interval, run_downstream_scoring=run_downstream_scoring)

    target.run_indicator_pipeline = run_indicator_pipeline_patched
    target.indicator_pipeline = indicator_pipeline_patched
    target.apply_indicator_pipeline = apply_indicator_pipeline_patched
    target._short_history_patch_v2_installed = True
    target._short_history_patch_v1_installed = True
    _PATCHED = True
    logger.warning("[IND SHORT PATCH] installed V2 ready_fill")
    return True


try:
    install_indicator_short_history_patch()
except Exception:
    logger.exception("[IND SHORT PATCH] auto install failed")


__all__ = ["install_indicator_short_history_patch", "add_short_history_indicators"]
