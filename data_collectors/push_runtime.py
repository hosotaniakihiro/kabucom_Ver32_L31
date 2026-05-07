# ============================================================
# File   : data_collectors/push_runtime.py
# Version: DATA-COLLECTORS-PUSH-RUNTIME-V3-SEED-ACTIVE-SYMBOLS
# ------------------------------------------------------------
# Purpose:
#   - PUSH受信本体を main.py から独立して起動する
#   - PUSH A/B 50銘柄ローテーションを明示的に有効化する
#   - rotation worker から subscription_manager.refresh_subscriptions を呼び、
#     株ステーションへの登録を実行する
#   - main.py から切り離したため、main_database.py 側でPUSH登録対象を生成・注入する
# ============================================================

from __future__ import annotations

import logging
import os
import schedule
import time
from typing import Any, Iterable, List

from data_collectors.config import (
    PUSH_BATCH_SIZE,
    PUSH_REGISTER_SEC,
    PUSH_SWITCH_GAP_SEC,
    PUSH_TARGET_TOTAL,
)
from data_collectors.heartbeat import write_heartbeat
from data_collectors.import_resolver import resolve_callable

logger = logging.getLogger(__name__)


SUBSCRIPTION_REFRESH_CANDIDATES = [
    ("trading.push.subscription_manager", "refresh_subscriptions"),
    ("trading.push.subscription_manager.core", "refresh_subscriptions"),
    ("trading.push.subscription_manager", "force_refresh_subscriptions"),
    ("trading.push.subscription_manager.core", "force_refresh_subscriptions"),
]

SUBSCRIPTION_MANAGER_START_CANDIDATES = [
    ("trading.push.subscription_manager", "start_symbol_subscription_manager"),
    ("trading.push.subscription_manager.core", "start_symbol_subscription_manager"),
    ("trading.push.symbol_subscription_manager", "start_symbol_subscription_manager"),
]

PUSH_START_CANDIDATES = [
    ("trading.push.push_stream.runtime", "start_push_stream"),
    ("trading.push.push_stream.runtime", "start"),
    ("trading.push.push_stream", "start_push_stream"),
    ("trading.push.push_stream", "start"),
    ("trading.push.push_stream.core", "start_push_stream"),
]

ROTATION_CONFIG_CANDIDATES = [
    ("trading.push.push_stream.rotation", "set_rotation_timing"),
    ("trading.push.push_stream.rotation", "configure_rotation"),
]


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in {"1", "true", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "no", "n", "off"}:
        return False
    return bool(default)


def _call_with_fallback(fn, *args, **kwargs) -> Any:
    """既存関数の引数差分を吸収する。"""
    try:
        return fn(*args, **kwargs)
    except TypeError:
        pass

    try:
        return fn()
    except TypeError:
        pass

    try:
        return fn(schedule)
    except TypeError:
        pass

    return fn(*args, **kwargs)


def _normalize_symbol(x: Any) -> str | None:
    if x is None:
        return None
    s = str(x).strip().upper()
    if not s or s in {"NONE", "NULL", "NAN", "NA", "-", "0"}:
        return None
    if s.startswith("FILLER"):
        return None
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    if not s.isalnum():
        return None
    if not (3 <= len(s) <= 5):
        return None
    return s


def _dedupe_symbols(items: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for x in items or []:
        s = _normalize_symbol(x)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _extract_symbols(src: Any) -> List[str]:
    if src is None:
        return []

    try:
        if hasattr(src, "columns"):
            cols = list(getattr(src, "columns", []))
            for c in ("symbol", "Symbol", "code", "Code", "stock_code", "銘柄コード"):
                if c in cols:
                    return _dedupe_symbols(src[c].tolist())
    except Exception:
        pass

    if isinstance(src, dict):
        for key in (
            "symbols",
            "codes",
            "items",
            "data",
            "active_symbols",
            "monitor_symbols",
            "push_symbols",
            "register_symbols",
            "subscription_symbols",
            "daily_watchlist_symbols",
        ):
            if key in src and src[key]:
                return _extract_symbols(src[key])
        return _dedupe_symbols(src.keys())

    if isinstance(src, str):
        return _dedupe_symbols([src])

    try:
        return _dedupe_symbols(list(src))
    except Exception:
        return []


def _set_global_symbols(symbols: List[str], *, source: str) -> None:
    """push_stream.rotation_symbols が読む global_data/runtime attr へ同じ100銘柄を注入する。"""
    if not symbols:
        return

    try:
        from global_state import global_data
    except Exception:
        global_data = None

    attrs = (
        "monitor_symbols",
        "candidate_push_symbols",
        "push_candidate_symbols",
        "push_symbols_100",
        "active_symbols",
        "ats_register_targets",
        "ats_targets",
        "should_register_symbols",
        "push_symbols",
        "register_symbols",
        "subscription_symbols",
        "daily_watchlist_symbols",
    )

    if global_data is not None:
        for attr in attrs:
            try:
                setattr(global_data, attr, list(symbols))
            except Exception:
                logger.debug("[PUSH RUNTIME] failed to set global_data.%s", attr, exc_info=True)
        try:
            global_data.symbols_active = set(symbols)
            global_data.active_symbol_source = source
            global_data.push_symbol_seed_source = source
            global_data.push_symbol_seed_count = len(symbols)
        except Exception:
            pass

    try:
        from trading.push.push_stream.runtime import _safe_set_runtime
        for attr in attrs:
            _safe_set_runtime(attr, list(symbols))
        _safe_set_runtime("symbols_active", list(symbols))
        _safe_set_runtime("push_symbol_seed_source", source)
        _safe_set_runtime("push_symbol_seed_count", len(symbols))
    except Exception:
        logger.debug("[PUSH RUNTIME] failed to set push_stream runtime symbols", exc_info=True)

    logger.warning(
        "[PUSH RUNTIME] seeded push/register symbols source=%s count=%d head=%s",
        source,
        len(symbols),
        symbols[:20],
    )


def _provider_call(fn) -> Any:
    patterns = (
        lambda: fn(force=True),
        lambda: fn(limit=PUSH_TARGET_TOTAL),
        lambda: fn(max_symbols=PUSH_TARGET_TOTAL),
        lambda: fn(PUSH_TARGET_TOTAL),
        lambda: fn(),
    )
    last_err: BaseException | None = None
    for caller in patterns:
        try:
            return caller()
        except TypeError as e:
            last_err = e
            continue
        except Exception:
            logger.debug("[PUSH RUNTIME] provider call failed fn=%s", fn, exc_info=True)
            return None
    if last_err is not None:
        logger.debug("[PUSH RUNTIME] provider signature mismatch fn=%s err=%s", fn, last_err)
    return None


def seed_push_symbols_once() -> List[str]:
    """
    main.py からPUSH stackを切り離したため、main_database.py側で
    active_symbol_manager.update_active_symbols() を明示実行して登録対象を作る。
    """
    providers = [
        ("trading.ranking.active_symbol_manager", "update_active_symbols"),
        ("trading.ranking.active_symbol_manager", "get_register_symbols"),
        ("trading.ranking.active_symbol_manager", "get_push_symbols"),
        ("trading.ranking.active_symbol_manager", "get_active_symbols"),
        ("optional.batch.daily_watchlist", "load_daily_watchlist_symbols"),
        ("optional.batch.daily_watchlist", "get_daily_watchlist_symbols"),
    ]

    for module_name, func_name in providers:
        try:
            import importlib
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
        except Exception:
            logger.debug("[PUSH RUNTIME] symbol seed import failed %s.%s", module_name, func_name, exc_info=True)
            continue

        if not callable(fn):
            continue

        src = _provider_call(fn)
        symbols = _extract_symbols(src)[:PUSH_TARGET_TOTAL]
        logger.info(
            "[PUSH RUNTIME] symbol seed provider=%s.%s count=%d head=%s",
            module_name,
            func_name,
            len(symbols),
            symbols[:10],
        )
        if symbols:
            _set_global_symbols(symbols, source=f"{module_name}.{func_name}")
            return symbols

    logger.error("[PUSH RUNTIME] failed to seed push/register symbols: all providers returned empty")
    return []


def schedule_symbol_seed_refresh() -> None:
    """
    ranking DB writer が後からランキングを取り込む場合に備えて、
    PUSH登録対象を定期更新する。
    """
    try:
        schedule.every(30).seconds.do(seed_push_symbols_once).tag("data_collectors_push_symbol_seed")
        logger.info("[PUSH RUNTIME] scheduled symbol seed refresh every 30 seconds")
    except Exception:
        logger.exception("[PUSH RUNTIME] failed to schedule symbol seed refresh")


def configure_push_rotation_if_supported() -> None:
    fn = resolve_callable(ROTATION_CONFIG_CANDIDATES, required=False)
    if fn is None:
        logger.info(
            "[PUSH RUNTIME] rotation config function not found; "
            "use existing rotation_settings defaults/env."
        )
        return

    kwargs = {
        "register_seconds": PUSH_REGISTER_SEC,
        "switch_gap_seconds": PUSH_SWITCH_GAP_SEC,
        "batch_size": PUSH_BATCH_SIZE,
        "target_total": PUSH_TARGET_TOTAL,
    }

    try:
        logger.info("[PUSH RUNTIME] configure rotation kwargs=%s", kwargs)
        _call_with_fallback(fn, **kwargs)
    except Exception:
        logger.exception("[PUSH RUNTIME] configure rotation failed")


def resolve_subscription_refresh_callable():
    return resolve_callable(SUBSCRIPTION_REFRESH_CANDIDATES, required=False)


def start_subscription_manager_if_requested() -> bool:
    if not _env_bool("DATA_COLLECTORS_START_SUB_MANAGER_LOOP", False):
        logger.info("[PUSH RUNTIME] subscription manager background loop skipped; rotation worker handles registration")
        return True

    fn = resolve_callable(SUBSCRIPTION_MANAGER_START_CANDIDATES, required=False)
    if fn is None:
        logger.error("[PUSH RUNTIME] no subscription manager start function resolved")
        return False

    logger.info("[PUSH RUNTIME] call subscription manager loop function: %s", fn)

    try:
        result = _call_with_fallback(fn)
        logger.info("[PUSH RUNTIME] subscription manager loop start returned: %r", result)
        return True
    except Exception:
        logger.exception("[PUSH RUNTIME] subscription manager loop start failed")
        return False


def start_push_stream() -> bool:
    fn = resolve_callable(PUSH_START_CANDIDATES, required=False)
    if fn is None:
        logger.error("[PUSH RUNTIME] no push start function resolved")
        return False

    refresh_callable = resolve_subscription_refresh_callable()

    logger.info(
        "[PUSH RUNTIME] call push stream function=%s enable_rotate=True refresh_callable=%s",
        fn,
        bool(callable(refresh_callable)),
    )

    try:
        result = _call_with_fallback(
            fn,
            refresh_callable=refresh_callable,
            enable_rotate=True,
        )
        logger.info("[PUSH RUNTIME] push stream start returned: %r", result)
        return True
    except Exception:
        logger.exception("[PUSH RUNTIME] push stream start failed")
        return False


def run_forever() -> int:
    logger.info("[PUSH RUNTIME] START")
    logger.info(
        "[PUSH RUNTIME] rotation target_total=%s batch_size=%s register_sec=%s gap_sec=%s",
        PUSH_TARGET_TOTAL,
        PUSH_BATCH_SIZE,
        PUSH_REGISTER_SEC,
        PUSH_SWITCH_GAP_SEC,
    )

    os.environ.setdefault("PUSH_REGISTER_SEC", str(PUSH_REGISTER_SEC))
    os.environ.setdefault("PUSH_SWITCH_GAP_SEC", str(PUSH_SWITCH_GAP_SEC))
    os.environ.setdefault("PUSH_ROTATION_HOLD_SEC", str(PUSH_REGISTER_SEC))
    os.environ.setdefault("PUSH_ROTATION_UNREGISTER_WAIT_SEC", str(PUSH_SWITCH_GAP_SEC))
    os.environ.setdefault("PUSH_BATCH_SIZE", str(PUSH_BATCH_SIZE))
    os.environ.setdefault("PUSH_TARGET_TOTAL", str(PUSH_TARGET_TOTAL))

    # 重要:
    #   main.pyから切り離したため、PUSH登録対象100銘柄をこのプロセス内で作る。
    seed_push_symbols_once()
    schedule_symbol_seed_refresh()

    configure_push_rotation_if_supported()

    sub_loop_ok = start_subscription_manager_if_requested()
    push_ok = start_push_stream()

    if not sub_loop_ok:
        logger.error("[PUSH RUNTIME] subscription manager background loop could not start")
    if not push_ok:
        logger.error("[PUSH RUNTIME] push stream could not start")

    if not push_ok:
        logger.error("[PUSH RUNTIME] abort because push stream failed")
        return 1

    last_hb = 0.0

    while True:
        try:
            schedule.run_pending()
        except Exception:
            logger.exception("[PUSH RUNTIME] schedule.run_pending failed")

        now = time.time()
        if now - last_hb >= 30:
            write_heartbeat(
                "push_receiver",
                status="alive",
                subscription_loop_started=sub_loop_ok,
                push_started=push_ok,
                rotation_enabled=True,
                register_seconds=PUSH_REGISTER_SEC,
                switch_gap_seconds=PUSH_SWITCH_GAP_SEC,
                batch_size=PUSH_BATCH_SIZE,
                target_total=PUSH_TARGET_TOTAL,
            )
            logger.info("[PUSH RUNTIME] heartbeat alive")
            last_hb = now

        time.sleep(1.0)
