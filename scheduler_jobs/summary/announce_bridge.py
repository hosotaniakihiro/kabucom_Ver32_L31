# ============================================================
# File   : scheduler_jobs/summary/announce_bridge.py
# Version: Ver1.0-PRODUCTION-SUMMARY-ANNOUNCE-BRIDGE
# ------------------------------------------------------------
# Function:
#   - PUSH / RANKING / 統合候補の Discord 送信橋渡し
#   - collectors.py + announce.py をつなぐ
#   - 日本語 reasons / setup 表示対応
#   - job 側から呼びやすい薄い facade
# ------------------------------------------------------------
# Main APIs:
#   ✔ announce_push_top_candidates()
#   ✔ announce_ranking_top_candidates()
#   ✔ announce_merged_top_candidates()
#   ✔ build_push_top_candidates_message()
#   ✔ build_ranking_top_candidates_message()
#   ✔ build_merged_top_candidates_message()
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, List, Sequence

from trading.summary.announce import (
    build_top_candidates_message,
    announce_top_candidates_to_discord,
)

from trading.summary.top_candidates_pkg.collectors import (
    collect_push_summary_candidates,
    collect_ranking_summary_candidates,
    collect_ai_entry_candidates,
)

logger = logging.getLogger(__name__)


# ============================================================
# util
# ============================================================

def _safe_title(prefix: str, intervals: Iterable[int] | None = None) -> str:
    try:
        if not intervals:
            return prefix
        vals = [str(int(x)) for x in intervals]
        if not vals:
            return prefix
        return f"{prefix} ({'/'.join(vals)}分)"
    except Exception:
        return prefix


def _normalize_candidates(candidates: Any) -> list[dict]:
    if candidates is None:
        return []
    if isinstance(candidates, list):
        return [x for x in candidates if isinstance(x, dict)]
    return []


# ============================================================
# message builders
# ============================================================

def build_push_top_candidates_message(
    *,
    intervals: Iterable[int] = (1, 3, 5),
    top_n: int = 10,
    sides: Iterable[str] = ("BUY", "SELL"),
    drop_fund_etf: bool = True,
    title: str | None = None,
    max_rows: int = 10,
) -> str:
    candidates = collect_push_summary_candidates(
        intervals=intervals,
        top_n=top_n,
        sides=sides,
        drop_fund_etf=drop_fund_etf,
    )

    candidates = _normalize_candidates(candidates)

    return build_top_candidates_message(
        candidates,
        title=title or _safe_title("PUSH候補", intervals),
        max_rows=max_rows,
        include_reason=True,
        include_reason_ja=True,
    )


def build_ranking_top_candidates_message(
    *,
    intervals: Iterable[int] = (1, 3, 5),
    top_n: int = 10,
    sides: Iterable[str] = ("BUY", "SELL"),
    drop_fund_etf: bool = True,
    title: str | None = None,
    max_rows: int = 10,
) -> str:
    candidates = collect_ranking_summary_candidates(
        intervals=intervals,
        top_n=top_n,
        sides=sides,
        drop_fund_etf=drop_fund_etf,
    )

    candidates = _normalize_candidates(candidates)

    return build_top_candidates_message(
        candidates,
        title=title or _safe_title("RANKING候補", intervals),
        max_rows=max_rows,
        include_reason=True,
        include_reason_ja=True,
    )


def build_merged_top_candidates_message(
    *,
    intervals: Iterable[int] = (1, 3, 5),
    top_n: int = 10,
    max_total: int = 30,
    sides: Iterable[str] = ("BUY", "SELL"),
    include_push: bool = True,
    include_ranking: bool = True,
    drop_fund_etf: bool = True,
    title: str | None = None,
    max_rows: int = 10,
) -> str:
    candidates = collect_ai_entry_candidates(
        intervals=intervals,
        top_n=top_n,
        max_total=max_total,
        sides=sides,
        include_push=include_push,
        include_ranking=include_ranking,
        drop_fund_etf=drop_fund_etf,
    )

    candidates = _normalize_candidates(candidates)

    return build_top_candidates_message(
        candidates,
        title=title or _safe_title("統合候補", intervals),
        max_rows=max_rows,
        include_reason=True,
        include_reason_ja=True,
    )


# ============================================================
# discord announcers
# ============================================================

def announce_push_top_candidates(
    discord_sender: Callable[[str], Any],
    *,
    intervals: Iterable[int] = (1, 3, 5),
    top_n: int = 10,
    sides: Iterable[str] = ("BUY", "SELL"),
    drop_fund_etf: bool = True,
    title: str | None = None,
    max_rows: int = 10,
) -> bool:
    candidates = collect_push_summary_candidates(
        intervals=intervals,
        top_n=top_n,
        sides=sides,
        drop_fund_etf=drop_fund_etf,
    )
    candidates = _normalize_candidates(candidates)

    logger.info(
        "[announce_bridge] push candidates=%d intervals=%s top_n=%s",
        len(candidates),
        list(intervals),
        top_n,
    )

    return announce_top_candidates_to_discord(
        discord_sender,
        candidates,
        title=title or _safe_title("PUSH候補", intervals),
        max_rows=max_rows,
        include_reason=True,
        include_reason_ja=True,
    )


def announce_ranking_top_candidates(
    discord_sender: Callable[[str], Any],
    *,
    intervals: Iterable[int] = (1, 3, 5),
    top_n: int = 10,
    sides: Iterable[str] = ("BUY", "SELL"),
    drop_fund_etf: bool = True,
    title: str | None = None,
    max_rows: int = 10,
) -> bool:
    candidates = collect_ranking_summary_candidates(
        intervals=intervals,
        top_n=top_n,
        sides=sides,
        drop_fund_etf=drop_fund_etf,
    )
    candidates = _normalize_candidates(candidates)

    logger.info(
        "[announce_bridge] ranking candidates=%d intervals=%s top_n=%s",
        len(candidates),
        list(intervals),
        top_n,
    )

    return announce_top_candidates_to_discord(
        discord_sender,
        candidates,
        title=title or _safe_title("RANKING候補", intervals),
        max_rows=max_rows,
        include_reason=True,
        include_reason_ja=True,
    )


def announce_merged_top_candidates(
    discord_sender: Callable[[str], Any],
    *,
    intervals: Iterable[int] = (1, 3, 5),
    top_n: int = 10,
    max_total: int = 30,
    sides: Iterable[str] = ("BUY", "SELL"),
    include_push: bool = True,
    include_ranking: bool = True,
    drop_fund_etf: bool = True,
    title: str | None = None,
    max_rows: int = 10,
) -> bool:
    candidates = collect_ai_entry_candidates(
        intervals=intervals,
        top_n=top_n,
        max_total=max_total,
        sides=sides,
        include_push=include_push,
        include_ranking=include_ranking,
        drop_fund_etf=drop_fund_etf,
    )
    candidates = _normalize_candidates(candidates)

    logger.info(
        "[announce_bridge] merged candidates=%d intervals=%s top_n=%s max_total=%s",
        len(candidates),
        list(intervals),
        top_n,
        max_total,
    )

    return announce_top_candidates_to_discord(
        discord_sender,
        candidates,
        title=title or _safe_title("統合候補", intervals),
        max_rows=max_rows,
        include_reason=True,
        include_reason_ja=True,
    )