# ==========================================================
# File   : trading/summary/controller_cache.py
# Version: Ver1.5-PRODUCTION-HARDENED-CONTROLLER-CACHE-DISPLAY-READY
#          -TECHNICAL-COLUMNS-AWARE
#          -MERGED-RICHNESS-ENHANCED
#          -DISPLAY-READY-STABLE
# ----------------------------------------------------------
# 【概要】
#   summary_controller 用 cache / history merge / overwrite 判定
#
# 【主な機能】
#   - history/latest cache read/write
#   - merged_summary read/write
#   - latest-only pollution 判定
#   - history source merge
#   - merged cache overwrite 判定
#   - short-history display_ready 判定
#
# 【今回の修正】
#   - Ver1.4 の display_ready 方針は維持
#   - latest-only merged summary を history source から除外する仕様も維持
#   - ただし merged の豊かさ判定に ma/atr/hist/breakdown 系も加味
#   - score/close がある短履歴候補を immature として過剰拒否しない
#   - attach_display_ready は列を落とさず display_ready だけ安定付与
#   - merged overwrite 判定のログを強化
#
# 【重要】
#   technical_ready:
#       テクニカル指標が十分揃った行だけ True
#
#   display_ready:
#       短履歴でも score / score_buy / score_sell / close があるなら True
#       TOP10表示用の緩い readiness
# ==========================================================

from __future__ import annotations

import logging
import warnings
from typing import Callable

import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)

SUMMARY_HISTORY_CACHE_KEY_TMPL = "summary_history_{tf}min"
SUMMARY_LATEST_CACHE_KEY_TMPL = "summary_latest_{tf}min"

DEFAULT_HISTORY_KEEP_ROWS = {
    1: 120,
    3: 120,
    5: 120,
    10: 120,
    15: 120,
    30: 120,
    60: 120,
}

MIN_HISTORY_ROWS_STRONG = 80


# ==========================================================
# datetime helpers
# ==========================================================

def _safe_to_datetime(s) -> pd.Series:
    """
    warning を抑えて datetime64[ns] へ変換する軽量 helper。
    """
    try:
        if isinstance(s, pd.Series):
            if pd.api.types.is_datetime64_any_dtype(s):
                out = pd.to_datetime(s, errors="coerce")
            else:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", UserWarning)
                    out = pd.to_datetime(s, errors="coerce")
        else:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                out = pd.to_datetime(pd.Series(s), errors="coerce")

        try:
            out = out.dt.tz_localize(None)
        except Exception:
            pass

        return out
    except Exception:
        try:
            return pd.Series(pd.NaT, index=getattr(s, "index", None), dtype="datetime64[ns]")
        except Exception:
            return pd.Series(dtype="datetime64[ns]")


# ==========================================================
# cache key helpers
# ==========================================================

def history_cache_key(interval: int) -> str:
    return SUMMARY_HISTORY_CACHE_KEY_TMPL.format(tf=int(interval))


def latest_cache_key(interval: int) -> str:
    return SUMMARY_LATEST_CACHE_KEY_TMPL.format(tf=int(interval))


# ==========================================================
# small scoring helpers
# ==========================================================

def _safe_symbol_count(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return 0
        s = df["symbol"].fillna("").astype(str).str.strip()
        s = s.replace({"": pd.NA, "nan": pd.NA, "None": pd.NA, "<NA>": pd.NA})
        return int(s.dropna().nunique())
    except Exception:
        return 0


def _safe_latest_dt(df: pd.DataFrame):
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        for c in ("datetime", "end_time", "snapshot_time", "tick_time"):
            if c in df.columns:
                s = _safe_to_datetime(df[c]).dropna()
                if not s.empty:
                    return s.max()
        return None
    except Exception:
        return None


def _safe_datetime_nonnull(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return 0
        for c in ("datetime", "end_time", "snapshot_time", "tick_time"):
            if c in df.columns:
                s = _safe_to_datetime(df[c])
                return int(s.notna().sum())
        return 0
    except Exception:
        return 0


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


def _safe_ready_count(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "technical_ready" not in df.columns:
            return 0
        s = pd.Series(df["technical_ready"]).fillna(False).astype(bool)
        return int(s.sum())
    except Exception:
        return 0


def _safe_ready_symbol_count(df: pd.DataFrame) -> int:
    try:
        if (
            not isinstance(df, pd.DataFrame)
            or df.empty
            or "symbol" not in df.columns
            or "technical_ready" not in df.columns
        ):
            return 0
        ready = pd.Series(df["technical_ready"]).fillna(False).astype(bool)
        return int(df.loc[ready, "symbol"].astype(str).nunique())
    except Exception:
        return 0


def _safe_display_ready_count(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return 0
        if "display_ready" in df.columns:
            return int(pd.Series(df["display_ready"]).fillna(False).astype(bool).sum())
        return int(_build_display_ready_mask(df).sum())
    except Exception:
        return 0


def _safe_display_ready_symbol_count(df: pd.DataFrame) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return 0
        if "display_ready" in df.columns:
            ready = pd.Series(df["display_ready"]).fillna(False).astype(bool)
        else:
            ready = _build_display_ready_mask(df)
        return int(df.loc[ready, "symbol"].astype(str).nunique())
    except Exception:
        return 0


def _safe_hist_profile(df: pd.DataFrame) -> tuple[int, int, int, float]:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol_hist_len" not in df.columns:
            return 0, 0, 0, 0.0
        s = pd.to_numeric(df["symbol_hist_len"], errors="coerce").fillna(0)
        ge3 = int((s >= 3).sum())
        ge5 = int((s >= 5).sum())
        ge26 = int((s >= 26).sum())
        mx = float(s.max()) if not s.empty else 0.0
        return ge3, ge5, ge26, mx
    except Exception:
        return 0, 0, 0, 0.0


def _safe_symbol_datetime_density(df: pd.DataFrame) -> tuple[int, int, float]:
    """
    returns:
        one_bar_symbols, total_symbols, one_bar_ratio
    """
    try:
        if (
            not isinstance(df, pd.DataFrame)
            or df.empty
            or "symbol" not in df.columns
            or "datetime" not in df.columns
        ):
            return 0, 0, 0.0

        x = df.copy()
        x["datetime"] = _safe_to_datetime(x["datetime"])
        x["symbol"] = x["symbol"].astype(str).str.strip()
        x = x.dropna(subset=["datetime"])
        x = x[x["symbol"] != ""]

        vc = x.groupby("symbol")["datetime"].nunique()

        if vc.empty:
            return 0, 0, 0.0

        one_bar = int((vc <= 1).sum())
        total = int(vc.size)
        ratio = (one_bar / total) if total > 0 else 0.0
        return one_bar, total, ratio
    except Exception:
        return 0, 0, 0.0


# ==========================================================
# display readiness
# ==========================================================

def _build_display_ready_mask(df: pd.DataFrame) -> pd.Series:
    """
    TOP10表示用の緩い readiness。

    条件:
      - symbol がある
      - close/price 系がある
      - score / score_buy / score_sell / final_score / display_score のいずれかが非ゼロ
        または score 系が non-null
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.Series(False, index=getattr(df, "index", None))

    idx = df.index

    try:
        if "symbol" in df.columns:
            symbol_ok = df["symbol"].fillna("").astype(str).str.strip().ne("")
        else:
            symbol_ok = pd.Series(False, index=idx)

        close_ok = pd.Series(False, index=idx)
        for c in ("close", "close_price", "price", "current_price", "last_price"):
            if c in df.columns:
                s = pd.to_numeric(df[c], errors="coerce")
                close_ok = close_ok | (s.notna() & s.fillna(0).ne(0))

        score_nonzero = pd.Series(False, index=idx)
        score_nonnull = pd.Series(False, index=idx)
        for c in (
            "score",
            "score_total",
            "final_score",
            "display_score",
            "score_buy",
            "score_sell",
            "buy_score",
            "sell_score",
            "combined_score",
            "absolute_score",
        ):
            if c in df.columns:
                s = pd.to_numeric(df[c], errors="coerce")
                score_nonzero = score_nonzero | s.fillna(0).ne(0)
                score_nonnull = score_nonnull | s.notna()

        return (symbol_ok & close_ok & (score_nonzero | score_nonnull)).fillna(False)

    except Exception:
        logger.debug("[summary_controller] display ready mask failed", exc_info=True)
        return pd.Series(False, index=idx)


def attach_display_ready(df: pd.DataFrame) -> pd.DataFrame:
    """
    display_ready 列を付与する。
    technical_ready は変更しない。
    列は一切削らない。
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()

    try:
        display_ready = _build_display_ready_mask(out)

        if "technical_ready" in out.columns:
            try:
                tech_ready = pd.Series(out["technical_ready"]).fillna(False).astype(bool)
                display_ready = display_ready | tech_ready
            except Exception:
                pass

        out["display_ready"] = display_ready.fillna(False).astype(bool)

        logger.info(
            "[summary_controller] attach_display_ready rows=%s cols=%s display_rows=%s display_symbols=%s score_nonzero=%s close_nonnull=%s",
            len(out),
            len(out.columns),
            _safe_display_ready_count(out),
            _safe_display_ready_symbol_count(out),
            _safe_numeric_nonzero(out, ("score", "score_buy", "score_sell", "final_score", "display_score")),
            _safe_numeric_nonnull(out, ("close", "close_price", "price", "current_price")),
        )

    except Exception:
        logger.exception("[summary_controller] attach_display_ready failed")
        try:
            out["display_ready"] = False
        except Exception:
            pass

    return out


# ==========================================================
# basic dataframe merge helpers
# ==========================================================

def concat_frames(
    frames: list[pd.DataFrame],
    normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
) -> pd.DataFrame:
    xs = []
    for x in frames:
        df = normalize_fn(x)
        if isinstance(df, pd.DataFrame) and not df.empty:
            df = attach_display_ready(df)
            xs.append(df)

    if not xs:
        return pd.DataFrame()

    try:
        out = pd.concat(xs, axis=0, ignore_index=True, sort=False)
        out = normalize_fn(out)
        out = attach_display_ready(out)
        return out
    except Exception:
        logger.exception("[summary_controller] concat frames failed")
        return pd.DataFrame()


def dedupe_symbol_datetime(
    df: pd.DataFrame,
    normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
) -> pd.DataFrame:
    out = normalize_fn(df)
    if out.empty or "symbol" not in out.columns or "datetime" not in out.columns:
        return attach_display_ready(out)

    try:
        out = out.copy()
        out["datetime"] = _safe_to_datetime(out["datetime"])
        out = out.dropna(subset=["symbol", "datetime"])
        out["symbol"] = out["symbol"].astype(str).str.strip()
        out = out[out["symbol"] != ""]

        out = (
            out.sort_values(["symbol", "datetime"], kind="mergesort")
               .drop_duplicates(["symbol", "datetime"], keep="last")
               .reset_index(drop=True)
        )
        out = attach_display_ready(out)
        return out
    except Exception:
        logger.exception("[summary_controller] dedupe symbol-datetime failed")
        return attach_display_ready(out)


def history_keep_rows(interval: int) -> int:
    try:
        return max(int(DEFAULT_HISTORY_KEEP_ROWS.get(int(interval), 120)), MIN_HISTORY_ROWS_STRONG)
    except Exception:
        return 120


def limit_history_rows_per_symbol(
    df: pd.DataFrame,
    interval: int,
    normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
) -> pd.DataFrame:
    out = normalize_fn(df)
    if out.empty or "symbol" not in out.columns or "datetime" not in out.columns:
        return attach_display_ready(out)

    keep_rows = history_keep_rows(interval)

    try:
        out = out.copy()
        out["datetime"] = _safe_to_datetime(out["datetime"])
        out = out.dropna(subset=["symbol", "datetime"])
        out["symbol"] = out["symbol"].astype(str).str.strip()
        out = out[out["symbol"] != ""]

        out = (
            out.sort_values(["symbol", "datetime"], kind="mergesort")
               .groupby("symbol", group_keys=False)
               .tail(keep_rows)
               .reset_index(drop=True)
        )
        out = attach_display_ready(out)
        return out
    except Exception:
        logger.exception("[summary_controller] limit history rows failed interval=%s", interval)
        return attach_display_ready(out)


# ==========================================================
# history/latest cache
# ==========================================================

def safe_global_get_history(
    interval: int,
    normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
) -> pd.DataFrame:
    try:
        key = history_cache_key(interval)
        df = getattr(global_data, key, None)
        if isinstance(df, pd.DataFrame):
            return attach_display_ready(normalize_fn(df))
    except Exception:
        logger.debug("[summary_controller] get history cache failed interval=%s", interval, exc_info=True)

    try:
        if hasattr(global_data, "get"):
            key = history_cache_key(interval)
            df = global_data.get(key, None)
            if isinstance(df, pd.DataFrame):
                return attach_display_ready(normalize_fn(df))
    except Exception:
        logger.debug("[summary_controller] get history cache by dict-style failed interval=%s", interval, exc_info=True)

    return pd.DataFrame()


def safe_global_set_history(interval: int, df: pd.DataFrame) -> None:
    try:
        key = history_cache_key(interval)
        setattr(global_data, key, attach_display_ready(df).copy())
    except Exception:
        logger.debug("[summary_controller] set history cache failed interval=%s", interval, exc_info=True)


def safe_global_set_latest(interval: int, df: pd.DataFrame) -> None:
    try:
        key = latest_cache_key(interval)
        setattr(global_data, key, attach_display_ready(df).copy())
    except Exception:
        logger.debug("[summary_controller] set latest cache failed interval=%s", interval, exc_info=True)


# ==========================================================
# merged summary cache
# ==========================================================

def safe_global_get_merged_summary(
    interval: int,
    normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
) -> pd.DataFrame:
    """
    PUSH由来 summary_controller 用 merged cache 読み出し。
    GlobalContext が source 指定に対応している場合は source="push" を明示する。
    """
    try:
        if hasattr(global_data, "get_merged_summary"):
            try:
                df = global_data.get_merged_summary(interval, source="push")
            except TypeError:
                df = global_data.get_merged_summary(interval)

            if isinstance(df, pd.DataFrame):
                return attach_display_ready(normalize_fn(df))
    except Exception:
        logger.debug(
            "[summary_controller] global get_merged_summary(source=push) failed interval=%s",
            interval,
            exc_info=True,
        )

    try:
        if hasattr(global_data, "get_push_merged_summary"):
            df = global_data.get_push_merged_summary(interval)
            if isinstance(df, pd.DataFrame):
                return attach_display_ready(normalize_fn(df))
    except Exception:
        logger.debug(
            "[summary_controller] get_push_merged_summary fallback failed interval=%s",
            interval,
            exc_info=True,
        )

    try:
        df = getattr(global_data, f"merged_summary_{interval}", None)
        if isinstance(df, pd.DataFrame):
            logger.warning(
                "[summary_controller] merged_summary_%s legacy fallback used",
                interval,
            )
            return attach_display_ready(normalize_fn(df))
    except Exception:
        logger.debug("[summary_controller] merged_summary_%s fallback failed", interval, exc_info=True)

    try:
        df = getattr(global_data, "merged_summary", None)
        if isinstance(df, pd.DataFrame):
            logger.warning(
                "[summary_controller] merged_summary legacy fallback used interval=%s",
                interval,
            )
            return attach_display_ready(normalize_fn(df))
    except Exception:
        logger.debug("[summary_controller] merged_summary fallback failed interval=%s", interval, exc_info=True)

    return pd.DataFrame()


def safe_global_set_merged_summary(interval: int, df: pd.DataFrame) -> None:
    """
    PUSH由来 summary_controller 用 merged cache 保存。
    GlobalContext が source 指定に対応している場合は source="push" を明示する。
    """
    if not isinstance(df, pd.DataFrame):
        return

    payload = attach_display_ready(df)

    try:
        if hasattr(global_data, "set_merged_summary"):
            try:
                global_data.set_merged_summary(interval, payload.copy(), source="push")
            except TypeError:
                global_data.set_merged_summary(interval, payload.copy())
            return
    except Exception:
        logger.exception(
            "[summary_controller] set_merged_summary(source=push) failed interval=%s",
            interval,
        )

    try:
        if hasattr(global_data, "set_push_merged_summary"):
            global_data.set_push_merged_summary(interval, payload.copy())
            return
    except Exception:
        logger.exception(
            "[summary_controller] set_push_merged_summary fallback failed interval=%s",
            interval,
        )

    try:
        setattr(global_data, f"merged_summary_{interval}", payload.copy())
        logger.warning(
            "[summary_controller] merged_summary_%s legacy fallback set used",
            interval,
        )
    except Exception:
        logger.exception("[summary_controller] merged_summary_%s fallback set failed", interval)


# ==========================================================
# latest-only pollution probe
# ==========================================================

def looks_latest_only_like(
    df: pd.DataFrame,
    interval: int,
    normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
) -> bool:
    out = normalize_fn(df)
    if out.empty or "symbol" not in out.columns or "datetime" not in out.columns:
        return False

    try:
        out = out.copy()
        out["datetime"] = _safe_to_datetime(out["datetime"])
        out = out.dropna(subset=["symbol", "datetime"])

        one_bar, total, ratio = _safe_symbol_datetime_density(out)
        rows = len(out)
        latest_dt = _safe_latest_dt(out)
        dt_nonnull = _safe_datetime_nonnull(out)
        symbols = _safe_symbol_count(out)
        unique_dt = int(out["datetime"].nunique()) if "datetime" in out.columns else 0

        logger.info(
            "[summary_controller] latest-only probe interval=%s one_bar=%s total=%s ratio=%.4f rows=%s symbols=%s unique_dt=%s datetime_nonnull=%s latest_dt=%s display_ready=%s",
            interval,
            one_bar,
            total,
            ratio,
            rows,
            symbols,
            unique_dt,
            dt_nonnull,
            latest_dt,
            _safe_display_ready_count(out),
        )

        if total <= 0:
            return False

        if int(interval) == 1:
            if rows >= 40 and total >= 20:
                return ratio >= 0.95
            if rows >= 20 and total >= 10:
                return ratio >= 0.90
            return ratio >= 0.85

        return ratio >= 0.90

    except Exception:
        logger.debug("[summary_controller] latest-only probe failed", exc_info=True)
        return False


def sanitize_existing_merged_for_history(
    df: pd.DataFrame,
    interval: int,
    normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
) -> pd.DataFrame:
    """
    履歴マージ元として unsafe な existing merged summary を除外する。
    特に latest-only 化した merged summary を history source として再利用しない。
    """
    out = attach_display_ready(normalize_fn(df))
    if out.empty:
        return pd.DataFrame()

    try:
        out = dedupe_symbol_datetime(out, normalize_fn=normalize_fn)
    except Exception:
        logger.debug("[summary_controller] sanitize existing merged dedupe failed", exc_info=True)

    try:
        if looks_latest_only_like(out, interval, normalize_fn):
            logger.warning(
                "[summary_controller] existing merged summary rejected from history merge because it looks latest-only interval=%s rows=%s symbols=%s display_ready=%s",
                interval,
                len(out),
                _safe_symbol_count(out),
                _safe_display_ready_count(out),
            )
            return pd.DataFrame()

        one_bar, total, ratio = _safe_symbol_datetime_density(out)
        logger.info(
            "[summary_controller] existing merged summary accepted for history merge interval=%s one_bar=%s total=%s ratio=%.4f rows=%s latest_dt=%s display_ready=%s",
            interval,
            one_bar,
            total,
            ratio,
            len(out),
            _safe_latest_dt(out),
            _safe_display_ready_count(out),
        )
        return attach_display_ready(out)

    except Exception:
        logger.debug("[summary_controller] sanitize existing merged failed", exc_info=True)
        return pd.DataFrame()


# ==========================================================
# history merge
# ==========================================================

def merge_history_sources(
    interval: int,
    current_df: pd.DataFrame,
    fetched_df: pd.DataFrame,
    engine_df: pd.DataFrame,
    normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
) -> pd.DataFrame:
    history_cache_df = safe_global_get_history(interval, normalize_fn)
    existing_merged_df = safe_global_get_merged_summary(interval, normalize_fn)
    existing_merged_for_history = sanitize_existing_merged_for_history(
        existing_merged_df,
        interval=interval,
        normalize_fn=normalize_fn,
    )

    hist = concat_frames(
        [
            history_cache_df,
            existing_merged_for_history,
            engine_df,
            fetched_df,
            current_df,
        ],
        normalize_fn=normalize_fn,
    )
    hist = dedupe_symbol_datetime(hist, normalize_fn=normalize_fn)
    hist = limit_history_rows_per_symbol(hist, interval, normalize_fn=normalize_fn)
    hist = attach_display_ready(hist)

    try:
        if isinstance(hist, pd.DataFrame) and not hist.empty and "symbol" in hist.columns and "datetime" in hist.columns:
            vc = hist.groupby("symbol")["datetime"].nunique().sort_values(ascending=False)
            logger.info(
                "[summary_controller] merged-history interval=%s symbols=%s rows=%s hist_len[min=%s p25=%.2f med=%.2f p75=%.2f max=%s mean=%.2f] latest_dt=%s display_ready=%s display_symbols=%s",
                interval,
                int(vc.shape[0]),
                int(len(hist)),
                int(vc.min()),
                float(vc.quantile(0.25)),
                float(vc.quantile(0.50)),
                float(vc.quantile(0.75)),
                int(vc.max()),
                float(vc.mean()),
                _safe_latest_dt(hist),
                _safe_display_ready_count(hist),
                _safe_display_ready_symbol_count(hist),
            )

            if int(vc.max()) <= 1:
                logger.warning(
                    "[summary_controller] merged-history still short/latest-only after fallback interval=%s rows=%s symbols=%s display_ready=%s -> TOP10 can still use display_ready but technicals remain immature",
                    interval,
                    len(hist),
                    _safe_symbol_count(hist),
                    _safe_display_ready_count(hist),
                )
    except Exception:
        logger.exception("[summary_controller] merged-history density log failed interval=%s", interval)

    return hist


# ==========================================================
# merged overwrite decision
# ==========================================================

def score_richness(df: pd.DataFrame) -> tuple[int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int, int]:
    """
    merged overwrite 判定用の豊かさスコア。
    Ver1.5 では MA/ATR/HIST/breakdown 系も加味する。
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)

    try:
        rows = len(df)
        symbols = _safe_symbol_count(df)

        score_nonzero = _safe_numeric_nonzero(df, ("score", "score_buy", "score_sell", "final_score", "display_score"))
        slope_nonzero = _safe_numeric_nonzero(df, ("slope_atr_scaled", "slope", "ma75_slope"))
        close_nonnull = _safe_numeric_nonnull(df, ("close", "close_price", "last_price", "current_price", "price"))
        technical_ready_true = _safe_ready_count(df)
        ready_symbol_count = _safe_ready_symbol_count(df)
        rsi_nonnull = _safe_numeric_nonnull(df, ("rsi",))
        macd_nonzero = _safe_numeric_nonzero(df, ("macd",))
        mtf_nonzero = _safe_numeric_nonzero(df, ("mtf", "score_mtf", "mtf_score", "mtf_alignment"))
        display_ready_true = _safe_display_ready_count(df)
        display_symbol_count = _safe_display_ready_symbol_count(df)

        ma_nonnull = _safe_numeric_nonnull(df, ("ma5", "ma25", "ma75"))
        atr_nonnull = _safe_numeric_nonnull(df, ("atr",))
        hist_nonzero = _safe_numeric_nonzero(df, ("hist",))
        breakdown_nonnull = _safe_numeric_nonnull(
            df,
            (
                "score_base",
                "score_trend",
                "score_momentum",
                "score_velocity",
                "score_penalty",
                "breakdown_base",
                "breakdown_trend",
                "breakdown_mom",
                "breakdown_vel",
                "breakdown_pen",
            ),
        )
        ohlc_nonnull = _safe_numeric_nonnull(df, ("open", "high", "low", "close"))
        volume_nonnull = _safe_numeric_nonnull(df, ("volume", "trading_volume"))

        return (
            rows,
            symbols,
            score_nonzero,
            slope_nonzero,
            close_nonnull,
            technical_ready_true,
            ready_symbol_count,
            rsi_nonnull,
            macd_nonzero,
            mtf_nonzero,
            display_ready_true,
            display_symbol_count,
            ma_nonnull,
            atr_nonnull,
            hist_nonzero,
            breakdown_nonnull,
            max(ohlc_nonnull, volume_nonnull),
        )
    except Exception:
        logger.debug("[summary_controller] richness score failed", exc_info=True)
        return (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)


def candidate_is_immature(df: pd.DataFrame) -> bool:
    """
    merged overwrite 用の immature 判定。

    Ver1.5:
      - score + close + display_ready があれば短履歴でも拒否しない
      - ma/atr/breakdown が少しでもあれば immature 扱いを緩める
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return True

    try:
        df = attach_display_ready(df)

        ready_symbols = _safe_ready_symbol_count(df)
        ready_rows = _safe_ready_count(df)
        display_ready = _safe_display_ready_count(df)
        display_symbols = _safe_display_ready_symbol_count(df)

        slope_nonzero = _safe_numeric_nonzero(df, ("slope_atr_scaled", "slope", "ma75_slope"))
        rsi_nonnull = _safe_numeric_nonnull(df, ("rsi",))
        macd_nonzero = _safe_numeric_nonzero(df, ("macd",))
        mtf_nonzero = _safe_numeric_nonzero(df, ("mtf", "score_mtf", "mtf_score", "mtf_alignment"))
        score_nonzero = _safe_numeric_nonzero(df, ("score", "score_buy", "score_sell", "final_score", "display_score"))
        close_nonnull = _safe_numeric_nonnull(df, ("close", "close_price", "price", "current_price"))
        ma_nonnull = _safe_numeric_nonnull(df, ("ma5", "ma25", "ma75"))
        atr_nonnull = _safe_numeric_nonnull(df, ("atr",))
        breakdown_nonnull = _safe_numeric_nonnull(
            df,
            (
                "score_base",
                "score_trend",
                "score_momentum",
                "score_velocity",
                "score_penalty",
                "breakdown_base",
                "breakdown_trend",
                "breakdown_mom",
                "breakdown_vel",
                "breakdown_pen",
            ),
        )

        hist_ge3, hist_ge5, hist_ge26, hist_max = _safe_hist_profile(df)

        logger.info(
            "[summary_controller] candidate maturity ready_rows=%s ready_symbols=%s display_ready=%s display_symbols=%s slope_nonzero=%s rsi_nonnull=%s macd_nonzero=%s mtf_nonzero=%s score_nonzero=%s close_nonnull=%s ma_nonnull=%s atr_nonnull=%s breakdown_nonnull=%s hist_ge3=%s hist_ge5=%s hist_ge26=%s hist_max=%s",
            ready_rows,
            ready_symbols,
            display_ready,
            display_symbols,
            slope_nonzero,
            rsi_nonnull,
            macd_nonzero,
            mtf_nonzero,
            score_nonzero,
            close_nonnull,
            ma_nonnull,
            atr_nonnull,
            breakdown_nonnull,
            hist_ge3,
            hist_ge5,
            hist_ge26,
            hist_max,
        )

        if display_ready > 0 and display_symbols > 0 and score_nonzero > 0 and close_nonnull > 0:
            return False

        if ma_nonnull > 0 or atr_nonnull > 0 or breakdown_nonnull > 0:
            return False

        if (
            ready_symbols <= 0
            and slope_nonzero <= 0
            and macd_nonzero <= 0
            and mtf_nonzero <= 0
            and score_nonzero <= 0
        ):
            return True

        if (
            ready_rows <= 0
            and rsi_nonnull <= 0
            and hist_ge3 <= 0
            and close_nonnull <= 0
            and score_nonzero <= 0
        ):
            return True

        if close_nonnull > 0 and score_nonzero > 0:
            return False

        if hist_ge5 > 0 or hist_max >= 5:
            return False

        return False
    except Exception:
        logger.debug("[summary_controller] candidate maturity check failed", exc_info=True)
        return False


def choose_merged_cache_payload(
    interval: int,
    df_hist: pd.DataFrame,
    df_latest: pd.DataFrame,
    normalize_fn: Callable[[pd.DataFrame], pd.DataFrame],
) -> pd.DataFrame:
    try:
        if int(interval) == 1:
            payload = limit_history_rows_per_symbol(df_hist, interval, normalize_fn=normalize_fn)
            payload = dedupe_symbol_datetime(payload, normalize_fn=normalize_fn)
            payload = attach_display_ready(payload)
            logger.info(
                "[summary_controller] merged cache payload interval=%s -> history rows=%s cols=%s symbols=%s latest_dt=%s display_ready=%s display_symbols=%s",
                interval,
                len(payload) if isinstance(payload, pd.DataFrame) else 0,
                len(payload.columns) if isinstance(payload, pd.DataFrame) else 0,
                _safe_symbol_count(payload),
                _safe_latest_dt(payload),
                _safe_display_ready_count(payload),
                _safe_display_ready_symbol_count(payload),
            )
            return payload

        payload = normalize_fn(df_latest)
        payload = dedupe_symbol_datetime(payload, normalize_fn=normalize_fn)
        payload = attach_display_ready(payload)
        logger.info(
            "[summary_controller] merged cache payload interval=%s -> latest rows=%s cols=%s symbols=%s latest_dt=%s display_ready=%s display_symbols=%s",
            interval,
            len(payload) if isinstance(payload, pd.DataFrame) else 0,
            len(payload.columns) if isinstance(payload, pd.DataFrame) else 0,
            _safe_symbol_count(payload),
            _safe_latest_dt(payload),
            _safe_display_ready_count(payload),
            _safe_display_ready_symbol_count(payload),
        )
        return payload

    except Exception:
        logger.exception("[summary_controller] choose merged cache payload failed interval=%s", interval)
        return attach_display_ready(normalize_fn(df_latest))


def should_overwrite_merged_summary(existing_df: pd.DataFrame, candidate_df: pd.DataFrame) -> bool:
    existing_df = attach_display_ready(existing_df) if isinstance(existing_df, pd.DataFrame) else existing_df
    candidate_df = attach_display_ready(candidate_df) if isinstance(candidate_df, pd.DataFrame) else candidate_df

    existing_score = score_richness(existing_df)
    candidate_score = score_richness(candidate_df)

    logger.info(
        "[summary_controller] overwrite merged? existing_score=%s candidate_score=%s",
        existing_score,
        candidate_score,
    )

    if not isinstance(candidate_df, pd.DataFrame) or candidate_df.empty:
        return False

    if not isinstance(existing_df, pd.DataFrame) or existing_df.empty:
        return True

    immature = candidate_is_immature(candidate_df)
    if immature:
        cand_rows, cand_symbols, cand_score_nonzero, *_rest = candidate_score
        cand_display_ready = candidate_score[10]
        cand_display_symbols = candidate_score[11]

        ex_rows, ex_symbols, ex_score_nonzero, *_exrest = existing_score
        ex_display_ready = existing_score[10]
        ex_display_symbols = existing_score[11]

        if (
            cand_display_ready >= ex_display_ready
            and cand_display_symbols >= ex_display_symbols
            and cand_score_nonzero >= ex_score_nonzero
            and cand_rows >= ex_rows
        ):
            logger.warning(
                "[summary_controller] overwrite allowed despite immature by display_ready: cand_display=%s>=%s cand_symbols=%s>=%s cand_score=%s>=%s cand_rows=%s>=%s",
                cand_display_ready,
                ex_display_ready,
                cand_display_symbols,
                ex_display_symbols,
                cand_score_nonzero,
                ex_score_nonzero,
                cand_rows,
                ex_rows,
            )
            return True

        if cand_rows >= ex_rows and cand_symbols >= ex_symbols and cand_score_nonzero >= ex_score_nonzero:
            logger.warning(
                "[summary_controller] overwrite allowed despite immature: candidate richer rows=%s>=%s symbols=%s>=%s score_nonzero=%s>=%s",
                cand_rows,
                ex_rows,
                cand_symbols,
                ex_symbols,
                cand_score_nonzero,
                ex_score_nonzero,
            )
            return True

        logger.warning("[summary_controller] overwrite blocked: candidate looks immature")
        return False

    return candidate_score >= existing_score


__all__ = [
    "history_cache_key",
    "latest_cache_key",
    "attach_display_ready",
    "concat_frames",
    "dedupe_symbol_datetime",
    "history_keep_rows",
    "limit_history_rows_per_symbol",
    "safe_global_get_history",
    "safe_global_set_history",
    "safe_global_set_latest",
    "safe_global_get_merged_summary",
    "safe_global_set_merged_summary",
    "looks_latest_only_like",
    "sanitize_existing_merged_for_history",
    "merge_history_sources",
    "score_richness",
    "candidate_is_immature",
    "choose_merged_cache_payload",
    "should_overwrite_merged_summary",
]