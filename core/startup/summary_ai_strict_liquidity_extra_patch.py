# ============================================================
# File   : core/startup/summary_ai_strict_liquidity_extra_patch.py
# Version: Ver01-LATEST-AVG-VOLUME-EXTRA-GUARD
# ------------------------------------------------------------
# 既存 summary_ai_liquidity_runtime_patch の後段で、さらに厳しく
# 直近1本出来高・平均出来高を確認する追加ガード。
#
# 目的:
#   5本合計 volume だけで薄い銘柄が通る問題を止める。
#
# ENV:
#   SUMMARY_AI_STRICT_LIQ_ENABLED=1
#   SUMMARY_AI_LIQ_MIN_LATEST_VOLUME=30000
#   SUMMARY_AI_LIQ_MIN_AVG_VOLUME=30000
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
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}


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
    return _f(row.get("volume") or row.get("Volume") or row.get("vol") or row.get("出来高"), 0.0)


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
        detail["reason"] = "SUMMARY_AI_LIQ_LATEST_VOLUME_LOW"
        return False, detail
    if avg < min_avg:
        detail["reason"] = "SUMMARY_AI_LIQ_AVG_VOLUME_LOW"
        return False, detail
    return True, detail


def _patched_filter(ok_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    base = _ORIG_FILTER(ok_items) if callable(_ORIG_FILTER) else ok_items
    if not _env_bool("SUMMARY_AI_STRICT_LIQ_ENABLED", True):
        return base
    kept: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for item in base or []:
        if not isinstance(item, dict):
            continue
        ok, detail = _strict_ok(item)
        if ok:
            kept.append(item)
        else:
            skipped.append(detail)
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
        if getattr(cur, "_summary_ai_strict_liq_extra_v1", False):
            _INSTALLED = True
            return True
        _ORIG_FILTER = cur
        _patched_filter._summary_ai_strict_liq_extra_v1 = True  # type: ignore[attr-defined]
        ex._filter_blocked_ai_ok_items = _patched_filter
        _INSTALLED = True
        logger.warning("[SUMMARY AI STRICT LIQ] installed min_latest=%s min_avg=%s", _env_float("SUMMARY_AI_LIQ_MIN_LATEST_VOLUME", 30000.0), _env_float("SUMMARY_AI_LIQ_MIN_AVG_VOLUME", 30000.0))
        return True
    except Exception as e:
        logger.exception("[SUMMARY AI STRICT LIQ] install failed err=%s", e)
        return False

try:
    install()
except Exception as e:
    logger.exception("[SUMMARY AI STRICT LIQ] auto install failed err=%s", e)

__all__ = ["install"]
