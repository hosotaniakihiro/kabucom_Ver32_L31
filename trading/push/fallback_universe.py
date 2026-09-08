# ============================================================
# File   : trading/push/fallback_universe.py
# Version: V1-PUSH-FALLBACK-UNIVERSE
# ------------------------------------------------------------
# PUSH 登録候補が Ranking だけに依存しないようにする。
#
# 背景 (2026-09-01 12:56-13:00 の実測):
#   Ranking API が全滅すると
#     [ACTIVE CATEGORY QUOTA] selected=0
#     [PUSH RUNTIME] failed to seed push/register symbols: all providers returned empty
#   となり、PUSH 登録候補がひとつも作れなくなっていた。
#   候補が作れない -> 再登録できない -> PUSH が止まる -> Summary が空 ->
#   Ranking 候補も作れない、という循環になる。
#
# ここでは Ranking / ネットワークに一切依存しない候補を返す。
# 優先順位:
#   1. last_good_registered_symbols (直近で kabu が受理した実績のある銘柄)
#   2. symbol_flags.db の売買対象ユニバース (オフラインで常に読める)
#   3. daily_watchlist (前日までに作った当日ウォッチリスト)
#
# これは「Ranking の代わり」ではなく「Ranking 障害中に現状を壊さないための下敷き」。
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V1-PUSH-FALLBACK-UNIVERSE"

_LOCK = threading.RLock()
_cache: list[str] = []
_cache_at: float = 0.0


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize(symbols: Any) -> list[str]:
    from .registration_state import normalize

    return normalize(symbols)


# ------------------------------------------------------------
# source 2: symbol_flags.db (オフラインで常に読める固定ユニバース)
# ------------------------------------------------------------

def load_symbol_flags_universe(limit: int = 0) -> list[str]:
    """symbol_flags.db の売買対象銘柄。ネットワーク不要。"""
    ttl = max(0, _env_int("PUSH_FALLBACK_UNIVERSE_CACHE_SEC", 600))
    with _LOCK:
        if _cache and ttl > 0 and (time.time() - _cache_at) < ttl:
            return list(_cache[:limit]) if limit else list(_cache)

    path = ""
    try:
        from config.paths import get_path

        path = str(get_path("symbol_flags_db") or "")
    except Exception:
        logger.debug("[PUSH FALLBACK] symbol_flags path resolve failed", exc_info=True)

    if not path or not os.path.exists(path):
        logger.warning("[PUSH FALLBACK] symbol_flags.db not found path=%s", path or "-")
        return []

    rows: list[str] = []
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=3.0)
        try:
            cols = {r[1] for r in con.execute('PRAGMA table_info("symbol_flags")')}
            where = []
            if "is_etf" in cols:
                where.append("COALESCE(is_etf,0)=0")
            if "buy_target" in cols and "sell_target" in cols:
                where.append("(COALESCE(buy_target,0)=1 OR COALESCE(sell_target,0)=1)")
            elif "buy_target" in cols:
                where.append("COALESCE(buy_target,0)=1")
            sql = 'SELECT symbol FROM symbol_flags'
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY symbol"
            rows = [str(r[0]) for r in con.execute(sql) if r and r[0]]
        finally:
            con.close()
    except Exception:
        logger.exception("[PUSH FALLBACK] symbol_flags.db read failed path=%s", path)
        return []

    syms = _normalize(rows)
    with _LOCK:
        globals()["_cache"] = syms
        globals()["_cache_at"] = time.time()
    logger.warning(
        "[PUSH FALLBACK] symbol_flags universe loaded count=%d path=%s", len(syms), path
    )
    return list(syms[:limit]) if limit else list(syms)


# ------------------------------------------------------------
# source 3: daily watchlist
# ------------------------------------------------------------

def load_daily_watchlist() -> list[str]:
    for module_name, func_name in (
        ("optional.batch.daily_watchlist", "load_daily_watchlist_symbols"),
        ("optional.batch.daily_watchlist", "get_daily_watchlist_symbols"),
    ):
        try:
            import importlib

            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
            if not callable(fn):
                continue
            syms = _normalize(fn())
            if syms:
                logger.warning("[PUSH FALLBACK] daily_watchlist count=%d via %s", len(syms), func_name)
                return syms
        except Exception:
            logger.debug("[PUSH FALLBACK] daily_watchlist %s failed", func_name, exc_info=True)
    return []


# ------------------------------------------------------------
# 公開入口
# ------------------------------------------------------------

def resolve_fallback_symbols(target_total: int = 100) -> tuple[list[str], str]:
    """Ranking 非依存の登録候補を返す。

    戻り値: (symbols, source)
    どれも取れなければ ([], "") を返す。
    """
    if not _env_bool("PUSH_FALLBACK_UNIVERSE_ENABLED", True):
        return [], ""

    target_total = max(1, int(target_total or 100))

    try:
        from .registration_state import last_good_registered_symbols

        last_good = last_good_registered_symbols()
    except Exception:
        last_good = []
    if last_good:
        logger.warning(
            "[PUSH FALLBACK] using last_good_registered_symbols count=%d "
            "(Ranking が空でも直近の正常登録を維持する)",
            len(last_good),
        )
        return list(last_good[:target_total]), "last_good_registered"

    flags = load_symbol_flags_universe()
    if flags:
        picked = list(flags[:target_total])
        logger.warning(
            "[PUSH FALLBACK] using symbol_flags universe count=%d/%d "
            "(Ranking 障害中の下敷き)",
            len(picked), len(flags),
        )
        return picked, "symbol_flags_universe"

    watch = load_daily_watchlist()
    if watch:
        return list(watch[:target_total]), "daily_watchlist"

    logger.error("[PUSH FALLBACK] no fallback source available")
    return [], ""


def build_ab_batches(
    symbols: Any,
    *,
    fixed: Any = None,
    batch_size: int = 50,
) -> tuple[list[str], list[str]]:
    """固定枠を必ず両バッチへ入れて A/B を作る。

    固定銘柄 (ポジション保有銘柄など) は Ranking 障害中でも
    どちらのバッチからも落ちないようにする。
    """
    batch_size = max(1, int(batch_size or 50))
    fixed_syms = _normalize(fixed)[:batch_size]
    pool = [s for s in _normalize(symbols) if s not in set(fixed_syms)]

    room = max(0, batch_size - len(fixed_syms))
    a_var = pool[:room]
    b_var = pool[room:room * 2] if room else []
    if not b_var:
        b_var = a_var

    batch_a = fixed_syms + a_var
    batch_b = fixed_syms + b_var
    return batch_a[:batch_size], batch_b[:batch_size]


def reset_cache_for_test() -> None:
    with _LOCK:
        globals()["_cache"] = []
        globals()["_cache_at"] = 0.0


__all__ = [
    "VERSION",
    "load_symbol_flags_universe",
    "load_daily_watchlist",
    "resolve_fallback_symbols",
    "build_ab_batches",
    "reset_cache_for_test",
]
