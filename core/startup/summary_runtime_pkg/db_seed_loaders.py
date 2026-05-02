# ============================================================
# File   : core/startup/summary_runtime_pkg/db_seed_loaders.py
# Version: REV1.1-SUMMARY-RUNTIME-DB-SEED-LOADERS
#          -MULTIDAY-DIRECT-PREFERRED
#          -EXPANDED-DATA-SELECTION
#          -HISTORY-QUALITY-AWARE
# ------------------------------------------------------------
# 【概要】
#   起動時 summary DB seed 用 loader resolver
#
# 【主な機能】
#   ✔ loaders_summary recent tail
#   ✔ multi-day SQLite direct
#   ✔ latest snapshot fallback
#   ✔ sqlite direct fallback
#   ✔ seed DF 選択 logic
#
# 【REV1.1 修正】
#   ✔ 日別 summary DB 構成では loader_df は現在の summary_engine のDB中心になる
#   ✔ direct_df は当日DB + 前営業日DB を読める
#   ✔ direct_df が rows / symbols を増やしている場合は direct を優先
#   ✔ median が少し低いだけで direct を捨てない
#   ✔ loader_median=31 / direct_median=30 のようなケースでは direct を採用
#   ✔ 選択理由を詳細ログに出す
#
# 【重要】
#   - 前営業日DBを読む目的は「履歴不足の補完」
#   - そのため、median だけでなく rows / symbols / max / coverage を見る
#   - direct が明らかに悪い場合だけ loader を選ぶ
# ============================================================

from __future__ import annotations

import importlib
import inspect
import logging
from typing import Any, Iterable, Optional

import pandas as pd

from .dataframe_utils import (
    normalize_datetime_for_tf,
    dedupe_symbol_datetime,
    tail_per_symbol,
)
from .sqlite_seed import load_summary_seed_by_sqlite_direct
from .db_seed_anchor import resolve_anchor_for_seed
from .db_seed_diagnostics import log_history_quality, safe_symbols_count
from .db_seed_multiday_sqlite import load_summary_seed_by_multiday_sqlite_direct
from .db_seed_policy import get_seed_bars, latest_dt

logger = logging.getLogger(__name__)


# ============================================================
# normalize
# ============================================================

def normalize_seed_df(
    df: pd.DataFrame,
    tf: int,
    *,
    bars: Optional[int] = None,
) -> pd.DataFrame:
    """
    seed DF 共通正規化。

    - datetime 正規化
    - symbol 正規化
    - symbol/datetime 重複除去
    - symbol ごとの tail
    """
    df = df if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if df.empty:
        return pd.DataFrame()

    try:
        df = normalize_datetime_for_tf(df, tf)
    except Exception:
        logger.debug(
            "[summary_runtime] normalize_datetime_for_tf failed tf=%s",
            tf,
            exc_info=True,
        )

    if "symbol" in df.columns:
        try:
            df["symbol"] = df["symbol"].astype(str).str.strip()
            df = df[df["symbol"].ne("")].copy()
        except Exception:
            logger.debug(
                "[summary_runtime] symbol normalize failed tf=%s",
                tf,
                exc_info=True,
            )

    if "datetime" in df.columns:
        try:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
            try:
                if getattr(df["datetime"].dt, "tz", None) is not None:
                    df["datetime"] = df["datetime"].dt.tz_localize(None)
            except Exception:
                pass
            df = df.dropna(subset=["datetime"]).copy()
        except Exception:
            logger.debug(
                "[summary_runtime] datetime normalize/drop failed tf=%s",
                tf,
                exc_info=True,
            )

    try:
        df = dedupe_symbol_datetime(df)
    except Exception:
        logger.debug(
            "[summary_runtime] dedupe_symbol_datetime failed tf=%s",
            tf,
            exc_info=True,
        )

    if bars is not None:
        try:
            df = tail_per_symbol(df, int(bars))
        except Exception:
            logger.debug(
                "[summary_runtime] tail_per_symbol failed tf=%s bars=%s",
                tf,
                bars,
                exc_info=True,
            )

    return df


# ============================================================
# call helper
# ============================================================

def call_loader_with_supported_kwargs(fn, **kwargs):
    """
    loader のシグネチャ差異を吸収する。
    """
    try:
        sig = inspect.signature(fn)
        supported = {k: v for k, v in kwargs.items() if k in sig.parameters}
        return fn(**supported)
    except TypeError:
        raise
    except Exception:
        raise


# ============================================================
# latest snapshot fallback
# ============================================================

def load_summary_seed_by_latest_snapshot(tf: int) -> pd.DataFrame:
    try:
        mod = importlib.import_module("trading.summary.recovery.loaders_summary")
        fn = getattr(mod, "load_latest_summary_snapshot", None)
        if not callable(fn):
            return pd.DataFrame()

        df = fn(int(tf))
        df = normalize_seed_df(df, tf, bars=None)

        if isinstance(df, pd.DataFrame) and not df.empty:
            logger.info(
                "[summary_runtime] DB seed latest snapshot loaded tf=%s rows=%d symbols=%d",
                tf,
                len(df),
                safe_symbols_count(df),
            )
            return df

    except Exception:
        logger.debug(
            "[summary_runtime] DB seed latest snapshot failed tf=%s",
            tf,
            exc_info=True,
        )

    return pd.DataFrame()


# ============================================================
# loaders_summary path
# ============================================================

def load_summary_seed_by_recent_tail_loader(
    tf: int,
    *,
    bars_per_symbol: int,
    dates: Optional[Iterable[Any]] = None,
    anchor_day=None,
    max_allowed_dt: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """
    loaders_summary から recent tail を読む。

    注意:
      loaders_summary は現在 rebind 済み summary_engine を読む。
      summary DB が日別ファイルの場合、target_dates に前営業日を渡しても
      現在DBに前営業日の行が無ければ読めない。
      その不足分は multi-day SQLite direct 側で補う。
    """
    try:
        mod = importlib.import_module("trading.summary.recovery.loaders_summary")

        fn = getattr(mod, "load_recent_summary_tail_per_symbol", None)
        if callable(fn):
            df = call_loader_with_supported_kwargs(
                fn,
                interval=int(tf),
                bars_per_symbol=int(bars_per_symbol),
                end_dt=max_allowed_dt,
                target_dates=dates,
                anchor_day=anchor_day,
                max_allowed_dt=max_allowed_dt,
                symbols=None,
                symbol_chunk_size=300,
            )
            df = normalize_seed_df(df, tf, bars=bars_per_symbol)
            if isinstance(df, pd.DataFrame) and not df.empty:
                logger.info(
                    "[summary_runtime] DB seed recent tail per-symbol loaded "
                    "tf=%s rows=%d symbols=%d bars=%d latest_dt=%s",
                    tf,
                    len(df),
                    safe_symbols_count(df),
                    bars_per_symbol,
                    latest_dt(df),
                )
                return df

        fn = getattr(mod, "load_recent_summary_tail_default", None)
        if callable(fn):
            df = call_loader_with_supported_kwargs(
                fn,
                interval=int(tf),
                bars_per_symbol=int(bars_per_symbol),
                end_dt=max_allowed_dt,
                target_dates=dates,
                anchor_day=anchor_day,
                max_allowed_dt=max_allowed_dt,
                symbols=None,
                symbol_chunk_size=300,
            )
            df = normalize_seed_df(df, tf, bars=bars_per_symbol)
            if isinstance(df, pd.DataFrame) and not df.empty:
                logger.info(
                    "[summary_runtime] DB seed recent tail default loaded "
                    "tf=%s rows=%d symbols=%d bars=%d latest_dt=%s",
                    tf,
                    len(df),
                    safe_symbols_count(df),
                    bars_per_symbol,
                    latest_dt(df),
                )
                return df

    except Exception:
        logger.debug(
            "[summary_runtime] DB seed recent tail loader failed tf=%s",
            tf,
            exc_info=True,
        )

    return pd.DataFrame()


# ============================================================
# selection logic
# ============================================================

def _seed_stats(df: pd.DataFrame) -> dict[str, float]:
    """
    seed DF の比較用統計。
    """
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return {
                "rows": 0.0,
                "symbols": 0.0,
                "min": 0.0,
                "median": 0.0,
                "mean": 0.0,
                "max": 0.0,
                "ge_20": 0.0,
                "ge_40": 0.0,
                "ge_75": 0.0,
            }

        counts = df.groupby("symbol").size()

        if counts.empty:
            return {
                "rows": float(len(df)),
                "symbols": 0.0,
                "min": 0.0,
                "median": 0.0,
                "mean": 0.0,
                "max": 0.0,
                "ge_20": 0.0,
                "ge_40": 0.0,
                "ge_75": 0.0,
            }

        return {
            "rows": float(len(df)),
            "symbols": float(df["symbol"].nunique()),
            "min": float(counts.min()),
            "median": float(counts.median()),
            "mean": float(counts.mean()),
            "max": float(counts.max()),
            "ge_20": float((counts >= 20).sum()),
            "ge_40": float((counts >= 40).sum()),
            "ge_75": float((counts >= 75).sum()),
        }

    except Exception:
        logger.debug("[summary_runtime] seed stats failed", exc_info=True)
        return {
            "rows": 0.0,
            "symbols": 0.0,
            "min": 0.0,
            "median": 0.0,
            "mean": 0.0,
            "max": 0.0,
            "ge_20": 0.0,
            "ge_40": 0.0,
            "ge_75": 0.0,
        }


def choose_better_seed_df(
    *,
    tf: int,
    bars: int,
    loader_df: pd.DataFrame,
    direct_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    loaders_summary 経由と multi-day direct のどちらを使うか判定する。

    REV1.1:
      - 日別DB構成では loader_df は現在の summary_engine のDBしか読めない
      - direct_df は当日DB + 前営業日DB を読める
      - したがって、direct_df が十分な行数・銘柄数を持つ場合は direct を優先する

    選択基準:
      1. direct が空なら loader
      2. loader が空なら direct
      3. direct が loader より rows または symbols で増えていれば direct 優先
      4. median が大幅に悪化していない限り direct 優先
      5. direct が明らかに悪い場合だけ loader
    """
    loader_df = loader_df if isinstance(loader_df, pd.DataFrame) else pd.DataFrame()
    direct_df = direct_df if isinstance(direct_df, pd.DataFrame) else pd.DataFrame()

    if direct_df.empty:
        logger.info(
            "[summary_runtime] choose loader seed tf=%s reason=direct_empty loader_rows=%d",
            tf,
            len(loader_df),
        )
        return loader_df

    if loader_df.empty:
        logger.info(
            "[summary_runtime] choose multi-day direct seed tf=%s reason=loader_empty direct_rows=%d",
            tf,
            len(direct_df),
        )
        return direct_df

    ls = _seed_stats(loader_df)
    ds = _seed_stats(direct_df)

    # direct がデータ量または銘柄数で増えているなら、基本 direct 優先。
    direct_expands_rows = ds["rows"] > ls["rows"]
    direct_expands_symbols = ds["symbols"] > ls["symbols"]

    # median が多少低いだけなら許容。
    # 例: loader_median=31, direct_median=30 は direct 採用。
    # bars=180 なら 5% = 9本、ただし 3〜10 本の範囲に制限。
    try:
        median_tolerance = max(3.0, min(10.0, float(bars) * 0.05))
    except Exception:
        median_tolerance = 5.0

    median_not_much_worse = ds["median"] >= (ls["median"] - median_tolerance)

    # coverage 改善を見る。
    # 特に 1分足は ge_75、3/5分足は ge_40 が改善していれば direct を優先したい。
    direct_improves_ge20 = ds["ge_20"] > ls["ge_20"]
    direct_improves_ge40 = ds["ge_40"] > ls["ge_40"]
    direct_improves_ge75 = ds["ge_75"] > ls["ge_75"]

    direct_expands_coverage = (
        direct_improves_ge20
        or direct_improves_ge40
        or direct_improves_ge75
    )

    if (direct_expands_rows or direct_expands_symbols or direct_expands_coverage) and median_not_much_worse:
        logger.info(
            "[summary_runtime] choose multi-day direct seed tf=%s reason=expanded_data "
            "loader(rows=%.0f symbols=%.0f median=%.1f mean=%.1f min=%.0f max=%.0f ge20=%.0f ge40=%.0f ge75=%.0f) "
            "direct(rows=%.0f symbols=%.0f median=%.1f mean=%.1f min=%.0f max=%.0f ge20=%.0f ge40=%.0f ge75=%.0f) "
            "median_tolerance=%.1f",
            tf,
            ls["rows"], ls["symbols"], ls["median"], ls["mean"], ls["min"], ls["max"],
            ls["ge_20"], ls["ge_40"], ls["ge_75"],
            ds["rows"], ds["symbols"], ds["median"], ds["mean"], ds["min"], ds["max"],
            ds["ge_20"], ds["ge_40"], ds["ge_75"],
            median_tolerance,
        )
        return direct_df

    if ds["median"] > ls["median"]:
        logger.info(
            "[summary_runtime] choose multi-day direct seed tf=%s reason=better_median "
            "loader_median=%.1f direct_median=%.1f loader_rows=%.0f direct_rows=%.0f",
            tf,
            ls["median"],
            ds["median"],
            ls["rows"],
            ds["rows"],
        )
        return direct_df

    if ds["max"] > ls["max"] and median_not_much_worse and (direct_expands_rows or direct_expands_symbols):
        logger.info(
            "[summary_runtime] choose multi-day direct seed tf=%s reason=better_max_with_expansion "
            "loader(max=%.0f median=%.1f rows=%.0f symbols=%.0f) "
            "direct(max=%.0f median=%.1f rows=%.0f symbols=%.0f)",
            tf,
            ls["max"], ls["median"], ls["rows"], ls["symbols"],
            ds["max"], ds["median"], ds["rows"], ds["symbols"],
        )
        return direct_df

    logger.info(
        "[summary_runtime] choose loader seed tf=%s reason=loader_better "
        "loader(rows=%.0f symbols=%.0f median=%.1f mean=%.1f min=%.0f max=%.0f ge20=%.0f ge40=%.0f ge75=%.0f) "
        "direct(rows=%.0f symbols=%.0f median=%.1f mean=%.1f min=%.0f max=%.0f ge20=%.0f ge40=%.0f ge75=%.0f) "
        "median_tolerance=%.1f",
        tf,
        ls["rows"], ls["symbols"], ls["median"], ls["mean"], ls["min"], ls["max"],
        ls["ge_20"], ls["ge_40"], ls["ge_75"],
        ds["rows"], ds["symbols"], ds["median"], ds["mean"], ds["min"], ds["max"],
        ds["ge_20"], ds["ge_40"], ds["ge_75"],
        median_tolerance,
    )
    return loader_df


# ============================================================
# main load
# ============================================================

def load_summary_seed_from_db(tf: int) -> pd.DataFrame:
    """
    summary DB から runtime cache 用の履歴を読む。

    優先順位:
      1. loaders_summary recent tail per-symbol
      2. multi-day SQLite direct
      3. latest snapshot
      4. SQLite direct fallback

    実際の選択:
      loader_df と direct_df を両方作り、choose_better_seed_df() で選ぶ。
      REV1.1 では、前営業日DBも含む direct_df をより採用しやすくしている。
    """
    bars = get_seed_bars(int(tf))
    dates, anchor_day, max_allowed_dt = resolve_anchor_for_seed()

    loader_df = load_summary_seed_by_recent_tail_loader(
        tf,
        bars_per_symbol=bars,
        dates=dates,
        anchor_day=anchor_day,
        max_allowed_dt=max_allowed_dt,
    )

    direct_df = load_summary_seed_by_multiday_sqlite_direct(
        int(tf),
        bars_per_symbol=bars,
        dates=dates,
        max_allowed_dt=max_allowed_dt,
    )

    df = choose_better_seed_df(
        tf=int(tf),
        bars=bars,
        loader_df=loader_df,
        direct_df=direct_df,
    )

    if isinstance(df, pd.DataFrame) and not df.empty:
        log_history_quality(df, tf=int(tf), bars=bars, label="DB seed selected")
        logger.info(
            "[summary_runtime] DB seed selected final tf=%s rows=%d symbols=%d latest_dt=%s",
            tf,
            len(df),
            safe_symbols_count(df),
            latest_dt(df),
        )
        return df

    df = load_summary_seed_by_latest_snapshot(tf)
    if isinstance(df, pd.DataFrame) and not df.empty:
        logger.warning(
            "[summary_runtime] DB seed using latest snapshot fallback tf=%s rows=%d symbols=%d "
            "=> history bars are not enough for indicators",
            tf,
            len(df),
            safe_symbols_count(df),
        )
        return df

    df = load_summary_seed_by_sqlite_direct(tf, bars_per_symbol=bars)
    df = normalize_seed_df(df, tf, bars=bars)
    if isinstance(df, pd.DataFrame) and not df.empty:
        logger.info(
            "[summary_runtime] DB seed sqlite direct loaded tf=%s rows=%d symbols=%d bars=%d latest_dt=%s",
            tf,
            len(df),
            safe_symbols_count(df),
            bars,
            latest_dt(df),
        )
        log_history_quality(df, tf=int(tf), bars=bars, label="DB seed sqlite-direct")
        return df

    logger.warning("[summary_runtime] DB seed no summary loaded tf=%s", tf)
    return pd.DataFrame()


__all__ = [
    "normalize_seed_df",
    "call_loader_with_supported_kwargs",
    "load_summary_seed_by_latest_snapshot",
    "load_summary_seed_by_recent_tail_loader",
    "choose_better_seed_df",
    "load_summary_seed_from_db",
]