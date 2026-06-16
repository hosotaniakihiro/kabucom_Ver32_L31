# ============================================================
# File   : trading/ranking/active_symbols/db_ranking_fallback_patch.py
# Version: Ver1.0-ACTIVE-SYMBOLS-DB-RANKING-FALLBACK
# ------------------------------------------------------------
# 目的:
#   active_symbol_manager 側で today_ranking が0件になっても、
#   当日の rankingYYYYMMDD.db から100銘柄を復元して
#   global_data.active_symbols / push_symbols_100 等へ反映する。
#
# 背景:
#   PUSH rotation 側は DB fallback で100銘柄に復旧できているが、
#   active_symbol_manager.update_active_symbols(force=True) が0件を返すと
#   runtime seed / active source が空のままになる。
# ============================================================
from __future__ import annotations

import datetime as _dt
import logging
import os
import sqlite3
from typing import Iterable, Any

from global_state import global_data

logger = logging.getLogger(__name__)

VERSION = "V1.0-ACTIVE-SYMBOLS-DB-RANKING-FALLBACK"


def _dedupe_symbols(values: Iterable[Any], *, limit: int = 300) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values or []:
        try:
            s = str(v or "").strip().upper()
        except Exception:
            continue
        if not s:
            continue
        s = s.replace(".T", "").replace("-T", "")
        if s.endswith("0") and len(s) == 5 and s[:4].isdigit():
            s = s[:4]
        if not (s[:4].isdigit() or (len(s) >= 3 and any(ch.isdigit() for ch in s))):
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _today_yyyymmdd() -> str:
    for name in ("TRADE_DATE", "KABU_TRADE_DATE", "ATS_TRADE_DATE", "TARGET_DATE"):
        v = str(os.environ.get(name, "")).strip()
        digits = "".join(ch for ch in v if ch.isdigit())
        if len(digits) >= 8:
            return digits[:8]
    return _dt.date.today().strftime("%Y%m%d")


def _iter_ranking_db_paths() -> list[str]:
    ymd = _today_yyyymmdd()
    roots = _dedupe_symbols([])  # type keeper; replaced below
    del roots
    raw_roots = [
        os.environ.get("AUTOSTOCK_ROOT"),
        os.environ.get("AUTO_STOCK_ROOT"),
        os.environ.get("KABU_DATA_ROOT"),
        os.environ.get("RAW_DATA_ROOT"),
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data",
    ]
    paths: list[str] = []
    seen: set[str] = set()
    for root in raw_roots:
        if not root:
            continue
        r = str(root).rstrip("\\/")
        for p in (
            rf"{r}\kabu_station\ranking\ranking{ymd}.db",
            rf"{r}\ranking\ranking{ymd}.db",
        ):
            key = p.lower()
            if key in seen:
                continue
            seen.add(key)
            if os.path.exists(p):
                paths.append(p)
    return paths


def _load_symbols_from_ranking_db(*, max_rows: int = 300) -> list[str]:
    out: list[str] = []
    for path in _iter_ranking_db_paths():
        try:
            con = sqlite3.connect(path, timeout=0.5)
            try:
                cur = con.cursor()
                tables = [
                    r[0]
                    for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                    if r and r[0]
                ]
                preferred = [
                    t for t in tables
                    if any(k in str(t).lower() for k in ("ranking", "snapshot", "raw"))
                ] or tables

                for table in preferred:
                    if len(out) >= max_rows:
                        break
                    try:
                        cols = [r[1] for r in cur.execute(f'PRAGMA table_info("{table}")').fetchall()]
                    except Exception:
                        continue
                    col = next((c for c in cols if str(c).lower() in {"symbol", "code", "銘柄コード"}), None)
                    if not col:
                        continue
                    order_col = next(
                        (
                            c for c in cols
                            if str(c).lower() in {
                                "datetime", "created_at", "updated_at", "currentpricetime",
                                "time", "timestamp", "no", "average_ranking",
                            }
                        ),
                        None,
                    )
                    sql = f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL'
                    if order_col:
                        # ランキング時刻系は新しい順、No系も先頭を優先する。
                        direction = "ASC" if str(order_col).lower() in {"no", "average_ranking"} else "DESC"
                        sql += f' ORDER BY "{order_col}" {direction}'
                    sql += f' LIMIT {int(max_rows)}'
                    try:
                        vals = [r[0] for r in cur.execute(sql).fetchall()]
                    except Exception:
                        continue
                    out = _dedupe_symbols(list(out) + vals, limit=max_rows)
                logger.warning(
                    "[ACTIVE DB FALLBACK] db scan path=%s symbols=%d head=%s",
                    path,
                    len(out),
                    out[:20],
                )
            finally:
                con.close()
        except Exception:
            logger.debug("[ACTIVE DB FALLBACK] read failed path=%s", path, exc_info=True)
        if len(out) >= max_rows:
            break
    return out[:max_rows]


def _publish(symbols: list[str], *, source: str) -> None:
    try:
        syms = _dedupe_symbols(symbols, limit=100)
        global_data.symbols_active = set(syms)
        global_data.active_symbols = list(syms)
        global_data.monitor_symbols = list(syms)
        global_data.push_symbols_100 = list(syms)
        global_data.candidate_push_symbols = list(syms)
        global_data.push_candidate_symbols = list(syms)
        global_data.ats_register_targets = list(syms)
        global_data.ats_targets = list(syms)
        global_data.should_register_symbols = list(syms)
        global_data.push_symbols = list(syms)
        global_data.active_symbol_source = source
        global_data.active_symbol_today_ranking_available = True
        global_data.active_symbol_allowed_universe_size = len(syms)
        logger.warning(
            "[ACTIVE DB FALLBACK] published total=%d source=%s head=%s",
            len(syms),
            source,
            syms[:20],
        )
    except Exception:
        logger.exception("[ACTIVE DB FALLBACK] publish failed")


def _fallback_symbols(*, target: int = 100) -> list[str]:
    symbols = _load_symbols_from_ranking_db(max_rows=max(target * 3, 300))
    symbols = _dedupe_symbols(symbols, limit=target)
    if symbols:
        _publish(symbols, source="today_ranking_db_fallback")
    return symbols


def install() -> bool:
    try:
        from . import manager as m
    except Exception:
        logger.exception("[ACTIVE DB FALLBACK] manager import failed")
        return False

    if getattr(m, "_ACTIVE_DB_FALLBACK_PATCHED", False):
        return True

    original_update = m.update_active_symbols
    original_get_active = m.get_active_symbols

    def update_active_symbols_patched(force: bool = False):
        symbols = []
        try:
            symbols = list(original_update(force=force) or [])
        except Exception:
            logger.exception("[ACTIVE DB FALLBACK] original update failed; try db fallback")
            symbols = []

        try:
            target = int(getattr(m, "TARGET_ACTIVE_SYMBOLS", 100) or 100)
        except Exception:
            target = 100

        if len(symbols) >= target:
            return symbols

        fb = _fallback_symbols(target=target)
        if fb:
            logger.warning(
                "[ACTIVE DB FALLBACK] recovered active symbols before=%d after=%d target=%d head=%s",
                len(symbols),
                len(fb),
                target,
                fb[:20],
            )
            return fb
        return symbols

    def get_active_symbols_patched(*args, **kwargs):
        symbols = []
        try:
            symbols = list(original_get_active(*args, **kwargs) or [])
        except Exception:
            logger.exception("[ACTIVE DB FALLBACK] original getter failed; try db fallback")
            symbols = []
        if symbols:
            return symbols[: getattr(m, "MAX_ACTIVE_SYMBOLS", 100)]
        return _fallback_symbols(target=int(getattr(m, "TARGET_ACTIVE_SYMBOLS", 100) or 100))

    def get_current_active_symbols_patched(*args, **kwargs):
        return get_active_symbols_patched(*args, **kwargs)

    def get_monitor_symbols_patched(*args, **kwargs):
        return get_active_symbols_patched(*args, **kwargs)[: getattr(m, "MAX_ACTIVE_SYMBOLS", 100)]

    def get_push_symbols_patched(*args, **kwargs):
        return get_monitor_symbols_patched(*args, **kwargs)

    def get_register_symbols_patched(*args, **kwargs):
        return get_monitor_symbols_patched(*args, **kwargs)

    def get_subscription_symbols_patched(*args, **kwargs):
        return get_monitor_symbols_patched(*args, **kwargs)

    def get_rotation_symbols_patched(*args, **kwargs):
        return get_monitor_symbols_patched(*args, **kwargs)

    m.update_active_symbols = update_active_symbols_patched
    m.get_active_symbols = get_active_symbols_patched
    m.get_current_active_symbols = get_current_active_symbols_patched
    m.get_monitor_symbols = get_monitor_symbols_patched
    m.get_push_symbols = get_push_symbols_patched
    m.get_register_symbols = get_register_symbols_patched
    m.get_subscription_symbols = get_subscription_symbols_patched
    m.get_rotation_symbols = get_rotation_symbols_patched
    m._ACTIVE_DB_FALLBACK_PATCHED = True

    logger.warning("[ACTIVE DB FALLBACK] installed version=%s", VERSION)
    return True
