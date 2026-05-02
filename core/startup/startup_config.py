# ============================================================
# File   : core/startup/startup_config.py
# Version: FINAL-PRODUCTION-REV23.1-STARTUP-CONFIG
#          -RANKING-SUMMARY-BOOTSTRAP-FLAGS
# ------------------------------------------------------------
# 【概要】
#   startup 共通設定・runtime flag 初期化・設定読込を担当
#
# 【機能】
#   ✔ settings.ini 読込
#   ✔ global_data 初期 runtime flags 設定
#   ✔ token refresh
#   ✔ path 定義
#   ✔ 汎用 helper
#
# 【REV23.1 変更点】
#   ✔ ranking summary bootstrap 用 runtime flags を追加
#   ✔ ranking_summary_bootstrap_started
#   ✔ ranking_summary_bootstrap_done
#   ✔ ranking_summary_bootstrap_failed
#   ✔ ranking_summary_bootstrap_result
#   ✔ ranking_summary_bootstrap_saved
#   ✔ ranking_summary_bootstrap_snapshot_rows
#   ✔ ranking_summary_bootstrap_db_path
#   ✔ 既存機能削除ゼロ
# ============================================================

from __future__ import annotations

import datetime as dt
import importlib
import logging
from configparser import ConfigParser
from typing import Any, Callable

from token_manager import refresh_token
from config.paths import get_path
from global_state import global_data

logger = logging.getLogger(__name__)

PUSH_DIR = str(get_path("raw_push"))
SUMMARY_DIR = get_path("summary_db_dir")
RANKING_DIR = get_path("ranking")

VERSION = "FINAL-PRODUCTION-REV23.1-STARTUP-CONFIG-RANKING-SUMMARY-BOOTSTRAP-FLAGS"


# ============================================================
# generic helpers
# ============================================================

def resolve_attr(module_name: str, attr_name: str) -> Any:
    """
    module.attr を安全に解決する。

    失敗しても startup は止めない。
    """
    try:
        mod = importlib.import_module(module_name)
        return getattr(mod, attr_name, None)
    except Exception:
        logger.debug(
            "[startup.config] resolve attr failed module=%s attr=%s",
            module_name,
            attr_name,
            exc_info=True,
        )
        return None


def safe_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """
    callable を安全に呼び出す。

    TypeError の場合は引数なし呼び出しも試す。
    失敗時は None を返し、startup を止めない。
    """
    try:
        return fn(*args, **kwargs)
    except TypeError:
        try:
            return fn()
        except Exception:
            logger.debug("[startup.config] safe_call failed fn=%s", fn, exc_info=True)
            return None
    except Exception:
        logger.debug("[startup.config] safe_call failed fn=%s", fn, exc_info=True)
        return None


def head(x: Any, n: int = 10) -> list[Any]:
    """
    任意 iterable の先頭 n 件を安全に list 化する。
    """
    try:
        return list(x)[:n]  # type: ignore[arg-type]
    except Exception:
        return []


def is_filler_symbol(x: Any) -> bool:
    """
    FILLER / NONE / NULL / NAN などのダミー銘柄判定。
    """
    s = str(x).strip().upper()
    return (
        not s
        or s.startswith("FILLER")
        or s in {"NONE", "NULL", "NAN", "NA", "-", "0"}
    )


def is_real_symbol(x: Any) -> bool:
    """
    実銘柄らしい symbol かを簡易判定する。
    """
    if x is None:
        return False

    s = str(x).strip().upper()

    if is_filler_symbol(s):
        return False

    if not s.isalnum():
        return False

    if not (3 <= len(s) <= 5):
        return False

    return True


def count_symbol_quality(symbols: Any) -> tuple[int, int, int]:
    """
    symbol list の品質を数える。

    Returns
    -------
    tuple[int, int, int]
        raw_count, real_count, filler_count
    """
    try:
        seq = list(symbols)  # type: ignore[arg-type]
    except Exception:
        return 0, 0, 0

    raw = len(seq)
    real = sum(1 for x in seq if is_real_symbol(x))
    filler = sum(1 for x in seq if is_filler_symbol(x))

    return raw, real, filler


# ============================================================
# internal flag helpers
# ============================================================

def _set_flag(name: str, value: Any) -> None:
    """
    global_data flag を安全に設定する。
    """
    try:
        setattr(global_data, name, value)
    except Exception:
        logger.debug(
            "[startup.config] set flag failed name=%s value=%s",
            name,
            value,
            exc_info=True,
        )


def _init_push_flags() -> None:
    # PUSH
    _set_flag("push_writer_running", False)
    _set_flag("push_storage_running", False)
    _set_flag("push_stream_running", False)
    _set_flag("ws_connected", False)

    # PUSH symbol bridge
    _set_flag("push_symbol_bridge_installed", False)
    _set_flag("push_symbol_bridge_count", 0)
    _set_flag("push_symbol_bridge_symbols", [])

    # push stream early start
    _set_flag("push_stream_early_start_started", False)
    _set_flag("push_stream_early_start_done", False)
    _set_flag("push_stream_early_start_failed", False)
    _set_flag("push_stream_early_start_result", None)


def _init_scheduler_flags() -> None:
    # scheduler bootstrap
    _set_flag("scheduler_bootstrap_registered", False)
    _set_flag("scheduler_bootstrap_registered_at", None)
    _set_flag("scheduler_bootstrap_failed", False)
    _set_flag("scheduler_bootstrap_result", None)

    # schedule loop
    _set_flag("scheduler_run_pending_loop_running", False)
    _set_flag("scheduler_run_pending_loop_started_at", None)
    _set_flag("scheduler_run_pending_loop_last_at", None)
    _set_flag("scheduler_run_pending_loop_count", 0)
    _set_flag("scheduler_jobs_count", 0)


def _init_startup_summary_restore_flags() -> None:
    # startup summary restore
    _set_flag("startup_summary_restore_started", False)
    _set_flag("startup_summary_restore_done", False)
    _set_flag("startup_summary_restore_failed", False)
    _set_flag("startup_summary_restore_result", None)


def _init_ranking_summary_bootstrap_flags() -> None:
    """
    ランキング由来サマリー bootstrap 用 flags。

    用途:
      - 起動時に ranking_snapshot DB から ranking_summary DB を復元・追加計算したか確認
      - STARTUP COMPLETE status へ出す
      - ログで起動時の ranking summary 保存状況を追跡する
    """
    _set_flag("ranking_summary_bootstrap_started", False)
    _set_flag("ranking_summary_bootstrap_done", False)
    _set_flag("ranking_summary_bootstrap_failed", False)
    _set_flag("ranking_summary_bootstrap_result", None)

    # 追加診断用
    _set_flag("ranking_summary_bootstrap_saved", {})
    _set_flag("ranking_summary_bootstrap_snapshot_rows", 0)
    _set_flag("ranking_summary_bootstrap_db_path", None)
    _set_flag("ranking_summary_bootstrap_message", "")


def _init_summary_tick_flags() -> None:
    # summary tick once
    _set_flag("summary_tick_once_debug_done", False)
    _set_flag("summary_tick_once_debug_failed", False)
    _set_flag("summary_tick_once_debug_result", None)


# ============================================================
# settings / flags
# ============================================================

def load_settings() -> tuple[str, str]:
    """
    settings.ini から kabu Station API password と WebSocket URL を読み込む。
    """
    conf = ConfigParser()
    conf.read("settings.ini", encoding="utf-8")

    api_password = conf.get("aukabu", "apipassword", fallback="")
    ws_url = conf.get("WebSocket", "url", fallback="")

    return api_password, ws_url


def init_runtime_flags(ws_url: str = "") -> None:
    """
    startup 開始時に runtime flags を初期化する。

    注意:
      - ここでは global_data.clear_all() はしない
      - clear_all は safe migration phase 側に任せる
      - 起動状態を追跡する flags の初期化のみ行う
    """
    try:
        _set_flag("ws_url", ws_url or "")
        _set_flag("push_ws_url", ws_url or "")
        _set_flag("today_str", dt.datetime.now().strftime("%Y%m%d"))
        _set_flag("recent_entry_symbols", [])

        # orders
        _set_flag("allow_orders", False)

        # PUSH / stream / bridge
        _init_push_flags()

        # startup summary restore
        _init_startup_summary_restore_flags()

        # ranking summary bootstrap
        _init_ranking_summary_bootstrap_flags()

        # scheduler / schedule loop
        _init_scheduler_flags()

        # summary debug
        _init_summary_tick_flags()

        logger.info(
            "[startup.config] runtime flags initialized version=%s today=%s",
            VERSION,
            getattr(global_data, "today_str", None),
        )

    except Exception:
        logger.debug("[startup.config] init_runtime_flags failed", exc_info=True)


def refresh_token_safe(api_password: str) -> None:
    """
    kabu Station API token を更新する。

    token 取得失敗時は startup 継続不可のため raise。
    """
    try:
        global_data.token_value = refresh_token(api_password)
        logger.info("🔐 API token refreshed")
    except Exception:
        logger.exception("❌ Token refresh failed")
        raise


__all__ = [
    "PUSH_DIR",
    "SUMMARY_DIR",
    "RANKING_DIR",
    "VERSION",
    "resolve_attr",
    "safe_call",
    "head",
    "is_filler_symbol",
    "is_real_symbol",
    "count_symbol_quality",
    "load_settings",
    "init_runtime_flags",
    "refresh_token_safe",
]