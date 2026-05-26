# ============================================================
# File   : core/startup/tonosama_5sec_advisory_patch.py
# Version: V1.0-TONOSAMA-5SEC-ADVISORY-NOT-MANDATORY
# ------------------------------------------------------------
# 【目的】
#   TONOSAMA ENTRY で一次/最終フィルタを通過した候補が、5秒足の
#   price_change_5s_pct=0.0 だけで全落ちする問題を緩和する。
#
# 【背景】
#   ユーザー方針: 「5秒足必須にはしたくない」
#   現行 runner.py Ver1.5 は has_5sec_bar=True の場合、
#     chg_5s > 0.0 and chg_5s >= MIN_5SEC_PRICE_CHANGE_PCT
#   を要求するため、0.0% 横ばいでも候補を捨てる。
#
# 【方針】
#   - trading.entry.tonosama.runner.iter_tonosama_candidate_rows をruntime patch
#   - 一次/最終フィルタは従来どおり維持
#   - 5秒足は「急落NG」だけを見る補助フィルタへ変更
#   - price_change_5s_pct=0.0 は、上位足の価格変化/出来高/値幅/slopeが通っていれば残す
#   - MAX_5SEC_DROP_PCT 以下の急落は従来どおり除外
#
# 【ENV】
#   TONOSAMA_5SEC_ADVISORY_ENABLED=1
#   TONOSAMA_5SEC_ALLOW_ZERO_IF_PRIMARY_PASS=1
# ============================================================

from __future__ import annotations

import logging
import os
import time
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIGINAL_ITER = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _patched_iter_tonosama_candidate_rows() -> pd.DataFrame:
    import trading.entry.tonosama.runner as r

    if not _env_bool("TONOSAMA_5SEC_ADVISORY_ENABLED", True):
        return _ORIGINAL_ITER() if callable(_ORIGINAL_ITER) else pd.DataFrame()

    started = time.perf_counter()
    x = r.build_feature_df_with_5sec()
    if x is None or x.empty:
        return pd.DataFrame()

    x = r._ensure_actual_movement_cols(x)
    sample_cols = [
        "symbol", "symbolname", "close", "_latest_volume", "_body_change_pct",
        "_intrabar_range_pct", "_max_volume_surge_ratio", "_max_price_change_pct",
        "_slope", "_tonosama_score", "has_5sec_bar", "price_change_5s_pct",
        "volume_surge_ratio_5s",
    ]

    before = x.copy()
    x = x[r._num_series(x, "close") > r.MIN_PRICE]
    r._log_filter_step(stage="final", before=before, after=x, reason="close_below_min_price", threshold={"MIN_PRICE": r.MIN_PRICE}, sample_cols=sample_cols)

    before = x.copy()
    x = x[r._num_series(x, "_latest_volume") >= r.MIN_LATEST_VOLUME]
    r._log_filter_step(stage="final", before=before, after=x, reason="latest_volume_low_flat_alert_guard", threshold={"MIN_LATEST_VOLUME": r.MIN_LATEST_VOLUME}, sample_cols=sample_cols)

    before = x.copy()
    x = x[r._num_series(x, "_body_change_pct") >= r.MIN_BODY_CHANGE_PCT]
    r._log_filter_step(stage="final", before=before, after=x, reason="body_change_low_flat_alert_guard", threshold={"MIN_BODY_CHANGE_PCT": r.MIN_BODY_CHANGE_PCT}, sample_cols=sample_cols)

    before = x.copy()
    x = x[r._num_series(x, "_intrabar_range_pct") >= r.MIN_INTRABAR_RANGE_PCT]
    r._log_filter_step(stage="final", before=before, after=x, reason="intrabar_range_low_flat_alert_guard", threshold={"MIN_INTRABAR_RANGE_PCT": r.MIN_INTRABAR_RANGE_PCT}, sample_cols=sample_cols)

    before = x.copy()
    x = x[r._num_series(x, "_max_volume_surge_ratio") >= r.MIN_VOLUME_SURGE_RATIO]
    r._log_filter_step(stage="final", before=before, after=x, reason="volume_surge_low", threshold={"MIN_VOLUME_SURGE_RATIO": r.MIN_VOLUME_SURGE_RATIO}, sample_cols=sample_cols)

    before = x.copy()
    x = x[r._num_series(x, "_max_price_change_pct") >= r.MIN_PRICE_CHANGE_PCT]
    r._log_filter_step(stage="final", before=before, after=x, reason="price_change_low", threshold={"MIN_PRICE_CHANGE_PCT": r.MIN_PRICE_CHANGE_PCT}, sample_cols=sample_cols)

    before = x.copy()
    x = x[r._num_series(x, "_slope") >= r.MIN_SLOPE]
    r._log_filter_step(stage="final", before=before, after=x, reason="slope_too_small", threshold={"MIN_SLOPE": r.MIN_SLOPE}, sample_cols=sample_cols)

    if r.USE_5SEC_CONFIRM and "has_5sec_bar" in x.columns:
        if r.REQUIRE_5SEC_BAR:
            before = x.copy()
            x = x[r._bool_series(x, "has_5sec_bar")]
            r._log_filter_step(stage="5sec", before=before, after=x, reason="missing_5sec_bar", threshold={"REQUIRE_5SEC_BAR": r.REQUIRE_5SEC_BAR}, sample_cols=sample_cols)

        before = x.copy()
        has_bar = r._bool_series(x, "has_5sec_bar")
        chg_5s = r._num_series(x, "price_change_5s_pct")

        # Advisory mode:
        # - 5秒足が無い場合は通す
        # - 5秒足が0.0%の場合も、一次/最終フィルタ通過済みなら通す
        # - ただし MAX_5SEC_DROP_PCT 以下の急落は止める
        if _env_bool("TONOSAMA_5SEC_ALLOW_ZERO_IF_PRIMARY_PASS", True):
            mask = (~has_bar) | (chg_5s >= r.MAX_5SEC_DROP_PCT)
            reason = "five_sec_advisory_drop_only"
            threshold = {
                "MIN_5SEC_PRICE_CHANGE_PCT": r.MIN_5SEC_PRICE_CHANGE_PCT,
                "REQUIRE_POSITIVE_5SEC_CHANGE": False,
                "ALLOW_ZERO_IF_PRIMARY_PASS": True,
                "MAX_5SEC_DROP_PCT": r.MAX_5SEC_DROP_PCT,
                "REQUIRE_5SEC_BAR": r.REQUIRE_5SEC_BAR,
            }
        else:
            mask = (~has_bar) | ((chg_5s >= r.MIN_5SEC_PRICE_CHANGE_PCT) & (chg_5s > r.MAX_5SEC_DROP_PCT))
            reason = "five_sec_advisory_min_change"
            threshold = {
                "MIN_5SEC_PRICE_CHANGE_PCT": r.MIN_5SEC_PRICE_CHANGE_PCT,
                "REQUIRE_POSITIVE_5SEC_CHANGE": False,
                "ALLOW_ZERO_IF_PRIMARY_PASS": False,
                "MAX_5SEC_DROP_PCT": r.MAX_5SEC_DROP_PCT,
                "REQUIRE_5SEC_BAR": r.REQUIRE_5SEC_BAR,
            }

        x = x[mask]
        r._log_filter_step(stage="5sec", before=before, after=x, reason=reason, threshold=threshold, sample_cols=sample_cols)
    elif r.USE_5SEC_CONFIRM:
        logger.warning("[TONOSAMA 5SEC ADVISORY PATCH] has_5sec_bar column missing cols=%s", list(x.columns))

    before = x.copy()
    x = x[r._num_series(x, "_tonosama_score") >= r.MIN_RAW_SCORE]
    r._log_filter_step(stage="score", before=before, after=x, reason="raw_score_low", threshold={"MIN_RAW_SCORE": r.MIN_RAW_SCORE}, sample_cols=sample_cols)

    if x.empty:
        logger.info(
            "[TONOSAMA ENTRY] no scalping candidates after advisory 5sec filters diag=%s elapsed=%.3fs",
            getattr(r, "_LAST_FILTER_DIAG", {}),
            time.perf_counter() - started,
        )
        return pd.DataFrame()

    out = x.sort_values("_tonosama_score", ascending=False).head(r.MAX_CANDIDATES).reset_index(drop=True)
    logger.warning(
        "[TONOSAMA 5SEC ADVISORY PATCH] candidates ready rows=%s head=%s elapsed=%.3fs",
        len(out),
        r._sample_rows(out, sample_cols, limit=12),
        time.perf_counter() - started,
    )
    return out


def install() -> bool:
    global _PATCHED, _ORIGINAL_ITER
    if _PATCHED:
        return True
    try:
        import trading.entry.tonosama.runner as r

        cur = getattr(r, "iter_tonosama_candidate_rows", None)
        if not callable(cur):
            logger.warning("[TONOSAMA 5SEC ADVISORY PATCH] target iter not callable")
            return False
        if getattr(cur, "_tonosama_5sec_advisory_patch", False):
            _PATCHED = True
            return True

        _ORIGINAL_ITER = cur
        _patched_iter_tonosama_candidate_rows._tonosama_5sec_advisory_patch = True  # type: ignore[attr-defined]
        _patched_iter_tonosama_candidate_rows._original = cur  # type: ignore[attr-defined]
        r.iter_tonosama_candidate_rows = _patched_iter_tonosama_candidate_rows
        _PATCHED = True
        logger.warning("[TONOSAMA 5SEC ADVISORY PATCH] installed allow_zero=%s", _env_bool("TONOSAMA_5SEC_ALLOW_ZERO_IF_PRIMARY_PASS", True))
        return True
    except Exception:
        logger.exception("[TONOSAMA 5SEC ADVISORY PATCH] install failed")
        return False


__all__ = ["install"]
