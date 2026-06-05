from __future__ import annotations

import functools
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_LOCK = threading.Lock()
_LAST_CALL_TS = 0.0
_GLOBAL_COOLDOWN_UNTIL = 0.0
_CACHE: dict[str, tuple[Any, float]] = {}


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _csv_env(name: str, default: str) -> list[str]:
    try:
        raw = os.getenv(name, default)
        return [x.strip() for x in str(raw).split(",") if x.strip()]
    except Exception:
        return [x.strip() for x in default.split(",") if x.strip()]


def _key(params: Any) -> str:
    try:
        if isinstance(params, dict):
            return repr(sorted((str(k), str(v)) for k, v in params.items()))
        return repr(params)
    except Exception:
        return repr(params)


def _looks_429(text: str) -> bool:
    s = str(text or "")
    return "429" in s or "4001006" in s or "API実行回数エラー" in s


def _set_defaults() -> None:
    os.environ.setdefault("RANKING_API_GLOBAL_MIN_INTERVAL_SEC", "0.35")
    os.environ.setdefault("RANKING_API_GLOBAL_429_COOLDOWN_SEC", "45.0")
    os.environ.setdefault("RANKING_API_GLOBAL_CACHE_TTL_SEC", "300.0")
    os.environ.setdefault("RANKING_API_GLOBAL_USE_CACHE_ON_COOLDOWN", "1")
    os.environ.setdefault("RANKING_API_429_RETRY_MAX", "1")
    os.environ.setdefault("RANKING_API_ENABLED_TYPE_IDS", "1,2")
    os.environ.setdefault("RANKING_API_ENABLED_MARKETS", "ALL,TP,TS")
    os.environ.setdefault("RANKING_API_CALL_SLEEP_SEC", "0.25")


def _apply_collector_budget() -> bool:
    try:
        import trading.ranking.collectors as c
        type_master = {
            1: "値上がり率",
            2: "値下がり率",
            3: "売買高上位",
            4: "売買代金",
            5: "TICK回数",
            6: "売買高急増",
            7: "売買代金急増",
        }
        market_master = {
            "ALL": "全市場",
            "TP": "東証プライム",
            "TS": "東証スタンダード",
            "TG": "東証グロース",
        }
        type_ids: list[int] = []
        for x in _csv_env("RANKING_API_ENABLED_TYPE_IDS", "1,2"):
            try:
                i = int(float(x))
                if i in type_master:
                    type_ids.append(i)
            except Exception:
                pass
        if not type_ids:
            type_ids = [1, 2]
        markets = [m for m in _csv_env("RANKING_API_ENABLED_MARKETS", "ALL,TP,TS") if m in market_master]
        if not markets:
            markets = ["ALL", "TP", "TS"]
        c.TYPE_TO_NAME = {i: type_master[i] for i in type_ids}
        c.EXCHANGE_DIVISIONS = {m: market_master[m] for m in markets}
        c.API_CALL_SLEEP_SEC = max(0.05, _env_float("RANKING_API_CALL_SLEEP_SEC", 0.25))
        logger.warning(
            "[RANKING API GLOBAL THROTTLE] collector budget applied type_ids=%s markets=%s calls_per_cycle=%s sleep=%.3fs",
            list(c.TYPE_TO_NAME.keys()), list(c.EXCHANGE_DIVISIONS.keys()), len(c.TYPE_TO_NAME) * len(c.EXCHANGE_DIVISIONS), c.API_CALL_SLEEP_SEC,
        )
        return True
    except Exception:
        logger.exception("[RANKING API GLOBAL THROTTLE] collector budget apply failed")
        return False


def _wait_rate_limit() -> None:
    global _LAST_CALL_TS
    min_interval = max(0.0, _env_float("RANKING_API_GLOBAL_MIN_INTERVAL_SEC", 0.35))
    if min_interval <= 0:
        return
    with _LOCK:
        now = time.monotonic()
        wait = min_interval - (now - float(_LAST_CALL_TS or 0.0))
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL_TS = time.monotonic()


def _start_cooldown(reason: str, params: Any) -> None:
    global _GLOBAL_COOLDOWN_UNTIL
    cooldown = max(1.0, _env_float("RANKING_API_GLOBAL_429_COOLDOWN_SEC", 45.0))
    until = time.time() + cooldown
    if until > _GLOBAL_COOLDOWN_UNTIL:
        _GLOBAL_COOLDOWN_UNTIL = until
    logger.warning("[RANKING API GLOBAL THROTTLE] cooldown start %.1fs reason=%s params=%s", cooldown, reason, params)


def _get_cache(k: str) -> Any | None:
    if not _env_bool("RANKING_API_GLOBAL_USE_CACHE_ON_COOLDOWN", True):
        return None
    item = _CACHE.get(k)
    if not item:
        return None
    payload, saved_at = item
    ttl = max(1.0, _env_float("RANKING_API_GLOBAL_CACHE_TTL_SEC", 300.0))
    if time.time() - float(saved_at) <= ttl:
        return payload
    return None


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        _apply_collector_budget()
        return True
    _set_defaults()
    _apply_collector_budget()
    try:
        import trading.ranking.api_client as api
    except Exception:
        logger.debug("[RANKING API GLOBAL THROTTLE] api_client not ready", exc_info=True)
        return False
    old = getattr(api, "get_data_from_api", None)
    if not callable(old):
        return False
    if getattr(old, "_ranking_api_global_throttle_v2", False):
        _INSTALLED = True
        return True

    @functools.wraps(old)
    def _wrapped(params: dict[str, Any], *, timeout_sec: float = None, retry_max: int = None) -> dict[str, Any] | None:
        k = _key(params)
        now = time.time()
        if now < float(_GLOBAL_COOLDOWN_UNTIL or 0.0):
            cached = _get_cache(k)
            remain = float(_GLOBAL_COOLDOWN_UNTIL or 0.0) - now
            if cached is not None:
                logger.warning("[RANKING API GLOBAL THROTTLE] cooldown -> cache params=%s remain=%.1fs", params, remain)
                return cached
            logger.warning("[RANKING API GLOBAL THROTTLE] skipped by cooldown params=%s remain=%.1fs", params, remain)
            return None
        _wait_rate_limit()
        try:
            eff_retry = 1 if retry_max is None else max(1, min(int(retry_max), 1))
            ret = old(params, timeout_sec=timeout_sec if timeout_sec is not None else getattr(api, "API_TIMEOUT_SEC", 5.0), retry_max=eff_retry)
            if ret is not None:
                _CACHE[k] = (ret, time.time())
                return ret
            return None
        except Exception as e:
            if _looks_429(repr(e) + " " + str(e)):
                _start_cooldown(type(e).__name__, params)
                cached = _get_cache(k)
                if cached is not None:
                    return cached
                return None
            raise

    _wrapped._ranking_api_global_throttle_v2 = True  # type: ignore[attr-defined]
    _wrapped._original = old  # type: ignore[attr-defined]
    api.get_data_from_api = _wrapped
    _INSTALLED = True
    logger.warning(
        "[RANKING API GLOBAL THROTTLE] installed v2 min_interval=%s cooldown=%s cache_ttl=%s types=%s markets=%s",
        os.getenv("RANKING_API_GLOBAL_MIN_INTERVAL_SEC"),
        os.getenv("RANKING_API_GLOBAL_429_COOLDOWN_SEC"),
        os.getenv("RANKING_API_GLOBAL_CACHE_TTL_SEC"),
        os.getenv("RANKING_API_ENABLED_TYPE_IDS"),
        os.getenv("RANKING_API_ENABLED_MARKETS"),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[RANKING API GLOBAL THROTTLE] auto install failed")

__all__ = ["install"]