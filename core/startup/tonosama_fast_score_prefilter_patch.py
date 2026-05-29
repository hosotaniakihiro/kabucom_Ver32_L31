# ============================================================
# File   : core/startup/tonosama_fast_score_prefilter_patch.py
# Version: V1-TONOSAMA-FAST-SCORE-PREFILTER
# ------------------------------------------------------------
# 目的:
#   殿様イナゴの処理時間が candidates=11 registered=0 でも19秒程度かかる問題を軽減する。
#
# 背景:
#   runner.py では以下の順序になっている。
#     1) base feature生成
#     2) primary filter
#     3) prepare_entry_scores
#     4) 5秒特徴取得
#     5) final filter
#     6) AI/fallback判定
#     7) final_score < MIN_FINAL_SCORE で低スコア除外
#
#   ログでは low_score_samples reason=final_score_low が多く、
#   AI/fallbackや後段処理後に落ちている。
#
# 方針:
#   - 5秒特徴取得前に _tonosama_score が低すぎる候補を早期除外する。
#   - AI/fallback呼び出し前にも raw_score < MIN_FINAL_SCORE を即NGにする。
#   - TONOSAMAだけに作用し、SUMMARY/RANKINGには影響しない。
#
# ENV:
#   TONOSAMA_FAST_SCORE_PREFILTER=1             # default enabled
#   TONOSAMA_FAST_SCORE_PREFILTER_RATIO=1.00    # MIN_FINAL_SCORE * ratio
#   TONOSAMA_FAST_SCORE_PREFILTER_MIN=0         # 0ならMIN_FINAL_SCORE使用
#   TONOSAMA_FAST_SCORE_AI_SHORT_CIRCUIT=1
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
        return float(v)
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


def _patched_build_feature_df_with_5sec() -> pd.DataFrame:
    """runner.build_feature_df_with_5sec の軽量置換。

    ほぼ元処理と同じだが、5秒特徴取得前に _tonosama_score の早期フィルタを追加する。
    """
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
            base_rows,
            primary_rows,
            getattr(runner, "_LAST_FILTER_DIAG", {}),
            time.perf_counter() - started,
        )
        return pd.DataFrame()

    try:
        x = runner.prepare_entry_scores(x)
        if "_tonosama_score" in x.columns:
            x = x.sort_values("_tonosama_score", ascending=False)
    except Exception:
        logger.warning("[TONOSAMA ENTRY] pre 5sec prepare_entry_scores failed", exc_info=True)

    sample_cols = [
        "symbol",
        "symbolname",
        "close",
        "_latest_volume",
        "_body_change_pct",
        "_signed_body_change_pct",
        "_intrabar_range_pct",
        "_close_position_pct",
        "_upper_wick_pct",
        "_lower_wick_pct",
        "_max_volume_surge_ratio",
        "_max_price_change_pct",
        "_slope",
        "_tonosama_score",
        "score",
        "final_score",
        "score_mtf",
        "mtf",
    ]

    # ここが追加点: 5秒特徴取得前に、どう見てもMIN_FINAL_SCOREへ届かない候補を落とす。
    if "_tonosama_score" in x.columns:
        before = x.copy()
        th = _threshold(runner)
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
                len(before),
                len(x),
                th,
                _sample_rows(runner, before, sample_cols, limit=8),
            )
        if x.empty:
            logger.info(
                "[TONOSAMA ENTRY] no candidates after fast score prefilter base_rows=%s primary_rows=%s threshold=%.3f elapsed=%.3fs",
                base_rows,
                primary_rows,
                th,
                time.perf_counter() - started,
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
    logger.info(
        "[TONOSAMA ENTRY] feature build done base_rows=%s primary_rows=%s five_sec_rows=%s feature_missing=%s pre_5sec_head=%s post_5sec_head=%s elapsed=%.3fs fast_score_prefilter=True",
        base_rows,
        primary_rows,
        len(x),
        feature_missing,
        before_head,
        _sample_rows(runner, x, [
            "symbol", "symbolname", "close", "_latest_volume", "_body_change_pct", "_signed_body_change_pct",
            "_intrabar_range_pct", "_close_position_pct", "_upper_wick_pct", "_lower_wick_pct",
            "_max_volume_surge_ratio", "_max_price_change_pct", "_slope", "_tonosama_score",
            "has_5sec_bar", "price_change_5s_pct", "volume_surge_ratio_5s"
        ], limit=12),
        time.perf_counter() - started,
    )
    return x


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
                    symbol,
                    raw_score,
                    th,
                )
                return False, 0.0, f"final_score_low_pre_ai raw_score={raw_score:.4f} < {th:.4f}"
        except Exception:
            logger.debug("[TONOSAMA FAST SCORE PREFILTER] AI short-circuit check failed", exc_info=True)
    return _ORIG_AI_CHECK(row)


def install() -> bool:
    global _PATCHED, _ORIG_BUILD_FEATURE_DF_WITH_5SEC, _ORIG_AI_CHECK
    if _PATCHED:
        return True
    try:
        import trading.entry.tonosama.runner as runner
        import trading.entry.tonosama.ai_gate as ai_gate

        patched = []
        cur_build = getattr(runner, "build_feature_df_with_5sec", None)
        if callable(cur_build) and not getattr(cur_build, "_tonosama_fast_score_prefilter_v1", False):
            _ORIG_BUILD_FEATURE_DF_WITH_5SEC = cur_build
            _patched_build_feature_df_with_5sec._tonosama_fast_score_prefilter_v1 = True  # type: ignore[attr-defined]
            runner.build_feature_df_with_5sec = _patched_build_feature_df_with_5sec
            patched.append("runner.build_feature_df_with_5sec")

        cur_ai = getattr(runner, "ai_check_tonosama_entry", None)
        if callable(cur_ai) and not getattr(cur_ai, "_tonosama_fast_score_prefilter_v1", False):
            _ORIG_AI_CHECK = cur_ai
            _patched_ai_check_tonosama_entry._tonosama_fast_score_prefilter_v1 = True  # type: ignore[attr-defined]
            runner.ai_check_tonosama_entry = _patched_ai_check_tonosama_entry
            ai_gate.ai_check_tonosama_entry = _patched_ai_check_tonosama_entry
            patched.append("ai_check_tonosama_entry")

        _PATCHED = True
        logger.warning(
            "[TONOSAMA FAST SCORE PREFILTER] installed patched=%s enabled=%s ratio=%.2f ai_short=%s",
            patched,
            _env_bool("TONOSAMA_FAST_SCORE_PREFILTER", True),
            _env_float("TONOSAMA_FAST_SCORE_PREFILTER_RATIO", 1.0),
            _env_bool("TONOSAMA_FAST_SCORE_AI_SHORT_CIRCUIT", True),
        )
        return True
    except Exception:
        logger.exception("[TONOSAMA FAST SCORE PREFILTER] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[TONOSAMA FAST SCORE PREFILTER] auto install failed")


__all__ = ["install"]
