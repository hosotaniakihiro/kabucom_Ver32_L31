from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)
_INSTALLED = False


def _num(c, out: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    try:
        if col in out.columns:
            return pd.to_numeric(out[col], errors="coerce")
        return pd.Series(default, index=out.index, dtype="float64")
    except Exception:
        return pd.Series(default, index=out.index, dtype="float64")


def _all_zero_or_na(s: pd.Series) -> bool:
    try:
        return bool((pd.to_numeric(s, errors="coerce").fillna(0.0) == 0.0).all())
    except Exception:
        return True


def _safe_nunique(out: pd.DataFrame, col: str) -> int:
    try:
        return int(out[col].nunique()) if col in out.columns else 0
    except Exception:
        return 0


def _repair_yahoo_signal_columns(c, df: pd.DataFrame, *, interval: int | None = None) -> pd.DataFrame:
    """
    Yahoo補完では scoring_pipeline が内部成分(mom/vel等)だけを作り、
    score_buy/score_sell/score_total/score が全ゼロで終わることがある。
    OHLCV/MA/RSI/MACDから保存・エントリー判定に使える最低限の信号列を復元する。
    既に非ゼロの列がある場合は上書きしない。
    """
    try:
        out = c.safe_df(df)
        if out.empty or "symbol" not in out.columns:
            return out

        if "datetime" in out.columns:
            try:
                out = out.sort_values(["symbol", "datetime"], kind="stable").copy()
            except Exception:
                out = out.copy()
        else:
            out = out.copy()

        grp = out["symbol"].astype(str)
        close = _num(c, out, "close")
        high = _num(c, out, "high")
        low = _num(c, out, "low")
        volume = _num(c, out, "volume")
        prev_close = close.groupby(grp, sort=False).shift(1).replace(0, pd.NA)
        close_denom = close.replace(0, pd.NA)

        # slope: close pct change per bar. 既存が全ゼロ/NAの時だけ復元。
        slope_pct = ((close - prev_close) / prev_close * 100.0).replace([float("inf"), float("-inf")], pd.NA).fillna(0.0)
        if "slope" not in out.columns or _all_zero_or_na(out["slope"]):
            out["slope"] = slope_pct

        # ATRが無ければ簡易TR/rollingで補完。
        atr = _num(c, out, "atr")
        if _all_zero_or_na(atr):
            tr1 = (high - low).abs()
            tr2 = (high - prev_close).abs()
            tr3 = (low - prev_close).abs()
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1).fillna(0.0)
            atr = tr.groupby(grp, sort=False).transform(lambda s: s.rolling(14, min_periods=3).mean()).fillna(tr)
            if "atr" not in out.columns or _all_zero_or_na(out["atr"]):
                out["atr"] = atr.fillna(0.0)

        atr_pct = (atr / close_denom * 100.0).replace([float("inf"), float("-inf")], pd.NA)
        slope_atr = (pd.to_numeric(out["slope"], errors="coerce") / atr_pct.replace(0, pd.NA)).replace(
            [float("inf"), float("-inf")], pd.NA
        ).fillna(pd.to_numeric(out["slope"], errors="coerce")).fillna(0.0)
        if "slope_atr_scaled" not in out.columns or _all_zero_or_na(out["slope_atr_scaled"]):
            out["slope_atr_scaled"] = slope_atr

        ma5 = _num(c, out, "ma5")
        ma25 = _num(c, out, "ma25")
        rsi = _num(c, out, "rsi")
        macd = _num(c, out, "macd")
        hist = _num(c, out, "hist")
        mom = _num(c, out, "mom")
        vel = _num(c, out, "vel")
        momentum_score = _num(c, out, "momentum_score")
        volume_score = _num(c, out, "volume_score")

        ma5_gap = ((close - ma5) / close_denom * 100.0).replace([float("inf"), float("-inf")], pd.NA).fillna(0.0)
        ma25_gap = ((close - ma25) / close_denom * 100.0).replace([float("inf"), float("-inf")], pd.NA).fillna(0.0)
        ma5_slope = ma5.groupby(grp, sort=False).diff().fillna(0.0)
        vol_prev = volume.groupby(grp, sort=False).shift(1).replace(0, pd.NA)
        vol_ratio = (volume / vol_prev).replace([float("inf"), float("-inf")], pd.NA).fillna(1.0)
        vol_boost = (vol_ratio.clip(lower=0.5, upper=5.0) - 1.0).fillna(0.0)

        trend_buy = (
            ma5_gap.clip(lower=0.0, upper=3.0)
            + ma25_gap.clip(lower=0.0, upper=3.0) * 0.5
            + pd.to_numeric(out["slope"], errors="coerce").clip(lower=0.0, upper=3.0) * 2.0
            + pd.Series(ma5_slope, index=out.index).clip(lower=0.0).where(close_denom.notna(), 0.0) / close_denom.fillna(1.0) * 100.0
        )
        trend_sell = (
            (-ma5_gap).clip(lower=0.0, upper=3.0)
            + (-ma25_gap).clip(lower=0.0, upper=3.0) * 0.5
            + (-pd.to_numeric(out["slope"], errors="coerce")).clip(lower=0.0, upper=3.0) * 2.0
            + (-pd.Series(ma5_slope, index=out.index)).clip(lower=0.0).where(close_denom.notna(), 0.0) / close_denom.fillna(1.0) * 100.0
        )

        osc_buy = (50.0 - rsi).clip(lower=0.0, upper=25.0) * 0.04 + hist.clip(lower=0.0).fillna(0.0) * 0.02 + macd.clip(lower=0.0).fillna(0.0) * 0.01
        osc_sell = (rsi - 50.0).clip(lower=0.0, upper=25.0) * 0.04 + (-hist).clip(lower=0.0).fillna(0.0) * 0.02 + (-macd).clip(lower=0.0).fillna(0.0) * 0.01
        component = (mom.abs().fillna(0.0) + vel.abs().fillna(0.0) + momentum_score.abs().fillna(0.0) + volume_score.abs().fillna(0.0)) * 0.15
        vol_component = vol_boost.clip(lower=0.0, upper=3.0) * 0.5

        buy = (trend_buy + osc_buy + component + vol_component).replace([float("inf"), float("-inf")], pd.NA).fillna(0.0)
        sell = (trend_sell + osc_sell + component + vol_component).replace([float("inf"), float("-inf")], pd.NA).fillna(0.0)

        # 超低情報行は0のままにする。
        valid = close.gt(0) & (prev_close.notna() | ma5.notna() | rsi.notna() | macd.notna())
        buy = buy.where(valid, 0.0)
        sell = sell.where(valid, 0.0)

        score_buy = _num(c, out, "score_buy")
        score_sell = _num(c, out, "score_sell")
        score_total = _num(c, out, "score_total")
        score = _num(c, out, "score")
        final_score = _num(c, out, "final_score")
        display_score = _num(c, out, "display_score")

        if _all_zero_or_na(score_buy):
            out["score_buy"] = buy
        if _all_zero_or_na(score_sell):
            out["score_sell"] = -sell
        if _all_zero_or_na(score_total):
            out["score_total"] = out["score_buy"] + out["score_sell"]

        abs_buy = pd.to_numeric(out["score_buy"], errors="coerce").abs().fillna(0.0)
        abs_sell = pd.to_numeric(out["score_sell"], errors="coerce").abs().fillna(0.0)
        signed = pd.to_numeric(out["score_buy"], errors="coerce").where(
            abs_buy >= abs_sell,
            pd.to_numeric(out["score_sell"], errors="coerce"),
        ).fillna(0.0)
        abs_score = signed.abs()

        if _all_zero_or_na(score):
            out["score"] = abs_score
        if _all_zero_or_na(final_score):
            out["final_score"] = abs_score
        if _all_zero_or_na(display_score):
            out["display_score"] = abs_score

        # mtf/score_mtf: 短期MA方向とscoreの向きを最低限反映。
        mtf_val = pd.Series(0.0, index=out.index, dtype="float64")
        mtf_val = mtf_val.where(~((ma5_gap > 0) & (pd.to_numeric(out["slope"], errors="coerce") > 0)), 1.0)
        mtf_val = mtf_val.where(~((ma5_gap < 0) & (pd.to_numeric(out["slope"], errors="coerce") < 0)), -1.0)
        if "mtf" not in out.columns or _all_zero_or_na(out["mtf"]):
            out["mtf"] = mtf_val
        if "score_mtf" not in out.columns or _all_zero_or_na(out["score_mtf"]):
            out["score_mtf"] = pd.to_numeric(out["mtf"], errors="coerce").fillna(0.0) * abs_score.clip(lower=0.0, upper=10.0)

        if "buy_score" not in out.columns or _all_zero_or_na(out["buy_score"]):
            out["buy_score"] = pd.to_numeric(out["score_buy"], errors="coerce").fillna(0.0)
        if "sell_score" not in out.columns or _all_zero_or_na(out["sell_score"]):
            out["sell_score"] = pd.to_numeric(out["score_sell"], errors="coerce").fillna(0.0)

        logger.warning(
            "[YAHOO COMPUTE SIGNAL REPAIR] interval=%s rows=%s symbols=%s score_nonzero=%s slope_nonzero=%s mtf_nonzero=%s",
            interval,
            len(out),
            _safe_nunique(out, "symbol"),
            int((_num(c, out, "score").fillna(0.0) != 0.0).sum()),
            int((_num(c, out, "slope").fillna(0.0) != 0.0).sum()),
            int((_num(c, out, "mtf").fillna(0.0) != 0.0).sum()),
        )
        return out
    except Exception:
        logger.exception("[YAHOO COMPUTE SIGNAL REPAIR] failed interval=%s", interval)
        return df


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.yahoo.pipeline.complement.compute as c

        old_schema = getattr(c, 'ensure_actual_db_schema_columns', None)
        if not getattr(old_schema, '_na_guard_v2', False):
            def patched_schema(df, interval):
                try:
                    out = c.safe_df(df)
                    if out.empty:
                        return pd.DataFrame()
                    table = c.summary_table_for_interval(interval)
                    db_cols = c.get_table_columns(table)
                    if not db_cols:
                        return out
                    before_cols = set(map(str, out.columns))
                    added_cols = []
                    zero_filled_cols = []
                    for col in db_cols:
                        if col == 'id' or col in out.columns:
                            continue
                        dv = c._default_value_for_missing_db_col(col)
                        out[col] = dv
                        added_cols.append(col)
                        is_zero = False
                        try:
                            if not pd.isna(dv) and not isinstance(dv, bool):
                                is_zero = isinstance(dv, (int, float)) and float(dv) == 0.0
                        except Exception:
                            is_zero = False
                        if is_zero:
                            zero_filled_cols.append(col)
                    after_cols = set(map(str, out.columns))
                    still_missing = [x for x in db_cols if x != 'id' and x not in after_cols]
                    computed_or_existing = [x for x in db_cols if x != 'id' and x in before_cols]
                    logger.warning('[YAHOO SUMMARY SCHEMA CHECK] table=%s interval=%s db_cols=%s df_cols_before=%s df_cols_after=%s added_cols=%s zero_filled_cols=%s still_missing=%s computed_or_existing=%s', table, interval, len(db_cols), len(before_cols), len(out.columns), added_cols[:120], zero_filled_cols[:120], still_missing[:120], computed_or_existing[:120])
                    preferred = [x for x in db_cols if x in out.columns and x != 'id']
                    others = [x for x in out.columns if x not in preferred]
                    return out[preferred + others].copy()
                except Exception:
                    logger.exception('[YAHOO COMPUTE] ensure actual db schema columns failed interval=%s', interval)
                    return c.safe_df(df)

            patched_schema._na_guard_v2 = True
            patched_schema._original = old_schema
            c.ensure_actual_db_schema_columns = patched_schema

        old_score = getattr(c, 'ensure_score_columns', None)
        if not getattr(old_score, '_signal_repair_v1', False):
            def patched_score(df):
                try:
                    out = old_score(df) if callable(old_score) else c.safe_df(df)
                    return _repair_yahoo_signal_columns(c, out, interval=None)
                except Exception:
                    logger.exception('[YAHOO COMPUTE SIGNAL REPAIR] patched ensure_score_columns failed')
                    return c.safe_df(df)

            patched_score._signal_repair_v1 = True
            patched_score._original = old_score
            c.ensure_score_columns = patched_score

        old_extra = getattr(c, 'ensure_yahoo_extra_calculated_columns', None)
        if not getattr(old_extra, '_signal_repair_v1', False):
            def patched_extra(df, interval):
                try:
                    out = old_extra(df, interval) if callable(old_extra) else c.safe_df(df)
                    return _repair_yahoo_signal_columns(c, out, interval=int(interval))
                except Exception:
                    logger.exception('[YAHOO COMPUTE SIGNAL REPAIR] patched ensure_yahoo_extra_calculated_columns failed interval=%s', interval)
                    return c.safe_df(df)

            patched_extra._signal_repair_v1 = True
            patched_extra._original = old_extra
            c.ensure_yahoo_extra_calculated_columns = patched_extra

        _INSTALLED = True
        logger.warning('[YAHOO COMPUTE SCHEMA NA GUARD] installed V2 signal_repair=True')
        return True
    except Exception:
        logger.exception('[YAHOO COMPUTE SCHEMA NA GUARD] install failed')
        return False

try:
    install()
except Exception:
    logger.exception('[YAHOO COMPUTE SCHEMA NA GUARD] auto install failed')

__all__ = ['install']