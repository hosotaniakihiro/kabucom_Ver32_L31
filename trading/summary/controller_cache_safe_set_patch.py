# ============================================================
# File   : trading/summary/controller_cache_safe_set_patch.py
# Version: PRODUCTION-STABLE-CONTROLLER-CACHE-SAFE-SET-PATCH-V1
# ------------------------------------------------------------
# Purpose:
#   controller_cache.safe_global_set_merged_summary() の安全化パッチ。
#
# Why:
#   ログ上、summary_controller の途中では slope / rsi / macd / signal が
#   入っているにもかかわらず、後段の display_ready 用短履歴データが
#   MERGED SET される時に technical columns が NaN / 0 となり、
#   既存の良いテクニカル値を上書きしていた。
#
# Example:
#   before:
#     [MERGED SET INPUT] tf=1 source=push nonzero slope=28 rsi=40 macd=25
#   later:
#     [MERGED SET INPUT] tf=1 source=push nonzero slope=0 rsi=0 macd=0
#     slope/rsi/macd が NaN のまま STORED
#
# Fix:
#   - set_merged_summary 前に既存 push merged summary を読む
#   - 同一 symbol の既存テクニカル列を候補側へ安全に移植
#   - 候補側が NaN / 空 / 0 で、既存側が有効な場合だけ補完
#   - close/price/score など最新性が重要な列は補完しない
#   - 元の controller_cache.py 本体を壊さず monkey patch する
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

_PATCHED = False

TECHNICAL_PRESERVE_COLUMNS = (
    "slope",
    "slope_atr_scaled",
    "score_slope",
    "rsi",
    "macd",
    "signal",
    "ma5",
    "ma25",
    "ma75",
    "atr",
    "atr_1m",
    "hist",
    "mtf",
    "score_mtf",
    "mtf_score",
    "mtf_alignment",
    "technical_ready",
)


def _safe_numeric_nonzero(df: pd.DataFrame, cols: tuple[str, ...]) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return 0
        best = 0
        for c in cols:
            if c in df.columns:
                s = pd.to_numeric(df[c], errors="coerce").fillna(0)
                best = max(best, int((s != 0).sum()))
        return best
    except Exception:
        return 0


def _safe_numeric_nonnull(df: pd.DataFrame, cols: tuple[str, ...]) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return 0
        best = 0
        for c in cols:
            if c in df.columns:
                s = pd.to_numeric(df[c], errors="coerce")
                best = max(best, int(s.notna().sum()))
        return best
    except Exception:
        return 0


def _profile(df: pd.DataFrame) -> dict[str, int]:
    return {
        "rows": int(len(df)) if isinstance(df, pd.DataFrame) else 0,
        "symbols": int(df["symbol"].astype(str).nunique()) if isinstance(df, pd.DataFrame) and not df.empty and "symbol" in df.columns else 0,
        "slope": _safe_numeric_nonzero(df, ("slope", "slope_atr_scaled")),
        "rsi": _safe_numeric_nonnull(df, ("rsi",)),
        "macd": _safe_numeric_nonzero(df, ("macd",)),
        "signal": _safe_numeric_nonzero(df, ("signal",)),
        "ma": _safe_numeric_nonnull(df, ("ma5", "ma25", "ma75")),
        "atr": _safe_numeric_nonnull(df, ("atr", "atr_1m")),
    }


def _is_missing_or_zero(series: pd.Series) -> pd.Series:
    try:
        s_num = pd.to_numeric(series, errors="coerce")
        return series.isna() | s_num.isna() | s_num.fillna(0).eq(0)
    except Exception:
        try:
            return series.isna() | series.astype(str).str.strip().isin(["", "nan", "None", "<NA>", "0", "0.0"])
        except Exception:
            return pd.Series(True, index=series.index)


def _existing_value_valid(series: pd.Series, *, allow_zero: bool = False) -> pd.Series:
    try:
        if allow_zero:
            return series.notna()
        s_num = pd.to_numeric(series, errors="coerce")
        return series.notna() & s_num.notna() & s_num.fillna(0).ne(0)
    except Exception:
        try:
            return series.notna() & ~series.astype(str).str.strip().isin(["", "nan", "None", "<NA>"])
        except Exception:
            return pd.Series(False, index=series.index)


def _latest_by_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return pd.DataFrame()

    x = df.copy()
    try:
        x["symbol"] = x["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        x = x[x["symbol"] != ""]
    except Exception:
        pass

    try:
        if "datetime" in x.columns:
            x["__dt"] = pd.to_datetime(x["datetime"], errors="coerce")
            x = x.sort_values(["symbol", "__dt"], kind="mergesort")
        else:
            x = x.sort_values(["symbol"], kind="mergesort")
    except Exception:
        pass

    try:
        x = x.drop_duplicates(["symbol"], keep="last").reset_index(drop=True)
    except Exception:
        pass

    try:
        if "__dt" in x.columns:
            x = x.drop(columns=["__dt"])
    except Exception:
        pass

    return x


def _read_existing_push_merged(interval: int) -> pd.DataFrame:
    try:
        from global_state import global_data
    except Exception:
        return pd.DataFrame()

    try:
        getter = getattr(global_data, "get_merged_summary", None)
        if callable(getter):
            try:
                df = getter(interval, source="push")
            except TypeError:
                df = getter(interval)
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df.copy()
    except Exception:
        logger.debug("[summary_controller_safe_set_patch] get_merged_summary failed", exc_info=True)

    try:
        getter = getattr(global_data, "get_push_merged_summary", None)
        if callable(getter):
            df = getter(interval)
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df.copy()
    except Exception:
        logger.debug("[summary_controller_safe_set_patch] get_push_merged_summary failed", exc_info=True)

    try:
        df = getattr(global_data, f"merged_summary_{int(interval)}", None)
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df.copy()
    except Exception:
        pass

    return pd.DataFrame()


def enrich_candidate_with_existing_technicals(
    *,
    interval: int,
    candidate: pd.DataFrame,
    existing: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """
    candidate の OHLC/score は維持し、テクニカル列だけ既存値で補完する。
    """
    if not isinstance(candidate, pd.DataFrame) or candidate.empty:
        return candidate

    if "symbol" not in candidate.columns:
        return candidate

    existing_df = existing if isinstance(existing, pd.DataFrame) else _read_existing_push_merged(interval)
    if not isinstance(existing_df, pd.DataFrame) or existing_df.empty or "symbol" not in existing_df.columns:
        return candidate

    cand = candidate.copy()
    old = _latest_by_symbol(existing_df)
    if old.empty:
        return cand

    try:
        cand["symbol"] = cand["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        old["symbol"] = old["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    except Exception:
        pass

    old_cols = [c for c in TECHNICAL_PRESERVE_COLUMNS if c in old.columns]
    if not old_cols:
        return cand

    before = _profile(cand)
    existing_profile = _profile(old)

    merged = cand.merge(
        old[["symbol"] + old_cols],
        on="symbol",
        how="left",
        suffixes=("", "__oldtech"),
    )

    replaced_total = 0

    for col in old_cols:
        old_col = f"{col}__oldtech"
        if old_col not in merged.columns:
            continue

        if col not in merged.columns:
            merged[col] = merged[old_col]
            replaced_total += int(merged[old_col].notna().sum())
            continue

        allow_zero = col in {"technical_ready"}
        missing = _is_missing_or_zero(merged[col])
        valid_old = _existing_value_valid(merged[old_col], allow_zero=allow_zero)
        mask = missing & valid_old

        if bool(mask.any()):
            merged.loc[mask, col] = merged.loc[mask, old_col]
            replaced_total += int(mask.sum())

    drop_cols = [c for c in merged.columns if c.endswith("__oldtech")]
    if drop_cols:
        merged = merged.drop(columns=drop_cols)

    after = _profile(merged)

    if replaced_total > 0:
        logger.warning(
            "[summary_controller_safe_set_patch] enriched candidate technicals interval=%s replaced=%s before=%s existing=%s after=%s",
            interval,
            replaced_total,
            before,
            existing_profile,
            after,
        )
    else:
        logger.info(
            "[summary_controller_safe_set_patch] no technical enrichment needed interval=%s candidate=%s existing=%s",
            interval,
            before,
            existing_profile,
        )

    return merged


def install_controller_cache_safe_set_patch() -> bool:
    global _PATCHED

    if _PATCHED:
        return True

    try:
        from trading.summary import controller_cache as target
    except Exception:
        logger.exception("[summary_controller_safe_set_patch] import controller_cache failed")
        return False

    if getattr(target, "_safe_set_patch_v1_installed", False):
        _PATCHED = True
        return True

    orig_set = getattr(target, "safe_global_set_merged_summary", None)
    if not callable(orig_set):
        logger.warning("[summary_controller_safe_set_patch] target safe_global_set_merged_summary not callable")
        return False

    def safe_global_set_merged_summary_patched(interval: int, df: pd.DataFrame) -> None:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return orig_set(interval, df)

        try:
            existing = _read_existing_push_merged(int(interval))
            payload = enrich_candidate_with_existing_technicals(
                interval=int(interval),
                candidate=df,
                existing=existing,
            )
            return orig_set(interval, payload)
        except Exception:
            logger.exception(
                "[summary_controller_safe_set_patch] patched set failed interval=%s -> fallback original",
                interval,
            )
            return orig_set(interval, df)

    target.safe_global_set_merged_summary = safe_global_set_merged_summary_patched
    target._safe_set_patch_v1_installed = True
    _PATCHED = True

    logger.warning("[summary_controller_safe_set_patch] installed V1")
    return True


try:
    install_controller_cache_safe_set_patch()
except Exception:
    logger.exception("[summary_controller_safe_set_patch] auto install failed")


__all__ = [
    "install_controller_cache_safe_set_patch",
    "enrich_candidate_with_existing_technicals",
]
