# -*- coding: utf-8 -*-
"""
Intraday load guard patch.

Reduces remaining production load issues observed after ranking/summary schema fixes:
- Yahoo complement worker running for minutes and causing repeated SKIP_RUNNING ticks.
- Yahoo repeated retries for symbols that fail on Yahoo Finance during the same day.
- PUSH rotation candidate pools briefly exceeding 100 symbols before final clamp.

The patch is intentionally defensive.  It mainly sets safer defaults before modules
read their environment, then wraps selected functions when they are already importable.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

VERSION = "V1-INTRADAY-LOAD-GUARD-YAHOO-BACKOFF-PUSH100"
_INSTALLED = False
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in _TRUE:
            return True
        if raw in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        v = int(float(str(os.getenv(name, str(default))).replace(",", "")))
    except Exception:
        v = int(default)
    if min_value is not None:
        v = max(v, min_value)
    if max_value is not None:
        v = min(v, max_value)
    return int(v)


def _norm_symbol(symbol: Any) -> str:
    s = str(symbol or "").strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    return s


def _dedupe_limit(items: Iterable[Any], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in items or []:
        s = _norm_symbol(x)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _set_default_envs() -> None:
    """Set safer defaults before yahoo_tasks / rotation modules read env."""
    # 日中Yahoo補完はランキング/PUSHを主軸にし、1分ごとの重複workerを避ける。
    os.environ.setdefault("YAHOO_COMPLEMENT_EVERY_SECONDS", "300")
    os.environ.setdefault("YAHOO_COMPLEMENT_TIMEOUT_SEC", "180")
    os.environ.setdefault("YAHOO_COMPLEMENT_STUCK_GRACE_SEC", "15")
    os.environ.setdefault("YAHOO_COMPLEMENT_STUCK_COOLDOWN_SEC", "180")
    os.environ.setdefault("YAHOO_COMPLEMENT_MIN_START_GAP_SEC", "240")
    os.environ.setdefault("YAHOO_COMPLEMENT_SKIP_LOG_EVERY", "5")

    # Yahooで当日失敗した銘柄は短時間で再試行しない。
    os.environ.setdefault("YAHOO_SYMBOL_FAIL_BACKOFF_SEC", "1800")
    os.environ.setdefault("YAHOO_SYMBOL_FAIL_CACHE_MAX", "1000")

    # PUSH候補は各段階で100固定に寄せる。
    os.environ.setdefault("PUSH_ROTATION_TARGET_MIN_KEEP", "100")
    os.environ.setdefault("PUSH_ROTATION_TARGET_MAX_KEEP", "100")
    os.environ.setdefault("PUSH_REGISTER_MIN_KEEP", "100")
    os.environ.setdefault("PUSH_REGISTER_MAX_KEEP", "100")


def _patch_yahoo_tasks() -> bool:
    try:
        import core.yahoo_tasks as yt  # type: ignore
    except Exception:
        logger.debug("[INTRADAY LOAD GUARD] core.yahoo_tasks not importable yet", exc_info=True)
        return False

    if getattr(yt, "_INTRADAY_LOAD_GUARD_PATCHED", False):
        return True

    old_start = getattr(yt, "_start_yahoo_worker", None)
    if not callable(old_start):
        return False

    last_attempt_at = 0.0
    skip_counter = 0

    def _start_yahoo_worker_patched(now):  # type: ignore[no-untyped-def]
        nonlocal last_attempt_at, skip_counter
        min_gap = _env_int("YAHOO_COMPLEMENT_MIN_START_GAP_SEC", 240, min_value=0, max_value=3600)
        now_ts = time.time()
        if min_gap > 0 and last_attempt_at > 0 and (now_ts - last_attempt_at) < min_gap:
            skip_counter += 1
            every = _env_int("YAHOO_COMPLEMENT_SKIP_LOG_EVERY", 5, min_value=1, max_value=100)
            if skip_counter == 1 or skip_counter % every == 0:
                logger.warning(
                    "[INTRADAY LOAD GUARD] Yahoo complement throttled elapsed=%.1fs min_gap=%ss skip_count=%s",
                    now_ts - last_attempt_at,
                    min_gap,
                    skip_counter,
                )
            return False
        started = bool(old_start(now))
        if started:
            last_attempt_at = now_ts
            skip_counter = 0
        return started

    try:
        yt._start_yahoo_worker = _start_yahoo_worker_patched  # type: ignore[attr-defined]
        yt._INTRADAY_LOAD_GUARD_PATCHED = True  # type: ignore[attr-defined]
        logger.warning(
            "[INTRADAY LOAD GUARD] yahoo task throttle patched every=%s timeout=%s min_gap=%s",
            os.getenv("YAHOO_COMPLEMENT_EVERY_SECONDS"),
            os.getenv("YAHOO_COMPLEMENT_TIMEOUT_SEC"),
            os.getenv("YAHOO_COMPLEMENT_MIN_START_GAP_SEC"),
        )
        return True
    except Exception:
        logger.exception("[INTRADAY LOAD GUARD] yahoo task throttle patch failed")
        return False


def _wrap_download_function(fn: Callable[..., Any]) -> Callable[..., Any]:
    fail_cache: dict[str, float] = {}

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        symbol = kwargs.get("symbol") or kwargs.get("code")
        if symbol is None and args:
            symbol = args[0]
        key = _norm_symbol(symbol)
        backoff = _env_int("YAHOO_SYMBOL_FAIL_BACKOFF_SEC", 1800, min_value=0, max_value=86400)
        now_ts = time.time()
        if key and backoff > 0:
            until = fail_cache.get(key, 0.0)
            if until > now_ts:
                logger.info(
                    "[INTRADAY LOAD GUARD] Yahoo symbol skipped by fail-cache symbol=%s remain=%.0fs",
                    key,
                    until - now_ts,
                )
                return None
        try:
            ret = fn(*args, **kwargs)
            empty = ret is None
            try:
                empty = empty or bool(getattr(ret, "empty", False))
            except Exception:
                pass
            if key and empty and backoff > 0:
                fail_cache[key] = now_ts + backoff
                if len(fail_cache) > _env_int("YAHOO_SYMBOL_FAIL_CACHE_MAX", 1000, min_value=10, max_value=10000):
                    for k, _ in sorted(fail_cache.items(), key=lambda kv: kv[1])[: max(1, len(fail_cache) // 4)]:
                        fail_cache.pop(k, None)
            return ret
        except Exception:
            if key and backoff > 0:
                fail_cache[key] = now_ts + backoff
            raise

    _wrapped._intraday_load_guard_wrapped = True  # type: ignore[attr-defined]
    _wrapped._original = fn  # type: ignore[attr-defined]
    return _wrapped


def _patch_yahoo_fail_cache() -> bool:
    """Best-effort wrapper for common Yahoo download helpers."""
    patched = 0
    candidates = (
        "trading.yahoo.pipeline.download",
        "trading.yahoo.pipeline.complement.download",
        "trading.yahoo.pipeline.complement.fetch",
        "trading.yahoo.scheduler.complement_scheduler",
    )
    names = (
        "download_1m",
        "download_symbol_1m",
        "fetch_1m",
        "fetch_symbol_1m",
        "_download_1m",
        "_fetch_symbol_1m",
    )
    for module_name in candidates:
        try:
            mod = __import__(module_name, fromlist=["*"])
        except Exception:
            continue
        for name in names:
            fn = getattr(mod, name, None)
            if callable(fn) and not getattr(fn, "_intraday_load_guard_wrapped", False):
                try:
                    setattr(mod, name, _wrap_download_function(fn))
                    patched += 1
                except Exception:
                    logger.debug("[INTRADAY LOAD GUARD] yahoo helper wrap failed %s.%s", module_name, name, exc_info=True)
    if patched:
        logger.warning("[INTRADAY LOAD GUARD] yahoo fail-cache wrappers installed count=%s", patched)
    return patched > 0


def _patch_push_rotation_clamp() -> bool:
    try:
        from trading.push.push_stream import rotation_symbols as rs  # type: ignore
    except Exception:
        logger.debug("[INTRADAY LOAD GUARD] rotation_symbols not importable yet", exc_info=True)
        return False

    patched = False
    for fn_name in ("resolve_monitor_symbols", "resolve_register_targets"):
        old = getattr(rs, fn_name, None)
        if not callable(old) or getattr(old, "_intraday_load_guard_push100", False):
            continue

        def _make_wrapper(fn: Callable[..., Any], label: str) -> Callable[..., Any]:
            def _wrapped(*args: Any, **kwargs: Any) -> list[str]:
                limit = _env_int("PUSH_ROTATION_TARGET_MAX_KEEP", 100, min_value=1, max_value=100)
                out = _dedupe_limit(fn(*args, **kwargs) or [], limit)
                if len(out) > limit:
                    out = out[:limit]
                return out
            _wrapped._intraday_load_guard_push100 = True  # type: ignore[attr-defined]
            _wrapped._original = fn  # type: ignore[attr-defined]
            return _wrapped

        try:
            setattr(rs, fn_name, _make_wrapper(old, fn_name))
            patched = True
        except Exception:
            logger.debug("[INTRADAY LOAD GUARD] push clamp wrap failed fn=%s", fn_name, exc_info=True)

    try:
        from trading.push.push_stream import rotation_core  # type: ignore
        old_core = getattr(rotation_core, "resolve_register_targets", None)
        if callable(old_core) and not getattr(old_core, "_intraday_load_guard_push100", False):
            def _core_wrapped(*args: Any, **kwargs: Any) -> list[str]:
                limit = _env_int("PUSH_ROTATION_TARGET_MAX_KEEP", 100, min_value=1, max_value=100)
                return _dedupe_limit(old_core(*args, **kwargs) or [], limit)
            _core_wrapped._intraday_load_guard_push100 = True  # type: ignore[attr-defined]
            _core_wrapped._original = old_core  # type: ignore[attr-defined]
            rotation_core.resolve_register_targets = _core_wrapped  # type: ignore[attr-defined]
            patched = True
    except Exception:
        pass

    if patched:
        logger.warning("[INTRADAY LOAD GUARD] push rotation clamp installed max_keep=%s", os.getenv("PUSH_ROTATION_TARGET_MAX_KEEP", "100"))
    return patched


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if _env_bool("DISABLE_INTRADAY_LOAD_GUARD_PATCH", False):
        logger.warning("[INTRADAY LOAD GUARD] disabled by env")
        return False
    _set_default_envs()
    yahoo_task_ok = _patch_yahoo_tasks()
    yahoo_cache_ok = _patch_yahoo_fail_cache()
    push_ok = _patch_push_rotation_clamp()
    _INSTALLED = True
    logger.warning(
        "[INTRADAY LOAD GUARD] installed version=%s yahoo_task=%s yahoo_fail_cache=%s push100=%s yahoo_every=%s yahoo_timeout=%s",
        VERSION,
        yahoo_task_ok,
        yahoo_cache_ok,
        push_ok,
        os.getenv("YAHOO_COMPLEMENT_EVERY_SECONDS"),
        os.getenv("YAHOO_COMPLEMENT_TIMEOUT_SEC"),
    )
    return True


__all__ = ["VERSION", "install"]
