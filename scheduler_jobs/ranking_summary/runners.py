# ============================================================
# File   : scheduler_jobs/ranking_summary/runners.py
# Version: Ver32_L01-RANKING-SUMMARY-RUNNER-JA-ANNOUNCE-BRIDGE
# ------------------------------------------------------------
# 機能:
#   - ランキング由来サマリーの実行線を一本化
#   - ranking pipeline 実行
#   - ranking専用cacheへ保存
#   - ranking専用displayを呼び出す
#   - 日本語 announce bridge を追加
#   - return_details=True 対応
#   - 例外時のログ切り分け
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional

import pandas as pd

from trading.ranking_summary.cache import (
    set_ranking_summary,
    set_ranking_summary_latest_dt,
    set_ranking_summary_meta,
)
from scheduler_jobs.ranking_summary.display import display_ranking_summary

logger = logging.getLogger(__name__)


def _resolve_ranking_pipeline():
    try:
        from trading.ranking_summary.pipeline import run_ranking_summary_pipeline
        return run_ranking_summary_pipeline
    except Exception:
        logger.exception("[ranking_summary.runners] resolve ranking pipeline failed")
        return None


def _resolve_announce_bridge_ranking():
    try:
        from scheduler_jobs.summary.announce_bridge import (
            announce_ranking_top_candidates,
            build_ranking_top_candidates_message,
        )
        return announce_ranking_top_candidates, build_ranking_top_candidates_message
    except Exception:
        logger.debug("[ranking_summary.runners] resolve announce bridge ranking failed", exc_info=True)
        return None, None


def _extract_latest_dt(df: pd.DataFrame):
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None

        for col in ("datetime", "dt", "snapshot_time", "timestamp"):
            if col in df.columns:
                s = df[col].dropna()
                if not s.empty:
                    return s.max()
        return None
    except Exception:
        logger.exception("[ranking_summary.runners] extract latest_dt failed")
        return None


def _safe_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "y", "ok")
    try:
        return bool(v)
    except Exception:
        return default


def _safe_int(v: Any, default: int) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _safe_sender(v: Any) -> Optional[Callable[[str], Any]]:
    if callable(v):
        return v
    return None


def _empty_result_dict(interval, df: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    return {
        "df": df if isinstance(df, pd.DataFrame) else pd.DataFrame(),
        "interval": interval,
        "announce_results": {
            "bridge_ranking": False,
        },
        "announce_messages": {
            "ranking": "",
        },
    }


def _run_announce_bridge_ranking(
    *,
    interval: int | str,
    discord_sender=None,
    announce_bridge_enabled: bool = False,
    top_n: int = 10,
    sides=("BUY", "SELL"),
) -> Dict[str, Any]:
    out = {
        "bridge_ranking": False,
        "ranking_message": "",
    }

    if not announce_bridge_enabled:
        return out

    announce_fn, build_msg_fn = _resolve_announce_bridge_ranking()
    if not callable(announce_fn) and not callable(build_msg_fn):
        logger.warning("[ranking_summary.runners] announce bridge ranking unavailable interval=%r", interval)
        return out

    try:
        if callable(build_msg_fn):
            out["ranking_message"] = build_msg_fn(
                intervals=(int(interval),),
                top_n=top_n,
                sides=sides,
                title=f"RANKING候補 ({interval}分)",
                max_rows=top_n,
            )

        if callable(announce_fn) and callable(discord_sender):
            out["bridge_ranking"] = bool(
                announce_fn(
                    discord_sender=discord_sender,
                    intervals=(int(interval),),
                    top_n=top_n,
                    sides=sides,
                    title=f"RANKING候補 ({interval}分)",
                    max_rows=top_n,
                )
            )
    except Exception:
        logger.exception("[ranking_summary.runners] announce bridge ranking failed interval=%r", interval)

    return out


def run_ranking_summary_job(
    interval: int | str = 1,
    display: bool = True,
    **kwargs,
) -> pd.DataFrame | Dict[str, Any]:
    details = _empty_result_dict(interval)

    try:
        logger.info(
            "[ranking_summary.runners] job start interval=%r display=%s kwargs_keys=%s",
            interval,
            display,
            sorted(kwargs.keys()),
        )

        fn = _resolve_ranking_pipeline()
        if not callable(fn):
            logger.error(
                "[ranking_summary.runners] ranking pipeline is not callable interval=%r",
                interval,
            )
            return details if _safe_bool(kwargs.get("return_details"), False) else pd.DataFrame()

        df = fn(interval=interval, **kwargs)
        if not isinstance(df, pd.DataFrame):
            logger.warning(
                "[ranking_summary.runners] pipeline returned non-DataFrame interval=%r type=%s",
                interval,
                type(df).__name__,
            )
            df = pd.DataFrame()

        details["df"] = df

        set_ranking_summary(interval, df)
        set_ranking_summary_latest_dt(interval, _extract_latest_dt(df))
        set_ranking_summary_meta(
            interval,
            {
                "rows": len(df),
                "columns": list(df.columns),
                "source": "ranking",
                "interval": interval,
            },
        )

        logger.info(
            "[ranking_summary.runners] job finished interval=%r rows=%s",
            interval,
            len(df),
        )

        if display:
            try:
                display_ranking_summary(interval=interval)
            except Exception:
                logger.exception("[ranking_summary.runners] display failed interval=%r", interval)

        bridge_res = _run_announce_bridge_ranking(
            interval=interval,
            discord_sender=_safe_sender(kwargs.get("discord_sender")),
            announce_bridge_enabled=_safe_bool(kwargs.get("announce_bridge"), False),
            top_n=_safe_int(kwargs.get("top_n"), 10),
            sides=kwargs.get("sides", ("BUY", "SELL")),
        )
        details["announce_results"]["bridge_ranking"] = bool(bridge_res.get("bridge_ranking"))
        details["announce_messages"]["ranking"] = bridge_res.get("ranking_message", "")

        if _safe_bool(kwargs.get("return_details"), False):
            return details
        return df
    except Exception:
        logger.exception(
            "[ranking_summary.runners] run_ranking_summary_job failed interval=%r",
            interval,
        )
        return details if _safe_bool(kwargs.get("return_details"), False) else pd.DataFrame()