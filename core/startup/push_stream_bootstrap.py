# ============================================================
# File   : core/startup/push_stream_bootstrap.py
# Version: PRODUCTION-STABLE-REV2.0-PUSH-STREAM-BOOTSTRAP-REAL-SYMBOL-BRIDGE
# ------------------------------------------------------------
# 【概要】
#   PUSH WebSocket runner 起動 bootstrap
#
# 【目的】
#   - push_stream runner を起動する
#   - 起動直前に実銘柄100件を解決して global_data / runtime へ注入する
#   - FILLER_* が monitor_symbols に残っている状態を修復する
#   - rotation.py 側が実銘柄を取得できる状態にしてから WebSocket を開始する
#
# 【重要】
#   - symbol_bootstrap の後に呼ばれる前提
#   - push stream 起動前に install_real_push_symbols() を必ず呼ぶ
#   - refresh_callable がなくても rotation.py の直接 register が動く
#   - ただし subscription_manager がある環境では refresh_callable も設定する
#
# 【ログ正常例】
#   [PUSH SYMBOL BRIDGE] install complete ... real=100 head=['7203', ...]
#   [push_stream] resolved register targets total=100 ...
#   [push_stream] register_symbols sent size=50 ...
# ============================================================

from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

VERSION = "PRODUCTION-STABLE-REV2.0-PUSH-STREAM-BOOTSTRAP-REAL-SYMBOL-BRIDGE"


# ============================================================
# import helpers
# ============================================================

def _resolve_attr(module_name: str, attr_name: str) -> Any:
    try:
        mod = importlib.import_module(module_name)
        return getattr(mod, attr_name, None)
    except Exception:
        logger.debug(
            "[push_stream_bootstrap] resolve failed %s.%s",
            module_name,
            attr_name,
            exc_info=True,
        )
        return None


def _call_safe(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    try:
        return fn(*args, **kwargs)
    except TypeError:
        try:
            return fn()
        except Exception:
            logger.debug("[push_stream_bootstrap] call failed fn=%s", fn, exc_info=True)
            return None
    except Exception:
        logger.debug("[push_stream_bootstrap] call failed fn=%s", fn, exc_info=True)
        return None


def _get_global_data() -> Any:
    candidates = (
        ("global_state", "global_data"),
        ("core.global_context.context", "global_data"),
    )

    for module_name, attr_name in candidates:
        gd = _resolve_attr(module_name, attr_name)
        if gd is not None:
            return gd

    return None


# ============================================================
# status helpers
# ============================================================

def _get_push_status() -> Dict[str, Any]:
    status_fn = _resolve_attr("trading.push.push_stream.runner", "get_status")
    if callable(status_fn):
        try:
            st = status_fn()
            if isinstance(st, dict):
                return st
        except Exception:
            logger.debug("[push_stream_bootstrap] get_status failed", exc_info=True)

    state_mod = None
    try:
        state_mod = importlib.import_module("trading.push.push_stream.state")
    except Exception:
        state_mod = None

    if state_mod is None:
        return {}

    def _safe_get(name: str, default: Any = None) -> Any:
        try:
            return getattr(state_mod, name, default)
        except Exception:
            return default

    out: Dict[str, Any] = {}

    for name in (
        "_rotation_enabled",
        "_refresh_callable",
        "_stop_event",
        "_connected_event",
    ):
        out[name] = _safe_get(name)

    return out


def _sync_global_runtime_flags(*, running: bool) -> None:
    gd = _get_global_data()
    if gd is None:
        return

    vals = {
        "push_stream_running": bool(running),
        "subscription_refresh_running": bool(running),
    }

    for k, v in vals.items():
        try:
            setattr(gd, k, v)
        except Exception:
            pass


# ============================================================
# symbol bridge
# ============================================================

def _install_real_symbols_before_start() -> list[str]:
    """
    push stream 起動前に実銘柄100件を注入する。
    """
    try:
        from core.startup.push_symbol_bridge import install_real_push_symbols
    except Exception:
        logger.exception("[push_stream_bootstrap] import push_symbol_bridge failed")
        return []

    try:
        symbols = install_real_push_symbols(limit=100, strict=False)
    except Exception:
        logger.exception("[push_stream_bootstrap] install_real_push_symbols failed")
        return []

    if symbols:
        logger.info(
            "[push_stream_bootstrap] real push symbols installed count=%d head=%s",
            len(symbols),
            symbols[:10],
        )
    else:
        logger.error(
            "[push_stream_bootstrap] real push symbols install returned empty. "
            "push_stream will start, but rotation will not register symbols."
        )

    return symbols


# ============================================================
# refresh callable
# ============================================================

def _build_refresh_callable(symbols: list[str]) -> Optional[Callable[..., Any]]:
    """
    subscription_manager が存在する場合は refresh callable を構築する。

    重要:
      - rotation.py から来る symbols/codes/items を優先
      - force / reason / clear_first / unregister_first を落とさない
      - wait_after_clear_sec / unregister_wait_sec を落とさない
      - TypeError 時も極力 kwargs を維持する
    """
    candidates = (
        ("trading.push.subscription_manager", "refresh_subscriptions"),
        ("trading.push.subscription_manager", "refresh_subscription"),
        ("trading.push.subscription_manager", "register_symbols"),
        ("trading.push.push_subscription_manager", "refresh_subscriptions"),
        ("trading.push.push_subscription_manager", "register_symbols"),
        ("trading.push.push_stream.subscription_manager", "refresh_subscriptions"),
        ("trading.push.push_stream.subscription_manager", "register_symbols"),
    )

    target_fn = None
    target_name = None

    for module_name, func_name in candidates:
        fn = _resolve_attr(module_name, func_name)
        if callable(fn):
            target_fn = fn
            target_name = f"{module_name}.{func_name}"
            break

    if not callable(target_fn):
        logger.info(
            "[push_stream_bootstrap] subscription refresh callable not found -> direct register mode"
        )
        return None

    def _refresh_callable(*args: Any, **kwargs: Any) -> Any:
        call_symbols = (
            kwargs.get("symbols")
            or kwargs.get("codes")
            or kwargs.get("items")
            or symbols
        )

        reason = kwargs.get("reason", "push_stream_bootstrap")
        force = kwargs.get("force", True)
        clear_first = kwargs.get("clear_first", True)
        unregister_first = kwargs.get("unregister_first", True)
        wait_after_clear_sec = kwargs.get(
            "wait_after_clear_sec",
            kwargs.get("unregister_wait_sec", 0.5),
        )
        unregister_wait_sec = kwargs.get(
            "unregister_wait_sec",
            wait_after_clear_sec,
        )

        logger.info(
            "[push_stream_bootstrap] refresh callable call target=%s reason=%s "
            "symbols=%d force=%s clear_first=%s unregister_first=%s wait_after_clear=%.3fs",
            target_name,
            reason,
            len(call_symbols or []),
            force,
            clear_first,
            unregister_first,
            float(wait_after_clear_sec or 0.0),
        )

        try:
            return target_fn(
                symbols=call_symbols,
                codes=call_symbols,
                items=call_symbols,
                force=force,
                clear_first=clear_first,
                unregister_first=unregister_first,
                wait_after_clear_sec=wait_after_clear_sec,
                unregister_wait_sec=unregister_wait_sec,
                reason=reason,
            )

        except TypeError as e:
            logger.warning(
                "[push_stream_bootstrap] refresh callable full kwargs failed target=%s err=%s",
                target_name,
                e,
            )

            try:
                return target_fn(
                    symbols=call_symbols,
                    force=force,
                    clear_first=clear_first,
                    reason=reason,
                )
            except TypeError as e2:
                logger.warning(
                    "[push_stream_bootstrap] refresh callable compat kwargs failed target=%s err=%s",
                    target_name,
                    e2,
                )

            try:
                return target_fn(call_symbols)
            except TypeError:
                return target_fn()

    logger.info(
        "[push_stream_bootstrap] subscription refresh callable resolved: %s",
        target_name,
    )
    return _refresh_callable


def _install_refresh_callable(symbols: list[str]) -> bool:
    set_refresh_callable = _resolve_attr(
        "trading.push.push_stream.transport",
        "set_refresh_callable",
    )

    if not callable(set_refresh_callable):
        logger.warning("[push_stream_bootstrap] set_refresh_callable not found")
        return False

    refresh_callable = _build_refresh_callable(symbols)

    try:
        set_refresh_callable(refresh_callable)
        logger.info(
            "[push_stream_bootstrap] refresh_callable installed callable=%s",
            callable(refresh_callable),
        )
        return callable(refresh_callable)
    except Exception:
        logger.exception("[push_stream_bootstrap] set_refresh_callable failed")
        return False


# ============================================================
# push stream runner
# ============================================================

def _enable_rotation() -> None:
    enable_rotation = _resolve_attr(
        "trading.push.push_stream.rotation",
        "enable_rotation",
    )

    if callable(enable_rotation):
        try:
            enable_rotation(True)
        except Exception:
            logger.exception("[push_stream_bootstrap] enable_rotation failed")


def _start_runner() -> bool:
    """
    push_stream runner を起動する。
    """
    candidates = (
        ("trading.push.push_stream.runner", "start_push_stream"),
        ("trading.push.push_stream.runner", "start"),
        ("trading.push.push_stream.runner", "run_background"),
        ("trading.push.push_stream", "start_push_stream"),
        ("trading.push.push_stream", "start"),
    )

    for module_name, func_name in candidates:
        fn = _resolve_attr(module_name, func_name)
        if not callable(fn):
            continue

        try:
            ret = fn()
            logger.info(
                "[push_stream_bootstrap] runner started via %s.%s ret=%s",
                module_name,
                func_name,
                ret,
            )
            return True
        except TypeError:
            try:
                ret = fn(None)
                logger.info(
                    "[push_stream_bootstrap] runner started via %s.%s ret=%s",
                    module_name,
                    func_name,
                    ret,
                )
                return True
            except Exception:
                logger.debug(
                    "[push_stream_bootstrap] runner start failed %s.%s",
                    module_name,
                    func_name,
                    exc_info=True,
                )
        except Exception:
            logger.exception(
                "[push_stream_bootstrap] runner start failed %s.%s",
                module_name,
                func_name,
            )

    logger.error("[push_stream_bootstrap] no push stream runner start function resolved")
    return False


# ============================================================
# public API
# ============================================================

def start_push_stream_bootstrap(*args: Any, **kwargs: Any) -> bool:
    """
    system_startup から呼ばれる push stream bootstrap 本体。
    """
    logger.info("📡 push stream runner bootstrap start version=%s", VERSION)

    # --------------------------------------------------------
    # 1. 実銘柄100件を必ず注入
    # --------------------------------------------------------
    symbols = _install_real_symbols_before_start()

    # --------------------------------------------------------
    # 2. refresh_callable を設定
    #    見つからなくても direct register mode で動く
    # --------------------------------------------------------
    refresh_callable_installed = _install_refresh_callable(symbols)

    # --------------------------------------------------------
    # 3. rotation 有効化
    # --------------------------------------------------------
    _enable_rotation()

    # --------------------------------------------------------
    # 4. runner 起動
    # --------------------------------------------------------
    ok = _start_runner()

    _sync_global_runtime_flags(running=ok)

    status = _get_push_status()

    logger.info(
        "✅ push stream runner bootstrap complete ok=%s real_symbols=%d refresh_callable=%s status=%s",
        ok,
        len(symbols),
        refresh_callable_installed,
        status,
    )

    return bool(ok)


# 既存import互換
def bootstrap_push_stream(*args: Any, **kwargs: Any) -> bool:
    return start_push_stream_bootstrap(*args, **kwargs)


def start_push_stream(*args: Any, **kwargs: Any) -> bool:
    return start_push_stream_bootstrap(*args, **kwargs)


def run_push_stream_bootstrap(*args: Any, **kwargs: Any) -> bool:
    return start_push_stream_bootstrap(*args, **kwargs)


__all__ = [
    "start_push_stream_bootstrap",
    "bootstrap_push_stream",
    "start_push_stream",
    "run_push_stream_bootstrap",
]