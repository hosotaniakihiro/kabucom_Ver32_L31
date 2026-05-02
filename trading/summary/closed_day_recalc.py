# ============================================================
# File   : trading/summary/closed_day_recalc.py
# Version: Ver2.2-PRODUCTION-CLOSED-DAY-RECALC-ACTUALDATE-PRIORITY
# ------------------------------------------------------------
# ✔ Ver2.1 完全保持（削除ゼロ）
# ✔ closed-day 表示専用の再計算ヘルパ
# ✔ 1min / 3min / 5min 再構成
# ✔ OHLC alias 補正
# ✔ symbol / datetime / numeric hard guard
# ✔ slope_atr_scaled fallback 強化
# ✔ MTF再計算補助
# ✔ 既存 pipeline を壊さない非破壊設計
# ------------------------------------------------------------
# 🔥 NEW (Ver2.2):
# ✔ 休場日でも入力実データの actual date を優先
# ✔ startup 復元済み PUSH 1m を closed-day recalc で落とさない
# ✔ allowed date guard は fallback 時のみ previous business day を使用
# ✔ resample / indicator / finalize の全段で actual-date 優先を維持
# ✔ 機能削除ゼロ
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# 営業日 helper
# ============================================================

def _safe_previous_business_day(base_date: dt.date) -> dt.date:
    try:
        from utils.business_day_utils import get_previous_business_day
        return get_previous_business_day(base_date)
    except Exception:
        d = base_date - dt.timedelta(days=1)
        while d.weekday() >= 5:
            d -= dt.timedelta(days=1)
        return d


def _is_today_business_day() -> bool:
    try:
        from utils.business_day_utils import is_today_business_day
        return bool(is_today_business_day())
    except Exception:
        return dt.date.today().weekday() < 5


def _fallback_allowed_closed_day_dates() -> set[dt.date]:
    today = dt.date.today()
    prev_bd = _safe_previous_business_day(today)

    if _is_today_business_day():
        return {prev_bd, today}

    return {prev_bd}


def _extract_actual_dates_from_df(df: pd.DataFrame) -> set[dt.date]:
    try:
        if df is None or df.empty:
            return set()

        for col in ("datetime", "end_time", "start_time", "date", "dt", "timestamp"):
            if col not in df.columns:
                continue
            s = pd.to_datetime(df[col], errors="coerce")
            vals = {x.date() for x in s.dropna()}
            if vals:
                return vals
    except Exception:
        logger.exception("[CLOSED DAY RECALC] extract actual dates failed")

    return set()


def _allowed_closed_day_dates_for_df(df: Optional[pd.DataFrame] = None) -> set[dt.date]:
    actual_dates = _extract_actual_dates_from_df(df) if isinstance(df, pd.DataFrame) else set()
    if actual_dates:
        return actual_dates
    return _fallback_allowed_closed_day_dates()


def _drop_outside_allowed_dates(df: pd.DataFrame, label: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    out = df.copy()

    if "datetime" not in out.columns:
        logger.warning("[CLOSED DAY RECALC] %s date guard skipped: datetime missing", label)
        return out

    dt_s = pd.to_datetime(out["datetime"], errors="coerce")
    allowed = _allowed_closed_day_dates_for_df(out)
    actual_dates = _extract_actual_dates_from_df(out)

    keep = dt_s.dt.date.isin(allowed)
    before = len(out)
    removed = int((~keep.fillna(False)).sum())

    if removed > 0:
        sample_cols = [
            c for c in [
                "symbol", "symbolname", "datetime", "date", "time",
                "start_time", "end_time", "time_range",
                "open", "high", "low", "close", "volume",
            ]
            if c in out.columns
        ]
        try:
            sample_txt = out.loc[~keep.fillna(False), sample_cols].head(20).to_string(index=False) if sample_cols else "(no sample)"
        except Exception:
            sample_txt = "(sample render failed)"

        logger.warning(
            "[CLOSED DAY RECALC] %s date guard removed=%d before=%d allowed=%s actual_dates=%s sample=\n%s",
            label,
            removed,
            before,
            sorted(str(x) for x in allowed),
            sorted(str(x) for x in actual_dates),
            sample_txt,
        )

    out = out.loc[keep.fillna(False)].copy().reset_index(drop=True)

    logger.info(
        "[CLOSED DAY RECALC] %s date guard rows=%d -> %d allowed=%s actual_dates=%s",
        label,
        before,
        len(out),
        sorted(str(x) for x in allowed),
        sorted(str(x) for x in actual_dates),
    )
    return out


# ============================================================
# 基本ユーティリティ
# ============================================================

def _safe_copy_df(df: Any) -> pd.DataFrame:
    try:
        if isinstance(df, pd.DataFrame):
            return df.copy()
    except Exception:
        pass
    return pd.DataFrame()


def _safe_str(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.lower() in {"nan", "none", "nat"}:
            return ""
        return s
    except Exception:
        return ""


def _normalize_symbol(v: Any) -> str:
    s = _safe_str(v)
    if not s:
        return ""
    if "." in s:
        s = s.split(".", 1)[0].strip()
    return s


def _drop_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    out = df.copy()

    try:
        if isinstance(out.columns, pd.MultiIndex):
            out.columns = [
                "_".join([str(x) for x in tup if str(x) != ""]).strip("_")
                for tup in out.columns
            ]
    except Exception:
        logger.exception("[CLOSED DAY RECALC] multiindex flatten failed")

    try:
        out.columns = [str(c).strip() for c in out.columns]
        if out.columns.duplicated().any():
            dup = out.columns[out.columns.duplicated()].tolist()
            logger.warning("[CLOSED DAY RECALC] duplicate columns removed: %s", dup)
            out = out.loc[:, ~out.columns.duplicated()]
    except Exception:
        logger.exception("[CLOSED DAY RECALC] duplicate column guard failed")

    return out


def _coalesce_col(df: pd.DataFrame, target: str, candidates: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    out = df.copy()
    if target in out.columns:
        return out

    for c in candidates:
        if c in out.columns:
            out[target] = out[c]
            logger.warning("[CLOSED DAY RECALC] alias used: %s -> %s", c, target)
            return out

    return out


def _ensure_columns(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    out = _safe_copy_df(df)
    for c in cols:
        if c not in out.columns:
            out[c] = pd.NA
    return out


def _sort_df(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    cols = [c for c in by if c in df.columns]
    if not cols:
        return df.copy()

    try:
        return df.sort_values(cols, kind="stable").reset_index(drop=True)
    except Exception:
        logger.exception("[CLOSED DAY RECALC] sort failed by=%s", cols)
        return df.copy()


# ============================================================
# 正規化
# ============================================================

def normalize_closed_day_input(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    x = _drop_duplicate_columns(df)

    x = _coalesce_col(x, "symbol", ["code", "ticker"])
    x = _coalesce_col(x, "symbolname", ["name", "issue_name", "stock_name"])
    x = _coalesce_col(x, "datetime", ["end_time", "snapshot_time", "timestamp", "date_time", "dt"])

    x = _coalesce_col(x, "open", ["open_price", "o"])
    x = _coalesce_col(x, "high", ["high_price", "h"])
    x = _coalesce_col(x, "low", ["low_price", "l"])
    x = _coalesce_col(x, "close", ["close_price", "price", "last_price", "current_price", "c"])
    x = _coalesce_col(x, "volume", ["volume_total", "current_volume", "cum_volume", "trading_volume", "v"])

    x = _ensure_columns(x, ["symbol", "datetime", "open", "high", "low", "close", "volume"])

    x["symbol"] = x["symbol"].map(_normalize_symbol)
    x = x[x["symbol"] != ""].copy()

    x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
    x = x.dropna(subset=["datetime"]).copy()

    x = _drop_outside_allowed_dates(x, "normalize_input")
    if x.empty:
        return pd.DataFrame()

    for c in ["open", "high", "low", "close", "volume"]:
        x[c] = pd.to_numeric(x[c], errors="coerce")

    x["close"] = x["close"].fillna(x["open"])
    x["open"] = x["open"].fillna(x["close"])
    x["high"] = x["high"].fillna(x["close"])
    x["low"] = x["low"].fillna(x["close"])
    x["volume"] = x["volume"].fillna(0.0)

    x["high"] = pd.concat([x["high"], x["open"], x["close"]], axis=1).max(axis=1)
    x["low"] = pd.concat([x["low"], x["open"], x["close"]], axis=1).min(axis=1)

    if "symbolname" not in x.columns:
        x["symbolname"] = x["symbol"]
    else:
        x["symbolname"] = x["symbolname"].fillna(x["symbol"]).astype(str)

    x["start_time"] = x["datetime"]
    x["end_time"] = x["datetime"]
    x["date"] = pd.to_datetime(x["datetime"], errors="coerce").dt.date
    x["time"] = pd.to_datetime(x["datetime"], errors="coerce").dt.time
    x["time_range"] = pd.to_datetime(x["datetime"], errors="coerce").dt.strftime("%H:%M")

    x = _sort_df(x, ["symbol", "datetime"])
    x = x.drop_duplicates(subset=["symbol", "datetime"], keep="last").reset_index(drop=True)
    return x


# ============================================================
# 指標 fallback
# ============================================================

def _calc_atr_group(g: pd.DataFrame, period: int = 14) -> pd.Series:
    high = pd.to_numeric(g["high"], errors="coerce")
    low = pd.to_numeric(g["low"], errors="coerce")
    close = pd.to_numeric(g["close"], errors="coerce")

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            (high - low).abs(),
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    atr = tr.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
    return pd.to_numeric(atr, errors="coerce").fillna(0.0)


def _calc_rsi_group(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta.clip(upper=0.0))

    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()

    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.where(~avg_loss.eq(0), 100.0)
    rsi = rsi.where(~avg_gain.eq(0), 0.0)
    return pd.to_numeric(rsi, errors="coerce").fillna(50.0).clip(0, 100)


def _calc_macd_group(close: pd.Series):
    ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False, min_periods=1).mean()
    hist = macd - signal
    return macd, signal, hist


def apply_closed_day_indicators(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    x = normalize_closed_day_input(df)
    if x.empty:
        return pd.DataFrame()

    out = []

    for _, g in x.groupby("symbol", sort=False):
        gg = g.copy()
        gg = _sort_df(gg, ["symbol", "datetime"])

        gg["ma5"] = gg["close"].rolling(5, min_periods=1).mean()
        gg["ma25"] = gg["close"].rolling(25, min_periods=1).mean()
        gg["ma75"] = gg["close"].rolling(75, min_periods=1).mean()

        gg["rsi"] = _calc_rsi_group(gg["close"], period=14)
        macd, signal, hist = _calc_macd_group(gg["close"])
        gg["macd"] = macd
        gg["signal"] = signal
        gg["hist"] = hist

        gg["atr"] = _calc_atr_group(gg, period=14)

        diff = gg["close"].diff().fillna(0.0)
        atr_safe = gg["atr"].replace(0, pd.NA)
        slope_atr = (diff / atr_safe).replace([float("inf"), float("-inf")], pd.NA)
        fallback_slope = diff.clip(-20.0, 20.0)

        gg["slope_atr_scaled"] = slope_atr.fillna(fallback_slope).fillna(0.0)
        gg["ma75_slope"] = gg["ma75"].diff().fillna(0.0).clip(-10.0, 10.0)

        out.append(gg)

    if not out:
        return x

    y = pd.concat(out, ignore_index=True)
    y = _drop_outside_allowed_dates(y, "apply_indicators")
    y = _sort_df(y, ["symbol", "datetime"])
    return y


# ============================================================
# timeframe resample
# ============================================================

def resample_timeframe(df_1m: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df_1m is None or df_1m.empty:
        return pd.DataFrame()

    if interval not in (1, 3, 5):
        raise ValueError(f"unsupported interval={interval}")

    x = normalize_closed_day_input(df_1m)
    if x.empty:
        return pd.DataFrame()

    x = _drop_outside_allowed_dates(x, f"before_resample_{interval}m")
    if x.empty:
        return pd.DataFrame()

    if interval == 1:
        y = x.copy()
        y["start_time"] = y["datetime"]
        y["end_time"] = y["datetime"]
    else:
        x["bucket"] = x["datetime"].dt.floor(f"{interval}min")

        y = (
            x.groupby(["symbol", "bucket"], as_index=False)
            .agg(
                symbolname=("symbolname", "last"),
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
            )
            .rename(columns={"bucket": "start_time"})
        )
        y["end_time"] = y["start_time"] + pd.Timedelta(minutes=interval)

    y["datetime"] = y["end_time"]
    y["interval"] = interval
    y["interval_name"] = f"{interval}min"
    y["date"] = pd.to_datetime(y["datetime"], errors="coerce").dt.date
    y["time"] = pd.to_datetime(y["datetime"], errors="coerce").dt.time
    y["time_range"] = (
        pd.to_datetime(y["start_time"], errors="coerce").dt.strftime("%H:%M")
        + " - " +
        pd.to_datetime(y["end_time"], errors="coerce").dt.strftime("%H:%M")
    )

    y = _drop_outside_allowed_dates(y, f"after_resample_{interval}m")
    if y.empty:
        return pd.DataFrame()

    y = apply_closed_day_indicators(y)
    y = _drop_outside_allowed_dates(y, f"after_indicator_{interval}m")
    y = _sort_df(y, ["symbol", "datetime"])
    return y


# ============================================================
# MTF fallback
# ============================================================

def attach_closed_day_mtf(
    df_1m: pd.DataFrame,
    df_3m: Optional[pd.DataFrame] = None,
    df_5m: Optional[pd.DataFrame] = None,
) -> pd.DataFrame:
    if df_1m is None or df_1m.empty:
        return pd.DataFrame()

    x1 = apply_closed_day_indicators(df_1m)
    x3 = apply_closed_day_indicators(df_3m) if isinstance(df_3m, pd.DataFrame) and not df_3m.empty else pd.DataFrame()
    x5 = apply_closed_day_indicators(df_5m) if isinstance(df_5m, pd.DataFrame) and not df_5m.empty else pd.DataFrame()

    base = x1.copy()

    if "datetime" in base.columns:
        base["datetime"] = pd.to_datetime(base["datetime"], errors="coerce")

    if not x3.empty:
        x3 = x3[["symbol", "datetime", "slope_atr_scaled"]].copy()
        x3["datetime"] = pd.to_datetime(x3["datetime"], errors="coerce")
        x3 = x3.rename(columns={"slope_atr_scaled": "slope_atr_scaled_3m"})
        base = pd.merge(base, x3, on=["symbol", "datetime"], how="left")

    if not x5.empty:
        x5 = x5[["symbol", "datetime", "slope_atr_scaled"]].copy()
        x5["datetime"] = pd.to_datetime(x5["datetime"], errors="coerce")
        x5 = x5.rename(columns={"slope_atr_scaled": "slope_atr_scaled_5m"})
        base = pd.merge(base, x5, on=["symbol", "datetime"], how="left")

    for c in ["slope_atr_scaled_3m", "slope_atr_scaled_5m"]:
        if c not in base.columns:
            base[c] = 0.0
        base[c] = pd.to_numeric(base[c], errors="coerce").fillna(0.0)

    base["mtf_score"] = (
        pd.to_numeric(base.get("slope_atr_scaled", 0.0), errors="coerce").fillna(0.0) * 0.5
        + base["slope_atr_scaled_3m"] * 0.3
        + base["slope_atr_scaled_5m"] * 0.2
    )

    base["mtf"] = base["mtf_score"]
    base = _drop_outside_allowed_dates(base, "attach_mtf")
    return base


# ============================================================
# scoring pipeline wrapper
# ============================================================

def apply_scoring_pipeline_safe(df: pd.DataFrame, interval_label: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()

    try:
        from trading.scoring.core.scoring_pipeline import run_scoring_pipeline

        out = run_scoring_pipeline(x, interval=interval_label)
        if isinstance(out, pd.DataFrame) and not out.empty:
            out = _drop_outside_allowed_dates(out, f"after_scoring_{interval_label}")
            return out
    except Exception:
        logger.exception("[CLOSED DAY RECALC] run_scoring_pipeline failed interval=%s", interval_label)

    for col in ["score", "score_total", "score_buy", "score_sell", "score_slope", "score_mtf"]:
        if col not in x.columns:
            x[col] = 0.0

    slope = pd.to_numeric(x.get("slope_atr_scaled", 0.0), errors="coerce").fillna(0.0)
    mtf = pd.to_numeric(x.get("mtf_score", 0.0), errors="coerce").fillna(0.0)

    x["score_slope"] = slope.clip(-500, 500)
    x["score_mtf"] = (mtf * 100.0).clip(-500, 500)
    x["score_total"] = pd.to_numeric(x.get("score_total", 0.0), errors="coerce").fillna(0.0)
    x["score"] = (x["score_total"] + x["score_slope"] + x["score_mtf"]).clip(-500, 500)
    x["score_buy"] = x["score"]
    x["score_sell"] = (-x["score"].clip(upper=0)).fillna(0.0)

    x = _drop_outside_allowed_dates(x, f"fallback_scoring_{interval_label}")
    return x


# ============================================================
# post process fallback
# ============================================================

def finalize_closed_day_scores(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()

    if "combined_score" not in x.columns:
        x["combined_score"] = pd.to_numeric(x.get("score", 0.0), errors="coerce").fillna(0.0)

    if "final_score" not in x.columns or pd.to_numeric(x["final_score"], errors="coerce").fillna(0.0).eq(0).all():
        x["final_score"] = pd.to_numeric(
            x.get("combined_score", x.get("score", 0.0)),
            errors="coerce",
        ).fillna(0.0)

    if "display_score" not in x.columns or pd.to_numeric(x["display_score"], errors="coerce").fillna(0.0).eq(0).all():
        x["display_score"] = pd.to_numeric(
            x.get("final_score", x.get("score_buy", 0.0)),
            errors="coerce",
        ).fillna(0.0)

    if "slope" not in x.columns or pd.to_numeric(x["slope"], errors="coerce").fillna(0.0).eq(0).all():
        x["slope"] = pd.to_numeric(x.get("score_slope", x.get("slope_atr_scaled", 0.0)), errors="coerce").fillna(0.0)

    if "mtf" not in x.columns or pd.to_numeric(x["mtf"], errors="coerce").fillna(0.0).eq(0).all():
        x["mtf"] = pd.to_numeric(x.get("mtf_score", 0.0), errors="coerce").fillna(0.0)

    if "buy_score" not in x.columns:
        x["buy_score"] = pd.to_numeric(x.get("score_buy", x.get("display_score", 0.0)), errors="coerce").fillna(0.0)

    if "sell_score" not in x.columns:
        x["sell_score"] = pd.to_numeric(x.get("score_sell", 0.0), errors="coerce").fillna(0.0)

    x = _drop_outside_allowed_dates(x, "finalize_scores")
    return x


# ============================================================
# lightweight helper
# ============================================================

def _limit_symbols(df: pd.DataFrame, limit_symbols: int = 300) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    if "symbol" not in df.columns:
        return df.copy()

    x = df.copy()
    x["symbol"] = x["symbol"].map(_normalize_symbol)
    keep = x["symbol"].dropna().astype(str).drop_duplicates().head(limit_symbols).tolist()
    x = x[x["symbol"].astype(str).isin(set(keep))].copy()

    logger.info(
        "[CLOSED DAY RECALC] lightweight symbol limit applied symbols=%d rows=%d",
        len(keep),
        len(x),
    )
    return x


# ============================================================
# main orchestration
# ============================================================

def rebuild_closed_day_all(
    df_1m_raw: pd.DataFrame,
    df_3m_raw: Optional[pd.DataFrame] = None,
    df_5m_raw: Optional[pd.DataFrame] = None,
    lightweight: bool = True,
    limit_symbols: int = 300,
) -> dict[str, pd.DataFrame]:
    """
    closed-day 表示専用の再構成:
      1) 1m 正規化＋indicator
      2) 3m/5m は 1m から再集約優先
      3) MTF付与
      4) scoring
      5) finalize
    """
    if df_1m_raw is None or df_1m_raw.empty:
        return {
            "1m": pd.DataFrame(),
            "3m": pd.DataFrame(),
            "5m": pd.DataFrame(),
        }

    raw1 = _safe_copy_df(df_1m_raw)

    if lightweight:
        raw1 = _limit_symbols(raw1, limit_symbols=limit_symbols)

    raw1 = normalize_closed_day_input(raw1)
    raw1 = _drop_outside_allowed_dates(raw1, "main_raw1")
    if raw1.empty:
        logger.warning("[CLOSED DAY RECALC] raw1 empty after business-day guard")
        return {
            "1m": pd.DataFrame(),
            "3m": pd.DataFrame(),
            "5m": pd.DataFrame(),
        }

    one = apply_closed_day_indicators(raw1)
    one = _drop_outside_allowed_dates(one, "main_1m_after_indicator")

    three = resample_timeframe(one, 3)
    five = resample_timeframe(one, 5)

    one = attach_closed_day_mtf(one, three, five)
    three = attach_closed_day_mtf(three, three, five)
    five = attach_closed_day_mtf(five, three, five)

    one = apply_scoring_pipeline_safe(one, "1m")
    three = apply_scoring_pipeline_safe(three, "3m")
    five = apply_scoring_pipeline_safe(five, "5m")

    one = finalize_closed_day_scores(one)
    three = finalize_closed_day_scores(three)
    five = finalize_closed_day_scores(five)

    one = _drop_outside_allowed_dates(one, "main_final_1m")
    three = _drop_outside_allowed_dates(three, "main_final_3m")
    five = _drop_outside_allowed_dates(five, "main_final_5m")

    logger.info(
        "[CLOSED DAY RECALC] rebuilt done 1m=%d 3m=%d 5m=%d lightweight=%s allowed_dates=%s",
        len(one),
        len(three),
        len(five),
        lightweight,
        sorted(str(x) for x in _allowed_closed_day_dates_for_df(raw1)),
    )

    return {
        "1m": one,
        "3m": three,
        "5m": five,
    }