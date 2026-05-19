# ============================================================
# File   : trading/scoring/core/flag_pattern_score_bridge.py
# Version: Ver01-FLAGS-AND-PATTERNS-TO-SCORE
# ------------------------------------------------------------
# trading/scoring/flags と trading/scoring/patterns をスコアへ反映する。
#
# 方針:
#   - flags 配下は generate_all_flags() で生成
#   - patterns 配下は pattern_dispatcher.dispatch_patterns() があれば呼ぶ
#   - score_config.ini を唯一の点数定義源にする
#   - BUY は score_base / flag_score へ加点
#   - SELL は direction_penalty / sell_pressure へ加算
#   - score_total はここでは直接確定しない
#     後段 scoring_pipeline が base+trend+mom+vel-pen で合成する
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _ensure_dataframe(df: Any) -> pd.DataFrame:
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
            out.columns = ["_".join([str(x) for x in col if x not in ("", None)]) for col in out.columns.to_flat_index()]
    except Exception:
        pass
    try:
        out.columns = [str(c) for c in out.columns]
        if out.columns.duplicated().any():
            out = out.loc[:, ~out.columns.duplicated(keep="last")].copy()
    except Exception:
        pass
    return out.reset_index(drop=True)


def _safe_num_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    if col not in df.columns:
        return pd.Series(default, index=df.index, dtype="float64")
    try:
        s = df[col]
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
        return pd.to_numeric(s, errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(default).astype("float64")
    except Exception:
        return pd.Series(default, index=df.index, dtype="float64")


def _flag_on_mask(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series(False, index=df.index, dtype=bool)
    try:
        s = df[col]
        if isinstance(s, pd.DataFrame):
            s = s.iloc[:, 0]
        num = pd.to_numeric(s, errors="coerce")
        text = s.astype(str).str.strip().str.lower()
        return (num.fillna(0.0).ne(0.0) | text.isin({"1", "true", "t", "yes", "y", "on", "ok", "pass", "passed"})).fillna(False).astype(bool)
    except Exception:
        return pd.Series(False, index=df.index, dtype=bool)


def _norm_key(key: str) -> str:
    return str(key or "").strip().lower()


def _load_tables() -> Dict[str, Dict[str, int]]:
    try:
        from trading.scoring.config.score_table import build_score_tables
        tables = build_score_tables()
        return {
            "buy_entry": dict(tables.get("buy_entry", {})),
            "buy_bonus": dict(tables.get("buy_bonus", {})),
            "sell_entry": dict(tables.get("sell_entry", {})),
            "sell_bonus": dict(tables.get("sell_bonus", {})),
        }
    except Exception:
        logger.exception("[FLAG/PATTERN SCORE BRIDGE] load score tables failed")
        return {"buy_entry": {}, "buy_bonus": {}, "sell_entry": {}, "sell_bonus": {}}


def _score_from_tables(flag: str, tables: Dict[str, Dict[str, int]]) -> Tuple[str, float, str]:
    key = _norm_key(flag)
    aliases = {key}
    if key.startswith("flag_"):
        aliases.add(key[5:])
    else:
        aliases.add("flag_" + key)

    for section in ("buy_entry", "buy_bonus"):
        for a in aliases:
            if a in tables.get(section, {}):
                return "BUY", float(abs(tables[section][a])), section

    for section in ("sell_entry", "sell_bonus"):
        for a in aliases:
            if a in tables.get(section, {}):
                return "SELL", float(abs(tables[section][a])), section

    return "", 0.0, ""


_SELL_WORDS = (
    "bear", "black_crows", "evening", "shooting", "hanging", "dark_cloud",
    "double_top", "upper_wick", "breakdown", "down", "negative", "fail",
    "reject", "resistance", "ask", "sell", "exit", "reversal_warning",
    "volume_drop", "peak_out", "exhaustion", "lower_high", "lower_low",
    "below", "dead_cross", "dc", "falling", "overbought",
)

_BUY_WORDS = (
    "bull", "white_soldiers", "morning", "hammer", "dragonfly", "rising",
    "double_bottom", "triple_bottom", "inverse_head", "lower_wick", "breakout",
    "up", "positive", "reclaim", "support", "bid", "buy", "rebound",
    "oversold", "ranking", "relative_strength", "outperform", "mtf", "tf3", "tf5",
    "tosama", "volume_spike", "volume_surge", "volume_expansion", "tick_surge",
    "trade_count_spike", "phase", "fakeout", "retest", "higher_high", "higher_low",
    "above", "macd_cross", "vwap_break", "trend_strength",
)


def _fallback_score(flag: str) -> Tuple[str, float, str]:
    if not _env_bool("SCORING_FLAG_PATTERN_UNKNOWN_ENABLED", True):
        return "", 0.0, ""
    key = _norm_key(flag)
    default_buy = _env_float("SCORING_FLAG_PATTERN_UNKNOWN_BUY_POINTS", 1.0)
    default_sell = _env_float("SCORING_FLAG_PATTERN_UNKNOWN_SELL_POINTS", 1.0)
    pattern_points = _env_float("SCORING_FLAG_PATTERN_UNKNOWN_PATTERN_POINTS", 2.0)

    if any(w in key for w in _SELL_WORDS):
        pts = pattern_points if any(w in key for w in ("bear", "black_crows", "evening", "shooting", "double_top", "upper_wick")) else default_sell
        return "SELL", float(abs(pts)), "fallback_sell"

    if any(w in key for w in _BUY_WORDS):
        pts = pattern_points if any(w in key for w in ("bull", "white_soldiers", "morning", "hammer", "double_bottom", "lower_wick")) else default_buy
        return "BUY", float(abs(pts)), "fallback_buy"

    return "", 0.0, ""


def _clip(s: pd.Series, limit: float) -> pd.Series:
    try:
        lim = abs(float(limit))
        return pd.to_numeric(s, errors="coerce").fillna(0.0).clip(-lim, lim)
    except Exception:
        return s


def _apply_pattern_dispatcher(out: pd.DataFrame) -> pd.DataFrame:
    if not _env_bool("SCORING_PATTERN_DISPATCHER_ENABLED", True):
        return out
    before = _safe_num_series(out, "score_total")
    try:
        from trading.scoring.patterns.pattern_dispatcher import dispatch_patterns
        x = dispatch_patterns(out.copy())
        if isinstance(x, pd.DataFrame) and not x.empty:
            out = x
    except Exception:
        logger.exception("[FLAG/PATTERN SCORE BRIDGE] pattern dispatcher failed")
        return out

    after = _safe_num_series(out, "score_total")
    delta = after - before
    out["pattern_score_delta"] = _safe_num_series(out, "pattern_score_delta") + delta if "pattern_score_delta" in out.columns else delta

    buy = delta.clip(lower=0.0)
    sell = (-delta).clip(lower=0.0)
    for col in ("score_base", "base", "flag_score"):
        if col not in out.columns:
            out[col] = 0.0
        out[col] = _safe_num_series(out, col) + buy
    for col in ("direction_penalty", "pen", "sell_pressure"):
        if col not in out.columns:
            out[col] = 0.0
        out[col] = _safe_num_series(out, col) + sell

    # 後段 scoring_pipeline で二重加算しないよう、score_total は元に戻す。
    out["score_total"] = before
    logger.info("[FLAG/PATTERN SCORE BRIDGE] pattern dispatcher applied rows=%s buy_nonzero=%s sell_nonzero=%s", len(out), int(buy.ne(0).sum()), int(sell.ne(0).sum()))
    return out


def attach_flag_and_pattern_scores(df: Any) -> pd.DataFrame:
    out = _ensure_dataframe(df)
    if out.empty:
        return out

    if not _env_bool("SCORING_FLAG_PATTERN_BRIDGE_ENABLED", True):
        return out

    try:
        from trading.scoring.flags.flag_generator import generate_all_flags
        out = generate_all_flags(out)
    except Exception:
        logger.exception("[FLAG/PATTERN SCORE BRIDGE] generate_all_flags failed")

    out = _apply_pattern_dispatcher(out)

    flag_cols = [c for c in out.columns if str(c).startswith("flag_")]
    if not flag_cols:
        logger.warning("[FLAG/PATTERN SCORE BRIDGE] no flag columns found")
        return out

    tables = _load_tables()
    buy_total = pd.Series(0.0, index=out.index, dtype="float64")
    sell_total = pd.Series(0.0, index=out.index, dtype="float64")
    reason_map = {int(i): [] for i in out.index}
    active_count = 0
    scored_count = 0
    fallback_count = 0

    for col in flag_cols:
        mask = _flag_on_mask(out, col)
        if not bool(mask.any()):
            continue
        active_count += 1
        side, points, source = _score_from_tables(col, tables)
        if not side:
            side, points, source = _fallback_score(col)
            if side:
                fallback_count += 1
        if not side or points == 0:
            continue
        scored_count += 1
        if side == "SELL":
            sell_total.loc[mask] += abs(points)
        else:
            buy_total.loc[mask] += abs(points)
        for idx in out.index[mask]:
            try:
                reason_map[int(idx)].append(f"{col}:{side}:{points:g}:{source}")
            except Exception:
                pass

    cap = _env_float("SCORING_FLAG_PATTERN_MAX_POINTS_PER_SIDE", 30.0)
    buy_total = _clip(buy_total, cap)
    sell_total = _clip(sell_total, cap)

    for col in ("score_base", "base", "flag_score"):
        if col not in out.columns:
            out[col] = 0.0
        out[col] = _safe_num_series(out, col) + buy_total

    for col in ("direction_penalty", "pen", "sell_pressure"):
        if col not in out.columns:
            out[col] = 0.0
        out[col] = _safe_num_series(out, col) + sell_total

    out["flag_score_buy_total"] = buy_total
    out["flag_score_sell_total"] = sell_total
    out["flag_score_total_delta"] = buy_total - sell_total
    out["flag_score_reasons"] = [" / ".join(reason_map.get(int(i), [])) for i in out.index]

    logger.info(
        "[FLAG/PATTERN SCORE BRIDGE] attached rows=%s flag_cols=%s active_flags=%s scored_flags=%s fallback_flags=%s buy_nonzero=%s sell_nonzero=%s cap=%.1f",
        len(out), len(flag_cols), active_count, scored_count, fallback_count,
        int(buy_total.ne(0).sum()), int(sell_total.ne(0).sum()), cap,
    )
    return out


__all__ = ["attach_flag_and_pattern_scores"]
