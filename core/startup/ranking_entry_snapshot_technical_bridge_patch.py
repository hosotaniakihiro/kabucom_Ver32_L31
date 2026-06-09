# ============================================================
# File   : core/startup/ranking_entry_snapshot_technical_bridge_patch.py
# Version: V2-RANKING-SNAPSHOT-DB-TECH-BRIDGE
# ------------------------------------------------------------
# 目的:
#   PUSH summary history が main.py 側で空、かつ prefilter後のrowから
#   ma/atr/slope/macd/rsi列が落ちている場合でも、ranking_snapshot_1min
#   の最新行をsymbol指定で直接読み直し、Ranking entry の pending / entry_row
#   にtechnical列を流す。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import math
import os
import sqlite3
from functools import lru_cache, wraps
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False

_BASE_TECH = (
    "ma5", "ma25", "ma75", "rsi", "macd", "signal", "macd_hist", "atr",
    "slope", "slope_pct", "slope_atr_scaled", "price_change_pct",
    "volume_sma5", "volume_sma25", "volume_ratio5", "technical_ready",
)
_EXTRA_COPY = (
    "open", "high", "low", "close", "price", "current_price", "volume", "turnover",
    "ranking_tech_source", "ranking_tech_reason", "ranking_tech_datetime", "datetime",
    "snapshot_time", "rank", "rank_type", "ranking_type", "change_percentage", "change_rate",
)
_TECH_DB_COLS = tuple(
    [f"{b}_{tf}m" for tf in (1, 3, 5) for b in _BASE_TECH]
    + ["open", "high", "low", "close", "price", "current_price", "volume", "turnover", "datetime", "snapshot_time", "symbol", "symbolname"]
)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _sf(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        s = str(v).strip().replace(",", "")
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return float(default)
        x = float(s)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _has_value(v: Any) -> bool:
    try:
        if v is None:
            return False
        s = str(v).strip()
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return False
        return True
    except Exception:
        return False


def _first(row: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        try:
            if k in row and _has_value(row.get(k)):
                return row.get(k)
        except Exception:
            pass
    return None


def _symbol(row: dict[str, Any]) -> str:
    try:
        return str(row.get("symbol") or row.get("Symbol") or "").strip()
    except Exception:
        return ""


def _today_yyyymmdd() -> str:
    try:
        from scheduler_jobs.summary.time_utils import now_naive
        return now_naive().strftime("%Y%m%d")
    except Exception:
        return dt.datetime.now().strftime("%Y%m%d")


def _ranking_db_path() -> str:
    try:
        from ats.ats_ranking.db_path import resolve_ranking_db_path
        p = resolve_ranking_db_path()
        if p:
            return str(p)
    except Exception:
        pass
    base = os.getenv("AUTOSTOCK_RANKING_DB_DIR", r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking")
    return os.path.join(base, f"ranking{_today_yyyymmdd()}.db")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _has_real_tech(row: dict[str, Any]) -> bool:
    try:
        for tf in (1, 3, 5):
            if _sf(row.get(f"ma5_{tf}m"), 0.0) > 0:
                return True
            if _sf(row.get(f"atr_{tf}m"), 0.0) > 0:
                return True
            if abs(_sf(row.get(f"slope_{tf}m"), 0.0)) > 0:
                return True
            if abs(_sf(row.get(f"macd_{tf}m"), 0.0)) > 0:
                return True
        return False
    except Exception:
        return False


@lru_cache(maxsize=1024)
def _load_snapshot_tech_cached(symbol: str, rank_type: str, side: str, bucket_minute: int) -> tuple[tuple[str, Any], ...]:
    """LRU cache keyにminute bucketを入れ、1分ごとに自然更新する。"""
    del side, bucket_minute
    db = _ranking_db_path()
    if not symbol or not os.path.exists(db):
        return tuple()
    try:
        with sqlite3.connect(db, timeout=2.0) as conn:
            conn.row_factory = sqlite3.Row
            cols = _table_columns(conn, "ranking_snapshot_1min")
            if not cols:
                return tuple()
            select_cols = [c for c in _TECH_DB_COLS if c in cols]
            for c in ("ma5_1m", "ma25_1m", "ma75_1m", "rsi_1m", "macd_1m", "signal_1m", "atr_1m", "slope_1m"):
                if c in cols and c not in select_cols:
                    select_cols.append(c)
            if "symbol" not in select_cols and "symbol" in cols:
                select_cols.append("symbol")
            if not select_cols:
                return tuple()
            where = "symbol=?"
            params: list[Any] = [symbol]
            # rank_typeが一致する行を優先。ただし無い場合も拾う。
            order_rank_type = "0"
            if rank_type and "rank_type" in cols:
                order_rank_type = "CASE WHEN rank_type=? THEN 0 ELSE 1 END"
                params.insert(0, rank_type)
            dt_col = "datetime" if "datetime" in cols else ("snapshot_time" if "snapshot_time" in cols else None)
            order_dt = f"{dt_col} DESC" if dt_col else "rowid DESC"
            sql = f"SELECT {', '.join(select_cols)} FROM ranking_snapshot_1min WHERE {where} ORDER BY {order_rank_type}, {order_dt}, rowid DESC LIMIT 1"
            row = conn.execute(sql, params).fetchone()
            if not row:
                return tuple()
            d = dict(row)
            logger.warning(
                "[RANKING SNAPSHOT TECH BRIDGE] db lookup symbol=%s rank_type=%s cols=%s ma5_1m=%s atr_1m=%s slope_1m=%s macd_1m=%s dt=%s",
                symbol, rank_type, len(d), d.get("ma5_1m"), d.get("atr_1m"), d.get("slope_1m"), d.get("macd_1m"), d.get("datetime") or d.get("snapshot_time"),
            )
            return tuple(d.items())
    except Exception:
        logger.debug("[RANKING SNAPSHOT TECH BRIDGE] db lookup failed symbol=%s db=%s", symbol, db, exc_info=True)
        return tuple()


def _merge_db_snapshot_tech(row: dict[str, Any]) -> tuple[dict[str, Any], int]:
    if not _env_bool("RANKING_SNAPSHOT_TECH_DB_LOOKUP", True):
        return row, 0
    if _has_real_tech(row):
        return row, 0
    sym = _symbol(row)
    rt = str(row.get("rank_type") or row.get("ranking_type") or "").strip()
    side = str(row.get("side") or row.get("entry_decision") or "").strip().upper()
    bucket = int(dt.datetime.now().timestamp() // max(10, _env_int("RANKING_SNAPSHOT_TECH_DB_CACHE_SEC", 60)))
    items = _load_snapshot_tech_cached(sym, rt, side, bucket)
    if not items:
        return row, 0
    src = dict(items)
    out = dict(row)
    copied = 0
    for k, v in src.items():
        if not _has_value(v):
            continue
        # row側が欠損または0ならDB snapshot値で補完する。
        if k not in out or not _has_value(out.get(k)) or (_sf(out.get(k), 0.0) == 0.0 and _sf(v, 0.0) != 0.0):
            out[k] = v
            copied += 1
    if copied:
        out["ranking_tech_db_lookup"] = True
        out["ranking_tech_db_source"] = "ranking_snapshot_1min_latest_by_symbol"
    return out, copied


def _choose_tf(row: dict[str, Any]) -> int:
    """Entry controller の1分ATR/MAガードには1mを優先する。なければ3m/5m。"""
    for tf in (1, 3, 5):
        if _sf(row.get(f"atr_{tf}m"), 0.0) > 0 or _sf(row.get(f"ma5_{tf}m"), 0.0) > 0 or abs(_sf(row.get(f"slope_{tf}m"), 0.0)) > 0:
            return tf
    return 1


def _copy_snapshot_tech(row: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
    if not isinstance(row, dict) or not row:
        return row, 0, 1
    merged, db_copied = _merge_db_snapshot_tech(dict(row))
    out = dict(merged)
    tf = _choose_tf(out)
    copied = int(db_copied)

    # unsuffixed aliases: downstream guards often read ma5/atr/slope directly.
    for base in _BASE_TECH:
        src = f"{base}_{tf}m"
        if src in out and _has_value(out.get(src)):
            if base not in out or not _has_value(out.get(base)) or _sf(out.get(base), 0.0) == 0.0:
                out[base] = out.get(src)
                copied += 1
        for t in (1, 3, 5):
            k = f"{base}_{t}m"
            if k in out and _has_value(out.get(k)):
                out[k] = out.get(k)

    close = _first(out, "close", "close_1m", "price", "current_price", "close_price")
    if close is not None:
        for k in ("close", "price", "current_price", "close_price"):
            if k not in out or not _has_value(out.get(k)) or _sf(out.get(k), 0.0) == 0.0:
                out[k] = close
                copied += 1

    for base in ("open", "high", "low"):
        v = _first(out, base, f"{base}_1m", f"{base}_{tf}m", f"{base}_price")
        if v is not None and (base not in out or not _has_value(out.get(base)) or _sf(out.get(base), 0.0) == 0.0):
            out[base] = v
            copied += 1

    slope1 = _sf(out.get("slope_1m") or out.get("slope"), 0.0)
    slope3 = _sf(out.get("slope_3m"), 0.0)
    slope5 = _sf(out.get("slope_5m"), 0.0)
    side = str(out.get("side") or out.get("entry_decision") or "").upper()
    aligned = 0
    if side == "SELL":
        aligned = int(slope1 < 0) + int(slope3 < 0) + int(slope5 < 0)
    elif side == "BUY":
        aligned = int(slope1 > 0) + int(slope3 > 0) + int(slope5 > 0)
    if aligned > 0:
        out.setdefault("mtf", float(aligned))
        out.setdefault("score_mtf", float(aligned))
        out.setdefault("mtf_score", float(aligned))
        copied += 1

    ready = any(
        _sf(out.get(f"atr_{t}m"), 0.0) > 0
        or _sf(out.get(f"ma5_{t}m"), 0.0) > 0
        or abs(_sf(out.get(f"slope_{t}m"), 0.0)) > 0
        or abs(_sf(out.get(f"macd_{t}m"), 0.0)) > 0
        for t in (1, 3, 5)
    )
    if ready:
        out["ranking_tech_ready"] = True
        out["technical_ready"] = True
        out["ranking_tech_source"] = out.get("ranking_tech_source") or out.get("ranking_tech_db_source") or "ranking_snapshot_1min"
        out["ranking_tech_reason"] = out.get("ranking_tech_reason") or f"snapshot_technical_bridge_tf={tf}m"
        copied += 1

    return out, copied, tf


def _patch_entry_from_ranking() -> bool:
    import trading.ranking.entry_from_ranking as efr
    patched = False

    cur_attach = getattr(efr, "attach_ranking_technicals", None)
    if callable(cur_attach) and not getattr(cur_attach, "_snapshot_technical_bridge_v2", False):
        orig_attach = getattr(cur_attach, "_original", cur_attach)

        @wraps(orig_attach)
        def attach_wrapper(row: dict[str, Any], tech_map: dict[str, dict[str, Any]] | None = None):
            try:
                ret = orig_attach(row, tech_map)
            except Exception:
                logger.debug("[RANKING SNAPSHOT TECH BRIDGE] original attach failed; use raw row", exc_info=True)
                ret = row
            try:
                # orig_attach/alias patch が列を落とす場合があるため、元rowとretを再マージ。
                base = dict(row or {})
                if isinstance(ret, dict):
                    base.update(ret)
                out, copied, tf = _copy_snapshot_tech(base)
                if copied > 0:
                    logger.info(
                        "[RANKING SNAPSHOT TECH BRIDGE] attached symbol=%s copied=%s tf=%sm db=%s atr=%s ma5=%s slope=%s macd=%s signal=%s",
                        out.get("symbol"), copied, tf, out.get("ranking_tech_db_lookup"), out.get("atr"), out.get("ma5"), out.get("slope"), out.get("macd"), out.get("signal"),
                    )
                return out
            except Exception:
                logger.exception("[RANKING SNAPSHOT TECH BRIDGE] attach bridge failed")
                return ret

        attach_wrapper._snapshot_technical_bridge_v2 = True  # type: ignore[attr-defined]
        attach_wrapper._original = orig_attach  # type: ignore[attr-defined]
        efr.attach_ranking_technicals = attach_wrapper
        patched = True

    cur_builder = getattr(efr, "build_entry_row", None)
    if callable(cur_builder) and not getattr(cur_builder, "_snapshot_technical_bridge_v2", False):
        orig_builder = getattr(cur_builder, "_original", cur_builder)

        @wraps(orig_builder)
        def build_entry_row_wrapper(row: dict[str, Any], *args: Any, **kwargs: Any):
            bridged, copied, tf = _copy_snapshot_tech(row if isinstance(row, dict) else dict(row or {}))
            entry = orig_builder(bridged, *args, **kwargs)
            if isinstance(entry, dict):
                for base in _BASE_TECH:
                    for key in (base, f"{base}_1m", f"{base}_3m", f"{base}_5m"):
                        if key in bridged and _has_value(bridged.get(key)):
                            entry[key] = bridged.get(key)
                for key in _EXTRA_COPY:
                    if key in bridged and _has_value(bridged.get(key)):
                        entry[key] = bridged.get(key)
                entry.setdefault("ranking_tech_ready", bridged.get("ranking_tech_ready", False))
                entry.setdefault("technical_ready", bridged.get("technical_ready", False))
                entry.setdefault("ranking_tech_source", bridged.get("ranking_tech_source", "ranking_snapshot_1min" if copied else ""))
                entry.setdefault("ranking_tech_reason", bridged.get("ranking_tech_reason", f"snapshot_technical_bridge_tf={tf}m" if copied else ""))
                entry.setdefault("ranking_tech_db_lookup", bridged.get("ranking_tech_db_lookup", False))
                if copied > 0:
                    logger.info("[RANKING SNAPSHOT TECH BRIDGE] build_entry_row copied symbol=%s copied=%s tf=%sm db=%s", entry.get("symbol") or bridged.get("symbol"), copied, tf, bridged.get("ranking_tech_db_lookup"))
            return entry

        build_entry_row_wrapper._snapshot_technical_bridge_v2 = True  # type: ignore[attr-defined]
        build_entry_row_wrapper._original = orig_builder  # type: ignore[attr-defined]
        efr.build_entry_row = build_entry_row_wrapper
        patched = True

    return patched or bool(getattr(getattr(efr, "attach_ranking_technicals", None), "_snapshot_technical_bridge_v2", False))


def install() -> bool:
    global _INSTALLED
    if not _env_bool("RANKING_SNAPSHOT_TECH_BRIDGE_ENABLED", True):
        logger.warning("[RANKING SNAPSHOT TECH BRIDGE] disabled by env")
        return False
    try:
        os.environ.setdefault("RANKING_SNAPSHOT_TECH_BRIDGE_ENABLED", "1")
        os.environ.setdefault("RANKING_SNAPSHOT_TECH_DB_LOOKUP", "1")
        os.environ.setdefault("RANKING_SNAPSHOT_TECH_DB_CACHE_SEC", "60")
        ok = _patch_entry_from_ranking()
        _INSTALLED = bool(ok)
        logger.warning("[RANKING SNAPSHOT TECH BRIDGE] installed v2 db_lookup=%s ok=%s", _env_bool("RANKING_SNAPSHOT_TECH_DB_LOOKUP", True), ok)
        return bool(ok)
    except Exception:
        logger.exception("[RANKING SNAPSHOT TECH BRIDGE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING SNAPSHOT TECH BRIDGE] auto install failed")


__all__ = ["install"]
