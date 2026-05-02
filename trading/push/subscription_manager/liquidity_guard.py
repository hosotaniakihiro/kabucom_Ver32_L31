# ============================================================
# File   : trading/push/subscription_manager/liquidity_guard.py
# Version: PRODUCTION-STABLE-PUSH-LIQUIDITY-GUARD-V1.0
# ------------------------------------------------------------
# 【概要】
#   PUSH登録対象から、日中出来高・売買代金が少なすぎる銘柄を除外する。
#
# 【目的】
#   - 日中100株程度しか出来ていない薄商い銘柄をPUSH登録対象に入れない
#   - rotation.py の A/B 50銘柄登録前に使う
#   - rankingYYYYMMDD.db の ranking_snapshot_1min / ranking_raw_1min 等から
#     当日累積出来高・価格を拾って判定する
#
# 【環境変数】
#   PUSH_REGISTER_LIQUIDITY_GUARD_ENABLED=1
#   PUSH_REGISTER_MIN_DAY_VOLUME=10000
#   PUSH_REGISTER_MIN_DAY_TURNOVER=10000000
#   PUSH_REGISTER_LIQUIDITY_CACHE_TTL_SEC=20
#   PUSH_REGISTER_LIQUIDITY_MIN_COVERAGE_RATIO=0.20
#
# 【重要】
#   - DBが見つからない、または取得率が低すぎる場合は登録対象を壊さないため pass-through
#   - 取得できた銘柄については volume / turnover で除外
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


ENABLED = str(os.environ.get("PUSH_REGISTER_LIQUIDITY_GUARD_ENABLED", "1")).lower() not in {
    "0",
    "false",
    "no",
    "off",
}

MIN_DAY_VOLUME = float(os.environ.get("PUSH_REGISTER_MIN_DAY_VOLUME", "10000"))
MIN_DAY_TURNOVER = float(os.environ.get("PUSH_REGISTER_MIN_DAY_TURNOVER", "10000000"))

CACHE_TTL_SEC = float(os.environ.get("PUSH_REGISTER_LIQUIDITY_CACHE_TTL_SEC", "20"))
MIN_COVERAGE_RATIO = float(os.environ.get("PUSH_REGISTER_LIQUIDITY_MIN_COVERAGE_RATIO", "0.20"))

DEFAULT_ROOT = r"\\192.168.0.22\AutoStockBuyAndSell"


_SYMBOL_COL_CANDIDATES = (
    "symbol",
    "Symbol",
    "code",
    "Code",
    "銘柄コード",
)

_VOLUME_COL_CANDIDATES = (
    "trading_volume",
    "volume",
    "volume_1m",
    "出来高",
    "売買高",
)

_PRICE_COL_CANDIDATES = (
    "current_price",
    "close",
    "close_price",
    "price",
    "現在値",
)

_TABLE_HINTS = (
    "ranking_snapshot_1min",
    "ranking_raw_1min",
    "値上がり",
    "値下がり",
    "売買高",
    "売買代金",
    "TICK",
    "tick",
    "ranking",
)


_CACHE_TS: float = 0.0
_CACHE_DB_PATH: Optional[str] = None
_CACHE_MAP: Dict[str, Dict[str, float]] = {}


def _normalize_symbol(symbol: Any) -> Optional[str]:
    if symbol is None:
        return None

    s = str(symbol).strip().upper()

    if not s:
        return None

    if s.endswith(".T"):
        s = s[:-2]

    if s.endswith(".0"):
        s2 = s[:-2]
        if s2.isdigit():
            s = s2

    if not s or s in {"NONE", "NULL", "NAN", "NA", "-", "0"}:
        return None

    if not s.isalnum():
        return None

    if not (3 <= len(s) <= 5):
        return None

    return s


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _numeric_expr(col: str) -> str:
    q = _quote_ident(col)
    return f"CAST(REPLACE(REPLACE({q}, ',', ''), ' ', '') AS REAL)"


def _find_existing_ranking_db_path() -> Optional[str]:
    """
    rankingYYYYMMDD.db を探す。
    優先:
      1. PUSH_REGISTER_RANKING_DB_PATH
      2. RANKING_DB_PATH
      3. KABU_RANKING_DB_PATH
      4. \\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking\rankingYYYYMMDD.db
      5. 直近7日分
    """
    for env_name in (
        "PUSH_REGISTER_RANKING_DB_PATH",
        "RANKING_DB_PATH",
        "KABU_RANKING_DB_PATH",
    ):
        p = os.environ.get(env_name)
        if p and Path(p).exists():
            return str(Path(p))

    root = os.environ.get("AUTO_STOCK_ROOT") or os.environ.get("KABU_AUTO_STOCK_ROOT") or DEFAULT_ROOT
    ranking_dir = Path(root) / "raw_data" / "kabu_station" / "ranking"

    today = dt.datetime.now().date()

    for i in range(0, 8):
        d = today - dt.timedelta(days=i)
        p = ranking_dir / f"ranking{d:%Y%m%d}.db"
        if p.exists():
            return str(p)

    return None


def _table_names(conn: sqlite3.Connection) -> List[str]:
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception:
        logger.debug("[PUSH LIQUIDITY GUARD] failed to list tables", exc_info=True)
        return []


def _pragma_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({_quote_ident(table)})").fetchall()
        return [str(r[1]) for r in rows if len(r) >= 2]
    except Exception:
        logger.debug(
            "[PUSH LIQUIDITY GUARD] failed to pragma table=%s",
            table,
            exc_info=True,
        )
        return []


def _pick_col(cols: Sequence[str], candidates: Sequence[str]) -> Optional[str]:
    col_set = set(cols)
    for c in candidates:
        if c in col_set:
            return c
    return None


def _is_target_table(table: str) -> bool:
    t = str(table)
    return any(h in t for h in _TABLE_HINTS)


def _load_liquidity_map_from_db(db_path: str) -> Dict[str, Dict[str, float]]:
    """
    ranking DB 内の有効そうなテーブルから、
    symbol -> {volume, turnover, price} を作る。

    複数テーブルに同一銘柄があれば最大値を採用する。
    """
    out: Dict[str, Dict[str, float]] = {}

    if not db_path or not Path(db_path).exists():
        return out

    try:
        conn = sqlite3.connect(db_path, timeout=3.0)
    except Exception:
        logger.debug(
            "[PUSH LIQUIDITY GUARD] sqlite connect failed db=%s",
            db_path,
            exc_info=True,
        )
        return out

    try:
        tables = _table_names(conn)

        used_tables = 0

        for table in tables:
            if not _is_target_table(table):
                continue

            cols = _pragma_columns(conn, table)
            if not cols:
                continue

            symbol_col = _pick_col(cols, _SYMBOL_COL_CANDIDATES)
            volume_col = _pick_col(cols, _VOLUME_COL_CANDIDATES)
            price_col = _pick_col(cols, _PRICE_COL_CANDIDATES)

            if symbol_col is None or volume_col is None:
                continue

            used_tables += 1

            symbol_expr = _quote_ident(symbol_col)
            volume_expr = _numeric_expr(volume_col)

            if price_col is not None:
                price_expr = _numeric_expr(price_col)
                turnover_expr = f"({price_expr} * {volume_expr})"
                sql = f"""
                    SELECT
                        {symbol_expr} AS symbol,
                        MAX({volume_expr}) AS max_volume,
                        MAX({price_expr}) AS max_price,
                        MAX({turnover_expr}) AS max_turnover
                    FROM {_quote_ident(table)}
                    WHERE {symbol_expr} IS NOT NULL
                    GROUP BY {symbol_expr}
                """
            else:
                sql = f"""
                    SELECT
                        {symbol_expr} AS symbol,
                        MAX({volume_expr}) AS max_volume,
                        0.0 AS max_price,
                        0.0 AS max_turnover
                    FROM {_quote_ident(table)}
                    WHERE {symbol_expr} IS NOT NULL
                    GROUP BY {symbol_expr}
                """

            try:
                rows = conn.execute(sql).fetchall()
            except Exception:
                logger.debug(
                    "[PUSH LIQUIDITY GUARD] query failed table=%s symbol_col=%s volume_col=%s price_col=%s",
                    table,
                    symbol_col,
                    volume_col,
                    price_col,
                    exc_info=True,
                )
                continue

            for symbol, volume, price, turnover in rows:
                s = _normalize_symbol(symbol)
                if not s:
                    continue

                try:
                    v = float(volume or 0.0)
                except Exception:
                    v = 0.0

                try:
                    p = float(price or 0.0)
                except Exception:
                    p = 0.0

                try:
                    tv = float(turnover or 0.0)
                except Exception:
                    tv = 0.0

                prev = out.get(s)
                if prev is None:
                    out[s] = {
                        "volume": v,
                        "price": p,
                        "turnover": tv,
                    }
                else:
                    prev["volume"] = max(float(prev.get("volume") or 0.0), v)
                    prev["price"] = max(float(prev.get("price") or 0.0), p)
                    prev["turnover"] = max(float(prev.get("turnover") or 0.0), tv)

        logger.info(
            "[PUSH LIQUIDITY GUARD] liquidity map loaded db=%s tables=%d symbols=%d",
            db_path,
            used_tables,
            len(out),
        )

        return out

    except Exception:
        logger.exception(
            "[PUSH LIQUIDITY GUARD] load liquidity map failed db=%s",
            db_path,
        )
        return out

    finally:
        try:
            conn.close()
        except Exception:
            pass


def get_liquidity_map() -> Dict[str, Dict[str, float]]:
    """
    TTL付きで liquidity map を返す。
    """
    global _CACHE_TS, _CACHE_DB_PATH, _CACHE_MAP

    now = time.time()
    db_path = _find_existing_ranking_db_path()

    if not db_path:
        logger.warning(
            "[PUSH LIQUIDITY GUARD] ranking db not found -> pass-through"
        )
        return {}

    if (
        _CACHE_MAP
        and _CACHE_DB_PATH == db_path
        and now - _CACHE_TS <= CACHE_TTL_SEC
    ):
        return _CACHE_MAP

    m = _load_liquidity_map_from_db(db_path)

    _CACHE_TS = now
    _CACHE_DB_PATH = db_path
    _CACHE_MAP = m

    return m


def filter_register_targets_by_liquidity(
    symbols: Iterable[Any],
    *,
    source: str = "rotation",
    min_day_volume: float | None = None,
    min_day_turnover: float | None = None,
    min_coverage_ratio: float | None = None,
) -> List[str]:
    """
    PUSH登録対象から薄商い銘柄を除外する。

    DB取得率が低すぎる場合は、安全のため元リストを返す。
    """
    cleaned: List[str] = []
    seen: set[str] = set()

    for x in symbols or []:
        s = _normalize_symbol(x)
        if not s or s in seen:
            continue
        seen.add(s)
        cleaned.append(s)

    if not cleaned:
        return []

    if not ENABLED:
        logger.info(
            "[PUSH LIQUIDITY GUARD] disabled source=%s count=%d",
            source,
            len(cleaned),
        )
        return cleaned

    if min_day_volume is None:
        min_day_volume = MIN_DAY_VOLUME

    if min_day_turnover is None:
        min_day_turnover = MIN_DAY_TURNOVER

    if min_coverage_ratio is None:
        min_coverage_ratio = MIN_COVERAGE_RATIO

    liquidity_map = get_liquidity_map()

    if not liquidity_map:
        logger.warning(
            "[PUSH LIQUIDITY GUARD] pass-through source=%s reason=no_liquidity_map count=%d",
            source,
            len(cleaned),
        )
        return cleaned

    matched = [s for s in cleaned if s in liquidity_map]
    coverage = len(matched) / max(1, len(cleaned))

    if coverage < float(min_coverage_ratio):
        logger.warning(
            "[PUSH LIQUIDITY GUARD] pass-through source=%s reason=low_coverage "
            "count=%d matched=%d coverage=%.3f min_coverage=%.3f",
            source,
            len(cleaned),
            len(matched),
            coverage,
            float(min_coverage_ratio),
        )
        return cleaned

    kept: List[str] = []
    removed: List[Tuple[str, float, float]] = []
    missing: List[str] = []

    for s in cleaned:
        info = liquidity_map.get(s)

        if not info:
            missing.append(s)
            continue

        volume = float(info.get("volume") or 0.0)
        turnover = float(info.get("turnover") or 0.0)

        if volume >= float(min_day_volume) and turnover >= float(min_day_turnover):
            kept.append(s)
        else:
            removed.append((s, volume, turnover))

    logger.info(
        "[PUSH LIQUIDITY GUARD] source=%s before=%d after=%d removed=%d missing=%d "
        "matched=%d coverage=%.3f min_day_volume=%.0f min_day_turnover=%.0f "
        "removed_head=%s kept_head=%s",
        source,
        len(cleaned),
        len(kept),
        len(removed),
        len(missing),
        len(matched),
        coverage,
        float(min_day_volume),
        float(min_day_turnover),
        removed[:10],
        kept[:10],
    )

    return kept


__all__ = [
    "filter_register_targets_by_liquidity",
    "get_liquidity_map",
]