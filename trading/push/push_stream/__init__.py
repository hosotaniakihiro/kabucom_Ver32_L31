# ============================================================
# File   : trading/push/push_stream/__init__.py
# Version: Ver2.1-MAIN-WS-HARD-SKIP-SPLIT-MODE
# ------------------------------------------------------------
# ✔ 旧 trading.push.push_stream 公開API互換
# ✔ 分割後モジュールの再エクスポート
# ✔ transport / rotation_core / runner / dataframe の公開窓口
# ✔ main_database.py 分離運用時、main.py側の重複PUSH WSをno-op化
# ✔ emergency時のみ AUTOSTOCK_MAIN_PUSH_WS_ENABLED=1 で main.py側WSを許可
# ✔ optional fallback は AUTOSTOCK_MAIN_PUSH_WS_AUTO_FALLBACK=1 の明示時だけ許可
# ✔ PUSH rotation stability patch / liquidity keep100 patch を自動適用
# ✔ runner module 直呼び経路も同じguardへ寄せるが、再帰は防ぐ
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# rotation系が import 時に os.environ を読む前に、必ず先に読み込む。
from . import rotation_settings as rotation_settings
from . import rotation_symbols as rotation_symbols
from . import rotation_register as rotation_register
from . import rotation_logging as rotation_logging

try:
    from . import rotation_stability_patch as rotation_stability_patch
    rotation_stability_patch.install()
except Exception:
    logger.exception("[push_stream] rotation_stability_patch install failed")

try:
    from . import rotation_liquidity_keep100_patch as rotation_liquidity_keep100_patch
    rotation_liquidity_keep100_patch.install()
except Exception:
    logger.exception("[push_stream] rotation_liquidity_keep100_patch install failed")

from .transport import (
    set_refresh_callable,
    refresh_subscriptions,
    get_ws_sender,
    wait_until_connected,
    is_connected,
)
from .dataframe import (
    get_push_dataframe,
    clear_push_dataframe,
)
from .rotation_register import register_symbols
from .rotation_core import enable_rotation
from . import runner as _runner_mod

_runner_start_push_stream = _runner_mod.start_push_stream
_TRUE_RUNNER_START_PUSH_STREAM = _runner_start_push_stream
stop_push_stream = _runner_mod.stop_push_stream
get_status = _runner_mod.get_status

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _parse_dt(v: Any) -> dt.datetime | None:
    if v is None:
        return None
    try:
        if isinstance(v, dt.datetime):
            x = v
        else:
            s = str(v).strip()
            if not s:
                return None
            x = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if x.tzinfo is not None:
            x = x.astimezone().replace(tzinfo=None)
        return x
    except Exception:
        return None


def _latest_summary_age_sec() -> float | None:
    """main.pyが既に持っているmerged summaryの鮮度を軽量確認する。"""
    try:
        from global_state import global_data
    except Exception:
        return None

    candidates: list[Any] = []
    for attr in (
        "push_summary_latest_dt",
        "latest_push_summary_dt",
        "last_push_summary_dt",
        "summary_1m_latest_dt",
        "merged_summary_1m_latest_dt",
    ):
        try:
            candidates.append(getattr(global_data, attr, None))
        except Exception:
            pass

    # DataFrame系から最新datetimeを見る。import pandasは避け、属性だけで軽く見る。
    for attr in ("push_merged_summary", "merged_summary_1min", "merged_summary_1m", "summary_1m", "push_summary_1m"):
        try:
            df = getattr(global_data, attr, None)
            if df is None or not hasattr(df, "empty") or bool(getattr(df, "empty", True)):
                continue
            cols = list(getattr(df, "columns", []))
            for c in ("datetime", "time", "last_tick_at", "received_at"):
                if c in cols:
                    val = df[c].max()
                    candidates.append(val)
                    break
        except Exception:
            continue

    now = dt.datetime.now()
    best_age: float | None = None
    for v in candidates:
        x = _parse_dt(v)
        if x is None:
            continue
        age = (now - x).total_seconds()
        if age >= 0 and (best_age is None or age < best_age):
            best_age = age
    return best_age


def _main_ws_fallback_allowed_due_to_stale_summary() -> bool:
    """
    split mode の main.py 側 memory-only PUSH WS fallback。

    以前はデフォルト True だったため、起動直後に summary がまだ見えないだけで
    main.py 側にも PUSH WebSocket が立ち上がり、main_database.py 側と二重接続になった。
    通常運用では main_database.py / data_collectors_runner が PUSH受信・保存を担当するため、
    fallback は明示的に AUTOSTOCK_MAIN_PUSH_WS_AUTO_FALLBACK=1 を指定した場合だけ許可する。
    """
    if not _env_bool("AUTOSTOCK_MAIN_PUSH_WS_AUTO_FALLBACK", False):
        return False
    stale_sec = max(30.0, _env_float("AUTOSTOCK_MAIN_PUSH_WS_FALLBACK_STALE_SEC", 180.0))
    age = _latest_summary_age_sec()
    if age is None:
        logger.warning(
            "[push_stream] main WS fallback allowed by explicit env: no fresh summary visible yet stale_sec=%.1f",
            stale_sec,
        )
        return True
    if age > stale_sec:
        logger.warning(
            "[push_stream] main WS fallback allowed by explicit env: summary stale age=%.1fs stale_sec=%.1f",
            age,
            stale_sec,
        )
        return True
    logger.info(
        "[push_stream] main WS fallback not needed: fresh summary age=%.1fs stale_sec=%.1f",
        age,
        stale_sec,
    )
    return False


def _should_skip_push_stream_start_in_main() -> bool:
    """
    main_database.py 分離運用では main.py のPUSH WSを止める。

    例外:
      - data_collector / push_receiver プロセス自身
      - AUTOSTOCK_MAIN_PUSH_WS_ENABLED=1 の emergency standalone
      - AUTOSTOCK_MAIN_PUSH_WS_AUTO_FALLBACK=1 を明示し、summary stale の時だけ memory-only fallback
    """
    if _env_bool("AUTOSTOCK_MAIN_PUSH_WS_ENABLED", False):
        logger.warning("[push_stream] main WS explicitly enabled by AUTOSTOCK_MAIN_PUSH_WS_ENABLED=1")
        return False
    try:
        from data_collectors.split_mode import (
            is_data_collector_process,
            should_skip_data_collector_work_in_main,
            external_data_collectors_enabled,
        )
        if bool(is_data_collector_process()):
            return False
        is_main_split = bool(external_data_collectors_enabled()) and bool(should_skip_data_collector_work_in_main())
        if not is_main_split:
            return False
        if _main_ws_fallback_allowed_due_to_stale_summary():
            return False
        return True
    except Exception:
        # split判定に失敗した場合は従来互換で起動を許可する。
        logger.debug("[push_stream] split mode check failed; allow start for compatibility", exc_info=True)
        return False


def _mark_main_ws_skipped() -> None:
    try:
        from .runtime import _safe_set_runtime
        _safe_set_runtime("push_stream_running", False)
        _safe_set_runtime("push_writer_running", False)
        _safe_set_runtime("push_stream_memory_only", True)
        _safe_set_runtime("push_stream_ws_skipped_in_main", True)
    except Exception:
        pass


def _prepare_main_memory_only_ws() -> None:
    """main.py fallback WSではDB保存・rotation登録を行わない。"""
    try:
        os.environ.setdefault("AUTOSTOCK_MAIN_MEMORY_ONLY", "1")
        os.environ.setdefault("AUTOSTOCK_SKIP_DATA_COLLECTOR_WORK_IN_MAIN", "1")
        os.environ["PUSH_STREAM_ENABLE_DB_WRITE"] = "0"
        os.environ["PUSH_DB_WRITE_ENABLED"] = "0"
        os.environ["PUSH_ROTATION_ENABLED"] = "0"
        os.environ["PUSH_STREAM_ENABLE_ROTATION"] = "0"
    except Exception:
        pass


def start_push_stream(*args, **kwargs):
    if _should_skip_push_stream_start_in_main():
        _mark_main_ws_skipped()
        logger.warning(
            "[push_stream] WS start skipped in main process; "
            "main_database.py handles PUSH WebSocket/registration/storage. "
            "Set AUTOSTOCK_MAIN_PUSH_WS_ENABLED=1 only for emergency standalone mode, "
            "or AUTOSTOCK_MAIN_PUSH_WS_AUTO_FALLBACK=1 for explicit memory-only fallback."
        )
        return None

    _prepare_main_memory_only_ws()
    return _runner_start_push_stream(*args, **kwargs)


def start(*args, **kwargs):
    return start_push_stream(*args, **kwargs)


def run_background(*args, **kwargs):
    return start_push_stream(*args, **kwargs)


def _runner_start_push_stream_guarded(*args, **kwargs):
    if _should_skip_push_stream_start_in_main():
        _mark_main_ws_skipped()
        logger.warning(
            "[push_stream] runner WS start skipped in main process; "
            "main_database.py handles PUSH WebSocket/registration/storage."
        )
        return None
    _prepare_main_memory_only_ws()
    return _TRUE_RUNNER_START_PUSH_STREAM(*args, **kwargs)


try:
    _runner_start_push_stream_guarded.__wrapped__ = _TRUE_RUNNER_START_PUSH_STREAM  # type: ignore[attr-defined]
    _runner_start_push_stream_guarded._push_stream_runner_true_original = _TRUE_RUNNER_START_PUSH_STREAM  # type: ignore[attr-defined]
    _runner_start_push_stream_guarded._push_stream_runner_guarded_no_recursion = True  # type: ignore[attr-defined]
    _runner_mod.start_push_stream = _runner_start_push_stream_guarded
    _runner_mod.start = _runner_start_push_stream_guarded
    _runner_mod.run_background = _runner_start_push_stream_guarded
except Exception:
    logger.debug("[push_stream] runner guard patch failed", exc_info=True)


__all__ = [
    "rotation_settings",
    "rotation_symbols",
    "rotation_register",
    "rotation_logging",
    "rotation_stability_patch",
    "rotation_liquidity_keep100_patch",
    "set_refresh_callable",
    "refresh_subscriptions",
    "get_ws_sender",
    "wait_until_connected",
    "is_connected",
    "get_push_dataframe",
    "clear_push_dataframe",
    "register_symbols",
    "enable_rotation",
    "start_push_stream",
    "stop_push_stream",
    "get_status",
    "start",
    "run_background",
]
