# ============================================================
# File   : trading/yahoo/pipeline/complement/compute.py
# Version: REV4.2-YAHOO-COMPLEMENT-FULL-DB-SCHEMA-COLUMNS
# ------------------------------------------------------------
# 【概要】
#   Yahoo補完サマリーの計算・整形
#
# 【主な機能】
#   - time_range / start_time / end_time 生成
#   - indicator計算
#   - scoring_pipeline実行
#   - score / final_score / display_score 保証
#   - Yahoo補完で計算可能な追加列を補完計算
#   - summary DB実カラムを読み取り、保存前DataFrameへ不足列を追加
#   - DB列/DF列/追加列/ゼロ補完列を診断ログに出力
#   - source付与
#
# REV4.2:
#   ✔ Yahoo補完保存前に summary DB 実カラムを読み取り、DF側の不足列を揃える
#   ✔ vwap / ma*_conf / ma75_slope / volume_slope / vwap_slope / rci を補完計算
#   ✔ atr_1m/3m/5m・slope_atr_scaled_1m/3m/5m を interval に応じて補完
#   ✔ symbol_hist_len / technical_ready を補完
#   ✔ DBカラムに存在するが計算不能な列は安全値で追加し、ログに zero_filled_cols として出す
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from trading.summary.indicators.indicator_calculator import add_all_indicators
from trading.scoring.core.scoring_pipeline import run_scoring_pipeline

from .constants import (
    PREFERRED_SUMMARY_COLUMNS,
    yahoo_source_for_interval,
    summary_table_for_interval,
)
from .db import get_table_columns
from .normalize import (
    safe_df,
    normalize_datetime_df,
    numeric_series,
    coalesce_series,
    backfill_symbolname,
)

logger = logging.getLogger(__name__)


# ============================================================
# time helpers
# ============================================================

def build_time_range_from_datetime(dt_series: pd.Series, interval: int) -> pd.Series:
    try:
        base = pd.to_datetime(dt_series, errors="coerce")
        end = base.dt.floor("min")
        start = end - pd.to_timedelta(max(int(interval) - 1, 0), unit="min")
        return start.dt.strftime("%H:%M") + "-" + end.dt.strftime("%H:%M")
    except Exception:
        logger.exception("[YAHOO COMPUTE] build time_range failed interval=%s", interval)
        return pd.Series(pd.NA, index=dt_series.index if hasattr(dt_series, "index") else None)


def build_start_time(dt_series: pd.Series, interval: int) -> pd.Series:
    try:
        base = pd.to_datetime(dt_series, errors="coerce")
        start = base.dt.floor("min") - pd.to_timedelta(max(int(interval) - 1, 0), unit="min")
        return start.dt.strftime("%H:%M:%S")
    except Exception:
        logger.exception("[YAHOO COMPUTE] build start_time failed interval=%s", interval)
        return pd.Series(pd.NA, index=dt_series.index if hasattr(dt_series, "index") else None)


def build_end_time(dt_series: pd.Series) -> pd.Series:
    try:
        base = pd.to_datetime(dt_series, errors="coerce")
        return base.dt.strftime("%H:%M:%S")
    except Exception:
        logger.exception("[YAHOO COMPUTE] build end_time failed")
        return pd.Series(pd.NA, index=dt_series.index if hasattr(dt_series, "index") else None)


# ============================================================
# generic helpers
# ============================================================

def _num(out: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col in out.columns:
        return pd.to_numeric(out[col], errors="coerce")
    return pd.Series(default, index=out.index, dtype="float64")


def _group_diff_ratio(out: pd.DataFrame, value_col: str, denom_col: str = "close") -> pd.Series:
    try:
        if value_col not in out.columns:
            return pd.Series(0.0, index=out.index, dtype="float64")
        val = _num(out, value_col)
        denom = _num(out, denom_col).replace(0, pd.NA)
        diff = val.groupby(out["symbol"].astype(str), sort=False).diff()
        return (diff / denom).replace([float("inf"), float("-inf")], pd.NA).fillna(0.0)
    except Exception:
        logger.debug("[YAHOO COMPUTE] group diff ratio failed col=%s", value_col, exc_info=True)
        return pd.Series(0.0, index=out.index, dtype="float64")


def _rolling_rci(close: pd.Series, window: int = 9) -> pd.Series:
    """
    RCIを簡易計算する。値は -100 ～ +100。
    window未満は0。
    """
    try:
        close = pd.to_numeric(close, errors="coerce")

        def calc(x: pd.Series) -> float:
            if len(x) < window or x.isna().any():
                return 0.0
            price_rank = x.rank(method="average")
            time_rank = pd.Series(range(1, len(x) + 1), index=x.index, dtype="float64")
            d = time_rank - price_rank
            n = float(len(x))
            return float((1.0 - (6.0 * (d * d).sum()) / (n * (n * n - 1.0))) * 100.0)

        return close.rolling(window=window, min_periods=window).apply(calc, raw=False).fillna(0.0)
    except Exception:
        logger.debug("[YAHOO COMPUTE] rolling rci failed", exc_info=True)
        return pd.Series(0.0, index=close.index, dtype="float64")


def _default_value_for_missing_db_col(col: str) -> Any:
    c = str(col).lower()
    if c in {"symbol", "symbolname", "date", "time", "time_range", "start_time", "end_time", "source", "signal"}:
        return ""
    if c in {"datetime", "last_update", "created_at", "updated_at"}:
        # pd.NA だと呼び出し側の `default_value in (0, 0.0)` 系の membership 判定で
        # TypeError になりうるため None にする (sqlite保存時は NULL になる)。
        return None
    if c in {"technical_ready", "display_ready", "is_ready", "ready"}:
        return 0
    return 0.0


def _is_zero_default_value(value: Any) -> bool:
    """pd.NA/NaNを真偽値評価せず、0系の安全値だけを判定する。"""
    try:
        if value is None or pd.isna(value):
            return False
    except Exception:
        if value is None:
            return False
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and float(value) == 0.0
    except Exception:
        return False


# ============================================================
# prepare
# ============================================================

def prepare_summary_frame(
    df: pd.DataFrame,
    interval: int,
    *,
    source: str | None = None,
) -> pd.DataFrame:
    work = safe_df(df)
    if work.empty:
        return work

    work = normalize_datetime_df(work)
    if work.empty:
        return work

    interval = int(interval)
    source = source or yahoo_source_for_interval(interval)

    for col in ["open", "high", "low", "close", "volume"]:
        if col in work.columns:
            work[col] = pd.to_numeric(work[col], errors="coerce")

    work["open_price"] = pd.to_numeric(work["open"], errors="coerce")
    work["high_price"] = pd.to_numeric(work["high"], errors="coerce")
    work["low_price"] = pd.to_numeric(work["low"], errors="coerce")
    work["close_price"] = pd.to_numeric(work["close"], errors="coerce")

    work["open"] = work["open_price"]
    work["high"] = work["high_price"]
    work["low"] = work["low_price"]
    work["close"] = work["close_price"]

    work["date"] = work["datetime"].dt.strftime("%Y-%m-%d")
    work["time_range"] = build_time_range_from_datetime(work["datetime"], interval)
    work["time"] = work["datetime"].dt.strftime("%H:%M:%S")
    work["start_time"] = build_start_time(work["datetime"], interval)
    work["end_time"] = build_end_time(work["datetime"])

    work["interval"] = interval
    work["source"] = source

    work["price"] = work["close"]
    work["current_price"] = work["close"]
    work["trading_volume"] = work["volume"]

    work["last_update"] = pd.Timestamp.now()

    if "signal" not in work.columns:
        work["signal"] = ""

    work = backfill_symbolname(work)

    return work


# ============================================================
# indicators / scoring
# ============================================================

def apply_indicators(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    try:
        try:
            ret = add_all_indicators(out, interval=int(interval))
        except TypeError:
            try:
                ret = add_all_indicators(out, interval=f"{int(interval)}min")
            except TypeError:
                ret = add_all_indicators(out)

        if isinstance(ret, pd.DataFrame):
            out = ret

        logger.info(
            "[YAHOO COMPUTE] indicators done interval=%s rows=%s",
            interval,
            len(out),
        )

    except Exception:
        logger.exception("[YAHOO COMPUTE] indicator calculation failed interval=%s", interval)

    return safe_df(out)


def apply_scoring(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    try:
        try:
            ret = run_scoring_pipeline(out, interval=f"{int(interval)}min")
        except TypeError:
            try:
                ret = run_scoring_pipeline(out, f"{int(interval)}min")
            except TypeError:
                ret = run_scoring_pipeline(out)

        if isinstance(ret, pd.DataFrame):
            out = ret

        logger.info(
            "[YAHOO COMPUTE] scoring done interval=%s rows=%s",
            interval,
            len(out),
        )

    except Exception:
        logger.exception("[YAHOO COMPUTE] scoring failed interval=%s", interval)

    return safe_df(out)


# ============================================================
# scores / schema
# ============================================================

def _yahoo_fallback_numeric(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    try:
        if col in df.columns:
            return pd.to_numeric(df[col], errors="coerce")
    except Exception:
        pass
    return pd.Series(default, index=df.index, dtype="float64")


def _yahoo_all_zero_or_na(s: pd.Series) -> bool:
    try:
        return bool((pd.to_numeric(s, errors="coerce").fillna(0.0) == 0.0).all())
    except Exception:
        return True


def _yahoo_fallback_direction_score(df: pd.DataFrame) -> pd.Series:
    """Yahoo補完用の最低限スコア (旧 yahoo_complement_safety_patch)。

    scoring_pipeline が列名不一致等で score/final_score/display_score を
    全行0のまま返した場合の最終救済。強すぎる点を付けず、AI/entry側で
    最終確認できるように 0～数点程度に抑える。
    """
    idx = df.index
    score = pd.Series(0.0, index=idx, dtype="float64")

    slope = _yahoo_fallback_numeric(df, "slope").fillna(_yahoo_fallback_numeric(df, "slope_atr_scaled")).fillna(0.0)
    hist = _yahoo_fallback_numeric(df, "hist").fillna(0.0)
    macd = _yahoo_fallback_numeric(df, "macd").fillna(0.0)
    signal = _yahoo_fallback_numeric(df, "signal").fillna(0.0)
    rsi = _yahoo_fallback_numeric(df, "rsi").fillna(50.0)

    macd_diff = hist.copy()
    try:
        if _yahoo_all_zero_or_na(macd_diff) and ("macd" in df.columns and "signal" in df.columns):
            macd_diff = (macd - signal).fillna(0.0)
    except Exception:
        macd_diff = hist.fillna(0.0)

    # slope: 方向性。小さな値でも少しだけ反映。
    score = score.where(~slope.gt(0.01), score + 2.0)
    score = score.where(~slope.lt(-0.01), score - 2.0)
    score = score.where(~slope.gt(0.03), score + 1.0)
    score = score.where(~slope.lt(-0.03), score - 1.0)

    # MACD差分: 方向補助。
    score = score.where(~macd_diff.gt(0), score + 1.0)
    score = score.where(~macd_diff.lt(0), score - 1.0)

    # RSI: 過熱/売られすぎを軽く加点。
    score = score.where(~rsi.gt(55), score + 0.5)
    score = score.where(~rsi.lt(45), score - 0.5)

    return score.fillna(0.0)


def _yahoo_apply_zero_score_fallback(out: pd.DataFrame) -> pd.DataFrame:
    try:
        score = _yahoo_fallback_numeric(out, "score").fillna(0.0)
        final_score = _yahoo_fallback_numeric(out, "final_score").fillna(0.0)
        display_score = _yahoo_fallback_numeric(out, "display_score").fillna(0.0)

        all_score_zero = bool((score == 0).all() and (final_score == 0).all() and (display_score == 0).all())
        has_signal = False
        for c in ("slope", "slope_atr_scaled", "hist", "macd", "rsi"):
            if c in out.columns and not _yahoo_all_zero_or_na(_yahoo_fallback_numeric(out, c)):
                has_signal = True
                break

        if all_score_zero and has_signal:
            fb = _yahoo_fallback_direction_score(out)
            buy = fb.clip(lower=0.0)
            sell = (-fb).clip(lower=0.0)

            out["score_buy"] = buy
            out["score_sell"] = sell
            out["score_total"] = fb
            out["score"] = fb
            out["final_score"] = fb
            out["display_score"] = fb

            if "buy_score" in out.columns:
                out["buy_score"] = buy
            if "sell_score" in out.columns:
                out["sell_score"] = sell

            logger.warning(
                "[YAHOO COMPUTE] zero-score fallback applied rows=%s nonzero=%s",
                len(out),
                int((fb.fillna(0.0) != 0.0).sum()),
            )
    except Exception:
        logger.exception("[YAHOO COMPUTE] zero-score fallback failed; keep original result")
    return out


def ensure_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    try:
        out = safe_df(df)
        if out.empty:
            return pd.DataFrame()

        score_buy = numeric_series(out, "score_buy").fillna(0.0)
        score_sell = numeric_series(out, "score_sell").fillna(0.0)
        score_total = numeric_series(out, "score_total").fillna(0.0)

        score = numeric_series(out, "score")
        final_score = numeric_series(out, "final_score")
        display_score = numeric_series(out, "display_score")

        slope = numeric_series(out, "slope")
        slope_alt = numeric_series(out, "slope_atr_scaled")
        mtf = numeric_series(out, "mtf")
        score_mtf = numeric_series(out, "score_mtf")

        if (slope.fillna(0) == 0).all() and not (slope_alt.fillna(0) == 0).all():
            slope = coalesce_series(slope.replace(0, pd.NA), slope_alt)

        if (mtf.fillna(0) == 0).all() and not (score_mtf.fillna(0) == 0).all():
            mtf = coalesce_series(mtf.replace(0, pd.NA), score_mtf)

        composed = score.copy()
        if (composed.fillna(0) == 0).all():
            if not (final_score.fillna(0) == 0).all():
                composed = final_score.copy()
            elif not (display_score.fillna(0) == 0).all():
                composed = display_score.copy()
            elif not (score_total.fillna(0) == 0).all():
                composed = score_total.copy()
            else:
                buy_abs = score_buy.abs()
                sell_abs = score_sell.abs()
                composed = score_buy.where(buy_abs >= sell_abs, score_sell)

        if (final_score.fillna(0) == 0).all():
            final_score = composed.copy()

        if (display_score.fillna(0) == 0).all():
            display_score = final_score.copy()

        out["score_buy"] = score_buy.fillna(0.0)
        out["score_sell"] = score_sell.fillna(0.0)
        out["score_total"] = score_total.fillna(0.0)
        out["score"] = composed.fillna(0.0)
        out["final_score"] = final_score.fillna(0.0)
        out["display_score"] = display_score.fillna(0.0)

        out["slope"] = slope.fillna(0.0)
        out["mtf"] = mtf.fillna(0.0)
        out["slope_atr_scaled"] = numeric_series(out, "slope_atr_scaled").fillna(out["slope"])
        out["score_mtf"] = numeric_series(out, "score_mtf").fillna(out["mtf"])

        if "buy_score" not in out.columns:
            out["buy_score"] = out["score_buy"]
        if "sell_score" not in out.columns:
            out["sell_score"] = out["score_sell"]

        out = _yahoo_apply_zero_score_fallback(out)

        return out

    except Exception:
        logger.exception("[YAHOO COMPUTE] ensure score columns failed")
        return pd.DataFrame()


def ensure_yahoo_extra_calculated_columns(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    Yahoo OHLCVから計算可能な列を追加する。
    DBに列があるのにYahoo補完では未作成、という状態を減らす。
    """
    try:
        out = safe_df(df)
        if out.empty:
            return pd.DataFrame()

        interval = int(interval)
        if "symbol" not in out.columns:
            return out

        out = out.sort_values(["symbol", "datetime"], kind="stable").copy()

        close = _num(out, "close")
        high = _num(out, "high")
        low = _num(out, "low")
        open_ = _num(out, "open")
        volume = _num(out, "volume")
        close_denom = close.replace(0, pd.NA)

        typical = (high + low + close) / 3.0
        pv = (typical.fillna(close) * volume.fillna(0.0)).fillna(0.0)
        vol = volume.fillna(0.0)
        grp = out["symbol"].astype(str)
        cum_pv = pv.groupby(grp, sort=False).cumsum()
        cum_vol = vol.groupby(grp, sort=False).cumsum().replace(0, pd.NA)
        out["vwap"] = coalesce_series(numeric_series(out, "vwap").replace(0, pd.NA), cum_pv / cum_vol).fillna(0.0)

        if "price_diff" not in out.columns:
            out["price_diff"] = (close - open_).fillna(0.0)

        for ma_col, conf_col in [("ma5", "ma5_conf"), ("ma25", "ma25_conf"), ("ma75", "ma75_conf")]:
            ma = numeric_series(out, ma_col)
            conf = ((close - ma) / close_denom).replace([float("inf"), float("-inf")], pd.NA).fillna(0.0)
            if conf_col not in out.columns or (numeric_series(out, conf_col).fillna(0) == 0).all():
                out[conf_col] = conf

        if "ma75_slope" not in out.columns or (numeric_series(out, "ma75_slope").fillna(0) == 0).all():
            out["ma75_slope"] = _group_diff_ratio(out, "ma75")

        if "volume_slope" not in out.columns or (numeric_series(out, "volume_slope").fillna(0) == 0).all():
            vol_prev = volume.groupby(grp, sort=False).shift(1).replace(0, pd.NA)
            out["volume_slope"] = ((volume - vol_prev) / vol_prev).replace([float("inf"), float("-inf")], pd.NA).fillna(0.0)

        if "vwap_slope" not in out.columns or (numeric_series(out, "vwap_slope").fillna(0) == 0).all():
            out["vwap_slope"] = _group_diff_ratio(out, "vwap")

        if "rci" not in out.columns or (numeric_series(out, "rci").fillna(0) == 0).all():
            out["rci"] = out.groupby(grp, group_keys=False, sort=False)["close"].apply(lambda s: _rolling_rci(s, window=9))

        atr = numeric_series(out, "atr").fillna(0.0)
        slope_atr = numeric_series(out, "slope_atr_scaled").fillna(numeric_series(out, "slope")).fillna(0.0)
        for iv in (1, 3, 5):
            atr_col = f"atr_{iv}m"
            slope_col = f"slope_atr_scaled_{iv}m"
            if atr_col not in out.columns:
                out[atr_col] = atr if interval == iv else 0.0
            elif interval == iv and (numeric_series(out, atr_col).fillna(0) == 0).all():
                out[atr_col] = atr
            if slope_col not in out.columns:
                out[slope_col] = slope_atr if interval == iv else 0.0
            elif interval == iv and (numeric_series(out, slope_col).fillna(0) == 0).all():
                out[slope_col] = slope_atr

        out["symbol_hist_len"] = out.groupby(grp, sort=False).cumcount() + 1

        score = numeric_series(out, "score").fillna(0.0)
        ma5 = numeric_series(out, "ma5")
        rsi = numeric_series(out, "rsi")
        technical_ready = (
            close.notna()
            & close.gt(0)
            & out["symbol_hist_len"].ge(3)
            & (ma5.notna() | rsi.notna() | score.ne(0))
        )
        out["technical_ready"] = technical_ready.astype(int)

        # scoring_pipelineの内部列名が存在する場合はDB列へ寄せる。
        alias_pairs = [
            ("_score_base", "base"),
            ("_score_trend", "trend"),
            ("_score_mom", "mom"),
            ("_score_velocity", "vel"),
            ("_score_penalty", "pen"),
            ("_combined_score", "combined_score"),
        ]
        for src, dst in alias_pairs:
            if src in out.columns and (dst not in out.columns or (numeric_series(out, dst).fillna(0) == 0).all()):
                out[dst] = numeric_series(out, src).fillna(0.0)

        if "combined_score" not in out.columns or (numeric_series(out, "combined_score").fillna(0) == 0).all():
            out["combined_score"] = numeric_series(out, "final_score").fillna(numeric_series(out, "score")).fillna(0.0)

        for col in ["base", "trend", "mom", "vel", "pen"]:
            if col not in out.columns:
                out[col] = 0.0

        return out

    except Exception:
        logger.exception("[YAHOO COMPUTE] ensure yahoo extra calculated columns failed interval=%s", interval)
        return safe_df(df)


def ensure_actual_db_schema_columns(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    summary DBの実カラムを読み取り、DataFrame側に不足列を追加する。
    計算不能列は安全値で追加し、ログに出す。
    """
    try:
        out = safe_df(df)
        if out.empty:
            return pd.DataFrame()

        table = summary_table_for_interval(interval)
        db_cols = get_table_columns(table)

        if not db_cols:
            logger.warning(
                "[YAHOO SUMMARY SCHEMA CHECK] table=%s interval=%s db_cols=0 -> use preferred columns only df_cols=%s",
                table,
                interval,
                len(out.columns),
            )
            return out

        before_cols = set(map(str, out.columns))
        added_cols: list[str] = []
        zero_filled_cols: list[str] = []

        for col in db_cols:
            if col == "id":
                continue
            if col in out.columns:
                continue
            default_value = _default_value_for_missing_db_col(col)
            out[col] = default_value
            added_cols.append(col)
            if _is_zero_default_value(default_value):
                zero_filled_cols.append(col)

        after_cols = set(map(str, out.columns))
        still_missing = [c for c in db_cols if c != "id" and c not in after_cols]
        computed_or_existing = [c for c in db_cols if c != "id" and c in before_cols]

        logger.warning(
            "[YAHOO SUMMARY SCHEMA CHECK] table=%s interval=%s db_cols=%s df_cols_before=%s df_cols_after=%s added_cols=%s zero_filled_cols=%s still_missing=%s computed_or_existing=%s",
            table,
            interval,
            len(db_cols),
            len(before_cols),
            len(out.columns),
            added_cols[:120],
            zero_filled_cols[:120],
            still_missing[:120],
            computed_or_existing[:120],
        )

        preferred = [c for c in db_cols if c in out.columns and c != "id"]
        others = [c for c in out.columns if c not in preferred]
        out = out[preferred + others].copy()

        return out

    except Exception:
        logger.exception("[YAHOO COMPUTE] ensure actual db schema columns failed interval=%s", interval)
        return safe_df(df)


def ensure_summary_schema_columns(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    try:
        out = safe_df(df)
        if out.empty:
            return pd.DataFrame()

        out = normalize_datetime_df(out)
        if out.empty:
            return pd.DataFrame()

        interval = int(interval)

        for src, dst in [
            ("open", "open_price"),
            ("high", "high_price"),
            ("low", "low_price"),
            ("close", "close_price"),
        ]:
            if src in out.columns and dst not in out.columns:
                out[dst] = pd.to_numeric(out[src], errors="coerce")

        for src, dst in [
            ("open_price", "open"),
            ("high_price", "high"),
            ("low_price", "low"),
            ("close_price", "close"),
        ]:
            if src in out.columns and dst not in out.columns:
                out[dst] = pd.to_numeric(out[src], errors="coerce")

        if "date" not in out.columns:
            out["date"] = out["datetime"].dt.strftime("%Y-%m-%d")

        if "time_range" not in out.columns:
            out["time_range"] = build_time_range_from_datetime(out["datetime"], interval)

        if "time" not in out.columns:
            out["time"] = out["datetime"].dt.strftime("%H:%M:%S")

        if "start_time" not in out.columns:
            out["start_time"] = build_start_time(out["datetime"], interval)

        if "end_time" not in out.columns:
            out["end_time"] = build_end_time(out["datetime"])

        out["interval"] = interval
        out["source"] = yahoo_source_for_interval(interval)

        out = backfill_symbolname(out)

        if "signal" not in out.columns:
            out["signal"] = ""

        if "volume" not in out.columns:
            out["volume"] = 0.0

        out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)

        if "last_update" not in out.columns:
            out["last_update"] = pd.Timestamp.now()

        for col in [
            "rsi", "macd", "hist", "ma5", "ma25", "ma75",
            "ema12", "ema26", "atr",
            "bb_mid", "bb_upper", "bb_lower", "bb_width",
            "score_buy", "score_sell", "score_total",
            "score", "final_score", "display_score",
            "slope", "slope_atr_scaled", "mtf", "score_mtf",
        ]:
            if col not in out.columns:
                out[col] = 0.0

        out = ensure_yahoo_extra_calculated_columns(out, interval=interval)
        if out.empty:
            return pd.DataFrame()

        out = ensure_actual_db_schema_columns(out, interval=interval)
        if out.empty:
            return pd.DataFrame()

        return out

    except Exception:
        logger.exception("[YAHOO COMPUTE] ensure summary schema columns failed")
        return pd.DataFrame()


def finalize_before_save(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    try:
        out = safe_df(df)
        if out.empty:
            return pd.DataFrame()

        out = normalize_datetime_df(out)
        if out.empty:
            return pd.DataFrame()

        if "symbol" not in out.columns or "datetime" not in out.columns:
            logger.warning("[YAHOO COMPUTE] finalize missing key columns")
            return pd.DataFrame()

        out = (
            out.dropna(subset=["symbol", "datetime"])
               .sort_values(["symbol", "datetime"], kind="stable")
               .drop_duplicates(subset=["symbol", "datetime"], keep="last")
               .reset_index(drop=True)
        )

        if out.empty:
            return pd.DataFrame()

        out = backfill_symbolname(out)
        out = ensure_score_columns(out)
        if out.empty:
            return pd.DataFrame()

        out = ensure_summary_schema_columns(out, interval=interval)
        if out.empty:
            return pd.DataFrame()

        out = (
            out.sort_values(["symbol", "datetime"], kind="stable")
               .drop_duplicates(subset=["symbol", "datetime"], keep="last")
               .reset_index(drop=True)
        )

        existing = [c for c in PREFERRED_SUMMARY_COLUMNS if c in out.columns]
        others = [c for c in out.columns if c not in existing]
        out = out[existing + others].copy()

        return out

    except Exception:
        logger.exception("[YAHOO COMPUTE] finalize before save failed")
        return pd.DataFrame()


def warn_if_suspicious_zero_scores(df: pd.DataFrame) -> None:
    try:
        if df is None or df.empty:
            return

        score_zero = (numeric_series(df, "score").fillna(0) == 0).all()
        final_zero = (numeric_series(df, "final_score").fillna(0) == 0).all()
        display_zero = (numeric_series(df, "display_score").fillna(0) == 0).all()

        signal_cols = [
            c for c in [
                "_score_base",
                "_score_trend",
                "_score_mom",
                "_score_velocity",
                "score_buy",
                "score_sell",
                "score_total",
                "score_mtf",
                "slope",
                "slope_atr_scaled",
                "mtf",
                "rsi",
                "macd",
                "hist",
            ] if c in df.columns
        ]

        signal_nonzero = False
        for c in signal_cols:
            s = numeric_series(df, c).fillna(0)
            if (s != 0).any():
                signal_nonzero = True
                break

        if score_zero and final_zero and display_zero and signal_nonzero:
            logger.warning(
                "[YAHOO COMPUTE] suspicious zero-score frame rows=%d symbols=%d signal_cols=%s",
                len(df),
                df["symbol"].nunique() if "symbol" in df.columns else 0,
                signal_cols,
            )
    except Exception:
        logger.debug("[YAHOO COMPUTE] zero-score anomaly check failed", exc_info=True)


def compute_summary_frame(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    """
    1 interval分のサマリー計算をまとめて実行する。
    """
    out = prepare_summary_frame(df, interval=interval)
    if out.empty:
        return out

    out = apply_indicators(out, interval=interval)
    if out.empty:
        return out

    out = apply_scoring(out, interval=interval)
    if out.empty:
        return out

    out = finalize_before_save(out, interval=interval)
    if out.empty:
        return out

    warn_if_suspicious_zero_scores(out)

    logger.info(
        "[YAHOO COMPUTE] computed interval=%s rows=%s symbols=%s latest=%s score_nonzero=%s cols=%s",
        interval,
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns else 0,
        out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
        int((numeric_series(out, "score").fillna(0) != 0).sum()) if not out.empty else 0,
        len(out.columns),
    )

    return out


__all__ = [
    "build_time_range_from_datetime",
    "build_start_time",
    "build_end_time",
    "prepare_summary_frame",
    "apply_indicators",
    "apply_scoring",
    "ensure_score_columns",
    "ensure_yahoo_extra_calculated_columns",
    "ensure_actual_db_schema_columns",
    "ensure_summary_schema_columns",
    "finalize_before_save",
    "warn_if_suspicious_zero_scores",
    "compute_summary_frame",
]
