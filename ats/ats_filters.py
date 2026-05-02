# ============================================================
# File   : ats_filters.py
# Version: Ver1.5-PRODUCTION-FILTER-MODULE-SYMBOLFLAGSDB-DIRECT-ABSOLUTE-STABLE
# ------------------------------------------------------------
# ✔ Ver1.4 の機能完全保持（削除ゼロ）
# ✔ etf_guard 完全保持
# ✔ market_filter 統合（最重要）
# ✔ utils.market_filter を唯一の市場判定基準に統一
# ✔ プライム / スタンダード / グロースのみ許可
# ✔ ETF / ETN / REIT / INDEX 完全防御
# ✔ symbol_flags フィルター整合化
# ✔ positions.db 参照問題を解消
# ✔ symbol_flags_db を直接参照
# ✔ is_etf 補正維持
# ✔ ats_ok フィルター
# ✔ push_ok フィルター
# ✔ 低流動性除外
# ✔ SQLite NULL 安全
# ✔ summary_cache 連携
# ✔ 高速化（apply排除）
# ✔ 本番例外耐性
# ✔ UnboundLocalError 完全解消
# ✔ symbol_flags 診断ログ安全化
# ✔ DB不整合時フェイルセーフ維持
# ✔ 起動初期 symbol_flags 0件時フェイルオープン
# ✔ symbol形式ゆれ吸収（.0除去）
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from typing import Iterable, List, Set

from sqlalchemy import inspect

from database import Session_position
from database.models import SymbolFlags
from global_state import global_data
from config.paths import get_path

# ★ 市場判定はこれに統一
from utils.market_filter import get_tradeable_symbols


logger = logging.getLogger(__name__)


# ============================================================
# ETF prefix（高速判定）
# ============================================================

ETF_CODE_PREFIX = ("13", "15", "16", "17", "25")


# ============================================================
# 流動性条件
# ============================================================

MIN_TURNOVER = 20_000_000
MIN_VOLUME = 30000


# ============================================================
# 起動時フェイルオープン設定
# ============================================================

# True:
#   symbol_flags が 0件一致 / テーブルなし / DB不整合でも
#   「市場フィルタ通過銘柄」はそのまま通す
# False:
#   旧挙動寄り（symbol_flags で0件なら全落ち）
STARTUP_FAIL_OPEN_SYMBOL_FLAGS = True

# symbol_flags テーブルが存在しない時も通す
BYPASS_IF_SYMBOL_FLAGS_TABLE_MISSING = True

# symbol_flags 照合 0件時も通す
BYPASS_IF_SYMBOL_FLAGS_MATCHED_NONE = True


# ============================================================
# 内部ユーティリティ
# ============================================================

def _has_column(model, col: str) -> bool:
    try:
        mapper = inspect(model)
        return col in mapper.columns
    except Exception:
        return False


def _normalize_symbol(s) -> str:
    try:
        s = str(s).strip()
    except Exception:
        return ""

    if not s:
        return ""

    # "7203.0" のような崩れを救済
    if s.endswith(".0"):
        s = s[:-2].strip()

    return s


def _normalize_symbols(symbols: Iterable[str]) -> List[str]:
    if not symbols:
        return []

    out: List[str] = []

    for s in symbols:
        s = _normalize_symbol(s)
        if not s:
            continue
        out.append(s)

    # 順序維持重複除去
    return list(dict.fromkeys(out))


def _safe_close_session(session) -> None:
    try:
        if session is not None:
            session.close()
    except Exception:
        pass


def _safe_get_summary_1min():
    try:
        summary_cache = getattr(global_data, "summary_cache", None)
        if isinstance(summary_cache, dict):
            return summary_cache.get("1min")
    except Exception:
        pass
    return None


def _should_fail_open_symbol_flags() -> bool:
    """
    起動初期や symbol_flags 未整備時に ATS 登録を止めない。
    """
    try:
        if STARTUP_FAIL_OPEN_SYMBOL_FLAGS:
            return True
    except Exception:
        pass
    return False


def _get_symbol_flags_db_path() -> str:
    try:
        path = get_path("symbol_flags_db")
        return str(path)
    except Exception:
        logger.exception("[ATSFilters] get_path(symbol_flags_db) failed")
        return ""


def _sqlite_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table_name,),
        ).fetchone()
        return bool(row)
    except Exception:
        return False


def _sqlite_columns(conn: sqlite3.Connection, table_name: str) -> List[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return [str(r[1]) for r in rows if len(r) > 1]
    except Exception:
        return []


def _fetch_symbol_flags_allowed_symbols(symbols: List[str]) -> List[str]:
    """
    symbol_flags_db を直接参照して、存在する銘柄と is_etf 補正済み銘柄を返す。
    """
    if not symbols:
        return []

    db_path = _get_symbol_flags_db_path()
    if not db_path:
        logger.warning("[ATSFilters] symbol_flags_db path empty")
        return []

    logger.warning("[ATSFilters] symbol_flags_db=%s", db_path)

    try:
        with sqlite3.connect(db_path) as conn:
            if not _sqlite_table_exists(conn, "symbol_flags"):
                logger.warning("[ATSFilters] symbol_flags table not found in symbol_flags_db")
                return []

            cols = _sqlite_columns(conn, "symbol_flags")
            has_is_etf = "is_etf" in cols

            placeholders = ",".join("?" for _ in symbols)
            sql = f"""
                SELECT symbol
                FROM symbol_flags
                WHERE symbol IN ({placeholders})
            """

            if has_is_etf:
                sql += """
                    AND (is_etf = 0 OR is_etf IS NULL)
                """

            rows = conn.execute(sql, tuple(symbols)).fetchall()

            logger.warning(
                "[ATSFilters] db matched rows=%s sample=%s",
                len(rows),
                rows[:10] if rows else [],
            )

            out = []
            for r in rows:
                try:
                    if not r:
                        continue
                    out.append(_normalize_symbol(r[0]))
                except Exception:
                    continue

            return _normalize_symbols(out)

    except Exception:
        logger.exception("[ATSFilters] symbol_flags_db query failed")
        return []


# ============================================================
# ETF完全防御（高速版）
# ============================================================

def filter_etf_guard(symbols: List[str]) -> List[str]:
    if not symbols:
        return []

    try:
        symbols = _normalize_symbols(symbols)

        filtered = [
            s for s in symbols
            if not s.startswith(ETF_CODE_PREFIX)
        ]

        removed = len(symbols) - len(filtered)
        if removed > 0:
            logger.info("[ATSFilters] ETF removed=%s", removed)

        return filtered

    except Exception:
        logger.exception("ETF filter failed")
        return _normalize_symbols(symbols)


# ============================================================
# market_filter（プライム/スタンダード/グロース）
# ============================================================

def filter_market_symbols(symbols: List[str]) -> List[str]:
    if not symbols:
        return []

    try:
        symbols = _normalize_symbols(symbols)
        valid = set(_normalize_symbols(get_tradeable_symbols()))

        # DB障害時は既存挙動互換で素通し
        if not valid:
            logger.warning("[ATSFilters] market filter empty -> bypass")
            return symbols

        filtered = [s for s in symbols if s in valid]

        removed = len(symbols) - len(filtered)
        if removed > 0:
            logger.info("[ATSFilters] market removed=%s", removed)

        return filtered

    except Exception:
        logger.exception("market filter failed")
        return _normalize_symbols(symbols)


# ============================================================
# symbol_flags guard
# ============================================================

def filter_symbol_flags(symbols: List[str]) -> List[str]:
    """
    本番では utils.market_filter を唯一の市場判定基準に統一しつつ、
    symbol_flags_db を直接参照して存在確認 / is_etf 補正を行う。

    役割:
    - market_type は utils.market_filter 側に一元化
    - symbol_flags_db に存在しない銘柄は除外（ただし起動時はfail-open可）
    - is_etf カラムがあれば is_etf=1 も除外
    """

    if not symbols:
        return []

    try:
        symbols = _normalize_symbols(symbols)
        if not symbols:
            return []

        # ★ 市場基準は utils.market_filter に統一
        tradeable = set(_normalize_symbols(get_tradeable_symbols()))

        logger.warning("[ATSFilters] input count=%s head=%s", len(symbols), symbols[:20])
        logger.warning("[ATSFilters] tradeable count=%s", len(tradeable))

        if not tradeable:
            logger.warning("[ATSFilters] tradeable symbols empty -> bypass")
            return symbols

        # まず市場基準で絞る
        symbols = [s for s in symbols if s in tradeable]

        logger.warning("[ATSFilters] after market count=%s head=%s", len(symbols), symbols[:20])

        if not symbols:
            logger.info("[ATSFilters] symbol_flags removed all by market filter")
            return []

        allowed = _fetch_symbol_flags_allowed_symbols(symbols)

        if not allowed:
            logger.warning("[ATSFilters] symbol_flags matched none")

            if BYPASS_IF_SYMBOL_FLAGS_MATCHED_NONE or _should_fail_open_symbol_flags():
                logger.warning("[ATSFilters] fail-open: matched none -> bypass market-filtered symbols")
                return symbols

            return []

        allowed_set: Set[str] = set(_normalize_symbols(allowed))
        allowed_set.discard("")

        if not allowed_set:
            logger.warning("[ATSFilters] allowed empty after normalize")
            if _should_fail_open_symbol_flags():
                logger.warning("[ATSFilters] fail-open: allowed empty -> bypass market-filtered symbols")
                return symbols
            return []

        filtered = [s for s in symbols if s in allowed_set]

        # ここも起動初期は全落ちさせない
        if not filtered:
            logger.warning("[ATSFilters] symbol_flags filtered empty after allowed intersect")
            if _should_fail_open_symbol_flags():
                logger.warning("[ATSFilters] fail-open: filtered empty -> bypass market-filtered symbols")
                return symbols
            return []

        removed = len(symbols) - len(filtered)
        if removed > 0:
            logger.info("[ATSFilters] symbol_flags removed=%s", removed)

        return filtered

    except Exception:
        logger.exception("symbol_flags filter failed")

        # 本番では filter 壊れで全登録停止しないようフェイルセーフ
        if _should_fail_open_symbol_flags():
            logger.warning("[ATSFilters] fail-open: exception -> bypass input symbols")
            return _normalize_symbols(symbols)

        return _normalize_symbols(symbols)


# ============================================================
# ATS許可銘柄
# ============================================================

def filter_ats_ok(symbols: List[str]) -> List[str]:
    if not symbols:
        return []

    session = None

    try:
        session = Session_position()
        symbols = _normalize_symbols(symbols)

        if not _has_column(SymbolFlags, "ats_ok"):
            return symbols

        rows = (
            session.query(SymbolFlags.symbol)
            .filter(
                SymbolFlags.symbol.in_(symbols),
                SymbolFlags.ats_ok == 1,
            )
            .all()
        )

        enabled = {
            _normalize_symbol(getattr(r, "symbol", None))
            for r in rows
            if getattr(r, "symbol", None) is not None
        }
        enabled.discard("")

        # 旧版互換: 全部落ちたら bypass
        if not enabled:
            return symbols

        return [s for s in symbols if s in enabled]

    except Exception:
        logger.exception("filter_ats_ok failed")
        return _normalize_symbols(symbols)

    finally:
        _safe_close_session(session)


# ============================================================
# push許可銘柄
# ============================================================

def filter_push_ok(symbols: List[str]) -> List[str]:
    if not symbols:
        return []

    session = None

    try:
        session = Session_position()
        symbols = _normalize_symbols(symbols)

        q = session.query(SymbolFlags.symbol).filter(
            SymbolFlags.symbol.in_(symbols)
        )

        if _has_column(SymbolFlags, "push_ok"):
            rows = q.filter(SymbolFlags.push_ok == 1).all()
        else:
            rows = q.all()

        out = [
            _normalize_symbol(getattr(r, "symbol", None))
            for r in rows
            if getattr(r, "symbol", None) is not None
        ]
        out = [s for s in out if s]

        # 全落ち時は既存互換で bypass
        if not out:
            return symbols

        return out

    except Exception:
        logger.exception("filter_push_ok failed")
        return _normalize_symbols(symbols)

    finally:
        _safe_close_session(session)


# ============================================================
# 低流動性除外
# ============================================================

def filter_low_liquidity(symbols: List[str]) -> List[str]:
    summary = _safe_get_summary_1min()

    if summary is None or summary.empty:
        return _normalize_symbols(symbols)

    df = summary.copy()

    if "turnover" not in df.columns:
        return _normalize_symbols(symbols)

    if "volume" not in df.columns:
        df["volume"] = 0

    if "symbol" not in df.columns:
        return _normalize_symbols(symbols)

    try:
        df["turnover"] = df["turnover"].astype(float).fillna(0)
        df["volume"] = df["volume"].astype(float).fillna(0)
        df["symbol"] = df["symbol"].astype(str).map(_normalize_symbol)

        ok = set(
            df.loc[
                (df["turnover"] >= MIN_TURNOVER)
                & (df["volume"] >= MIN_VOLUME),
                "symbol",
            ].astype(str)
        )
        ok = {_normalize_symbol(s) for s in ok if _normalize_symbol(s)}

        symbols = _normalize_symbols(symbols)
        filtered = [s for s in symbols if s in ok]

        # 旧版互換: 全落ち時は bypass
        if not filtered:
            logger.warning("[ATSFilters] liquidity removed all -> bypass")
            return symbols

        removed = len(symbols) - len(filtered)
        if removed > 0:
            logger.info("[ATSFilters] liquidity removed=%s", removed)

        return filtered

    except Exception:
        logger.exception("filter_low_liquidity error")
        return _normalize_symbols(symbols)


# ============================================================
# フィルター統合（最重要）
# ============================================================

def apply_all_filters(symbols: List[str]) -> List[str]:
    if not symbols:
        return []

    try:
        symbols = _normalize_symbols(symbols)

        # ★ 順序重要（高速＋精度）
        symbols = filter_etf_guard(symbols)      # ETF prefix除外
        symbols = filter_market_symbols(symbols) # 市場限定（プライム/スタンダード/グロース）
        symbols = filter_symbol_flags(symbols)   # symbol_flags_db存在/is_etf補正
        symbols = filter_low_liquidity(symbols)  # 流動性

        return _normalize_symbols(symbols)

    except Exception:
        logger.exception("apply_all_filters failed")
        return _normalize_symbols(symbols)