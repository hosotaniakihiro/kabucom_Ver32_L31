# ============================================================
# File   : trading/entry/tonosama/fast_score_prefilter.py
# ------------------------------------------------------------
# 殿様エントリーの高速スコア事前フィルタ / zero-surge 救済 / AI soft rescue。
#
# 旧 core/startup/tonosama_fast_score_prefilter_patch.py V5.2 が
#   - trading/entry/tonosama/runner.build_feature_df_with_5sec
#   - trading/entry/tonosama/runner.ai_check_tonosama_entry
#   - trading/entry/tonosama/ai_gate.ai_check_tonosama_entry
# を差し替えて行っていた処理を本文側へ移設したもの。
#
# 旧パッチは sitecustomize の BACKGROUND_MAIN_PATCHES 経由 = main.py 専用で、
# DB専用子プロセスでは適用されていなかった。本文化後も
# fast_score_scope_enabled() で同じスコープを保つ。
# ============================================================
# ============================================================
# File   : core/startup/tonosama_fast_score_prefilter_patch.py
# Version: V5.2-TONOSAMA-RANGE-REPAIR-CHAIN
# ------------------------------------------------------------
# Purpose:
#   Speed up Tonosama feature building and avoid losing strong candidates
#   when 3m/5m surge history is missing or price_change is 0.00 while
#   range/volume/slope evidence is strong.
#
# Fix V5.2:
#   - Chain TONOSAMA_RANGE_FROM_HISTORY so the primary intrabar range guard
#     can use actual 3m/5m raw-history movement when the latest PUSH 1m bar
#     is a one-tick flat bar (high == low == close).
#   - This is not fail-open; if no history movement exists, strict filters
#     still drop the row.
# ============================================================

from __future__ import annotations

import logging
import os
import time
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V5.2-TONOSAMA-RANGE-REPAIR-CHAIN"
_PATCHED = False
_ORIG_BUILD_FEATURE_DF_WITH_5SEC = None
_ORIG_AI_CHECK = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        if hasattr(pd, "isna") and pd.isna(v):
            return float(default)
        s = str(v).strip().replace(",", "")
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return float(default)
        return float(s)
    except Exception:
        return float(default)


def _get(row: Any, key: str, default: Any = None) -> Any:
    try:
        if hasattr(row, "get"):
            return row.get(key, default)
        return getattr(row, key, default)
    except Exception:
        return default


def _num(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    try:
        if df is None or df.empty or col not in df.columns:
            return pd.Series(default, index=df.index if df is not None else None, dtype="float64")
        return pd.to_numeric(df[col], errors="coerce").fillna(default).astype(float)
    except Exception:
        return pd.Series(default, index=df.index if df is not None else None, dtype="float64")



def _threshold(runner: Any) -> float:
    try:
        base = float(getattr(runner, "MIN_FINAL_SCORE", 2.5))
    except Exception:
        base = 2.5
    explicit = _env_float("TONOSAMA_FAST_SCORE_PREFILTER_MIN", 0.0)
    if explicit > 0:
        return explicit
    return max(0.0, base * _env_float("TONOSAMA_FAST_SCORE_PREFILTER_RATIO", 1.0))


def _sample_rows(runner: Any, df: pd.DataFrame, cols: list[str], limit: int = 8):
    try:
        return runner._sample_rows(df, cols, limit=limit)
    except Exception:
        if df is None or df.empty:
            return []
        use_cols = [c for c in cols if c in df.columns]
        return df[use_cols].head(limit).to_dict("records")


def _zero_surge_score_rescue(df: pd.DataFrame, *, threshold: float) -> pd.DataFrame:
    try:
        if df is None or df.empty or not _env_bool("TONOSAMA_ZERO_SURGE_PREFILTER_SCORE_RESCUE", True):
            return df
        x = df.copy()
        surge = _num(x, "_max_volume_surge_ratio", 0.0)
        volume = _num(x, "_latest_volume", 0.0).combine(_num(x, "volume", 0.0), max)
        rng = _num(x, "_intrabar_range_pct", 0.0)
        body = _num(x, "_body_change_pct", 0.0)
        mtf = _num(x, "mtf", 0.0).abs().combine(_num(x, "score_mtf", 0.0).abs(), max)
        score_abs = _num(x, "score", 0.0).abs().combine(_num(x, "final_score", 0.0).abs(), max)

        min_volume = _env_float("TONOSAMA_ZERO_SURGE_PREFILTER_MIN_VOLUME", _env_float("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_VOLUME", 500000.0))
        min_range = _env_float("TONOSAMA_ZERO_SURGE_PREFILTER_MIN_RANGE_PCT", _env_float("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_RANGE_PCT", 4.0))
        min_body = _env_float("TONOSAMA_ZERO_SURGE_PREFILTER_MIN_BODY_PCT", 0.0)
        min_score = _env_float("TONOSAMA_ZERO_SURGE_PREFILTER_MIN_ABS_SCORE", _env_float("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_ABS_SCORE", 0.8))
        min_mtf = _env_float("TONOSAMA_ZERO_SURGE_PREFILTER_MIN_MTF", _env_float("TONOSAMA_VOLUME_SURGE_ZERO_RESCUE_MIN_MTF", 1.0))
        failopen_surge = _env_float("TONOSAMA_VOLUME_SURGE_FAILOPEN_VALUE", 3.0)

        rescue = (surge <= 0) & (volume >= min_volume) & (rng >= min_range) & (body >= min_body) & (score_abs >= min_score) & (mtf >= min_mtf)
        if not rescue.any():
            return x

        x.loc[rescue, "_max_volume_surge_ratio"] = failopen_surge
        x.loc[rescue, "_volume_surge_failopen"] = True
        x.loc[rescue, "_volume_surge_history_missing"] = True
        if "_surge_tf" not in x.columns:
            x["_surge_tf"] = ""
        x.loc[rescue, "_surge_tf"] = x.loc[rescue, "_surge_tf"].replace("", "1m_failopen")

        if "_tonosama_score" in x.columns:
            cur = pd.to_numeric(x.loc[rescue, "_tonosama_score"], errors="coerce").fillna(0.0)
            x.loc[rescue, "_tonosama_score"] = cur.clip(lower=threshold + 0.05)
        if "pending_score" in x.columns:
            cur = pd.to_numeric(x.loc[rescue, "pending_score"], errors="coerce").fillna(0.0)
            x.loc[rescue, "pending_score"] = cur.clip(lower=threshold + 0.05)

        sample_cols = ["symbol", "symbolname", "close", "_latest_volume", "_intrabar_range_pct", "_max_volume_surge_ratio", "_tonosama_score", "score", "final_score", "mtf", "score_mtf"]
        logger.warning(
            "[TONOSAMA ZERO SURGE PREFILTER RESCUE] rescued=%s threshold=%.3f failopen_surge=%.2f min_volume=%.0f min_range=%.3f min_score=%.3f min_mtf=%.3f sample=%s",
            int(rescue.sum()), threshold, failopen_surge, min_volume, min_range, min_score, min_mtf,
            x.loc[rescue, [c for c in sample_cols if c in x.columns]].head(10).to_dict("records"),
        )
        return x
    except Exception:
        logger.exception("[TONOSAMA ZERO SURGE PREFILTER RESCUE] failed")
        return df



def _movement_values(row: Any) -> tuple[float, float, float, float, str]:
    price_chg = _safe_float(_get(row, "_max_price_change_pct"), 0.0)
    body = _safe_float(_get(row, "_signed_body_change_pct"), 0.0)
    slope = _safe_float(_get(row, "_slope"), 0.0)
    effective_slope = slope
    source = "slope"
    if abs(effective_slope) <= 1e-12:
        if abs(price_chg) > 0:
            effective_slope = price_chg / 1000.0
            source = "price_chg_proxy"
        elif abs(body) > 0:
            effective_slope = body / 1000.0
            source = "body_proxy"
    return price_chg, body, slope, effective_slope, source


def _infer_side(row: Any) -> str:
    price_chg, body, slope, effective_slope, _ = _movement_values(row)
    if price_chg < 0 or body < 0 or effective_slope < 0 or slope < 0:
        return "SELL"
    return "BUY"


def _sell_close_position_block(row: Any) -> tuple[bool, str]:
    side = _infer_side(row)
    if side != "SELL":
        return False, "not_sell"
    close_pos = _safe_float(_get(row, "_close_position_pct"), 50.0)
    max_close_pos = _env_float("TONOSAMA_AI_SELL_MAX_CLOSE_POSITION_PCT", 65.0)
    if close_pos > max_close_pos:
        return True, f"sell_close_position_high close_position={close_pos:.2f} max={max_close_pos:.2f}"
    return False, "ok"


def _soft_rescue_ai_ng(row: Any, reason: str) -> tuple[bool, str]:
    if not _env_bool("TONOSAMA_AI_SOFT_RESCUE", True):
        return False, "disabled"
    r = str(reason or "")

    true_hard_words = (
        "climax", "selling_climax", "buying_climax", "reverse",
        "upper_wick_reversal", "lower_wick_reversal",
    )
    if any(w in r for w in true_hard_words):
        return False, "hard_reason"
    if not ("price change low" in r or "slope low" in r or "range_rescue_direction_ng" in r):
        return False, "not_soft_reason"

    block, block_reason = _sell_close_position_block(row)
    if block:
        return False, block_reason

    volume = _safe_float(_get(row, "_latest_volume"), 0.0)
    rng = _safe_float(_get(row, "_intrabar_range_pct"), 0.0)
    surge = _safe_float(_get(row, "_max_volume_surge_ratio"), 0.0)
    price_chg, body, slope, effective_slope, slope_source = _movement_values(row)
    side = _infer_side(row)

    min_vol = _env_float("TONOSAMA_AI_RESCUE_MIN_VOLUME", 500000.0)
    min_range = _env_float("TONOSAMA_AI_RESCUE_MIN_RANGE_PCT", 4.0)
    min_price_body_range = _env_float("TONOSAMA_AI_RESCUE_PRICE_BODY_MIN_RANGE_PCT", 1.0)
    min_surge = _env_float("TONOSAMA_AI_RESCUE_MIN_SURGE", 3.0)
    min_chg = _env_float("TONOSAMA_AI_RESCUE_MIN_PRICE_CHANGE_PCT", 0.08)
    min_slope_abs = _env_float("TONOSAMA_AI_RESCUE_MIN_SLOPE_ABS", 0.0003)
    zero_price_slope_min = _env_float("TONOSAMA_AI_RESCUE_ZERO_PRICE_MIN_SLOPE_ABS", min_slope_abs)

    strong_price_body_move = abs(price_chg) >= min_chg or abs(body) >= min_chg
    slope_missing_fallback = abs(slope) <= 1e-12 and abs(effective_slope) >= min_slope_abs and strong_price_body_move
    required_range = min_price_body_range if slope_missing_fallback else min_range

    if volume < min_vol:
        return False, "volume_low"
    if rng < required_range:
        return False, f"range_low min={required_range:.3f} slope_source={slope_source}"
    if surge < min_surge:
        return False, "surge_low"

    zero_price_direction_rescue = (
        "range_rescue_direction_ng" in r
        and abs(price_chg) < min_chg
        and abs(body) < min_chg
        and abs(effective_slope) >= zero_price_slope_min
    )

    if not zero_price_direction_rescue:
        if not strong_price_body_move and abs(effective_slope) < min_slope_abs:
            return False, "move_low"
        if side == "BUY" and price_chg < -min_chg:
            return False, "buy_price_reverse"
        if side == "SELL" and price_chg > min_chg:
            return False, "sell_price_reverse"

    detail = (
        f"soft_rescue side={side} volume={volume:.0f} range={rng:.3f} required_range={required_range:.3f} "
        f"surge={surge:.2f} price_chg={price_chg:.3f} body={body:.3f} "
        f"slope={slope:.6f} effective_slope={effective_slope:.6f} slope_source={slope_source} "
        f"slope_missing_fallback={slope_missing_fallback} zero_price_direction_rescue={zero_price_direction_rescue}"
    )
    return True, detail



# ============================================================
# スコープ判定 + 公開入口
# ============================================================

import sys  # noqa: E402

_FSP_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)


def fast_score_scope_enabled() -> bool:
    """このプロセスで fast score prefilter / AI rescue を使うか。

    旧パッチは sitecustomize の BACKGROUND_MAIN_PATCHES 経由 = main.py 専用。
    TONOSAMA_FAST_SCORE_SCOPE_ENABLED=1/0 で明示的に上書きできる。
    CORE_STARTUP_FORCE_FULL_INIT_PATCHES=1 のときは DB プロセスでも有効。
    """
    try:
        raw = os.getenv("TONOSAMA_FAST_SCORE_SCOPE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return _env_bool("TONOSAMA_FAST_SCORE_SCOPE_ENABLED", True)
        if _env_bool("CORE_STARTUP_FORCE_FULL_INIT_PATCHES", False):
            return True
        argv_text = " ".join(str(x) for x in sys.argv).replace("\\", "/").lower()
        return not any(name in argv_text for name in _FSP_DB_PROCESS_NAMES)
    except Exception:
        return False


def prefilter_enabled() -> bool:
    """build_feature_df_with_5sec 側の fast score prefilter を使うか。"""
    return fast_score_scope_enabled() and _env_bool("TONOSAMA_FAST_SCORE_PREFILTER", True)


def score_threshold(runner: Any) -> float:
    return _threshold(runner)


def zero_surge_score_rescue(df: pd.DataFrame, *, threshold: float) -> pd.DataFrame:
    return _zero_surge_score_rescue(df, threshold=threshold)


def ai_short_circuit_reason(row: Any, runner: Any):
    """AI 判定前の足切り。止めるなら (ok, prob, reason) を返し、通すなら None。"""
    if not fast_score_scope_enabled():
        return None
    if not _env_bool("TONOSAMA_FAST_SCORE_AI_SHORT_CIRCUIT", True):
        return None
    try:
        raw_score = _safe_float(_get(row, "_tonosama_score"), 0.0)
        th = _threshold(runner)
        if raw_score < th:
            logger.info(
                "[TONOSAMA FAST SCORE PREFILTER] AI short-circuit symbol=%s raw_score=%.4f threshold=%.4f "
                "reason=final_score_low_pre_ai",
                _get(row, "symbol", ""), raw_score, th,
            )
            return False, 0.0, f"final_score_low_pre_ai raw_score={raw_score:.4f} < {th:.4f}"
    except Exception:
        logger.debug("[TONOSAMA FAST SCORE PREFILTER] AI short-circuit check failed", exc_info=True)
    return None


def post_ai_adjust(row: Any, ok: bool, prob: float, reason: str):
    """AI 判定後の SELL 高値掴みブロック / soft rescue。"""
    if not fast_score_scope_enabled():
        return ok, prob, reason

    if ok:
        block, block_reason = _sell_close_position_block(row)
        if block:
            logger.warning(
                "[TONOSAMA AI SELL CLOSE POSITION GUARD] block symbol=%s reason=%s version=%s",
                _get(row, "symbol", ""), block_reason, VERSION,
            )
            return False, prob, block_reason
        return ok, prob, reason

    try:
        symbol = _get(row, "symbol", "")
        rescue, detail = _soft_rescue_ai_ng(row, str(reason or ""))
        if rescue:
            logger.warning(
                "[TONOSAMA AI SOFT RESCUE] OK symbol=%s original_reason=%s detail=%s version=%s",
                symbol, reason, detail, VERSION,
            )
            return True, max(_safe_float(prob, 0.0), 0.0), f"AI soft rescue: {detail}; original={reason}"
        logger.info(
            "[TONOSAMA AI SOFT RESCUE] keep NG symbol=%s original_reason=%s no_rescue=%s version=%s",
            symbol, reason, detail, VERSION,
        )
    except Exception:
        logger.debug("[TONOSAMA AI SOFT RESCUE] failed", exc_info=True)
    return ok, prob, reason


__all__ = [
    "fast_score_scope_enabled",
    "prefilter_enabled",
    "score_threshold",
    "zero_surge_score_rescue",
    "ai_short_circuit_reason",
    "post_ai_adjust",
]
