# ============================================================
# File   : trading/summary/engine/ranking_summary_engine.py
# Ver    : PRODUCTION-STABLE-RANKING-SUMMARY-SCORE-TECH-FILL-V2
# ------------------------------------------------------------
# ✔ build_ranking_summary 公開
# ✔ global_data.ranking_snapshot_1min 優先
# ✔ ranking DB fallback
# ✔ ranking_ma_1min join
# ✔ ma5 / ma25 / ma75 summary互換補完
# ✔ market filter
# ✔ symbolname / price / close / datetime 安全補完
# ✔ ranking_ma datetime列自動解決
# ✔ ranking_type / type / rank_types 補完
# ✔ score / score_buy / score_sell / score_total / final_score 補完
# ✔ slope / slope_atr_scaled / score_slope 補完
# ✔ mtf / score_mtf / mtf_score 補完
# ✔ macd / signal / hist / rsi 補完
# ✔ 1min / 3min / 5min のランキング由来サマリーを同じ列構成に揃える
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
from typing import Optional, Iterable

import numpy as np
import pandas as pd
from sqlalchemy import text

logger = logging.getLogger(__name__)

try:
    from global_state import global_data  # type: ignore
except Exception:
    try:
        from core.global_context import global_data  # type: ignore
    except Exception:
        class _FallbackGlobalData:
            pass
        global_data = _FallbackGlobalData()


# ============================================================
# helpers
# ============================================================

def _safe_df(obj) -> pd.DataFrame:
    if isinstance(obj, pd.DataFrame):
        try:
            return obj.copy()
        except Exception:
            return obj
    try:
        return pd.DataFrame(obj).copy()
    except Exception:
        return pd.DataFrame()


def _safe_text(v) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.lower() in {"nan", "none", "nat", "<na>", "pd.na"}:
            return ""
        return s
    except Exception:
        return ""


def _safe_symbol(v) -> str:
    s = _safe_text(v)
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _to_dt_series(s: pd.Series) -> pd.Series:
    try:
        out = pd.to_datetime(s, errors="coerce")
        try:
            out = out.dt.tz_localize(None)
        except Exception:
            pass
        return out
    except Exception:
        return pd.to_datetime(pd.Series([None] * len(s), index=s.index), errors="coerce")


def _to_num(s, default=np.nan) -> pd.Series:
    try:
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0] if s.shape[1] else pd.Series([], dtype="float64")
        out = pd.to_numeric(s, errors="coerce")
        if not isinstance(out, pd.Series):
            out = pd.Series(out)
        if default is not None:
            out = out.fillna(default)
        return out.astype("float64")
    except Exception:
        try:
            return pd.Series(default, index=s.index, dtype="float64")
        except Exception:
            return pd.Series(dtype="float64")


def _first_existing_series(df: pd.DataFrame, candidates: Iterable[str], default="") -> pd.Series:
    out = pd.Series(default, index=df.index, dtype="object")
    for c in candidates:
        if c not in df.columns:
            continue
        try:
            s = df[c]
            if isinstance(s, pd.DataFrame):
                if s.shape[1] == 0:
                    continue
                s = s.iloc[:, 0]
            ss = s.astype(str).replace({"nan": "", "NaN": "", "None": "", "<NA>": "", "pd.NA": ""})
            mask = out.astype(str).str.len().eq(0) & ss.astype(str).str.len().gt(0)
            out.loc[mask] = ss.loc[mask]
        except Exception:
            continue
    return out


def _normalize_market_text(v: object) -> str:
    txt = _safe_text(v).upper()
    mapping = {
        "TS": "スタンダード",
        "TG": "グロース",
        "TP": "プライム",
        "STD": "スタンダード",
        "GRT": "グロース",
        "PRM": "プライム",
        "STANDARD": "スタンダード",
        "GROWTH": "グロース",
        "PRIME": "プライム",
        "東証S": "スタンダード",
        "東証G": "グロース",
        "東証P": "プライム",
    }
    return mapping.get(txt, _safe_text(v))


def _safe_getattr(obj, name: str, default=None):
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _ensure_summary_compatible_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty:
        return out

    try:
        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].map(_safe_symbol)
        else:
            for c in ("Symbol", "銘柄コード", "code", "Code"):
                if c in out.columns:
                    out["symbol"] = out[c].map(_safe_symbol)
                    break
            if "symbol" not in out.columns:
                out["symbol"] = ""

        if "symbolname" not in out.columns:
            out["symbolname"] = _first_existing_series(
                out,
                ["symbolname_view", "name", "銘柄名", "SymbolName", "symbol_name"],
                default="",
            )
        else:
            out["symbolname"] = out["symbolname"].fillna("").astype(str)
        miss = out["symbolname"].astype(str).str.len().eq(0)
        out.loc[miss, "symbolname"] = out.loc[miss, "symbol"]
        if "symbolname_view" not in out.columns:
            out["symbolname_view"] = out["symbolname"]

        dt_col = None
        for c in ("datetime", "snapshot_time", "time", "end_time", "start_time", "inserted_at", "created_at"):
            if c in out.columns:
                dt_col = c
                break

        if dt_col is None:
            if "date" in out.columns and "time" in out.columns:
                out["datetime"] = _to_dt_series(out["date"].astype(str).str.strip() + " " + out["time"].astype(str).str.strip())
            else:
                out["datetime"] = pd.NaT
        else:
            out["datetime"] = _to_dt_series(out[dt_col])

        if "close" not in out.columns:
            for c in ("current_price", "price", "close_price", "現在値", "price_now"):
                if c in out.columns:
                    out["close"] = pd.to_numeric(out[c], errors="coerce")
                    break
        if "close" not in out.columns:
            out["close"] = pd.NA
        out["close"] = pd.to_numeric(out["close"], errors="coerce")

        if "close_price" not in out.columns:
            out["close_price"] = out["close"]
        if "price" not in out.columns:
            out["price"] = out["close"]
        if "current_price" not in out.columns:
            out["current_price"] = out["close"]

        for c in ("open", "high", "low"):
            if c not in out.columns:
                out[c] = out["close"]
            else:
                out[c] = pd.to_numeric(out[c], errors="coerce").where(pd.to_numeric(out[c], errors="coerce").notna(), out["close"])

        for src, dst in (("open", "open_price"), ("high", "high_price"), ("low", "low_price")):
            if dst not in out.columns:
                out[dst] = pd.to_numeric(out[src], errors="coerce")

        if "volume" not in out.columns:
            for c in ("trading_volume", "vol", "出来高"):
                if c in out.columns:
                    out["volume"] = pd.to_numeric(out[c], errors="coerce")
                    break
        if "volume" not in out.columns:
            out["volume"] = 0.0
        out["volume"] = pd.to_numeric(out["volume"], errors="coerce").fillna(0.0)

        if "turnover" not in out.columns:
            for c in ("trading_value", "売買代金", "amount"):
                if c in out.columns:
                    out["turnover"] = pd.to_numeric(out[c], errors="coerce")
                    break
        if "turnover" not in out.columns:
            out["turnover"] = pd.NA

        # ranking type / rank aliases
        rank_type = _first_existing_series(
            out,
            [
                "ranking_type",
                "rank_type",
                "rank_types",
                "type",
                "Type",
                "ランキング種別",
                "category",
                "ranking_category",
            ],
            default="",
        )
        out["ranking_type"] = rank_type
        out["rank_types"] = out["ranking_type"]
        out["type"] = out["ranking_type"]

        if "rank" not in out.columns:
            for c in ("rank_position", "ranking_rank", "best_rank_position", "順位"):
                if c in out.columns:
                    out["rank"] = pd.to_numeric(out[c], errors="coerce")
                    break
        if "rank" not in out.columns:
            out["rank"] = pd.NA

        if "best_rank_position" not in out.columns:
            out["best_rank_position"] = pd.to_numeric(out["rank"], errors="coerce")
        if "last_rank_position" not in out.columns:
            out["last_rank_position"] = pd.to_numeric(out["rank"], errors="coerce")

        if "market_type_norm" not in out.columns:
            if "market" in out.columns:
                out["market_type_norm"] = out["market"].map(_normalize_market_text)
            elif "market_type" in out.columns:
                out["market_type_norm"] = out["market_type"].map(_normalize_market_text)
            else:
                out["market_type_norm"] = ""
        if "market_type" not in out.columns:
            out["market_type"] = out["market_type_norm"]

        for c in (
            "score", "score_buy", "score_sell", "buy_score", "sell_score", "score_total", "total_score",
            "display_score", "final_score", "slope", "slope_atr_scaled", "score_slope", "mtf", "score_mtf",
            "mtf_score", "rsi", "macd", "signal", "hist",
        ):
            if c not in out.columns:
                out[c] = pd.NA

        return out
    except Exception:
        logger.exception("[RANKING SUMMARY] summary compatible normalize failed")
        return out


# ============================================================
# source loaders
# ============================================================

def _load_from_global_data() -> pd.DataFrame:
    candidates = []
    for attr in ("ranking_snapshot_1min", "latest_ranking_raw", "ranking_snapshot"):
        try:
            candidates.append((attr, _safe_getattr(global_data, attr, None)))
        except Exception:
            pass

    for attr_name, src in candidates:
        df = _safe_df(src)
        if not df.empty:
            logger.info("[RANKING SUMMARY] global_data source hit name=%s rows=%d", attr_name, len(df))
            return df
    return pd.DataFrame()


def _load_from_ranking_db() -> pd.DataFrame:
    try:
        from database.session import get_ranking_engine
        engine = get_ranking_engine()
        if engine is None:
            logger.warning("[RANKING SUMMARY] ranking engine unavailable")
            return pd.DataFrame()

        sql = text("SELECT * FROM ranking_snapshot_1min")
        with engine.begin() as conn:
            df = pd.read_sql(sql, conn)
        return _safe_df(df)
    except Exception:
        logger.exception("[RANKING SUMMARY] ranking DB load failed")
        return pd.DataFrame()


# ============================================================
# ranking_ma helpers
# ============================================================

def _get_table_columns(conn, table_name: str) -> set[str]:
    try:
        rows = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        return {str(r[1]) for r in rows if len(r) > 1}
    except Exception:
        logger.exception("[RANKING SUMMARY] failed to inspect columns: %s", table_name)
        return set()


def _resolve_ranking_ma_dt_expr(columns: set[str]) -> tuple[str | None, str | None]:
    if "datetime" in columns:
        return "datetime", "datetime"
    if "end_time" in columns:
        return "end_time", "end_time"
    if "start_time" in columns:
        return "start_time", "start_time"
    if "snapshot_time" in columns:
        return "snapshot_time", "snapshot_time"
    if "date" in columns and "time" in columns:
        expr = "date || ' ' || time"
        return expr, expr
    return None, None


def _load_latest_ranking_ma(conn, symbols: Iterable[str]) -> pd.DataFrame:
    symbols = [str(s).strip() for s in (symbols or []) if str(s).strip()]
    if not symbols:
        return pd.DataFrame(columns=["symbol", "datetime", "close", "ma5", "ma25", "ma75"])

    table_name = "ranking_ma_1min"
    columns = _get_table_columns(conn, table_name)
    if not columns:
        logger.warning("[RANKING SUMMARY] ranking_ma columns not found")
        return pd.DataFrame(columns=["symbol", "datetime", "close", "ma5", "ma25", "ma75"])

    dt_key_expr, dt_select_expr = _resolve_ranking_ma_dt_expr(columns)
    if not dt_key_expr or not dt_select_expr:
        logger.warning("[RANKING SUMMARY] ranking_ma usable datetime column not found: cols=%s", sorted(columns))
        return pd.DataFrame(columns=["symbol", "datetime", "close", "ma5", "ma25", "ma75"])

    value_cols = []
    for c in ["close", "ma5", "ma25", "ma75"]:
        value_cols.append(f"t.{c}" if c in columns else f"NULL AS {c}")

    placeholders = ", ".join([f":s{i}" for i in range(len(symbols))])
    params = {f"s{i}": s for i, s in enumerate(symbols)}

    sql = f"""
        WITH latest AS (
            SELECT symbol, MAX({dt_key_expr}) AS max_dt
            FROM {table_name}
            WHERE symbol IN ({placeholders})
            GROUP BY symbol
        )
        SELECT t.symbol, {dt_select_expr} AS datetime, {", ".join(value_cols)}
        FROM {table_name} t
        INNER JOIN latest l ON t.symbol = l.symbol AND {dt_key_expr} = l.max_dt
    """

    try:
        df = pd.read_sql(text(sql), conn, params=params)
        df = _safe_df(df)
        if df.empty:
            logger.warning("[RANKING SUMMARY] ranking_ma empty")
            return pd.DataFrame(columns=["symbol", "datetime", "close", "ma5", "ma25", "ma75"])
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        for c in ("close", "ma5", "ma25", "ma75"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        return df
    except Exception:
        logger.exception("[RANKING SUMMARY] ranking_ma load failed")
        return pd.DataFrame(columns=["symbol", "datetime", "close", "ma5", "ma25", "ma75"])


# ============================================================
# filters / enrich
# ============================================================

def _apply_market_filter(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty:
        return out
    try:
        before = len(out)
        if "market_type_norm" in out.columns:
            keep = out["market_type_norm"].isin(["プライム", "スタンダード", "グロース", "ALL", ""])
            out = out[keep].copy()
        logger.info("[RANKING SUMMARY] market filter before=%d after=%d", before, len(out))
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[RANKING SUMMARY] market filter failed")
        return out


def _attach_ranking_ma(conn, df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty or "symbol" not in out.columns:
        return out
    try:
        ma_df = _load_latest_ranking_ma(conn, out["symbol"].astype(str).tolist())
        if ma_df.empty:
            for c in ("ma5", "ma25", "ma75"):
                if c not in out.columns:
                    out[c] = pd.NA
            logger.warning("[RANKING SUMMARY] ranking_ma empty")
            return out

        keep_cols = [c for c in ["symbol", "ma5", "ma25", "ma75"] if c in ma_df.columns]
        ma_df = ma_df[keep_cols].drop_duplicates(subset=["symbol"], keep="last")
        before_cols = set(out.columns)
        out = out.merge(ma_df, on="symbol", how="left", suffixes=("", "_majoin"))

        for c in ("ma5", "ma25", "ma75"):
            alt = f"{c}_majoin"
            if alt in out.columns:
                if c in before_cols:
                    base = pd.to_numeric(out[c], errors="coerce")
                    addv = pd.to_numeric(out[alt], errors="coerce")
                    out[c] = base.where(base.notna(), addv)
                else:
                    out[c] = pd.to_numeric(out[alt], errors="coerce")
                out.drop(columns=[alt], inplace=True, errors="ignore")

        logger.info(
            "[RANKING SUMMARY] ranking_ma joined rows=%d ma5_nonnull=%d ma25_nonnull=%d ma75_nonnull=%d",
            len(out),
            int(pd.to_numeric(out["ma5"], errors="coerce").notna().sum()) if "ma5" in out.columns else 0,
            int(pd.to_numeric(out["ma25"], errors="coerce").notna().sum()) if "ma25" in out.columns else 0,
            int(pd.to_numeric(out["ma75"], errors="coerce").notna().sum()) if "ma75" in out.columns else 0,
        )
        return out
    except Exception:
        logger.exception("[RANKING SUMMARY] ranking_ma attach failed")
        return out


def _join_rank_types(s: pd.Series) -> str:
    vals: list[str] = []
    for v in s.astype(str).tolist():
        for part in str(v).split(","):
            part = part.strip()
            if part and part not in vals and part not in ("nan", "None", "<NA>"):
                vals.append(part)
    return ",".join(vals)


def _resample_ranking_rows(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    out = _safe_df(df)
    interval = int(interval)
    if out.empty:
        return out
    if "datetime" not in out.columns:
        return out

    try:
        out["datetime"] = _to_dt_series(out["datetime"])
        out = out.dropna(subset=["datetime"]).copy()
        if interval <= 1:
            out["interval"] = 1
            return out

        out["bucket"] = out["datetime"].dt.floor(f"{interval}min")
        out = out.sort_values(["symbol", "bucket", "datetime"]).copy()

        agg = {
            "datetime": ("bucket", "last"),
            "symbolname": ("symbolname", "last"),
            "symbolname_view": ("symbolname_view", "last"),
            "close": ("close", "last"),
            "price": ("price", "last"),
            "current_price": ("current_price", "last"),
            "volume": ("volume", "max"),
            "turnover": ("turnover", "last"),
            "rank": ("rank", "last"),
            "best_rank_position": ("best_rank_position", "min"),
            "last_rank_position": ("last_rank_position", "last"),
            "ranking_type": ("ranking_type", _join_rank_types),
            "rank_types": ("rank_types", _join_rank_types),
            "type": ("type", _join_rank_types),
            "market": ("market", "last") if "market" in out.columns else ("market_type_norm", "last"),
            "market_type": ("market_type", "last"),
            "market_type_norm": ("market_type_norm", "last"),
        }
        usable = {k: v for k, v in agg.items() if v[0] in out.columns}
        res = out.groupby(["symbol", "bucket"], sort=False).agg(**usable).reset_index(drop=True)
        res["open"] = res["close"]
        res["high"] = res["close"]
        res["low"] = res["close"]
        res["open_price"] = res["open"]
        res["high_price"] = res["high"]
        res["low_price"] = res["low"]
        res["close_price"] = res["close"]
        res["interval"] = interval
        return res
    except Exception:
        logger.exception("[RANKING SUMMARY] resample failed interval=%s", interval)
        out["interval"] = interval
        return out


def _calc_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    try:
        delta = close.diff()
        gain = delta.clip(lower=0).rolling(window, min_periods=2).mean()
        loss = (-delta.clip(upper=0)).rolling(window, min_periods=2).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - (100 / (1 + rs))
        return rsi.fillna(50.0)
    except Exception:
        return pd.Series(50.0, index=close.index, dtype="float64")


def _type_bias_for_buy(rank_type: pd.Series) -> pd.Series:
    txt = rank_type.fillna("").astype(str)
    buy_words = ("値上", "上昇", "買", "TICK", "出来高", "売買代金", "急騰")
    sell_words = ("値下", "下落", "売", "急落")
    buy = txt.apply(lambda x: any(w in x for w in buy_words)).astype(float)
    sell = txt.apply(lambda x: any(w in x for w in sell_words)).astype(float)
    return buy - sell


def _fill_score_and_technical_columns(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty:
        return out

    try:
        out = _ensure_summary_compatible_columns(out)
        out["datetime"] = _to_dt_series(out["datetime"])
        out = out.dropna(subset=["datetime"]).copy()
        out = out.sort_values(["symbol", "datetime"]).reset_index(drop=True)

        close = pd.to_numeric(out["close"], errors="coerce")
        out["close"] = close
        out["close_price"] = close
        out["price"] = pd.to_numeric(out.get("price", close), errors="coerce").where(pd.to_numeric(out.get("price", close), errors="coerce").notna(), close)
        out["current_price"] = pd.to_numeric(out.get("current_price", close), errors="coerce").where(pd.to_numeric(out.get("current_price", close), errors="coerce").notna(), close)

        g = out.groupby("symbol", sort=False)

        # slope: 直近変化率をベースにする。ランキング由来は疑似足なので、ATRが無くても必ず算出する。
        pct1 = g["close"].pct_change(1).replace([np.inf, -np.inf], np.nan) * 100.0
        pct3 = g["close"].pct_change(3).replace([np.inf, -np.inf], np.nan) * 100.0
        pct5 = g["close"].pct_change(5).replace([np.inf, -np.inf], np.nan) * 100.0
        slope_calc = pct1.fillna(0.0)
        mtf_calc = (pct1.fillna(0.0) * 0.50) + (pct3.fillna(0.0) * 0.30) + (pct5.fillna(0.0) * 0.20)

        # macd / signal / hist / rsi
        macd_parts = []
        signal_parts = []
        hist_parts = []
        rsi_parts = []
        for _, sub in out.groupby("symbol", sort=False):
            c = pd.to_numeric(sub["close"], errors="coerce")
            ema12 = c.ewm(span=12, adjust=False, min_periods=2).mean()
            ema26 = c.ewm(span=26, adjust=False, min_periods=2).mean()
            macd = (ema12 - ema26).fillna(0.0)
            signal = macd.ewm(span=9, adjust=False, min_periods=2).mean().fillna(0.0)
            hist = (macd - signal).fillna(0.0)
            rsi = _calc_rsi(c)
            macd_parts.append(macd)
            signal_parts.append(signal)
            hist_parts.append(hist)
            rsi_parts.append(rsi)

        macd_calc = pd.concat(macd_parts).sort_index() if macd_parts else pd.Series(0.0, index=out.index)
        signal_calc = pd.concat(signal_parts).sort_index() if signal_parts else pd.Series(0.0, index=out.index)
        hist_calc = pd.concat(hist_parts).sort_index() if hist_parts else pd.Series(0.0, index=out.index)
        rsi_calc = pd.concat(rsi_parts).sort_index() if rsi_parts else pd.Series(50.0, index=out.index)

        def fill_numeric_col(col: str, calc: pd.Series, *, overwrite_zero: bool = True) -> None:
            base = pd.to_numeric(out[col], errors="coerce") if col in out.columns else pd.Series(np.nan, index=out.index)
            if overwrite_zero:
                mask = base.isna() | base.eq(0)
            else:
                mask = base.isna()
            out[col] = base.where(~mask, pd.to_numeric(calc, errors="coerce"))

        fill_numeric_col("slope", slope_calc)
        fill_numeric_col("slope_atr_scaled", slope_calc)
        fill_numeric_col("score_slope", slope_calc)
        fill_numeric_col("mtf", mtf_calc)
        fill_numeric_col("score_mtf", mtf_calc)
        fill_numeric_col("mtf_score", mtf_calc)
        fill_numeric_col("macd", macd_calc)
        fill_numeric_col("signal", signal_calc)
        fill_numeric_col("hist", hist_calc)
        fill_numeric_col("rsi", rsi_calc, overwrite_zero=False)

        # ranking_type / type が空なら rank_types から再補完
        if "ranking_type" not in out.columns or out["ranking_type"].astype(str).str.len().eq(0).all():
            out["ranking_type"] = _first_existing_series(out, ["rank_types", "type", "rank_type"], default="")
        out["rank_types"] = out["ranking_type"]
        out["type"] = out["ranking_type"]

        rank = pd.to_numeric(out.get("best_rank_position", out.get("rank", np.nan)), errors="coerce")
        rank_score = ((101.0 - rank.clip(lower=1, upper=100)) / 20.0).fillna(0.0)
        type_bias = _type_bias_for_buy(out["ranking_type"])
        slope_v = pd.to_numeric(out["slope"], errors="coerce").fillna(0.0)
        mtf_v = pd.to_numeric(out["mtf"], errors="coerce").fillna(0.0)
        macd_v = pd.to_numeric(out["macd"], errors="coerce").fillna(0.0)
        hist_v = pd.to_numeric(out["hist"], errors="coerce").fillna(0.0)
        rsi_v = pd.to_numeric(out["rsi"], errors="coerce").fillna(50.0)

        trend_buy = (slope_v.clip(lower=0) * 2.0) + (mtf_v.clip(lower=0) * 1.2) + (hist_v.clip(lower=0) * 0.5)
        trend_sell = ((-slope_v).clip(lower=0) * 2.0) + ((-mtf_v).clip(lower=0) * 1.2) + ((-hist_v).clip(lower=0) * 0.5)
        macd_buy = (macd_v > 0).astype(float) * 0.5
        macd_sell = (macd_v < 0).astype(float) * 0.5
        rsi_buy = ((rsi_v >= 45) & (rsi_v <= 75)).astype(float) * 0.3
        rsi_sell = ((rsi_v >= 25) & (rsi_v <= 55)).astype(float) * 0.3
        type_buy = type_bias.clip(lower=0) * 1.0
        type_sell = (-type_bias.clip(upper=0)) * 1.0

        score_buy_calc = (rank_score + trend_buy + macd_buy + rsi_buy + type_buy).round(4)
        score_sell_calc = (rank_score + trend_sell + macd_sell + rsi_sell + type_sell).round(4)
        score_calc = pd.concat([score_buy_calc, score_sell_calc], axis=1).max(axis=1).round(4)
        total_calc = (score_buy_calc + score_sell_calc).round(4)

        fill_numeric_col("score_buy", score_buy_calc)
        fill_numeric_col("buy_score", score_buy_calc)
        fill_numeric_col("score_sell", score_sell_calc)
        fill_numeric_col("sell_score", score_sell_calc)
        fill_numeric_col("score", score_calc)
        fill_numeric_col("display_score", score_calc)
        fill_numeric_col("final_score", score_calc)
        fill_numeric_col("score_total", total_calc)
        fill_numeric_col("total_score", total_calc)

        out["interval"] = int(interval)
        out["source"] = f"ranking_summary_{int(interval)}min"
        out["summary_source"] = "ranking"
        out["updated_at"] = pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")

        logger.info(
            "[RANKING SUMMARY] score/tech filled interval=%s rows=%d score_nonzero=%d slope_nonzero=%d mtf_nonzero=%d macd_nonzero=%d type_nonempty=%d",
            interval,
            len(out),
            int(pd.to_numeric(out["score"], errors="coerce").fillna(0).ne(0).sum()),
            int(pd.to_numeric(out["slope"], errors="coerce").fillna(0).ne(0).sum()),
            int(pd.to_numeric(out["mtf"], errors="coerce").fillna(0).ne(0).sum()),
            int(pd.to_numeric(out["macd"], errors="coerce").fillna(0).ne(0).sum()),
            int(out["ranking_type"].fillna("").astype(str).str.len().gt(0).sum()),
        )
        return out
    except Exception:
        logger.exception("[RANKING SUMMARY] score/technical fill failed interval=%s", interval)
        return out


# ============================================================
# main
# ============================================================

def build_ranking_summary(interval: int = 1) -> pd.DataFrame:
    interval = int(interval)
    logger.info("▶ ranking summary start interval=%s", interval)

    try:
        df = _load_from_global_data()
        src_name = "global_data.ranking_snapshot_1min"

        if df.empty:
            df = _load_from_ranking_db()
            src_name = "ranking_db.ranking_snapshot_1min"

        if df.empty:
            logger.warning("[RANKING SUMMARY] snapshot empty")
            return pd.DataFrame()

        logger.info(
            "[RANKING SUMMARY] loaded source=%s rows=%d symbols=%d",
            src_name,
            len(df),
            int(df["symbol"].astype(str).nunique()) if "symbol" in df.columns else 0,
        )

        df = _ensure_summary_compatible_columns(df)

        try:
            logger.info("[RANKING SUMMARY] columns=%s market_head=%s", list(df.columns), df["market"].head(10).tolist() if "market" in df.columns else [])
            logger.info("[RANKING SUMMARY] market_type_head=%s", df["market_type"].head(10).tolist() if "market_type" in df.columns else [])
            logger.info("[RANKING SUMMARY] market_type_norm_head=%s", df["market_type_norm"].head(10).tolist() if "market_type_norm" in df.columns else [])
        except Exception:
            logger.debug("[RANKING SUMMARY] column profile log failed", exc_info=True)

        df = _apply_market_filter(df)

        # 3min/5min はランキングsnapshotを疑似足として時間バケット化してから指標を作る。
        df = _resample_ranking_rows(df, interval)

        try:
            from database.session import get_ranking_engine
            engine = get_ranking_engine()
        except Exception:
            engine = None

        if engine is not None:
            try:
                with engine.begin() as conn:
                    df = _attach_ranking_ma(conn, df)
            except Exception:
                logger.exception("[RANKING SUMMARY] attach ranking_ma with engine failed")
        else:
            logger.warning("[RANKING SUMMARY] ranking engine unavailable for ranking_ma attach")
            for c in ("ma5", "ma25", "ma75"):
                if c not in df.columns:
                    df[c] = pd.NA

        # 最新1行に潰す前に、全履歴で slope / MACD / MTF / score を算出する。
        df = _fill_score_and_technical_columns(df, interval)

        if "datetime" in df.columns:
            s = _to_dt_series(df["datetime"])
            if s.notna().any():
                latest = s.max()
                df = df.loc[s == latest].copy().reset_index(drop=True)

        # 最新化後にも重要列を再保証する。
        df = _ensure_summary_compatible_columns(df)
        df["interval"] = interval
        df["source"] = f"ranking_summary_{interval}min"
        df["summary_source"] = "ranking"

        logger.info(
            "[RANKING SUMMARY] done interval=%s rows=%d symbols=%d latest_dt=%s ma75_nonnull=%d score_nonzero=%d slope_nonzero=%d mtf_nonzero=%d macd_nonzero=%d type_nonempty=%d",
            interval,
            len(df),
            int(df["symbol"].astype(str).nunique()) if not df.empty and "symbol" in df.columns else 0,
            str(df["datetime"].max()) if not df.empty and "datetime" in df.columns else None,
            int(pd.to_numeric(df["ma75"], errors="coerce").notna().sum()) if "ma75" in df.columns else 0,
            int(pd.to_numeric(df["score"], errors="coerce").fillna(0).ne(0).sum()) if "score" in df.columns else 0,
            int(pd.to_numeric(df["slope"], errors="coerce").fillna(0).ne(0).sum()) if "slope" in df.columns else 0,
            int(pd.to_numeric(df["mtf"], errors="coerce").fillna(0).ne(0).sum()) if "mtf" in df.columns else 0,
            int(pd.to_numeric(df["macd"], errors="coerce").fillna(0).ne(0).sum()) if "macd" in df.columns else 0,
            int(df["ranking_type"].fillna("").astype(str).str.len().gt(0).sum()) if "ranking_type" in df.columns else 0,
        )
        return df

    except Exception:
        logger.exception("[RANKING SUMMARY] build failed")
        return pd.DataFrame()


def run_ranking_summary(interval: int = 1, **kwargs) -> pd.DataFrame:
    return build_ranking_summary(interval=interval)


def job_ranking_summary(interval: int = 1, **kwargs) -> pd.DataFrame:
    return build_ranking_summary(interval=interval)


def run(interval: int = 1, **kwargs) -> pd.DataFrame:
    return build_ranking_summary(interval=interval)


__all__ = [
    "build_ranking_summary",
    "run_ranking_summary",
    "job_ranking_summary",
    "run",
]
