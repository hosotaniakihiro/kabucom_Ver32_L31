# ============================================================
# File   : ats/ats_ranking_source.py
# Version: Ver1.0-ATS-RANKING-SOURCE
# ------------------------------------------------------------
# ranking DB / symbol_flags から ATS対象を構築
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
import threading as _threading
import time as _time
from typing import List, Optional

import pandas as pd

from config.paths import get_path
from global_state import global_data
from .ats_register_state import ATS_BATCH_SIZE, sanitize_symbols
from sqlite_cm import committing_connect
from nas_root import nas_path, nas_root

logger = logging.getLogger(__name__)

RANKING_DB_BASE_DIRS = [
    nas_path(r"raw_data\kabu_station\ranking"),
    nas_path(r"raw_data\kabu_station\Ranking"),
]

SYMBOL_FLAGS_DB = nas_path(r"Basic\symbol_flags.db")
_ALLOWED_MARKET_TYPES = {"プライム", "スタンダード", "グロース"}


def ranking_db_candidates(today_ymd: str) -> List[str]:
    out = []
    for base in RANKING_DB_BASE_DIRS:
        out.append(os.path.join(base, f"ranking{today_ymd}.db"))
    return out


def detect_existing_table(conn: sqlite3.Connection, candidates: List[str]) -> Optional[str]:
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        names = {str(r[0]) for r in rows}
        for c in candidates:
            if c in names:
                return c
    except Exception:
        logger.exception("[ATS RANKING ONLY] detect table failed")
    return None


def load_ranking_symbols(today_ymd: str, limit: int = ATS_BATCH_SIZE) -> List[str]:
    table_candidates = ["ranking_snapshot_1min", "ranking_raw_1min"]

    for path in ranking_db_candidates(today_ymd):
        if not os.path.exists(path):
            logger.info("[ATS RANKING ONLY] ranking db not found path=%s", path)
            continue

        try:
            with committing_connect(path, timeout=30) as conn:
                table = detect_existing_table(conn, table_candidates)
                if not table:
                    logger.warning("[ATS RANKING ONLY] ranking table not found path=%s", path)
                    continue

                cols = pd.read_sql_query(f"PRAGMA table_info({table})", conn)
                col_names = set(cols["name"].astype(str).tolist()) if not cols.empty else set()

                time_col = ""
                for c in ("snapshot_time", "created_at", "datetime"):
                    if c in col_names:
                        time_col = c
                        break

                if time_col:
                    q = f"""
                        SELECT symbol
                        FROM {table}
                        WHERE symbol IS NOT NULL
                          AND TRIM(symbol) <> ''
                        ORDER BY {time_col} DESC
                    """
                else:
                    q = f"""
                        SELECT symbol
                        FROM {table}
                        WHERE symbol IS NOT NULL
                          AND TRIM(symbol) <> ''
                    """

                df = pd.read_sql_query(q, conn)

            if df.empty or "symbol" not in df.columns:
                logger.warning("[ATS RANKING ONLY] ranking empty path=%s table=%s", path, table)
                continue

            symbols = sanitize_symbols(df["symbol"].tolist())
            if symbols:
                logger.info(
                    "[ATS RANKING ONLY] ranking symbols loaded path=%s table=%s count=%d",
                    path, table, len(symbols)
                )
                return symbols[:limit]

        except Exception:
            logger.exception("[ATS RANKING ONLY] ranking read failed path=%s", path)

    return []


# P0-7: 直近で読めた許可銘柄集合 (last-good)。
# DB が一時的に読めないときに全銘柄を通してしまわないために保持する。
_SYMBOL_FLAGS_LASTGOOD: dict = {"allowed": None, "ts": 0.0}
_SYMBOL_FLAGS_LOCK = _threading.Lock()


def _symbol_flags_fail_open() -> bool:
    v = os.getenv("ATS_SYMBOL_FLAGS_FAIL_OPEN")
    if v is None or str(v).strip() == "":
        return False
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _symbol_flags_lastgood_max_sec() -> float:
    try:
        return float(os.getenv("ATS_SYMBOL_FLAGS_LASTGOOD_MAX_SEC", "3600"))
    except Exception:
        return 3600.0


def _symbol_flags_degraded(symbols: List[str], reason: str) -> List[str]:
    """DB が使えないときの縮退。fail-open しない。

    last-good があればそれで絞る。無ければ fail-closed ([])。
    """
    if _symbol_flags_fail_open():
        logger.warning(
            "[ATS RANKING ONLY] symbol_flags 障害 reason=%s だが "
            "ATS_SYMBOL_FLAGS_FAIL_OPEN=1 のため全銘柄を通す count=%d",
            reason, len(symbols),
        )
        return symbols

    with _SYMBOL_FLAGS_LOCK:
        allowed = _SYMBOL_FLAGS_LASTGOOD["allowed"]
        ts = _SYMBOL_FLAGS_LASTGOOD["ts"]
    age = (_time.time() - ts) if ts else None
    max_age = _symbol_flags_lastgood_max_sec()

    if allowed and age is not None and age <= max_age:
        out = [s for s in symbols if s in allowed]
        logger.warning(
            "[ATS RANKING ONLY] symbol_flags 障害 reason=%s -> last-good で絞る "
            "age=%.0fs allowed=%d before=%d after=%d",
            reason, age, len(allowed), len(symbols), len(out),
        )
        return out

    # last-good が無い/古すぎる -> fail-closed
    logger.error(
        "[ATS RANKING ONLY] symbol_flags 障害 reason=%s かつ last-good なし"
        "(age=%s max=%.0fs) -> fail-closed で 0 件にする "
        "(DB 障害を理由に全銘柄を許可しない)",
        reason, "None" if age is None else "%.0fs" % age, max_age,
    )
    return []


def filter_symbols_by_symbol_flags(symbols: List[str]) -> List[str]:
    symbols = sanitize_symbols(symbols)
    if not symbols:
        return []

    if not os.path.exists(SYMBOL_FLAGS_DB):
        return _symbol_flags_degraded(symbols, "db_not_found:%s" % SYMBOL_FLAGS_DB)

    try:
        # `with sqlite3.connect(...)` は close しないためリークになる。
        # reader なので read-only で開き、空 DB も作らない。
        from database.sqlite_util import readonly_connect
        with readonly_connect(SYMBOL_FLAGS_DB, timeout=30, busy_timeout_ms=5000) as conn:
            df = pd.read_sql_query(
                """
                SELECT
                    symbol,
                    symbolname,
                    market_type,
                    is_etf,
                    buy_target,
                    sell_target,
                    credit_type
                FROM symbol_flags
                """,
                conn,
            )

        if df.empty or "symbol" not in df.columns:
            return _symbol_flags_degraded(symbols, "query_empty")

        df["symbol"] = df["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        df = df[df["symbol"] != ""].copy()

        if "market_type" in df.columns:
            df = df[df["market_type"].isin(list(_ALLOWED_MARKET_TYPES))].copy()

        if "is_etf" in df.columns:
            try:
                df = df[df["is_etf"].fillna(0).astype(int) != 1].copy()
            except Exception:
                pass

        allowed = set(df["symbol"].tolist())
        out = [s for s in symbols if s in allowed]

        # 読めたので last-good を更新する
        if allowed:
            with _SYMBOL_FLAGS_LOCK:
                _SYMBOL_FLAGS_LASTGOOD["allowed"] = allowed
                _SYMBOL_FLAGS_LASTGOOD["ts"] = _time.time()

        logger.info(
            "[ATS RANKING ONLY] symbol_flags filtered before=%d after=%d allowed=%d",
            len(symbols), len(out), len(allowed)
        )
        return out

    except FileNotFoundError as e:
        return _symbol_flags_degraded(symbols, "db_missing:%s" % e)
    except Exception as e:
        logger.exception("[ATS RANKING ONLY] symbol_flags filter failed")
        return _symbol_flags_degraded(symbols, "query_failed:%r" % (e,))


def resolve_ranking_only_targets(today_ymd: str, limit: int = ATS_BATCH_SIZE) -> List[str]:
    symbols = load_ranking_symbols(today_ymd, limit=max(limit * 3, limit))
    symbols = filter_symbols_by_symbol_flags(symbols)
    symbols = sanitize_symbols(symbols)[:limit]

    try:
        global_data.ats_register_targets = symbols
        global_data.ats_targets = symbols
        global_data.should_register_symbols = symbols
        global_data.push_symbols = symbols
    except Exception:
        logger.debug("[ATS RANKING ONLY] global_data reflect failed", exc_info=True)

    logger.info("[ATS RANKING ONLY] final targets count=%d", len(symbols))
    return symbols