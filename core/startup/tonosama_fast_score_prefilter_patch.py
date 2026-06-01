# ============================================================
# File   : core/startup/tonosama_fast_score_prefilter_patch.py
# Version: V3-TONOSAMA-AI-SOFT-RESCUE-ZERO-SURGE-SCORE
# ------------------------------------------------------------
# 目的:
#   殿様イナゴの処理時間が candidates=11 registered=0 でも19秒程度かかる問題を軽減する。
#
# Ver2:
#   - 価格変化/傾きがわずかに閾値未満でも、出来高・レンジ・方向が十分な候補は
#     AI fallback OK としてPENDING登録へ進める。
#
# Ver3:
#   - 3m/5m PUSH merged が古い Yahoo 復旧データのままになると、volume_surge.py は
#     controlled fail-open しても _max_volume_surge_ratio=0 のfeature dfを返す。
#   - その結果、prepare_entry_scores 後の _tonosama_score が -0.0x〜1.0 程度になり、
#     fast score prefilter threshold=2.3 で候補0件になる。
#   - 1m側の出来高・レンジ・MTFが十分な場合だけ、Tonosama限定で
#     _max_volume_surge_ratio を fail-open 値に補完し、pre_5sec scoreを救済する。
#   - クライマックス/方向NGは後段のpending/AI/final guardで維持する。
# ============================================================

from __future__ import annotations

import logging
import os
import time
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
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
    ratio = _env_float("TONOSAMA_FAST_SCORE_PREFILTER_RATIO", 1.0)
    return max(0.0, base * ratio)


def _sample_rows(runner: Any, df: pd.DataFrame, cols: list[str], limit: int = 8):
    try:
        return runner._sample_rows(df, cols, limit=limit)
    except Exception:
        if df is None or df.empty:
            return []
        use_cols = [c for c in cols if c in df.columns]
        return df[use_cols].head(limit).to_dict("records")


def _zero_surge_score_rescue(df: pd.DataFrame, *, threshold: float) -> pd.DataFrame:
    """Rescue Tonosama feature rows when 3m/5m history is missing but 1m evidence is strong."""
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

        # prepare_entry_scores がそれでも低く出る場合に備え、pre_5sec prefilterだけ通す最低点を付与。
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


def _patched_build_feature_df_with_5sec() -> pd.DataFrame:
    if not _env_bool("TONOSAMA_FAST_SCORE_PREFILTER", True):
        return _ORIG_BUILD_FEATURE_DF_WITH_5SEC()

    import trading.entry.tonosama.runner as runner

    started = time.perf_counter()
    x = runner.build_scalping_feature_df()
    if x is None or x.empty:
        logger.info("[TONOSAMA ENTRY] base feature empty")
        return pd.DataFrame()

    base_rows = len(x)
    x = runner._apply_primary_filters(x)
    primary_rows = len(x)
    if x.empty:
        logger.info(
            "[TONOSAMA ENTRY] no candidates after primary filters base_rows=%s primary_rows=%s diag=%s elapsed=%.3fs",
            base_rows, primary_rows, getattr(runner, "_LAST_FILTER_DIAG", {}), time.perf_counter() - started,
        )
        return pd.DataFrame()

    th = _threshold(runner)
    x = _zero_surge_score_rescue(x, threshold=th)

    try:
        x = runner.prepare_entry_scores(x)
        # score計算後にも再度救済。prepare_entry_scores がfailopen scoreを上書きするケース対策。
        x = _zero_surge_score_rescue(x, threshold=th)
        if "_tonosama_score" in x.columns:
            x = x.sort_values("_tonosama_score", ascending=False)
    except Exception:
        logger.warning("[TONOSAMA ENTRY] pre 5sec prepare_entry_scores failed", exc_info=True)

    sample_cols = [
        "symbol", "symbolname", "close", "_latest_volume",
        "_body_change_pct", "_signed_body_change_pct", "_intrabar_range_pct",
        "_close_position_pct", "_upper_wick_pct", "_lower_wick_pct",
        "_max_volume_surge_ratio", "_max_price_change_pct", "_slope",
        "_tonosama_score", "score", "final_score", "score_mtf", "mtf", "_volume_surge_failopen",
    ]

    if "_tonosama_score" in x.columns:
        before = x.copy()
        x = x[pd.to_numeric(x["_tonosama_score"], errors="coerce").fillna(0.0) >= th]
        try:
            runner._log_filter_step(
                stage="pre_5sec_score",
                before=before,
                after=x,
                reason="tonosama_score_below_min_final_pre_5sec",
                threshold={"MIN_FINAL_SCORE": getattr(runner, "MIN_FINAL_SCORE", 2.5), "effective_threshold": th},
                sample_cols=sample_cols,
            )
        except Exception:
            logger.warning(
                "[TONOSAMA FAST SCORE PREFILTER] pre_5sec before=%s after=%s threshold=%.3f sample=%s",
                len(before), len(x), th, _sample_rows(runner, before, sample_cols, limit=8),
            )
        if x.empty:
            logger.info(
                "[TONOSAMA ENTRY] no candidates after fast score prefilter base_rows=%s primary_rows=%s threshold=%.3f elapsed=%.3fs",
                base_rows, primary_rows, th, time.perf_counter() - started,
            )
            return pd.DataFrame()

    max_5sec = int(getattr(runner, "MAX_5SEC_FEATURE_SYMBOLS", 0) or 0)
    if max_5sec <= 0:
        max_5sec = int(getattr(runner, "MAX_CANDIDATES", 80) or 80)

    before_head = _sample_rows(runner, x, sample_cols, limit=12)
    x = x.head(min(max_5sec, int(getattr(runner, "MAX_CANDIDATES", 80) or 80))).reset_index(drop=True)

    features = []
    feature_missing = 0
    for _, row in x.iterrows():
        sym = runner.normalize_symbol(row.get("symbol"))
        if not sym:
            continue
        try:
            f = runner.build_5sec_features(sym)
            if not isinstance(f, dict):
                f = {}
            if not f:
                feature_missing += 1
            f["symbol"] = sym
            features.append(f)
        except Exception:
            feature_missing += 1
            logger.warning("[TONOSAMA ENTRY] build_5sec_features failed symbol=%s", sym, exc_info=True)
            features.append({"symbol": sym})

    if features:
        x = x.merge(pd.DataFrame(features), on="symbol", how="left")
    for c in ["price_change_5s_pct", "volume_surge_ratio_5s", "latest_5sec_close", "latest_5sec_volume"]:
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")

    x = runner.prepare_entry_scores(x)
    x = _zero_surge_score_rescue(x, threshold=th)
    logger.info(
        "[TONOSAMA ENTRY] feature build done base_rows=%s primary_rows=%s five_sec_rows=%s feature_missing=%s pre_5sec_head=%s post_5sec_head=%s elapsed=%.3fs fast_score_prefilter=True",
        base_rows, primary_rows, len(x), feature_missing, before_head,
        _sample_rows(runner, x, [
            "symbol", "symbolname", "close", "_latest_volume", "_body_change_pct", "_signed_body_change_pct",
            "_intrabar_range_pct", "_close_position_pct", "_upper_wick_pct", "_lower_wick_pct",
            "_max_volume_surge_ratio", "_max_price_change_pct", "_slope", "_tonosama_score",
            "has_5sec_bar", "price_change_5s_pct", "volume_surge_ratio_5s", "_volume_surge_failopen",
        ], limit=12),
        time.perf_counter() - started,
    )
    return x


def _infer_side(row: Any) -> str:
    max_chg = _safe_float(row.get("_max_price_change_pct"), 0.0) if hasattr(row, "get") else 0.0
    signed_body = _safe_float(row.get("_signed_body_change_pct"), max_chg) if hasattr(row, "get") else max_chg
    slope = _safe_float(row.get("_slope"), 0.0) if hasattr(row, "get") else 0.0
    if max_chg < 0 or signed_body < 0 or slope < 0:
        return "SELL"
    return "BUY"


def _soft_rescue_ai_ng(row: Any, reason: str) -> tuple[bool, str]:
    if not _env_bool("TONOSAMA_AI_SOFT_RESCUE", True):
        return False, "disabled"
    r = str(reason or "")
    hard_words = (
        "climax", "selling_climax", "buying_climax", "direction_ng", "reverse",
        "upper_wick_reversal", "lower_wick_reversal",
    )
    if any(w in r for w in hard_words):
        return False, "hard_reason"
    if not ("price change low" in r or "slope low" in r):
        return False, "not_soft_reason"

    volume = _safe_float(row.get("_latest_volume"), 0.0) if hasattr(row, "get") else 0.0
    rng = _safe_float(row.get("_intrabar_range_pct"), 0.0) if hasattr(row, "get") else 0.0
    surge = _safe_float(row.get("_max_volume_surge_ratio"), 0.0) if hasattr(row, "get") else 0.0
    price_chg = _safe_float(row.get("_max_price_change_pct"), 0.0) if hasattr(row, "get") else 0.0
    body = _safe_float(row.get("_signed_body_change_pct"), 0.0) if hasattr(row, "get") else 0.0
    slope = _safe_float(row.get("_slope"), 0.0) if hasattr(row, "get") else 0.0
    side = _infer_side(row)

    min_vol = _env_float("TONOSAMA_AI_RESCUE_MIN_VOLUME", 500000.0)
    min_range = _env_float("TONOSAMA_AI_RESCUE_MIN_RANGE_PCT", 4.0)
    min_surge = _env_float("TONOSAMA_AI_RESCUE_MIN_SURGE", 3.0)
    min_chg = _env_float("TONOSAMA_AI_RESCUE_MIN_PRICE_CHANGE_PCT", 0.08)
    min_slope_abs = _env_float("TONOSAMA_AI_RESCUE_MIN_SLOPE_ABS", 0.0003)

    if volume < min_vol:
        return False, "volume_low"
    if rng < min_range:
        return False, "range_low"
    if surge < min_surge:
        return False, "surge_low"
    if abs(price_chg) < min_chg and abs(body) < min_chg and abs(slope) < min_slope_abs:
        return False, "move_low"
    if side == "BUY" and price_chg < -min_chg:
        return False, "buy_price_reverse"
    if side == "SELL" and price_chg > min_chg:
        return False, "sell_price_reverse"

    detail = (
        f"soft_rescue side={side} volume={volume:.0f} range={rng:.3f} "
        f"surge={surge:.2f} price_chg={price_chg:.3f} body={body:.3f} slope={slope:.6f}"
    )
    return True, detail


def _patched_ai_check_tonosama_entry(row: Any):
    if _env_bool("TONOSAMA_FAST_SCORE_AI_SHORT_CIRCUIT", True):
        try:
            import trading.entry.tonosama.runner as runner
            raw_score = _safe_float(row.get("_tonosama_score"), 0.0) if hasattr(row, "get") else 0.0
            th = _threshold(runner)
            if raw_score < th:
                symbol = row.get("symbol", "") if hasattr(row, "get") else ""
                logger.info(
                    "[TONOSAMA FAST SCORE PREFILTER] AI short-circuit symbol=%s raw_score=%.4f threshold=%.4f reason=final_score_low_pre_ai",
                    symbol, raw_score, th,
                )
                return False, 0.0, f"final_score_low_pre_ai raw_score={raw_score:.4f} < {th:.4f}"
        except Exception:
            logger.debug("[TONOSAMA FAST SCORE PREFILTER] AI short-circuit check failed", exc_info=True)

    ok, prob, reason = _ORIG_AI_CHECK(row)
    if ok:
        return ok, prob, reason

    try:
        symbol = row.get("symbol", "") if hasattr(row, "get") else ""
        rescue, detail = _soft_rescue_ai_ng(row, str(reason or ""))
        if rescue:
            logger.warning(
                "[TONOSAMA AI SOFT RESCUE] OK symbol=%s original_reason=%s detail=%s",
                symbol, reason, detail,
            )
            return True, max(_safe_float(prob, 0.0), 0.0), f"AI soft rescue: {detail}; original={reason}"
        logger.info(
            "[TONOSAMA AI SOFT RESCUE] keep NG symbol=%s original_reason=%s no_rescue=%s",
            symbol, reason, detail,
        )
    except Exception:
        logger.debug("[TONOSAMA AI SOFT RESCUE] failed", exc_info=True)
    return ok, prob, reason


def install() -> bool:
    global _PATCHED, _ORIG_BUILD_FEATURE_DF_WITH_5SEC, _ORIG_AI_CHECK
    if _PATCHED:
        return True
    try:
        import trading.entry.tonosama.runner as runner
        import trading.entry.tonosama.ai_gate as ai_gate

        patched = []
        cur_build = getattr(runner, "build_feature_df_with_5sec", None)
        if callable(cur_build) and not getattr(cur_build, "_tonosama_fast_score_prefilter_v3", False):
            _ORIG_BUILD_FEATURE_DF_WITH_5SEC = getattr(cur_build, "_original", cur_build)
            _patched_build_feature_df_with_5sec._tonosama_fast_score_prefilter_v3 = True  # type: ignore[attr-defined]
            _patched_build_feature_df_with_5sec._original = _ORIG_BUILD_FEATURE_DF_WITH_5SEC  # type: ignore[attr-defined]
            runner.build_feature_df_with_5sec = _patched_build_feature_df_with_5sec
            patched.append("runner.build_feature_df_with_5sec")

        cur_ai = getattr(runner, "ai_check_tonosama_entry", None)
        if callable(cur_ai) and not getattr(cur_ai, "_tonosama_fast_score_prefilter_v3", False):
            _ORIG_AI_CHECK = getattr(cur_ai, "_original", cur_ai)
            _patched_ai_check_tonosama_entry._tonosama_fast_score_prefilter_v3 = True  # type: ignore[attr-defined]
            _patched_ai_check_tonosama_entry._original = _ORIG_AI_CHECK  # type: ignore[attr-defined]
            runner.ai_check_tonosama_entry = _patched_ai_check_tonosama_entry
            ai_gate.ai_check_tonosama_entry = _patched_ai_check_tonosama_entry
            patched.append("ai_check_tonosama_entry")

        _PATCHED = True
        logger.warning(
            "[TONOSAMA FAST SCORE PREFILTER] installed v3 patched=%s enabled=%s ratio=%.2f ai_short=%s soft_rescue=%s zero_surge_score_rescue=%s rescue_min_vol=%.0f rescue_min_range=%.2f rescue_min_chg=%.3f",
            patched,
            _env_bool("TONOSAMA_FAST_SCORE_PREFILTER", True),
            _env_float("TONOSAMA_FAST_SCORE_PREFILTER_RATIO", 1.0),
            _env_bool("TONOSAMA_FAST_SCORE_AI_SHORT_CIRCUIT", True),
            _env_bool("TONOSAMA_AI_SOFT_RESCUE", True),
            _env_bool("TONOSAMA_ZERO_SURGE_PREFILTER_SCORE_RESCUE", True),
            _env_float("TONOSAMA_AI_RESCUE_MIN_VOLUME", 500000.0),
            _env_float("TONOSAMA_AI_RESCUE_MIN_RANGE_PCT", 4.0),
            _env_float("TONOSAMA_AI_RESCUE_MIN_PRICE_CHANGE_PCT", 0.08),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA FAST SCORE PREFILTER] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA FAST SCORE PREFILTER] auto install failed")
