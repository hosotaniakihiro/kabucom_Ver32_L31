# ============================================================
# ats_sources.py
# Ver1.0-PRODUCTION-ATS-SOURCES
# ------------------------------------------------------------
# ✔ OPENポジション取得
# ✔ pendingエントリー取得
# ✔ ACTIVE銘柄ローテーション
# ✔ light銘柄 fallback
# ✔ tier(A/B/C)対応
# ✔ rotation index 管理
# ✔ SQLite安全
# ✔ 本番例外耐性
# ============================================================

import logging
from typing import List, Tuple

from database import Session_position
from database.models import Position

from global_state import global_data

from trading.entry.pending_manager import get_bucket


logger = logging.getLogger(__name__)


# ============================================================
# OPENポジション取得
# ============================================================

def get_open_symbols(max_symbols: int = 5) -> List[str]:

    session = Session_position()

    try:

        rows = (
            session.query(Position)
            .filter(Position.status == "OPEN")
            .all()
        )

        symbols = [
            str(r.symbol)
            for r in rows
            if r.symbol
        ]

        return symbols[:max_symbols]

    except Exception:

        logger.exception("get_open_symbols error")
        return []

    finally:

        session.close()


# ============================================================
# pendingエントリー
# ============================================================

def get_pending_symbols(limit: int = 20) -> List[str]:

    symbols = []

    pending = getattr(global_data, "pending_entries", {})

    if not isinstance(pending, dict):
        return symbols

    try:

        for sym in pending.keys():

            bucket = get_bucket(sym)

            if bucket:
                symbols.append(str(sym))

            if len(symbols) >= limit:
                break

    except Exception:

        logger.exception("get_pending_symbols error")

    return symbols


# ============================================================
# ACTIVE銘柄取得
# ============================================================

def _get_active_symbol_pool() -> List[str]:

    ordered = []

    tiers = getattr(global_data, "symbols_active_tier", None)

    # tier構造
    if isinstance(tiers, dict):

        ordered += tiers.get("A", [])
        ordered += tiers.get("B", [])
        ordered += tiers.get("C", [])

    # fallback
    if not ordered:

        ordered = list(
            getattr(global_data, "symbols_active", []) or []
        )

    # light fallback
    if not ordered:

        ordered = list(
            getattr(global_data, "symbols_light", []) or []
        )

    ordered = [str(s) for s in ordered if s]

    return ordered


# ============================================================
# ACTIVEローテーション
# ============================================================

def get_rotated_active_symbols(
    batch_size: int = 50,
    shift: int = 20
) -> Tuple[List[str], int | None]:

    ordered = _get_active_symbol_pool()

    if not ordered:
        return [], None

    # rotation index
    if not hasattr(global_data, "rotation_index"):
        global_data.rotation_index = 0

    n = len(ordered)

    start = global_data.rotation_index % n

    rotated = [
        ordered[(start + i) % n]
        for i in range(min(batch_size, n))
    ]

    next_index = (start + shift) % n

    return rotated, next_index


# ============================================================
# ATS候補生成
# ============================================================

def build_source_candidates(
    batch_size: int = 50,
    max_open: int = 5,
    pending_limit: int = 20
) -> Tuple[List[str], int | None]:

    open_syms = get_open_symbols(max_open)

    pending_syms = get_pending_symbols(pending_limit)

    rotated_syms, next_index = get_rotated_active_symbols(
        batch_size=batch_size
    )

    merged = open_syms + pending_syms + rotated_syms

    # order維持ユニーク
    uniq = list(dict.fromkeys([str(s) for s in merged if s]))

    logger.info(
        "[ATS Sources] open=%d pending=%d rotated=%d total=%d",
        len(open_syms),
        len(pending_syms),
        len(rotated_syms),
        len(uniq),
    )

    return uniq, next_index


# ============================================================
# rotation index 更新
# ============================================================

def update_rotation_index(next_index: int | None):

    if next_index is None:
        return

    try:

        global_data.rotation_index = next_index

    except Exception:

        logger.exception("rotation index update failed")