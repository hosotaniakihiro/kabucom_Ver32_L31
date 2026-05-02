# ============================================================
# File   : scheduler_jobs/push_summary/display.py
# Version: Ver31_L24-PUSH-SUMMARY-DISPLAY-INTEGRITY-GUARD
# ------------------------------------------------------------
# 機能:
#   - PUSH由来サマリーの専用表示
#   - PUSH専用キャッシュからDataFrame取得
#   - SUMMARY TOP10 をログへ出力
#   - 株価は小数点第1位、指標は小数点第2位で整形
#   - symbolname/name の重複を避けて表示
#   - 表示前に symbol ごと 1行へ重複除去
#   - 未完成DF / 材料DF の表示混入を抑止
#   - MTF不整合を表示前に補正
#
# 目的:
#   - ランキング由来表示と完全分離
#   - PUSH由来サマリーが「計算されたのに表示されない」問題の
#     切り分けを容易にする
#   - 材料DF混入による NaN / 0 / 重複 / 名前欠落を防ぐ
#
# 主な関数:
#   - display_push_summary(interval=1, top_n=10)
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable

import pandas as pd

from global_state import global_data
from trading.push_summary.cache import (
    get_push_summary,
    get_push_summary_latest_dt,
)

logger = logging.getLogger(__name__)


def _safe_df(df) -> pd.DataFrame:
    try:
        if isinstance(df, pd.DataFrame):
            return df.copy()
        return pd.DataFrame()
    except Exception:
        logger.exception("[PUSH DISPLAY] _safe_df failed")
        return pd.DataFrame()


def _normalize_symbol_value(v) -> str:
    try:
        if pd.isna(v):
            return ""
        s = str(v).strip()
        if not s:
            return ""
        if s.endswith(".0"):
            s2 = s[:-2]
            if s2.isdigit():
                return s2
        return s
    except Exception:
        return ""


def _pick_name(row: pd.Series) -> str:
    for col in ("symbolname_view", "symbolname", "name"):
        v = row.get(col)
        if pd.notna(v) and str(v).strip():
            return str(v).strip()
    symbol = str(row.get("symbol", "")).strip()
    return symbol if symbol else "-"


def _fmt_price(v) -> str:
    try:
        if pd.isna(v):
            return "-"
        return f"{float(v):,.1f}"
    except Exception:
        return "-"


def _fmt_metric(v) -> str:
    try:
        if pd.isna(v):
            return "-"
        return f"{float(v):,.2f}"
    except Exception:
        return "-"


def _find_col(df: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    cols = set(df.columns)
    for c in candidates:
        if c in cols:
            return c
    return None


def _pick_numeric_series(df: pd.DataFrame, candidates: Iterable[str], default=0.0) -> pd.Series:
    for c in candidates:
        if c in df.columns:
            try:
                s = pd.to_numeric(df[c], errors="coerce")
                s = s.replace([float("inf"), float("-inf")], default)
                return s.fillna(default)
            except Exception:
                pass
    return pd.Series(default, index=df.index, dtype="float64")


def _pick_numeric_series_nan(df: pd.DataFrame, candidates: Iterable[str]) -> pd.Series:
    for c in candidates:
        if c in df.columns:
            try:
                s = pd.to_numeric(df[c], errors="coerce")
                s = s.replace([float("inf"), float("-inf")], float("nan"))
                return s
            except Exception:
                pass
    return pd.Series(float("nan"), index=df.index, dtype="float64")


def _pick_text_series(df: pd.DataFrame, candidates: Iterable[str], default="") -> pd.Series:
    for c in candidates:
        if c in df.columns:
            try:
                return df[c].fillna(default).astype(str)
            except Exception:
                pass
    return pd.Series(default, index=df.index, dtype="object")


def _resolve_symbolname_series(df: pd.DataFrame) -> pd.Series:
    symbol_s = _pick_text_series(df, ["symbol"], default="").astype(str).str.strip()
    symbolname_s = _pick_text_series(df, ["symbolname"], default="").astype(str).str.strip()
    name_s = _pick_text_series(df, ["name"], default="").astype(str).str.strip()

    out = symbolname_s.copy()
    out = out.mask(out.eq(""), name_s)

    try:
        mp = getattr(global_data, "symbol_name_map", {})
        if isinstance(mp, dict) and mp:
            mapped = symbol_s.map(lambda x: str(mp.get(str(x).strip(), "")).strip())
            out = out.mask(out.eq(""), mapped)
    except Exception:
        pass

    out = out.mask(out.eq(""), symbol_s)
    out = out.fillna("").astype(str).str.strip()
    out = out.mask(out.eq(""), symbol_s)
    return out


def _is_completed_summary_df(df: pd.DataFrame) -> bool:
    try:
        if df is None or df.empty:
            return False

        if "symbol" not in df.columns:
            return False

        symbol_s = df["symbol"].fillna("").astype(str).str.strip()
        if symbol_s.eq("").all():
            return False

        if "score" not in df.columns:
            score_alt = _find_col(df, ["score_total", "total_score", "final_score", "display_score"])
            if not score_alt:
                return False
        score_s = _pick_numeric_series_nan(df, ["score", "score_total", "total_score", "final_score", "display_score"])
        if score_s.notna().sum() == 0:
            return False

        buy_s = _pick_numeric_series_nan(df, ["score_buy", "buy_score", "buy"])
        sell_s = _pick_numeric_series_nan(df, ["score_sell", "sell_score", "sell"])
        if buy_s.notna().sum() == 0 and sell_s.notna().sum() == 0:
            return False

        return True
    except Exception:
        return False


def _repair_mtf_consistency(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty:
        return out

    try:
        mtf_s = _pick_numeric_series_nan(out, ["mtf", "mtf_alignment"])
        score_mtf_s = _pick_numeric_series_nan(out, ["score_mtf", "mtf_score"])
        final_s = _pick_numeric_series_nan(out, ["final", "final_score", "display_score"])
        total_s = _pick_numeric_series_nan(out, ["total", "score_total", "score", "display_score"])

        bad_mask = mtf_s.fillna(0).eq(0)

        if "score_mtf" in out.columns:
            mask = bad_mask & score_mtf_s.fillna(0).gt(0)
            if mask.any():
                out.loc[mask, "score_mtf"] = 0.0

        if "mtf_score" in out.columns:
            mask = bad_mask & score_mtf_s.fillna(0).gt(0)
            if mask.any():
                out.loc[mask, "mtf_score"] = 0.0

        if "final" in out.columns:
            mask = bad_mask & final_s.fillna(0).gt(0) & total_s.notna()
            if mask.any():
                out.loc[mask, "final"] = total_s[mask]

        if "final_score" in out.columns:
            mask = bad_mask & final_s.fillna(0).gt(0) & total_s.notna()
            if mask.any():
                out.loc[mask, "final_score"] = total_s[mask]

        return out

    except Exception:
        logger.exception("[PUSH DISPLAY] _repair_mtf_consistency failed")
        return df


def _dedupe_symbol_rows(df: pd.DataFrame) -> pd.DataFrame:
    """
    表示前の symbol 単位重複除去。
    同一銘柄が複数行ある場合は、完成度と新しさで1行に絞る。
    """
    out = _safe_df(df)
    if out.empty or "symbol" not in out.columns:
        return out

    try:
        out["symbol"] = out["symbol"].map(_normalize_symbol_value)
        out = out[out["symbol"] != ""].copy()
        if out.empty:
            return out

        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            try:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
            except Exception:
                pass

        complete_score = pd.Series(0, index=out.index, dtype="int64")

        if "symbolname_view" in out.columns:
            name_ok = out["symbolname_view"].fillna("").astype(str).str.strip().ne("")
            complete_score += name_ok.astype(int) * 10
        elif "symbolname" in out.columns:
            name_ok = out["symbolname"].fillna("").astype(str).str.strip().ne("")
            complete_score += name_ok.astype(int) * 10

        for c, w in [
            ("score", 8),
            ("buy_score", 6),
            ("sell_score", 6),
            ("buy", 6),
            ("sell", 6),
            ("total", 5),
            ("final", 5),
            ("final_score", 5),
            ("mtf", 4),
            ("score_mtf", 4),
            ("slope", 4),
            ("rsi", 3),
            ("macd", 3),
            ("signal", 3),
            ("close", 1),
        ]:
            if c in out.columns:
                s = pd.to_numeric(out[c], errors="coerce")
                complete_score += s.notna().astype(int) * w

        out["_complete_score"] = complete_score

        sort_cols = ["symbol", "_complete_score"]
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
                "[PUSH DISPLAY] dedupe by symbol removed=%s remaining=%s symbols=%s",
                removed,
                len(out),
                out["symbol"].nunique(),
            )

        return out.drop(columns=["_complete_score"], errors="ignore")

    except Exception:
        logger.exception("[PUSH DISPLAY] _dedupe_symbol_rows failed")
        return df


def _prepare_display_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty:
        return out

    try:
        out = _repair_mtf_consistency(out)

        if "symbol" not in out.columns:
            return pd.DataFrame()

        out["symbol"] = out["symbol"].map(_normalize_symbol_value)
        out = out[out["symbol"] != ""].copy()
        if out.empty:
            return pd.DataFrame()

        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            try:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
            except Exception:
                pass

        out["symbolname_view"] = _resolve_symbolname_series(out)

        # score列候補
        score_col = _find_col(out, ["score", "score_total", "total_score", "final_score", "display_score"])
        if score_col and score_col != "score":
            out["score"] = pd.to_numeric(out[score_col], errors="coerce")

        # buy/sell候補
        buy_col = _find_col(out, ["score_buy", "buy_score", "buy"])
        sell_col = _find_col(out, ["score_sell", "sell_score", "sell"])
        total_col = _find_col(out, ["total", "score_total", "combined_score", "display_score", "score"])
        final_col = _find_col(out, ["final", "final_score", "display_score", "score_total", "score"])

        if buy_col and buy_col != "buy_score":
            out["buy_score"] = pd.to_numeric(out[buy_col], errors="coerce")
        elif "buy_score" in out.columns:
            out["buy_score"] = pd.to_numeric(out["buy_score"], errors="coerce")

        if sell_col and sell_col != "sell_score":
            out["sell_score"] = pd.to_numeric(out[sell_col], errors="coerce")
        elif "sell_score" in out.columns:
            out["sell_score"] = pd.to_numeric(out["sell_score"], errors="coerce")

        if total_col and total_col != "total":
            out["total"] = pd.to_numeric(out[total_col], errors="coerce")
        elif "total" in out.columns:
            out["total"] = pd.to_numeric(out["total"], errors="coerce")

        if final_col and final_col != "final":
            out["final"] = pd.to_numeric(out[final_col], errors="coerce")
        elif "final" in out.columns:
            out["final"] = pd.to_numeric(out["final"], errors="coerce")

        # completed summary 相当の最低要件
        if not _is_completed_summary_df(out):
            logger.warning("[PUSH DISPLAY] reject incomplete df rows=%s cols=%s", len(out), len(out.columns))
            return pd.DataFrame()

        # ソート前に 1銘柄1行へ
        out = _dedupe_symbol_rows(out)
        if out.empty:
            return pd.DataFrame()

        # BUY優先の全体ソート
        if "score" in out.columns:
            sort_cols = ["score"]
            ascending = [False]
        elif "buy_score" in out.columns:
            sort_cols = ["buy_score"]
            ascending = [False]
        else:
            sort_cols = ["symbol"]
            ascending = [True]

        if "datetime" in out.columns:
            sort_cols.append("datetime")
            ascending.append(False)

        out = out.sort_values(sort_cols, ascending=ascending, na_position="last", kind="stable")
        return out.reset_index(drop=True)

    except Exception:
        logger.exception("[PUSH DISPLAY] _prepare_display_df failed")
        return pd.DataFrame()


def display_push_summary(interval: int | str = 1, top_n: int = 10) -> None:
    """
    PUSH由来サマリーのTOP表示
    """
    try:
        df = get_push_summary(interval)
        df = _prepare_display_df(df)

        latest_dt = get_push_summary_latest_dt(interval)
        interval_label = f"{int(str(interval).replace('min', ''))}min"

        logger.info("")
        logger.info("=== ⏱ 最新 %s サマリー｜%s ===", interval_label, latest_dt if latest_dt else "-")
        logger.info("")
        logger.info("========== 📊 SUMMARY TOP10 (%s) ==========", interval_label)
        logger.info("🔵 BUY TOP10（score / buy / sell / slope / mtf / total / final / rsi / macd）")

        if df.empty:
            logger.info(" (no completed buy candidates)")
            logger.info("🔴 SELL TOP10（下落圧が強い）")
            logger.info(" (no completed sell candidates)")
            return

        buy_df = df.copy()
        if "buy_score" in buy_df.columns:
            buy_df = buy_df.sort_values(
                ["buy_score", "score", "mtf", "slope"] if {"buy_score", "score", "mtf", "slope"}.issubset(buy_df.columns) else ["buy_score"],
                ascending=False if "buy_score" in buy_df.columns else True,
                na_position="last",
                kind="stable",
            )
        buy_df = buy_df.head(top_n).copy()

        for i, (_, row) in enumerate(buy_df.iterrows(), start=1):
            symbol = str(row.get("symbol", "-"))
            name = _pick_name(row)

            close_v = row.get("close", row.get("close_price"))
            score_v = row.get("score")
            buy_v = row.get("buy_score", row.get("buy"))
            sell_v = row.get("sell_score", row.get("sell"))
            slope_v = row.get("slope", row.get("slope_atr_scaled"))
            mtf_v = row.get("mtf", row.get("score_mtf"))
            total_v = row.get("total", row.get("score_total"))
            final_v = row.get("final", row.get("final_score"))
            rsi_v = row.get("rsi")
            macd_v = row.get("macd")

            line = (
                f"{i:>2}. ⚪ {symbol:<6} {name:<30} "
                f"close={_fmt_price(close_v):>8} "
                f"score={_fmt_metric(score_v):>7} "
                f"buy={_fmt_metric(buy_v):>7} "
                f"sell={_fmt_metric(sell_v):>7} "
                f"slope={_fmt_metric(slope_v):>7} "
                f"mtf={_fmt_metric(mtf_v):>7} "
                f"total={_fmt_metric(total_v):>7} "
                f"final={_fmt_metric(final_v):>7} "
                f"rsi={_fmt_metric(rsi_v):>7} "
                f"macd={_fmt_metric(macd_v):>7}"
            )
            logger.info(line)

        logger.info("🔴 SELL TOP10（下落圧が強い）")

        sell_col = _find_col(df, ["sell_score", "sell"])
        if not sell_col:
            logger.info(" (no completed sell candidates)")
            return

        sell_df = df.copy()
        sell_df[sell_col] = pd.to_numeric(sell_df[sell_col], errors="coerce").fillna(0.0).abs()

        # SELL側は未計算テクニカルの優先度を下げる
        tech_quality = pd.Series(0, index=sell_df.index, dtype="int64")
        for col in ("rsi", "macd", "signal"):
            if col in sell_df.columns:
                s = pd.to_numeric(sell_df[col], errors="coerce")
                tech_quality += s.notna().astype(int) * 2

        if {"rsi", "macd", "signal"}.issubset(sell_df.columns):
            rsi0 = pd.to_numeric(sell_df["rsi"], errors="coerce").fillna(0).eq(0)
            macd0 = pd.to_numeric(sell_df["macd"], errors="coerce").fillna(0).eq(0)
            signal0 = pd.to_numeric(sell_df["signal"], errors="coerce").fillna(0).eq(0)
            all_zero_tech = rsi0 & macd0 & signal0
            tech_quality -= all_zero_tech.astype(int) * 3

        sell_df["_tech_quality"] = tech_quality

        sort_cols = [sell_col, "_tech_quality"]
        ascending = [False, False]
        if "datetime" in sell_df.columns:
            sell_df["datetime"] = pd.to_datetime(sell_df["datetime"], errors="coerce")
            sort_cols.append("datetime")
            ascending.append(False)

        sell_df = sell_df.sort_values(sort_cols, ascending=ascending, na_position="last", kind="stable")
        sell_df = _dedupe_symbol_rows(sell_df).head(top_n)

        if sell_df.empty:
            logger.info(" (no completed sell candidates)")
            return

        for i, (_, row) in enumerate(sell_df.iterrows(), start=1):
            symbol = str(row.get("symbol", "-"))
            name = _pick_name(row)

            close_v = row.get("close", row.get("close_price"))
            score_v = row.get("score")
            buy_v = row.get("buy_score", row.get("buy"))
            sell_v = row.get("sell_score", row.get("sell"))
            slope_v = row.get("slope", row.get("slope_atr_scaled"))
            mtf_v = row.get("mtf", row.get("score_mtf"))
            total_v = row.get("total", row.get("score_total"))
            final_v = row.get("final", row.get("final_score"))
            rsi_v = row.get("rsi")
            macd_v = row.get("macd")

            line = (
                f"{i:>2}. 🔴 {symbol:<6} {name:<30} "
                f"close={_fmt_price(close_v):>8} "
                f"score={_fmt_metric(score_v):>7} "
                f"buy={_fmt_metric(buy_v):>7} "
                f"sell={_fmt_metric(sell_v):>7} "
                f"slope={_fmt_metric(slope_v):>7} "
                f"mtf={_fmt_metric(mtf_v):>7} "
                f"total={_fmt_metric(total_v):>7} "
                f"final={_fmt_metric(final_v):>7} "
                f"rsi={_fmt_metric(rsi_v):>7} "
                f"macd={_fmt_metric(macd_v):>7}"
            )
            logger.info(line)

    except Exception:
        logger.exception("[PUSH DISPLAY] display_push_summary failed interval=%r", interval)