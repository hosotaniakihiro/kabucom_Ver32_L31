# ============================================================
# File   : core/startup/summary_ai_strict_liquidity_extra_patch.py
# Version: Ver02-ROW-VOLUME-FALLBACK-GUARD
# ------------------------------------------------------------
# 既存 summary_ai_liquidity_runtime_patch の後段で、さらに直近1本出来高・平均出来高を確認する追加ガード。
#
# Ver02:
#   - 1min DB側の直近5本が薄く見えても、候補行の row_volume が十分大きい場合は通す。
#   - ログ上、AI_OK後に row_volume が数十万〜数百万株あるにもかかわらず、
#     1min DBの latest_volume/avg_volume が 30000 未満で全落ちしていたため。
#   - DB統計が古い/薄い/欠損のときだけ row_volume fallback を使う。
#
# ENV:
#   SUMMARY_AI_STRICT_LIQ_ENABLED=1
#   SUMMARY_AI_LIQ_MIN_LATEST_VOLUME=30000
#   SUMMARY_AI_LIQ_MIN_AVG_VOLUME=30000
#   SUMMARY_AI_LIQ_ROW_VOLUME_FALLBACK_ENABLED=1
#   SUMMARY_AI_LIQ_MIN_ROW_VOLUME_FALLBACK=300000
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_FILTER = None


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(default) if v is None or str(v).strip() == "" else float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(default) if v is None or str(v).strip() == "" else int(float(v))
    except Exception:
        return int(default)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        return float(default) if x != x else x
    except Exception:
        return float(default)


def _sym(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s


def _today() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _summary_db_path() -> str:
    base = os.getenv("SUMMARY_DB_DIR", r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary")
    return os.getenv("SUMMARY_DB_PATH", str(Path(base) / f"summary{_today()}.db"))


def _merged_item(item: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k in ("source_row", "ai_row"):
        v = item.get(k)
        if isinstance(v, dict):
            out.update(v)
    out.update(item)
    return out


def _col(conn: sqlite3.Connection, table: str, names: list[str]) -> str:
    try:
        cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for n in names:
            if n in cols:
                return n
    except Exception:
        return ""
    return ""


def _recent_volume_stats(symbol: str) -> dict[str, Any]:
    path = _summary_db_path()
    table = os.getenv("SUMMARY_AI_LIQ_SUMMARY_TABLE", "stock_summary_1min")
    bars = max(1, _env_int("SUMMARY_AI_LIQ_RECENT_BARS", 5))
    if not symbol or not Path(path).exists():
        return {}
    try:
        with sqlite3.connect(path, timeout=1.0) as conn:
            conn.execute("PRAGMA busy_timeout=1000")
            sym = _col(conn, table, ["symbol", "code", "stock_code"])
            tm = _col(conn, table, ["datetime", "dt", "timestamp", "time"])
            vo = _col(conn, table, ["volume", "Volume", "vol", "出来高"])
            if not sym or not tm or not vo:
                return {}
            rows = conn.execute(
                f"SELECT {tm}, {vo} FROM {table} WHERE CAST({sym} AS TEXT)=? ORDER BY {tm} DESC LIMIT ?",
                (_sym(symbol), bars),
            ).fetchall()
        vols = [max(0.0, _f(r[1], 0.0)) for r in rows]
        if not vols:
            return {}
        return {
            "latest_dt": str(rows[0][0]),
            "latest_volume": vols[0],
            "avg_volume": sum(vols) / max(1, len(vols)),
            "total_volume": sum(vols),
            "bars": len(vols),
        }
    except Exception:
        return {}


def _item_symbol(item: Dict[str, Any]) -> str:
    row = _merged_item(item)
    return _sym(row.get("symbol") or row.get("code") or row.get("stock_code"))


def _row_volume(item: Dict[str, Any]) -> float:
    row = _merged_item(item)
    for k in ("volume", "Volume", "vol", "出来高", "trading_volume"):
        v = _f(row.get(k), 0.0)
        if v > 0:
            return v
    return 0.0


def _row_volume_fallback_ok(row_vol: float, stats: dict[str, Any], detail: dict[str, Any]) -> bool:
    if not _env_bool("SUMMARY_AI_LIQ_ROW_VOLUME_FALLBACK_ENABLED", True):
        return False
    min_row = _env_float("SUMMARY_AI_LIQ_MIN_ROW_VOLUME_FALLBACK", 300000.0)
    if row_vol < min_row:
        return False

    latest = _f(stats.get("latest_volume"), 0.0)
    avg = _f(stats.get("avg_volume"), 0.0)
    bars = _f(stats.get("bars"), 0.0)

    # DB側の直近統計が薄い・不足・古い場合は、候補行の出来高を信用する。
    detail["row_volume_fallback"] = True
    detail["min_row_volume_fallback"] = min_row
    detail["fallback_reason"] = f"row_volume_ok latest={latest} avg={avg} bars={bars}"
    return True


def _strict_ok(item: Dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    symbol = _item_symbol(item)
    stats = _recent_volume_stats(symbol)
    row_vol = _row_volume(item)
    latest = _f(stats.get("latest_volume"), row_vol)
    avg = _f(stats.get("avg_volume"), row_vol)
    min_latest = _env_float("SUMMARY_AI_LIQ_MIN_LATEST_VOLUME", 30000.0)
    min_avg = _env_float("SUMMARY_AI_LIQ_MIN_AVG_VOLUME", 30000.0)
    detail = {"symbol": symbol, "row_volume": row_vol, **stats, "min_latest": min_latest, "min_avg": min_avg}

    if latest < min_latest:
        if _row_volume_fallback_ok(row_vol, stats, detail):
            detail["reason"] = "SUMMARY_AI_LIQ_ROW_VOLUME_FALLBACK_LATEST"
            return True, detail
        detail["reason"] = "SUMMARY_AI_LIQ_LATEST_VOLUME_LOW"
        return False, detail

    if avg < min_avg:
        if _row_volume_fallback_ok(row_vol, stats, detail):
            detail["reason"] = "SUMMARY_AI_LIQ_ROW_VOLUME_FALLBACK_AVG"
            return True, detail
        detail["reason"] = "SUMMARY_AI_LIQ_AVG_VOLUME_LOW"
        return False, detail

    return True, detail


def _patched_filter(ok_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    base = _ORIG_FILTER(ok_items) if callable(_ORIG_FILTER) else ok_items
    if not _env_bool("SUMMARY_AI_STRICT_LIQ_ENABLED", True):
        return base
    kept: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    fallback: List[Dict[str, Any]] = []
    for item in base or []:
        if not isinstance(item, dict):
            continue
        ok, detail = _strict_ok(item)
        if ok:
            kept.append(item)
            if detail.get("row_volume_fallback"):
                fallback.append(detail)
        else:
            skipped.append(detail)
    if fallback:
        logger.warning("[SUMMARY AI STRICT LIQ] row-volume fallback passed count=%s samples=%s", len(fallback), fallback[:30])
    if skipped:
        logger.warning("[SUMMARY AI STRICT LIQ] filtered before approved before=%s after=%s skipped=%s", len(base or []), len(kept), skipped[:80])
    else:
        logger.info("[SUMMARY AI STRICT LIQ] passed count=%s", len(kept))
    return kept


def install() -> bool:
    global _INSTALLED, _ORIG_FILTER
    if _INSTALLED:
        return True
    try:
        import trading.entry.summary_ai.executor as ex
        cur = getattr(ex, "_filter_blocked_ai_ok_items", None)
        if getattr(cur, "_summary_ai_strict_liq_extra_v2", False):
            _INSTALLED = True
            return True
        _ORIG_FILTER = cur
        _patched_filter._summary_ai_strict_liq_extra_v2 = True  # type: ignore[attr-defined]
        ex._filter_blocked_ai_ok_items = _patched_filter
        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI STRICT LIQ] installed V2 min_latest=%s min_avg=%s row_fallback=%s min_row=%s",
            _env_float("SUMMARY_AI_LIQ_MIN_LATEST_VOLUME", 30000.0),
            _env_float("SUMMARY_AI_LIQ_MIN_AVG_VOLUME", 30000.0),
            _env_bool("SUMMARY_AI_LIQ_ROW_VOLUME_FALLBACK_ENABLED", True),
            _env_float("SUMMARY_AI_LIQ_MIN_ROW_VOLUME_FALLBACK", 300000.0),
        )
        return True
    except Exception as e:
        logger.exception("[SUMMARY AI STRICT LIQ] install failed err=%s", e)
        return False

try:
    install()
except Exception as e:
    logger.exception("[SUMMARY AI STRICT LIQ] auto install failed err=%s", e)

__all__ = ["install"]
