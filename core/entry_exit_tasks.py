# ============================================================
# File   : core/entry_exit_tasks.py
# Version: PRODUCTION-STABLE-REV1.1-COMPAT-SHIM
# ------------------------------------------------------------
# 【概要】
#   entry / exit タスク登録用の互換 shim
#
# 【目的】
#   - 旧 import パス:
#       core.entry_exit_tasks.register_entry_exit_tasks
#     を維持する
#
#   - 新構成で entry_exit_tasks が別モジュールへ移動していても
#     scheduler_bootstrap 側を壊さない
#
#   - 実体が未実装でも起動を止めない
#
# 【設計】
#   - 実体が存在するモジュールを順番に探索
#   - 見つかれば委譲
#   - 見つからない場合は WARNING を出して no-op
#   - 例外発生時も False を返して startup を継続
#
# 【対応】
#   - schedule ライブラリ型:
#       register_entry_exit_tasks()
#
#   - APScheduler型:
#       register_entry_exit_tasks(scheduler)
#
#   - kwargs 付き呼び出し:
#       register_entry_exit_tasks(scheduler=scheduler, ...)
#
# 【重要】
#   - 起動を止めない
#   - entry/exit 機能が未実装でも system_startup を継続する
#   - この shim 自身 core.entry_exit_tasks は候補から除外する
#
# 【修正履歴】
#   REV1.1:
#     - return Fals 構文エラー修正
#     - 自己参照候補除外
#     - register_jobs / setup_entry_exit_tasks 等の互換候補追加
#     - 解決結果キャッシュ追加
#     - no-op API追加
#     - __all__ 整備
# ============================================================

from __future__ import annotations

import importlib
import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ============================================================
# 候補モジュール
# ============================================================

_REGISTER_CANDIDATES: tuple[tuple[str, str], ...] = (
    # --------------------------------------------------------
    # trading.entry_exit 系: 新しめの候補
    # --------------------------------------------------------
    ("trading.entry_exit.tasks", "register_entry_exit_tasks"),
    ("trading.entry_exit.scheduler", "register_entry_exit_tasks"),
    ("trading.entry_exit.entry_exit_tasks", "register_entry_exit_tasks"),
    ("trading.entry_exit.pipeline", "register_entry_exit_tasks"),
    ("trading.entry_exit.runner", "register_entry_exit_tasks"),

    # --------------------------------------------------------
    # trading.entry_exit 系: 互換名候補
    # --------------------------------------------------------
    ("trading.entry_exit.tasks", "register_jobs"),
    ("trading.entry_exit.scheduler", "register_jobs"),
    ("trading.entry_exit.runner", "register_jobs"),
    ("trading.entry_exit.pipeline", "register_jobs"),

    ("trading.entry_exit.tasks", "setup_entry_exit_tasks"),
    ("trading.entry_exit.scheduler", "setup_entry_exit_tasks"),
    ("trading.entry_exit.runner", "setup_entry_exit_tasks"),

    ("trading.entry_exit.tasks", "start_entry_exit_tasks"),
    ("trading.entry_exit.scheduler", "start_entry_exit_tasks"),

    # --------------------------------------------------------
    # core 配下に移動している場合
    # --------------------------------------------------------
    ("core.scheduler.entry_exit_tasks", "register_entry_exit_tasks"),
    ("core.tasks.entry_exit_tasks", "register_entry_exit_tasks"),
    ("core.entry_exit.scheduler", "register_entry_exit_tasks"),
    ("core.entry_exit.tasks", "register_entry_exit_tasks"),

    ("core.scheduler.entry_exit_tasks", "register_jobs"),
    ("core.tasks.entry_exit_tasks", "register_jobs"),
    ("core.entry_exit.scheduler", "register_jobs"),
    ("core.entry_exit.tasks", "register_jobs"),

    # --------------------------------------------------------
    # scheduler_jobs 配下に分離されている場合
    # --------------------------------------------------------
    ("scheduler_jobs.entry_exit.scheduler", "register_entry_exit_tasks"),
    ("scheduler_jobs.entry_exit.tasks", "register_entry_exit_tasks"),
    ("scheduler_jobs.entry_exit.entry_exit_tasks", "register_entry_exit_tasks"),

    ("scheduler_jobs.entry_exit.scheduler", "register_jobs"),
    ("scheduler_jobs.entry_exit.tasks", "register_jobs"),

    # --------------------------------------------------------
    # 旧互換候補
    # --------------------------------------------------------
    ("entry_exit_tasks", "register_entry_exit_tasks"),
    ("entry_exit_tasks", "register_jobs"),
)


# ============================================================
# module state
# ============================================================

_RESOLVE_LOCK = threading.RLock()
_RESOLVED_FUNC: Optional[Callable[..., Any]] = None
_RESOLVED_NAME: Optional[str] = None
_RESOLVE_ATTEMPTED = False


# ============================================================
# global_data helpers
# ============================================================

def _get_global_data():
    try:
        from global_state import global_data  # type: ignore
        return global_data
    except Exception:
        pass

    try:
        from core.global_context.context import global_data  # type: ignore
        return global_data
    except Exception:
        return None


def _set_global_attr(name: str, value: Any) -> None:
    gd = _get_global_data()
    if gd is None:
        return

    try:
        setattr(gd, name, value)
    except Exception:
        pass


def _get_global_attr(name: str, default: Any = None) -> Any:
    gd = _get_global_data()
    if gd is None:
        return default

    try:
        return getattr(gd, name, default)
    except Exception:
        return default


# ============================================================
# resolver helpers
# ============================================================

def _is_self_reference(module_name: str) -> bool:
    """
    この shim 自身を候補として解決しないための保護。
    """
    return module_name in {
        "core.entry_exit_tasks",
        __name__,
    }


def _safe_import_module(module_name: str):
    """
    import を安全に実行する。
    """
    if _is_self_reference(module_name):
        logger.debug(
            "[entry_exit_tasks shim] skip self reference module=%s",
            module_name,
        )
        return None

    try:
        return importlib.import_module(module_name)

    except ModuleNotFoundError:
        logger.debug(
            "[entry_exit_tasks shim] module not found module=%s",
            module_name,
        )
        return None

    except Exception:
        logger.exception(
            "[entry_exit_tasks shim] import failed module=%s",
            module_name,
        )
        return None


def _resolve_register_func(
    *,
    force_refresh: bool = False,
) -> Optional[Callable[..., Any]]:
    """
    register_entry_exit_tasks の実体を探索する。

    Parameters
    ----------
    force_refresh:
        True の場合、キャッシュを無視して再探索する。

    Returns
    -------
    Optional[Callable]
        見つかった登録関数。見つからなければ None。
    """
    global _RESOLVED_FUNC
    global _RESOLVED_NAME
    global _RESOLVE_ATTEMPTED

    with _RESOLVE_LOCK:
        if not force_refresh and _RESOLVED_FUNC is not None:
            return _RESOLVED_FUNC

        if force_refresh:
            _RESOLVED_FUNC = None
            _RESOLVED_NAME = None
            _RESOLVE_ATTEMPTED = False

        for module_name, func_name in _REGISTER_CANDIDATES:
            mod = _safe_import_module(module_name)
            if mod is None:
                continue

            try:
                fn = getattr(mod, func_name, None)
            except Exception:
                logger.exception(
                    "[entry_exit_tasks shim] getattr failed module=%s func=%s",
                    module_name,
                    func_name,
                )
                continue

            if not callable(fn):
                logger.debug(
                    "[entry_exit_tasks shim] callable not found module=%s func=%s",
                    module_name,
                    func_name,
                )
                continue

            # 念のため、このファイル自身の register_entry_exit_tasks を掴まない
            if fn is register_entry_exit_tasks:
                logger.debug(
                    "[entry_exit_tasks shim] skip recursive callable module=%s func=%s",
                    module_name,
                    func_name,
                )
                continue

            _RESOLVED_FUNC = fn
            _RESOLVED_NAME = f"{module_name}.{func_name}"
            _RESOLVE_ATTEMPTED = True

            logger.info(
                "[entry_exit_tasks shim] resolved %s",
                _RESOLVED_NAME,
            )

            _set_global_attr("entry_exit_tasks_resolved", True)
            _set_global_attr("entry_exit_tasks_resolved_name", _RESOLVED_NAME)

            return _RESOLVED_FUNC

        _RESOLVE_ATTEMPTED = True

        logger.warning(
            "[entry_exit_tasks shim] register_entry_exit_tasks implementation "
            "not found. entry/exit scheduler registration skipped."
        )

        _set_global_attr("entry_exit_tasks_resolved", False)
        _set_global_attr("entry_exit_tasks_resolved_name", None)

        return None


# ============================================================
# call helpers
# ============================================================

def _call_resolved_func(
    fn: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> bool:
    """
    解決済み関数を安全に呼ぶ。

    Returns
    -------
    bool
        True  : 実体関数の呼び出し成功
        False : 実体関数で例外発生、または明示 False
    """
    try:
        result = fn(*args, **kwargs)

        # 実体側が bool を返す場合は尊重
        if isinstance(result, bool):
            return result

        # None 返却は schedule 登録系では成功扱いにする
        return True

    except TypeError as e:
        # scheduler 引数あり/なしの差異を吸収するため、
        # 呼び出しが合わない場合だけ no-args fallback を試す。
        logger.warning(
            "[entry_exit_tasks shim] call signature mismatch resolved=%s err=%s "
            "-> retry without args",
            _RESOLVED_NAME,
            e,
        )

        try:
            result = fn()

            if isinstance(result, bool):
                return result

            return True

        except Exception:
            logger.exception(
                "[entry_exit_tasks shim] register_entry_exit_tasks retry without args failed "
                "resolved=%s",
                _RESOLVED_NAME,
            )
            return False

    except Exception:
        logger.exception(
            "[entry_exit_tasks shim] register_entry_exit_tasks failed resolved=%s",
            _RESOLVED_NAME,
        )
        return False


# ============================================================
# public api
# ============================================================

def register_entry_exit_tasks(*args: Any, **kwargs: Any) -> bool:
    """
    entry / exit タスクを scheduler に登録する互換 API。

    旧 import パス:
        core.entry_exit_tasks.register_entry_exit_tasks

    を維持するための shim。

    Parameters
    ----------
    *args, **kwargs:
        実体関数へそのまま委譲する。
        schedule方式なら引数なし、APScheduler方式なら scheduler を渡す想定。

    Returns
    -------
    bool
        True  : 実体関数を呼び出し成功
        False : 実体が見つからず skip、または実体呼び出し失敗
    """
    logger.info("[entry_exit_tasks shim] register_entry_exit_tasks start")

    _set_global_attr("entry_exit_tasks_registering", True)

    try:
        fn = _resolve_register_func()

        if not callable(fn):
            logger.warning(
                "[entry_exit_tasks shim] register_entry_exit_tasks skipped: "
                "implementation not available"
            )
            return False

        ok = _call_resolved_func(fn, *args, **kwargs)

        logger.info(
            "[entry_exit_tasks shim] register_entry_exit_tasks done ok=%s resolved=%s",
            ok,
            _RESOLVED_NAME,
        )

        _set_global_attr("entry_exit_tasks_registered", bool(ok))
        _set_global_attr("entry_exit_tasks_last_register_ok", bool(ok))

        return bool(ok)

    finally:
        _set_global_attr("entry_exit_tasks_registering", False)


def register_jobs(*args: Any, **kwargs: Any) -> bool:
    """
    互換入口。

    scheduler_bootstrap 側が register_jobs を探す場合に対応。
    """
    return register_entry_exit_tasks(*args, **kwargs)


def setup_entry_exit_tasks(*args: Any, **kwargs: Any) -> bool:
    """
    互換入口。
    """
    return register_entry_exit_tasks(*args, **kwargs)


def start_entry_exit_tasks(*args: Any, **kwargs: Any) -> bool:
    """
    互換入口。
    """
    return register_entry_exit_tasks(*args, **kwargs)


def is_entry_exit_tasks_available(*, force_refresh: bool = False) -> bool:
    """
    entry/exit タスク登録実体が存在するか確認する。
    """
    fn = _resolve_register_func(force_refresh=force_refresh)
    return callable(fn)


def get_resolved_entry_exit_register_name(
    *,
    force_refresh: bool = False,
) -> Optional[str]:
    """
    解決済みの entry/exit 登録関数名を返す。
    """
    if force_refresh or _RESOLVED_FUNC is None:
        _resolve_register_func(force_refresh=force_refresh)

    return _RESOLVED_NAME


def clear_entry_exit_resolver_cache() -> None:
    """
    resolver キャッシュをクリアする。
    開発中にモジュール配置を変更した場合の手動リセット用。
    """
    global _RESOLVED_FUNC
    global _RESOLVED_NAME
    global _RESOLVE_ATTEMPTED

    with _RESOLVE_LOCK:
        _RESOLVED_FUNC = None
        _RESOLVED_NAME = None
        _RESOLVE_ATTEMPTED = False

    logger.info("[entry_exit_tasks shim] resolver cache cleared")


def noop_register_entry_exit_tasks(*args: Any, **kwargs: Any) -> bool:
    """
    明示的な no-op 登録関数。

    entry/exit 未実装でも、呼び出し元が bool を期待する場合に使える。
    """
    logger.warning(
        "[entry_exit_tasks shim] noop_register_entry_exit_tasks called. "
        "entry/exit tasks are not implemented."
    )
    return False


# ============================================================
# exports
# ============================================================

__all__ = [
    "register_entry_exit_tasks",
    "register_jobs",
    "setup_entry_exit_tasks",
    "start_entry_exit_tasks",
    "is_entry_exit_tasks_available",
    "get_resolved_entry_exit_register_name",
    "clear_entry_exit_resolver_cache",
    "noop_register_entry_exit_tasks",
]