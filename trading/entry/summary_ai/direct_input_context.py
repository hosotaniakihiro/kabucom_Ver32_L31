# -*- coding: utf-8 -*-
# ============================================================
# File   : trading/entry/summary_ai/direct_input_context.py
# Version: V7-STABLE-ASYNC-DIRECT-REPAIR
# ------------------------------------------------------------
# 旧 core/startup/summary_ai_runtime_consistency_patch.py を summary_ai 側へ移設した。
#
# 目的は 1 つだけ:
#   「いま SUMMARY_AI へ直接渡された 1m df」をスレッドローカルへ置き、
#   global_context の summary_history が古いだけで safety guard に止められる
#   状態を防ぐ。読み手は candidate_refill._get_push_1m_context()。
#
# 旧 install() の内訳と行方:
#   _patch_hook_direct_context     -> このモジュール install_hook_direct_context()
#   _patch_safety_guard_context    -> runner ラップはこのモジュール
#                                     install_runner_direct_context()。
#                                     _get_push_1m_context の direct-first は
#                                     candidate_refill 本体へ本文化済み。
#   _install_fast_order_builder    -> trading/handlers/entry_order_builder.
#                                     apply_fast_order_builder_defaults()
#   _break_async_direct_cycle      -> **削除**。参照先の
#                                     summary_ai_async_entry_patch /
#                                     summary_ai_async_direct_dispatch_patch が
#                                     どちらも本文化済みで存在せず、
#                                     async_patch/direct_patch が常に None のため
#                                     どの分岐にも入らない死にコードだった。
#   _patch_executor_selection      -> **削除**。ネイティブの
#                                     executor._select_ai_ok_items と同じ
#                                     (_selected_pool[:cap]) を書き戻すだけの層で、
#                                     唯一の存在理由だった
#                                     final_board_guard_signature_compat_patch の
#                                     selection pool 差し替えを解体済みのため不要。
#   _install_blowoff_prefilter     -> **削除**。env を読んで返すだけの no-op。
#                                     本体は trading/summary/pipeline/blowoff_prefilter.py。
#
# watcher は load-bearing。strict_entry_defaults / more_candidates などが
# run_summary_ai_entry_from_df を包み直すと direct context のラッパーが
# 剥がれるため、毎秒掛け直して定常状態を保っている。
#
# 旧 install 元は sitecustomize.py の 3 箇所 (BG / LAST など)。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V7-STABLE-ASYNC-DIRECT-REPAIR"
_INSTALLED = False
_WATCHER_STARTED = False
_TLS = threading.local()
_LAST_DIRECT_DF: Any = None
_LAST_DIRECT_AT: float = 0.0


def _env_bool(name: str, default: bool = True) -> bool:
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


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _is_df(v: Any) -> bool:
    try:
        import pandas as pd

        return isinstance(v, pd.DataFrame) and not v.empty
    except Exception:
        return False


def _extract_df(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    try:
        for k in ("summary_df", "df", "source_df", "base_df"):
            if _is_df(kwargs.get(k)):
                return kwargs.get(k)
        for x in args:
            if _is_df(x):
                return x
    except Exception:
        pass
    return None


def _latest_dt(df: Any) -> dt.datetime | None:
    try:
        import pandas as pd

        if not _is_df(df):
            return None
        for c in ("datetime", "end_time", "last_update", "updated_at", "inserted_at", "time"):
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce")
                try:
                    s = s.dt.tz_localize(None)
                except Exception:
                    pass
                mx = s.max()
                if pd.notna(mx):
                    ts = pd.Timestamp(mx)
                    try:
                        if ts.tzinfo is not None:
                            ts = ts.tz_localize(None)
                    except Exception:
                        pass
                    return ts.to_pydatetime().replace(tzinfo=None)
    except Exception:
        pass
    return None


def df_age(df: Any) -> tuple[bool, float | None, dt.datetime | None, int]:
    try:
        if not _is_df(df):
            return False, None, None, 0
        latest = _latest_dt(df)
        if latest is None:
            return False, None, None, len(df)
        age = (dt.datetime.now().replace(tzinfo=None) - latest).total_seconds()
        return True, age, latest, len(df)
    except Exception:
        return False, None, None, 0


def fresh_direct_df(df: Any) -> bool:
    ok, age, _latest, _rows = df_age(df)
    if not ok or age is None:
        return False
    max_age = _env_float("SUMMARY_AI_DIRECT_INPUT_MAX_AGE_SEC", _env_float("SUMMARY_AI_MAX_PUSH_1M_AGE_SEC", 120.0))
    return age <= max_age


def set_direct_context(df: Any) -> tuple[Any, bool]:
    global _LAST_DIRECT_DF, _LAST_DIRECT_AT
    prev = getattr(_TLS, "direct_df", None)
    if _is_df(df):
        _TLS.direct_df = df
        _LAST_DIRECT_DF = df
        _LAST_DIRECT_AT = time.time()
        return prev, True
    return prev, False


def restore_direct_context(prev: Any) -> None:
    try:
        _TLS.direct_df = prev
    except Exception:
        pass


def resolve_direct_df() -> Any:
    """新鮮な direct 入力があれば返す。無ければ None。

    candidate_refill._get_push_1m_context() の先頭から呼ばれる
    (旧 _get_push_1m_context_direct_first ラッパーの本文化先)。
    """
    if not _env_bool("SUMMARY_AI_DIRECT_INPUT_FRESH_CHECK", True):
        return None
    direct = getattr(_TLS, "direct_df", None)
    if fresh_direct_df(direct):
        ok, age, latest, rows = df_age(direct)
        logger.warning("[SUMMARY AI DIRECT INPUT GUARD] fresh-check source=direct_runner_input rows=%s latest=%s age=%.1f version=%s", rows, latest, float(age or 0.0), VERSION)
        return direct
    if fresh_direct_df(_LAST_DIRECT_DF):
        ok, age, latest, rows = df_age(_LAST_DIRECT_DF)
        logger.warning("[SUMMARY AI DIRECT INPUT GUARD] fresh-check source=last_direct_input rows=%s latest=%s age=%.1f version=%s", rows, latest, float(age or 0.0), VERSION)
        return _LAST_DIRECT_DF
    return None


def install_hook_direct_context() -> bool:
    """summary_ai_entry_hook_v20.run_summary_ai_entry_safe を包む。"""
    if not _env_bool("SUMMARY_AI_DIRECT_INPUT_FRESH_CHECK", True):
        return False
    try:
        import scheduler_jobs.summary.summary_ai_entry_hook_v20 as hook

        cur = getattr(hook, "run_summary_ai_entry_safe", None)
        if not callable(cur) or getattr(cur, "_summary_ai_hook_direct_input_v3", False):
            return False

        @wraps(cur)
        def _run_summary_ai_entry_safe_with_direct_df(*args: Any, **kwargs: Any):
            df = _extract_df(args, kwargs)
            prev, set_ok = set_direct_context(df)
            if set_ok:
                ok, age, latest, rows = df_age(df)
                logger.warning("[SUMMARY AI DIRECT INPUT GUARD] hook context set rows=%s latest=%s age=%.1f version=%s", rows, latest, float(age or 0.0), VERSION)
            try:
                return cur(*args, **kwargs)
            finally:
                restore_direct_context(prev)

        _run_summary_ai_entry_safe_with_direct_df._summary_ai_hook_direct_input_v3 = True  # type: ignore[attr-defined]
        _run_summary_ai_entry_safe_with_direct_df._summary_ai_hook_direct_input_v2 = True  # type: ignore[attr-defined]
        _run_summary_ai_entry_safe_with_direct_df._summary_ai_hook_direct_input_v1 = True  # type: ignore[attr-defined]
        _run_summary_ai_entry_safe_with_direct_df._original = cur  # type: ignore[attr-defined]
        hook.run_summary_ai_entry_safe = _run_summary_ai_entry_safe_with_direct_df
        logger.warning("[SUMMARY AI DIRECT INPUT GUARD] hook wrapped version=%s", VERSION)
        return True
    except Exception:
        logger.debug("[SUMMARY AI DIRECT INPUT GUARD] hook wrap not ready", exc_info=True)
        return False


def install_runner_direct_context() -> bool:
    """run_summary_ai_entry_from_df を包んで direct context を張る。

    読み手 (_get_push_1m_context) は candidate_refill 層から呼ばれるため、
    ネイティブの中では遅すぎる。ラッパーのまま残す必要がある。
    """
    if not _env_bool("SUMMARY_AI_DIRECT_INPUT_FRESH_CHECK", True):
        return False
    try:
        import trading.entry.summary_ai.runner as runner

        cur = getattr(runner, "run_summary_ai_entry_from_df", None)
        if not callable(cur) or getattr(cur, "_summary_ai_direct_input_v3", False):
            return False

        @wraps(cur)
        def _run_with_direct_df_context(*args: Any, **kwargs: Any):
            df = _extract_df(args, kwargs)
            prev, _ = set_direct_context(df)
            try:
                return cur(*args, **kwargs)
            finally:
                restore_direct_context(prev)

        _run_with_direct_df_context._summary_ai_direct_input_v3 = True  # type: ignore[attr-defined]
        _run_with_direct_df_context._summary_ai_direct_input_v2 = True  # type: ignore[attr-defined]
        _run_with_direct_df_context._summary_ai_direct_input_v1 = True  # type: ignore[attr-defined]
        _run_with_direct_df_context._original = cur  # type: ignore[attr-defined]
        runner.run_summary_ai_entry_from_df = _run_with_direct_df_context
        logger.warning("[SUMMARY AI DIRECT INPUT GUARD] installed version=%s", VERSION)
        return True
    except Exception:
        logger.debug("[SUMMARY AI DIRECT INPUT GUARD] install not ready", exc_info=True)
        return False


def _install_fast_order_builder() -> bool:
    try:
        import trading.handlers.entry_order_builder as eob

        fn = getattr(eob, "apply_fast_order_builder_defaults", None)
        return bool(fn()) if callable(fn) else False
    except Exception:
        logger.debug("[SUMMARY AI RUNTIME CONSISTENCY] fast order builder install not ready", exc_info=True)
        return False


def enforce(reason: str = "install") -> bool:
    changed_hook = install_hook_direct_context()
    changed_direct = install_runner_direct_context()
    changed_fast_order = _install_fast_order_builder()
    changed = bool(changed_hook or changed_direct or changed_fast_order)
    if changed:
        logger.warning(
            "[SUMMARY AI RUNTIME CONSISTENCY] enforce reason=%s hook=%s direct_input=%s fast_order=%s version=%s",
            reason, changed_hook, changed_direct, changed_fast_order, VERSION,
        )
    return changed


def _watcher() -> None:
    loops = max(1, _env_int("SUMMARY_AI_CONSISTENCY_WATCH_LOOPS", 240))
    sleep_sec = max(0.5, _env_float("SUMMARY_AI_CONSISTENCY_WATCH_INTERVAL", 1.0))
    for i in range(loops):
        try:
            enforce(reason=f"watcher:{i}")
        except Exception:
            pass
        time.sleep(sleep_sec)
    logger.warning("[SUMMARY AI RUNTIME CONSISTENCY] watcher done loops=%s version=%s", loops, VERSION)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    if not _env_bool("SUMMARY_AI_RUNTIME_CONSISTENCY_ENABLED", True):
        logger.warning("[SUMMARY AI RUNTIME CONSISTENCY] disabled by env")
        return False
    changed = enforce(reason="install")
    if not _WATCHER_STARTED and _env_bool("SUMMARY_AI_RUNTIME_CONSISTENCY_WATCHER", True):
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="summary-ai-runtime-consistency-watch", daemon=True).start()
        logger.warning("[SUMMARY AI RUNTIME CONSISTENCY] watcher started version=%s", VERSION)
    _INSTALLED = bool(changed or _WATCHER_STARTED)
    logger.warning("[SUMMARY AI RUNTIME CONSISTENCY] installed ok=%s changed=%s version=%s", _INSTALLED, changed, VERSION)
    return _INSTALLED


__all__ = [
    "VERSION",
    "install",
    "enforce",
    "resolve_direct_df",
    "install_hook_direct_context",
    "install_runner_direct_context",
]
