# ============================================================
# File   : core/startup/entry_reentry_stale_429_exit_safety_patch.py
# Version: V1.0-REENTRY-STALE-429-EXIT-SAFETY
# ------------------------------------------------------------
# 目的:
#   1. 未約定/キャンセルの entry_sent_count だけで同一銘柄を止めない
#   2. スキャルピング向けに同一銘柄の当日再エントリー余地を広げる
#   3. PUSH stale 時はランキング由来サマリー代替を許可するための runtime flags を立てる
#   4. ランキングAPI 429 は直近成功結果を再利用し、連続リクエストを冷却する
#   5. EXIT empty fast skip 中も broker 建玉確認を間引き過ぎない設定にする
# ============================================================

from __future__ import annotations

import datetime as dt
import functools
import importlib
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Tuple

logger = logging.getLogger(__name__)
_INSTALLED = False

_RANKING_429_CACHE: Dict[str, Any] = {}
_RANKING_429_COOLDOWN_UNTIL: Dict[str, float] = {}
_EXIT_CALL_COUNT = 0


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _today() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _raise_int_env_min(name: str, minimum: int) -> None:
    cur = _env_int(name, minimum)
    if cur < minimum:
        old = os.getenv(name)
        os.environ[name] = str(int(minimum))
        logger.warning("[REENTRY SAFETY] env raised %s %s->%s", name, old, minimum)


def _install_env_defaults() -> None:
    # 同一銘柄を「注文送信済みだけ」で止めない。実際の返済済み回数を基準にする。
    if not _env_bool("ENTRY_KEEP_SENT_COUNT_LIMIT", False):
        old = os.getenv("ENTRY_COUNT_SENT_ORDER_AS_DAILY_ENTRY")
        os.environ["ENTRY_COUNT_SENT_ORDER_AS_DAILY_ENTRY"] = "0"
        logger.warning("[REENTRY SAFETY] env force ENTRY_COUNT_SENT_ORDER_AS_DAILY_ENTRY %s->0", old)

    _raise_int_env_min("ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL", 4)
    _raise_int_env_min("ENTRY_WINNING_SYMBOL_MAX_DAILY_ENTRIES", 6)
    os.environ.setdefault("ENTRY_WINNING_SYMBOL_REENTRY_ENABLED", "1")
    os.environ.setdefault("ENTRY_WINNING_SYMBOL_IGNORE_SENT_ONLY", "1")
    os.environ.setdefault("ENTRY_STOP_AFTER_FIRST_LOSS_ONLY_IF_NET_NEGATIVE", "1")

    # PUSH が古い時にランキング由来の最新サマリーへ逃がすための共通フラグ。
    os.environ.setdefault("ENTRY_ALLOW_RANKING_FALLBACK_WHEN_PUSH_STALE", "1")
    os.environ.setdefault("SUMMARY_ALLOW_RANKING_FALLBACK_WHEN_PUSH_STALE", "1")
    os.environ.setdefault("TONOSAMA_ALLOW_RANKING_FALLBACK_WHEN_PUSH_STALE", "1")
    os.environ.setdefault("RANKING_SUMMARY_FALLBACK_MAX_AGE_SEC", "360")

    # ランキングAPI 429 対策。
    os.environ.setdefault("RANKING_API_429_COOLDOWN_SEC", "30")
    os.environ.setdefault("RANKING_API_429_USE_LAST_SUCCESS", "1")

    # EXIT empty fast skip が続いても broker 建玉確認を間引き過ぎない。
    os.environ.setdefault("EXIT_EMPTY_FAST_FORCE_BROKER_EVERY_N", "5")
    os.environ.setdefault("OPEN_POSITION_EMPTY_THROTTLE_SEC", "2.0")


def _trade_guard_db_path() -> str:
    base = os.getenv(
        "TRADE_GUARD_DB_DIR",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\trade_guard",
    )
    return os.getenv("TRADE_GUARD_DB_PATH", str(Path(base) / f"trade_guard{_today()}.db"))


def _reset_sent_only_counts() -> bool:
    if not _env_bool("ENTRY_DAILY_RISK_ZERO_SENT_COUNT_ON_START", True):
        return False
    try:
        path = _trade_guard_db_path()
        if not path or not Path(path).exists():
            return False
        with sqlite3.connect(path, timeout=3.0) as conn:
            conn.execute("PRAGMA busy_timeout=3000")
            cur1 = conn.execute(
                "UPDATE symbol_daily_entry_risk SET entry_sent_count=0, updated_at=? WHERE trade_date=? AND COALESCE(entry_count,0)=0 AND COALESCE(entry_sent_count,0)>0",
                (dt.datetime.now().replace(microsecond=0).isoformat(sep=" "), _today()),
            )
            cur2 = conn.execute(
                "UPDATE global_daily_entry_risk SET entry_sent_count=0, updated_at=? WHERE trade_date=? AND COALESCE(trade_count,0)=0 AND COALESCE(entry_sent_count,0)>0",
                (dt.datetime.now().replace(microsecond=0).isoformat(sep=" "), _today()),
            )
            conn.commit()
        logger.warning(
            "[REENTRY SAFETY] reset sent-only counts path=%s symbol_rows=%s global_rows=%s",
            path,
            getattr(cur1, "rowcount", None),
            getattr(cur2, "rowcount", None),
        )
        return True
    except Exception:
        logger.exception("[REENTRY SAFETY] reset sent-only counts failed")
        return False


def _install_daily_risk_reentry_patch() -> bool:
    try:
        dr = importlib.import_module("core.startup.entry_daily_risk_runtime_patch")
    except Exception:
        logger.exception("[REENTRY SAFETY] daily risk module import failed")
        return False

    if getattr(dr, "_reentry_sent_only_ignored_v10", False):
        return True

    def _record_entry_sent_noop(symbol: Any) -> None:
        logger.warning("[REENTRY SAFETY] entry_sent not counted symbol=%s", symbol)

    original_record_entry_sent = getattr(dr, "_record_entry_sent", None)
    if callable(original_record_entry_sent) and not _env_bool("ENTRY_KEEP_SENT_COUNT_LIMIT", False):
        dr._record_entry_sent = _record_entry_sent_noop

    def _risk_block_reason_reentry(symbol: str, side: str) -> Tuple[bool, str, Dict[str, Any]]:
        symbol = dr._norm_symbol(symbol)
        side_u = str(side or "").upper()
        if side_u == "BUY" and not dr._env_bool("ENTRY_BUY_ENABLED", True):
            return True, "BUY_DISABLED_BY_DAILY_RISK_PATCH", {"symbol": symbol, "side": side_u}

        srow = dr._get_symbol_row(symbol)
        grow = dr._get_global_row()

        max_entries = dr._env_int("ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL", 4)
        winning_max_entries = dr._env_int("ENTRY_WINNING_SYMBOL_MAX_DAILY_ENTRIES", 6)
        symbol_max_loss = dr._env_float("ENTRY_SYMBOL_MAX_DAILY_LOSS_YEN", -1500.0)
        stop_after_first_loss = dr._env_bool("ENTRY_STOP_SYMBOL_AFTER_FIRST_LOSS", True)
        stop_after_first_loss_only_net_negative = dr._env_bool("ENTRY_STOP_AFTER_FIRST_LOSS_ONLY_IF_NET_NEGATIVE", True)
        global_max_loss = dr._env_float("ENTRY_GLOBAL_MAX_DAILY_LOSS_YEN", -50000.0)
        global_max_trades = dr._env_int("ENTRY_GLOBAL_MAX_DAILY_TRADES", 30)
        global_max_consec_losses = dr._env_int("ENTRY_GLOBAL_MAX_CONSECUTIVE_LOSSES", 20)

        entry_count = int(srow.get("entry_count") or 0)
        entry_sent_count = int(srow.get("entry_sent_count") or 0)
        loss_count = int(srow.get("loss_count") or 0)
        daily_pnl = float(srow.get("daily_pnl") or 0.0)
        winning_symbol = dr._is_winning_symbol(srow)

        if not dr._env_bool("ENTRY_COUNT_SENT_ORDER_AS_DAILY_ENTRY", False):
            symbol_seen_entries = entry_count
            count_mode = "actual_entry_count_only_sent_ignored"
        elif winning_symbol and dr._env_bool("ENTRY_WINNING_SYMBOL_IGNORE_SENT_ONLY", True):
            symbol_seen_entries = entry_count
            count_mode = "actual_entry_count_only_for_winning_symbol"
        else:
            symbol_seen_entries = max(entry_count, entry_sent_count)
            count_mode = "max_entry_count_or_sent_count"

        effective_max_entries = max_entries
        if winning_symbol:
            effective_max_entries = max(max_entries, winning_max_entries)

        if global_max_trades > 0 and int(grow.get("trade_count") or 0) >= global_max_trades:
            return True, "GLOBAL_DAILY_TRADE_LIMIT", {"symbol": symbol, "side": side_u, "max_trades": global_max_trades, **grow}
        if float(grow.get("daily_pnl") or 0.0) <= global_max_loss:
            return True, "GLOBAL_DAILY_LOSS_LIMIT", {"symbol": symbol, "side": side_u, "max_loss": global_max_loss, **grow}
        if global_max_consec_losses > 0 and int(grow.get("consecutive_losses") or 0) >= global_max_consec_losses:
            return True, "GLOBAL_CONSECUTIVE_LOSS_LIMIT", {"symbol": symbol, "side": side_u, "max_consecutive_losses": global_max_consec_losses, **grow}

        if stop_after_first_loss and loss_count >= 1:
            if not (stop_after_first_loss_only_net_negative and winning_symbol and daily_pnl > 0):
                return True, "SYMBOL_STOP_AFTER_FIRST_LOSS", {
                    "symbol": symbol,
                    "side": side_u,
                    "winning_symbol": winning_symbol,
                    "stop_after_first_loss_only_net_negative": stop_after_first_loss_only_net_negative,
                    **srow,
                }

        if float(srow.get("daily_pnl") or 0.0) <= symbol_max_loss:
            return True, "SYMBOL_DAILY_LOSS_LIMIT", {"symbol": symbol, "side": side_u, "max_loss": symbol_max_loss, **srow}

        if effective_max_entries > 0 and symbol_seen_entries >= effective_max_entries:
            return True, "SYMBOL_DAILY_ENTRY_LIMIT", {
                "symbol": symbol,
                "side": side_u,
                "max_entries": effective_max_entries,
                "base_max_entries": max_entries,
                "winning_max_entries": winning_max_entries,
                "winning_symbol": winning_symbol,
                "symbol_seen_entries": symbol_seen_entries,
                "count_mode": count_mode,
                **srow,
            }

        return False, "", {
            "symbol": srow,
            "global": grow,
            "winning_symbol": winning_symbol,
            "effective_max_entries": effective_max_entries,
            "symbol_seen_entries": symbol_seen_entries,
            "count_mode": count_mode,
        }

    dr._risk_block_reason = _risk_block_reason_reentry
    dr._reentry_sent_only_ignored_v10 = True
    logger.warning(
        "[REENTRY SAFETY] daily risk patched max_entries=%s winning_max=%s count_sent=%s",
        _env_int("ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL", 4),
        _env_int("ENTRY_WINNING_SYMBOL_MAX_DAILY_ENTRIES", 6),
        _env_bool("ENTRY_COUNT_SENT_ORDER_AS_DAILY_ENTRY", False),
    )
    return True


def _looks_like_429(exc: BaseException) -> bool:
    text = repr(exc) + " " + str(exc)
    return "429" in text or "Too Many Requests" in text


def _wrap_ranking_callable(mod: Any, name: str, fn: Callable[..., Any]) -> bool:
    if getattr(fn, "_ranking_429_cache_wrapped_v10", False):
        return False
    key = f"{getattr(mod, '__name__', repr(mod))}.{name}"

    @functools.wraps(fn)
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        now = time.time()
        until = float(_RANKING_429_COOLDOWN_UNTIL.get(key, 0.0) or 0.0)
        if now < until and _env_bool("RANKING_API_429_USE_LAST_SUCCESS", True) and key in _RANKING_429_CACHE:
            logger.warning("[RANKING 429 GUARD] cooldown cache hit key=%s remain=%.1fs", key, until - now)
            return _RANKING_429_CACHE[key]
        try:
            result = fn(*args, **kwargs)
            if result is not None:
                _RANKING_429_CACHE[key] = result
            return result
        except Exception as e:
            if _looks_like_429(e):
                cooldown = _env_float("RANKING_API_429_COOLDOWN_SEC", 30.0)
                _RANKING_429_COOLDOWN_UNTIL[key] = time.time() + cooldown
                if _env_bool("RANKING_API_429_USE_LAST_SUCCESS", True) and key in _RANKING_429_CACHE:
                    logger.warning("[RANKING 429 GUARD] 429 -> reuse last success key=%s cooldown=%.1fs", key, cooldown)
                    return _RANKING_429_CACHE[key]
                logger.warning("[RANKING 429 GUARD] 429 no cache key=%s cooldown=%.1fs", key, cooldown)
            raise

    _wrapped._ranking_429_cache_wrapped_v10 = True  # type: ignore[attr-defined]
    setattr(mod, name, _wrapped)
    return True


def _install_ranking_429_guard() -> bool:
    module_names = [
        "trading.ranking.entry_from_ranking",
        "trading.ranking.scheduler",
        "trading.ranking.collector",
        "trading.ranking.collectors",
        "trading.ranking.ranking_collector",
        "trading.ranking.ranking_source_selector",
        "trading.ranking.ranking_selector",
        "trading.summary.ranking.runner",
        "trading.ranking.summary.runner",
        "kabu_api.ranking",
        "kabu_api.ranking_api",
    ]
    wrapped = 0
    for mod_name in module_names:
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue
        for name in dir(mod):
            lname = name.lower()
            if "ranking" not in lname and "rank" not in lname:
                continue
            if lname.startswith("_") and "run" not in lname and "fetch" not in lname and "load" not in lname:
                continue
            try:
                fn = getattr(mod, name)
            except Exception:
                continue
            if callable(fn):
                try:
                    if _wrap_ranking_callable(mod, name, fn):
                        wrapped += 1
                except Exception:
                    logger.debug("[RANKING 429 GUARD] wrap failed %s.%s", mod_name, name, exc_info=True)
    logger.warning("[RANKING 429 GUARD] installed wrapped=%s", wrapped)
    return wrapped > 0


def _install_stale_ranking_fallback_flags() -> bool:
    try:
        from global_state import global_data
        setattr(global_data, "allow_ranking_fallback_when_push_stale", True)
        setattr(global_data, "ranking_summary_fallback_max_age_sec", _env_float("RANKING_SUMMARY_FALLBACK_MAX_AGE_SEC", 360.0))
    except Exception:
        logger.debug("[STALE FALLBACK] global_data flag install failed", exc_info=True)
    logger.warning(
        "[STALE FALLBACK] enabled push_stale_ranking_fallback=%s max_age=%s",
        _env_bool("ENTRY_ALLOW_RANKING_FALLBACK_WHEN_PUSH_STALE", True),
        _env_float("RANKING_SUMMARY_FALLBACK_MAX_AGE_SEC", 360.0),
    )
    return True


def _install_exit_empty_broker_safety() -> bool:
    try:
        eh = importlib.import_module("trading.handlers.exit_handler")
    except Exception:
        logger.exception("[EXIT EMPTY BROKER SAFETY] exit_handler import failed")
        return False

    old = getattr(eh, "run_exit_pipeline", None)
    if not callable(old):
        return False
    if getattr(old, "_exit_empty_broker_safety_wrapped_v10", False):
        return True

    @functools.wraps(old)
    def _run_exit_pipeline_broker_safety(*args: Any, **kwargs: Any) -> Any:
        global _EXIT_CALL_COUNT
        _EXIT_CALL_COUNT += 1
        every_n = max(1, _env_int("EXIT_EMPTY_FAST_FORCE_BROKER_EVERY_N", 5))
        if _EXIT_CALL_COUNT % every_n == 0:
            os.environ["OPEN_POSITION_FORCE_BROKER_CHECK_ONCE"] = "1"
            os.environ["EXIT_FORCE_BROKER_POSITION_CHECK_ONCE"] = "1"
        return old(*args, **kwargs)

    _run_exit_pipeline_broker_safety._exit_empty_broker_safety_wrapped_v10 = True  # type: ignore[attr-defined]
    eh.run_exit_pipeline = _run_exit_pipeline_broker_safety

    main_mod = sys.modules.get("__main__")
    if main_mod is not None and getattr(main_mod, "run_exit_pipeline", None) is old:
        setattr(main_mod, "run_exit_pipeline", _run_exit_pipeline_broker_safety)

    logger.warning(
        "[EXIT EMPTY BROKER SAFETY] installed force_every_n=%s empty_throttle_sec=%s",
        _env_int("EXIT_EMPTY_FAST_FORCE_BROKER_EVERY_N", 5),
        os.getenv("OPEN_POSITION_EMPTY_THROTTLE_SEC"),
    )
    return True


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    _install_env_defaults()
    ok_reset = _reset_sent_only_counts()
    ok_daily = _install_daily_risk_reentry_patch()
    ok_stale = _install_stale_ranking_fallback_flags()
    ok_429 = _install_ranking_429_guard()
    ok_exit = _install_exit_empty_broker_safety()
    _INSTALLED = bool(ok_reset or ok_daily or ok_stale or ok_429 or ok_exit)
    logger.warning(
        "[REENTRY STALE 429 EXIT SAFETY] installed=%s reset=%s daily=%s stale=%s rank429=%s exit=%s",
        _INSTALLED,
        ok_reset,
        ok_daily,
        ok_stale,
        ok_429,
        ok_exit,
    )
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[REENTRY STALE 429 EXIT SAFETY] auto install failed")


__all__ = ["install"]
