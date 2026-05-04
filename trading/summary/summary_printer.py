# ============================================================
# File   : trading/summary/summary_printer.py
# Function:
#   - completed summary のみを安全に取得して表示する
#   - push -> legacy -> ranking の順で completed summary を明示取得する
#   - symbolname を再補完し、1銘柄1行へ正規化して表示品質を安定させる
#   - 最新バー表示と BUY TOP10 / SELL TOP10 表示を行う
#   - 可能であれば trading.summary.top_candidates.prepare_buy_sell_top_df()
#     を使い、エントリー候補と同じ母集団の TOP10 を表示する
#   - score / score_total / final / buy-sell / OHLC / technical 指標の列揺れを吸収する
# ------------------------------------------------------------
# Version: Ver33.0-PRODUCTION-DISPLAY-INTEGRITY-GUARD-BUYSELL-ALIGNED-FINAL
# ------------------------------------------------------------
# ✔ completed summary のみ表示対象
# ✔ source未指定fallback依存を回避
# ✔ push -> legacy -> ranking の明示取得
# ✔ symbolname 再補完
# ✔ 1銘柄1行へ正規化
# ✔ 未完成行の表示除外
# ✔ mtf / score_mtf / final_score の不整合補正
# ✔ SELL側の未計算テクニカル行を優先度低下
# ✔ score / score_total / buy-sell / final 完全対応
# ✔ OHLC 完全互換
# ✔ 価格は小数第1位 / 指標は小数第2位
# ✔ BUY TOP10 / SELL TOP10 を明示表示
# ✔ top_candidates があればエントリー候補と表示候補を一致
# ✔ top_candidates が無くても従来ロジックで安全に表示
# ============================================================

from __future__ import annotations

from typing import Optional, Tuple

import pandas as pd
from global_state import global_data

try:
    from trading.summary.top_candidates import prepare_buy_sell_top_df as _prepare_buy_sell_top_df
except Exception:
    _prepare_buy_sell_top_df = None


# ============================================================
# basic helpers
# ============================================================

def _to_float(v, default=0.0):
    try:
        x = pd.to_numeric(v, errors="coerce")
        if pd.isna(x):
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def _fmt_price(v):
    try:
        x = pd.to_numeric(v, errors="coerce")
        if pd.isna(x):
            return "-"
        return f"{float(x):.1f}"
    except Exception:
        return "-"


def _fmt_metric(v):
    try:
        x = pd.to_numeric(v, errors="coerce")
        if pd.isna(x):
            return "-"
        return f"{float(x):.2f}"
    except Exception:
        return "-"


def _parse_interval(label_or_interval):
    if isinstance(label_or_interval, int) and label_or_interval in (1, 3, 5, 10, 15, 30, 60):
        return label_or_interval

    if isinstance(label_or_interval, str):
        s = label_or_interval.lower().replace("min", "").replace("m", "").strip()
        if s.isdigit():
            v = int(s)
            if v in (1, 3, 5, 10, 15, 30, 60):
                return v

    return None


def _ensure_dataframe(df):
    if df is None:
        return pd.DataFrame()

    if isinstance(df, pd.DataFrame):
        out = df.copy()
    elif isinstance(df, pd.Series):
        out = pd.DataFrame([df.to_dict()])
    elif isinstance(df, dict):
        out = pd.DataFrame([df])
    else:
        try:
            out = pd.DataFrame(df).copy()
        except Exception:
            return pd.DataFrame()

    if out.empty:
        return pd.DataFrame()

    try:
        if isinstance(out.columns, pd.MultiIndex):
            out.columns = [
                "_".join([str(x) for x in col if str(x) != ""])
                for col in out.columns.to_flat_index()
            ]
    except Exception:
        pass

    try:
        out = out.reset_index(drop=True)
    except Exception:
        pass

    return out


def _coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    try:
        cols = list(df.columns)
        if len(cols) == len(set(cols)):
            return df
    except Exception:
        return df

    df = df.copy()

    try:
        unique_cols = []
        seen = set()
        for c in df.columns:
            if c not in seen:
                unique_cols.append(c)
                seen.add(c)

        out = {}
        for c in unique_cols:
            idxs = [i for i, name in enumerate(df.columns) if name == c]
            if len(idxs) == 1:
                out[c] = df.iloc[:, idxs[0]]
                continue

            s = df.iloc[:, idxs[0]]
            for j in idxs[1:]:
                try:
                    s = s.combine_first(df.iloc[:, j])
                except Exception:
                    try:
                        s = s.where(s.notna(), df.iloc[:, j])
                    except Exception:
                        pass
            out[c] = s

        return pd.DataFrame(out).reset_index(drop=True)

    except Exception:
        try:
            return df.loc[:, ~df.columns.duplicated()].copy()
        except Exception:
            return df


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


def _pick_series(df: pd.DataFrame, candidates, default=0.0):
    for col in candidates:
        if col in df.columns:
            try:
                s = pd.to_numeric(df[col], errors="coerce")
                s = s.replace([float("inf"), float("-inf")], default)
                return s.fillna(default)
            except Exception:
                pass
    return pd.Series(default, index=df.index, dtype="float64")


def _pick_series_nan(df: pd.DataFrame, candidates):
    for col in candidates:
        if col in df.columns:
            try:
                s = pd.to_numeric(df[col], errors="coerce")
                s = s.replace([float("inf"), float("-inf")], float("nan"))
                return s
            except Exception:
                pass
    return pd.Series(float("nan"), index=df.index, dtype="float64")


def _pick_text_series(df: pd.DataFrame, candidates, default=""):
    for col in candidates:
        if col in df.columns:
            try:
                return df[col].fillna(default).astype(str)
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


def _get_ohlc(row: dict):
    try:
        o = row.get("open", row.get("open_price"))
        h = row.get("high", row.get("high_price"))
        l = row.get("low", row.get("low_price"))

        c = row.get("close")
        if c is None or pd.isna(c):
            c = row.get("close_price")
        if c is None or pd.isna(c):
            c = row.get("c")

        return o, h, l, c
    except Exception:
        return None, None, None, None


# ============================================================
# completed-summary guards
# ============================================================

def _is_completed_summary_df(df: pd.DataFrame) -> bool:
    try:
        if df is None or df.empty:
            return False

        needed = {"symbol", "score"}
        if not needed.issubset(set(df.columns)):
            return False

        symbol_s = df["symbol"].fillna("").astype(str).str.strip()
        if symbol_s.eq("").all():
            return False

        score_s = pd.to_numeric(df["score"], errors="coerce") if "score" in df.columns else pd.Series(dtype=float)
        if score_s.notna().sum() == 0:
            return False

        buy_s = _pick_series_nan(df, ["score_buy", "buy_score", "buy"])
        sell_s = _pick_series_nan(df, ["score_sell", "sell_score", "sell"])
        if buy_s.notna().sum() == 0 and sell_s.notna().sum() == 0:
            return False

        return True
    except Exception:
        return False


def _fetch_completed_summary(interval: int) -> pd.DataFrame:
    """
    source未指定取得を避ける。
    push -> legacy -> ranking の順で、completed summary のみ採用。
    """
    sources = ("push", "legacy", "ranking")

    for source in sources:
        try:
            df = global_data.get_merged_summary(interval, source=source)
            df = _ensure_dataframe(df)
            df = _coalesce_duplicate_columns(df)
            if _is_completed_summary_df(df):
                return df
        except TypeError:
            try:
                getter_name = f"get_{source}_merged_summary"
                getter = getattr(global_data, getter_name, None)
                if callable(getter):
                    df = getter(interval)
                    df = _ensure_dataframe(df)
                    df = _coalesce_duplicate_columns(df)
                    if _is_completed_summary_df(df):
                        return df
            except Exception:
                pass
        except Exception:
            pass

    return pd.DataFrame()


# ============================================================
# display normalization
# ============================================================

def _repair_mtf_consistency(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()

    try:
        mtf = _pick_series_nan(out, ["mtf", "mtf_alignment"])
        score_mtf = _pick_series_nan(out, ["score_mtf", "mtf_score"])
        final_score = _pick_series_nan(out, ["final_score", "display_score"])
        total_score = _pick_series_nan(out, ["score_total", "combined_score", "score"])

        bad_mask = mtf.fillna(0).eq(0)

        if "score_mtf" in out.columns:
            out.loc[bad_mask & score_mtf.fillna(0).gt(0), "score_mtf"] = 0.0
        if "mtf_score" in out.columns:
            out.loc[bad_mask & score_mtf.fillna(0).gt(0), "mtf_score"] = 0.0

        if "final_score" in out.columns:
            repl_mask = bad_mask & final_score.fillna(0).gt(0) & total_score.notna()
            if repl_mask.any():
                out.loc[repl_mask, "final_score"] = total_score[repl_mask]
    except Exception:
        pass

    return out


def _dedupe_latest_best(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df

    out = df.copy()

    if "symbol" not in out.columns:
        return pd.DataFrame()

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

    for c, w in [
        ("disp_total_score", 8),
        ("disp_buy_score", 6),
        ("disp_sell_score", 6),
        ("disp_final_score", 6),
        ("disp_slope", 4),
        ("disp_score_slope", 4),
        ("disp_mtf", 4),
        ("disp_score_mtf", 4),
        ("disp_rsi", 3),
        ("disp_macd", 3),
        ("disp_signal", 3),
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
    out = out.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)
    return out.drop(columns=["_complete_score"], errors="ignore")


def _normalize_for_display(df: pd.DataFrame):
    if df is None or df.empty:
        return pd.DataFrame()

    try:
        df = _ensure_dataframe(df)
        df = _coalesce_duplicate_columns(df)
        df = _repair_mtf_consistency(df)

        if "symbol" not in df.columns:
            return pd.DataFrame()

        df["symbol"] = df["symbol"].map(_normalize_symbol_value)
        df = df[df["symbol"] != ""].copy()
        if df.empty:
            return pd.DataFrame()

        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
            try:
                df["datetime"] = df["datetime"].dt.tz_localize(None)
            except Exception:
                pass

        df["symbolname_view"] = _resolve_symbolname_series(df)

        df["disp_buy_score"] = _pick_series(df, ["score_buy", "buy_score", "buy"], default=0.0)
        df["disp_sell_score"] = _pick_series(df, ["score_sell", "sell_score", "sell"], default=0.0).abs()

        df["disp_total_score"] = _pick_series(
            df,
            ["score_total", "combined_score", "display_score", "score", "final_score"],
            default=0.0,
        )
        if float(df["disp_total_score"].abs().sum()) == 0.0:
            df["disp_total_score"] = df["disp_buy_score"] - df["disp_sell_score"]

        df["disp_final_score"] = _pick_series(
            df,
            ["final_score", "display_score", "score_total", "score"],
            default=0.0,
        )
        if float(df["disp_final_score"].abs().sum()) == 0.0:
            df["disp_final_score"] = df["disp_total_score"]

        df["disp_slope"] = _pick_series(
            df,
            ["slope", "slope_atr_scaled", "ma75_slope"],
            default=0.0,
        )
        df["disp_score_slope"] = _pick_series(
            df,
            ["score_slope", "slope_atr_scaled", "slope"],
            default=0.0,
        )

        df["disp_mtf"] = _pick_series(
            df,
            ["mtf", "mtf_alignment"],
            default=0.0,
        )
        df["disp_score_mtf"] = _pick_series(
            df,
            ["score_mtf", "mtf_score", "mtf"],
            default=0.0,
        )

        df["disp_rsi"] = _pick_series_nan(df, ["rsi", "RSI"])
        df["disp_macd"] = _pick_series_nan(df, ["macd", "MACD"])
        df["disp_signal"] = _pick_series_nan(df, ["signal", "macd_signal", "SIGNAL"])

        df["disp_base"] = _pick_series_nan(df, ["score_base", "_score_base", "base"])
        df["disp_trend"] = _pick_series_nan(df, ["score_trend", "_score_trend", "trend"])
        df["disp_mom"] = _pick_series_nan(
            df,
            ["score_momentum", "_score_momentum", "mom", "momentum"],
        )
        df["disp_vel"] = _pick_series_nan(
            df,
            ["score_velocity", "_score_velocity", "vel", "velocity"],
        )
        df["disp_pen"] = _pick_series_nan(
            df,
            ["direction_penalty", "direction_penalty_score", "penalty", "penalty_score", "pen"],
        )

        valid_mask = (
            df["disp_total_score"].notna()
            & df["disp_buy_score"].notna()
            & df["disp_sell_score"].notna()
            & df["symbol"].fillna("").astype(str).str.strip().ne("")
        )
        df = df[valid_mask].copy()
        if df.empty:
            return pd.DataFrame()

        df = _dedupe_latest_best(df)
        if df.empty:
            return pd.DataFrame()

        sort_cols = ["disp_buy_score", "disp_total_score", "disp_mtf", "disp_slope"]
        ascending = [False, False, False, False]
        if "datetime" in df.columns:
            sort_cols.append("datetime")
            ascending.append(False)

        df = df.sort_values(sort_cols, ascending=ascending, kind="stable").reset_index(drop=True)
        return df

    except Exception:
        return pd.DataFrame()


# ============================================================
# top-candidates alignment helpers
# ============================================================

def _legacy_prepare_buy_sell_top_df(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    top_candidates が無い場合の従来互換フォールバック。
    """
    work = _normalize_for_display(df)
    if work.empty:
        return pd.DataFrame(), pd.DataFrame()

    buy_df = (
        work[work["disp_buy_score"] > 0]
        .sort_values(
            ["disp_buy_score", "disp_total_score", "disp_mtf", "disp_slope"],
            ascending=[False, False, False, False],
            kind="stable",
        )
        .head(10)
        .copy()
    )

    if buy_df.empty:
        buy_df = (
            work.sort_values(
                ["disp_buy_score", "disp_total_score", "disp_mtf", "disp_slope"],
                ascending=[False, False, False, False],
                kind="stable",
            )
            .head(10)
            .copy()
        )

    sell_df = work[work["disp_sell_score"] > 0].copy()
    if not sell_df.empty:
        tech_quality = pd.Series(0, index=sell_df.index, dtype="int64")

        for col in ("disp_rsi", "disp_macd", "disp_signal"):
            s = pd.to_numeric(sell_df[col], errors="coerce")
            tech_quality += s.notna().astype(int) * 2

        rsi0 = pd.to_numeric(sell_df["disp_rsi"], errors="coerce").fillna(0).eq(0)
        macd0 = pd.to_numeric(sell_df["disp_macd"], errors="coerce").fillna(0).eq(0)
        signal0 = pd.to_numeric(sell_df["disp_signal"], errors="coerce").fillna(0).eq(0)
        all_zero_tech = rsi0 & macd0 & signal0
        tech_quality -= all_zero_tech.astype(int) * 3

        sell_df["_tech_quality"] = tech_quality
        sort_cols = ["disp_sell_score", "_tech_quality", "disp_mtf", "disp_slope"]
        ascending = [False, False, True, True]
        if "datetime" in sell_df.columns:
            sort_cols.append("datetime")
            ascending.append(False)

        sell_df = sell_df.sort_values(sort_cols, ascending=ascending, kind="stable").head(10).copy()
        sell_df = sell_df.drop(columns=["_tech_quality"], errors="ignore")
    else:
        sell_df = pd.DataFrame()

    return buy_df, sell_df


def _aligned_prepare_buy_sell_top_df(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    top_candidates がある場合はエントリー候補と同じロジックを優先利用し、
    返ってきた行に display 用列を再付与する。
    """
    work = _normalize_for_display(df)
    if work.empty:
        return pd.DataFrame(), pd.DataFrame()

    if callable(_prepare_buy_sell_top_df):
        try:
            buy_raw, sell_raw = _prepare_buy_sell_top_df(work, buy_top_n=10, sell_top_n=10)
            buy_raw = _ensure_dataframe(buy_raw)
            sell_raw = _ensure_dataframe(sell_raw)

            if buy_raw.empty and sell_raw.empty:
                return _legacy_prepare_buy_sell_top_df(work)

            # 表示用列を不足補完
            def _merge_display_cols(target_df: pd.DataFrame) -> pd.DataFrame:
                if target_df is None or target_df.empty:
                    return pd.DataFrame()

                target_df = _ensure_dataframe(target_df)
                target_df = _coalesce_duplicate_columns(target_df)

                if "symbol" not in target_df.columns:
                    return pd.DataFrame()

                target_df["symbol"] = target_df["symbol"].map(_normalize_symbol_value)
                target_df = target_df[target_df["symbol"] != ""].copy()
                if target_df.empty:
                    return pd.DataFrame()

                base_cols = [c for c in work.columns if c not in target_df.columns]
                if base_cols:
                    merged = target_df.merge(
                        work[["symbol"] + base_cols].drop_duplicates(subset=["symbol"], keep="first"),
                        on="symbol",
                        how="left",
                    )
                else:
                    merged = target_df

                merged = _coalesce_duplicate_columns(merged)
                merged = _normalize_for_display(merged)
                return merged

            buy_df = _merge_display_cols(buy_raw)
            sell_df = _merge_display_cols(sell_raw)

            if buy_df.empty and sell_df.empty:
                return _legacy_prepare_buy_sell_top_df(work)

            return buy_df.head(10), sell_df.head(10)

        except Exception:
            return _legacy_prepare_buy_sell_top_df(work)

    return _legacy_prepare_buy_sell_top_df(work)


# ============================================================
# latest bar
# ============================================================

def print_latest_bar(interval: int | str):
    interval = _parse_interval(interval)

    if interval is None:
        print("[SUMMARY] interval invalid")
        return

    try:
        df = _fetch_completed_summary(interval)

        if df is None or df.empty:
            print(f"[SUMMARY] no completed summary for {interval}min")
            return

        df = _ensure_dataframe(df)
        df = _coalesce_duplicate_columns(df)

        if "datetime" not in df.columns or "symbol" not in df.columns:
            print(f"[SUMMARY] no completed summary for {interval}min")
            return

        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime"]).copy()
        if df.empty:
            print(f"[SUMMARY] no completed summary for {interval}min")
            return

        try:
            df["datetime"] = df["datetime"].dt.tz_localize(None)
        except Exception:
            pass

        df["symbol"] = df["symbol"].map(_normalize_symbol_value)
        df = df[df["symbol"] != ""].copy()
        if df.empty:
            print(f"[SUMMARY] no completed summary for {interval}min")
            return

        df["symbolname_view"] = _resolve_symbolname_series(df)
        df = df.sort_values(["datetime", "symbol"], kind="stable")
        row = df.iloc[-1].to_dict()

        symbol = str(row.get("symbol", "")).strip()
        name = str(row.get("symbolname_view", "")).strip()
        o, h, l, c = _get_ohlc(row)
        t = row.get("datetime")

        print()
        print(f"=== ⏱ 最新 {interval}min サマリー｜{t} ===")
        print(
            f"{symbol:>4}   {name:<20} "
            f"始:{_fmt_price(o)} 高:{_fmt_price(h)} 安:{_fmt_price(l)} 終:{_fmt_price(c)}"
        )

    except Exception:
        print(f"[SUMMARY] display error {interval}min")


# ============================================================
# top10 display
# ============================================================

def _detail_line(r) -> Optional[str]:
    base = _fmt_metric(getattr(r, "disp_base", None))
    trend = _fmt_metric(getattr(r, "disp_trend", None))
    mom = _fmt_metric(getattr(r, "disp_mom", None))
    vel = _fmt_metric(getattr(r, "disp_vel", None))
    pen = _fmt_metric(getattr(r, "disp_pen", None))
    score_slope = _fmt_metric(getattr(r, "disp_score_slope", None))
    score_mtf = _fmt_metric(getattr(r, "disp_score_mtf", None))
    signal = _fmt_metric(getattr(r, "disp_signal", None))

    values = [base, trend, mom, vel, pen, score_slope, score_mtf, signal]
    if all(x == "-" for x in values):
        return None

    return (
        f"    base={base} trend={trend} mom={mom} vel={vel} pen={pen} "
        f"score_slope={score_slope} score_mtf={score_mtf} signal={signal}"
    )


def print_summary_top10(df=None, label=None, interval=None):
    if interval is None:
        interval = _parse_interval(label)

    interval = _parse_interval(interval)

    if interval is None:
        print("[TOP10] interval invalid")
        return

    try:
        if df is None:
            df = _fetch_completed_summary(interval)

        if df is None or df.empty:
            print(f"[TOP10 {interval}min] completed summary データなし")
            return

        df = _normalize_for_display(df)

        if df.empty:
            print(f"[TOP10 {interval}min] completed summary データなし")
            return

        buy_df, sell_df = _aligned_prepare_buy_sell_top_df(df)

        print(f"\n========== 📊 SUMMARY TOP10 ({interval}min) ==========")
        print("🔵 BUY TOP10（score / buy / sell / slope / mtf / total / final / rsi / macd）")

        if buy_df.empty:
            print("🔵 BUY TOP10（該当なし）")
        else:
            for i, r in enumerate(buy_df.itertuples(), 1):
                score = _fmt_metric(getattr(r, "disp_total_score", None))
                buy = _fmt_metric(getattr(r, "disp_buy_score", None))
                sell = _fmt_metric(getattr(r, "disp_sell_score", None))
                slope = _fmt_metric(getattr(r, "disp_slope", None))
                mtf = _fmt_metric(getattr(r, "disp_mtf", None))
                total = _fmt_metric(getattr(r, "disp_total_score", None))
                final = _fmt_metric(getattr(r, "disp_final_score", None))
                rsi = _fmt_metric(getattr(r, "disp_rsi", None))
                macd = _fmt_metric(getattr(r, "disp_macd", None))

                print(
                    f"{i:>2}. ⚪ {str(r.symbol):<6} {str(getattr(r, 'symbolname_view', '')):<24} "
                    f"score={score:>6} buy={buy:>6} sell={sell:>6} "
                    f"slope={slope:>6} mtf={mtf:>6} total={total:>6} final={final:>6} "
                    f"rsi={rsi:>6} macd={macd:>6}"
                )

                detail = _detail_line(r)
                if detail:
                    print(detail)

        print("🔴 SELL TOP10（下落圧が強い）")

        if sell_df.empty:
            print("🔴 SELL TOP10（該当なし）")
        else:
            for i, r in enumerate(sell_df.itertuples(), 1):
                score = _fmt_metric(getattr(r, "disp_total_score", None))
                buy = _fmt_metric(getattr(r, "disp_buy_score", None))
                sell = _fmt_metric(getattr(r, "disp_sell_score", None))
                slope = _fmt_metric(getattr(r, "disp_slope", None))
                mtf = _fmt_metric(getattr(r, "disp_mtf", None))
                total = _fmt_metric(getattr(r, "disp_total_score", None))
                final = _fmt_metric(getattr(r, "disp_final_score", None))
                rsi = _fmt_metric(getattr(r, "disp_rsi", None))
                macd = _fmt_metric(getattr(r, "disp_macd", None))

                print(
                    f"{i:>2}. 🔴 {str(r.symbol):<6} {str(getattr(r, 'symbolname_view', '')):<24} "
                    f"score={score:>6} buy={buy:>6} sell={sell:>6} "
                    f"slope={slope:>6} mtf={mtf:>6} total={total:>6} final={final:>6} "
                    f"rsi={rsi:>6} macd={macd:>6}"
                )

                detail = _detail_line(r)
                if detail:
                    print(detail)

        print("=" * 60)

    except Exception:
        print(f"[TOP10] display error {interval}min")


# ============================================================
# ranking attach helpers
# ============================================================

def _fetch_latest_ranking_view(interval: int | None = None) -> pd.DataFrame:
    """
    ranking系の最新 view を表示用に取得する。
    取得元は ranking merged summary を優先し、無ければ空DFを返す。
    """
    try:
        df = global_data.get_merged_summary(1, source="ranking")
        df = _ensure_dataframe(df)
        df = _coalesce_duplicate_columns(df)

        if df.empty or "symbol" not in df.columns:
            return pd.DataFrame()

        df["symbol"] = df["symbol"].map(_normalize_symbol_value)
        df = df[df["symbol"] != ""].copy()
        if df.empty:
            return pd.DataFrame()

        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
            try:
                df["datetime"] = df["datetime"].dt.tz_localize(None)
            except Exception:
                pass
            df = df.sort_values(["symbol", "datetime"], ascending=[True, False], kind="stable")
        else:
            df = df.sort_values(["symbol"], kind="stable")

        df = df.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)

        out = pd.DataFrame()
        out["symbol"] = df["symbol"]

        # ranking category / type
        if "ranking_type" in df.columns:
            out["ranking_type_view"] = df["ranking_type"]
        elif "type_name" in df.columns:
            out["ranking_type_view"] = df["type_name"]
        elif "type" in df.columns:
            out["ranking_type_view"] = df["type"]
        else:
            out["ranking_type_view"] = ""

        # rank
        if "ranking_rank" in df.columns:
            out["ranking_rank_view"] = pd.to_numeric(df["ranking_rank"], errors="coerce")
        elif "rank" in df.columns:
            out["ranking_rank_view"] = pd.to_numeric(df["rank"], errors="coerce")
        else:
            out["ranking_rank_view"] = pd.Series(pd.NA, index=df.index)

        # metrics
        for src_col, dst_col in [
            ("change_percentage", "ranking_change_pct_view"),
            ("turnover", "ranking_turnover_view"),
            ("tick_count", "ranking_tick_count_view"),
            ("trading_volume", "ranking_volume_view"),
            ("trading_value", "ranking_value_view"),
            ("current_price", "ranking_price_view"),
        ]:
            if src_col in df.columns:
                out[dst_col] = pd.to_numeric(df[src_col], errors="coerce")
            else:
                out[dst_col] = pd.Series(pd.NA, index=df.index)

        return out

    except Exception:
        return pd.DataFrame()


def _attach_ranking_info(df: pd.DataFrame, interval: int | None = None) -> pd.DataFrame:
    """
    completed summary に ranking 情報を symbol で付与する。
    """
    try:
        base = _ensure_dataframe(df)
        base = _coalesce_duplicate_columns(base)

        if base.empty or "symbol" not in base.columns:
            return pd.DataFrame() if base is None else base

        base["symbol"] = base["symbol"].map(_normalize_symbol_value)
        base = base[base["symbol"] != ""].copy()
        if base.empty:
            return base

        rank_df = _fetch_latest_ranking_view(interval=interval)
        if rank_df.empty:
            return base

        merged = base.merge(
            rank_df.drop_duplicates(subset=["symbol"], keep="first"),
            on="symbol",
            how="left",
        )
        merged = _coalesce_duplicate_columns(merged)
        return merged

    except Exception:
        return df