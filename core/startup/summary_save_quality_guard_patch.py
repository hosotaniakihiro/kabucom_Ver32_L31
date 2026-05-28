# ============================================================
# File   : core/startup/summary_save_quality_guard_patch.py
# Version: Ver1-SUMMARY-SAVE-ZERO-TECH-GUARD
# ------------------------------------------------------------
# 目的:
#   本日summary DBへ OHLCだけ入って score/rsi/macd/signal/mtf 等が
#   0/欠損のまま保存される事故を防ぐ。
#
# 背景:
#   起動直後やPUSH初期seedでは、価格OHLCはあるが指標未計算のDFが
#   一時的に流れることがある。
#   それをDBへUPSERTすると、その時刻の本日summary行が
#   「項目未格納/0埋め」の状態になる。
#
# 方針:
#   - bootstrap/rebuild/recovery/backfill/repair など保守系保存は通す
#   - 通常の push/ranking/periodic/cache_writer 保存だけ検査する
#   - 価格はあるが score系・technical系が全て0/欠損ならDB保存しない
#   - cache保存や表示は既存処理に任せる
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_BULK = None
_ORIG_SAVE_BULK = None
_ORIG_SAVE_DF = None


_SCORE_COLS = [
    "score", "score_total", "final_score", "display_score",
    "score_buy", "score_sell", "score_slope", "score_mtf", "mtf_score",
]
_TECH_COLS = [
    "slope", "slope_atr_scaled", "rsi", "macd", "signal", "hist",
    "mtf", "score_mtf", "ma5", "ma25", "ma75", "atr", "vwap",
]
_PRICE_COLS = ["close", "close_price", "price", "current_price", "open", "open_price"]
_MAINT_WORDS = (
    "bootstrap", "rebuild", "recovery", "recover", "backfill", "repair",
    "migrate", "migration", "historical", "history", "catchup", "startup",
    "yahoo", "night", "lunch",
)
_TARGET_WORDS = (
    "push", "ranking", "periodic", "tick", "display", "latest", "cache_writer", "scheduled", "scheduler",
)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _is_maintenance_reason(reason: str) -> bool:
    r = str(reason or "").strip().lower()
    return any(w in r for w in _MAINT_WORDS)


def _is_target_reason(reason: str) -> bool:
    r = str(reason or "").strip().lower()
    if not r:
        return True
    return any(w in r for w in _TARGET_WORDS)


def _num_sum_abs(df: pd.DataFrame, cols: list[str]) -> float:
    use = [c for c in cols if c in df.columns]
    if not use:
        return 0.0
    total = 0.0
    for c in use:
        try:
            s = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
            total += float(s.abs().sum())
        except Exception:
            pass
    return float(total)


def _has_price(df: pd.DataFrame) -> bool:
    for c in _PRICE_COLS:
        if c not in df.columns:
            continue
        try:
            s = pd.to_numeric(df[c], errors="coerce")
            if bool((s.notna() & (s != 0)).any()):
                return True
        except Exception:
            pass
    return False


def _looks_uncomputed_for_db(df: Any, *, save_reason: str = "", interval: int | None = None) -> tuple[bool, dict[str, Any]]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return False, {"reason": "empty_or_not_df"}

    if _is_maintenance_reason(save_reason):
        return False, {"reason": "maintenance_save_reason", "save_reason": save_reason}

    if not _is_target_reason(save_reason):
        return False, {"reason": "non_target_save_reason", "save_reason": save_reason}

    has_price = _has_price(df)
    score_abs = _num_sum_abs(df, _SCORE_COLS)
    tech_abs = _num_sum_abs(df, _TECH_COLS)
    score_cols = [c for c in _SCORE_COLS if c in df.columns]
    tech_cols = [c for c in _TECH_COLS if c in df.columns]

    # priceだけあるがscore/technicalが完全に0または欠損なら未計算とみなす。
    bad = bool(has_price and score_abs == 0.0 and tech_abs == 0.0)

    diag = {
        "rows": len(df),
        "cols": len(df.columns),
        "interval": interval,
        "save_reason": save_reason,
        "has_price": has_price,
        "score_abs": score_abs,
        "tech_abs": tech_abs,
        "score_cols": score_cols,
        "tech_cols": tech_cols,
    }
    try:
        if "datetime" in df.columns:
            dt = pd.to_datetime(df["datetime"], errors="coerce")
            diag["latest_dt"] = str(dt.max())
            diag["earliest_dt"] = str(dt.min())
        if "symbol" in df.columns:
            diag["symbols"] = int(df["symbol"].astype(str).nunique())
    except Exception:
        pass
    return bad, diag


def _guarded_call(orig, df: pd.DataFrame, interval: int, *args: Any, **kwargs: Any) -> int:
    save_reason = str(kwargs.get("save_reason", "") or "")
    enabled = _env_bool("SUMMARY_SAVE_ZERO_TECH_GUARD", True)
    if enabled:
        bad, diag = _looks_uncomputed_for_db(df, save_reason=save_reason, interval=int(interval))
        if bad:
            logger.warning(
                "[SUMMARY SAVE QUALITY GUARD] skip DB save uncomputed-zero-tech interval=%s diag=%s",
                interval,
                diag,
            )
            return 0
    return orig(df, interval, *args, **kwargs)


def install() -> bool:
    global _INSTALLED, _ORIG_BULK, _ORIG_SAVE_BULK, _ORIG_SAVE_DF
    if _INSTALLED:
        return True
    try:
        import trading.summary.persistence.summary_saver_bulk as mod

        _ORIG_BULK = getattr(mod, "bulk_upsert_summary", None)
        _ORIG_SAVE_BULK = getattr(mod, "save_summary_bulk", None)
        _ORIG_SAVE_DF = getattr(mod, "save_summary_df", None)

        if callable(_ORIG_BULK):
            def bulk_upsert_summary(df, interval, *args, **kwargs):
                return _guarded_call(_ORIG_BULK, df, interval, *args, **kwargs)
            bulk_upsert_summary._summary_save_quality_guard_v1 = True  # type: ignore[attr-defined]
            mod.bulk_upsert_summary = bulk_upsert_summary

        if callable(_ORIG_SAVE_BULK):
            def save_summary_bulk(df, interval, *args, **kwargs):
                return _guarded_call(_ORIG_SAVE_BULK, df, interval, *args, **kwargs)
            save_summary_bulk._summary_save_quality_guard_v1 = True  # type: ignore[attr-defined]
            mod.save_summary_bulk = save_summary_bulk

        if callable(_ORIG_SAVE_DF):
            def save_summary_df(df, interval, *args, **kwargs):
                return _guarded_call(_ORIG_SAVE_DF, df, interval, *args, **kwargs)
            save_summary_df._summary_save_quality_guard_v1 = True  # type: ignore[attr-defined]
            mod.save_summary_df = save_summary_df

        _INSTALLED = True
        logger.warning("[SUMMARY SAVE QUALITY GUARD] installed v1 enabled=%s", _env_bool("SUMMARY_SAVE_ZERO_TECH_GUARD", True))
        return True
    except Exception:
        logger.exception("[SUMMARY SAVE QUALITY GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY SAVE QUALITY GUARD] auto install failed")

__all__ = ["install"]
