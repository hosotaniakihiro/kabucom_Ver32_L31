# -*- coding: utf-8 -*-
"""
Full pipeline stability runtime patch.

Purpose
-------
Apply the remaining production-safe fixes in one startup patch, in the order
requested by operation priority:

1. Yahoo complement: reduce non-differential summary reflection during market.
2. kabu Station token/API key: propagate startup token to all common holders,
   then keep runtime calls on settings.ini token without automatic refresh.
3. PUSH A/B rotation: use strict clear/register timing and avoid stale register state.
4. 3m/5m summary recovery: enable DB/global-context fallback instead of hard empty.
5. Entry/exit scheduler congestion: shorten per-symbol waits and avoid one candidate
   blocking the whole entry pass.
6. PUSH summary cache: prefer fresh latest rows when merged history is stale.

This patch intentionally keeps final safety guards such as liquidity, position,
order, and hard API errors. It only removes unnecessary duplicate work and stale
state that caused low entry counts and long scheduler stalls.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from functools import wraps
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)
VERSION = "V1.2-FULL-PIPELINE-STABILITY-STARTUP-ONCE-TOKEN"
_INSTALLED = False


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _argv_context() -> str:
    try:
        text = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if "main_database.py" in text:
            return "main_database"
        if "main.py" in text:
            return "main"
        if text:
            return "helper"
    except Exception:
        pass
    return "unknown"


def _setdefault_env(name: str, value: Any) -> None:
    os.environ.setdefault(name, str(value))


def _publish_token(token: Any, source: str = "unknown") -> bool:
    token_s = str(token or "").strip()
    if not token_s:
        return False
    try:
        for key in ("KABU_API_KEY", "KABUSAPI_TOKEN", "X_API_KEY", "KABU_STATION_TOKEN"):
            os.environ[key] = token_s
        # Common global holders used by older modules.
        for mod_name in ("global_data", "core.global_data"):
            try:
                mod = __import__(mod_name, fromlist=["*"])
            except Exception:
                continue
            try:
                setattr(mod, "API_TOKEN", token_s)
            except Exception:
                pass
            try:
                setattr(mod, "api_token", token_s)
            except Exception:
                pass
            try:
                headers = getattr(mod, "headers", None)
                if isinstance(headers, dict):
                    headers["X-API-KEY"] = token_s
                    headers["X_API_KEY"] = token_s
                else:
                    setattr(mod, "headers", {"X-API-KEY": token_s})
            except Exception:
                pass
        logger.info("[FULL PIPELINE STABILITY] token published source=%s token_len=%s", source, len(token_s))
        return True
    except Exception:
        logger.exception("[FULL PIPELINE STABILITY] token publish failed source=%s", source)
        return False


def _install_token_startup_once_policy() -> bool:
    """Disable automatic runtime token refresh after startup.

    main_database.py may refresh once at startup and save settings.ini before this
    patch is installed.  After this point, all processes should read settings.ini
    / runtime cache and should not call /token automatically on 401.
    """
    try:
        if os.environ.get("DISABLE_TOKEN_STARTUP_ONCE_POLICY_PATCH", "").strip() == "1":
            logger.warning("[FULL PIPELINE STABILITY] startup-once token policy disabled by env")
            return False
        # token_startup_once_policy_patch は kabu_api/token_startup_policy.py へ本文化済み
        # (refresh_token の置き換えは token_manager 側が持つ)。
        from kabu_api import token_startup_policy

        return bool(token_startup_policy.enable_token_startup_policy())
    except Exception:
        logger.exception("[FULL PIPELINE STABILITY] startup-once token policy install failed")
        return False


def publish_token_result(name: str, ret: Any) -> Any:
    """旧 _install_token_unifier のラッパー本体。

    token_manager.<name> の戻り値からトークンを取り出して各所へ配る。
    戻り値はそのまま返す。
    """
    token = ret
    if isinstance(ret, dict):
        token = ret.get("Token") or ret.get("token") or ret.get("api_key") or ret.get("APIKey")
    _publish_token(token, source=f"token_manager.{name}")
    return ret


def publish_loaded_token() -> bool:
    """旧 _install_token_unifier の末尾。既に読み込み済みのトークンを配る。"""
    ok = False
    try:
        import token_manager  # type: ignore
    except Exception:
        return False
    for attr in ("API_TOKEN", "api_token", "TOKEN", "token"):
        try:
            token = getattr(token_manager, attr, None)
            if token:
                _publish_token(token, source=f"token_manager.{attr}")
                ok = True
        except Exception:
            pass
    return ok


def reflect_saved_yahoo_differential(orig, *args: Any, **kwargs: Any) -> Any:
    """旧 _reflect_wrapper (download_flow.reflect_saved_yahoo_to_summary_db 差し替え)。

    pre-download / empty-download の全銘柄 reflect を飛ばす。
    post-download の反映はそのまま通す。
    """
    label = str(kwargs.get("label") or "")
    symbols = kwargs.get("symbols")
    try:
        if symbols is None and len(args) >= 2:
            # Do not mutate positional args; only use for diagnostics.
            symbols = args[1]
    except Exception:
        symbols = None
    count = len(symbols) if isinstance(symbols, (list, tuple, set)) else None

    skip_pre = _truthy(os.environ.get("YAHOO_LIVE_SKIP_PRE_DOWNLOAD_REFLECT", "1"))
    skip_empty = _truthy(os.environ.get("YAHOO_LIVE_SKIP_EMPTY_DOWNLOAD_REFLECT", "1"))
    is_pre = "pre-download" in label or "pre_download" in label
    is_empty = "empty-download" in label or "empty_download" in label
    if (is_pre and skip_pre) or (is_empty and skip_empty):
        logger.warning(
            "[FULL PIPELINE STABILITY][YAHOO] reflect skipped label=%s symbols=%s reason=%s",
            label,
            count,
            "pre_download" if is_pre else "empty_download",
        )
        return {"ok": True, "skipped": True, "label": label, "symbols": count, "reason": "differential_reflect"}

    return orig(*args, **kwargs)


def _install_push_rotation_defaults() -> bool:
    """Set strict A/B rotation and short register waits."""
    defaults = {
        "PUSH_ROTATION_STRICT_SEQUENCE": "1",
        "PUSH_ROTATION_USE_UNREGISTER_ALL": "1",
        "PUSH_ROTATION_REGISTER_SECONDS": "4.8",
        "PUSH_ROTATION_UNREGISTER_WAIT_SEC": "0.2",
        "PUSH_ROTATION_WAIT_AFTER_CLEAR_SEC": "0.2",
        "PUSH_REGISTER_ABORT_IF_CLEAR_FAILED": "1",
        "PUSH_REGISTER_RETRY_ON_REGIST_COUNT_ERROR": "1",
        "PUSH_REGISTER_LOCK_TIMEOUT_SEC": "2.0",
        "PUSH_REGISTER_HTTP_TIMEOUT_SEC": "3.0",
        # Do not call /token during live rotation.  Use settings.ini token only.
        "PUSH_REGISTER_RECOVERY_REFRESH_TOKEN": "0",
        "PUSH_REGISTER_AUTH_RETRY_ENABLED": "0",
        "BOARD_RETRY_DURING_ROTATION_SEC": "0.2",
        "BOARD_ALLOW_MISSING_DURING_ROTATION": "1",
    }
    for k, v in defaults.items():
        _setdefault_env(k, v)
    return True


def _install_summary_recovery_defaults() -> bool:
    defaults = {
        "SUMMARY_RECOVER_TF3_TF5_FROM_DB": "1",
        "SUMMARY_RECOVER_EMPTY_TF_FROM_LAST_GOOD": "1",
        "SUMMARY_KEEP_LAST_GOOD_CONTEXT": "1",
        "SUMMARY_DB_BACKFILL_FOR_MTF": "1",
        "SUMMARY_ALLOW_STALE_LAST_GOOD_SEC": "900",
        "ENTRY_ORDER_REQUIRE_MTF_DATA": "0",
        "ENTRY_SHORT_MTF_MIN_AVAILABLE": "1",
        "ENTRY_SHORT_MTF_MIN_ALIGNED": "1",
        "ENTRY_DAILY_MTF_OPTIONAL": "1",
        # If interval=1 history cache becomes stale, latest PUSH rows must win.
        "SUMMARY_FORCE_LATEST_WHEN_HISTORY_LAG_SEC": "180",
        "SUMMARY_FORCE_LATEST_MIN_SYMBOLS": "20",
    }
    for k, v in defaults.items():
        _setdefault_env(k, v)
    return True


def _install_summary_latest_prefer() -> bool:
    """summary_latest_prefer_patch は trading/summary/controller_cache.py へ本文化済み。

    choose_merged_cache_payload 本体に組み込んだので install は不要。
    キルスイッチ DISABLE_SUMMARY_LATEST_PREFER_PATCH /
    DISABLE_FULL_PIPELINE_STABILITY_PATCH は controller_cache._lp_enabled() が
    そのまま見ている。summary_controller.py:38 の from-import も
    最初から合成後の関数を掴むため、旧 install が行っていた
    sc.choose_merged_cache_payload の上書きも要らなくなった。
    """
    return True


def _install_entry_exit_congestion_defaults() -> bool:
    defaults = {
        "ENTRY_BOARD_WAIT_SEC": "0.2",
        "ENTRY_BOARD_EXTRA_RETRY": "0",
        "ENTRY_ORDER_BUILD_TIMEOUT_SEC": "4.0",
        "ENTRY_PER_SYMBOL_TIMEOUT_SEC": "3.0",
        "ENTRY_PASS_TIMEOUT_SEC": "12.0",
        "RANKING_ENTRY_BUILD_TIMEOUT_SEC": "12.0",
        "TONOSAMA_ENTRY_TIMEOUT_SEC": "12.0",
        "FINAL_ENTRY_SAFETY_ORIG_TIMEOUT_SEC": "5.0",
        "ENTRY_CONTINUE_NEXT_ON_ORDER_BUILD_NG": "1",
        "ENTRY_CONTINUE_NEXT_ON_TIMEOUT": "1",
        "EXIT_LOOP_SKIP_HEAVY_WHEN_NO_POSITION": "1",
        "EXIT_LOOP_NO_POSITION_INTERVAL_SEC": "15",
        "EXIT_LOOP_POSITION_INTERVAL_SEC": "5",
    }
    for k, v in defaults.items():
        _setdefault_env(k, v)
    return True


def _install_register_ops_guard() -> bool:
    # 旧 install はここで register_ops の refresh_subscriptions /
    # register_symbols / register_items を包もうとしていたが、
    # trading/push/subscription_manager/register_ops.py にはどの名前も
    # 定義も再エクスポートも無く、常に False を返す不活性な処理だった。
    return False


_ENABLED = False


def pipeline_stability_enabled() -> bool:
    """旧 install() が走ったか。token_manager / download_flow が見る。"""
    return bool(_ENABLED)


def enable_pipeline_stability() -> bool:
    """旧 install()。関数差し替えは本文化済みなので env と publish だけ。"""
    global _INSTALLED, _ENABLED
    if _INSTALLED:
        return True
    try:
        if _truthy(os.environ.get("DISABLE_FULL_PIPELINE_STABILITY_PATCH")):
            logger.warning("[FULL PIPELINE STABILITY] disabled by env")
            return False
        context = _argv_context()
        token_policy_ok = _install_token_startup_once_policy()
        _ENABLED = True
        token_ok = publish_loaded_token()
        push_env_ok = _install_push_rotation_defaults()
        push_guard_ok = _install_register_ops_guard()
        summary_ok = _install_summary_recovery_defaults()
        summary_latest_ok = _install_summary_latest_prefer()
        entry_ok = _install_entry_exit_congestion_defaults()
        _INSTALLED = True
        logger.warning(
            "[FULL PIPELINE STABILITY] installed version=%s context=%s yahoo_diff=native token_policy=%s token=%s push_env=%s push_guard=%s summary_recovery=%s summary_latest=%s entry_exit=%s",
            VERSION,
            context,
            token_policy_ok,
            token_ok,
            push_env_ok,
            push_guard_ok,
            summary_ok,
            summary_latest_ok,
            entry_ok,
        )
        return True
    except Exception:
        logger.exception("[FULL PIPELINE STABILITY] install failed")
        return False


__all__ = [
    "VERSION",
    "enable_pipeline_stability",
    "pipeline_stability_enabled",
    "publish_token_result",
    "reflect_saved_yahoo_differential",
]
