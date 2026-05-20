# ============================================================
# File   : core/startup/watchlist_recent_liquidity_guard_patch.py
# Version: Ver01-WATCHLIST-RECENT-LIQUIDITY-GUARD
# ------------------------------------------------------------
# 監視銘柄選定時点で、直近出来高/売買代金の最低条件を追加する。
#
# 目的:
#   active_symbols / PUSH登録対象に、極端に出来高が薄い銘柄が入るのを防ぐ。
#
# default:
#   WATCHLIST_RECENT_LIQ_ENABLED=1
#   WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME=3000
#   WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME=3000
#   WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN=1000000
#   WATCHLIST_RECENT_LIQ_BARS=5
#
# 対象:
#   - trading.ranking.active_symbols.manager.update_active_symbols の戻り値
#   - trading.ranking.active_symbols.manager.get_active_symbols 系
#   - trading.push.push_stream.rotation_symbols.apply_register_liquidity_guard
#
# 注意:
#   protected銘柄、保有中/未約定/直近ENTRY候補は既存ロジックを尊重し、
#   WATCHLIST_RECENT_LIQ_PROTECT_BYPASS=1 の場合は除外しない。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterable, List, Sequence

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_UPDATE_ACTIVE = None
_ORIG_GET_ACTIVE = None
_ORIG_GET_MONITOR = None
_ORIG_GET_PUSH = None
_ORIG_GET_REGISTER = None
_ORIG_GET_SUBSCRIPTION = None
_ORIG_GET_ROTATION = None
_ORIG_APPLY_REGISTER_LIQ = None

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    except Exception:
        return bool(default)


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


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _dedupe(items: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for x in items or []:
        s = _norm_symbol(x)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _today() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _summary_db_path() -> str:
    base = os.getenv(
        "SUMMARY_DB_DIR",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary",
    )
    return os.getenv("SUMMARY_DB_PATH", str(Path(base) / f"summary{_today()}.db"))


def _col(conn: sqlite3.Connection, table: str, names: Sequence[str]) -> str:
    try:
        cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for n in names:
            if n in cols:
                return n
    except Exception:
        return ""
    return ""


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        return float(default) if x != x else x
    except Exception:
        return float(default)


def _protected_symbols() -> set[str]:
    if not _env_bool("WATCHLIST_RECENT_LIQ_PROTECT_BYPASS", True):
        return set()
    try:
        from trading.ranking.active_symbols.protected import get_protected_symbols
        return {_norm_symbol(s) for s in (get_protected_symbols() or []) if _norm_symbol(s)}
    except Exception:
        return set()


def _recent_stats(symbol: str) -> dict[str, Any]:
    symbol = _norm_symbol(symbol)
    if not symbol:
        return {}

    ttl = max(0.0, _env_float("WATCHLIST_RECENT_LIQ_CACHE_TTL_SEC", 10.0))
    now_ts = dt.datetime.now().timestamp()
    cached = _CACHE.get(symbol)
    if cached and ttl > 0 and now_ts - cached[0] <= ttl:
        return dict(cached[1])

    path = _summary_db_path()
    table = os.getenv("WATCHLIST_RECENT_LIQ_SUMMARY_TABLE", "stock_summary_1min")
    bars = max(1, _env_int("WATCHLIST_RECENT_LIQ_BARS", 5))

    if not Path(path).exists():
        return {}

    try:
        with sqlite3.connect(path, timeout=1.0) as conn:
            conn.execute("PRAGMA busy_timeout=1000")
            sym_col = _col(conn, table, ["symbol", "code", "stock_code"])
            tm_col = _col(conn, table, ["datetime", "dt", "timestamp", "time"])
            close_col = _col(conn, table, ["close_price", "close", "price", "current_price"])
            vol_col = _col(conn, table, ["volume", "Volume", "vol", "出来高"])
            turn_col = _col(conn, table, ["turnover", "turnover_yen", "trading_value", "売買代金"])
            if not sym_col or not tm_col or not vol_col:
                return {}

            select_close = close_col if close_col else "0"
            select_turn = turn_col if turn_col else "0"
            sql = f"""
                SELECT {tm_col}, {select_close}, {vol_col}, {select_turn}
                FROM {table}
                WHERE CAST({sym_col} AS TEXT)=?
                ORDER BY {tm_col} DESC
                LIMIT ?
            """
            rows = conn.execute(sql, (symbol, bars)).fetchall()

        if not rows:
            return {}

        latest_dt = str(rows[0][0])
        latest_close = _f(rows[0][1], 0.0)
        volumes = [max(0.0, _f(r[2], 0.0)) for r in rows]
        turnovers = [max(0.0, _f(r[3], 0.0)) for r in rows]
        latest_volume = volumes[0] if volumes else 0.0
        avg_volume = sum(volumes) / max(1, len(volumes))
        total_volume = sum(volumes)
        total_turnover = sum(turnovers)

        if total_turnover <= 0 and latest_close > 0 and total_volume > 0:
            total_turnover = latest_close * total_volume

        stats = {
            "symbol": symbol,
            "latest_dt": latest_dt,
            "bars": len(rows),
            "close": latest_close,
            "latest_volume": latest_volume,
            "avg_volume": avg_volume,
            "total_volume": total_volume,
            "total_turnover": total_turnover,
            "db": path,
        }
        _CACHE[symbol] = (now_ts, stats)
        return dict(stats)
    except Exception:
        logger.debug("[WATCHLIST RECENT LIQ] read failed symbol=%s path=%s", symbol, path, exc_info=True)
        return {}


def _ok(symbol: str, *, protected: set[str]) -> tuple[bool, str, dict[str, Any]]:
    symbol = _norm_symbol(symbol)
    if not symbol:
        return False, "NO_SYMBOL", {"symbol": symbol}

    if symbol in protected:
        return True, "PROTECTED", {"symbol": symbol, "protected": True}

    stats = _recent_stats(symbol)
    min_latest = _env_float("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", 3000.0)
    min_avg = _env_float("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", 3000.0)
    min_turnover = _env_float("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", 1_000_000.0)

    detail = {
        "symbol": symbol,
        **stats,
        "min_latest_volume": min_latest,
        "min_avg_volume": min_avg,
        "min_turnover": min_turnover,
    }

    if not stats:
        return False, "NO_RECENT_SUMMARY", detail
    if _f(stats.get("latest_volume"), 0.0) < min_latest:
        return False, "LATEST_VOLUME_LOW", detail
    if _f(stats.get("avg_volume"), 0.0) < min_avg:
        return False, "AVG_VOLUME_LOW", detail
    if _f(stats.get("total_turnover"), 0.0) < min_turnover:
        return False, "TURNOVER_LOW", detail
    return True, "OK", detail


def _filter_symbols(symbols: Iterable[Any], *, context: str) -> List[str]:
    items = _dedupe(symbols)
    if not _env_bool("WATCHLIST_RECENT_LIQ_ENABLED", True):
        return items

    protected = _protected_symbols()
    kept: List[str] = []
    skipped: List[dict[str, Any]] = []
    for s in items:
        ok, reason, detail = _ok(s, protected=protected)
        if ok:
            kept.append(s)
        else:
            skipped.append({"reason": reason, **detail})

    if skipped:
        logger.warning(
            "[WATCHLIST RECENT LIQ] filtered context=%s before=%s after=%s skipped=%s",
            context,
            len(items),
            len(kept),
            skipped[:80],
        )
    else:
        logger.info(
            "[WATCHLIST RECENT LIQ] passed context=%s count=%s min_latest=%s min_avg=%s min_turnover=%s",
            context,
            len(kept),
            _env_float("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", 3000.0),
            _env_float("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", 3000.0),
            _env_float("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", 1_000_000.0),
        )
    return kept


def _patched_update_active_symbols(*args, **kwargs):
    symbols = _ORIG_UPDATE_ACTIVE(*args, **kwargs) if callable(_ORIG_UPDATE_ACTIVE) else []
    filtered = _filter_symbols(symbols, context="active.update_active_symbols")
    try:
        from trading.ranking.active_symbols.reflect import reflect_active_to_global
        reflect_active_to_global(filtered)
    except Exception:
        logger.debug("[WATCHLIST RECENT LIQ] reflect after update failed", exc_info=True)
    return filtered


def _wrap_getter(orig, name: str):
    def _getter(*args, **kwargs):
        symbols = orig(*args, **kwargs) if callable(orig) else []
        return _filter_symbols(symbols, context=f"active.{name}")
    _getter._watchlist_recent_liq_patch_v1 = True  # type: ignore[attr-defined]
    return _getter


def _patched_apply_register_liquidity_guard(targets: Sequence[str]) -> List[str]:
    base = _ORIG_APPLY_REGISTER_LIQ(targets) if callable(_ORIG_APPLY_REGISTER_LIQ) else list(targets or [])
    return _filter_symbols(base, context="push.rotation.apply_register_liquidity_guard")


def install() -> bool:
    global _INSTALLED
    global _ORIG_UPDATE_ACTIVE, _ORIG_GET_ACTIVE, _ORIG_GET_MONITOR, _ORIG_GET_PUSH, _ORIG_GET_REGISTER, _ORIG_GET_SUBSCRIPTION, _ORIG_GET_ROTATION, _ORIG_APPLY_REGISTER_LIQ

    if _INSTALLED:
        return True

    try:
        import trading.ranking.active_symbols.manager as mgr
        import trading.ranking.active_symbol_manager as compat
        import trading.push.push_stream.rotation_symbols as rot_sym
        import trading.push.push_stream.rotation as rot

        _ORIG_UPDATE_ACTIVE = getattr(mgr, "update_active_symbols", None)
        _ORIG_GET_ACTIVE = getattr(mgr, "get_active_symbols", None)
        _ORIG_GET_MONITOR = getattr(mgr, "get_monitor_symbols", None)
        _ORIG_GET_PUSH = getattr(mgr, "get_push_symbols", None)
        _ORIG_GET_REGISTER = getattr(mgr, "get_register_symbols", None)
        _ORIG_GET_SUBSCRIPTION = getattr(mgr, "get_subscription_symbols", None)
        _ORIG_GET_ROTATION = getattr(mgr, "get_rotation_symbols", None)
        _ORIG_APPLY_REGISTER_LIQ = getattr(rot_sym, "apply_register_liquidity_guard", None)

        mgr.update_active_symbols = _patched_update_active_symbols
        mgr.get_active_symbols = _wrap_getter(_ORIG_GET_ACTIVE, "get_active_symbols")
        mgr.get_current_active_symbols = mgr.get_active_symbols
        mgr.get_monitor_symbols = _wrap_getter(_ORIG_GET_MONITOR, "get_monitor_symbols")
        mgr.get_push_symbols = _wrap_getter(_ORIG_GET_PUSH, "get_push_symbols")
        mgr.get_register_symbols = _wrap_getter(_ORIG_GET_REGISTER, "get_register_symbols")
        mgr.get_subscription_symbols = _wrap_getter(_ORIG_GET_SUBSCRIPTION, "get_subscription_symbols")
        mgr.get_rotation_symbols = _wrap_getter(_ORIG_GET_ROTATION, "get_rotation_symbols")

        # 互換ラッパー側も差し替える
        compat.update_active_symbols = mgr.update_active_symbols
        compat.get_active_symbols = mgr.get_active_symbols
        compat.get_current_active_symbols = mgr.get_current_active_symbols
        compat.get_monitor_symbols = mgr.get_monitor_symbols
        compat.get_push_symbols = mgr.get_push_symbols
        compat.get_register_symbols = mgr.get_register_symbols
        compat.get_subscription_symbols = mgr.get_subscription_symbols
        compat.get_rotation_symbols = mgr.get_rotation_symbols

        rot_sym.apply_register_liquidity_guard = _patched_apply_register_liquidity_guard
        try:
            rot._apply_register_liquidity_guard = _patched_apply_register_liquidity_guard
        except Exception:
            pass

        _INSTALLED = True
        logger.warning(
            "[WATCHLIST RECENT LIQ] installed enabled=%s latest>=%s avg>=%s turnover>=%s bars=%s protect_bypass=%s",
            _env_bool("WATCHLIST_RECENT_LIQ_ENABLED", True),
            _env_float("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", 3000.0),
            _env_float("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", 3000.0),
            _env_float("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", 1_000_000.0),
            _env_int("WATCHLIST_RECENT_LIQ_BARS", 5),
            _env_bool("WATCHLIST_RECENT_LIQ_PROTECT_BYPASS", True),
        )
        return True
    except Exception as e:
        logger.exception("[WATCHLIST RECENT LIQ] install failed err=%s", e)
        return False

try:
    install()
except Exception as e:
    logger.exception("[WATCHLIST RECENT LIQ] auto install failed err=%s", e)

__all__ = ["install"]
