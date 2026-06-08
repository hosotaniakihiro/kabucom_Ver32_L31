# ============================================================
# File   : core/startup/kabu_api_token_runtime_patch.py
# Version: V2-IDEMPOTENT-FAST-STARTUP
# ------------------------------------------------------------
# 目的:
#   force_cancel_loop.py / kabu_api.positions.py が古い token 属性
#   global_data.API_TOKEN / global_data.token_value を直接見て、
#   startup_config の token refresh 後も `API TOKEN not ready` / `token 不在`
#   を出し続ける問題を runtime patch で吸収する。
#
# V2:
#   - 起動時に多数のstartup patchから install() が連鎖呼び出しされても、
#     2回目以降は即returnして再wrap/大量ログを出さない。
#   - force_cancel_loop / kabu_api.positions 側にも marker を付け、
#     既にpatch済みなら再代入しない。
# ============================================================
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_OK: bool | None = None


def _resolve_token() -> str | None:
    try:
        from global_state import global_data
        for name in ("token_value", "API_TOKEN", "api_token", "token"):
            try:
                token = getattr(global_data, name, None)
                if token:
                    token = str(token)
                    _sync_global_token(token)
                    return token
            except Exception:
                pass
    except Exception:
        global_data = None  # noqa: F841

    try:
        from token_manager import get_valid_token
        token = get_valid_token()
        if token:
            token = str(token)
            _sync_global_token(token)
            return token
    except Exception:
        logger.debug("[KABU API TOKEN PATCH] get_valid_token failed", exc_info=True)

    return None


def _sync_global_token(token: str) -> None:
    if not token:
        return
    try:
        from global_state import global_data
        for name in ("token_value", "API_TOKEN", "api_token", "token"):
            try:
                setattr(global_data, name, token)
            except Exception:
                pass
    except Exception:
        pass

    try:
        import kabu_api.global_data as kgd  # type: ignore
        for name in ("token_value", "API_TOKEN", "api_token", "token"):
            try:
                setattr(kgd, name, token)
            except Exception:
                pass
    except Exception:
        pass


def _patch_force_cancel_loop() -> bool:
    try:
        import force_cancel_loop as fcl
        old_has = getattr(fcl, "_has_api_token", None)
        old_headers = getattr(fcl, "_safe_get_headers", None)
        if getattr(old_has, "_kabu_api_token_runtime_patch_v2", False) and getattr(old_headers, "_kabu_api_token_runtime_patch_v2", False):
            return True

        def _has_api_token_patched() -> bool:
            return bool(_resolve_token())

        def _safe_get_headers_patched(context: str) -> dict[str, str] | None:
            try:
                from kabu_api.api_common import get_headers
                token = _resolve_token()
                if not token:
                    logger.warning("[FORCE_CANCEL] API TOKEN not ready; skip %s", context)
                    return None
                return get_headers()
            except RuntimeError as e:
                if "API TOKEN is not set" in str(e):
                    logger.warning("[FORCE_CANCEL] API TOKEN not ready in get_headers; skip %s", context)
                    return None
                raise
            except Exception:
                logger.exception("[FORCE_CANCEL] get_headers failed; skip %s", context)
                return None

        _has_api_token_patched._kabu_api_token_runtime_patch_v2 = True  # type: ignore[attr-defined]
        _safe_get_headers_patched._kabu_api_token_runtime_patch_v2 = True  # type: ignore[attr-defined]
        fcl._has_api_token = _has_api_token_patched
        fcl._safe_get_headers = _safe_get_headers_patched
        logger.warning("[KABU API TOKEN PATCH] force_cancel_loop token helpers patched v2")
        return True
    except Exception:
        logger.exception("[KABU API TOKEN PATCH] force_cancel_loop patch failed")
        return False


def _patch_positions_module() -> bool:
    try:
        import kabu_api.positions as pos
        import requests
        import time

        if getattr(getattr(pos, "get_positions", None), "_kabu_api_token_runtime_patch_v2", False) and getattr(getattr(pos, "sync_positions_from_kabus", None), "_kabu_api_token_runtime_patch_v2", False):
            return True

        API_URL = getattr(pos, "API_URL", "http://localhost:18080/kabusapi")
        logger_pos = getattr(pos, "logger", logger)

        def get_positions_patched():
            now = time.time()
            try:
                cache = getattr(pos, "_POS_CACHE", None)
                cache_time = float(getattr(pos, "_POS_CACHE_TIME", 0.0) or 0.0)
                cache_ttl = float(getattr(pos, "_POS_CACHE_TTL", 5.0) or 5.0)
                if cache is not None and (now - cache_time) < cache_ttl:
                    return cache
            except Exception:
                pass

            token = _resolve_token()
            if not token:
                logger_pos.warning("⚠ get_positions: token 不在 → skip")
                return getattr(pos, "_POS_CACHE", None) or []

            headers = {"Content-Type": "application/json", "X-API-KEY": token}
            url = f"{API_URL}/positions"
            for i in range(3):
                try:
                    res = requests.get(url, headers=headers, timeout=10)
                    if res.status_code == 429:
                        logger_pos.warning("⚠ /positions rate limited (429) → skip this cycle")
                        time.sleep(0.5)
                        return getattr(pos, "_POS_CACHE", None) or []
                    res.raise_for_status()
                    positions = res.json()
                    if not isinstance(positions, list):
                        logger_pos.error("❌ /positions 不正レスポンス: %s", positions)
                        return getattr(pos, "_POS_CACHE", None) or []
                    pos._POS_CACHE = positions
                    pos._POS_CACHE_TIME = now
                    return positions
                except requests.exceptions.ReadTimeout:
                    logger_pos.warning("⚠ get_positions timeout retry=%s", i + 1)
                    time.sleep(0.5)
                except requests.exceptions.ConnectionError:
                    logger_pos.warning("⚠ get_positions connection error retry=%s", i + 1)
                    time.sleep(0.5)
                except Exception:
                    logger_pos.error("❌ get_positions unexpected error", exc_info=True)
                    return getattr(pos, "_POS_CACHE", None) or []
            logger_pos.warning("⚠ get_positions failed after retries → use cache")
            return getattr(pos, "_POS_CACHE", None) or []

        old_sync = getattr(pos, "sync_positions_from_kabus", None)
        if getattr(old_sync, "_kabu_api_token_runtime_patch_v2", False):
            old_sync = getattr(old_sync, "_original", None)

        def sync_positions_from_kabus_patched(*args: Any, **kwargs: Any):
            token = _resolve_token()
            if not token:
                logger_pos.warning("⚠ sync_positions: token 不在 → skip")
                return None
            if callable(old_sync):
                return old_sync(*args, **kwargs)
            return None

        get_positions_patched._kabu_api_token_runtime_patch_v2 = True  # type: ignore[attr-defined]
        sync_positions_from_kabus_patched._kabu_api_token_runtime_patch_v2 = True  # type: ignore[attr-defined]
        sync_positions_from_kabus_patched._original = old_sync  # type: ignore[attr-defined]
        pos.get_positions = get_positions_patched
        pos.sync_positions_from_kabus = sync_positions_from_kabus_patched
        logger.warning("[KABU API TOKEN PATCH] kabu_api.positions patched v2")
        return True
    except Exception:
        logger.exception("[KABU API TOKEN PATCH] kabu_api.positions patch failed")
        return False


def install() -> bool:
    global _INSTALLED, _OK
    if _INSTALLED:
        return bool(_OK)
    ok1 = _patch_force_cancel_loop()
    ok2 = _patch_positions_module()
    _OK = bool(ok1 or ok2)
    _INSTALLED = True
    logger.warning("[KABU API TOKEN PATCH] installed v2 force_cancel=%s positions=%s", ok1, ok2)
    return bool(_OK)


try:
    install()
except Exception:
    logger.exception("[KABU API TOKEN PATCH] auto install failed")

__all__ = ["install"]
