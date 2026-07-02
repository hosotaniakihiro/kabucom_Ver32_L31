# ============================================================
# File   : trading/push/push_stream/__init__.py
# Version: Ver2.2-MAIN-STALE-MEMORY-WS-FALLBACK-WATCHER
# ------------------------------------------------------------
# ✔ 旧 trading.push.push_stream 公開API互換
# ✔ 分割後モジュールの再エクスポート
# ✔ transport / rotation_core / runner / dataframe の公開窓口
# ✔ main_database.py 分離運用時、main.py側の重複PUSH WSをno-op化
# ✔ emergency時のみ AUTOSTOCK_MAIN_PUSH_WS_ENABLED=1 で main.py側WSを許可
# ✔ Ver2.2: main_database側PUSH/summaryがstale化した時だけ、main.py側で
#           DB保存なし・登録ローテなしの memory-only fallback WS を起動する監視を追加
# ✔ PUSH DB重複保存はしない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
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
_WATCHER_STARTED = False
_FALLBACK_STARTED = False
_FALLBACK_LOCK = threading.Lock()


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


def _split_mode_main() -> bool:
    try:
        from data_collectors.split_mode import (
            is_data_collector_process,
            should_skip_data_collector_work_in_main,
            external_data_collectors_enabled,
        )
        if bool(is_data_collector_process()):
            return False
        return bool(external_data_collectors_enabled()) and bool(should_skip_data_collector_work_in_main())
    except Exception:
        logger.debug("[push_stream] split mode check failed", exc_info=True)
        return False


def _main_ws_fallback_enabled() -> bool:
    # Ver2.2: 明示OFFされない限り、stale時のmemory-only fallback watcherは有効。
    return _env_bool("AUTOSTOCK_MAIN_PUSH_WS_AUTO_FALLBACK", _env_bool("AUTOSTOCK_MAIN_PUSH_WS_STALE_FALLBACK", True))


def _main_ws_fallback_allowed_due_to_stale_summary() -> bool:
    """
    split mode の main.py 側 memory-only PUSH WS fallback。

    通常は main_database.py / data_collectors_runner が PUSH受信・保存を担当する。
    Ver2.2 では、summary が stale になった時だけ main.py 側 memory-only WS を許可する。
    DB保存と登録ローテは必ずOFFにするため、PUSH DBの重複保存は起きない。
    """
    if not _main_ws_fallback_enabled():
        return False
    stale_sec = max(30.0, _env_float("AUTOSTOCK_MAIN_PUSH_WS_FALLBACK_STALE_SEC", 180.0))
    age = _latest_summary_age_sec()
    if age is None:
        # 起動直後の二重WSを避けるため、no-summaryだけでは即許可しない。
        no_summary_grace = max(60.0, _env_float("AUTOSTOCK_MAIN_PUSH_WS_NO_SUMMARY_GRACE_SEC", 300.0))
        logger.info(
            "[push_stream] main WS fallback not yet allowed: no summary visible grace=%.1fs",
            no_summary_grace,
        )
        return False
    if age > stale_sec:
        logger.warning(
            "[push_stream] main WS fallback allowed: summary stale age=%.1fs stale_sec=%.1f memory_only=True db_write=False rotation=False",
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
      - summary stale の時だけ memory-only fallback
    """
    if _env_bool("AUTOSTOCK_MAIN_PUSH_WS_ENABLED", False):
        logger.warning("[push_stream] main WS explicitly enabled by AUTOSTOCK_MAIN_PUSH_WS_ENABLED=1")
        return False
    try:
        if not _split_mode_main():
            return False
        if _main_ws_fallback_allowed_due_to_stale_summary():
            return False
        _start_main_stale_fallback_watcher()
        return True
    except Exception:
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


def _start_memory_only_fallback_ws(reason: str) -> bool:
    global _FALLBACK_STARTED
    with _FALLBACK_LOCK:
        if _FALLBACK_STARTED:
            return True
        try:
            _prepare_main_memory_only_ws()
            logger.warning(
                "[push_stream] starting main memory-only fallback WS reason=%s db_write=%s rotation=%s version=Ver2.2-MAIN-STALE-MEMORY-WS-FALLBACK-WATCHER",
                reason,
                os.environ.get("PUSH_DB_WRITE_ENABLED"),
                os.environ.get("PUSH_ROTATION_ENABLED"),
            )
            ret = _TRUE_RUNNER_START_PUSH_STREAM()
            _FALLBACK_STARTED = True
            logger.warning("[push_stream] main memory-only fallback WS started reason=%s ret=%s", reason, ret)
            return True
        except Exception:
            logger.exception("[push_stream] main memory-only fallback WS start failed reason=%s", reason)
            return False


def _start_main_stale_fallback_watcher() -> None:
    global _WATCHER_STARTED
    if _WATCHER_STARTED:
        return
    if not _main_ws_fallback_enabled():
        return
    if not _split_mode_main():
        return
    _WATCHER_STARTED = True

    def _watch() -> None:
        interval = max(5.0, _env_float("AUTOSTOCK_MAIN_PUSH_WS_FALLBACK_CHECK_SEC", 15.0))
        stale_sec = max(30.0, _env_float("AUTOSTOCK_MAIN_PUSH_WS_FALLBACK_STALE_SEC", 180.0))
        logger.warning(
            "[push_stream] main stale fallback watcher started interval=%.1fs stale_sec=%.1fs memory_only=True db_write=False rotation=False version=Ver2.2-MAIN-STALE-MEMORY-WS-FALLBACK-WATCHER",
            interval,
            stale_sec,
        )
        while True:
            try:
                if _FALLBACK_STARTED:
                    return
                age = _latest_summary_age_sec()
                if age is not None and age > stale_sec:
                    _start_memory_only_fallback_ws(reason=f"summary_stale_age={age:.1f}s")
                    return
                logger.info("[push_stream] main stale fallback watcher heartbeat age=%s stale_sec=%.1f", f"{age:.1f}" if age is not None else None, stale_sec)
            except Exception:
                logger.exception("[push_stream] main stale fallback watcher loop failed")
            time.sleep(interval)

    threading.Thread(target=_watch, name="main-push-memory-fallback-watch", daemon=True).start()


def start_push_stream(*args, **kwargs):
    if _should_skip_push_stream_start_in_main():
        _mark_main_ws_skipped()
        logger.warning(
            "[push_stream] WS start skipped in main process; "
            "main_database.py handles PUSH WebSocket/registration/storage. "
            "memory-only stale fallback watcher is active when enabled."
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
            "main_database.py handles PUSH WebSocket/registration/storage. "
            "memory-only stale fallback watcher is active when enabled."
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
