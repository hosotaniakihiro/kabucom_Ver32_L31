# -*- coding: utf-8 -*-
# ============================================================
# File   : trading/ranking/active_symbols/recent_liquidity.py
# ------------------------------------------------------------
# 旧 3 層を 1 本にまとめたもの:
#   core/startup/watchlist_recent_liquidity_guard_patch.py   (_wrl_*)
#   core/startup/watchlist_recent_liquidity_bulk_patch.py    (_wbk_*)
#   usercustomize.py の PUSH ROTATION LIQ FAILOPEN ラッパー    (_uc_*)
#
# 直近の出来高・売買代金が細い銘柄を watchlist / PUSH 登録母集団から
# 落とす。実行時の合成は
#
#   guard の per-symbol 実装
#     <- bulk が**完全置換** (SQLite を 1 回のクエリでまとめ読み)
#       <- usercustomize が min_keep fail-open で包む
#
# だった。bulk と uc はどちらも usercustomize から main.py 限定で入るため、
# scope_bulk() が真のときだけ bulk + uc 経路、偽なら guard の per-symbol 経路。
# filter_symbols() がその分岐をそのまま持っている。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterable, List, Sequence
from sqlite_cm import committing_connect
from nas_root import nas_path, nas_root

logger = logging.getLogger(__name__)




_wrl_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _wrl_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    except Exception:
        return bool(default)


def _wrl_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(default) if v is None or str(v).strip() == "" else float(v)
    except Exception:
        return float(default)


def _wrl_env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(default) if v is None or str(v).strip() == "" else int(float(v))
    except Exception:
        return int(default)


def _wrl_norm_symbol(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _wrl_dedupe(items: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for x in items or []:
        s = _wrl_norm_symbol(x)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _wrl_today() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _wrl_summary_db_path() -> str:
    base = os.getenv(
        "SUMMARY_DB_DIR",
        nas_path(r"raw_data\kabu_station\summary"),
    )
    return os.getenv("SUMMARY_DB_PATH", str(Path(base) / f"summary{_wrl_today()}.db"))


def _wrl_col(conn: sqlite3.Connection, table: str, names: Sequence[str]) -> str:
    try:
        cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for n in names:
            if n in cols:
                return n
    except Exception:
        return ""
    return ""


def _wrl_f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        return float(default) if x != x else x
    except Exception:
        return float(default)


def _wrl_protected_symbols() -> set[str]:
    if not _wrl_env_bool("WATCHLIST_RECENT_LIQ_PROTECT_BYPASS", True):
        return set()
    try:
        from trading.ranking.active_symbols.protected import get_protected_symbols
        return {_wrl_norm_symbol(s) for s in (get_protected_symbols() or []) if _wrl_norm_symbol(s)}
    except Exception:
        return set()


def _wrl_recent_stats(symbol: str) -> dict[str, Any]:
    symbol = _wrl_norm_symbol(symbol)
    if not symbol:
        return {}

    ttl = max(0.0, _wrl_env_float("WATCHLIST_RECENT_LIQ_CACHE_TTL_SEC", 10.0))
    now_ts = dt.datetime.now().timestamp()
    cached = _wrl_cache.get(symbol)
    if cached and ttl > 0 and now_ts - cached[0] <= ttl:
        return dict(cached[1])

    path = _wrl_summary_db_path()
    table = os.getenv("WATCHLIST_RECENT_LIQ_SUMMARY_TABLE", "stock_summary_1min")
    bars = max(1, _wrl_env_int("WATCHLIST_RECENT_LIQ_BARS", 5))

    if not Path(path).exists():
        return {}

    try:
        with committing_connect(path, timeout=1.0) as conn:
            conn.execute("PRAGMA busy_timeout=1000")
            sym_col = _wrl_col(conn, table, ["symbol", "code", "stock_code"])
            tm_col = _wrl_col(conn, table, ["datetime", "dt", "timestamp", "time"])
            close_col = _wrl_col(conn, table, ["close_price", "close", "price", "current_price"])
            vol_col = _wrl_col(conn, table, ["volume", "Volume", "vol", "出来高"])
            turn_col = _wrl_col(conn, table, ["turnover", "turnover_yen", "trading_value", "売買代金"])
            if not sym_col or not tm_col or not vol_col:
                return {}

            select_close = close_col if close_col else "0"
            select_turn = turn_col if turn_col else "0"
            # symbol 列は TEXT NOT NULL (全行 text)。CAST(symbol AS TEXT) は
            # (symbol, datetime) のインデックスを殺して全表スキャンになり、
            # NAS 上の 4.8 万行で 1 銘柄 27〜58 秒かかっていた (symbol=? なら 0.2 秒)。
            # 2026-09-14 後場、100 銘柄の判定で PUSH 登録が 45 分以上止まった原因。
            sql = f"""
                SELECT {tm_col}, {select_close}, {vol_col}, {select_turn}
                FROM {table}
                WHERE {sym_col}=?
                ORDER BY {tm_col} DESC
                LIMIT ?
            """
            rows = conn.execute(sql, (symbol, bars)).fetchall()

        if not rows:
            return {}

        latest_dt = str(rows[0][0])
        latest_close = _wrl_f(rows[0][1], 0.0)
        volumes = [max(0.0, _wrl_f(r[2], 0.0)) for r in rows]
        latest_volume = volumes[0] if volumes else 0.0
        avg_volume = sum(volumes) / max(1, len(volumes))
        total_volume = sum(volumes)

        # summary DB の turnover 列はランキング由来の「当日累積」売買代金で、
        # 1分足ごとの値ではない (volume=0 の分でも非0、分をまたいでも同値)。
        # しかも enrich が届いた分しか入らず大半が NULL なので、これを bars 本
        # 合計すると「データがある銘柄ほど不利」という逆転が起きていた。
        # 直近 bars 本の売買代金は各足の close x volume で出す (常に円建て)。
        total_turnover = 0.0
        for r in rows:
            c = _wrl_f(r[1], 0.0)
            v = max(0.0, _wrl_f(r[2], 0.0))
            if c > 0 and v > 0:
                total_turnover += c * v
        if total_turnover <= 0 and latest_close > 0 and total_volume > 0:
            total_turnover = latest_close * total_volume

        # 当日累積の売買代金は別指標として残す (累積なので max を取る)。
        day_turnover = max((max(0.0, _wrl_f(r[3], 0.0)) for r in rows), default=0.0)

        stats = {
            "symbol": symbol,
            "latest_dt": latest_dt,
            "bars": len(rows),
            "close": latest_close,
            "latest_volume": latest_volume,
            "avg_volume": avg_volume,
            "total_volume": total_volume,
            "total_turnover": total_turnover,
            "day_turnover": day_turnover,
            "db": path,
        }
        _wrl_cache[symbol] = (now_ts, stats)
        return dict(stats)
    except Exception:
        logger.debug("[WATCHLIST RECENT LIQ] read failed symbol=%s path=%s", symbol, path, exc_info=True)
        return {}


def _wrl_ok(symbol: str, *, protected: set[str]) -> tuple[bool, str, dict[str, Any]]:
    symbol = _wrl_norm_symbol(symbol)
    if not symbol:
        return False, "NO_SYMBOL", {"symbol": symbol}

    if symbol in protected:
        return True, "PROTECTED", {"symbol": symbol, "protected": True}

    stats = _wrl_recent_stats(symbol)
    min_latest = _wrl_env_float("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", 3000.0)
    min_avg = _wrl_env_float("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", 3000.0)
    min_turnover = _wrl_env_float("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", 1_000_000.0)

    detail = {
        "symbol": symbol,
        **stats,
        "min_latest_volume": min_latest,
        "min_avg_volume": min_avg,
        "min_turnover": min_turnover,
    }

    if not stats:
        return False, "NO_RECENT_SUMMARY", detail
    if _wrl_f(stats.get("latest_volume"), 0.0) < min_latest:
        return False, "LATEST_VOLUME_LOW", detail
    if _wrl_f(stats.get("avg_volume"), 0.0) < min_avg:
        return False, "AVG_VOLUME_LOW", detail
    if _wrl_f(stats.get("total_turnover"), 0.0) < min_turnover:
        return False, "TURNOVER_LOW", detail
    return True, "OK", detail


def _wrl_filter_symbols(symbols: Iterable[Any], *, context: str) -> List[str]:
    items = _wrl_dedupe(symbols)
    if not _wrl_env_bool("WATCHLIST_RECENT_LIQ_ENABLED", True):
        return items

    protected = _wrl_protected_symbols()
    kept: List[str] = []
    skipped: List[dict[str, Any]] = []
    for s in items:
        ok, reason, detail = _wrl_ok(s, protected=protected)
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
            _wrl_env_float("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", 3000.0),
            _wrl_env_float("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", 3000.0),
            _wrl_env_float("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", 1_000_000.0),
        )
    return kept




def _wbk_as_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        return float(default) if x != x else x
    except Exception:
        return float(default)


def _wbk_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _wbk_env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _wbk_env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _wbk_norm_context(context: Any) -> str:
    try:
        return str(context or "").strip().lower()
    except Exception:
        return ""


def _wbk_is_push_rotation_context(context: Any) -> bool:
    c = _wbk_norm_context(context)
    return (
        c.startswith("push.rotation")
        or c.startswith("rotation")
        or "push_stream.rotation" in c
        or "apply_register_liquidity_guard" in c
    )


def _wbk_is_push_registration_source_context(context: Any) -> bool:
    """Contexts that feed PUSH registration targets before PUSH summary exists."""
    c = _wbk_norm_context(context)
    if _wbk_is_push_rotation_context(c):
        return True
    return c in {
        "active.get_active_symbols",
        "active.get_current_active_symbols",
        "active.get_monitor_symbols",
        "active.get_push_symbols",
        "active.get_register_symbols",
        "active.get_subscription_symbols",
        "active.get_rotation_symbols",
        "active.update_active_symbols",
    } or c.startswith("active.get_")


def _wbk_min_keep_for_registration(before: int) -> int:
    if before <= 0:
        return 0
    return max(
        1,
        min(
            int(before),
            max(
                _wbk_env_int("UC_PUSH_ROTATION_LIQ_FAILOPEN_MIN_KEEP", 50),
                _wbk_env_int("WATCHLIST_RECENT_LIQ_MIN_KEEP_ON_MISSING", 50),
            ),
        ),
    )


def _wbk_is_main_py_process() -> bool:
    try:
        argv = [str(x).replace("\\", "/").lower() for x in sys.argv]
        return any(x.endswith("/main.py") or x == "main.py" for x in argv)
    except Exception:
        return False


def _wbk_split_mode_says_main_should_skip_db_work() -> bool:
    try:
        from data_collectors.split_mode import should_skip_data_collector_work_in_main
        return bool(should_skip_data_collector_work_in_main())
    except Exception:
        return False


def _wbk_should_skip_db_read_in_main() -> bool:
    if _wbk_env_bool("WATCHLIST_RECENT_LIQ_BULK_RUN_IN_MAIN", False):
        return False
    if not _wbk_env_bool("WATCHLIST_RECENT_LIQ_BULK_SKIP_DB_IN_MAIN", True):
        return False
    if _wbk_split_mode_says_main_should_skip_db_work():
        return True
    if _wbk_is_main_py_process() and not _wbk_env_bool("AUTOSTOCK_DATA_COLLECTORS_PROCESS", False):
        return True
    return False


def _wbk_read_bulk_stats_sync(mod: Any, missing: List[str], symbols_total: int) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not missing:
        return out

    if _wbk_should_skip_db_read_in_main():
        logger.warning(
            "[WATCHLIST RECENT LIQ BULK] sync read blocked in main.py symbols=%d missing=%d reason=split_mode_main_memory_only",
            symbols_total,
            len(missing),
        )
        return out

    now_ts = dt.datetime.now().timestamp()
    path = _wrl_summary_db_path()
    table = os.getenv("WATCHLIST_RECENT_LIQ_SUMMARY_TABLE", "stock_summary_1min")
    bars = max(1, _wrl_env_int("WATCHLIST_RECENT_LIQ_BARS", 5))
    limit_rows = max(len(missing) * max(bars, 5) * 3, len(missing) * bars)

    if not Path(path).exists():
        logger.warning("[WATCHLIST RECENT LIQ BULK] summary db not found fail-open path=%s", path)
        return out

    t0 = time.monotonic()
    deadline = t0 + max(0.2, _wbk_env_float("WATCHLIST_RECENT_LIQ_BULK_SQL_HARD_TIMEOUT_SEC", 2.0))

    with committing_connect(path, timeout=0.25) as conn:
        conn.execute("PRAGMA busy_timeout=250")

        def _progress_handler() -> int:
            return 1 if time.monotonic() > deadline else 0

        try:
            conn.set_progress_handler(_progress_handler, 10_000)
        except Exception:
            pass

        sym_col = _wrl_col(conn, table, ["symbol", "code", "stock_code"])
        tm_col = _wrl_col(conn, table, ["datetime", "dt", "timestamp", "time"])
        close_col = _wrl_col(conn, table, ["close_price", "close", "price", "current_price"])
        vol_col = _wrl_col(conn, table, ["volume", "Volume", "vol", "出来高"])
        turn_col = _wrl_col(conn, table, ["turnover", "turnover_yen", "trading_value", "売買代金"])
        if not sym_col or not tm_col or not vol_col:
            logger.warning(
                "[WATCHLIST RECENT LIQ BULK] missing columns table=%s sym=%s tm=%s vol=%s path=%s",
                table, sym_col, tm_col, vol_col, path,
            )
            return out

        select_close = close_col if close_col else "0"
        select_turn = turn_col if turn_col else "0"
        placeholders = ",".join(["?"] * len(missing))
        sql = f"""
            SELECT {sym_col} AS symbol, {tm_col}, {select_close}, {vol_col}, {select_turn}
            FROM {table}
            WHERE {sym_col} IN ({placeholders})
            ORDER BY {sym_col}, {tm_col} DESC
            LIMIT ?
        """
        try:
            rows = conn.execute(sql, tuple(missing) + (int(limit_rows),)).fetchall()
        except sqlite3.OperationalError as e:
            if "interrupted" in str(e).lower():
                logger.warning(
                    "[WATCHLIST RECENT LIQ BULK] SQL interrupted hard-timeout fail-open symbols=%d missing=%d timeout=%.2fs path=%s",
                    symbols_total,
                    len(missing),
                    _wbk_env_float("WATCHLIST_RECENT_LIQ_BULK_SQL_HARD_TIMEOUT_SEC", 2.0),
                    path,
                )
                return out
            raise
        finally:
            try:
                conn.set_progress_handler(None, 0)
            except Exception:
                pass

    grouped: dict[str, list[tuple[Any, Any, Any, Any]]] = {s: [] for s in missing}
    for r in rows or []:
        s = _wrl_norm_symbol(r[0])
        if s in grouped and len(grouped[s]) < bars:
            grouped[s].append((r[1], r[2], r[3], r[4]))

    for s, rs in grouped.items():
        if not rs:
            continue
        latest_dt = str(rs[0][0])
        latest_close = _wbk_as_float(rs[0][1], 0.0)
        volumes = [max(0.0, _wbk_as_float(r[2], 0.0)) for r in rs]
        latest_volume = volumes[0] if volumes else 0.0
        avg_volume = sum(volumes) / max(1, len(volumes))
        total_volume = sum(volumes)

        # 単体版 (_wrl_stats) と同じ理由で、直近 bars 本の売買代金は
        # 各足の close x volume から出す。turnover 列は当日累積なので合計しない。
        total_turnover = 0.0
        for r in rs:
            c = _wbk_as_float(r[1], 0.0)
            v = max(0.0, _wbk_as_float(r[2], 0.0))
            if c > 0 and v > 0:
                total_turnover += c * v
        if total_turnover <= 0 and latest_close > 0 and total_volume > 0:
            total_turnover = latest_close * total_volume

        day_turnover = max((max(0.0, _wbk_as_float(r[3], 0.0)) for r in rs), default=0.0)

        stats = {
            "symbol": s,
            "latest_dt": latest_dt,
            "bars": len(rs),
            "close": latest_close,
            "latest_volume": latest_volume,
            "avg_volume": avg_volume,
            "total_volume": total_volume,
            "total_turnover": total_turnover,
            "day_turnover": day_turnover,
            "db": path,
            "bulk": True,
        }
        _wrl_cache[s] = (now_ts, stats)
        out[s] = dict(stats)

    elapsed = time.monotonic() - t0
    if elapsed >= _wrl_env_float("WATCHLIST_RECENT_LIQ_BULK_LOG_SEC", 0.5):
        logger.warning(
            "[WATCHLIST RECENT LIQ BULK] read done symbols=%d missing=%d hit=%d rows=%d elapsed=%.3fs path=%s",
            symbols_total, len(missing), len(out), len(rows or []), elapsed, path,
        )
    else:
        logger.info(
            "[WATCHLIST RECENT LIQ BULK] read done symbols=%d missing=%d hit=%d elapsed=%.3fs",
            symbols_total, len(missing), len(out), elapsed,
        )
    return out


def _wbk_bulk_stats(mod: Any, symbols: List[str]) -> tuple[dict[str, dict[str, Any]], bool]:
    """Return (stats_map, timed_out_or_skipped)."""
    symbols = _wrl_dedupe(symbols)
    if not symbols:
        return {}, False

    now_ts = dt.datetime.now().timestamp()
    ttl = max(0.0, _wrl_env_float("WATCHLIST_RECENT_LIQ_CACHE_TTL_SEC", 10.0))
    out: dict[str, dict[str, Any]] = {}
    missing: List[str] = []

    for s in symbols:
        cached = _wrl_cache.get(s)
        if cached and ttl > 0 and now_ts - cached[0] <= ttl:
            out[s] = dict(cached[1])
        else:
            missing.append(s)

    if not missing:
        return out, False

    if _wbk_should_skip_db_read_in_main():
        logger.warning(
            "[WATCHLIST RECENT LIQ BULK] DB read HARD-SKIPPED in main.py fail-open symbols=%d missing=%d split_mode_skip=%s argv_main=%s set WATCHLIST_RECENT_LIQ_BULK_RUN_IN_MAIN=1 to force",
            len(symbols),
            len(missing),
            _wbk_split_mode_says_main_should_skip_db_work(),
            _wbk_is_main_py_process(),
        )
        return out, True

    timeout_sec = max(0.0, _wbk_env_float("WATCHLIST_RECENT_LIQ_BULK_TIMEOUT_SEC", 1.5))
    if timeout_sec <= 0:
        try:
            out.update(_wbk_read_bulk_stats_sync(mod, missing, len(symbols)))
        except Exception as e:
            logger.warning("[WATCHLIST RECENT LIQ BULK] read failed fail-open err=%s", e, exc_info=False)
        return out, False

    box: dict[str, Any] = {"done": False, "result": {}, "error": None}

    def _worker() -> None:
        try:
            box["result"] = _wbk_read_bulk_stats_sync(mod, missing, len(symbols))
        except Exception as e:
            box["error"] = e
        finally:
            box["done"] = True

    th = threading.Thread(target=_worker, name="watchlist-recent-liq-bulk-read", daemon=True)
    th.start()
    th.join(timeout_sec)

    if th.is_alive():
        logger.warning(
            "[WATCHLIST RECENT LIQ BULK] timeout fail-open symbols=%d missing=%d timeout=%.2fs hard_timeout=%.2fs",
            len(symbols), len(missing), timeout_sec,
            _wbk_env_float("WATCHLIST_RECENT_LIQ_BULK_SQL_HARD_TIMEOUT_SEC", 2.0),
        )
        return out, True

    if box.get("error") is not None:
        logger.warning("[WATCHLIST RECENT LIQ BULK] read failed fail-open err=%s", box.get("error"), exc_info=False)
        return out, False

    try:
        out.update(box.get("result") or {})
    except Exception:
        pass
    return out, False


def _wbk_filter_symbols_bulk(symbols: Iterable[Any], *, context: str) -> List[str]:
    items = _wrl_dedupe(symbols)
    if not _wrl_env_bool("WATCHLIST_RECENT_LIQ_ENABLED", True):
        return items

    # PUSH登録直前は summary DB が未作成/未保存でも正常。
    # rotation context は無条件で元リストを維持する。
    if _wbk_is_push_rotation_context(context):
        logger.warning(
            "[WATCHLIST RECENT LIQ BULK] push rotation context=%s -> fail-open keep original count=%s reason=summary_not_available_before_registration",
            context,
            len(items),
        )
        return items

    protected = _wrl_protected_symbols()
    protected_items = [s for s in items if s in protected]
    # 旧 patch では対象モジュールを mod として受け取っていた。本文化で同居した
    # のに引数の書き換えが漏れて未定義参照になっていたので自モジュールを渡す。
    stats_map, timed_out = _wbk_bulk_stats(
        sys.modules[__name__], [s for s in items if s not in protected]
    )

    if timed_out and _wrl_env_bool("WATCHLIST_RECENT_LIQ_FAIL_OPEN_ON_TIMEOUT", True):
        logger.warning(
            "[WATCHLIST RECENT LIQ BULK] fail-open context=%s count=%s reason=bulk_timeout_or_main_skip",
            context, len(items),
        )
        return items

    kept: List[str] = []
    skipped: List[dict[str, Any]] = []
    missing_count = 0
    min_latest = _wrl_env_float("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", 3000.0)
    min_avg = _wrl_env_float("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", 3000.0)
    min_turnover = _wrl_env_float("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", 1_000_000.0)

    for s in items:
        if s in protected:
            kept.append(s)
            continue
        st = stats_map.get(s) or {}
        detail = {
            "symbol": s,
            **st,
            "min_latest_volume": min_latest,
            "min_avg_volume": min_avg,
            "min_turnover": min_turnover,
        }
        if not st:
            missing_count += 1
            skipped.append({"reason": "NO_RECENT_SUMMARY", **detail})
        elif _wbk_as_float(st.get("latest_volume"), 0.0) < min_latest:
            skipped.append({"reason": "LATEST_VOLUME_LOW", **detail})
        elif _wbk_as_float(st.get("avg_volume"), 0.0) < min_avg:
            skipped.append({"reason": "AVG_VOLUME_LOW", **detail})
        elif _wbk_as_float(st.get("total_turnover"), 0.0) < min_turnover:
            skipped.append({"reason": "TURNOVER_LOW", **detail})
        else:
            kept.append(s)

    # active.get_* は PUSH登録母集団にも使われる。
    # 今日のsummary未作成で50件未満に縮退する場合は、元候補を維持する。
    if _wbk_is_push_registration_source_context(context) and items:
        min_keep = _wbk_min_keep_for_registration(len(items))
        mostly_missing = missing_count >= max(1, int(len(items) * _wbk_env_float("WATCHLIST_RECENT_LIQ_MISSING_FAILOPEN_RATIO", 0.50)))
        if len(kept) < min_keep and mostly_missing:
            logger.warning(
                "[WATCHLIST RECENT LIQ BULK] registration source fail-open context=%s before=%s after=%s missing=%s min_keep=%s -> keep original targets reason=NO_RECENT_SUMMARY_BEFORE_PUSH",
                context,
                len(items),
                len(kept),
                missing_count,
                min_keep,
            )
            return items

    if skipped:
        logger.warning(
            "[WATCHLIST RECENT LIQ BULK] filtered context=%s before=%s after=%s protected=%s skipped=%s",
            context, len(items), len(kept), len(protected_items), skipped[:80],
        )
    else:
        logger.info(
            "[WATCHLIST RECENT LIQ BULK] passed context=%s count=%s protected=%s",
            context, len(kept), len(protected_items),
        )
    return kept


def _uc_norm_symbol(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _uc_dedupe_symbols(values: Iterable[Any]) -> list[str]:
    out = []
    seen = set()

    for x in values or []:
        s = _uc_norm_symbol(x)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)

    return out


def _uc_is_push_rotation_context(context: str) -> bool:
    c = str(context or "").lower()
    return "push.rotation" in c or "apply_register_liquidity_guard" in c or "push_stream.rotation" in c


# ------------------------------------------------------------
# 3 層の合成 (旧 usercustomize._uc_patch_watchlist_recent_liq_rotation_failopen)
# ------------------------------------------------------------


def _bulk_scope_enabled() -> bool:
    """bulk / uc 層は usercustomize から main.py プロセス限定で入っていた。"""
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        return "main.py" in argv and "main_database.py" not in argv
    except Exception:
        return False


def _env_int_uc(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def filter_symbols(symbols: Iterable[Any], *, context: str) -> List[str]:
    """3 層合成後の公開 API。"""
    if not _bulk_scope_enabled():
        return _wrl_filter_symbols(symbols, context=context)

    items = _uc_dedupe_symbols(symbols)
    out = _wbk_filter_symbols_bulk(items, context=context)
    out_items = _uc_dedupe_symbols(out)

    if _uc_is_push_rotation_context(context) and items:
        min_keep = max(1, min(len(items), _env_int_uc("UC_PUSH_ROTATION_LIQ_FAILOPEN_MIN_KEEP", 50)))
        if len(out_items) < min_keep:
            logger.warning(
                "[USERCUSTOMIZE][PUSH ROTATION LIQ FAILOPEN] context=%s before=%s after=%s min_keep=%s -> keep original targets",
                context, len(items), len(out_items), min_keep,
            )
            return items
    return out_items


def apply_failopen_min_keep(items: Sequence[str], out: Sequence[str]) -> List[str]:
    """apply_register_liquidity_guard 用の min_keep fail-open (旧 uc の apply ラッパー)。"""
    items = _uc_dedupe_symbols(items)
    out_items = _uc_dedupe_symbols(out)
    if not _bulk_scope_enabled() or not items:
        return out_items
    min_keep = max(1, min(len(items), _env_int_uc("UC_PUSH_ROTATION_LIQ_FAILOPEN_MIN_KEEP", 50)))
    if len(out_items) < min_keep:
        logger.warning(
            "[USERCUSTOMIZE][PUSH ROTATION APPLY FAILOPEN] before=%s after=%s min_keep=%s -> keep original targets",
            len(items), len(out_items), min_keep,
        )
        return list(items)
    return out_items
