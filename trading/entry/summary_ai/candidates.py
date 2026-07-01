# ============================================================
# File   : trading/entry/summary_ai/candidates.py
# Version: PRODUCTION-STABLE-REV2.4-SUMMARY-SCORE-BRIDGE
# ------------------------------------------------------------
# Purpose:
#   - SUMMARY / RANKING SUMMARY の DataFrame からAI gate候補を作る
#   - build_summary_ai_entry_candidates() で BUY TOP と SELL TOP を同時に返す
#   - 各行に ai_side / side = BUY or SELL を付与し、AI gate側で行ごとに判定する
#
# REV2.4:
#   - 既存summary側に score_buy / score_sell / score_total / final_score / display_score
#     があるのに、候補生成側の ai_disp_* / config_* が0扱いになる事故を本体で防ぐ。
#   - score_config.ini のflag加点が0でも、既存スコアを壊さず最大値を採用する。
#   - runtime patch が通らない呼び出しルートでも候補ゼロにならないよう、
#     candidates.py 内でスコア橋渡しを必ず実施する。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Optional

import pandas as pd

from .utils import (
    VALID_MARKET_TYPES,
    is_truthy,
    normalize_symbol,
    pick_num_series,
    pick_text_series,
    safe_df,
)

logger = logging.getLogger(__name__)

DEFAULT_TOP_N = 20
DEFAULT_MIN_BUY_SCORE = 5.0
DEFAULT_MAX_SELL_SCORE = 2.0
DEFAULT_MIN_VOLUME = 1.0
DEFAULT_MIN_PRICE = 200.0
DEFAULT_MIN_BUY_SLOPE = 0.01
DEFAULT_MAX_SELL_SLOPE = -0.01


# ============================================================
# env / basic helpers
# ============================================================

def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "on", "y", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "off", "n", "ng", "disable", "disabled", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _is_ranking_source(source: str) -> bool:
    try:
        return "RANKING" in str(source or "").upper()
    except Exception:
        return False


def _resolve_top_n(top_n: int | str | None) -> int:
    try:
        n = int(top_n or DEFAULT_TOP_N)
    except Exception:
        n = DEFAULT_TOP_N
    return max(DEFAULT_TOP_N, n)


def _entry_min_price(default: float = DEFAULT_MIN_PRICE) -> float:
    for name in ("ENTRY_MIN_PRICE", "SUMMARY_AI_ENTRY_MIN_PRICE"):
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return _env_float(name, default)
    return float(default)


def _entry_min_buy_slope() -> float:
    for name in ("ENTRY_MIN_BUY_SLOPE", "SUMMARY_AI_MIN_BUY_SLOPE"):
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return _env_float(name, DEFAULT_MIN_BUY_SLOPE)
    return float(DEFAULT_MIN_BUY_SLOPE)


def _entry_max_sell_slope() -> float:
    for name in ("ENTRY_MAX_SELL_SLOPE", "SUMMARY_AI_MAX_SELL_SLOPE"):
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return _env_float(name, DEFAULT_MAX_SELL_SLOPE)
    return float(DEFAULT_MAX_SELL_SLOPE)


def _summary_ai_max_candidate_age_sec(default: float = 900.0) -> float:
    for name in (
        "SUMMARY_AI_MAX_CANDIDATE_AGE_SEC",
        "SUMMARY_ENTRY_PENDING_MAX_AGE_SEC",
        "SUMMARY_AI_PENDING_MAX_AGE_SEC",
        "ENTRY_CANDIDATE_MAX_AGE_SEC",
    ):
        v = os.getenv(name)
        if v is not None and str(v).strip() != "":
            return _env_float(name, default)
    return float(default)


def _safe_symbols(df: pd.DataFrame, n: int = 30) -> list[str]:
    try:
        if isinstance(df, pd.DataFrame) and not df.empty and "symbol" in df.columns:
            return list(df["symbol"].astype(str).head(n))
    except Exception:
        pass
    return []


def _truthy(v: Any) -> bool:
    try:
        if v is None:
            return False
        if isinstance(v, bool):
            return bool(v)
        if isinstance(v, (int, float)):
            return float(v) != 0.0
        s = str(v).strip().lower()
        return s in {"1", "true", "t", "yes", "y", "on", "ok"}
    except Exception:
        return False


def _norm_key(key: str) -> str:
    return str(key or "").strip().lower()


def _flag_variants(key: str) -> list[str]:
    k = _norm_key(key)
    if not k:
        return []
    if k.startswith("flag_"):
        out = [k, k[5:]]
    else:
        out = [k, f"flag_{k}"]
    seen: set[str] = set()
    uniq: list[str] = []
    for x in out:
        if x and x not in seen:
            uniq.append(x)
            seen.add(x)
    return uniq


def _row_has_flag(row: pd.Series, flag_key: str) -> bool:
    for key in _flag_variants(flag_key):
        if key in row.index and _truthy(row.get(key)):
            return True
    return False


def _num(df: pd.DataFrame, names: list[str] | tuple[str, ...], default: float = 0.0) -> pd.Series:
    for name in names:
        try:
            if name in df.columns:
                return pd.to_numeric(df[name], errors="coerce").fillna(default).astype(float)
        except Exception:
            continue
    return pd.Series(default, index=df.index, dtype="float64")


def _max_series(*series: pd.Series) -> pd.Series:
    frames: list[pd.Series] = []
    for s in series:
        try:
            frames.append(pd.to_numeric(s, errors="coerce").fillna(0.0).astype(float))
        except Exception:
            pass
    if not frames:
        return pd.Series(0.0)
    return pd.concat(frames, axis=1).max(axis=1).fillna(0.0).astype(float)


# ============================================================
# score_config.ini flag scoring bridge
# ============================================================

def _load_score_tables() -> dict[str, dict[str, int]]:
    try:
        from trading.scoring.config.score_table import build_score_tables
        tables = build_score_tables()
    except Exception:
        try:
            from scoring.config.score_table import build_score_tables  # type: ignore
            tables = build_score_tables()
        except Exception:
            logger.debug("[SUMMARY AI CANDIDATES] score_config load failed", exc_info=True)
            return {}

    out: dict[str, dict[str, int]] = {}
    for name in ("buy_entry", "buy_bonus", "sell_entry", "sell_bonus"):
        t = tables.get(name, {}) if isinstance(tables, dict) else {}
        if isinstance(t, dict):
            out[name] = {str(k): int(v) for k, v in t.items() if isinstance(v, int) or str(v).lstrip("-").isdigit()}
        else:
            out[name] = {}
    return out


def _score_flags_for_row(row: pd.Series, table: dict[str, int]) -> tuple[float, list[str]]:
    total = 0.0
    hits: list[str] = []
    used_base_keys: set[str] = set()
    for key, score in table.items():
        base_key = _norm_key(key)
        if base_key.startswith("flag_"):
            base_key = base_key[5:]
        if base_key in used_base_keys:
            continue
        if _row_has_flag(row, key):
            used_base_keys.add(base_key)
            total += float(score)
            hits.append(f"{key}:{score}")
    return total, hits


def _apply_score_config_flags(df: pd.DataFrame) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    # ここで既存summaryスコアを退避する。config加点が0でもこの値を残す。
    src_buy = _num(out, ("ai_disp_buy_score", "disp_buy_score", "score_buy", "buy_score", "buy", "buy_signal_score"), 0.0)
    src_sell = _num(out, ("ai_disp_sell_score", "disp_sell_score", "score_sell", "sell_score", "sell", "sell_signal_score"), 0.0).abs()
    src_total = _num(out, ("ai_disp_total_score", "disp_total_score", "score_total", "total_score", "combined_score", "final_score", "display_score", "score"), 0.0)
    total_buy = src_total.clip(lower=0.0)
    total_sell = (-src_total).clip(lower=0.0)

    cfg_buy = pd.Series(0.0, index=out.index, dtype="float64")
    cfg_sell = pd.Series(0.0, index=out.index, dtype="float64")
    buy_hits_col = [""] * len(out)
    sell_hits_col = [""] * len(out)

    if _env_bool("SUMMARY_AI_APPLY_SCORE_CONFIG_FLAGS", True):
        tables = _load_score_tables()
        buy_entry_t = tables.get("buy_entry", {})
        buy_bonus_t = tables.get("buy_bonus", {})
        sell_entry_t = tables.get("sell_entry", {})
        sell_bonus_t = tables.get("sell_bonus", {})
        if any((buy_entry_t, buy_bonus_t, sell_entry_t, sell_bonus_t)):
            buy_scores: list[float] = []
            sell_scores: list[float] = []
            buy_hits_col = []
            sell_hits_col = []
            for _, row in out.iterrows():
                be, be_hits = _score_flags_for_row(row, buy_entry_t)
                bb, bb_hits = _score_flags_for_row(row, buy_bonus_t)
                se, se_hits = _score_flags_for_row(row, sell_entry_t)
                sb, sb_hits = _score_flags_for_row(row, sell_bonus_t)
                buy_scores.append(float(be) + float(bb))
                sell_scores.append(abs(float(se)) + abs(float(sb)))
                buy_hits_col.append("|".join((be_hits + bb_hits)[:30]))
                sell_hits_col.append("|".join((se_hits + sb_hits)[:30]))
            cfg_buy = pd.Series(buy_scores, index=out.index, dtype="float64")
            cfg_sell = pd.Series(sell_scores, index=out.index, dtype="float64")
        else:
            logger.warning("[SUMMARY AI CANDIDATES] score_config tables empty; use existing summary scores only")
    else:
        logger.warning("[SUMMARY AI CANDIDATES] score_config flag scoring disabled by env; use existing summary scores only")

    # REV2.4 core fix:
    # config点だけでなく、既存の score_buy/score_sell/score_total/final_score/display_score を最大値で橋渡しする。
    final_buy = _max_series(src_buy, cfg_buy, total_buy)
    final_sell = _max_series(src_sell, cfg_sell, total_sell).abs()
    final_total = final_buy - final_sell

    out["config_buy_entry_score"] = cfg_buy
    out["config_buy_bonus_score"] = 0.0
    out["config_buy_score"] = _max_series(cfg_buy, src_buy, total_buy)
    out["config_sell_entry_score"] = cfg_sell
    out["config_sell_bonus_score"] = 0.0
    out["config_sell_score"] = _max_series(cfg_sell, src_sell, total_sell).abs()
    out["config_buy_hits"] = buy_hits_col
    out["config_sell_hits"] = sell_hits_col

    out["ai_disp_buy_score"] = final_buy
    out["ai_disp_sell_score"] = final_sell
    out["score_buy"] = final_buy
    out["buy_score"] = final_buy
    out["score_sell"] = final_sell
    out["sell_score"] = final_sell
    out["score_total"] = final_total
    out["total_score"] = final_total
    out["final_score"] = final_total
    out["display_score"] = final_total
    out["ai_disp_total_score"] = final_total
    out["ai_disp_final_score"] = final_total

    try:
        logger.warning(
            "[SUMMARY AI SCORE BRIDGE CORE] applied rows=%s buy_positive=%s sell_positive=%s buy_max=%.2f sell_max=%.2f src_buy_pos=%s src_sell_pos=%s src_total_pos=%s src_total_neg=%s",
            len(out),
            int((final_buy > 0).sum()),
            int((final_sell > 0).sum()),
            float(final_buy.max()) if len(final_buy) else 0.0,
            float(final_sell.max()) if len(final_sell) else 0.0,
            int((src_buy > 0).sum()),
            int((src_sell > 0).sum()),
            int((src_total > 0).sum()),
            int((src_total < 0).sum()),
        )
        logger.warning(
            "[SUMMARY AI CANDIDATES] score_config applied rows=%s buy_positive=%s sell_positive=%s buy_max=%.2f sell_max=%.2f",
            len(out),
            int((out["config_buy_score"] > 0).sum()),
            int((out["config_sell_score"] > 0).sum()),
            float(pd.to_numeric(out["config_buy_score"], errors="coerce").fillna(0.0).max()),
            float(pd.to_numeric(out["config_sell_score"], errors="coerce").fillna(0.0).max()),
        )
    except Exception:
        pass

    return out.reset_index(drop=True)


# ============================================================
# dataframe preparation
# ============================================================

def attach_display_like_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    if "symbol" not in out.columns:
        logger.warning("[SUMMARY AI CANDIDATES] missing symbol column")
        return pd.DataFrame()

    out["symbol"] = out["symbol"].map(normalize_symbol)
    out = out[out["symbol"].astype(str).str.strip() != ""].copy()
    if out.empty:
        return out

    if "symbolname_view" not in out.columns:
        symbolname = pick_text_series(out, ["symbolname", "name", "display_name"], "")
        out["symbolname_view"] = symbolname.mask(symbolname.str.strip().eq(""), out["symbol"])

    out["ai_disp_buy_score"] = pick_num_series(out, ["disp_buy_score", "score_buy", "buy_score", "buy"], 0.0)
    out["ai_disp_sell_score"] = pick_num_series(out, ["disp_sell_score", "score_sell", "sell_score", "sell"], 0.0).abs()
    out["ai_disp_score"] = pick_num_series(out, ["disp_score", "display_score", "score", "final_score"], 0.0)
    out["ai_disp_total_score"] = pick_num_series(out, ["disp_total_score", "score_total", "total_score", "combined_score", "final_score", "display_score", "score"], 0.0)
    if float(out["ai_disp_total_score"].abs().sum()) == 0.0:
        out["ai_disp_total_score"] = out["ai_disp_buy_score"] - out["ai_disp_sell_score"]
    out["ai_disp_final_score"] = pick_num_series(out, ["disp_final_score", "final_score", "display_score", "score_total", "score"], 0.0)
    out["ai_disp_close"] = pick_num_series(out, ["disp_close", "close", "close_price", "current_price", "price", "last_price"], 0.0)
    out["ai_disp_volume"] = pick_num_series(out, ["volume", "trading_volume", "出来高"], 0.0)
    out["ai_disp_turnover"] = pick_num_series(out, ["turnover", "trading_value", "売買代金", "ai_turnover"], 0.0)
    if float(out["ai_disp_turnover"].abs().sum()) == 0.0:
        out["ai_disp_turnover"] = out["ai_disp_close"] * out["ai_disp_volume"]
    out["ai_disp_slope"] = pick_num_series(out, ["disp_slope", "slope", "slope_atr_scaled", "score_slope"], 0.0)
    out["ai_disp_mtf"] = pick_num_series(out, ["disp_mtf", "score_mtf", "mtf_score", "mtf"], 0.0)
    out["ai_disp_rsi"] = pick_num_series(out, ["disp_rsi", "rsi", "RSI"], 50.0)
    out["ai_disp_macd"] = pick_num_series(out, ["disp_macd", "macd", "MACD"], 0.0)
    out["ai_disp_signal"] = pick_num_series(out, ["disp_signal", "signal", "macd_signal", "SIGNAL"], 0.0)
    out["ai_score_base"] = pick_num_series(out, ["disp_base", "score_base", "breakdown_base", "base"], 0.0)
    out["ai_score_trend"] = pick_num_series(out, ["disp_trend", "score_trend", "breakdown_trend", "trend"], 0.0)
    out["ai_score_momentum"] = pick_num_series(out, ["disp_mom", "score_momentum", "breakdown_mom", "mom", "momentum"], 0.0)
    out["ai_score_velocity"] = pick_num_series(out, ["disp_vel", "score_velocity", "breakdown_vel", "vel", "velocity"], 0.0)
    out["ai_score_penalty"] = pick_num_series(out, ["disp_pen", "score_penalty", "breakdown_pen", "pen", "penalty"], 0.0)

    if "datetime" in out.columns:
        try:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            try:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
            except Exception:
                pass
        except Exception:
            logger.debug("[SUMMARY AI CANDIDATES] datetime normalize failed", exc_info=True)

    out = _apply_score_config_flags(out)
    return out.reset_index(drop=True)


def filter_common_stock_rows(df: pd.DataFrame, *, require_buy_target: bool = False, exclude_etf_fund: bool = True, allowed_market_types: Optional[set[str]] = None) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    allowed_market_types = allowed_market_types or VALID_MARKET_TYPES

    try:
        if require_buy_target and "buy_target" in out.columns:
            out = out[out["buy_target"].map(lambda x: is_truthy(x, False))].copy()

        if exclude_etf_fund:
            for col in ("is_etf", "is_reit", "is_fund"):
                if col in out.columns:
                    out = out[~out[col].map(lambda x: is_truthy(x, False))].copy()

            name_col = next((c for c in ("symbolname_view", "symbolname", "name") if c in out.columns), None)
            if name_col:
                s = out[name_col].fillna("").astype(str).str.upper()
                mask = (
                    s.str.contains("ETF", na=False)
                    | s.str.contains("ETN", na=False)
                    | s.str.contains("REIT", na=False)
                    | s.str.contains("リート", na=False)
                    | s.str.contains("投信", na=False)
                    | s.str.contains("FUND", na=False)
                    | s.str.contains("ＦＵＮＤ", na=False)
                )
                out = out[~mask].copy()

        if "market_type" in out.columns:
            mt = out["market_type"].fillna("").astype(str).str.strip()
            out = out[mt.isin(allowed_market_types) | mt.eq("")].copy()

    except Exception:
        logger.debug("[SUMMARY AI CANDIDATES] common stock filter failed", exc_info=True)

    return out.reset_index(drop=True)


def dedupe_one_row_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    try:
        quality = pd.Series(0, index=out.index, dtype="int64")
        for col, weight in [
            ("ai_disp_buy_score", 10),
            ("ai_disp_sell_score", 10),
            ("ai_disp_total_score", 8),
            ("ai_disp_final_score", 8),
            ("ai_disp_close", 4),
            ("ai_disp_volume", 3),
            ("ai_disp_rsi", 2),
            ("ai_disp_macd", 2),
        ]:
            if col in out.columns:
                quality += pd.to_numeric(out[col], errors="coerce").notna().astype(int) * weight
        out["_ai_quality"] = quality

        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            try:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
            except Exception:
                pass
            out = out.sort_values(
                ["symbol", "datetime", "_ai_quality"],
                ascending=[True, False, False],
                na_position="last",
                kind="mergesort",
            )
        else:
            out = out.sort_values(
                ["symbol", "_ai_quality"],
                ascending=[True, False],
                na_position="last",
                kind="mergesort",
            )

        out = out.drop_duplicates(subset=["symbol"], keep="first")
        return out.drop(columns=["_ai_quality"], errors="ignore").reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY AI CANDIDATES] dedupe failed")
        return out.reset_index(drop=True)


def _filter_fresh_candidate_rows(df: pd.DataFrame) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out
    if not _env_bool("SUMMARY_AI_CANDIDATE_FRESHNESS_FILTER_ENABLED", True):
        return out
    if "datetime" not in out.columns:
        logger.warning("[SUMMARY AI CANDIDATES] freshness filter skipped reason=no_datetime rows=%s", len(out))
        return out

    max_age = _summary_ai_max_candidate_age_sec(900.0)
    if max_age <= 0:
        logger.warning("[SUMMARY AI CANDIDATES] freshness filter disabled max_age=%.1fs", max_age)
        return out

    try:
        dt_s = pd.to_datetime(out["datetime"], errors="coerce")
        try:
            dt_s = dt_s.dt.tz_localize(None)
        except Exception:
            pass

        now = pd.Timestamp.now()
        age_sec = (now - dt_s).dt.total_seconds()
        before = len(out)
        latest = dt_s.max()
        oldest = dt_s.min()

        keep = dt_s.notna() & (age_sec >= -60.0) & (age_sec <= float(max_age))
        out = out[keep].copy()
        out["datetime"] = dt_s[keep]

        logger.warning(
            "[SUMMARY AI CANDIDATES] freshness filter rows=%s->%s max_age=%.1fs latest=%s oldest=%s now=%s",
            before,
            len(out),
            float(max_age),
            latest,
            oldest,
            now,
        )
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY AI CANDIDATES] freshness filter failed; continue fail-open")
        return out.reset_index(drop=True)


def _prepare_base(summary_df: pd.DataFrame, *, require_buy_target: bool, exclude_etf_fund: bool) -> pd.DataFrame:
    df = attach_display_like_columns(summary_df)
    if df.empty:
        return df
    df = filter_common_stock_rows(df, require_buy_target=require_buy_target, exclude_etf_fund=exclude_etf_fund)
    if df.empty:
        return df
    df = _filter_fresh_candidate_rows(df)
    if df.empty:
        logger.warning("[SUMMARY AI CANDIDATES] no fresh rows after freshness filter")
        return df
    return dedupe_one_row_per_symbol(df)


# ============================================================
# candidate extraction
# ============================================================

def _buy_candidates_from_prepared(df: pd.DataFrame, *, interval: int | str, top_n: int, min_buy_score: float, max_sell_score: float, min_volume: float, min_price: float, source: str) -> pd.DataFrame:
    if df.empty:
        return df
    top_n = _resolve_top_n(top_n)
    resolved_min_price = max(float(min_price), _entry_min_price(DEFAULT_MIN_PRICE))
    resolved_min_buy_slope = _entry_min_buy_slope()
    before = len(df)

    base_mask = (
        (pd.to_numeric(df["ai_disp_buy_score"], errors="coerce").fillna(0.0) >= float(min_buy_score))
        & (pd.to_numeric(df["ai_disp_sell_score"], errors="coerce").fillna(0.0) <= float(max_sell_score))
        & (pd.to_numeric(df["ai_disp_close"], errors="coerce").fillna(0.0) > float(resolved_min_price))
        & (pd.to_numeric(df["ai_disp_volume"], errors="coerce").fillna(0.0) >= float(min_volume))
    )
    if not _is_ranking_source(source):
        base_mask = base_mask & (pd.to_numeric(df["ai_disp_slope"], errors="coerce").fillna(0.0) > float(resolved_min_buy_slope))

    out = df[base_mask].copy()
    if out.empty:
        try:
            top_buy = df.sort_values("ai_disp_buy_score", ascending=False).head(10)[[c for c in ("symbol", "datetime", "ai_disp_buy_score", "config_buy_score", "ai_disp_sell_score", "ai_disp_close", "ai_disp_volume", "ai_disp_slope") if c in df.columns]].to_dict(orient="records")
        except Exception:
            top_buy = []
        logger.warning(
            "[SUMMARY AI CANDIDATES] BUY empty interval=%s source=%s before=%s min_buy=%.2f max_sell=%.2f min_price=%.1f min_volume=%.1f ranking_source=%s slope_gate=%s top_buy=%s",
            interval,
            source,
            before,
            float(min_buy_score),
            float(max_sell_score),
            float(resolved_min_price),
            float(min_volume),
            _is_ranking_source(source),
            not _is_ranking_source(source),
            top_buy,
        )
        return out
    out["_ai_sort_score"] = (
        pd.to_numeric(out["ai_disp_buy_score"], errors="coerce").fillna(0.0) * 10.0
        + pd.to_numeric(out["ai_disp_total_score"], errors="coerce").fillna(0.0) * 3.0
        + pd.to_numeric(out["ai_disp_mtf"], errors="coerce").fillna(0.0)
        + pd.to_numeric(out["ai_disp_slope"], errors="coerce").fillna(0.0)
        - pd.to_numeric(out["ai_disp_sell_score"], errors="coerce").fillna(0.0) * 5.0
    )
    sort_cols = ["_ai_sort_score", "ai_disp_buy_score", "ai_disp_total_score"]
    ascending = [False, False, False]
    if "datetime" in out.columns:
        sort_cols = ["datetime"] + sort_cols
        ascending = [False] + ascending
    out = out.sort_values(sort_cols, ascending=ascending, na_position="last", kind="mergesort").head(top_n)
    out = out.drop(columns=["_ai_sort_score"], errors="ignore").reset_index(drop=True)
    out["ai_side"] = "BUY"
    out["side"] = "BUY"
    out["entry_decision"] = "BUY"
    logger.warning("[SUMMARY AI CANDIDATES] BUY_TOP_READY interval=%s source=%s count=%s top_n=%s symbols=%s", interval, source, len(out), top_n, _safe_symbols(out, top_n))
    return out


def _sell_candidates_from_prepared(df: pd.DataFrame, *, interval: int | str, top_n: int, min_sell_score: float, max_buy_score: float, min_volume: float, min_price: float, source: str) -> pd.DataFrame:
    if df.empty:
        return df
    top_n = _resolve_top_n(top_n)
    resolved_min_price = max(float(min_price), _entry_min_price(DEFAULT_MIN_PRICE))
    resolved_max_sell_slope = _entry_max_sell_slope()
    before = len(df)

    base_mask = (
        (pd.to_numeric(df["ai_disp_sell_score"], errors="coerce").fillna(0.0) >= float(min_sell_score))
        & (pd.to_numeric(df["ai_disp_buy_score"], errors="coerce").fillna(0.0) <= float(max_buy_score))
        & (pd.to_numeric(df["ai_disp_close"], errors="coerce").fillna(0.0) > float(resolved_min_price))
        & (pd.to_numeric(df["ai_disp_volume"], errors="coerce").fillna(0.0) >= float(min_volume))
    )
    if not _is_ranking_source(source):
        base_mask = base_mask & (pd.to_numeric(df["ai_disp_slope"], errors="coerce").fillna(0.0) < float(resolved_max_sell_slope))

    out = df[base_mask].copy()
    if out.empty:
        try:
            top_sell = df.sort_values("ai_disp_sell_score", ascending=False).head(10)[[c for c in ("symbol", "datetime", "ai_disp_sell_score", "config_sell_score", "ai_disp_buy_score", "ai_disp_close", "ai_disp_volume", "ai_disp_slope") if c in df.columns]].to_dict(orient="records")
        except Exception:
            top_sell = []
        logger.warning(
            "[SUMMARY AI CANDIDATES] SELL empty interval=%s source=%s before=%s ranking_source=%s slope_gate=%s top_sell=%s",
            interval,
            source,
            before,
            _is_ranking_source(source),
            not _is_ranking_source(source),
            top_sell,
        )
        return out
    out["_ai_sort_score"] = (
        pd.to_numeric(out["ai_disp_sell_score"], errors="coerce").fillna(0.0) * 10.0
        - pd.to_numeric(out["ai_disp_buy_score"], errors="coerce").fillna(0.0) * 5.0
        - pd.to_numeric(out["ai_disp_slope"], errors="coerce").fillna(0.0) * 3.0
        - pd.to_numeric(out["ai_disp_total_score"], errors="coerce").fillna(0.0)
    )
    sort_cols = ["_ai_sort_score", "ai_disp_sell_score"]
    ascending = [False, False]
    if "datetime" in out.columns:
        sort_cols = ["datetime"] + sort_cols
        ascending = [False] + ascending
    out = out.sort_values(sort_cols, ascending=ascending, na_position="last", kind="mergesort").head(top_n)
    out = out.drop(columns=["_ai_sort_score"], errors="ignore").reset_index(drop=True)
    out["ai_side"] = "SELL"
    out["side"] = "SELL"
    out["entry_decision"] = "SELL"
    logger.warning("[SUMMARY AI CANDIDATES] SELL_TOP_READY interval=%s source=%s count=%s top_n=%s symbols=%s", interval, source, len(out), top_n, _safe_symbols(out, top_n))
    return out


def build_summary_ai_entry_candidates(
    summary_df: pd.DataFrame,
    *,
    interval: int | str = 1,
    top_n: int = DEFAULT_TOP_N,
    min_buy_score: float = DEFAULT_MIN_BUY_SCORE,
    max_sell_score: float = DEFAULT_MAX_SELL_SCORE,
    min_volume: float = DEFAULT_MIN_VOLUME,
    min_price: float = DEFAULT_MIN_PRICE,
    require_buy_target: bool = False,
    exclude_etf_fund: bool = True,
    source: str = "SUMMARY",
) -> pd.DataFrame:
    top_n = _resolve_top_n(top_n)

    base = _prepare_base(summary_df, require_buy_target=require_buy_target, exclude_etf_fund=exclude_etf_fund)
    if base.empty:
        logger.info("[SUMMARY AI CANDIDATES] no rows after base prepare interval=%s source=%s", interval, source)
        return base

    buy_df = _buy_candidates_from_prepared(
        base,
        interval=interval,
        top_n=top_n,
        min_buy_score=min_buy_score,
        max_sell_score=max_sell_score,
        min_volume=min_volume,
        min_price=min_price,
        source=source,
    )
    sell_df = _sell_candidates_from_prepared(
        base,
        interval=interval,
        top_n=top_n,
        min_sell_score=0.01,
        max_buy_score=999999.0,
        min_volume=min_volume,
        min_price=min_price,
        source=source,
    )

    frames = [x for x in (buy_df, sell_df) if isinstance(x, pd.DataFrame) and not x.empty]
    if not frames:
        logger.warning("[SUMMARY AI CANDIDATES] BUY_SELL combined empty interval=%s source=%s base_rows=%s", interval, source, len(base))
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True, sort=False)
    logger.warning(
        "[SUMMARY AI CANDIDATES] BUY_SELL_COMBINED_READY interval=%s source=%s buy_count=%s sell_count=%s total=%s top_n_each=%s buy_symbols=%s sell_symbols=%s",
        interval,
        source,
        len(buy_df) if isinstance(buy_df, pd.DataFrame) else 0,
        len(sell_df) if isinstance(sell_df, pd.DataFrame) else 0,
        len(out),
        top_n,
        _safe_symbols(buy_df, top_n),
        _safe_symbols(sell_df, top_n),
    )
    return out.reset_index(drop=True)


def build_summary_ai_sell_entry_candidates(
    summary_df: pd.DataFrame,
    *,
    interval: int | str = 1,
    top_n: int = DEFAULT_TOP_N,
    min_sell_score: float = 0.01,
    max_buy_score: float = 2.0,
    min_volume: float = DEFAULT_MIN_VOLUME,
    min_price: float = DEFAULT_MIN_PRICE,
    require_buy_target: bool = False,
    exclude_etf_fund: bool = True,
    source: str = "SUMMARY",
) -> pd.DataFrame:
    top_n = _resolve_top_n(top_n)
    base = _prepare_base(summary_df, require_buy_target=require_buy_target, exclude_etf_fund=exclude_etf_fund)
    if base.empty:
        return base
    return _sell_candidates_from_prepared(
        base,
        interval=interval,
        top_n=top_n,
        min_sell_score=min_sell_score,
        max_buy_score=max_buy_score,
        min_volume=min_volume,
        min_price=min_price,
        source=source,
    )


__all__ = [
    "DEFAULT_TOP_N",
    "DEFAULT_MIN_BUY_SCORE",
    "DEFAULT_MAX_SELL_SCORE",
    "DEFAULT_MIN_VOLUME",
    "DEFAULT_MIN_PRICE",
    "DEFAULT_MIN_BUY_SLOPE",
    "DEFAULT_MAX_SELL_SLOPE",
    "attach_display_like_columns",
    "filter_common_stock_rows",
    "dedupe_one_row_per_symbol",
    "build_summary_ai_entry_candidates",
    "build_summary_ai_sell_entry_candidates",
]
