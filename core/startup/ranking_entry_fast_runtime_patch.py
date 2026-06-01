# ============================================================
# File   : core/startup/ranking_entry_fast_runtime_patch.py
# Version: V4-RANKING-ENTRY-RUNTIME-BUDGET
# ------------------------------------------------------------
# 目的:
#   ランキング由来エントリー作成が90秒timeoutして古いworker threadが
#   残る問題を軽減する。
#
# V4:
#   - entry_from_ranking.entry_from_ranking() 自体をbudget付き軽量版へ差し替え。
#   - 外側timeout任せにせず、本体が自分で早期終了する。
#   - prefilter後の重いtechnical対象を既定40件に制限。
#   - pending追加は既定5件で即終了。
#   - technical readonly DB読込も短いtimeout・memory cacheで行う。
#
# ENV:
#   RANKING_ENTRY_RUNTIME_BUDGET_SEC=25
#   RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS=40
#   RANKING_ENTRY_FAST_MAX_SYMBOLS=40
#   RANKING_ENTRY_MAX_PENDING_PER_RUN=5
#   RANKING_ENTRY_SKIP_TECH_SAVE=1
#   RANKING_ENTRY_TECH_READONLY=1
#   RANKING_ENTRY_TECH_MEMORY_CACHE=1
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIG_LIGHT_PREFILTER = None
_ORIG_SAVE_TECH = None
_ORIG_LOAD_HISTORY = None
_ORIG_ENTRY_FROM_RANKING = None
_ORIG_RUN_PIPELINE = None

# db_path -> {"ts": float, "mtime": float, "items": dict[symbol, technical]}
_TECH_MEMORY_CACHE: dict[str, dict[str, Any]] = {}

# 起動時に他patchより先に効かせるため、既定値をここで強める。
os.environ.setdefault("RANKING_ENTRY_RUNTIME_BUDGET_SEC", "25")
os.environ.setdefault("RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS", "40")
os.environ.setdefault("RANKING_ENTRY_FAST_MAX_SYMBOLS", "40")
os.environ.setdefault("RANKING_ENTRY_FAST_MAX_PER_SIDE", "25")
os.environ.setdefault("RANKING_ENTRY_FAST_MAX_PER_TYPE", "12")
os.environ.setdefault("RANKING_ENTRY_MAX_PENDING_PER_RUN", "5")
os.environ.setdefault("RANKING_ENTRY_SKIP_TECH_SAVE", "1")
os.environ.setdefault("RANKING_ENTRY_TECH_READONLY", "1")
os.environ.setdefault("RANKING_ENTRY_TECH_MEMORY_CACHE", "1")
os.environ.setdefault("RANKING_ENTRY_TECH_CACHE_TTL_SEC", "90")
os.environ.setdefault("RANKING_ENTRY_TECH_READ_BATCH_SIZE", "40")

TECH_COLUMNS = [
    "ma5", "ma25", "ma75", "rsi", "macd", "signal", "macd_hist", "atr",
    "slope", "slope_atr_scaled", "vwap", "score_buy", "score_sell", "score_total",
    "ranking_tech_score", "ranking_tech_ready", "ranking_tech_reason",
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


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(raw)
    except Exception:
        return float(default)


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
    score = abs(_safe_float(row.get("score") or row.get("score_total") or row.get("ranking_only_score"), 0.0))
    rt_w = _rank_type_weight(str(row.get("rank_type") or ""))
    return (rank, -score, -rt_w, -turnover, -volume, -day)


def _cap_rows(rows: List[Dict[str, Any]], *, context: str) -> List[Dict[str, Any]]:
    if not _env_bool("RANKING_ENTRY_FAST_CAP_ENABLED", True):
        return rows
    max_rows = max(5, _env_int("RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS", 40))
    max_symbols = max(5, _env_int("RANKING_ENTRY_FAST_MAX_SYMBOLS", 40))
    max_per_side = max(3, _env_int("RANKING_ENTRY_FAST_MAX_PER_SIDE", 25))
    max_per_type = max(3, _env_int("RANKING_ENTRY_FAST_MAX_PER_TYPE", 12))
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
        context, len(rows), len(kept), max_rows, max_symbols, dict(per_side), dict(per_type), dict(rejects),
    )
    return kept


def _patched_light_prefilter_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    base = _ORIG_LIGHT_PREFILTER(rows)
    return _cap_rows(base, context="after_light_prefilter")


def _resolve_ranking_db_path() -> str:
    try:
        from ats.ats_ranking.db_path import get_usable_ranking_db_path
        p = get_usable_ranking_db_path(force_refresh=False, allow_fallback=False, prefer_today_even_if_empty=True)
        if p:
            return str(p)
    except Exception:
        pass
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


def _db_mtime(path: str) -> float:
    try:
        return float(os.path.getmtime(path))
    except Exception:
        return 0.0


def _cache_get(db: str, symbols: list[str]) -> tuple[Dict[str, Dict[str, Any]], list[str], str]:
    if not _env_bool("RANKING_ENTRY_TECH_MEMORY_CACHE", True):
        return {}, symbols, "disabled"
    now = time.time()
    ttl = _env_float("RANKING_ENTRY_TECH_CACHE_TTL_SEC", 90.0)
    mtime = _db_mtime(db)
    cache = _TECH_MEMORY_CACHE.get(db)
    if not cache:
        return {}, symbols, "empty"
    if now - float(cache.get("ts") or 0.0) > ttl:
        return {}, symbols, "ttl_expired"
    if float(cache.get("mtime") or 0.0) != mtime:
        return {}, symbols, "db_mtime_changed"
    items = cache.get("items") or {}
    if not isinstance(items, dict):
        return {}, symbols, "invalid"
    hits = {s: dict(items[s]) for s in symbols if s in items}
    missing = [s for s in symbols if s not in hits]
    return hits, missing, "hit" if not missing else "partial"


def _cache_put(db: str, items: Dict[str, Dict[str, Any]]) -> None:
    if not _env_bool("RANKING_ENTRY_TECH_MEMORY_CACHE", True):
        return
    try:
        now = time.time()
        mtime = _db_mtime(db)
        cur = _TECH_MEMORY_CACHE.get(db)
        if not cur or float(cur.get("mtime") or 0.0) != mtime:
            cur = {"ts": now, "mtime": mtime, "items": {}}
        cur_items = cur.setdefault("items", {})
        if isinstance(cur_items, dict):
            cur_items.update({str(k): dict(v) for k, v in (items or {}).items()})
        cur["ts"] = now
        cur["mtime"] = mtime
        _TECH_MEMORY_CACHE[db] = cur
    except Exception:
        logger.debug("[RANKING ENTRY FAST PATCH] cache put failed", exc_info=True)


def _rows_to_tech_map(df: pd.DataFrame, db: str) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    if df is None or df.empty:
        return result
    if "datetime" in df.columns:
        df = df.copy()
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False).tail(1)
    for _, r in df.iterrows():
        sym = str(r.get("symbol") or "").strip()
        if not sym:
            continue
        item = {c: r.get(c) for c in TECH_COLUMNS if c in r.index}
        item["ranking_tech_datetime"] = r.get("datetime")
        item["ranking_tech_db"] = db
        item["ranking_tech_readonly"] = True
        item["ranking_tech_cacheable"] = True
        result[sym] = item
    return result


def _read_latest_tech_from_db(db: str, symbols: list[str]) -> Dict[str, Dict[str, Any]]:
    if not symbols:
        return {}
    table = "ranking_technical_1min"
    batch_size = max(10, _env_int("RANKING_ENTRY_TECH_READ_BATCH_SIZE", 40))
    chunks = []
    with sqlite3.connect(db, timeout=1.5) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=1200")
        if not _table_exists(conn, table):
            logger.warning("[RANKING ENTRY FAST PATCH] readonly tech skipped reason=table_missing db=%s table=%s", db, table)
            return {}
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            placeholders = ",".join(["?"] * len(batch))
            q = f"""
                SELECT t.*
                FROM {table} t
                JOIN (
                    SELECT symbol, MAX(datetime) AS max_dt
                    FROM {table}
                    WHERE symbol IN ({placeholders})
                    GROUP BY symbol
                ) m
                  ON t.symbol = m.symbol
                 AND t.datetime = m.max_dt
                WHERE t.symbol IN ({placeholders})
            """
            params = tuple(batch) + tuple(batch)
            part = pd.read_sql_query(q, conn, params=params)
            if not part.empty:
                chunks.append(part)
    if not chunks:
        return {}
    return _rows_to_tech_map(pd.concat(chunks, ignore_index=True), db)


def _latest_existing_technicals(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    try:
        capped = _cap_rows(rows, context="before_readonly_tech")
        symbols = [str(r.get("symbol") or "").strip() for r in capped]
        symbols = [s for s in dict.fromkeys(symbols) if s]
        if not symbols:
            return {}
        db = _resolve_ranking_db_path()
        if not db or not os.path.exists(db):
            logger.warning("[RANKING ENTRY FAST PATCH] readonly tech skipped reason=db_missing db=%s symbols=%s", db, len(symbols))
            return {}

        t0 = time.time()
        cached, missing, cache_state = _cache_get(db, symbols)
        if cached and not missing:
            logger.warning("[RANKING ENTRY FAST PATCH] readonly tech cache hit symbols=%s hit=%s db=%s elapsed=%.3fs", len(symbols), len(cached), db, time.time() - t0)
            return cached
        db_items = _read_latest_tech_from_db(db, missing if cached else symbols)
        if db_items:
            _cache_put(db, db_items)
        result = dict(cached)
        result.update(db_items)
        logger.warning("[RANKING ENTRY FAST PATCH] readonly tech loaded symbols=%s hit=%s cache=%s db_read=%s db=%s elapsed=%.3fs", len(symbols), len(result), cache_state, len(db_items), db, time.time() - t0)
        return result
    except Exception:
        logger.exception("[RANKING ENTRY FAST PATCH] readonly tech load failed")
        return {}


def _patched_save_ranking_pseudo_technicals(rows: List[Dict[str, Any]], *args: Any, **kwargs: Any) -> Dict[str, Dict[str, Any]]:
    if not _env_bool("RANKING_ENTRY_FAST_TECH_CAP_ENABLED", True):
        return _ORIG_SAVE_TECH(rows, *args, **kwargs)
    capped = _cap_rows(rows, context="before_save_technical")
    if _env_bool("RANKING_ENTRY_SKIP_TECH_SAVE", True):
        t0 = time.time()
        ret = _latest_existing_technicals(capped) if _env_bool("RANKING_ENTRY_TECH_READONLY", True) else {}
        logger.warning("[RANKING ENTRY FAST PATCH] technical save skipped rows %s->%s readonly_hit=%s elapsed=%.3fs", len(rows), len(capped), len(ret or {}), time.time() - t0)
        return ret
    lookback = _env_int("RANKING_ENTRY_FAST_TECH_LOOKBACK_ROWS", 30)
    kwargs.setdefault("lookback_rows", lookback)
    t0 = time.time()
    ret = _ORIG_SAVE_TECH(capped, *args, **kwargs)
    logger.warning("[RANKING ENTRY FAST PATCH] technical done rows %s->%s latest=%s elapsed=%.3fs lookback=%s", len(rows), len(capped), len(ret or {}), time.time() - t0, kwargs.get("lookback_rows"))
    return ret


def _patched_load_history(conn: Any, symbols: List[str], lookback_rows: int = 120) -> pd.DataFrame:
    try:
        import trading.ranking.ranking_technical_store as store
        if not symbols:
            return pd.DataFrame()
        symbols = [str(s) for s in dict.fromkeys(symbols) if str(s).strip()]
        if not symbols:
            return pd.DataFrame()
        batch_size = max(10, _env_int("RANKING_ENTRY_FAST_HISTORY_BATCH_SIZE", 40))
        lookback_rows = min(int(lookback_rows or 30), _env_int("RANKING_ENTRY_FAST_TECH_LOOKBACK_ROWS", 30))
        chunks = []
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            placeholders = ",".join(["?"] * len(batch))
            q = f"SELECT * FROM {store.TABLE_NAME} WHERE symbol IN ({placeholders}) ORDER BY symbol ASC, datetime DESC"
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


def _patched_entry_from_ranking() -> int:
    """Budgeted replacement for trading.ranking.entry_from_ranking.entry_from_ranking."""
    import trading.ranking.entry_from_ranking as efr

    started_dt = dt.datetime.now()
    started = time.perf_counter()
    budget_sec = max(5.0, _env_float("RANKING_ENTRY_RUNTIME_BUDGET_SEC", 25.0))
    deadline = started + budget_sec
    max_pending = max(1, _env_int("RANKING_ENTRY_MAX_PENDING_PER_RUN", 5))

    logger.info("[RANKING ENTRY BUDGET] start at=%s budget_sec=%.1f max_pending=%s", started_dt.strftime("%Y-%m-%d %H:%M:%S"), budget_sec, max_pending)

    try:
        if not efr.is_time_allowed(started_dt):
            logger.info("[RANKING ENTRY BUDGET] skip reason=TIME_GUARD now=%s", started_dt.strftime("%H:%M:%S"))
            return 0

        ranking_df = efr._get_ranking_source_df()
        if ranking_df is None or ranking_df.empty:
            logger.info("[RANKING ENTRY BUDGET] skip reason=no_ranking_df")
            return 0

        rows_all = efr._prepare_rows(ranking_df)
        if not rows_all:
            logger.info("[RANKING ENTRY BUDGET] skip reason=no_normalized_rows")
            return 0

        rows = efr._light_prefilter_rows(rows_all)
        rows = _cap_rows(rows, context="runtime_budget_rows")
        if not rows:
            logger.info("[RANKING ENTRY BUDGET] skip reason=no_prefilter_rows raw_rows=%s", len(rows_all))
            return 0

        if time.perf_counter() >= deadline:
            logger.warning("[RANKING ENTRY BUDGET] stop before technical elapsed=%.3fs", time.perf_counter() - started)
            return 0

        tech_map: Dict[str, Dict[str, Any]] = {}
        cfg_tech = efr.RANKING_ENTRY_CONFIG.get("TECHNICAL", {}) or {}
        if bool(cfg_tech.get("ENABLED", True)) and callable(getattr(efr, "save_ranking_pseudo_technicals", None)):
            tech_map = efr.save_ranking_pseudo_technicals(rows)
            logger.info("[RANKING ENTRY BUDGET] technical attached symbols=%s prefiltered_rows=%s raw_rows=%s", len(tech_map), len(rows), len(rows_all))

        created = 0
        build_reject = 0
        filter_reject = 0
        pending_reject = 0
        reject_samples: List[Dict[str, Any]] = []
        reject_counts = Counter()
        current_keys: set[str] = set()
        best_by_symbol_side: Dict[Tuple[str, str], Dict[str, Any]] = {}
        now = efr._now()

        for row in rows:
            if time.perf_counter() >= deadline:
                logger.warning("[RANKING ENTRY BUDGET] stop while scoring elapsed=%.3fs candidates=%s", time.perf_counter() - started, len(best_by_symbol_side))
                break
            if callable(getattr(efr, "attach_ranking_technicals", None)):
                row = efr.attach_ranking_technicals(row, tech_map)
            symbol = str(row.get("symbol") or "").strip()
            side = efr._infer_side(row)
            row["side"] = side
            current_keys.add(efr._history_key(symbol, side))
            prev_h = efr._get_prev_history(symbol, side)
            prev_price = efr._safe_float(prev_h.get("last_price"), 0.0)
            prev_rank = efr._safe_int(prev_h.get("last_rank_position"), 999999)
            consecutive = int(prev_h.get("consecutive", 0)) + 1 if prev_h else 1
            score, parts = efr._calc_ranking_only_score(row, side, prev_price, prev_rank, consecutive)
            row["score"] = score
            row["score_total"] = score
            row["ranking_only_score"] = score
            row["ranking_score_parts"] = parts
            ok, reason = efr._passes_ranking_only_filters(row, side, prev_h, score, parts)
            efr._update_history(symbol, side, efr._safe_float(row.get("price") or row.get("current_price"), 0.0), efr._safe_int(row.get("rank_position"), 999999), str(row.get("rank_type") or ""), now)
            if not ok:
                filter_reject += 1
                reason_key = str(reason).split()[0].split("=")[0]
                reject_counts[reason_key] += 1
                if len(reject_samples) < 12:
                    reject_samples.append({
                        "symbol": symbol,
                        "side": side,
                        "rank_type": row.get("rank_type"),
                        "rank": row.get("rank_position"),
                        "price": row.get("price"),
                        "prev_price": prev_price,
                        "volume": row.get("volume"),
                        "turnover": row.get("turnover"),
                        "day_change_pct": row.get("day_change_pct"),
                        "score": round(score, 2),
                        "reason": reason,
                    })
                continue
            key = (symbol, side)
            old = best_by_symbol_side.get(key)
            old_score = efr._safe_float(old["row"].get("score_total"), 0.0) if isinstance(old, dict) and isinstance(old.get("row"), dict) else 0.0
            if old is None or score > old_score:
                best_by_symbol_side[key] = {"row": row, "parts": parts, "prev_price": prev_price, "prev_rank": prev_rank, "consecutive": consecutive}

        try:
            efr._reset_missing_histories(current_keys)
        except Exception:
            logger.debug("[RANKING ENTRY BUDGET] reset histories failed", exc_info=True)

        packs = list(best_by_symbol_side.items())
        packs.sort(key=lambda kv: efr._safe_float(kv[1]["row"].get("score_total"), 0.0), reverse=True)

        for (symbol, side), pack in packs:
            if created >= max_pending:
                break
            if time.perf_counter() >= deadline:
                logger.warning("[RANKING ENTRY BUDGET] stop while pending_add elapsed=%.3fs created=%s", time.perf_counter() - started, created)
                break
            row = pack["row"]
            parts = pack["parts"]
            prev_price = pack["prev_price"]
            prev_rank = pack["prev_rank"]
            consecutive = pack["consecutive"]
            final_score = efr._safe_float(row.get("score_total"), 0.0)
            entry_row = efr.build_entry_row(row)
            if not entry_row:
                build_reject += 1
                continue
            entry_row["side"] = side
            entry_row["source"] = "RANKING"
            entry_row["symbol"] = symbol
            entry_row.setdefault("entry_type", "RANKING")
            entry_row.setdefault("interval", 1)
            entry_row["score"] = final_score
            entry_row["score_total"] = final_score
            entry_row["ranking_only_score"] = final_score
            entry_row["ranking_entry_mode"] = "RANKING_ONLY_WITH_TECH_BUDGET"
            entry_row["ranking_prev_price"] = prev_price
            entry_row["ranking_prev_rank"] = prev_rank
            entry_row["ranking_consecutive"] = consecutive
            entry_row["ranking_step_pct"] = parts.get("step_pct")
            entry_row["ranking_rank_improve"] = parts.get("rank_improve")
            entry_row["ranking_score_parts"] = parts
            for k in ("ma5", "ma25", "ma75", "rsi", "macd", "signal", "macd_hist", "atr", "slope", "slope_atr_scaled", "vwap", "ranking_tech_score", "ranking_tech_ready", "ranking_tech_reason", "ranking_tech_datetime", "ranking_tech_db"):
                if k in row:
                    entry_row[k] = row.get(k)
            pending_entry = {
                **entry_row,
                "source": "RANKING",
                "created_at": now,
                "ranking_fallback_used": False,
                "ranking_strength": final_score,
                "technical_score": efr._safe_float(row.get("ranking_tech_score"), 0.0),
                "snapshot_score": final_score,
            }
            if efr.add_pending(pending_entry):
                created += 1
                logger.info(
                    "[RANKING PENDING ADD] mode=RANKING_ONLY_WITH_TECH_BUDGET symbol=%s side=%s rank_type=%s rank=%s price=%.2f prev_price=%.2f step=%.3f%% day=%.3f%% volume=%.0f turnover=%.0f consecutive=%s rank_improve=%.1f score=%.2f tech=%.2f",
                    symbol, side, row.get("rank_type"), row.get("rank_position"),
                    efr._safe_float(row.get("price") or row.get("current_price"), 0.0), prev_price,
                    efr._safe_float(parts.get("step_pct"), 0.0), efr._safe_float(row.get("day_change_pct"), 0.0),
                    efr._safe_float(row.get("volume"), 0.0), efr._safe_float(row.get("turnover"), 0.0), consecutive,
                    efr._safe_float(parts.get("rank_improve"), 0.0), final_score, efr._safe_float(row.get("ranking_tech_score"), 0.0),
                )
            else:
                pending_reject += 1

        elapsed = time.perf_counter() - started
        if reject_samples:
            logger.warning("[RANKING ENTRY BUDGET] reject_counts=%s samples=%s", dict(reject_counts), reject_samples)
        logger.info(
            "[RANKING ENTRY BUDGET] done created=%s raw_total=%s prefiltered=%s candidates=%s filter_reject=%s build_reject=%s pending_reject=%s budget_sec=%.1f elapsed=%.3fs",
            created, len(rows_all), len(rows), len(best_by_symbol_side), filter_reject, build_reject, pending_reject, budget_sec, elapsed,
        )
        return created
    except Exception:
        logger.exception("[RANKING ENTRY BUDGET] failed")
        return 0


def _patched_run_ranking_entry_pipeline() -> int:
    return _patched_entry_from_ranking()


def install() -> bool:
    global _PATCHED, _ORIG_LIGHT_PREFILTER, _ORIG_SAVE_TECH, _ORIG_LOAD_HISTORY, _ORIG_ENTRY_FROM_RANKING, _ORIG_RUN_PIPELINE
    if _PATCHED:
        return True
    try:
        import trading.ranking.entry_from_ranking as efr
        import trading.ranking.ranking_technical_store as store

        patched = []
        cur_pf = getattr(efr, "_light_prefilter_rows", None)
        if callable(cur_pf) and not getattr(cur_pf, "_ranking_entry_fast_patch_v4", False):
            _ORIG_LIGHT_PREFILTER = cur_pf
            _patched_light_prefilter_rows._ranking_entry_fast_patch_v4 = True  # type: ignore[attr-defined]
            efr._light_prefilter_rows = _patched_light_prefilter_rows
            patched.append("entry_from_ranking._light_prefilter_rows")

        cur_save = getattr(efr, "save_ranking_pseudo_technicals", None)
        if callable(cur_save) and not getattr(cur_save, "_ranking_entry_fast_patch_v4", False):
            _ORIG_SAVE_TECH = cur_save
            _patched_save_ranking_pseudo_technicals._ranking_entry_fast_patch_v4 = True  # type: ignore[attr-defined]
            efr.save_ranking_pseudo_technicals = _patched_save_ranking_pseudo_technicals
            store.save_ranking_pseudo_technicals = _patched_save_ranking_pseudo_technicals
            patched.append("save_ranking_pseudo_technicals_budgeted_readonly")

        cur_load = getattr(store, "_load_history", None)
        if callable(cur_load) and not getattr(cur_load, "_ranking_entry_fast_patch_v4", False):
            _ORIG_LOAD_HISTORY = cur_load
            _patched_load_history._ranking_entry_fast_patch_v4 = True  # type: ignore[attr-defined]
            store._load_history = _patched_load_history
            patched.append("ranking_technical_store._load_history")

        cur_entry = getattr(efr, "entry_from_ranking", None)
        if callable(cur_entry) and not getattr(cur_entry, "_ranking_entry_fast_patch_v4", False):
            _ORIG_ENTRY_FROM_RANKING = cur_entry
            _patched_entry_from_ranking._ranking_entry_fast_patch_v4 = True  # type: ignore[attr-defined]
            efr.entry_from_ranking = _patched_entry_from_ranking
            patched.append("entry_from_ranking.entry_from_ranking_budgeted")

        cur_run = getattr(efr, "run_ranking_entry_pipeline", None)
        if callable(cur_run) and not getattr(cur_run, "_ranking_entry_fast_patch_v4", False):
            _ORIG_RUN_PIPELINE = cur_run
            _patched_run_ranking_entry_pipeline._ranking_entry_fast_patch_v4 = True  # type: ignore[attr-defined]
            efr.run_ranking_entry_pipeline = _patched_run_ranking_entry_pipeline
            patched.append("entry_from_ranking.run_ranking_entry_pipeline_budgeted")

        _PATCHED = True
        logger.warning(
            "[RANKING ENTRY FAST PATCH] installed V4 patched=%s budget=%.1fs max_rows=%s max_symbols=%s max_pending=%s skip_save=%s readonly=%s cache=%s ttl=%.1f",
            patched,
            _env_float("RANKING_ENTRY_RUNTIME_BUDGET_SEC", 25.0),
            _env_int("RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS", 40),
            _env_int("RANKING_ENTRY_FAST_MAX_SYMBOLS", 40),
            _env_int("RANKING_ENTRY_MAX_PENDING_PER_RUN", 5),
            _env_bool("RANKING_ENTRY_SKIP_TECH_SAVE", True),
            _env_bool("RANKING_ENTRY_TECH_READONLY", True),
            _env_bool("RANKING_ENTRY_TECH_MEMORY_CACHE", True),
            _env_float("RANKING_ENTRY_TECH_CACHE_TTL_SEC", 90.0),
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
