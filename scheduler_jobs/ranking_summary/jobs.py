# ============================================================
# File   : scheduler_jobs/ranking_summary/jobs.py
# Version: Ver32_L01-RANKING-SUMMARY-JOBS-JA-ANNOUNCE-BRIDGE
# ------------------------------------------------------------
# 機能:
#   - ランキング由来サマリーの定時job定義
#   - 1min / 3min / 5min の各jobを分離
#   - runners.py の ranking専用実行線を呼び出す
#   - return_details=True の場合は詳細dictを返却
#   - 既存互換として通常は DataFrame を返却
#   - 日本語 announce bridge の結果を details に統合
#
# 目的:
#   - 旧 job_ranking_summary などの曖昧さを減らし、
#     ランキング由来サマリーの定時実行入口を明確にする
#   - PUSH側と揃えて details 返却互換を持たせる
#   - bridge 関数が利用可能な場合は、日本語 reasons / setup を含む
#     Discord通知もここから呼べるようにする
#
# 主な関数:
#   - job_ranking_summary_1m()
#   - job_ranking_summary_3m()
#   - job_ranking_summary_5m()
#   - job_ranking_summary(interval)
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict, Callable, Optional

import pandas as pd

from scheduler_jobs.ranking_summary.runners import run_ranking_summary_job

logger = logging.getLogger(__name__)

try:
    from scheduler_jobs.summary.announce_bridge import (
        announce_ranking_top_candidates,
        build_ranking_top_candidates_message,
    )
except Exception:  # pragma: no cover
    announce_ranking_top_candidates = None
    build_ranking_top_candidates_message = None


def _empty_details(interval: int | str) -> Dict[str, Any]:
    return {
        "df": pd.DataFrame(),
        "interval": interval,
        "announce_results": {
            "ranking": False,
            "bridge_ranking": False,
        },
        "announce_messages": {
            "ranking": "",
        },
        "top_candidates": [],
        "entry_results": [],
    }


def _safe_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "y")
    try:
        return bool(v)
    except Exception:
        return default


def _safe_sender(v: Any) -> Optional[Callable[[str], Any]]:
    if callable(v):
        return v
    return None


def _merge_details(base: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)

    for key, value in result.items():
        if key == "announce_results":
            merged = dict(out.get("announce_results", {}))
            if isinstance(value, dict):
                merged.update(value)
            out["announce_results"] = merged
            continue

        if key == "announce_messages":
            merged = dict(out.get("announce_messages", {}))
            if isinstance(value, dict):
                merged.update(value)
            out["announce_messages"] = merged
            continue

        out[key] = value

    return out


def _run_bridge_announce_ranking(
    *,
    discord_sender: Optional[Callable[[str], Any]],
    interval: int | str,
    top_n: int,
    sides: Any,
    details: Dict[str, Any],
) -> Dict[str, Any]:
    if discord_sender is None:
        return details

    if announce_ranking_top_candidates is None and build_ranking_top_candidates_message is None:
        logger.debug("[ranking_summary.jobs] announce bridge not available")
        return details

    try:
        if build_ranking_top_candidates_message is not None:
            message = build_ranking_top_candidates_message(
                intervals=(int(interval),),
                top_n=top_n,
                sides=sides,
                title=f"RANKING候補 ({interval}分)",
                max_rows=top_n,
            )
            details.setdefault("announce_messages", {})
            details["announce_messages"]["ranking"] = message

        if announce_ranking_top_candidates is not None:
            ok = announce_ranking_top_candidates(
                discord_sender=discord_sender,
                intervals=(int(interval),),
                top_n=top_n,
                sides=sides,
                title=f"RANKING候補 ({interval}分)",
                max_rows=top_n,
            )
            details.setdefault("announce_results", {})
            details["announce_results"]["bridge_ranking"] = bool(ok)

    except Exception:
        logger.exception("[ranking_summary.jobs] bridge announce failed interval=%r", interval)

    return details


def job_ranking_summary(
    interval: int | str = 1,
    display: bool = True,
    **kwargs,
) -> pd.DataFrame | Dict[str, Any]:
    """
    ランキング由来サマリーjob共通入口

    後方互換:
      - 通常は DataFrame を返す
      - return_details=True の場合は dict を返す

    追加:
      - discord_sender を渡した場合、日本語 announce bridge をここから呼べる
      - announce_bridge=True の場合に bridge 実行を試行
    """
    return_details = _safe_bool(kwargs.get("return_details"), False)
    use_announce_bridge = _safe_bool(kwargs.get("announce_bridge"), False)
    discord_sender = _safe_sender(kwargs.get("discord_sender"))
    top_n = int(kwargs.get("top_n", 10))
    sides = kwargs.get("sides", ("BUY", "SELL"))

    details = _empty_details(interval)

    try:
        logger.info(
            "[ranking_summary.jobs] start interval=%r display=%s return_details=%s announce_bridge=%s",
            interval,
            display,
            return_details,
            use_announce_bridge,
        )

        result = run_ranking_summary_job(interval=interval, display=display, **kwargs)

        # ----------------------------------------------------
        # details 優先
        # ----------------------------------------------------
        if return_details:
            if isinstance(result, dict):
                details = _merge_details(details, result)
                df = details.get("df", pd.DataFrame())

                if use_announce_bridge and discord_sender is not None:
                    details = _run_bridge_announce_ranking(
                        discord_sender=discord_sender,
                        interval=interval,
                        top_n=top_n,
                        sides=sides,
                        details=details,
                    )

                logger.info(
                    "[ranking_summary.jobs] finished interval=%r rows=%s details=True",
                    interval,
                    len(df) if isinstance(df, pd.DataFrame) else 0,
                )
                return details

            if isinstance(result, pd.DataFrame):
                details["df"] = result

                if use_announce_bridge and discord_sender is not None:
                    details = _run_bridge_announce_ranking(
                        discord_sender=discord_sender,
                        interval=interval,
                        top_n=top_n,
                        sides=sides,
                        details=details,
                    )

                logger.info(
                    "[ranking_summary.jobs] finished interval=%r rows=%s details=True(fallback)",
                    interval,
                    len(result),
                )
                return details

            logger.warning(
                "[ranking_summary.jobs] runner returned unexpected type interval=%r type=%s details=True",
                interval,
                type(result).__name__,
            )
            return details

        # ----------------------------------------------------
        # 既存互換
        # ----------------------------------------------------
        if isinstance(result, dict):
            df = result.get("df", pd.DataFrame())

            if use_announce_bridge and discord_sender is not None:
                _run_bridge_announce_ranking(
                    discord_sender=discord_sender,
                    interval=interval,
                    top_n=top_n,
                    sides=sides,
                    details=details,
                )

            logger.info(
                "[ranking_summary.jobs] finished interval=%r rows=%s details=False(dict->df)",
                interval,
                len(df) if isinstance(df, pd.DataFrame) else 0,
            )
            return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

        if isinstance(result, pd.DataFrame):
            if use_announce_bridge and discord_sender is not None:
                _run_bridge_announce_ranking(
                    discord_sender=discord_sender,
                    interval=interval,
                    top_n=top_n,
                    sides=sides,
                    details=details,
                )

            logger.info(
                "[ranking_summary.jobs] finished interval=%r rows=%s",
                interval,
                len(result),
            )
            return result

        logger.warning(
            "[ranking_summary.jobs] runner returned unexpected type interval=%r type=%s",
            interval,
            type(result).__name__,
        )
        return pd.DataFrame()

    except Exception:
        logger.exception(
            "[ranking_summary.jobs] job_ranking_summary failed interval=%r",
            interval,
        )
        return details if return_details else pd.DataFrame()


def job_ranking_summary_1m(display: bool = True, **kwargs) -> pd.DataFrame | Dict[str, Any]:
    """
    ランキング由来 1分サマリーjob
    """
    return job_ranking_summary(interval=1, display=display, **kwargs)


def job_ranking_summary_3m(display: bool = True, **kwargs) -> pd.DataFrame | Dict[str, Any]:
    """
    ランキング由来 3分サマリーjob
    """
    return job_ranking_summary(interval=3, display=display, **kwargs)


def job_ranking_summary_5m(display: bool = True, **kwargs) -> pd.DataFrame | Dict[str, Any]:
    """
    ランキング由来 5分サマリーjob
    """
    return job_ranking_summary(interval=5, display=display, **kwargs)