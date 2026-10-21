# ============================================================
# File   : trading/scoring/core/scoring_pipeline.py
# Version: Ver1.3-PRODUCTION-SCORING-PIPELINE-SIDE-SCORE-REPAIR
# ------------------------------------------------------------
# ✔ scoring pipeline public entrypoint
# ✔ backward compatibility for run_scoring_pipeline
# ✔ scoring_main public alias
# ✔ interval / analysis_only / force kwargs safe ignore
# ✔ detail score columns support
# ✔ score / buy / sell / total / final support
# ✔ score_slope / score_mtf support
# ✔ production safe
# ✔ FIX: detail列の NaN preserve
# ✔ FIX: total合成時のみ一時 fillna(0.0)
# ✔ FIX: 既存 total/score/final を不必要に上書きしない
# ✔ FIX: display_score は total の符号を維持
# ✔ FIX: passthrough テクニカル列を破壊しない
# ✔ Ver1.3: score_buy / score_sell が既存0で埋まっているだけの場合、
#            signed total score から安全に再補完する。
# ============================================================

from __future__ import annotations

import logging
import math
import os
import time

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# dataframe helpers
# ============================================================

def _ensure_dataframe(df) -> pd.DataFrame:
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
            logger.exception("[SCORING] dataframe conversion failed")
            return pd.DataFrame()

    if out.empty:
        return pd.DataFrame()

    try:
        if isinstance(out.columns, pd.MultiIndex):
            out.columns = [
                "_".join([str(x) for x in col if str(x) not in ("", "None")]).strip("_")
                for col in out.columns.to_flat_index()
            ]
    except Exception:
        logger.debug("[SCORING] multiindex flatten failed", exc_info=True)

    try:
        if out.columns.duplicated().any():
            out = out.loc[:, ~out.columns.duplicated()].copy()
    except Exception:
        logger.debug("[SCORING] duplicate column cleanup failed", exc_info=True)

    return out.reset_index(drop=True)


def _safe_num(v, default=np.nan) -> float:
    try:
        if v is None:
            return default
        if isinstance(v, str) and not v.strip():
            return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _safe_series(df: pd.DataFrame, col: str, default=np.nan) -> pd.Series:
    if df is None or df.empty or col not in df.columns:
        return pd.Series(default, index=df.index if isinstance(df, pd.DataFrame) else None, dtype="float64")
    try:
        s = pd.to_numeric(df[col], errors="coerce")
        s = s.replace([np.inf, -np.inf], np.nan)
        return s
    except Exception:
        logger.debug("[SCORING] safe_series failed col=%s", col, exc_info=True)
        return pd.Series(default, index=df.index, dtype="float64")


def _pick_series(df: pd.DataFrame, cols: list[str], default=np.nan) -> pd.Series:
    for c in cols:
        if c in df.columns:
            return _safe_series(df, c, default=default)
    return pd.Series(default, index=df.index, dtype="float64")


def _fillna_num(series: pd.Series, default: float = 0.0) -> pd.Series:
    try:
        s = pd.to_numeric(series, errors="coerce")
        s = s.replace([np.inf, -np.inf], np.nan)
        return s.fillna(default)
    except Exception:
        return pd.Series(default, index=series.index if hasattr(series, "index") else None, dtype="float64")


def _nonnull_count(s: pd.Series) -> int:
    try:
        return int(pd.to_numeric(s, errors="coerce").notna().sum())
    except Exception:
        return 0


def _nonzero_count(s: pd.Series) -> int:
    try:
        return int(pd.to_numeric(s, errors="coerce").fillna(0.0).ne(0).sum())
    except Exception:
        return 0


def _has_positive(s: pd.Series) -> bool:
    try:
        return bool(pd.to_numeric(s, errors="coerce").fillna(0.0).gt(0.0).any())
    except Exception:
        return False


def _has_negative(s: pd.Series) -> bool:
    try:
        return bool(pd.to_numeric(s, errors="coerce").fillna(0.0).lt(0.0).any())
    except Exception:
        return False


# ============================================================
# detail score normalization
# ============================================================

def _normalize_detail_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    out["base"] = _pick_series(out, ["base", "score_base"], default=np.nan)
    out["trend"] = _pick_series(out, ["trend", "score_trend"], default=np.nan)
    out["mom"] = _pick_series(out, ["mom", "score_momentum", "momentum"], default=np.nan)
    out["vel"] = _pick_series(out, ["vel", "score_velocity", "velocity"], default=np.nan)
    out["pen"] = _pick_series(out, ["pen", "direction_penalty", "penalty"], default=np.nan)

    out["score_slope"] = _pick_series(out, ["score_slope", "slope_score", "slope"], default=np.nan)
    out["score_mtf"] = _pick_series(out, ["score_mtf", "mtf_score", "mtf"], default=np.nan)

    return out


# ============================================================
# score calculation core
# ============================================================

def _build_total_score(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out = _normalize_detail_columns(out)

    base = _safe_series(out, "base", default=np.nan)
    trend = _safe_series(out, "trend", default=np.nan)
    mom = _safe_series(out, "mom", default=np.nan)
    vel = _safe_series(out, "vel", default=np.nan)
    pen = _safe_series(out, "pen", default=np.nan)

    score_slope_raw = _safe_series(out, "score_slope", default=np.nan)
    score_mtf_raw = _safe_series(out, "score_mtf", default=np.nan)

    total_raw = _fillna_num(base, 0.0) + _fillna_num(trend, 0.0) + _fillna_num(mom, 0.0) + _fillna_num(vel, 0.0) - _fillna_num(pen, 0.0)
    total_score_synth = total_raw + _fillna_num(score_slope_raw, 0.0) + _fillna_num(score_mtf_raw, 0.0)

    existing_total = _safe_series(out, "score_total", default=np.nan)
    existing_score = _safe_series(out, "score", default=np.nan)
    existing_final = _safe_series(out, "final_score", default=np.nan)
    existing_combined = _safe_series(out, "combined_score", default=np.nan)
    existing_display = _safe_series(out, "display_score", default=np.nan)
    existing_buy = _safe_series(out, "score_buy", default=np.nan)
    existing_sell = _safe_series(out, "score_sell", default=np.nan)

    resolved_total = existing_total.copy()
    resolved_total = resolved_total.where(resolved_total.notna(), existing_score)
    resolved_total = resolved_total.where(resolved_total.notna(), existing_final)
    resolved_total = resolved_total.where(resolved_total.notna(), existing_combined)
    resolved_total = resolved_total.where(resolved_total.notna(), total_score_synth)

    out["score_total"] = resolved_total
    out["combined_score"] = existing_combined.where(existing_combined.notna(), resolved_total)
    out["final_score"] = existing_final.where(existing_final.notna(), resolved_total)
    out["display_score"] = existing_display.where(existing_display.notna(), resolved_total)
    out["score"] = existing_score.where(existing_score.notna(), resolved_total)

    buy_score_synth = resolved_total.clip(lower=0.0)
    sell_score_synth = (-resolved_total).clip(lower=0.0)

    buy_present = existing_buy.notna()
    sell_present = existing_sell.notna()
    buy_nonzero = _nonzero_count(existing_buy)
    sell_nonzero = _nonzero_count(existing_sell)
    total_has_positive = _has_positive(resolved_total)
    total_has_negative = _has_negative(resolved_total)

    # 既存の score_buy/score_sell が 0.0 で埋まっているだけの場合は「実質欠損」とみなす。
    # これにより score が正なのに score_buy が全件0、または score が負なのに score_sell が全件0になる状態を修復する。
    if buy_nonzero == 0 and total_has_positive:
        out["score_buy"] = buy_score_synth
        logger.warning("[SCORING SIDE REPAIR] score_buy all-zero repaired from signed total rows=%s positive=%s", len(out), int(buy_score_synth.gt(0).sum()))
    else:
        out["score_buy"] = existing_buy.where(buy_present, buy_score_synth)

    if sell_nonzero == 0 and total_has_negative:
        out["score_sell"] = sell_score_synth
        logger.warning("[SCORING SIDE REPAIR] score_sell all-zero repaired from signed total rows=%s negative=%s", len(out), int(sell_score_synth.gt(0).sum()))
    else:
        out["score_sell"] = existing_sell.where(sell_present, sell_score_synth)

    # 片側だけが既存0で、もう片側だけ残っている場合も、符号側スコアは同期する。
    out["buy_score"] = out["score_buy"]
    out["sell_score"] = out["score_sell"]

    out["score_slope"] = score_slope_raw
    out["score_mtf"] = score_mtf_raw

    return out


# ============================================================
# optional passthrough columns
# ============================================================

def _normalize_passthrough_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    if "slope" not in out.columns and "score_slope" in out.columns:
        out["slope"] = pd.to_numeric(out["score_slope"], errors="coerce")

    if "mtf" not in out.columns and "score_mtf" in out.columns:
        out["mtf"] = pd.to_numeric(out["score_mtf"], errors="coerce")

    for c in ("slope", "mtf", "score_slope", "score_mtf", "rsi", "macd", "signal", "hist"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")

    return out


def _finalize_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    for c in ("score_total", "combined_score", "final_score", "display_score", "score", "score_buy", "buy_score", "score_sell", "sell_score"):
        if c not in out.columns:
            out[c] = 0.0
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0)

    for c in ("slope", "mtf", "score_slope", "score_mtf", "rsi", "macd", "signal", "hist"):
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")

    return out


# ============================================================
# main implementation
# ============================================================

def _flag_builder_enabled() -> bool:
    try:
        v = os.getenv("SCORE_FLAG_BUILDER_ENABLED")
        if v is None or str(v).strip() == "":
            return True
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return True


def _flag_builder_max_rows() -> int:
    """行ごと評価なので上限を設ける。

    実測 (2026-08-26): 130行 0.9s / 300行 2.1s / 644行 8.4s / 1000行 7.3s。
    毎分 Summary の履歴込みフレームは 644 行あり 8.4 秒 x2 かかっていた。
    上限を 400 にすると自動的に latest_per_symbol (152行) 評価へ落ち、
    約 2 秒で済む。判定内容・閾値は変えていない。
    """
    try:
        return max(1, int(float(os.getenv("SCORE_FLAG_BUILDER_MAX_ROWS", "400"))))
    except Exception:
        return 400


def _attach_condition_flags(out: pd.DataFrame) -> pd.DataFrame:
    """旧 builder (conditions_long/short) と追加フラグ (flag_builder_extra) を順に付ける。
    旧 builder が行数上限などで skip しても追加フラグは必ず付ける。"""
    out = _attach_condition_flags_legacy(out)
    return _attach_extra_flags(out)


def _attach_extra_flags(out: pd.DataFrame) -> pd.DataFrame:
    # 2026-09-15: score_config.ini にあるのに一度も算出されなかった 60 個の
    #   フラグ (クロス系 / 構造 / VWAP / 出来高 / 時間帯) をベクトル化して足す。
    #   全行 (履歴込み) に対して groupby(symbol) で計算するので軽い。
    #   trading/indicators/flag_builder_extra.py。SCORE_FLAG_EXTRA_ENABLED=0 で OFF。
    if not isinstance(out, pd.DataFrame) or out.empty:
        return out
    if str(os.getenv("SCORE_FLAG_EXTRA_ENABLED", "1")).strip().lower() in {"0", "false", "no", "off"}:
        return out
    if "datetime" not in out.columns or "symbol" not in out.columns:
        return out
    t1 = time.perf_counter()
    try:
        from trading.indicators.flag_builder_extra import build_extra_flags, EXTRA_FLAGS
        extra = build_extra_flags(out)
        if isinstance(extra, pd.DataFrame) and len(extra) == len(out):
            # 追加フラグ列は毎回計算し直した値で置き換える (前 tick の値が入力に残るため)
            extra_cols = [c for c in EXTRA_FLAGS if c in extra.columns]
            new_cols = [c for c in extra_cols if c not in out.columns]
            if new_cols:
                out = pd.concat([out, extra[new_cols].reindex(out.index)], axis=1)
            for c in extra_cols:
                if c not in new_cols:
                    out[c] = extra[c].reindex(out.index)
            logger.warning(
                "[SCORE FLAGS] extra attached rows=%s new_flag_cols=%s elapsed=%.2fs",
                len(out), len(extra_cols), time.perf_counter() - t1,
            )
    except Exception:
        logger.exception("[SCORE FLAGS] extra flags failed rows=%s", len(out))
    return out


def _attach_condition_flags_legacy(out: pd.DataFrame) -> pd.DataFrame:
    """score_config.ini の flag に対応する 0/1 列を DataFrame へ生やす。

    これが無いと score_breakdown が table_flags=180 / matched_flags=2 で
    fallback に落ちる。実測で matched 2 -> 71 になる。
    """
    if not isinstance(out, pd.DataFrame) or out.empty:
        return out
    if not _flag_builder_enabled():
        logger.warning("[SCORE FLAGS] skipped reason=SCORE_FLAG_BUILDER_ENABLED=0 rows=%s", len(out))
        return out
    if "datetime" not in out.columns or "symbol" not in out.columns:
        logger.warning(
            "[SCORE FLAGS] skipped reason=symbol/datetime 列が無い rows=%s cols=%s",
            len(out), len(out.columns),
        )
        return out

    limit = _flag_builder_max_rows()
    n_before = len(out)
    target = out
    scope = "all_rows"
    if n_before > limit:
        # 履歴込みで数千〜万行になることがある (優先4 の履歴 seed 後は
        # 246 銘柄 x 75 本 = 12,345 行)。全行に行ごと評価をかけると
        # 毎分処理を潰すので、ENTRY 判定に効く「銘柄あたり最新足」だけに絞る。
        try:
            # 2026-09-15: 最新 1 本だけ渡すと conditions_long/short の「前の足との
            #   比較」(ma5_ma25_cross / macd_gc など) が prev=None で一度も立たない。
            #   銘柄あたり直近 SCORE_FLAG_HISTORY_BARS 本 (既定 3) を渡し、
            #   最新足の行にだけ結果を取り込む (下の add.reindex)。
            _hist_bars = max(1, int(os.getenv("SCORE_FLAG_HISTORY_BARS", "3") or 3))
            _sorted = out.sort_values(["symbol", "datetime"], kind="stable")
            target = _sorted.groupby("symbol", as_index=False).tail(_hist_bars)
            scope = f"last{_hist_bars}_per_symbol"
            if len(target) > limit and _hist_bars > 1:
                # 履歴込みで上限を超えるなら、従来どおり最新 1 本に落として黙って捨てない
                target = _sorted.groupby("symbol", as_index=False).tail(1)
                scope = "latest_per_symbol"
        except Exception:
            logger.exception("[SCORE FLAGS] latest-per-symbol 抽出に失敗 rows=%s", n_before)
            return out
        if len(target) > limit:
            # 黙って諦めない。上限に当たったことを必ず残す。
            logger.warning(
                "[SCORE FLAGS] skipped reason=latest_per_symbol(%s)>%d rows=%s "
                "(flag_builder は行ごと評価。SCORE_FLAG_BUILDER_MAX_ROWS で調整可)",
                len(target), limit, n_before,
            )
            return out

    t0 = time.perf_counter()
    before_cols = set(out.columns)
    n_target = len(target)
    try:
        from trading.indicators.flag_builder_buy import build_buy_flags
        from trading.indicators.flag_builder_sell import build_sell_flags

        built = build_buy_flags(target.copy())
        built = build_sell_flags(built)
    except Exception:
        logger.exception("[SCORE FLAGS] flag builder failed rows=%s -> 元のフレームを使う", n_target)
        return out

    if not isinstance(built, pd.DataFrame) or built.empty:
        logger.warning("[SCORE FLAGS] builder returned empty rows=%s -> 元のフレームを使う", n_target)
        return out

    # builder は datetime 欠損行を dropna する。行が黙って消えるのは危険なので
    # 行数/index が変わったら結果ごと捨てる。
    if len(built) != n_target or not built.index.equals(target.index):
        logger.warning(
            "[SCORE FLAGS] builder changed row set %s -> %s (index一致=%s) "
            "-> 結果を破棄して元のフレームを使う",
            n_target, len(built), built.index.equals(target.index),
        )
        return out

    # 既存列は絶対に上書きしない。新規に生えた flag 列だけ取り込む。
    new_cols = [c for c in built.columns if c not in before_cols]
    if not new_cols:
        logger.warning("[SCORE FLAGS] no new flag columns rows=%s", n_target)
        return out
    try:
        # latest_per_symbol のときは対象外の行が NaN になる。
        # 0 で埋めない (「評価していない」と「条件が立たなかった」を区別する)。
        add = built[new_cols].reindex(out.index)
        out = pd.concat([out, add], axis=1)
    except Exception:
        logger.exception("[SCORE FLAGS] flag column merge failed rows=%s", n_target)
        return out

    try:
        nonzero = int(sum(
            1 for c in new_cols
            if float(pd.to_numeric(out[c], errors="coerce").fillna(0).abs().sum()) > 0.0
        ))
    except Exception:
        nonzero = -1
    logger.warning(
        "[SCORE FLAGS] attached rows=%s scope=%s evaluated_rows=%s "
        "new_flag_cols=%s nonzero_cols=%s elapsed=%.2fs",
        len(out), scope, n_target, len(new_cols), nonzero, time.perf_counter() - t0,
    )

    return out


def scoring_pipeline(df: pd.DataFrame, *args, **kwargs) -> pd.DataFrame:
    out = _ensure_dataframe(df)

    if out.empty:
        logger.info("[SCORING] scoring_pipeline input empty")
        return out

    out = _normalize_passthrough_columns(out)
    out = _build_total_score(out)
    out = _finalize_score_columns(out)

    # score_breakdown が SCORE_TABLE を使えるように flag 列を先に生やす。
    out = _attach_condition_flags(out)

    try:
        from trading.scoring.core.score_breakdown import attach_score_breakdown
        out = attach_score_breakdown(out, debug=True)
    except Exception:
        logger.exception("[SCORING] attach_score_breakdown failed")

    logger.info(
        "[SCORING] scoring_pipeline done rows=%s score_nonnull=%s score_nonzero=%s buy_nonnull=%s buy_nonzero=%s sell_nonnull=%s sell_nonzero=%s slope_nonnull=%s slope_nonzero=%s mtf_nonnull=%s mtf_nonzero=%s base_nonnull=%s base_nonzero=%s trend_nonnull=%s trend_nonzero=%s mom_nonnull=%s mom_nonzero=%s vel_nonnull=%s vel_nonzero=%s pen_nonnull=%s pen_nonzero=%s",
        len(out),
        _nonnull_count(out["score"]) if "score" in out.columns else 0,
        _nonzero_count(out["score"]) if "score" in out.columns else 0,
        _nonnull_count(out["score_buy"]) if "score_buy" in out.columns else 0,
        _nonzero_count(out["score_buy"]) if "score_buy" in out.columns else 0,
        _nonnull_count(out["score_sell"]) if "score_sell" in out.columns else 0,
        _nonzero_count(out["score_sell"]) if "score_sell" in out.columns else 0,
        _nonnull_count(out["score_slope"]) if "score_slope" in out.columns else 0,
        _nonzero_count(out["score_slope"]) if "score_slope" in out.columns else 0,
        _nonnull_count(out["score_mtf"]) if "score_mtf" in out.columns else 0,
        _nonzero_count(out["score_mtf"]) if "score_mtf" in out.columns else 0,
        _nonnull_count(out["score_base"]) if "score_base" in out.columns else 0,
        _nonzero_count(out["score_base"]) if "score_base" in out.columns else 0,
        _nonnull_count(out["score_trend"]) if "score_trend" in out.columns else 0,
        _nonzero_count(out["score_trend"]) if "score_trend" in out.columns else 0,
        _nonnull_count(out["score_momentum"]) if "score_momentum" in out.columns else 0,
        _nonzero_count(out["score_momentum"]) if "score_momentum" in out.columns else 0,
        _nonnull_count(out["score_velocity"]) if "score_velocity" in out.columns else 0,
        _nonzero_count(out["score_velocity"]) if "score_velocity" in out.columns else 0,
        _nonnull_count(out["score_penalty"]) if "score_penalty" in out.columns else 0,
        _nonzero_count(out["score_penalty"]) if "score_penalty" in out.columns else 0,
    )

    return out


# ============================================================
# public main entrypoint
# ============================================================

def scoring_main(df, *args, **kwargs):
    kwargs.pop("interval", None)
    kwargs.pop("analysis_only", None)
    kwargs.pop("force", None)
    kwargs.pop("evaluate_signals", None)
    kwargs.pop("latest_only", None)
    kwargs.pop("recent_bars_per_symbol", None)

    try:
        return scoring_pipeline(df, *args, **kwargs)
    except TypeError:
        logger.warning("[SCORING] scoring_main TypeError -> retry without extra args")
        return scoring_pipeline(df)


def run_scoring_pipeline(df, *args, **kwargs):
    kwargs.pop("interval", None)
    kwargs.pop("analysis_only", None)
    kwargs.pop("force", None)
    kwargs.pop("evaluate_signals", None)
    kwargs.pop("latest_only", None)
    kwargs.pop("recent_bars_per_symbol", None)

    try:
        return scoring_pipeline(df, *args, **kwargs)
    except TypeError:
        logger.warning("[SCORING] run_scoring_pipeline TypeError -> retry without extra args")
        return scoring_pipeline(df)


def run_pipeline(df, *args, **kwargs):
    return run_scoring_pipeline(df, *args, **kwargs)


__all__ = ["scoring_pipeline", "scoring_main", "run_scoring_pipeline", "run_pipeline"]
