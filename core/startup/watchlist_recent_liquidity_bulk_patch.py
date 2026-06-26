# ============================================================
# File   : core/startup/watchlist_recent_liquidity_bulk_patch.py
# Version: V1.5-PUSH-REGISTRATION-RECENT-LIQ-FAIL-OPEN
# ------------------------------------------------------------
# watchlist_recent_liquidity_guard_patch の per-symbol SQLite 読取を
# 1回のbulk読取に差し替える。
#
# V1.5:
#   - active.get_* / active.update_active_symbols 経由で PUSH 登録母集団を作る時も、
#     NO_RECENT_SUMMARY だけで50件未満へ縮退させない。
#   - PUSH登録前は「今日のPUSH summaryが無い」のが正常なので、ここで削ると
#     PUSH未登録 -> summary無し -> 除外 -> 未登録 の循環になる。
#   - 登録前の候補は最低 UC_PUSH_ROTATION_LIQ_FAILOPEN_MIN_KEEP
#     または WATCHLIST_RECENT_LIQ_MIN_KEEP_ON_MISSING 件を維持する。
#   - 低流動性の最終除外は entry 側の最終ガードに残す。
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
from typing import Any, Iterable, List

logger = logging.getLogger(__name__)
_INSTALLED = False


def _as_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        return float(default) if x != x else x
    except Exception:
        return float(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _norm_context(context: Any) -> str:
    try:
        return str(context or "").strip().lower()
    except Exception:
        return ""


def _is_push_rotation_context(context: Any) -> bool:
    c = _norm_context(context)
    return (
        c.startswith("push.rotation")
        or c.startswith("rotation")
        or "push_stream.rotation" in c
        or "apply_register_liquidity_guard" in c
    )


def _is_push_registration_source_context(context: Any) -> bool:
    """Contexts that feed PUSH registration targets before PUSH summary exists."""
    c = _norm_context(context)
    if _is_push_rotation_context(c):
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


def _min_keep_for_registration(before: int) -> int:
    if before <= 0:
        return 0
    return max(
        1,
        min(
            int(before),
            max(
                _env_int("UC_PUSH_ROTATION_LIQ_FAILOPEN_MIN_KEEP", 50),
                _env_int("WATCHLIST_RECENT_LIQ_MIN_KEEP_ON_MISSING", 50),
            ),
        ),
    )


def _is_main_py_process() -> bool:
    try:
        argv = [str(x).replace("\\", "/").lower() for x in sys.argv]
        return any(x.endswith("/main.py") or x == "main.py" for x in argv)
    except Exception:
        return False


def _split_mode_says_main_should_skip_db_work() -> bool:
    try:
        from data_collectors.split_mode import should_skip_data_collector_work_in_main
        return bool(should_skip_data_collector_work_in_main())
    except Exception:
        return False


def _should_skip_db_read_in_main() -> bool:
    if _env_bool("WATCHLIST_RECENT_LIQ_BULK_RUN_IN_MAIN", False):
        return False
    if not _env_bool("WATCHLIST_RECENT_LIQ_BULK_SKIP_DB_IN_MAIN", True):
        return False
    if _split_mode_says_main_should_skip_db_work():
        return True
    if _is_main_py_process() and not _env_bool("AUTOSTOCK_DATA_COLLECTORS_PROCESS", False):
        return True
    return False


def _read_bulk_stats_sync(mod: Any, missing: List[str], symbols_total: int) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    if not missing:
        return out

    if _should_skip_db_read_in_main():
        logger.warning(
            "[WATCHLIST RECENT LIQ BULK] sync read blocked in main.py symbols=%d missing=%d reason=split_mode_main_memory_only",
            symbols_total,
            len(missing),
        )
        return out

    now_ts = dt.datetime.now().timestamp()
    path = mod._summary_db_path()
    table = os.getenv("WATCHLIST_RECENT_LIQ_SUMMARY_TABLE", "stock_summary_1min")
    bars = max(1, mod._env_int("WATCHLIST_RECENT_LIQ_BARS", 5))
    limit_rows = max(len(missing) * max(bars, 5) * 3, len(missing) * bars)

    if not Path(path).exists():
        logger.warning("[WATCHLIST RECENT LIQ BULK] summary db not found fail-open path=%s", path)
        return out

    t0 = time.monotonic()
    deadline = t0 + max(0.2, _env_float("WATCHLIST_RECENT_LIQ_BULK_SQL_HARD_TIMEOUT_SEC", 2.0))

    with sqlite3.connect(path, timeout=0.25) as conn:
        conn.execute("PRAGMA busy_timeout=250")

        def _progress_handler() -> int:
            return 1 if time.monotonic() > deadline else 0

        try:
            conn.set_progress_handler(_progress_handler, 10_000)
        except Exception:
            pass

        sym_col = mod._col(conn, table, ["symbol", "code", "stock_code"])
        tm_col = mod._col(conn, table, ["datetime", "dt", "timestamp", "time"])
        close_col = mod._col(conn, table, ["close_price", "close", "price", "current_price"])
        vol_col = mod._col(conn, table, ["volume", "Volume", "vol", "出来高"])
        turn_col = mod._col(conn, table, ["turnover", "turnover_yen", "trading_value", "売買代金"])
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
            SELECT CAST({sym_col} AS TEXT) AS symbol, {tm_col}, {select_close}, {vol_col}, {select_turn}
            FROM {table}
            WHERE CAST({sym_col} AS TEXT) IN ({placeholders})
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
                    _env_float("WATCHLIST_RECENT_LIQ_BULK_SQL_HARD_TIMEOUT_SEC", 2.0),
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
        s = mod._norm_symbol(r[0])
        if s in grouped and len(grouped[s]) < bars:
            grouped[s].append((r[1], r[2], r[3], r[4]))

    for s, rs in grouped.items():
        if not rs:
            continue
        latest_dt = str(rs[0][0])
        latest_close = _as_float(rs[0][1], 0.0)
        volumes = [max(0.0, _as_float(r[2], 0.0)) for r in rs]
        turnovers = [max(0.0, _as_float(r[3], 0.0)) for r in rs]
        latest_volume = volumes[0] if volumes else 0.0
        avg_volume = sum(volumes) / max(1, len(volumes))
        total_volume = sum(volumes)
        total_turnover = sum(turnovers)
        if total_turnover <= 0 and latest_close > 0 and total_volume > 0:
            total_turnover = latest_close * total_volume
        stats = {
            "symbol": s,
            "latest_dt": latest_dt,
            "bars": len(rs),
            "close": latest_close,
            "latest_volume": latest_volume,
            "avg_volume": avg_volume,
            "total_volume": total_volume,
            "total_turnover": total_turnover,
            "db": path,
            "bulk": True,
        }
        mod._CACHE[s] = (now_ts, stats)
        out[s] = dict(stats)

    elapsed = time.monotonic() - t0
    if elapsed >= mod._env_float("WATCHLIST_RECENT_LIQ_BULK_LOG_SEC", 0.5):
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


def _bulk_stats(mod: Any, symbols: List[str]) -> tuple[dict[str, dict[str, Any]], bool]:
    """Return (stats_map, timed_out_or_skipped)."""
    symbols = mod._dedupe(symbols)
    if not symbols:
        return {}, False

    now_ts = dt.datetime.now().timestamp()
    ttl = max(0.0, mod._env_float("WATCHLIST_RECENT_LIQ_CACHE_TTL_SEC", 10.0))
    out: dict[str, dict[str, Any]] = {}
    missing: List[str] = []

    for s in symbols:
        cached = mod._CACHE.get(s)
        if cached and ttl > 0 and now_ts - cached[0] <= ttl:
            out[s] = dict(cached[1])
        else:
            missing.append(s)

    if not missing:
        return out, False

    if _should_skip_db_read_in_main():
        logger.warning(
            "[WATCHLIST RECENT LIQ BULK] DB read HARD-SKIPPED in main.py fail-open symbols=%d missing=%d split_mode_skip=%s argv_main=%s set WATCHLIST_RECENT_LIQ_BULK_RUN_IN_MAIN=1 to force",
            len(symbols),
            len(missing),
            _split_mode_says_main_should_skip_db_work(),
            _is_main_py_process(),
        )
        return out, True

    timeout_sec = max(0.0, _env_float("WATCHLIST_RECENT_LIQ_BULK_TIMEOUT_SEC", 1.5))
    if timeout_sec <= 0:
        try:
            out.update(_read_bulk_stats_sync(mod, missing, len(symbols)))
        except Exception as e:
            logger.warning("[WATCHLIST RECENT LIQ BULK] read failed fail-open err=%s", e, exc_info=False)
        return out, False

    box: dict[str, Any] = {"done": False, "result": {}, "error": None}

    def _worker() -> None:
        try:
            box["result"] = _read_bulk_stats_sync(mod, missing, len(symbols))
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
            _env_float("WATCHLIST_RECENT_LIQ_BULK_SQL_HARD_TIMEOUT_SEC", 2.0),
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


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from core.startup import watchlist_recent_liquidity_guard_patch as mod
    except Exception as e:
        logger.warning("[WATCHLIST RECENT LIQ BULK] base patch import failed err=%s", e, exc_info=False)
        return False

    def _filter_symbols_bulk(symbols: Iterable[Any], *, context: str) -> List[str]:
        items = mod._dedupe(symbols)
        if not mod._env_bool("WATCHLIST_RECENT_LIQ_ENABLED", True):
            return items

        # PUSH登録直前は summary DB が未作成/未保存でも正常。
        # rotation context は無条件で元リストを維持する。
        if _is_push_rotation_context(context):
            logger.warning(
                "[WATCHLIST RECENT LIQ BULK] push rotation context=%s -> fail-open keep original count=%s reason=summary_not_available_before_registration",
                context,
                len(items),
            )
            return items

        protected = mod._protected_symbols()
        protected_items = [s for s in items if s in protected]
        stats_map, timed_out = _bulk_stats(mod, [s for s in items if s not in protected])

        if timed_out and mod._env_bool("WATCHLIST_RECENT_LIQ_FAIL_OPEN_ON_TIMEOUT", True):
            logger.warning(
                "[WATCHLIST RECENT LIQ BULK] fail-open context=%s count=%s reason=bulk_timeout_or_main_skip",
                context, len(items),
            )
            return items

        kept: List[str] = []
        skipped: List[dict[str, Any]] = []
        missing_count = 0
        min_latest = mod._env_float("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", 3000.0)
        min_avg = mod._env_float("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", 3000.0)
        min_turnover = mod._env_float("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", 1_000_000.0)

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
            elif _as_float(st.get("latest_volume"), 0.0) < min_latest:
                skipped.append({"reason": "LATEST_VOLUME_LOW", **detail})
            elif _as_float(st.get("avg_volume"), 0.0) < min_avg:
                skipped.append({"reason": "AVG_VOLUME_LOW", **detail})
            elif _as_float(st.get("total_turnover"), 0.0) < min_turnover:
                skipped.append({"reason": "TURNOVER_LOW", **detail})
            else:
                kept.append(s)

        # active.get_* は PUSH登録母集団にも使われる。
        # 今日のsummary未作成で50件未満に縮退する場合は、元候補を維持する。
        if _is_push_registration_source_context(context) and items:
            min_keep = _min_keep_for_registration(len(items))
            mostly_missing = missing_count >= max(1, int(len(items) * _env_float("WATCHLIST_RECENT_LIQ_MISSING_FAILOPEN_RATIO", 0.50)))
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

    mod._filter_symbols = _filter_symbols_bulk
    _INSTALLED = True
    logger.warning(
        "[WATCHLIST RECENT LIQ BULK] installed v1.5 push_registration_fail_open=1 timeout=%.2fs hard_timeout=%.2fs fail_open=%s skip_db_in_main=%s split_mode_skip=%s argv_main=%s min_keep=%s",
        _env_float("WATCHLIST_RECENT_LIQ_BULK_TIMEOUT_SEC", 1.5),
        _env_float("WATCHLIST_RECENT_LIQ_BULK_SQL_HARD_TIMEOUT_SEC", 2.0),
        mod._env_bool("WATCHLIST_RECENT_LIQ_FAIL_OPEN_ON_TIMEOUT", True),
        _should_skip_db_read_in_main(),
        _split_mode_says_main_should_skip_db_work(),
        _is_main_py_process(),
        _min_keep_for_registration(100),
    )
    return True


try:
    install()
except Exception as e:
    logger.warning("[WATCHLIST RECENT LIQ BULK] auto install failed err=%s", e, exc_info=False)

__all__ = ["install"]
