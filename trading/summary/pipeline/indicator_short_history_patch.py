# ============================================================
# File   : trading/summary/pipeline/indicator_short_history_patch.py
# Version: PRODUCTION-STABLE-INDICATOR-SHORT-HISTORY-PATCH-V1
# ------------------------------------------------------------
# Purpose:
#   indicator_pipeline の短履歴問題を補正する。
#
# Why:
#   indicator_calculator.py は本番品質のため、1分足では
#     RSI   : 14本
#     ATR   : 14本
#     MACD  : 26〜34本
#     ma75  : 75本
#   を要求する。
#
#   しかし PUSH登録ローテーション直後や起動直後は symbol_hist_len が
#   1〜3本しかなく、slope/rsi/macd/signal/atr がすべて NaN になり、
#   SUMMARY_AI / ENTRY側の判定材料が消える。
#
# Fix:
#   - run_indicator_pipeline() の戻りDFに短履歴フォールバックを適用
#   - 厳密指標が計算済みなら上書きしない
#   - 未計算 NaN の場合だけ lightweight 値で補完
#   - technical_ready は厳密指標の意味を残すため基本的に変えない
#   - display/entry用に usable_technical_ready を追加する
#
# Columns filled when missing:
#   - slope
#   - slope_atr_scaled
#   - atr / atr_1m
#   - rsi
#   - macd
#   - signal
#   - hist
#   - score_slope
#   - usable_technical_ready
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
        "slope_nonnull": _nonnull(df, "slope"),
        "slope_nonzero": _nonzero(df, "slope"),
        "atr_nonnull": _nonnull(df, "atr"),
        "rsi_nonnull": _nonnull(df, "rsi"),
        "macd_nonzero": _nonzero(df, "macd"),
        "signal_nonzero": _nonzero(df, "signal"),
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
            try:
                base = base.combine_first(s)
            except Exception:
                base = base.where(base.notna(), s)
    return base


def _fill_missing_numeric(out: pd.DataFrame, col: str, values: pd.Series, *, zero_is_missing: bool = False) -> tuple[pd.DataFrame, int]:
    if col not in out.columns:
        out[col] = np.nan
    cur = pd.to_numeric(out[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
    val = pd.to_numeric(values, errors="coerce").replace([np.inf, -np.inf], np.nan).reindex(out.index)
    mask = cur.isna() & val.notna()
    if zero_is_missing:
        mask = (cur.isna() | cur.fillna(0).eq(0)) & val.notna() & val.fillna(0).ne(0)
    n = int(mask.sum())
    if n > 0:
        out.loc[mask, col] = val.loc[mask]
    return out, n


def add_short_history_indicators(df: pd.DataFrame, *, interval: Any = None) -> pd.DataFrame:
    """
    短履歴でもENTRY/表示に使える最低限の指標を補完する。

    注意:
      厳密なテクニカル値が既にある場合は上書きしない。
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    if "symbol" not in df.columns:
        return df

    out = _ensure_sorted(df)
    before = _profile(out)

    close = _first_price(out, ("close", "close_price", "price", "current_price", "last_price"))
    high = _first_price(out, ("high", "high_price"))
    low = _first_price(out, ("low", "low_price"))
    open_ = _first_price(out, ("open", "open_price"))

    high = high.combine_first(close)
    low = low.combine_first(close)
    open_ = open_.combine_first(close)

    try:
        grouped_close = close.groupby(out["symbol"], sort=False)
        diff = grouped_close.diff()
        prev_close = grouped_close.shift(1)
        pct = diff / prev_close.replace(0, np.nan)
    except Exception:
        diff = pd.Series(np.nan, index=out.index, dtype="float64")
        pct = pd.Series(np.nan, index=out.index, dtype="float64")
        prev_close = pd.Series(np.nan, index=out.index, dtype="float64")

    # --------------------------------------------------------
    # slope fallback
    # --------------------------------------------------------
    try:
        slope_raw = pct.groupby(out["symbol"], sort=False).transform(lambda x: x.rolling(3, min_periods=1).mean())
    except Exception:
        slope_raw = pct
    slope_raw = slope_raw.replace([np.inf, -np.inf], np.nan).clip(-0.2, 0.2)

    # score_slope が存在する場合、価格差由来の方向性として補助利用
    if "score_slope" in out.columns:
        score_slope = pd.to_numeric(out["score_slope"], errors="coerce").replace([np.inf, -np.inf], np.nan)
        score_slope_scaled = (score_slope / 100.0).clip(-0.2, 0.2)
        slope_raw = slope_raw.combine_first(score_slope_scaled)

    out, n_slope = _fill_missing_numeric(out, "slope", slope_raw, zero_is_missing=True)
    out, n_slope_atr = _fill_missing_numeric(out, "slope_atr_scaled", slope_raw, zero_is_missing=True)

    # --------------------------------------------------------
    # ATR fallback: high-low と前回close差から簡易TRを作る
    # --------------------------------------------------------
    try:
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        atr_light = tr.groupby(out["symbol"], sort=False).transform(lambda x: x.rolling(3, min_periods=1).mean())
        atr_light = atr_light.replace([np.inf, -np.inf], np.nan).mask(atr_light <= 0, np.nan)
    except Exception:
        atr_light = pd.Series(np.nan, index=out.index, dtype="float64")

    out, n_atr = _fill_missing_numeric(out, "atr", atr_light)
    out, n_atr1 = _fill_missing_numeric(out, "atr_1m", atr_light)

    # slope_atr_scaled がNaNで、slopeとatrがある場合は slope を優先保持
    # ここでは価格比率の slope_raw を使うので atr割りしない。

    # --------------------------------------------------------
    # RSI fallback: 直近増減から50中心の軽量RSI
    # --------------------------------------------------------
    try:
        gain = diff.clip(lower=0)
        loss = (-diff.clip(upper=0))
        avg_gain = gain.groupby(out["symbol"], sort=False).transform(lambda x: x.rolling(3, min_periods=1).mean())
        avg_loss = loss.groupby(out["symbol"], sort=False).transform(lambda x: x.rolling(3, min_periods=1).mean())
        rs = avg_gain / avg_loss.replace(0, np.nan)
        rsi_light = 100.0 - (100.0 / (1.0 + rs))
        rsi_light = rsi_light.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
        rsi_light = rsi_light.where(~((avg_loss == 0) & (avg_gain == 0)), 50.0)
        rsi_light = rsi_light.where(close.notna(), np.nan).clip(0, 100)
    except Exception:
        rsi_light = pd.Series(np.nan, index=out.index, dtype="float64")

    out, n_rsi = _fill_missing_numeric(out, "rsi", rsi_light)

    # --------------------------------------------------------
    # MACD fallback: 短EMA差分。短履歴専用なので厳密MACDではない。
    # --------------------------------------------------------
    try:
        ema_fast = close.groupby(out["symbol"], sort=False).transform(lambda x: x.ewm(span=3, adjust=False, min_periods=1).mean())
        ema_slow = close.groupby(out["symbol"], sort=False).transform(lambda x: x.ewm(span=6, adjust=False, min_periods=1).mean())
        macd_light = (ema_fast - ema_slow).replace([np.inf, -np.inf], np.nan)
        signal_light = macd_light.groupby(out["symbol"], sort=False).transform(lambda x: x.ewm(span=3, adjust=False, min_periods=1).mean())
        hist_light = macd_light - signal_light
    except Exception:
        macd_light = pd.Series(np.nan, index=out.index, dtype="float64")
        signal_light = pd.Series(np.nan, index=out.index, dtype="float64")
        hist_light = pd.Series(np.nan, index=out.index, dtype="float64")

    out, n_macd = _fill_missing_numeric(out, "macd", macd_light, zero_is_missing=True)
    out, n_signal = _fill_missing_numeric(out, "signal", signal_light, zero_is_missing=True)
    out, n_hist = _fill_missing_numeric(out, "hist", hist_light, zero_is_missing=True)

    # --------------------------------------------------------
    # score_slope fallback
    # --------------------------------------------------------
    slope_for_score = pd.to_numeric(out.get("slope", pd.Series(np.nan, index=out.index)), errors="coerce")
    score_slope_light = (slope_for_score * 100.0).clip(-20, 20)
    out, n_score_slope = _fill_missing_numeric(out, "score_slope", score_slope_light, zero_is_missing=True)

    # --------------------------------------------------------
    # usable readiness: 厳密technical_readyは維持し、補助列を追加
    # --------------------------------------------------------
    try:
        usable = (
            close.notna()
            & (
                pd.to_numeric(out.get("slope", pd.Series(np.nan, index=out.index)), errors="coerce").notna()
                | pd.to_numeric(out.get("rsi", pd.Series(np.nan, index=out.index)), errors="coerce").notna()
                | pd.to_numeric(out.get("macd", pd.Series(np.nan, index=out.index)), errors="coerce").notna()
            )
        )
        out["usable_technical_ready"] = usable.fillna(False).astype(bool)
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
            "usable_ready": int(pd.Series(out.get("usable_technical_ready", False)).fillna(False).astype(bool).sum()),
        },
        before,
        after,
    )

    # 元の並びに近づけるため datetime/symbol順のまま返す。
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

    if getattr(target, "_short_history_patch_v1_installed", False):
        _PATCHED = True
        return True

    orig_run = getattr(target, "run_indicator_pipeline", None)
    if not callable(orig_run):
        logger.warning("[IND SHORT PATCH] run_indicator_pipeline not callable")
        return False

    def run_indicator_pipeline_patched(df: pd.DataFrame, interval=None, run_downstream_scoring: bool = True) -> pd.DataFrame:
        out = orig_run(df=df, interval=interval, run_downstream_scoring=run_downstream_scoring)
        try:
            return add_short_history_indicators(out, interval=interval)
        except Exception:
            logger.exception("[IND SHORT PATCH] fallback failed interval=%s", interval)
            return out

    def indicator_pipeline_patched(df: pd.DataFrame, interval=None, run_downstream_scoring: bool = True) -> pd.DataFrame:
        return run_indicator_pipeline_patched(df=df, interval=interval, run_downstream_scoring=run_downstream_scoring)

    def apply_indicator_pipeline_patched(df: pd.DataFrame, interval=None, run_downstream_scoring: bool = True) -> pd.DataFrame:
        return run_indicator_pipeline_patched(df=df, interval=interval, run_downstream_scoring=run_downstream_scoring)

    target.run_indicator_pipeline = run_indicator_pipeline_patched
    target.indicator_pipeline = indicator_pipeline_patched
    target.apply_indicator_pipeline = apply_indicator_pipeline_patched
    target._short_history_patch_v1_installed = True
    _PATCHED = True

    logger.warning("[IND SHORT PATCH] installed V1")
    return True


try:
    install_indicator_short_history_patch()
except Exception:
    logger.exception("[IND SHORT PATCH] auto install failed")


__all__ = ["install_indicator_short_history_patch", "add_short_history_indicators"]
