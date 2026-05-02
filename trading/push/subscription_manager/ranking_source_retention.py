# ============================================================
# File   : trading/push/subscription_manager/ranking_source_retention.py
# Function:
#   - PUSH登録候補銘柄の20分保持ルール
#   - 一度候補に入った銘柄は20分間保持
#   - priority_symbols は20分制限より優先して保持
# ------------------------------------------------------------
# Version: PRODUCTION-REV1.0-RANKING-SOURCE-RETENTION
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .symbols import dedupe_keep_order, normalize_symbol

logger = logging.getLogger(__name__)

REGISTER_MAX_SYMBOLS = 100
RETENTION_MINUTES = 20

_last_seen_by_symbol: Dict[str, dt.datetime] = {}
_last_rank_by_symbol: Dict[str, int] = {}


def _now() -> dt.datetime:
    return dt.datetime.now()


def normalize_symbols(symbols: Optional[Iterable[Any]]) -> List[str]:
    if not symbols:
        return []

    out: List[str] = []
    for x in symbols:
        try:
            s = normalize_symbol(x)
        except Exception:
            s = ""

        if s:
            out.append(s)

    return dedupe_keep_order(out)


def append_unique(result: List[str], symbols: Iterable[Any], *, limit: int) -> None:
    if len(result) >= limit:
        return

    for x in symbols:
        try:
            s = normalize_symbol(x)
        except Exception:
            s = ""

        if not s:
            continue

        if s in result:
            continue

        result.append(s)

        if len(result) >= limit:
            break


def apply_symbol_retention(
    fresh_symbols: Sequence[Any],
    *,
    limit: int = REGISTER_MAX_SYMBOLS,
    retention_minutes: int = RETENTION_MINUTES,
    now: Optional[dt.datetime] = None,
    priority_symbols: Optional[Sequence[Any]] = None,
) -> List[str]:
    """
    毎分の新候補に対して、20分保持ルールを適用する。

    - priority_symbols は最優先で残す
    - fresh_symbols に入った銘柄は last_seen を now に更新
    - fresh_symbols から外れても retention_minutes 分は保持
    - retention_minutes を超えた銘柄は除外
    - 最終的に limit 件まで返す
    """
    global _last_seen_by_symbol
    global _last_rank_by_symbol

    if limit <= 0:
        return []

    now = now or _now()

    priority = normalize_symbols(priority_symbols)
    fresh = normalize_symbols(fresh_symbols)

    for idx, s in enumerate(fresh):
        _last_rank_by_symbol[s] = idx

    for s in fresh:
        _last_seen_by_symbol[s] = now

    for s in priority:
        _last_seen_by_symbol[s] = now
        _last_rank_by_symbol.setdefault(s, -100000)

    cutoff = now - dt.timedelta(minutes=max(1, int(retention_minutes)))

    priority_set = set(priority)
    _last_seen_by_symbol = {
        s: ts
        for s, ts in _last_seen_by_symbol.items()
        if s in priority_set or ts >= cutoff
    }

    alive = set(_last_seen_by_symbol.keys())
    _last_rank_by_symbol = {
        s: r
        for s, r in _last_rank_by_symbol.items()
        if s in alive
    }

    result: List[str] = []

    append_unique(result, priority, limit=limit)
    append_unique(result, fresh, limit=limit)

    retained = sorted(
        _last_seen_by_symbol.items(),
        key=lambda x: (
            x[1],
            -1 * int(_last_rank_by_symbol.get(x[0], 999999)),
        ),
        reverse=True,
    )

    retained_symbols = [s for s, _ts in retained]
    append_unique(result, retained_symbols, limit=limit)

    logger.info(
        "[SUB MANAGER] retention applied fresh=%d retained_state=%d priority=%d result=%d cutoff=%s",
        len(fresh),
        len(_last_seen_by_symbol),
        len(priority),
        len(result),
        cutoff.strftime("%H:%M:%S"),
    )

    return result[:limit]


def seed_retention_state(
    symbols: Sequence[Any],
    *,
    now: Optional[dt.datetime] = None,
) -> List[str]:
    """
    履歴DBから復元した銘柄などを retention state に投入する。
    """
    global _last_seen_by_symbol
    global _last_rank_by_symbol

    now = now or _now()
    syms = normalize_symbols(symbols)

    for idx, s in enumerate(syms):
        _last_seen_by_symbol[s] = now
        _last_rank_by_symbol[s] = idx

    logger.info("[SUB MANAGER] retention state seeded count=%d", len(syms))
    return syms


def has_retention_state() -> bool:
    return bool(_last_seen_by_symbol)


def reset_symbol_retention_state() -> None:
    global _last_seen_by_symbol
    global _last_rank_by_symbol

    _last_seen_by_symbol = {}
    _last_rank_by_symbol = {}

    logger.info("[SUB MANAGER] retention state reset")


def get_symbol_retention_state() -> Dict[str, str]:
    try:
        return {
            s: ts.strftime("%Y-%m-%d %H:%M:%S")
            for s, ts in _last_seen_by_symbol.items()
        }
    except Exception:
        return {}