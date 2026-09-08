# ============================================================
# File   : trading/entry/summary_ai/candidates.py
# Version: PRODUCTION-STABLE-REV2.6-SUMMARY-1M-MOVEMENT-SCORE-BRIDGE-STRONG
# ------------------------------------------------------------
# Purpose:
#   - SUMMARY / RANKING SUMMARY の DataFrame からAI gate候補を作る。
#   - build_summary_ai_entry_candidates() で BUY TOP と SELL TOP を同時に返す。
#   - 各行に ai_side / side = BUY or SELL を付与し、AI gate側で行ごとに判定する。
#
# REV2.6:
#   - PUSH 1m memory summary の score/slope/mtf が全0の場合でも、
#     open/high/low/close/range_pct/atr/macd/volume から候補生成専用スコアを作る。
#   - REV2.5 の movement score は倍率が弱く、MIN_ENTRY_SCORE=3.0 環境で
#     BUY/SELL候補が0件になりやすかったため、候補化できる強度へ補正。
#   - ガード緩和ではない。低出来高・低変動・blowoff・板・order_builder の
#     最終除外は後段に残す。
# ============================================================
from __future__ import annotations

import logging
import os
import sys
import time
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
VERSION = "REV2.7-SUMMARY-1M-MOVEMENT-SCORE-BRIDGE-STRONG-RANGE-PCT-PERCENT"

# range_pct 系の列はすべて percent 単位 (1.88% = 1.88)。
# 単位の定義は trading/entry/summary_ai/range_defs.py と揃えること。
RANGE_PCT_SCALE = 100.0
RANGE_PCT_RATIO_HINT = 1.0

# 旧 tonosama_history_failclose_strict_patch._patch_summary_ai() の定数表。
STRICT_CANDIDATE_CONSTANTS: dict[str, float] = {
    "DEFAULT_MIN_BUY_SCORE": 3.0,
    "DEFAULT_MAX_SELL_SCORE": 3.0,
    "DEFAULT_MIN_BUY_SLOPE": 0.0,
    "DEFAULT_MAX_SELL_SLOPE": 0.0,
}


def apply_strict_candidate_constants() -> dict[str, tuple[Any, Any]]:
    """旧 tonosama_history_failclose_strict_patch._patch_summary_ai() の定数部を本文化。

    core/startup/strict_entry_defaults.py が install 時と watcher から毎秒呼ぶ。
    これらは関数シグネチャの既定値としても使われるが、既定値は import 時に
    束縛済みなので、実効があるのは実行時に globals() を読む経路
    (_resolve_min_buy_slope / _resolve_max_sell_slope など) 側。
    """
    changed: dict[str, tuple[Any, Any]] = {}
    for name, value in STRICT_CANDIDATE_CONSTANTS.items():
        old = globals().get(name)
        if old != value:
            globals()[name] = value
            changed[name] = (old, value)
    return changed


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
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
    except Exception:
        pass
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
        return str(v).strip().lower() in {"1", "true", "t", "yes", "y", "on", "ok"}
    except Exception:
        return False


def _norm_key(key: str) -> str:
    return str(key or "").strip().lower()


def _flag_variants(key: str) -> list[str]:
    k = _norm_key(key)
    if not k:
        return []
    out = [k, k[5:]] if k.startswith("flag_") else [k, f"flag_{k}"]
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
        out[name] = {str(k): int(v) for k, v in t.items() if isinstance(v, int) or str(v).lstrip("-").isdigit()} if isinstance(t, dict) else {}
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


def _movement_score_fallback(out: pd.DataFrame, final_buy: pd.Series, final_sell: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series, pd.Series]:
    """When explicit score columns are all zero, derive candidate-only scores from real 1m movement."""
    if not _env_bool("SUMMARY_AI_DERIVE_SCORE_FROM_1M_MOVEMENT", True):
        return final_buy, final_sell, pd.Series(False, index=out.index), pd.Series(0.0, index=out.index), pd.Series(0.0, index=out.index)
    try:
        if int(((final_buy.abs() > 0) | (final_sell.abs() > 0)).sum()) > 0:
            return final_buy, final_sell, pd.Series(False, index=out.index), pd.Series(0.0, index=out.index), pd.Series(0.0, index=out.index)

        close = _num(out, ("close", "close_price", "current_price", "price", "ai_disp_close"), 0.0)
        open_ = _num(out, ("open", "open_price", "day_open"), 0.0)
        high = _num(out, ("high", "high_price", "day_high"), 0.0)
        low = _num(out, ("low", "low_price", "day_low"), 0.0)
        volume = _num(out, ("volume", "trading_volume", "ai_disp_volume"), 0.0)
        turnover = _num(out, ("turnover", "trading_value", "ai_disp_turnover"), 0.0)
        slope = _num(out, ("slope", "slope_atr_scaled", "score_slope", "ai_disp_slope"), 0.0)
        macd = _num(out, ("macd", "MACD", "ai_disp_macd"), 0.0)
        signal = _num(out, ("signal", "macd_signal", "ai_disp_signal"), 0.0)
        atr = _num(out, ("atr", "atr_1m", "ATR"), 0.0)
        range_pct_col = _num(out, ("range_pct", "intraday_range_pct", "day_range_pct"), 0.0)

        safe_close = close.where(close > 0, 1.0)
        open_valid = open_.where(open_ > 0, close)
        co_pct = ((close - open_valid) / open_valid.where(open_valid > 0, 1.0)).fillna(0.0)
        hl_pct = ((high - low) / safe_close).where((high > 0) & (low > 0) & (high >= low), 0.0).fillna(0.0)
        # range_pct 列は percent 単位 (2026-09-01 に統一)。
        # ここから下の内部計算は比率 (0-1) なので 100 で割って揃える。
        # 割らずに混ぜると raw_strength が 100 倍になり、
        # movement_ok が常に True になって低変動ガードが効かなくなる。
        range_pct = _max_series(hl_pct.abs(), (range_pct_col.abs() / RANGE_PCT_SCALE))
        atr_pct = (atr.abs() / safe_close).fillna(0.0)
        macd_diff = ((macd - signal) / safe_close).fillna(0.0)

        min_range = _env_float("SUMMARY_AI_MOVEMENT_SCORE_MIN_RANGE_PCT", 0.0015)
        min_turnover = _env_float("SUMMARY_AI_MOVEMENT_SCORE_MIN_TURNOVER", 1_000_000.0)
        min_volume = _env_float("SUMMARY_AI_MOVEMENT_SCORE_MIN_VOLUME", 3_000.0)
        min_raw_strength = _env_float("SUMMARY_AI_MOVEMENT_SCORE_MIN_SCORE", 0.20)
        candidate_floor = _env_float("SUMMARY_AI_MOVEMENT_SCORE_CANDIDATE_FLOOR", _env_float("MIN_ENTRY_SCORE", 3.0))
        max_score = _env_float("SUMMARY_AI_MOVEMENT_SCORE_MAX_SCORE", 6.0)

        raw_strength = _max_series(
            range_pct * 2500.0,
            atr_pct * 2200.0,
            co_pct.abs() * 2200.0,
            slope.abs() * 600.0,
            macd_diff.abs() * 600.0,
        ).clip(lower=0.0, upper=max_score)
        movement_ok = (close > 0) & (raw_strength >= min_raw_strength) & ((range_pct >= min_range) | (atr_pct >= min_range) | (co_pct.abs() >= min_range))
        movement_ok = movement_ok & ((volume >= min_volume) | (turnover >= min_turnover))

        close_pos = ((close - low) / (high - low).where((high - low) > 0, 1.0)).fillna(0.5)
        direction = co_pct + slope + macd_diff
        buy_dir = (direction > 0) | ((direction.abs() <= 1e-12) & (close_pos >= 0.5))
        sell_dir = (direction < 0) | ((direction.abs() <= 1e-12) & (close_pos < 0.5))

        movement_strength = raw_strength.where(~movement_ok, raw_strength.clip(lower=candidate_floor, upper=max_score))
        fb = final_buy.copy().mask(movement_ok & buy_dir, movement_strength)
        fs = final_sell.copy().mask(movement_ok & sell_dir, movement_strength)

        derived_slope = direction.where(direction.abs() > 1e-12, (close_pos - 0.5) * range_pct).fillna(0.0)
        if float(_num(out, ("ai_disp_slope", "slope", "slope_atr_scaled", "score_slope"), 0.0).abs().sum()) == 0.0:
            out["ai_disp_slope"] = derived_slope
            out["slope"] = derived_slope
            out["slope_atr_scaled"] = derived_slope
            out["score_slope"] = derived_slope
        if float(_num(out, ("ai_disp_mtf", "mtf", "score_mtf", "mtf_score"), 0.0).abs().sum()) == 0.0:
            mtf = movement_strength.where(movement_ok, 0.0)
            out["ai_disp_mtf"] = mtf
            out["mtf"] = mtf
            out["score_mtf"] = mtf
            out["mtf_score"] = mtf

        logger.warning(
            "[SUMMARY AI MOVEMENT SCORE BRIDGE] derived rows=%s buy_positive=%s sell_positive=%s min_range=%.6f min_volume=%.0f min_turnover=%.0f floor=%.2f buy_max=%.3f sell_max=%.3f version=%s",
            int(movement_ok.sum()), int((fb > 0).sum()), int((fs > 0).sum()), min_range, min_volume, min_turnover, candidate_floor,
            float(fb.max()) if len(fb) else 0.0, float(fs.max()) if len(fs) else 0.0, VERSION,
        )
        return fb, fs, movement_ok, range_pct, movement_strength
    except Exception:
        logger.exception("[SUMMARY AI MOVEMENT SCORE BRIDGE] failed")
        return final_buy, final_sell, pd.Series(False, index=out.index), pd.Series(0.0, index=out.index), pd.Series(0.0, index=out.index)


def _apply_score_config_flags(df: pd.DataFrame) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

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
            logger.warning("[SUMMARY AI CANDIDATES] score_config tables empty; use existing/movement scores only")
    else:
        logger.warning("[SUMMARY AI CANDIDATES] score_config flag scoring disabled by env; use existing/movement scores only")

    final_buy = _max_series(src_buy, cfg_buy, total_buy)
    final_sell = _max_series(src_sell, cfg_sell, total_sell).abs()
    final_buy, final_sell, movement_ok, movement_range_pct, movement_strength = _movement_score_fallback(out, final_buy, final_sell)
    final_total = final_buy - final_sell

    out["config_buy_entry_score"] = cfg_buy
    out["config_buy_bonus_score"] = 0.0
    out["config_buy_score"] = _max_series(cfg_buy, src_buy, total_buy, final_buy)
    out["config_sell_entry_score"] = cfg_sell
    out["config_sell_bonus_score"] = 0.0
    out["config_sell_score"] = _max_series(cfg_sell, src_sell, total_sell, final_sell).abs()
    out["config_buy_hits"] = buy_hits_col
    out["config_sell_hits"] = sell_hits_col
    out["summary_ai_movement_score_ok"] = movement_ok
    out["summary_ai_movement_range_pct"] = movement_range_pct
    out["summary_ai_movement_strength"] = movement_strength

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
            "[SUMMARY AI SCORE BRIDGE CORE] applied rows=%s buy_positive=%s sell_positive=%s buy_max=%.2f sell_max=%.2f src_buy_pos=%s src_sell_pos=%s src_total_pos=%s src_total_neg=%s version=%s",
            len(out), int((final_buy > 0).sum()), int((final_sell > 0).sum()),
            float(final_buy.max()) if len(final_buy) else 0.0, float(final_sell.max()) if len(final_sell) else 0.0,
            int((src_buy > 0).sum()), int((src_sell > 0).sum()), int((src_total > 0).sum()), int((src_total < 0).sum()), VERSION,
        )
    except Exception:
        pass
    return out.reset_index(drop=True)


def attach_display_like_columns_core(df: pd.DataFrame) -> pd.DataFrame:
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


# ============================================================
# SUMMARY AI candidate zero-score rescue
#   旧 core/startup/summary_ai_liquidity_rescue_patch.py の後半 (V1)
# ------------------------------------------------------------
# score が全ゼロ / slope が全ゼロのとき、候補抽出そのものが成立しなくなる。
# MTF + 現在足方向 + MACD方向 + レンジから候補抽出用のスコアと疑似 slope を
# 作って、抽出だけは通るようにする。実売買の判定は後段ガードが行う。
#
# 旧パッチは sitecustomize の SUMMARY_AI_RESCUE_PATCHES = opt-in グループ
# (既定 OFF) で install されていた。そのスコープを再現する。
# ============================================================
_ZERO_SCORE_RESCUE_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)
_ZERO_SCORE_RESCUE_DB_PROCESS_ENVS = (
    "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
    "AUTOSTOCK_SUMMARY_DB_WRITER",
    "AUTOSTOCK_MAIN_DATABASE_PROCESS",
    "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
)


def _zero_score_rescue_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _zero_score_rescue_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _zero_score_rescue_scope_enabled() -> bool:
    """このプロセスでゼロスコア救済を使うか。

    旧パッチは SUMMARY_AI_RESCUE_PATCHES (opt-in、既定 OFF) 経由で、
    main lite の background loop か SITECUSTOMIZE_ENABLE_FULL_NONMAIN=1 の
    ときだけ install されていた。
    SUMMARY_AI_ZERO_SCORE_RESCUE_SCOPE_ENABLED=1/0 で明示上書きできる。
    """
    try:
        raw = os.getenv("SUMMARY_AI_ZERO_SCORE_RESCUE_SCOPE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return _zero_score_rescue_env_bool("SUMMARY_AI_ZERO_SCORE_RESCUE_SCOPE_ENABLED", False)
        if str(os.environ.get("DISABLE_SUMMARY_AI_LIQ_RESCUE_PATCH", "")).strip() == "1":
            return False
        if not (
            _zero_score_rescue_env_bool("SITECUSTOMIZE_ENABLE_RESCUE_PATCHES", False)
            or _zero_score_rescue_env_bool("SITECUSTOMIZE_ENABLE_SUMMARY_AI_RESCUE_PATCHES", False)
        ):
            return False
        argv_list = [str(x).replace("\\", "/").lower() for x in sys.argv]
        argv = " ".join(argv_list)
        if any(name in argv for name in _ZERO_SCORE_RESCUE_DB_PROCESS_NAMES):
            return False
        if any(_zero_score_rescue_env_bool(name, False) for name in _ZERO_SCORE_RESCUE_DB_PROCESS_ENVS):
            return False
        if any(a.endswith("/main.py") or a == "main.py" for a in argv_list) and _zero_score_rescue_env_bool("SITECUSTOMIZE_MAIN_LITE", True):
            return True
        return _zero_score_rescue_env_bool("SITECUSTOMIZE_ENABLE_FULL_NONMAIN", False)
    except Exception:
        return False


def _apply_zero_score_rescue(out: pd.DataFrame) -> pd.DataFrame:
    if not _zero_score_rescue_env_bool("SUMMARY_AI_ZERO_SCORE_RESCUE_ENABLED", True):
        return out
    try:
        import numpy as np

        if out is None or not isinstance(out, pd.DataFrame) or out.empty:
            return out

        work = out.copy()

        def num(col: str, default: float = 0.0):
            if col in work.columns:
                return pd.to_numeric(work[col], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(default)
            return pd.Series(default, index=work.index, dtype="float64")

        buy = num("ai_disp_buy_score")
        sell = num("ai_disp_sell_score").abs()
        slope = num("ai_disp_slope")
        close = num("ai_disp_close")
        volume = num("ai_disp_volume")
        turnover = num("ai_disp_turnover")
        open_px = num("open", np.nan)
        if open_px.isna().all():
            open_px = num("open_price", np.nan)
        high = num("high", np.nan)
        if high.isna().all():
            high = num("high_price", np.nan)
        low = num("low", np.nan)
        if low.isna().all():
            low = num("low_price", np.nan)
        mtf = num("ai_disp_mtf")
        macd = num("ai_disp_macd")
        sig = num("ai_disp_signal")

        min_vol = _zero_score_rescue_env_float("SUMMARY_AI_ZERO_SCORE_RESCUE_MIN_VOLUME", 3000.0)
        min_turnover = _zero_score_rescue_env_float("SUMMARY_AI_ZERO_SCORE_RESCUE_MIN_TURNOVER", 1000000.0)
        min_score = _zero_score_rescue_env_float("SUMMARY_AI_ZERO_SCORE_RESCUE_MIN_SCORE", 4.1)

        liquid = (volume >= min_vol) | (turnover >= min_turnover)
        price_ok = close > 0
        delta_pct = ((close - open_px) / open_px.replace(0, np.nan) * 100.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        range_pct = ((high - low) / close.replace(0, np.nan) * 100.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        macd_bias = (macd - sig).fillna(0.0)

        # score が全ゼロなら、MTF + 現在足方向 + MACD方向 + レンジを候補抽出用スコアへ変換する。
        score_all_zero = float(buy.abs().sum() + sell.abs().sum()) <= 0.000001
        if score_all_zero:
            buy_dir = liquid & price_ok & ((delta_pct > 0) | (macd_bias > 0))
            sell_dir = liquid & price_ok & ((delta_pct < 0) | (macd_bias < 0))
            base_strength = mtf.clip(lower=0.0) + range_pct.clip(lower=0.0, upper=3.0) + macd_bias.abs().clip(lower=0.0, upper=2.0)
            rescued_buy = base_strength.where(buy_dir, 0.0).clip(lower=0.0)
            rescued_sell = base_strength.where(sell_dir, 0.0).clip(lower=0.0)
            rescued_buy = rescued_buy.where(rescued_buy <= 0.0, rescued_buy.clip(lower=min_score))
            rescued_sell = rescued_sell.where(rescued_sell <= 0.0, rescued_sell.clip(lower=min_score))
            work["ai_disp_buy_score"] = rescued_buy
            work["ai_disp_sell_score"] = rescued_sell
            work["score_buy"] = rescued_buy
            work["buy_score"] = rescued_buy
            work["score_sell"] = rescued_sell
            work["sell_score"] = rescued_sell
            work["score_total"] = rescued_buy - rescued_sell
            work["total_score"] = work["score_total"]
            work["final_score"] = work["score_total"]
            work["display_score"] = work["score_total"]
            work["ai_disp_total_score"] = work["score_total"]
            work["ai_disp_final_score"] = work["score_total"]

        # slope が全ゼロなら、同じ足の OHLC から候補抽出用の疑似slopeを作る。
        slope_all_zero = float(slope.abs().sum()) <= 0.000001
        if slope_all_zero:
            pseudo_slope = delta_pct.where(delta_pct.abs() > 0.0, macd_bias / 10.0)
            work["ai_disp_slope"] = pseudo_slope
            if "slope" in work.columns:
                work["slope"] = pseudo_slope
            if "slope_atr_scaled" in work.columns:
                work["slope_atr_scaled"] = pseudo_slope
            if "score_slope" in work.columns:
                work["score_slope"] = pseudo_slope

        if score_all_zero or slope_all_zero:
            try:
                rb = pd.to_numeric(work.get("ai_disp_buy_score", 0.0), errors="coerce").fillna(0.0)
                rs = pd.to_numeric(work.get("ai_disp_sell_score", 0.0), errors="coerce").fillna(0.0)
                ps = pd.to_numeric(work.get("ai_disp_slope", 0.0), errors="coerce").fillna(0.0)
                logger.warning(
                    "[SUMMARY AI ZERO SCORE RESCUE] applied rows=%s score_all_zero=%s slope_all_zero=%s buy_pos=%s sell_pos=%s slope_nonzero=%s min_vol=%s min_turnover=%s",
                    len(work), score_all_zero, slope_all_zero,
                    int((rb > 0).sum()), int((rs > 0).sum()), int((ps.abs() > 0).sum()),
                    min_vol, min_turnover,
                )
            except Exception:
                pass

        return work.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY AI ZERO SCORE RESCUE] failed; returning original dataframe")
        return out


# ============================================================
# range_pct 補修
# (旧 core/startup/summary_ai_more_candidates_patch._install_range_pct_repair_patch)
# ------------------------------------------------------------
# 1分足の high/low が close と同値へ潰れていても、day_high/day_low や
# 始値-終値から実レンジを復元して range_pct 系の列へ書き戻す。
# これが無いと SUMMARY AI LOW MOVE PREFILTER が range_pct=0 だけを見て
# 候補を大量に落としてしまう。低変動ガード自体は緩めない。
#
# 旧パッチは attach_display_like_columns を外から包んでいたが、
# 同じ属性を奪い合う相手が居ないので本体末尾へ畳んだ。
# ============================================================

_RANGE_PCT_REPAIR_VERSION = "Ver1.8-SUMMARY-AI-APPROVED-ROW-RANGE-REPAIR"


def _rpr_series(df: pd.DataFrame, names: tuple[str, ...], default: float = 0.0):
    for name in names:
        try:
            if name in df.columns:
                return pd.to_numeric(df[name], errors="coerce").fillna(default).astype(float)
        except Exception:
            continue
    return pd.Series(default, index=df.index, dtype="float64")


def _rpr_percent(s):
    """既存の range_pct 系列を percent 単位へ正規化する (入力境界で 1 回だけ)。

    range_pct は percent 単位 (1.88% = 1.88) に統一した。
    古い比率表記 (0.0188) が残っている場合だけ 100 倍して揃える。
    実務上 1% 未満の値幅は「比率で書かれた古い値」か
    「本当に 1% 未満」かの区別が付かないため、
    0 < v < RANGE_PCT_RATIO_HINT のときだけ比率とみなす。
    """
    ss = pd.to_numeric(s, errors="coerce").fillna(0.0).astype(float).abs()
    return ss.mask((ss > 0.0) & (ss < RANGE_PCT_RATIO_HINT), ss * RANGE_PCT_SCALE)


# 後方互換 alias (旧名で参照している箇所があっても壊さない)
_rpr_fraction = _rpr_percent


def _repair_range_pct_columns(out: pd.DataFrame) -> pd.DataFrame:
    if not _env_bool("SUMMARY_AI_RANGE_PCT_REPAIR_ENABLED", True):
        return out
    try:
        if not isinstance(out, pd.DataFrame) or out.empty:
            return out
        close = _rpr_series(out, ("close", "close_price", "current_price", "price", "last_price", "ai_disp_close"), 0.0)
        safe_close = close.where(close > 0, 1.0)
        existing = _rpr_percent(_rpr_series(out, ("range_pct", "intraday_range_pct", "day_range_pct", "summary_ai_movement_range_pct"), 0.0))

        high = _rpr_series(out, ("high",), 0.0)
        low = _rpr_series(out, ("low",), 0.0)
        bar_rng = ((high - low) / safe_close * RANGE_PCT_SCALE).where((high > 0) & (low > 0) & (high >= low), 0.0).abs().fillna(0.0)

        day_high = _rpr_series(out, ("day_high", "today_high", "intraday_high", "session_high", "high_price", "high_price_day", "range_high"), 0.0)
        day_low = _rpr_series(out, ("day_low", "today_low", "intraday_low", "session_low", "low_price", "low_price_day", "range_low"), 0.0)
        day_rng = ((day_high - day_low) / safe_close * RANGE_PCT_SCALE).where((day_high > 0) & (day_low > 0) & (day_high >= day_low), 0.0).abs().fillna(0.0)

        open_ = _rpr_series(out, ("opening_price", "open_price", "open", "day_open"), 0.0)
        open_rng = ((close - open_) / safe_close * RANGE_PCT_SCALE).where((open_ > 0) & (close > 0), 0.0).abs().fillna(0.0)

        repaired = pd.concat([existing, bar_rng, day_rng, open_rng], axis=1).max(axis=1).fillna(0.0).astype(float)
        improved = repaired > (existing + 1e-12)
        if bool(improved.any()):
            out = out.copy()
            out["range_pct"] = repaired
            out["intraday_range_pct"] = repaired
            out["day_range_pct"] = repaired
            out["ai_disp_range_pct"] = repaired
            try:
                samples = out.loc[improved, [c for c in ("symbol", "close", "close_price", "current_price", "high", "low", "high_price", "low_price", "day_high", "day_low", "open", "open_price", "range_pct") if c in out.columns]].head(12).to_dict(orient="records")
            except Exception:
                samples = []
            logger.warning(
                "[SUMMARY AI RANGE PCT REPAIR] repaired rows=%s before_pos=%s after_pos=%s max=%.6f samples=%s version=%s",
                int(improved.sum()),
                int((existing > 0).sum()),
                int((repaired > 0).sum()),
                float(repaired.max()) if len(repaired) else 0.0,
                samples,
                _RANGE_PCT_REPAIR_VERSION,
            )
        return out
    except Exception:
        logger.exception("[SUMMARY AI RANGE PCT REPAIR] repair failed; return original")
        return out


def attach_display_like_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = attach_display_like_columns_core(df)
    if _zero_score_rescue_scope_enabled():
        out = _apply_zero_score_rescue(out)
    return _repair_range_pct_columns(out)


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
                mask = s.str.contains("ETF", na=False) | s.str.contains("ETN", na=False) | s.str.contains("REIT", na=False) | s.str.contains("リート", na=False) | s.str.contains("投信", na=False) | s.str.contains("FUND", na=False) | s.str.contains("ＦＵＮＤ", na=False)
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
        for col, weight in [("ai_disp_buy_score", 10), ("ai_disp_sell_score", 10), ("ai_disp_total_score", 8), ("ai_disp_final_score", 8), ("ai_disp_close", 4), ("ai_disp_volume", 3), ("ai_disp_rsi", 2), ("ai_disp_macd", 2)]:
            if col in out.columns:
                quality += pd.to_numeric(out[col], errors="coerce").notna().astype(int) * weight
        out["_ai_quality"] = quality
        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            try:
                out["datetime"] = out["datetime"].dt.tz_localize(None)
            except Exception:
                pass
            out = out.sort_values(["symbol", "datetime", "_ai_quality"], ascending=[True, False, False], na_position="last", kind="mergesort")
        else:
            out = out.sort_values(["symbol", "_ai_quality"], ascending=[True, False], na_position="last", kind="mergesort")
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
        logger.warning("[SUMMARY AI CANDIDATES] freshness filter rows=%s->%s max_age=%.1fs latest=%s oldest=%s now=%s", before, len(out), float(max_age), latest, oldest, now)
        return out.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY AI CANDIDATES] freshness filter failed; continue fail-open")
        return out.reset_index(drop=True)


def _keep_latest_slot_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    """銘柄ごとの最新 datetime の行だけ残す (同着は全部残す)。

    AI へ渡すのは銘柄あたり 1 行なので、履歴全体に重い列生成をかける
    必要が無い。実測で 55,131 行 -> 数千行になり 75〜92 秒が消える。
    同一 datetime の行は残すので、dedupe_one_row_per_symbol の
    _ai_quality によるタイブレーク結果は変わらない。
    """
    out = safe_df(df)
    if out.empty or "symbol" not in out.columns or "datetime" not in out.columns:
        return out
    if not _env_bool("SUMMARY_AI_CANDIDATE_LATEST_ONLY", True):
        return out
    before = len(out)
    t0 = time.perf_counter()
    try:
        ts = pd.to_datetime(out["datetime"], errors="coerce")
        try:
            ts = ts.dt.tz_localize(None)
        except Exception:
            pass
        work = out.copy()
        work["_slot_dt"] = ts
        valid = work[work["_slot_dt"].notna()]
        if valid.empty:
            logger.warning(
                "[SUMMARY AI CANDIDATES] latest-slot trim skipped reason=datetime全欠損 rows=%s", before,
            )
            return out
        mx = valid.groupby("symbol")["_slot_dt"].transform("max")
        kept = valid[valid["_slot_dt"] == mx].drop(columns=["_slot_dt"])
        # datetime が欠損している行は判定できないので落とさず残す
        na_rows = work[work["_slot_dt"].isna()].drop(columns=["_slot_dt"])
        if not na_rows.empty:
            kept = pd.concat([kept, na_rows], ignore_index=True, sort=False)
        kept = kept.reset_index(drop=True)
    except Exception:
        logger.exception("[SUMMARY AI CANDIDATES] latest-slot trim failed rows=%s -> 元のまま使う", before)
        return out
    logger.warning(
        "[SUMMARY AI CANDIDATES] latest-slot trim rows=%s->%s symbols=%s elapsed=%.2fs",
        before, len(kept),
        int(kept["symbol"].nunique()) if "symbol" in kept.columns else 0,
        time.perf_counter() - t0,
    )
    return kept


def _prepare_base(summary_df: pd.DataFrame, *, require_buy_target: bool, exclude_etf_fund: bool) -> pd.DataFrame:
    t_all = time.perf_counter()
    n_in = len(safe_df(summary_df))
    # 重い列生成の前に「銘柄ごとの最新足」まで絞る
    df = _keep_latest_slot_per_symbol(summary_df)
    t0 = time.perf_counter()
    df = attach_display_like_columns(df)
    t_disp = time.perf_counter() - t0
    if df.empty:
        return df
    df = filter_common_stock_rows(df, require_buy_target=require_buy_target, exclude_etf_fund=exclude_etf_fund)
    if df.empty:
        return df
    # 先に銘柄あたり 1 行へ畳んでから鮮度を見る。
    # 逆順だと「履歴の古い行が多いので全部落ちる」という誤判定になる。
    df = dedupe_one_row_per_symbol(df)
    if df.empty:
        return df
    df = _filter_fresh_candidate_rows(df)
    if df.empty:
        logger.warning("[SUMMARY AI CANDIDATES] no fresh rows after freshness filter")
        return df
    logger.warning(
        "[SUMMARY AI CANDIDATES] prepare_base done in_rows=%s out_rows=%s "
        "attach_display=%.2fs total=%.2fs",
        n_in, len(df), t_disp, time.perf_counter() - t_all,
    )
    return df


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
            top_buy = df.sort_values("ai_disp_buy_score", ascending=False).head(10)[[c for c in ("symbol", "datetime", "ai_disp_buy_score", "config_buy_score", "ai_disp_sell_score", "ai_disp_close", "ai_disp_volume", "ai_disp_slope", "summary_ai_movement_strength") if c in df.columns]].to_dict(orient="records")
        except Exception:
            top_buy = []
        logger.warning("[SUMMARY AI CANDIDATES] BUY empty interval=%s source=%s before=%s min_buy=%.2f max_sell=%.2f min_price=%.1f min_volume=%.1f ranking_source=%s slope_gate=%s top_buy=%s", interval, source, before, float(min_buy_score), float(max_sell_score), float(resolved_min_price), float(min_volume), _is_ranking_source(source), not _is_ranking_source(source), top_buy)
        return out
    out["_ai_sort_score"] = pd.to_numeric(out["ai_disp_buy_score"], errors="coerce").fillna(0.0) * 10.0 + pd.to_numeric(out["ai_disp_total_score"], errors="coerce").fillna(0.0) * 3.0 + pd.to_numeric(out["ai_disp_mtf"], errors="coerce").fillna(0.0) + pd.to_numeric(out["ai_disp_slope"], errors="coerce").fillna(0.0) - pd.to_numeric(out["ai_disp_sell_score"], errors="coerce").fillna(0.0) * 5.0
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


# ============================================================
# SELL 候補の short_ok 事前フィルタ
# (旧 core/startup/summary_ai_more_candidates_patch._install_sell_short_ok_filter_patch)
# ------------------------------------------------------------
# 空売り可否 (short_ok) が立っていない銘柄を SELL 候補の段階で落とす。
# 旧パッチは _sell_candidates_from_prepared を外から包んでいたが、
# 同じ属性を奪い合う相手が居ないので本体へ畳んだ。ネイティブは *_core へ改名。
# ============================================================


def _apply_sell_short_ok_prefilter(out: pd.DataFrame) -> pd.DataFrame:
    if not _env_bool("SUMMARY_AI_SELL_SHORT_OK_PREFILTER", True):
        return out
    try:
        from AI.sell_credit_guard import can_sell_symbol

        if not isinstance(out, pd.DataFrame) or out.empty:
            return out
        before = len(out)
        keep_idx: list[Any] = []
        skipped: list[dict[str, Any]] = []
        for idx, row in out.iterrows():
            try:
                row_dict = row.to_dict() if hasattr(row, "to_dict") else dict(row)
                symbol = str(row_dict.get("symbol") or "").strip()
                if can_sell_symbol(row_dict, default=False):
                    keep_idx.append(idx)
                else:
                    skipped.append({"symbol": symbol, "reason": "short_ok_not_1"})
            except Exception as e:
                skipped.append({"symbol": str(getattr(row, "symbol", "")), "reason": f"guard_error:{e}"})
        if len(keep_idx) == before:
            return out
        filtered = out.loc[keep_idx].copy().reset_index(drop=True) if keep_idx else out.iloc[0:0].copy()
        kept_symbols = list(filtered["symbol"].astype(str).head(20)) if "symbol" in filtered.columns and not filtered.empty else []
        logger.warning(
            "[SUMMARY AI SELL SHORT_OK PREFILTER] filtered before=%s after=%s skipped=%s kept_symbols=%s",
            before, len(filtered), skipped[:20], kept_symbols,
        )
        return filtered
    except Exception:
        logger.exception("[SUMMARY AI SELL SHORT_OK PREFILTER] failed; return original candidates")
        return out


def _sell_candidates_from_prepared(df: pd.DataFrame, *, interval: int | str, top_n: int, min_sell_score: float, max_buy_score: float, min_volume: float, min_price: float, source: str) -> pd.DataFrame:
    out = _sell_candidates_from_prepared_core(
        df, interval=interval, top_n=top_n, min_sell_score=min_sell_score,
        max_buy_score=max_buy_score, min_volume=min_volume, min_price=min_price, source=source,
    )
    return _apply_sell_short_ok_prefilter(out)


def _sell_candidates_from_prepared_core(df: pd.DataFrame, *, interval: int | str, top_n: int, min_sell_score: float, max_buy_score: float, min_volume: float, min_price: float, source: str) -> pd.DataFrame:
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
            top_sell = df.sort_values("ai_disp_sell_score", ascending=False).head(10)[[c for c in ("symbol", "datetime", "ai_disp_sell_score", "config_sell_score", "ai_disp_buy_score", "ai_disp_close", "ai_disp_volume", "ai_disp_slope", "summary_ai_movement_strength") if c in df.columns]].to_dict(orient="records")
        except Exception:
            top_sell = []
        logger.warning("[SUMMARY AI CANDIDATES] SELL empty interval=%s source=%s before=%s ranking_source=%s slope_gate=%s top_sell=%s", interval, source, before, _is_ranking_source(source), not _is_ranking_source(source), top_sell)
        return out
    out["_ai_sort_score"] = pd.to_numeric(out["ai_disp_sell_score"], errors="coerce").fillna(0.0) * 10.0 - pd.to_numeric(out["ai_disp_buy_score"], errors="coerce").fillna(0.0) * 5.0 - pd.to_numeric(out["ai_disp_slope"], errors="coerce").fillna(0.0) * 3.0 - pd.to_numeric(out["ai_disp_total_score"], errors="coerce").fillna(0.0)
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


def _build_summary_ai_entry_candidates_core(summary_df: pd.DataFrame, *, interval: int | str = 1, top_n: int = DEFAULT_TOP_N, min_buy_score: float = DEFAULT_MIN_BUY_SCORE, max_sell_score: float = DEFAULT_MAX_SELL_SCORE, min_volume: float = DEFAULT_MIN_VOLUME, min_price: float = DEFAULT_MIN_PRICE, require_buy_target: bool = False, exclude_etf_fund: bool = True, source: str = "SUMMARY") -> pd.DataFrame:
    top_n = _resolve_top_n(top_n)
    base = _prepare_base(summary_df, require_buy_target=require_buy_target, exclude_etf_fund=exclude_etf_fund)
    if base.empty:
        logger.info("[SUMMARY AI CANDIDATES] no rows after base prepare interval=%s source=%s", interval, source)
        return base
    buy_df = _buy_candidates_from_prepared(base, interval=interval, top_n=top_n, min_buy_score=min_buy_score, max_sell_score=max_sell_score, min_volume=min_volume, min_price=min_price, source=source)
    sell_df = _sell_candidates_from_prepared(base, interval=interval, top_n=top_n, min_sell_score=0.01, max_buy_score=999999.0, min_volume=min_volume, min_price=min_price, source=source)
    frames = [x for x in (buy_df, sell_df) if isinstance(x, pd.DataFrame) and not x.empty]
    if not frames:
        logger.warning("[SUMMARY AI CANDIDATES] BUY_SELL combined empty interval=%s source=%s base_rows=%s", interval, source, len(base))
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True, sort=False)
    logger.warning("[SUMMARY AI CANDIDATES] BUY_SELL_COMBINED_READY interval=%s source=%s buy_count=%s sell_count=%s total=%s top_n_each=%s buy_symbols=%s sell_symbols=%s", interval, source, len(buy_df) if isinstance(buy_df, pd.DataFrame) else 0, len(sell_df) if isinstance(sell_df, pd.DataFrame) else 0, len(out), top_n, _safe_symbols(buy_df, top_n), _safe_symbols(sell_df, top_n))
    return out.reset_index(drop=True)


# --- entry_affordability_runtime_patch (native化, 元 core/startup/entry_affordability_runtime_patch.py) ---
# AIに確認する前に、対象価格帯外/最低1単元でも予算上限を超える銘柄を除外する。
def _entry_affordability_safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _entry_affordability_pick_price_from_row(row: Any) -> float:
    try:
        for key in (
            "ai_disp_close",
            "close_price",
            "price",
            "current_price",
            "close",
            "last_price",
            "CurrentPrice",
        ):
            try:
                v = row.get(key)
            except Exception:
                v = None
            p = _entry_affordability_safe_float(v, 0.0)
            if p > 0:
                return p
    except Exception:
        pass
    return 0.0


def _entry_affordability_symbol_from_row(row: Any) -> str:
    try:
        s = str(row.get("symbol") or "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _entry_affordability_filter_candidates_df(df, *, label: str):
    try:
        from trading.entry.entry_budget import can_afford_min_lot, log_entry_budget_config
    except Exception:
        logger.exception("[ENTRY AFFORDABILITY PATCH] import failed")
        return df

    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return df

        log_entry_budget_config(prefix="[ENTRY AFFORDABILITY PATCH]")

        keep_idx: list[Any] = []
        skipped: list[dict[str, Any]] = []

        for idx, row in df.iterrows():
            price = _entry_affordability_pick_price_from_row(row)
            ok, diag = can_afford_min_lot(price)
            if ok:
                keep_idx.append(idx)
                continue

            skipped.append(
                {
                    "symbol": _entry_affordability_symbol_from_row(row),
                    "side": str(row.get("side") or row.get("ai_side") or ""),
                    "price": round(float(diag.get("price") or price or 0.0), 2),
                    "min_price": round(float(diag.get("min_price") or 0.0), 2),
                    "max_price": round(float(diag.get("max_price") or 0.0), 2),
                    "configured_max_price": round(float(diag.get("configured_max_price") or 0.0), 2),
                    "affordable_max_price": round(float(diag.get("affordable_max_price") or 0.0), 2),
                    "budget_yen": round(float(diag.get("budget_yen") or 0.0), 0),
                    "lot_size": int(diag.get("lot_size") or 0),
                    "min_notional": round(float(diag.get("min_notional") or 0.0), 0),
                    "reason": str(diag.get("reason") or "price_range_or_budget_ng"),
                }
            )

        if not skipped:
            return df

        out = df.loc[keep_idx].copy().reset_index(drop=True)
        logger.warning(
            "[ENTRY AFFORDABILITY PATCH] AI_PRE_FILTER label=%s before=%s after=%s skipped=%s",
            label,
            len(df),
            len(out),
            skipped[:80],
        )
        return out

    except Exception:
        logger.exception("[ENTRY AFFORDABILITY PATCH] filter failed label=%s", label)
        return df


def build_summary_ai_entry_candidates(summary_df: pd.DataFrame, *, interval: int | str = 1, top_n: int = DEFAULT_TOP_N, min_buy_score: float = DEFAULT_MIN_BUY_SCORE, max_sell_score: float = DEFAULT_MAX_SELL_SCORE, min_volume: float = DEFAULT_MIN_VOLUME, min_price: float = DEFAULT_MIN_PRICE, require_buy_target: bool = False, exclude_etf_fund: bool = True, source: str = "SUMMARY") -> pd.DataFrame:
    df = _build_summary_ai_entry_candidates_core(
        summary_df,
        interval=interval,
        top_n=top_n,
        min_buy_score=min_buy_score,
        max_sell_score=max_sell_score,
        min_volume=min_volume,
        min_price=min_price,
        require_buy_target=require_buy_target,
        exclude_etf_fund=exclude_etf_fund,
        source=source,
    )
    return _entry_affordability_filter_candidates_df(df, label="summary_ai_candidates_after_build_before_ai_gate")


def build_summary_ai_sell_entry_candidates(summary_df: pd.DataFrame, *, interval: int | str = 1, top_n: int = DEFAULT_TOP_N, min_sell_score: float = 0.01, max_buy_score: float = 2.0, min_volume: float = DEFAULT_MIN_VOLUME, min_price: float = DEFAULT_MIN_PRICE, require_buy_target: bool = False, exclude_etf_fund: bool = True, source: str = "SUMMARY") -> pd.DataFrame:
    top_n = _resolve_top_n(top_n)
    base = _prepare_base(summary_df, require_buy_target=require_buy_target, exclude_etf_fund=exclude_etf_fund)
    if base.empty:
        return base
    return _sell_candidates_from_prepared(base, interval=interval, top_n=top_n, min_sell_score=min_sell_score, max_buy_score=max_buy_score, min_volume=min_volume, min_price=min_price, source=source)


__all__ = [
    "DEFAULT_TOP_N",
    "DEFAULT_MIN_BUY_SCORE",
    "DEFAULT_MAX_SELL_SCORE",
    "DEFAULT_MIN_VOLUME",
    "DEFAULT_MIN_PRICE",
    "build_summary_ai_entry_candidates",
    "build_summary_ai_sell_entry_candidates",
    "attach_display_like_columns",
]


# ============================================================
# ENTRY FIRE RESCUE のしきい値緩和
# (旧 core/startup/entry_fire_rescue_runtime_patch._patch_candidates)
# ------------------------------------------------------------
# 旧パッチは install 時にこの 4 定数を env 由来の値で「より緩い側へ」
# 寄せていた (min は min()、max は max())。同じ計算を import 時に行う。
# 適用スコープは entry_fire_rescue.scope_enabled() (main.py のみ)。
# ============================================================


def _efr_apply_threshold_relax() -> None:
    global DEFAULT_MIN_BUY_SCORE, DEFAULT_MAX_SELL_SCORE
    global DEFAULT_MIN_BUY_SLOPE, DEFAULT_MAX_SELL_SLOPE
    try:
        from .entry_fire_rescue import scope_enabled as _efr_scope, _env_float as _efr_env_float

        if not _efr_scope():
            return
        DEFAULT_MIN_BUY_SCORE = min(float(DEFAULT_MIN_BUY_SCORE), _efr_env_float("SUMMARY_AI_MIN_BUY_SCORE", 0.12))
        DEFAULT_MAX_SELL_SCORE = min(float(DEFAULT_MAX_SELL_SCORE), _efr_env_float("SUMMARY_AI_MIN_SELL_SCORE", 0.12))
        DEFAULT_MIN_BUY_SLOPE = min(float(DEFAULT_MIN_BUY_SLOPE), _efr_env_float("SUMMARY_AI_MIN_BUY_SLOPE", 0.0))
        DEFAULT_MAX_SELL_SLOPE = max(float(DEFAULT_MAX_SELL_SLOPE), _efr_env_float("SUMMARY_AI_MAX_SELL_SLOPE", 0.0))
    except Exception:
        logger.debug("[ENTRY FIRE RESCUE] candidates threshold relax skipped", exc_info=True)


_efr_apply_threshold_relax()
