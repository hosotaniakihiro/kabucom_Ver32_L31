# ============================================================
# File   : trading/summary/controller_cache_safe_set_patch.py
# Version: PRODUCTION-STABLE-CONTROLLER-CACHE-SAFE-SET-PATCH-V2
# ------------------------------------------------------------
# Purpose:
#   controller_cache.safe_global_set_merged_summary() の安全化パッチ。
#
# Why:
#   summary_controller の途中では slope / rsi / macd / signal が入っていても、
#   後段の display_ready 用 short/latest-only データが MERGED SET されると、
#   ENTRY / AI が slope=0, macd=0, signal=0, symbol_hist_len=1 の未成熟データを
#   参照して候補 0 件になりやすい。
#
# Fix:
#   - 既存 push merged summary のテクニカル列を候補側へ安全に移植する。
#   - close/price/score など最新性が重要な列は補完しない。
#   - 補完後も hist<5 かつ slope/macd/signal 等が全ゼロの short/latest-only 候補は、
#     既存 cache がある場合 merged cache を上書きしない。
#   - TOP10 表示用の短履歴データは latest/display 側で使えるようにし、
#     ENTRY / AI 用 merged cache の技術指標汚染だけを止める。
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
    "hist",
    "ma5",
    "ma25",
    "ma75",
    "atr",
    "atr_1m",
    "atr_3m",
    "atr_5m",
    "mtf",
    "score_mtf",
    "mtf_score",
    "mtf_alignment",
    "technical_ready",
    "symbol_hist_len",
)

TECH_ZERO_COLUMNS = (
    "slope",
    "slope_atr_scaled",
    "score_slope",
    "macd",
    "signal",
    "hist",
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


def _safe_bool_true(df: pd.DataFrame, col: str) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or col not in df.columns:
            return 0
        return int(pd.Series(df[col]).fillna(False).astype(bool).sum())
    except Exception:
        return 0


def _hist_max(df: pd.DataFrame) -> float:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol_hist_len" not in df.columns:
            return 0.0
        s = pd.to_numeric(df["symbol_hist_len"], errors="coerce").fillna(0)
        return float(s.max()) if not s.empty else 0.0
    except Exception:
        return 0.0


def _hist_ge5(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol_hist_len" not in df.columns:
            return 0
        s = pd.to_numeric(df["symbol_hist_len"], errors="coerce").fillna(0)
        return int((s >= 5).sum())
    except Exception:
        return 0


def _symbol_count(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return 0
        s = df["symbol"].fillna("").astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        s = s.replace({"": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA})
        return int(s.dropna().nunique())
    except Exception:
        return 0


def _datetime_nunique(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "datetime" not in df.columns:
            return 0
        s = pd.to_datetime(df["datetime"], errors="coerce")
        return int(s.dropna().nunique())
    except Exception:
        return 0


def _latest_dt(df: pd.DataFrame):
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        for c in ("datetime", "end_time", "snapshot_time", "tick_time"):
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce").dropna()
                if not s.empty:
                    return s.max()
    except Exception:
        return None
    return None


def _profile(df: pd.DataFrame) -> dict[str, Any]:
    return {
        "rows": int(len(df)) if isinstance(df, pd.DataFrame) else 0,
        "symbols": _symbol_count(df),
        "unique_dt": _datetime_nunique(df),
        "latest_dt": str(_latest_dt(df)),
        "slope": _safe_numeric_nonzero(df, ("slope", "slope_atr_scaled", "score_slope")),
        "rsi": _safe_numeric_nonnull(df, ("rsi",)),
        "macd": _safe_numeric_nonzero(df, ("macd",)),
        "signal": _safe_numeric_nonzero(df, ("signal",)),
        "hist": _safe_numeric_nonzero(df, ("hist",)),
        "ma": _safe_numeric_nonnull(df, ("ma5", "ma25", "ma75")),
        "atr": _safe_numeric_nonnull(df, ("atr", "atr_1m", "atr_3m", "atr_5m")),
        "mtf": _safe_numeric_nonzero(df, ("mtf", "score_mtf", "mtf_score", "mtf_alignment")),
        "technical_ready": _safe_bool_true(df, "technical_ready"),
        "display_ready": _safe_bool_true(df, "display_ready"),
        "hist_ge5": _hist_ge5(df),
        "hist_max": _hist_max(df),
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


def _looks_entry_immature_latest_only(df: pd.DataFrame) -> bool:
    """
    ENTRY / AI 用 merged cache を汚染しやすい short/latest-only summary を検出する。

    display_ready は TOP10 表示用に緩くてもよいが、ENTRY / AI は最低限 5 本以上の履歴、
    または slope/macd/signal/hist のどれかが非ゼロであることを要求する。
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return True

    p = _profile(df)
    tech_nonzero = max(int(p["slope"]), int(p["macd"]), int(p["signal"]), int(p["hist"]))

    # symbol_hist_len がある場合は最優先で見る。
    if "symbol_hist_len" in df.columns and float(p["hist_max"]) < 5 and tech_nonzero <= 0:
        return True

    # symbol_hist_len がない場合でも、全銘柄が同一時刻近辺の1本だけで技術指標ゼロなら latest-only とみなす。
    rows = int(p["rows"])
    symbols = int(p["symbols"])
    unique_dt = int(p["unique_dt"])
    if rows > 0 and symbols > 0 and unique_dt <= 2 and tech_nonzero <= 0:
        if rows <= symbols * 2:
            return True

    return False


def _existing_is_better_for_entry(existing: pd.DataFrame, candidate: pd.DataFrame) -> bool:
    if not isinstance(existing, pd.DataFrame) or existing.empty:
        return False
    if not isinstance(candidate, pd.DataFrame) or candidate.empty:
        return True

    ex = _profile(existing)
    cand = _profile(candidate)

    ex_tech = max(int(ex["slope"]), int(ex["macd"]), int(ex["signal"]), int(ex["hist"]))
    cand_tech = max(int(cand["slope"]), int(cand["macd"]), int(cand["signal"]), int(cand["hist"]))

    if float(ex["hist_max"]) >= 5 and float(cand["hist_max"]) < 5:
        return True
    if ex_tech > cand_tech and float(cand["hist_max"]) < 5:
        return True
    if int(ex["technical_ready"]) > 0 and int(cand["technical_ready"]) <= 0:
        return True

    return False


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

    if getattr(target, "_safe_set_patch_v2_installed", False):
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
            interval_i = int(interval)
            existing = _read_existing_push_merged(interval_i)
            payload = enrich_candidate_with_existing_technicals(
                interval=interval_i,
                candidate=df,
                existing=existing,
            )

            if (
                isinstance(existing, pd.DataFrame)
                and not existing.empty
                and _looks_entry_immature_latest_only(payload)
                and _existing_is_better_for_entry(existing, payload)
            ):
                logger.warning(
                    "[summary_controller_safe_set_patch] blocked immature/latest-only merged overwrite interval=%s candidate=%s existing=%s",
                    interval_i,
                    _profile(payload),
                    _profile(existing),
                )
                return None

            return orig_set(interval_i, payload)
        except Exception:
            logger.exception(
                "[summary_controller_safe_set_patch] patched set failed interval=%s -> fallback original",
                interval,
            )
            return orig_set(interval, df)

    target.safe_global_set_merged_summary = safe_global_set_merged_summary_patched
    target._safe_set_patch_v2_installed = True
    # Backward-compatible marker: avoid another V1 installer treating this as unpatched.
    target._safe_set_patch_v1_installed = True
    _PATCHED = True

    logger.warning("[summary_controller_safe_set_patch] installed V2")
    return True


try:
    install_controller_cache_safe_set_patch()
except Exception:
    logger.exception("[summary_controller_safe_set_patch] auto install failed")


__all__ = [
    "install_controller_cache_safe_set_patch",
    "enrich_candidate_with_existing_technicals",
]
