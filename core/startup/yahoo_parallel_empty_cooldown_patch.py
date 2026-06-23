# -*- coding: utf-8 -*-
"""Cooldown Yahoo parallel fetch when it repeatedly returns zero frames."""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-YAHOO-PARALLEL-EMPTY-COOLDOWN"
_INSTALLED = False


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


def _set_defaults() -> None:
    os.environ.setdefault("YAHOO_PARALLEL_EMPTY_BATCH_THRESHOLD", "2")
    os.environ.setdefault("YAHOO_PARALLEL_EMPTY_BATCH_COOLDOWN_SEC", "900")
    os.environ.setdefault("YAHOO_PARALLEL_EMPTY_MIN_SYMBOLS", "20")


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    if str(os.getenv("DISABLE_YAHOO_PARALLEL_EMPTY_COOLDOWN_PATCH", "")).strip() == "1":
        logger.warning("[YAHOO PARALLEL EMPTY COOLDOWN] disabled by env")
        return False
    _set_defaults()
    try:
        import trading.yahoo.yahoo_parallel_fetch as ypf  # type: ignore
    except Exception:
        logger.debug("[YAHOO PARALLEL EMPTY COOLDOWN] yahoo_parallel_fetch not importable yet", exc_info=True)
        return False

    old = getattr(ypf, "parallel_fetch_symbols", None)
    if not callable(old):
        return False
    if getattr(old, "_yahoo_parallel_empty_cooldown", False):
        _INSTALLED = True
        return True

    state = {"empty_count": 0, "cooldown_until": 0.0}

    def _wrapped(symbols, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        syms = list(symbols or [])
        now_ts = time.time()
        threshold = _env_int("YAHOO_PARALLEL_EMPTY_BATCH_THRESHOLD", 2, min_value=1, max_value=20)
        cooldown_sec = _env_int("YAHOO_PARALLEL_EMPTY_BATCH_COOLDOWN_SEC", 900, min_value=0, max_value=7200)
        min_symbols = _env_int("YAHOO_PARALLEL_EMPTY_MIN_SYMBOLS", 20, min_value=1, max_value=1000)

        if len(syms) >= min_symbols and state["cooldown_until"] > now_ts:
            logger.warning(
                "[YAHOO PARALLEL EMPTY COOLDOWN] skipped remain=%.0fs symbols=%d empty_count=%d",
                state["cooldown_until"] - now_ts,
                len(syms),
                state["empty_count"],
            )
            return []

        ret = old(symbols, *args, **kwargs)
        try:
            result_len = len(ret or [])
        except Exception:
            result_len = 0

        if len(syms) >= min_symbols and result_len == 0:
            state["empty_count"] += 1
            if cooldown_sec > 0 and state["empty_count"] >= threshold:
                state["cooldown_until"] = now_ts + cooldown_sec
                logger.warning(
                    "[YAHOO PARALLEL EMPTY COOLDOWN] cooldown start empty_count=%d threshold=%d cooldown=%ss symbols=%d",
                    state["empty_count"],
                    threshold,
                    cooldown_sec,
                    len(syms),
                )
        else:
            if state["empty_count"]:
                logger.info("[YAHOO PARALLEL EMPTY COOLDOWN] reset empty_count=%d result_frames=%d", state["empty_count"], result_len)
            state["empty_count"] = 0
            state["cooldown_until"] = 0.0
        return ret

    _wrapped._yahoo_parallel_empty_cooldown = True  # type: ignore[attr-defined]
    _wrapped._original = old  # type: ignore[attr-defined]
    ypf.parallel_fetch_symbols = _wrapped  # type: ignore[attr-defined]
    _INSTALLED = True
    logger.warning(
        "[YAHOO PARALLEL EMPTY COOLDOWN] installed version=%s threshold=%s cooldown=%s min_symbols=%s",
        VERSION,
        os.getenv("YAHOO_PARALLEL_EMPTY_BATCH_THRESHOLD"),
        os.getenv("YAHOO_PARALLEL_EMPTY_BATCH_COOLDOWN_SEC"),
        os.getenv("YAHOO_PARALLEL_EMPTY_MIN_SYMBOLS"),
    )
    return True


__all__ = ["VERSION", "install"]
