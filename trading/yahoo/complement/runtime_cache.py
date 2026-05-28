# ============================================================
# File   : trading/yahoo/complement/runtime_cache.py
# Version: YAHOO-RUNTIME-DF-CACHE-REV2-FULL-TECHNICAL-MERGE
# ------------------------------------------------------------
# 【概要】
#   Yahoo補完で計算した 1m/3m/5m のテクニカル付きsummaryを
#   runtime DF cache / global_data に反映する。
#
# 【重要】
#   旧版は「銘柄ごとの最新1行」だけを cache に渡していた。
#   それだけでは ma5/ma25/ma75/rsi/macd/slope などの再計算・補助計算に
#   必要な履歴DFとして使いにくい。
#
#   REV2では以下を追加する。
#     1. Yahoo計算済みDF全体を global_data.summary_1m_df/3m_df/5m_df へマージ
#     2. Yahoo専用DFとして global_data.yahoo_summary_1m_df/3m_df/5m_df も保持
#     3. global_data.yahoo_technical_summary_df_map[interval] にも保持
#     4. 既存PUSH行と同じ symbol+datetime は Yahoo行で上書き
#     5. 最新1行cache更新も従来通り実行
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any

import pandas as pd

from .logging_utils import log_step

logger = logging.getLogger(__name__)

try:
    from core.global_context.context import global_data
except Exception:
    try:
        from global_state import global_data  # type: ignore
    except Exception:
        global_data = None

try:
    from trading.ranking.runtime_symbols import ensure_ranking_symbol_cache, clear_intraday_cache
    _HAS_RANKING_CACHE = True
except Exception:
    _HAS_RANKING_CACHE = False

    def ensure_ranking_symbol_cache(*args, **kwargs):
        return None

    def clear_intraday_cache(*args, **kwargs):
        return None


SUMMARY_DF_ATTR_BY_INTERVAL = {
    1: "summary_1m_df",
    3: "summary_3m_df",
    5: "summary_5m_df",
}

YAHOO_SUMMARY_DF_ATTR_BY_INTERVAL = {
    1: "yahoo_summary_1m_df",
    3: "yahoo_summary_3m_df",
    5: "yahoo_summary_5m_df",
}


def _env_int(name: str, default: int, *, min_value: int | None = None) -> int:
    try:
        v = int(str(os.environ.get(name, str(default))).strip())
        if min_value is not None:
            v = max(v, min_value)
        return v
    except Exception:
        return default


# runtime上のDFが無制限に肥大化しないようにする。
# テクニカル再計算の補助なら銘柄ごと直近数百本あれば十分なため、既定は全体30万行。
YAHOO_RUNTIME_DF_CACHE_MAX_ROWS = _env_int("YAHOO_RUNTIME_DF_CACHE_MAX_ROWS", 300000, min_value=10000)


def safe_set_global_attr(name: str, value: Any) -> None:
    try:
        if global_data is not None:
            setattr(global_data, name, value)
    except Exception:
        logger.debug("[YAHOO COMPLEMENT] setattr failed name=%s", name, exc_info=True)


def safe_get_global_attr(name: str, default=None):
    try:
        return getattr(global_data, name, default) if global_data is not None else default
    except Exception:
        return default


def ensure_daily_cache_state(target_date: dt.date) -> None:
    ts = time.time()
    try:
        if not _HAS_RANKING_CACHE:
            logger.info("[YAHOO COMPLEMENT] runtime cache unavailable -> skip daily cache state")
            return

        ensure_ranking_symbol_cache(target_date=target_date)
        current = target_date.strftime("%Y%m%d")
        last = safe_get_global_attr("yahoo_cache_trade_date", None)

        if last != current:
            try:
                clear_intraday_cache(target_date=current)
            except Exception:
                logger.exception("[YAHOO COMPLEMENT] clear_intraday_cache failed")

            safe_set_global_attr("yahoo_cache_trade_date", current)
            logger.info("[YAHOO COMPLEMENT] intraday cache reset trade_date=%s prev=%s", current, last)
        else:
            logger.info("[YAHOO COMPLEMENT] intraday cache keep trade_date=%s", current)

        log_step("daily_cache_state_done", ts, target_date=target_date)

    except Exception:
        logger.exception("[YAHOO COMPLEMENT] ensure_daily_cache_state failed")


def _safe_df(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()
    return df.copy()


def _normalize_symbol_datetime(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty:
        return out

    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str).str.replace(".0", "", regex=False).str.strip()

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    elif "date" in out.columns and "time" in out.columns:
        out["datetime"] = pd.to_datetime(out["date"].astype(str) + " " + out["time"].astype(str), errors="coerce")

    if "last_update" not in out.columns:
        out["last_update"] = pd.Timestamp.now()

    return out


def _dedup_key_for_interval(df: pd.DataFrame, *, interval: int) -> list[str]:
    cols = set(df.columns)
    if {"symbol", "datetime"}.issubset(cols):
        return ["symbol", "datetime"]
    if int(interval) in (3, 5) and {"symbol", "date", "time_range"}.issubset(cols):
        return ["symbol", "date", "time_range"]
    if {"symbol", "date", "time"}.issubset(cols):
        return ["symbol", "date", "time"]
    if "symbol" in cols:
        return ["symbol"]
    return []


def _trim_cache_rows(df: pd.DataFrame, *, max_rows: int) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty or max_rows <= 0 or len(out) <= max_rows:
        return out

    try:
        if "datetime" in out.columns:
            out = out.sort_values("datetime", kind="stable").tail(max_rows)
        else:
            out = out.tail(max_rows)
        return out.reset_index(drop=True)
    except Exception:
        return out.tail(max_rows).reset_index(drop=True)


def _merge_runtime_df(existing: pd.DataFrame | None, incoming: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    inc = _normalize_symbol_datetime(incoming)
    if inc.empty:
        return _safe_df(existing)

    old = _normalize_symbol_datetime(existing)

    if old.empty:
        merged = inc.copy()
    else:
        # 列差分があっても壊れないよう union columns でconcatする。
        merged = pd.concat([old, inc], ignore_index=True, sort=False)

    key_cols = _dedup_key_for_interval(merged, interval=interval)
    if key_cols:
        try:
            merged = merged.dropna(subset=key_cols)
        except Exception:
            pass
        try:
            merged = merged.drop_duplicates(subset=key_cols, keep="last")
        except Exception:
            logger.debug("[YAHOO CACHE] drop_duplicates failed interval=%s key=%s", interval, key_cols, exc_info=True)

    try:
        sort_cols = [c for c in ["symbol", "datetime", "date", "time_range", "time"] if c in merged.columns]
        if sort_cols:
            merged = merged.sort_values(sort_cols, kind="stable")
    except Exception:
        pass

    merged = _trim_cache_rows(merged, max_rows=YAHOO_RUNTIME_DF_CACHE_MAX_ROWS)
    return merged.reset_index(drop=True)


def latest_per_symbol_for_cache(df: pd.DataFrame, *, interval: int, label: str) -> pd.DataFrame:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    out = df.copy()

    if "symbol" not in out.columns:
        logger.warning("[YAHOO CACHE] skip no symbol column interval=%s label=%s", interval, label)
        return pd.DataFrame()

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out.dropna(subset=["datetime"])
        if not out.empty:
            out = out.sort_values(["symbol", "datetime"]).groupby("symbol", as_index=False).tail(1)
    elif "date" in out.columns and "time" in out.columns:
        out["datetime"] = pd.to_datetime(out["date"].astype(str) + " " + out["time"].astype(str), errors="coerce")
        out = out.dropna(subset=["datetime"])
        if not out.empty:
            out = out.sort_values(["symbol", "datetime"]).groupby("symbol", as_index=False).tail(1)
    else:
        out = out.drop_duplicates(subset=["symbol"], keep="last")

    if out.empty:
        return pd.DataFrame()

    out["interval"] = int(interval)
    if "source" not in out.columns:
        out["source"] = "yahoo"
    if "last_update" not in out.columns:
        out["last_update"] = pd.Timestamp.now()

    return out.reset_index(drop=True)


def merge_calculated_summary_df_cache_from_result_map(result_map: dict, *, label: str) -> None:
    """
    Yahoo補完でテクニカル計算済みのDF全体を runtime DF へマージする。

    主な格納先:
      - global_data.summary_1m_df / summary_3m_df / summary_5m_df
      - global_data.yahoo_summary_1m_df / yahoo_summary_3m_df / yahoo_summary_5m_df
      - global_data.yahoo_technical_summary_df_map[interval]

    main_database.py と main.py は別プロセスなので、これは同一プロセス内のDF cacheである。
    main.py 側は既存のDBロード/summary cache refresh 経路で summary DB から取り込む。
    """
    if not isinstance(result_map, dict) or not result_map:
        logger.info("[YAHOO CACHE] skip full df merge empty result_map label=%s", label)
        return

    ts = time.time()
    merged_count = 0

    try:
        df_map = safe_get_global_attr("yahoo_technical_summary_df_map", None)
        if not isinstance(df_map, dict):
            df_map = {}

        for interval, df in sorted(result_map.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 999):
            try:
                interval_i = int(interval)
            except Exception:
                continue

            incoming = _normalize_symbol_datetime(df if isinstance(df, pd.DataFrame) else None)
            if incoming.empty:
                continue

            # Yahoo専用DF: Yahooで計算済みの履歴を保持
            yahoo_attr = YAHOO_SUMMARY_DF_ATTR_BY_INTERVAL.get(interval_i, f"yahoo_summary_{interval_i}m_df")
            yahoo_existing = safe_get_global_attr(yahoo_attr, None)
            yahoo_merged = _merge_runtime_df(yahoo_existing, incoming, interval=interval_i)
            safe_set_global_attr(yahoo_attr, yahoo_merged)
            df_map[interval_i] = yahoo_merged

            # 汎用summary DF: PUSH由来DFがあれば同一keyをYahooで補完/上書きしつつマージ
            summary_attr = SUMMARY_DF_ATTR_BY_INTERVAL.get(interval_i, f"summary_{interval_i}m_df")
            summary_existing = safe_get_global_attr(summary_attr, None)
            summary_merged = _merge_runtime_df(summary_existing, incoming, interval=interval_i)
            safe_set_global_attr(summary_attr, summary_merged)

            merged_count += 1

            logger.info(
                "[YAHOO CACHE] full technical DF merged interval=%s label=%s incoming_rows=%s yahoo_rows=%s summary_rows=%s yahoo_attr=%s summary_attr=%s latest=%s",
                interval_i,
                label,
                len(incoming),
                len(yahoo_merged),
                len(summary_merged),
                yahoo_attr,
                summary_attr,
                incoming["datetime"].max() if "datetime" in incoming.columns and not incoming.empty else None,
            )

        safe_set_global_attr("yahoo_technical_summary_df_map", df_map)
        safe_set_global_attr("yahoo_technical_summary_df_cache_updated_at", pd.Timestamp.now())

        logger.info(
            "[YAHOO CACHE] full technical DF merge done label=%s merged_intervals=%s max_rows=%s",
            label,
            merged_count,
            YAHOO_RUNTIME_DF_CACHE_MAX_ROWS,
        )
        log_step("yahoo_full_technical_df_cache_merge_done", ts, merged_intervals=merged_count)

    except Exception:
        logger.exception("[YAHOO CACHE] full technical DF merge fatal label=%s", label)


def update_runtime_df_cache_from_result_map(result_map: dict, *, label: str) -> None:
    """
    1. 計算済みDF全体を global_data.summary_*_df へマージする。
    2. 従来互換として各銘柄の最新1行も既存cache backendへ渡す。
    """
    if not isinstance(result_map, dict) or not result_map:
        logger.info("[YAHOO CACHE] skip empty result_map label=%s", label)
        return

    ts = time.time()

    # まず履歴DFとして使える形で反映する。
    merge_calculated_summary_df_cache_from_result_map(result_map, label=label)

    attempted = 0
    updated = 0

    try:
        try:
            from trading.yahoo.pipeline.complement.save import update_global_cache_if_possible, finalize_for_upsert_if_possible  # type: ignore
        except Exception:
            update_global_cache_if_possible = None
            finalize_for_upsert_if_possible = None

        for interval, df in sorted(result_map.items(), key=lambda kv: int(kv[0]) if str(kv[0]).isdigit() else 999):
            if not isinstance(df, pd.DataFrame) or df.empty:
                continue

            try:
                interval_i = int(interval)
            except Exception:
                continue

            cache_df = latest_per_symbol_for_cache(df, interval=interval_i, label=label)
            if cache_df.empty:
                continue

            attempted += 1

            try:
                if finalize_for_upsert_if_possible is not None:
                    cache_df = finalize_for_upsert_if_possible(cache_df, interval=interval_i)  # type: ignore[misc]

                if update_global_cache_if_possible is not None:
                    update_global_cache_if_possible(cache_df, interval=interval_i)  # type: ignore[misc]
                    updated += 1
                    continue

                try:
                    from core.global_context import global_data as gd  # type: ignore
                except Exception:
                    gd = None

                if gd is not None and hasattr(gd, "set_merged_summary"):
                    gd.set_merged_summary(interval_i, cache_df, source="yahoo")
                    updated += 1
                elif gd is not None and hasattr(gd, "set_push_merged_summary"):
                    gd.set_push_merged_summary(interval_i, cache_df)
                    updated += 1
                else:
                    logger.warning(
                        "[YAHOO CACHE] no runtime cache backend interval=%s rows=%s label=%s",
                        interval_i,
                        len(cache_df),
                        label,
                    )

            except Exception:
                logger.exception("[YAHOO CACHE] runtime latest-row cache update failed interval=%s label=%s", interval_i, label)

        logger.info(
            "[YAHOO CACHE] runtime df/cache update done label=%s attempted=%s updated=%s",
            label,
            attempted,
            updated,
        )
        log_step("yahoo_runtime_df_cache_update_done", ts, attempted=attempted, updated=updated)

    except Exception:
        logger.exception("[YAHOO CACHE] runtime df/cache update fatal label=%s", label)


__all__ = [
    "safe_set_global_attr",
    "safe_get_global_attr",
    "ensure_daily_cache_state",
    "latest_per_symbol_for_cache",
    "merge_calculated_summary_df_cache_from_result_map",
    "update_runtime_df_cache_from_result_map",
]
