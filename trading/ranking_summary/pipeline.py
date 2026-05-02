# ============================================================
# File   : trading/ranking_summary/pipeline.py
# Version: Ver31_L23-RANKING-SUMMARY-PIPELINE-HARDENED
# ------------------------------------------------------------
# 機能:
#   - ランキング由来サマリー計算の安全な入口
#   - 既存 ranking scheduler / engine / summary 関数候補を順次解決
#   - 戻り値ゆらぎ（DataFrame, list, dict, tuple）への耐性
#   - datetime列の正規化
#
# 目的:
#   - ranking由来サマリーを PUSH由来サマリーと完全分離した入口で扱う
#   - 実ファイル内の関数名差異があっても落ちにくくする
#
# 主な関数:
#   - run_ranking_summary_pipeline(...)
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# interval helpers
# ============================================================

def _normalize_interval(interval: int | str = 1) -> int:
    try:
        s = str(interval).strip().lower().replace(" ", "")
        if s.endswith("min"):
            s = s[:-3]
        n = int(s)
        return n if n > 0 else 1
    except Exception:
        logger.exception("[ranking_summary.pipeline] interval normalize failed interval=%r", interval)
        return 1


# ============================================================
# DataFrame safety
# ============================================================

def _ensure_dataframe(obj: Any, name: str = "obj") -> pd.DataFrame:
    """
    DataFrame / list[dict] / dict などを安全にDataFrame化する
    """
    try:
        if obj is None:
            return pd.DataFrame()

        if isinstance(obj, pd.DataFrame):
            out = obj.copy()

        elif isinstance(obj, dict):
            # dict の場合:
            # 1) {data: [...]} / {rows: [...]} を優先
            for key in ("data", "rows", "items", "result", "results"):
                val = obj.get(key)
                if isinstance(val, (list, tuple)):
                    out = pd.DataFrame(val).copy()
                    break
            else:
                out = pd.DataFrame([obj]).copy()

        elif isinstance(obj, (list, tuple)):
            out = pd.DataFrame(obj).copy()

        else:
            out = pd.DataFrame(obj).copy()

        if out.empty:
            return pd.DataFrame()

        try:
            out.replace([np.inf, -np.inf], np.nan, inplace=True)
        except Exception:
            logger.exception(
                "[ranking_summary.pipeline] inf replace failed name=%s",
                name,
            )

        return out

    except Exception:
        logger.exception(
            "[ranking_summary.pipeline] dataframe conversion failed name=%s",
            name,
        )
        return pd.DataFrame()


def _extract_df_from_return(value: Any, label: str) -> pd.DataFrame:
    """
    候補関数の戻り値から DataFrame を取り出す
    """
    try:
        if isinstance(value, pd.DataFrame):
            return value.copy()

        # tuple(df, meta) / (ok, df) などを想定
        if isinstance(value, tuple):
            for idx, item in enumerate(value):
                df = _ensure_dataframe(item, f"{label}[{idx}]")
                if not df.empty:
                    return df

        # dict / list も許容
        df = _ensure_dataframe(value, label)
        return df

    except Exception:
        logger.exception(
            "[ranking_summary.pipeline] _extract_df_from_return failed label=%s",
            label,
        )
        return pd.DataFrame()


def _coerce_datetime_columns(df: pd.DataFrame) -> pd.DataFrame:
    try:
        out = _ensure_dataframe(df, "datetime_coerce")
        if out.empty:
            return out

        # snapshot_time を datetime に寄せる
        if "snapshot_time" in out.columns and "datetime" not in out.columns:
            out["datetime"] = out["snapshot_time"]

        for col in ("datetime", "dt", "snapshot_time", "timestamp"):
            if col in out.columns:
                try:
                    out[col] = pd.to_datetime(out[col], errors="coerce")
                except Exception:
                    logger.exception(
                        "[ranking_summary.pipeline] datetime coercion failed col=%s",
                        col,
                    )

        return out

    except Exception:
        logger.exception("[ranking_summary.pipeline] _coerce_datetime_columns failed")
        return _ensure_dataframe(df, "datetime_coerce_failed")


def _latest_only(df: pd.DataFrame) -> pd.DataFrame:
    try:
        out = _ensure_dataframe(df, "latest_only")
        if out.empty:
            return out

        dt_col = None
        for c in ("datetime", "snapshot_time", "dt", "timestamp"):
            if c in out.columns:
                dt_col = c
                break

        if not dt_col or "symbol" not in out.columns:
            return out

        out = out.dropna(subset=["symbol", dt_col]).copy()
        if out.empty:
            return out

        out = out.sort_values(["symbol", dt_col])
        out = out.groupby("symbol", as_index=False).tail(1)
        return out.reset_index(drop=True)

    except Exception:
        logger.exception("[ranking_summary.pipeline] _latest_only failed")
        return _ensure_dataframe(df, "latest_only_failed")


def _postprocess_output(df: pd.DataFrame, *, latest_only: bool) -> pd.DataFrame:
    try:
        out = _ensure_dataframe(df, "postprocess")
        out = _coerce_datetime_columns(out)

        # current_price -> close に寄せる
        if "close" not in out.columns and "current_price" in out.columns:
            out["close"] = out["current_price"]

        # type列の吸収
        if "rank_type" not in out.columns and "type" in out.columns:
            out["rank_type"] = out["type"]

        if latest_only:
            out = _latest_only(out)

        logger.info(
            "[ranking_summary.pipeline] postprocess done rows=%s cols=%s",
            len(out),
            list(out.columns)[:20],
        )
        return out

    except Exception:
        logger.exception("[ranking_summary.pipeline] _postprocess_output failed")
        return _ensure_dataframe(df, "postprocess_failed")


# ============================================================
# candidate invocation helpers
# ============================================================

def _call_candidate(fn: Callable, *, interval: int, kwargs: dict, label: str) -> pd.DataFrame:
    """
    既存関数の引数差異にある程度耐えるため、複数パターンで呼ぶ
    """
    patterns = [
        lambda: fn(interval=interval, **kwargs),
        lambda: fn(interval=f"{interval}min", **kwargs),
        lambda: fn(tf=interval, **kwargs),
        lambda: fn(timeframe=interval, **kwargs),
        lambda: fn(),  # 最後の保険
    ]

    last_error = None
    for idx, call in enumerate(patterns, start=1):
        try:
            value = call()
            df = _extract_df_from_return(value, f"{label}.pattern{idx}")
            if isinstance(df, pd.DataFrame):
                logger.info(
                    "[ranking_summary.pipeline] candidate ok label=%s pattern=%s rows=%s",
                    label,
                    idx,
                    len(df),
                )
                return df
        except Exception as e:
            last_error = e

    if last_error is not None:
        logger.exception(
            "[ranking_summary.pipeline] candidate failed label=%s",
            label,
        )
    return pd.DataFrame()


def _resolve_candidates() -> list[tuple[str, Callable]]:
    """
    実環境で名前が揺れていても拾いやすいように候補を並べる
    """
    candidates: list[tuple[str, Callable]] = []

    # 1) ranking_summary_engine 系
    try:
        from trading.ranking.summary import ranking_summary_engine as mod

        for name in (
            "build_ranking_summary_dataframe",
            "build_ranking_summary_df",
            "run_ranking_summary",
            "run_ranking_summary_engine",
            "make_ranking_summary_dataframe",
            "make_ranking_summary_df",
        ):
            fn = getattr(mod, name, None)
            if callable(fn):
                candidates.append((f"ranking_summary_engine.{name}", fn))
    except Exception:
        logger.exception("[ranking_summary.pipeline] import failed: trading.ranking.summary.ranking_summary_engine")

    # 2) trading.ranking.scheduler 系
    try:
        import trading.ranking.scheduler as mod

        for name in (
            "update_ranking_summaries",
            "build_ranking_summaries",
            "collect_ranking_summary",
            "collect_ranking_summaries",
            "run_ranking_summary_job",
            "make_ranking_summary_dataframe",
        ):
            fn = getattr(mod, name, None)
            if callable(fn):
                candidates.append((f"trading.ranking.scheduler.{name}", fn))
    except Exception:
        logger.exception("[ranking_summary.pipeline] import failed: trading.ranking.scheduler")

    # 3) trading.ranking.ranking_summary_engine 系
    try:
        import trading.ranking.ranking_summary_engine as mod

        for name in (
            "build_ranking_summary_dataframe",
            "build_ranking_summary_df",
            "run_ranking_summary",
            "run_ranking_summary_engine",
            "make_ranking_summary_dataframe",
        ):
            fn = getattr(mod, name, None)
            if callable(fn):
                candidates.append((f"trading.ranking.ranking_summary_engine.{name}", fn))
    except Exception:
        logger.exception("[ranking_summary.pipeline] import failed: trading.ranking.ranking_summary_engine")

    return candidates


# ============================================================
# public entrypoint
# ============================================================

def run_ranking_summary_pipeline(
    *,
    interval: int | str = 1,
    latest_only: bool = True,
    **kwargs,
) -> pd.DataFrame:
    """
    ランキング由来サマリー計算の安全な入口
    """
    try:
        interval_n = _normalize_interval(interval)

        logger.info(
            "[ranking_summary.pipeline] start interval=%s latest_only=%s kwargs=%s",
            interval_n,
            latest_only,
            list(kwargs.keys()),
        )

        candidates = _resolve_candidates()
        if not candidates:
            logger.warning(
                "[ranking_summary.pipeline] no callable candidates found interval=%s",
                interval_n,
            )
            return pd.DataFrame()

        seen = set()
        for label, fn in candidates:
            if label in seen:
                continue
            seen.add(label)

            try:
                df = _call_candidate(
                    fn,
                    interval=interval_n,
                    kwargs=kwargs,
                    label=label,
                )
                if isinstance(df, pd.DataFrame) and (not df.empty or len(df.columns) > 0):
                    return _postprocess_output(df, latest_only=latest_only)

            except Exception:
                logger.exception(
                    "[ranking_summary.pipeline] candidate wrapper failed label=%s interval=%s",
                    label,
                    interval_n,
                )

        logger.warning(
            "[ranking_summary.pipeline] all candidates failed interval=%s",
            interval_n,
        )
        return pd.DataFrame()

    except Exception:
        logger.exception(
            "[ranking_summary.pipeline] run_ranking_summary_pipeline failed interval=%r",
            interval,
        )
        return pd.DataFrame()