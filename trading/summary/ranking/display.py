# ============================================================
# File   : trading/summary/ranking/display.py
# Version: Ver1.1-PRODUCTION-RANKING-SUMMARY-DISPLAY
#          -RANKING-ONLY
#          -SESSION-ANCHOR
#          -NONZERO-TOP10-GUARD
#          -SCORE-AWARE-DEDUPE
# ------------------------------------------------------------
# ✔ RANKING SUMMARY TOP10 表示専用
# ✔ PUSH系表示関数は持たない
# ✔ 1銘柄2行固定表示
# ✔ 定時表示は print 出力
# ✔ score / buy / sell / slope / mtf / total / final / rsi / macd 表示
# ✔ base / trend / mom / vel / pen 表示
# ✔ symbolname は 1つだけ表示
# ✔ datetime はヘッダ以外では表示しない
# ✔ 株価は小数点第1位
# ✔ 指標は小数点第2位
# ✔ DataFrame 安全化
# ✔ 表示前に symbol ごと 1行へ重複除去
# ✔ 市場外 / 昼休み / 休場日の表示アンカー対応
# ✔ global_data / merged_summary を直接参照しない
#
# 【Ver1.1 修正】
# ✔ BUY TOP10 で score/buy/final/display が全0の行を除外
# ✔ SELL TOP10 で sell/下落score が全0の行を除外
# ✔ dedupe時に score あり行を優先して残す
# ✔ best_rank / rank / ranking_score / best_rank_value の列名揺れ吸収を強化
# ✔ score が無い場合 score_buy / score_sell / final_score から補完
# ✔ datetime parse warning を抑制
# ✔ 全0しか無い場合はゼロTOP10を出さず no candidates 表示
# ============================================================

from __future__ import annotations

import logging
import warnings
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

from .time_utils import resolve_display_slot

logger = logging.getLogger(__name__)


# ============================================================
# basic helpers
# ============================================================

def _safe_df(df: Any) -> pd.DataFrame:
    try:
        if df is None:
            return pd.DataFrame()

        if isinstance(df, pd.DataFrame):
            out = df.copy()
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
            logger.debug("[RANKING DISPLAY] multiindex flatten failed", exc_info=True)

        try:
            out.columns = [str(c) for c in out.columns]
        except Exception:
            logger.debug("[RANKING DISPLAY] stringify columns failed", exc_info=True)

        try:
            if out.columns.duplicated().any():
                out = out.loc[:, ~out.columns.duplicated(keep="last")].copy()
        except Exception:
            logger.debug("[RANKING DISPLAY] duplicate column cleanup failed", exc_info=True)

        out.replace([np.inf, -np.inf], np.nan, inplace=True)
        return out.reset_index(drop=True)

    except Exception:
        logger.exception("[RANKING DISPLAY] _safe_df failed")
        return pd.DataFrame()


def _first_existing(row: pd.Series, names: Iterable[str], default=None):
    for name in names:
        try:
            if name in row.index:
                val = row.get(name)
                if val is not None:
                    return val
        except Exception:
            pass
    return default


def _first_existing_col(df: pd.DataFrame, names: Iterable[str]) -> Optional[str]:
    try:
        cols = set(df.columns)
        for name in names:
            if name in cols:
                return name
    except Exception:
        pass
    return None


def _to_float(v: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if v is None:
            return default
        if isinstance(v, str) and not v.strip():
            return default
        x = float(v)
        if pd.isna(x):
            return default
        return x
    except Exception:
        return default


def _is_blank_indicator(v: Any, eps: float = 1e-12) -> bool:
    x = _to_float(v, None)
    if x is None:
        return True
    return abs(x) <= eps


def _fmt_ind(v: Any, default: str = "0.00") -> str:
    x = _to_float(v, 0.0)
    if x is None:
        return default
    return f"{x:.2f}"


def _fmt_ind_dash(v: Any, default: str = "-") -> str:
    if _is_blank_indicator(v):
        return default
    x = _to_float(v, None)
    if x is None:
        return default
    return f"{x:.2f}"


def _fmt_score(v: Any, default: str = "0.00") -> str:
    x = _to_float(v, 0.0)
    if x is None:
        return default
    return f"{x:.2f}"


def _fmt_text(v: Any, default: str = "-") -> str:
    try:
        if v is None:
            return default
        s = str(v).strip()
        if not s or s.lower() == "nan":
            return default
        return s
    except Exception:
        return default


def _safe_nonneg_num(v: Any, default: float = 0.0) -> float:
    x = _to_float(v, default)
    if x is None:
        return default
    return max(0.0, abs(float(x)))


def _print_line(s: str = "") -> None:
    try:
        print(s, flush=True)
    except Exception:
        logger.exception("[RANKING DISPLAY] print failed")


# ============================================================
# datetime helpers
# ============================================================

def _strip_tz_keep_wallclock(v: Any):
    """
    timezone付き datetime を UTC変換せず、壁時計時刻を維持して tz だけ外す。

    例:
      2026-04-20 11:03:00+09:00
          -> 2026-04-20 11:03:00
    """
    try:
        if v is None:
            return pd.NaT

        if isinstance(v, str):
            s = v.strip()
            if not s or s.lower() in {"nan", "none", "nat", "<na>", "null"}:
                return pd.NaT
            v = s

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            ts = pd.Timestamp(v)

        if pd.isna(ts):
            return pd.NaT

        if ts.tzinfo is not None:
            try:
                ts = ts.tz_localize(None)
            except Exception:
                try:
                    ts = pd.Timestamp(ts.replace(tzinfo=None))
                except Exception:
                    pass

        return pd.Timestamp(ts)

    except Exception:
        return pd.NaT


def _safe_to_datetime_naive_series(s: Any) -> pd.Series:
    """
    UserWarning: Could not infer format... を出さずに datetime 化する。
    UTC変換はせず、JSTの壁時計時刻を維持する。
    """
    try:
        if s is None:
            return pd.Series(dtype="datetime64[ns]")

        if isinstance(s, pd.DataFrame):
            if s.shape[1] <= 0:
                return pd.Series(dtype="datetime64[ns]")
            s = s.iloc[:, 0]

        if not isinstance(s, pd.Series):
            s = pd.Series(s)

        if pd.api.types.is_datetime64_any_dtype(s) and not pd.api.types.is_datetime64tz_dtype(s):
            out = pd.to_datetime(s, errors="coerce")
            try:
                out = out.dt.tz_localize(None)
            except Exception:
                pass
            return out

        out = s.map(_strip_tz_keep_wallclock)
        out = pd.to_datetime(out, errors="coerce")

        try:
            out = out.dt.tz_localize(None)
        except Exception:
            pass

        return out

    except Exception:
        logger.debug("[RANKING DISPLAY] safe datetime parse failed", exc_info=True)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                return pd.to_datetime(pd.Series(s), errors="coerce")
        except Exception:
            return pd.Series(dtype="datetime64[ns]")


# ============================================================
# numeric / score helpers
# ============================================================

def _numeric_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    try:
        if col not in df.columns:
            return pd.Series(default, index=df.index, dtype="float64")
        return pd.to_numeric(df[col], errors="coerce").fillna(default)
    except Exception:
        return pd.Series(default, index=df.index, dtype="float64")


def _numeric_series_nan(df: pd.DataFrame, col: str) -> pd.Series:
    try:
        if col not in df.columns:
            return pd.Series(np.nan, index=df.index, dtype="float64")
        return pd.to_numeric(df[col], errors="coerce")
    except Exception:
        return pd.Series(np.nan, index=df.index, dtype="float64")


def _nonzero_score_mask(df: pd.DataFrame, cols: list[str], eps: float = 1e-12) -> pd.Series:
    mask = pd.Series(False, index=df.index)

    for c in cols:
        if c in df.columns:
            s = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
            mask = mask | s.abs().gt(eps)

    return mask.fillna(False)


def _positive_score_mask(df: pd.DataFrame, cols: list[str], eps: float = 1e-12) -> pd.Series:
    mask = pd.Series(False, index=df.index)

    for c in cols:
        if c in df.columns:
            s = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
            mask = mask | s.gt(eps)

    return mask.fillna(False)


def _negative_score_mask(df: pd.DataFrame, cols: list[str], eps: float = 1e-12) -> pd.Series:
    mask = pd.Series(False, index=df.index)

    for c in cols:
        if c in df.columns:
            s = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
            mask = mask | s.lt(-eps)

    return mask.fillna(False)


def _coalesce_numeric_cols(df: pd.DataFrame, dst: str, srcs: list[str]) -> pd.DataFrame:
    out = df.copy()

    try:
        base = pd.Series(np.nan, index=out.index, dtype="float64")
        if dst in out.columns:
            base = pd.to_numeric(out[dst], errors="coerce")

        for src in srcs:
            if src not in out.columns:
                continue
            s = pd.to_numeric(out[src], errors="coerce")
            base = base.combine_first(s)

        out[dst] = base
    except Exception:
        logger.debug("[RANKING DISPLAY] coalesce numeric failed dst=%s srcs=%s", dst, srcs, exc_info=True)

    return out


def _score_from_buy_sell(df: pd.DataFrame) -> pd.Series:
    buy = pd.Series(np.nan, index=df.index, dtype="float64")
    sell = pd.Series(np.nan, index=df.index, dtype="float64")

    for c in ("score_buy", "buy_score", "buy"):
        if c in df.columns:
            buy = buy.combine_first(pd.to_numeric(df[c], errors="coerce"))

    for c in ("score_sell", "sell_score", "sell"):
        if c in df.columns:
            sell = sell.combine_first(pd.to_numeric(df[c], errors="coerce"))

    out = buy.combine_first(sell)

    try:
        both = buy.notna() & sell.notna()
        choose_sell = sell.abs() > buy.abs()
        out.loc[both & choose_sell] = sell.loc[both & choose_sell]
        out.loc[both & ~choose_sell] = buy.loc[both & ~choose_sell]
    except Exception:
        pass

    return out


def _ensure_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    表示用 score 系列を補完する。

    score が無い/全NaNでも、score_buy / score_sell / final_score / display_score から復元する。
    """
    out = df.copy()

    try:
        if "score_buy" not in out.columns:
            for c in ("buy_score", "buy"):
                if c in out.columns:
                    out["score_buy"] = pd.to_numeric(out[c], errors="coerce")
                    break
        else:
            out["score_buy"] = pd.to_numeric(out["score_buy"], errors="coerce")
            for c in ("buy_score", "buy"):
                if c in out.columns:
                    out["score_buy"] = out["score_buy"].combine_first(pd.to_numeric(out[c], errors="coerce"))

        if "buy_score" not in out.columns and "score_buy" in out.columns:
            out["buy_score"] = out["score_buy"]

        if "score_sell" not in out.columns:
            for c in ("sell_score", "sell"):
                if c in out.columns:
                    out["score_sell"] = pd.to_numeric(out[c], errors="coerce")
                    break
        else:
            out["score_sell"] = pd.to_numeric(out["score_sell"], errors="coerce")
            for c in ("sell_score", "sell"):
                if c in out.columns:
                    out["score_sell"] = out["score_sell"].combine_first(pd.to_numeric(out[c], errors="coerce"))

        if "sell_score" not in out.columns and "score_sell" in out.columns:
            out["sell_score"] = out["score_sell"]

        fallback_score = _score_from_buy_sell(out)

        if "score" not in out.columns:
            out["score"] = fallback_score
        else:
            out["score"] = pd.to_numeric(out["score"], errors="coerce").combine_first(fallback_score)

        out = _coalesce_numeric_cols(out, "score", ["score", "score_total", "display_score", "final_score", "ranking_score", "combined_score"])
        out["score"] = pd.to_numeric(out["score"], errors="coerce").combine_first(fallback_score)

        if "score_total" not in out.columns:
            out["score_total"] = out["score"]
        else:
            out["score_total"] = pd.to_numeric(out["score_total"], errors="coerce").combine_first(out["score"])

        if "display_score" not in out.columns:
            out["display_score"] = out["score"]
        else:
            out["display_score"] = pd.to_numeric(out["display_score"], errors="coerce").combine_first(out["score"])

        if "final_score" not in out.columns:
            out["final_score"] = out["display_score"]
        else:
            out["final_score"] = pd.to_numeric(out["final_score"], errors="coerce").combine_first(out["display_score"])

    except Exception:
        logger.exception("[RANKING DISPLAY] _ensure_score_columns failed")

    return out


# ============================================================
# dedupe helpers
# ============================================================

def _build_display_strength(df: pd.DataFrame, side: str = "any") -> pd.Series:
    """
    dedupe時に「スコアがある行」を優先して残すための強度。
    """
    strength = pd.Series(0.0, index=df.index, dtype="float64")

    try:
        score = _numeric_series(df, "score", 0.0).abs()
        total = _numeric_series(df, "score_total", 0.0).abs()
        final = _numeric_series(df, "final_score", 0.0).abs()
        display = _numeric_series(df, "display_score", 0.0).abs()
        buy = _numeric_series(df, "score_buy", 0.0).abs()
        sell = _numeric_series(df, "score_sell", 0.0).abs()

        if side == "buy":
            strength += buy * 1000
            strength += score.clip(lower=0) * 100
            strength += final.clip(lower=0) * 50
        elif side == "sell":
            strength += sell * 1000
            strength += score * 10
            strength += final * 5
        else:
            strength += score * 100
            strength += final * 50
            strength += display * 25
            strength += buy * 20
            strength += sell * 20
            strength += total * 10

        for c, w in [
            ("best_rank", 15),
            ("best_rank_value", 15),
            ("rank", 12),
            ("rank_position", 12),
            ("ranking_score", 10),
            ("slope", 5),
            ("slope_atr_scaled", 5),
            ("mtf", 5),
            ("score_mtf", 5),
            ("rsi", 3),
            ("macd", 3),
            ("signal", 2),
            ("hist", 2),
            ("close", 2),
        ]:
            if c in df.columns:
                s = pd.to_numeric(df[c], errors="coerce")
                strength += s.notna().astype(float) * w
                strength += s.fillna(0).abs().clip(upper=9999) * 0.0001

        if "symbolname" in df.columns:
            name_ok = df["symbolname"].notna() & df["symbolname"].astype(str).str.strip().ne("")
            strength += name_ok.astype(float) * 10

    except Exception:
        logger.debug("[RANKING DISPLAY] display strength failed side=%s", side, exc_info=True)

    return strength.fillna(0.0)


def _dedupe_symbol_rows(df: pd.DataFrame, *, side: str = "any") -> pd.DataFrame:
    df = _safe_df(df)
    if df.empty or "symbol" not in df.columns:
        return df

    try:
        out = df.copy()
        out = _ensure_score_columns(out)
        out["symbol"] = out["symbol"].astype(str).fillna("").str.strip()
        out = out.loc[out["symbol"].ne("")].copy()

        if "datetime" in out.columns:
            out["datetime"] = _safe_to_datetime_naive_series(out["datetime"])

        out["_display_strength"] = _build_display_strength(out, side=side)

        sort_cols = ["symbol", "_display_strength"]
        ascending = [True, False]

        if "datetime" in out.columns:
            sort_cols.append("datetime")
            ascending.append(False)

        out = out.sort_values(sort_cols, ascending=ascending, kind="stable")

        before = len(out)
        out = out.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)
        removed = before - len(out)

        if removed > 0:
            logger.info(
                "[RANKING DISPLAY] dedupe by symbol removed=%s remaining=%s symbols=%s side=%s nonzero=%s",
                removed,
                len(out),
                out["symbol"].nunique(),
                side,
                int(_nonzero_score_mask(
                    out,
                    ["score", "score_total", "display_score", "final_score", "score_buy", "score_sell", "buy_score", "sell_score"],
                ).sum()),
            )

        return out.drop(columns=["_display_strength"], errors="ignore")

    except Exception:
        logger.exception("[RANKING DISPLAY] _dedupe_symbol_rows failed")
        return df


# ============================================================
# normalization
# ============================================================

def _normalize_display_df(df: pd.DataFrame) -> pd.DataFrame:
    df = _safe_df(df)
    if df.empty:
        return df

    try:
        if "symbol" not in df.columns:
            for c in ("code", "ticker", "stock_code", "Symbol"):
                if c in df.columns:
                    df["symbol"] = df[c]
                    break

        if "symbol" in df.columns:
            df["symbol"] = df["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)

        if "symbolname" not in df.columns:
            for c in ("name", "company_name", "SymbolName", "銘柄名"):
                if c in df.columns:
                    df["symbolname"] = df[c]
                    break
            if "symbolname" not in df.columns:
                df["symbolname"] = ""

        if "name" not in df.columns:
            df["name"] = df["symbolname"] if "symbolname" in df.columns else ""

        if "close" not in df.columns:
            for c in ("close_price", "current_price", "price", "last_price", "CurrentPrice", "LastPrice"):
                if c in df.columns:
                    df["close"] = df[c]
                    break

        # score / buy / sell 補完
        df = _ensure_score_columns(df)

        # best_rank / ranking related aliases
        if "best_rank" not in df.columns:
            for c in (
                "best_rank_value",
                "rank_position",
                "rank",
                "best_position",
                "ranking_rank",
                "ranking_position",
                "Rank",
            ):
                if c in df.columns:
                    df["best_rank"] = df[c]
                    break

        if "ranking_score" not in df.columns:
            for c in ("rank_score", "ranking_points", "best_rank_score"):
                if c in df.columns:
                    df["ranking_score"] = df[c]
                    break

        if "mtf" not in df.columns:
            for c in ("mtf_alignment", "score_mtf", "mtf_score"):
                if c in df.columns:
                    df["mtf"] = df[c]
                    break

        if "slope" not in df.columns:
            for c in ("slope_atr_scaled", "ma75_slope", "score_slope"):
                if c in df.columns:
                    df["slope"] = df[c]
                    break

        if "symbolname" in df.columns:
            sname = df["symbolname"].astype(str).fillna("").str.strip()
            if "name" in df.columns:
                n = df["name"].astype(str).fillna("").str.strip()
                sym = df["symbol"].astype(str).fillna("").str.strip() if "symbol" in df.columns else ""
                fill_mask = sname.eq("") | sname.eq("nan")
                if isinstance(sym, pd.Series):
                    fill_mask = fill_mask | sname.eq(sym)
                try:
                    df.loc[fill_mask, "symbolname"] = n.loc[fill_mask]
                except Exception:
                    pass

        if "datetime" in df.columns:
            df["datetime"] = _safe_to_datetime_naive_series(df["datetime"])

        return df

    except Exception:
        logger.exception("[RANKING DISPLAY] _normalize_display_df failed")
        return df


# ============================================================
# session-anchor filtering
# ============================================================

def _filter_df_for_display_slot(
    df: pd.DataFrame,
    *,
    interval: int,
    now=None,
) -> tuple[pd.DataFrame, Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    df = _normalize_display_df(df)
    if df.empty or "datetime" not in df.columns:
        return df, None, None

    try:
        _, slot_dt = resolve_display_slot(interval=interval, now=now)
        slot_ts = pd.Timestamp(slot_dt)

        x = df.copy()
        x["datetime"] = _safe_to_datetime_naive_series(x["datetime"])
        x = x.dropna(subset=["datetime"]).copy()
        if x.empty:
            return x, slot_ts, None

        x = x.loc[x["datetime"] <= slot_ts].copy()
        if x.empty:
            return x, slot_ts, None

        latest_dt = x["datetime"].max()
        x = x.loc[x["datetime"] == latest_dt].copy()

        logger.info(
            "[RANKING DISPLAY] display slot filtered interval=%s slot_dt=%s latest_dt=%s rows=%d symbols=%s nonzero_score=%s",
            interval,
            slot_dt,
            latest_dt,
            len(x),
            x["symbol"].astype(str).nunique() if "symbol" in x.columns else 0,
            int(_nonzero_score_mask(
                x,
                ["score", "score_total", "display_score", "final_score", "score_buy", "score_sell", "buy_score", "sell_score"],
            ).sum()),
        )
        return x.reset_index(drop=True), slot_ts, latest_dt

    except Exception:
        logger.exception("[RANKING DISPLAY] _filter_df_for_display_slot failed interval=%s", interval)
        return df, None, None


# ============================================================
# detail pickers / row builders
# ============================================================

def _pick_extended_detail_map(row: pd.Series) -> dict[str, Any]:
    return {
        "symbol": _first_existing(row, ["symbol", "code", "ticker"], ""),
        "symbolname": _first_existing(row, ["symbolname", "name", "company_name"], ""),
        "score": _first_existing(row, ["score", "score_total", "display_score", "final_score", "ranking_score"]),
        "score_total": _first_existing(row, ["score_total", "score", "display_score", "final_score", "ranking_score"]),
        "final_score": _first_existing(row, ["final_score", "display_score", "score_total", "score", "ranking_score"]),
        "score_buy": _first_existing(row, ["score_buy", "buy_score", "buy"]),
        "score_sell": _first_existing(row, ["score_sell", "sell_score", "sell"]),
        "best_rank": _first_existing(
            row,
            [
                "best_rank",
                "best_rank_value",
                "rank_position",
                "rank",
                "best_position",
                "ranking_rank",
                "ranking_position",
                "Rank",
            ],
        ),
        "hist": _first_existing(row, ["hist", "macd_hist"]),
        "rsi": _first_existing(row, ["rsi", "RSI"]),
        "macd": _first_existing(row, ["macd", "MACD"]),
        "signal": _first_existing(row, ["signal", "macd_signal"]),
        "slope": _first_existing(row, ["slope", "slope_atr_scaled", "ma75_slope", "score_slope"]),
        "mtf": _first_existing(row, ["mtf", "mtf_alignment", "score_mtf", "mtf_score"]),
        "base": _first_existing(row, ["score_base", "_score_base", "base"]),
        "trend": _first_existing(row, ["score_trend", "_score_trend", "trend"]),
        "mom": _first_existing(row, ["score_momentum", "_score_momentum", "mom", "momentum"]),
        "vel": _first_existing(row, ["score_velocity", "_score_velocity", "vel", "velocity"]),
        "pen": _first_existing(row, ["direction_penalty", "direction_penalty_score", "penalty", "penalty_score", "pen"]),
    }


def _build_common_lines(rank_no: int, row: pd.Series, side_icon: str) -> list[str]:
    d = _pick_extended_detail_map(row)

    symbol = _fmt_text(d.get("symbol"), "-")
    symbolname = _fmt_text(d.get("symbolname"), "-")

    score = _safe_nonneg_num(d.get("score"), 0.0)
    buy = _safe_nonneg_num(d.get("score_buy"), 0.0)
    sell = _safe_nonneg_num(d.get("score_sell"), 0.0)
    slope = d.get("slope")
    mtf = d.get("mtf")
    total = _safe_nonneg_num(d.get("score_total"), score)
    final = _safe_nonneg_num(d.get("final_score"), total)
    rsi = d.get("rsi")
    macd = d.get("macd")
    best_rank = d.get("best_rank")

    base = _safe_nonneg_num(d.get("base"), 0.0)
    trend = _safe_nonneg_num(d.get("trend"), 0.0)
    mom = _safe_nonneg_num(d.get("mom"), 0.0)
    vel = _safe_nonneg_num(d.get("vel"), 0.0)
    pen = _safe_nonneg_num(d.get("pen"), 0.0)
    hist = d.get("hist")
    signal = d.get("signal")

    line1 = (
        f"{rank_no:>2}. {side_icon} {symbol:<6} {symbolname:<28} "
        f"score={_fmt_score(score):>6} buy={_fmt_score(buy):>6} sell={_fmt_score(sell):>6} "
        f"slope={_fmt_ind_dash(slope):>6} mtf={_fmt_ind_dash(mtf):>6} "
        f"total={_fmt_score(total):>6} final={_fmt_score(final):>6} "
        f"rsi={_fmt_ind_dash(rsi):>6} macd={_fmt_ind_dash(macd):>6}"
    )

    line2 = (
        f"    best_rank={_fmt_ind_dash(best_rank):>6} "
        f"base={_fmt_ind(base)} trend={_fmt_ind(trend)} "
        f"mom={_fmt_ind(mom)} vel={_fmt_ind(vel)} "
        f"pen={_fmt_ind(pen)} hist={_fmt_ind_dash(hist)} signal={_fmt_ind_dash(signal)}"
    )

    return [line1, line2]


def _build_buy_line(rank_no: int, row: pd.Series) -> str:
    return "\n".join(_build_common_lines(rank_no, row, "⚪"))


def _build_sell_line(rank_no: int, row: pd.Series) -> str:
    return "\n".join(_build_common_lines(rank_no, row, "🔴"))


# ============================================================
# sorting
# ============================================================

def _coerce_numeric_col(df: pd.DataFrame, col: str) -> pd.DataFrame:
    try:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    except Exception:
        logger.exception("[RANKING DISPLAY] numeric coercion failed: %s", col)
    return df


def _coerce_display_numeric_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for col in [
        "score",
        "score_total",
        "display_score",
        "final_score",
        "score_buy",
        "score_sell",
        "buy_score",
        "sell_score",
        "ranking_score",
        "rsi",
        "macd",
        "signal",
        "hist",
        "slope",
        "slope_atr_scaled",
        "mtf",
        "score_mtf",
        "mtf_score",
        "best_rank",
        "best_rank_value",
        "rank",
        "rank_position",
        "score_base",
        "score_trend",
        "score_momentum",
        "score_velocity",
        "direction_penalty",
    ]:
        out = _coerce_numeric_col(out, col)
    return out


def _prepare_buy_df(df: pd.DataFrame) -> pd.DataFrame:
    df = _normalize_display_df(df)
    if df.empty:
        return df

    df = _coerce_display_numeric_columns(df)
    df = _ensure_score_columns(df)

    # BUY候補: buy系または正のscore系がある行だけ
    buy_mask = _positive_score_mask(
        df,
        ["score_buy", "buy_score", "score", "score_total", "final_score", "display_score", "ranking_score"],
    )

    before = len(df)
    df = df.loc[buy_mask].copy()
    removed_zero = before - len(df)

    logger.info(
        "[RANKING DISPLAY] buy nonzero filter before=%s after=%s removed_zero=%s",
        before,
        len(df),
        removed_zero,
    )

    if df.empty:
        return df

    df["score_buy"] = _numeric_series(df, "score_buy", 0.0).clip(lower=0.0)
    df["_buy_sort_score"] = (
        _numeric_series(df, "score_buy", 0.0) * 1000
        + _numeric_series(df, "score", 0.0).clip(lower=0.0) * 100
        + _numeric_series(df, "final_score", 0.0).clip(lower=0.0) * 50
        + _numeric_series(df, "display_score", 0.0).clip(lower=0.0) * 25
        + _numeric_series(df, "ranking_score", 0.0).abs() * 5
    )

    sort_cols = ["_buy_sort_score", "score_buy", "score", "final_score"]
    ascending = [False, False, False, False]

    if "datetime" in df.columns:
        df["datetime"] = _safe_to_datetime_naive_series(df["datetime"])
        sort_cols.append("datetime")
        ascending.append(False)

    df = df.sort_values(sort_cols, ascending=ascending, na_position="last", kind="stable")
    df = _dedupe_symbol_rows(df, side="buy")
    df = df.drop(columns=["_buy_sort_score"], errors="ignore")

    return df.reset_index(drop=True)


def _prepare_sell_df(df: pd.DataFrame) -> pd.DataFrame:
    df = _normalize_display_df(df)
    if df.empty:
        return df

    df = _coerce_display_numeric_columns(df)
    df = _ensure_score_columns(df)

    # SELL候補:
    #   sell系が正
    #   または score/final/display が負
    sell_mask = _positive_score_mask(df, ["score_sell", "sell_score"])
    sell_mask = sell_mask | _negative_score_mask(df, ["score", "score_total", "final_score", "display_score"])

    before = len(df)
    df = df.loc[sell_mask].copy()
    removed_zero = before - len(df)

    logger.info(
        "[RANKING DISPLAY] sell nonzero filter before=%s after=%s removed_zero=%s",
        before,
        len(df),
        removed_zero,
    )

    if df.empty:
        return df

    sell_src = _numeric_series(df, "score_sell", 0.0)
    df["score_sell_display"] = sell_src.abs().clip(lower=0.0)

    buy_src = _numeric_series(df, "score_buy", 0.0)
    df["score_buy_display"] = buy_src.clip(lower=0.0)

    # SELLは売り圧が強い順。
    # scoreが負ならより上位へ。
    df["_sell_sort_score"] = (
        df["score_sell_display"] * 1000
        + _numeric_series(df, "score", 0.0).clip(upper=0.0).abs() * 100
        + _numeric_series(df, "final_score", 0.0).clip(upper=0.0).abs() * 50
        + _numeric_series(df, "display_score", 0.0).clip(upper=0.0).abs() * 25
        + _numeric_series(df, "ranking_score", 0.0).abs() * 5
    )

    sort_cols = ["_sell_sort_score", "score_sell_display"]
    ascending = [False, False]

    if "score" in df.columns:
        sort_cols.append("score")
        ascending.append(True)
    elif "score_total" in df.columns:
        sort_cols.append("score_total")
        ascending.append(True)

    if "datetime" in df.columns:
        df["datetime"] = _safe_to_datetime_naive_series(df["datetime"])
        sort_cols.append("datetime")
        ascending.append(False)

    df = df.sort_values(sort_cols, ascending=ascending, na_position="last", kind="stable")
    df = _dedupe_symbol_rows(df, side="sell")
    df = df.drop(columns=["_sell_sort_score"], errors="ignore")

    return df.reset_index(drop=True)


# ============================================================
# header helper
# ============================================================

def _latest_header_text(
    df: pd.DataFrame,
    title_label: str,
    *,
    slot_dt: Optional[pd.Timestamp] = None,
    latest_dt: Optional[pd.Timestamp] = None,
) -> str | None:
    try:
        view_dt = latest_dt

        if view_dt is None and df is not None and not df.empty:
            dt_col = None
            for c in ("datetime", "end_time", "start_time"):
                if c in df.columns:
                    dt_col = c
                    break
            if dt_col is not None:
                s = _safe_to_datetime_naive_series(df[dt_col]).dropna()
                if not s.empty:
                    view_dt = s.max()

        if view_dt is None and slot_dt is None:
            return None

        if slot_dt is not None and view_dt is not None:
            return f"=== ⏱ 表示 RANKING {title_label}｜slot={slot_dt} / data={view_dt} ==="

        if view_dt is not None:
            return f"=== ⏱ 表示 RANKING {title_label}｜{view_dt} ==="

        return f"=== ⏱ 表示 RANKING {title_label}｜slot={slot_dt} ==="

    except Exception:
        logger.debug("[RANKING DISPLAY] latest header build failed", exc_info=True)
        return None


# ============================================================
# public printers / wrappers
# ============================================================

def print_ranking_summary_top10(summary_df: pd.DataFrame, interval_label: str = "1min", now=None) -> None:
    try:
        df = _safe_df(summary_df)

        try:
            interval = int(str(interval_label).replace("min", "").strip())
        except Exception:
            interval = 1

        df_view, slot_dt, latest_dt = _filter_df_for_display_slot(df, interval=interval, now=now)

        logger.info(
            "[RANKING DISPLAY] interval=%s input_rows=%s view_rows=%s slot_dt=%s latest_dt=%s nonzero=%s",
            interval,
            len(df) if isinstance(df, pd.DataFrame) else 0,
            len(df_view) if isinstance(df_view, pd.DataFrame) else 0,
            slot_dt,
            latest_dt,
            int(_nonzero_score_mask(
                df_view,
                ["score", "score_total", "display_score", "final_score", "score_buy", "score_sell", "buy_score", "sell_score", "ranking_score"],
            ).sum()) if isinstance(df_view, pd.DataFrame) and not df_view.empty else 0,
        )

        header = _latest_header_text(
            df_view,
            f"{interval_label} サマリー",
            slot_dt=slot_dt,
            latest_dt=latest_dt,
        )
        if header:
            _print_line("")
            _print_line(header)

        _print_line("")
        _print_line(f"========== 📊 RANKING SUMMARY TOP10 ({interval_label}) ==========")
        _print_line("🔵 BUY TOP10（score / buy / sell / slope / mtf / total / final / rsi / macd）")

        buy_df = _prepare_buy_df(df_view)
        if buy_df.empty:
            _print_line(" (no buy candidates: nonzero score/buy/final not found)")
        else:
            for i, (_, row) in enumerate(buy_df.head(10).iterrows(), start=1):
                _print_line(_build_buy_line(i, row))

        _print_line("🔴 SELL TOP10（下落圧が強い）")

        sell_df = _prepare_sell_df(df_view)
        if sell_df.empty:
            _print_line(" (no sell candidates: nonzero sell or negative score not found)")
        else:
            for i, (_, row) in enumerate(sell_df.head(10).iterrows(), start=1):
                _print_line(_build_sell_line(i, row))

    except Exception:
        logger.exception("[RANKING DISPLAY] print_ranking_summary_top10 failed")


def display_ranking_summary(summary_df: pd.DataFrame | None = None, interval_label: str = "1min", **kwargs) -> None:
    if summary_df is None:
        return
    if "interval" in kwargs and (interval_label == "1min" or not interval_label):
        try:
            interval_label = f"{int(kwargs['interval'])}min"
        except Exception:
            pass
    print_ranking_summary_top10(summary_df=summary_df, interval_label=interval_label, now=kwargs.get("now"))


__all__ = [
    "print_ranking_summary_top10",
    "display_ranking_summary",
]

