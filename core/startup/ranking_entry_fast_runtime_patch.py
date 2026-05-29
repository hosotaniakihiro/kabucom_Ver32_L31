# ============================================================
# File   : core/startup/ranking_entry_fast_runtime_patch.py
# Version: V2-RANKING-ENTRY-FAST-READONLY-TECHNICALS
# ------------------------------------------------------------
# 目的:
#   ランキング由来エントリー作成が80秒以上かかる問題を軽減する。
#
# 背景:
#   2026-05-29ログ:
#     [RANKING ENTRY LOOP] done ... raw_total=1358 prefiltered=184 ... elapsed=80.612s
#   その後のログ:
#     [RANKING ENTRY FAST PATCH] cap ... before=177 after=60
#     [RANKING ENTRY FAST PATCH] technical done ... elapsed=57.164s
#
# 主因:
#   entry実行時に ranking_technical_1min へ毎回保存し、履歴取得・計算・UPDATE を行うため、
#   NAS sqlite で非常に重くなる。
#
# 方針 V2:
#   - main.py(entry_only) 側では、ranking technical のDB保存をデフォルトでスキップする。
#   - 既存の ranking_technical_1min から最新technicalを読むだけにする。
#   - technical保存・再計算は main_database.py / data collector 側へ寄せる。
#   - 既存DBにtechnicalが無い銘柄は NO_TECH のまま後段ガードへ渡す。
#
# ENV:
#   RANKING_ENTRY_SKIP_TECH_SAVE=1            # default 1: entry側では保存しない
#   RANKING_ENTRY_TECH_READONLY=1             # default 1: 既存DBから読む
#   RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS=80
#   RANKING_ENTRY_FAST_MAX_SYMBOLS=60
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIG_LIGHT_PREFILTER = None
_ORIG_SAVE_TECH = None
_ORIG_LOAD_HISTORY = None

TECH_COLUMNS = [
    "ma5",
    "ma25",
    "ma75",
    "rsi",
    "macd",
    "signal",
    "macd_hist",
    "atr",
    "slope",
    "slope_atr_scaled",
    "vwap",
    "score_buy",
    "score_sell",
    "score_total",
    "ranking_tech_score",
    "ranking_tech_ready",
    "ranking_tech_reason",
]


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return int(default)
        return int(float(raw))
    except Exception:
        return int(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip().replace(",", "").replace("%", "")
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return default
        return float(s)
    except Exception:
        return default


def _safe_int(v: Any, default: int = 999999) -> int:
    try:
        return int(_safe_float(v, float(default)))
    except Exception:
        return default


def _side(row: Dict[str, Any]) -> str:
    s = str(row.get("side") or row.get("entry_decision") or "").upper().strip()
    if s in {"BUY", "SELL"}:
        return s
    rt = str(row.get("rank_type") or "")
    if "値下" in rt or "下落" in rt:
        return "SELL"
    return "BUY"


def _rank_type_weight(rt: str) -> float:
    s = str(rt or "")
    if "値上" in s or "値下" in s:
        return 1.20
    if "売買代金" in s:
        return 1.10
    if "TICK" in s.upper() or "ティック" in s:
        return 1.00
    if "出来高" in s or "売買高" in s:
        return 0.95
    return 0.80


def _row_priority(row: Dict[str, Any]) -> tuple:
    rank = _safe_int(row.get("rank_position") or row.get("rank"), 999999)
    turnover = _safe_float(row.get("turnover") or row.get("trading_value"), 0.0)
    volume = _safe_float(row.get("volume") or row.get("trading_volume"), 0.0)
    day = abs(_safe_float(row.get("day_change_pct"), 0.0))
    rt_w = _rank_type_weight(str(row.get("rank_type") or ""))
    return (rank, -rt_w, -turnover, -volume, -day)


def _cap_rows(rows: List[Dict[str, Any]], *, context: str) -> List[Dict[str, Any]]:
    if not _env_bool("RANKING_ENTRY_FAST_CAP_ENABLED", True):
        return rows
    max_rows = _env_int("RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS", 80)
    max_symbols = _env_int("RANKING_ENTRY_FAST_MAX_SYMBOLS", 60)
    max_per_side = _env_int("RANKING_ENTRY_FAST_MAX_PER_SIDE", 45)
    max_per_type = _env_int("RANKING_ENTRY_FAST_MAX_PER_TYPE", 18)
    if len(rows) <= max_rows:
        return rows

    ordered = sorted([dict(r) for r in rows], key=_row_priority)
    kept: List[Dict[str, Any]] = []
    seen_symbols: set[str] = set()
    seen_symbol_side: set[tuple[str, str]] = set()
    per_side = Counter()
    per_type = Counter()
    rejects = Counter()

    for row in ordered:
        symbol = str(row.get("symbol") or "").strip()
        side = _side(row)
        rt = str(row.get("rank_type") or "")
        if not symbol:
            rejects["NO_SYMBOL"] += 1
            continue
        if (symbol, side) in seen_symbol_side:
            rejects["DUP_SYMBOL_SIDE"] += 1
            continue
        if len(seen_symbols) >= max_symbols and symbol not in seen_symbols:
            rejects["SYMBOL_LIMIT"] += 1
            continue
        if per_side[side] >= max_per_side:
            rejects["SIDE_LIMIT"] += 1
            continue
        if per_type[rt] >= max_per_type:
            rejects["TYPE_LIMIT"] += 1
            continue
        kept.append(row)
        seen_symbols.add(symbol)
        seen_symbol_side.add((symbol, side))
        per_side[side] += 1
        per_type[rt] += 1
        if len(kept) >= max_rows:
            break

    logger.warning(
        "[RANKING ENTRY FAST PATCH] cap context=%s before=%s after=%s max_rows=%s max_symbols=%s per_side=%s per_type=%s rejects=%s",
        context,
        len(rows),
        len(kept),
        max_rows,
        max_symbols,
        dict(per_side),
        dict(per_type),
        dict(rejects),
    )
    return kept


def _patched_light_prefilter_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    base = _ORIG_LIGHT_PREFILTER(rows)
    return _cap_rows(base, context="after_light_prefilter")


def _resolve_ranking_db_path() -> str:
    try:
        from ats.ats_ranking.db_path import resolve_ranking_db_path
        p = resolve_ranking_db_path()
        if p:
            return str(p)
    except Exception:
        pass
    explicit = os.getenv("RANKING_TECH_DB_PATH") or os.getenv("RANKING_DB_PATH")
    if explicit:
        return explicit
    root = os.getenv("AUTOSTOCK_ROOT", r"\\192.168.0.22\AutoStockBuyAndSell")
    today = dt.datetime.now().strftime("%Y%m%d")
    return str(Path(root) / "raw_data" / "kabu_station" / "ranking" / f"ranking{today}.db")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        r = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return bool(r)
    except Exception:
        return False


def _latest_existing_technicals(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """ranking_technical_1min から対象symbolの最新technicalだけ読む。DB更新はしない。"""
    try:
        symbols = [str(r.get("symbol") or "").strip() for r in rows]
        symbols = [s for s in dict.fromkeys(symbols) if s]
        if not symbols:
            return {}
        db = _resolve_ranking_db_path()
        if not db or not os.path.exists(db):
            logger.warning("[RANKING ENTRY FAST PATCH] readonly tech skipped reason=db_missing db=%s symbols=%s", db, len(symbols))
            return {}

        table = "ranking_technical_1min"
        batch_size = max(20, _env_int("RANKING_ENTRY_TECH_READ_BATCH_SIZE", 80))
        chunks = []
        t0 = time.time()
        with sqlite3.connect(db, timeout=2.0) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=2000")
            if not _table_exists(conn, table):
                logger.warning("[RANKING ENTRY FAST PATCH] readonly tech skipped reason=table_missing db=%s table=%s", db, table)
                return {}
            for i in range(0, len(symbols), batch_size):
                batch = symbols[i:i + batch_size]
                placeholders = ",".join(["?"] * len(batch))
                # window関数が使えない古いsqliteも想定し、単純取得後にpandasで最新化する。
                q = f"""
                    SELECT * FROM {table}
                    WHERE symbol IN ({placeholders})
                    ORDER BY symbol ASC, datetime DESC
                """
                part = pd.read_sql_query(q, conn, params=tuple(batch))
                if not part.empty:
                    part = part.groupby("symbol", group_keys=False).head(1)
                    chunks.append(part)
        if not chunks:
            logger.warning("[RANKING ENTRY FAST PATCH] readonly tech empty symbols=%s db=%s elapsed=%.3fs", len(symbols), db, time.time() - t0)
            return {}
        df = pd.concat(chunks, ignore_index=True)
        if "datetime" in df.columns:
            df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
            df = df.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False).tail(1)
        result: Dict[str, Dict[str, Any]] = {}
        for _, r in df.iterrows():
            sym = str(r.get("symbol") or "").strip()
            if not sym:
                continue
            item = {c: r.get(c) for c in TECH_COLUMNS if c in r.index}
            item["ranking_tech_datetime"] = r.get("datetime")
            item["ranking_tech_db"] = db
            item["ranking_tech_readonly"] = True
            result[sym] = item
        logger.warning(
            "[RANKING ENTRY FAST PATCH] readonly tech loaded symbols=%s hit=%s db=%s elapsed=%.3fs",
            len(symbols),
            len(result),
            db,
            time.time() - t0,
        )
        return result
    except Exception:
        logger.exception("[RANKING ENTRY FAST PATCH] readonly tech load failed")
        return {}


def _patched_save_ranking_pseudo_technicals(rows: List[Dict[str, Any]], *args: Any, **kwargs: Any) -> Dict[str, Dict[str, Any]]:
    if not _env_bool("RANKING_ENTRY_FAST_TECH_CAP_ENABLED", True):
        return _ORIG_SAVE_TECH(rows, *args, **kwargs)

    capped = _cap_rows(rows, context="before_save_technical")

    # main.py(entry_only) ではDB保存・再計算をデフォルトで行わない。
    if _env_bool("RANKING_ENTRY_SKIP_TECH_SAVE", True):
        t0 = time.time()
        ret = _latest_existing_technicals(capped) if _env_bool("RANKING_ENTRY_TECH_READONLY", True) else {}
        logger.warning(
            "[RANKING ENTRY FAST PATCH] technical save skipped rows %s->%s readonly_hit=%s elapsed=%.3fs",
            len(rows),
            len(capped),
            len(ret or {}),
            time.time() - t0,
        )
        return ret

    lookback = _env_int("RANKING_ENTRY_FAST_TECH_LOOKBACK_ROWS", 60)
    kwargs.setdefault("lookback_rows", lookback)
    t0 = time.time()
    ret = _ORIG_SAVE_TECH(capped, *args, **kwargs)
    logger.warning(
        "[RANKING ENTRY FAST PATCH] technical done rows %s->%s latest=%s elapsed=%.3fs lookback=%s",
        len(rows),
        len(capped),
        len(ret or {}),
        time.time() - t0,
        kwargs.get("lookback_rows"),
    )
    return ret


def _patched_load_history(conn: Any, symbols: List[str], lookback_rows: int = 120) -> pd.DataFrame:
    """symbolごとのSQL発行をやめ、IN句でまとめて取得する。"""
    try:
        import trading.ranking.ranking_technical_store as store

        if not symbols:
            return pd.DataFrame()
        symbols = [str(s) for s in dict.fromkeys(symbols) if str(s).strip()]
        if not symbols:
            return pd.DataFrame()
        batch_size = max(20, _env_int("RANKING_ENTRY_FAST_HISTORY_BATCH_SIZE", 80))
        chunks = []
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            placeholders = ",".join(["?"] * len(batch))
            q = f"""
                SELECT * FROM {store.TABLE_NAME}
                WHERE symbol IN ({placeholders})
                ORDER BY symbol ASC, datetime DESC
            """
            part = pd.read_sql_query(q, conn, params=tuple(batch))
            if not part.empty:
                part = part.groupby("symbol", group_keys=False).head(int(lookback_rows))
                chunks.append(part)
        if not chunks:
            return pd.DataFrame()
        df = pd.concat(chunks, ignore_index=True)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        return df.dropna(subset=["datetime"]).sort_values(["symbol", "datetime"], kind="stable")
    except Exception:
        logger.exception("[RANKING ENTRY FAST PATCH] batch load_history failed -> original")
        return _ORIG_LOAD_HISTORY(conn, symbols, lookback_rows=lookback_rows)


def install() -> bool:
    global _PATCHED, _ORIG_LIGHT_PREFILTER, _ORIG_SAVE_TECH, _ORIG_LOAD_HISTORY
    if _PATCHED:
        return True
    try:
        import trading.ranking.entry_from_ranking as efr
        import trading.ranking.ranking_technical_store as store

        patched = []
        cur_pf = getattr(efr, "_light_prefilter_rows", None)
        if callable(cur_pf) and not getattr(cur_pf, "_ranking_entry_fast_patch_v2", False):
            _ORIG_LIGHT_PREFILTER = cur_pf
            _patched_light_prefilter_rows._ranking_entry_fast_patch_v2 = True  # type: ignore[attr-defined]
            efr._light_prefilter_rows = _patched_light_prefilter_rows
            patched.append("entry_from_ranking._light_prefilter_rows")

        cur_save = getattr(efr, "save_ranking_pseudo_technicals", None)
        if callable(cur_save) and not getattr(cur_save, "_ranking_entry_fast_patch_v2", False):
            _ORIG_SAVE_TECH = cur_save
            _patched_save_ranking_pseudo_technicals._ranking_entry_fast_patch_v2 = True  # type: ignore[attr-defined]
            efr.save_ranking_pseudo_technicals = _patched_save_ranking_pseudo_technicals
            store.save_ranking_pseudo_technicals = _patched_save_ranking_pseudo_technicals
            patched.append("save_ranking_pseudo_technicals_readonly")

        cur_load = getattr(store, "_load_history", None)
        if callable(cur_load) and not getattr(cur_load, "_ranking_entry_fast_patch_v2", False):
            _ORIG_LOAD_HISTORY = cur_load
            _patched_load_history._ranking_entry_fast_patch_v2 = True  # type: ignore[attr-defined]
            store._load_history = _patched_load_history
            patched.append("ranking_technical_store._load_history")

        _PATCHED = True
        logger.warning(
            "[RANKING ENTRY FAST PATCH] installed V2 patched=%s max_rows=%s max_symbols=%s skip_save=%s readonly=%s",
            patched,
            _env_int("RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS", 80),
            _env_int("RANKING_ENTRY_FAST_MAX_SYMBOLS", 60),
            _env_bool("RANKING_ENTRY_SKIP_TECH_SAVE", True),
            _env_bool("RANKING_ENTRY_TECH_READONLY", True),
        )
        return True
    except Exception:
        logger.exception("[RANKING ENTRY FAST PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY FAST PATCH] auto install failed")


__all__ = ["install"]
