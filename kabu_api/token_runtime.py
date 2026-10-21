# ============================================================
# File   : kabu_api/token_runtime.py
# Version: V5-DIRECT-HEADERS-TRANSIENT-401-SAFE
# ------------------------------------------------------------
# 目的:
#   force_cancel_loop.py / kabu_api.positions.py が古い token 属性
#   global_data.API_TOKEN / global_data.token_value を直接見て、
#   startup_config の token refresh 後も `API TOKEN not ready` / `token 不在`
#   を出し続ける問題を runtime patch で吸収する。
#
# V3:
#   - kabu_api.positions.get_positions が 401 を受けたら token_manager.refresh_token()
#     で再発行し、global tokenへ同期して1回だけリトライする。
#   - refresh_token の引数有無差異に対応する。
#
# V4:
#   - PUSH register/unregister のローテーションと token refresh が競合した場合、
#     /positions が refresh 後も一時的に 401 になることがある。
#   - 2回目以降の 401 を unexpected ERROR に落とさず、この周期だけ cache/[] を返す。
#
# V5:
#   - force_cancel_loop の _safe_get_headers を get_headers() 依存に戻さず、
#     _resolve_token() で得た最新tokenから直接 X-API-KEY ヘッダーを作る。
#   - refresh直後の一時401は ERROR traceback に落とさず cache/[] を返す。
# ============================================================
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_OK: bool | None = None


def _sync_global_token(token: str) -> None:
    if not token:
        return
    token = str(token).strip()
    if not token:
        return
    try:
        from global_state import global_data
        for name in ("token_value", "API_TOKEN", "api_token", "token", "kabu_api_token"):
            try:
                setattr(global_data, name, token)
            except Exception:
                pass
    except Exception:
        pass

    try:
        import kabu_api.global_data as kgd  # type: ignore
        for name in ("token_value", "API_TOKEN", "api_token", "token", "kabu_api_token"):
            try:
                setattr(kgd, name, token)
            except Exception:
                pass
    except Exception:
        pass

    try:
        import token_manager
        for name in ("API_TOKEN", "token", "api_token"):
            try:
                setattr(token_manager, name, token)
            except Exception:
                pass
    except Exception:
        pass

    try:
        from trading.push.subscription_manager import register_ops
        for name in ("_API_KEY", "API_KEY", "_api_key"):
            try:
                setattr(register_ops, name, token)
            except Exception:
                pass
    except Exception:
        pass


def _make_headers(token: str) -> dict[str, str]:
    return {"Content-Type": "application/json", "X-API-KEY": str(token).strip()}


def _resolve_token() -> str | None:
    try:
        from global_state import global_data
        for name in ("token_value", "API_TOKEN", "api_token", "token", "kabu_api_token"):
            try:
                token = getattr(global_data, name, None)
                if token:
                    token = str(token).strip()
                    _sync_global_token(token)
                    return token
            except Exception:
                pass
    except Exception:
        global_data = None  # noqa: F841

    try:
        import kabu_api.global_data as kgd  # type: ignore
        for name in ("token_value", "API_TOKEN", "api_token", "token", "kabu_api_token"):
            try:
                token = getattr(kgd, name, None)
                if token:
                    token = str(token).strip()
                    _sync_global_token(token)
                    return token
            except Exception:
                pass
    except Exception:
        pass

    try:
        import token_manager
        for name in ("API_TOKEN", "token", "api_token"):
            try:
                token = getattr(token_manager, name, None)
                if token:
                    token = str(token).strip()
                    _sync_global_token(token)
                    return token
            except Exception:
                pass
    except Exception:
        pass

    try:
        from token_manager import get_valid_token
        token = get_valid_token()
        if token:
            token = str(token).strip()
            _sync_global_token(token)
            return token
    except Exception:
        logger.debug("[KABU API TOKEN PATCH] get_valid_token failed", exc_info=True)

    return None


def _refresh_token_safe(logger_pos: logging.Logger | None = None) -> str | None:
    log = logger_pos or logger
    try:
        import token_manager
    except Exception:
        log.warning("[KABU API TOKEN PATCH] token_manager import failed", exc_info=True)
        return None

    # 既存実装差異に対応: refresh_token() / refresh_token(api_password) の両方を試す。
    try:
        token = token_manager.refresh_token()
        if token:
            token = str(token).strip()
            _sync_global_token(token)
            log.warning("[KABU API TOKEN PATCH] token refreshed by refresh_token() token_len=%s", len(token))
            return token
    except TypeError:
        pass
    except Exception:
        log.warning("[KABU API TOKEN PATCH] refresh_token() failed; try with settings", exc_info=True)

    try:
        from pathlib import Path
        from configparser import ConfigParser
        root = Path(__file__).resolve().parents[2]
        conf = ConfigParser()
        conf.read(str(root / "settings.ini"), encoding="utf-8")
        section = "aukabu" if conf.has_section("aukabu") else "kabusapi"
        api_password = conf.get(section, "apipassword", fallback="")
        if not api_password:
            log.warning("[KABU API TOKEN PATCH] apipassword missing; cannot refresh token")
            return None
        token = token_manager.refresh_token(api_password)
        if token:
            token = str(token).strip()
            _sync_global_token(token)
            log.warning("[KABU API TOKEN PATCH] token refreshed with settings token_len=%s", len(token))
            return token
    except Exception:
        log.warning("[KABU API TOKEN PATCH] refresh_token(api_password) failed", exc_info=True)
    return None


def safe_get_headers(context: str) -> dict[str, str] | None:
    """旧 _safe_get_headers_patched (force_cancel_loop._safe_get_headers の完全置換)。"""
    token = _resolve_token()
    if not token:
        logger.warning("[FORCE_CANCEL] API TOKEN not ready; skip %s", context)
        return None
    return _make_headers(token)


# 旧 _patch_force_cancel_loop は _has_api_token も生やしていたが、
# force_cancel_loop.py 側に定義も参照も無く、誰も読まない不活性な属性だった。


def _pos_module():
    import kabu_api.positions as pos

    return pos


def _pos_logger():
    return getattr(_pos_module(), "logger", logger)


def _pos_api_url() -> str:
    return getattr(_pos_module(), "API_URL", "http://localhost:18080/kabusapi")


def _cached_positions() -> list[Any]:
    cached = getattr(_pos_module(), "_POS_CACHE", None)
    return cached if isinstance(cached, list) else []


def get_positions_with_token():
    """旧 get_positions_patched (kabu_api.positions.get_positions の完全置換)。

    token を解決し、401/429/タイムアウトはキャッシュへ退避する。
    """
    import requests
    import time

    pos = _pos_module()
    logger_pos = _pos_logger()
    API_URL = _pos_api_url()
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
        logger_pos.warning("⚠ get_positions: token 不在 → refresh attempt")
        token = _refresh_token_safe(logger_pos)
    if not token:
        logger_pos.warning("⚠ get_positions: token 不在 → skip")
        return _cached_positions()

    url = f"{API_URL}/positions"
    refreshed_after_401 = False
    for i in range(3):
        try:
            res = requests.get(url, headers=_make_headers(token), timeout=10)
            if res.status_code == 401:
                if not refreshed_after_401:
                    logger_pos.warning("⚠ get_positions got 401 -> refresh token and retry once")
                    new_token = _refresh_token_safe(logger_pos)
                    if new_token:
                        token = new_token
                        refreshed_after_401 = True
                        time.sleep(0.2)
                        continue
                    logger_pos.warning("⚠ get_positions token refresh after 401 failed -> use cache")
                    return _cached_positions()
                logger_pos.warning("⚠ get_positions still 401 after token refresh -> use cache this cycle")
                return _cached_positions()
            if res.status_code == 429:
                logger_pos.warning("⚠ /positions rate limited (429) → skip this cycle")
                time.sleep(0.5)
                return _cached_positions()
            res.raise_for_status()
            positions = res.json()
            if not isinstance(positions, list):
                logger_pos.error("❌ /positions 不正レスポンス: %s", positions)
                return _cached_positions()
            pos._POS_CACHE = positions
            pos._POS_CACHE_TIME = now
            return positions
        except requests.exceptions.ReadTimeout:
            logger_pos.warning("⚠ get_positions timeout retry=%s", i + 1)
            time.sleep(0.5)
        except requests.exceptions.ConnectionError:
            logger_pos.warning("⚠ get_positions connection error retry=%s", i + 1)
            time.sleep(0.5)
        except requests.exceptions.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 401:
                logger_pos.warning("⚠ get_positions HTTP 401 after retry path -> use cache this cycle")
                return _cached_positions()
            logger_pos.warning("⚠ get_positions HTTP error status=%s -> use cache", status)
            return _cached_positions()
        except Exception as e:
            logger_pos.warning("⚠ get_positions transient error -> use cache err=%s", e)
            return _cached_positions()
    logger_pos.warning("⚠ get_positions failed after retries → use cache")
    return _cached_positions()


def sync_positions_with_token(orig, *args: Any, **kwargs: Any):
    """旧 sync_positions_from_kabus_patched。token が無ければ素の実装を呼ばない。"""
    logger_pos = _pos_logger()
    token = _resolve_token()
    if not token:
        token = _refresh_token_safe(logger_pos)
    if not token:
        logger_pos.warning("⚠ sync_positions: token 不在 → skip")
        return None
    if callable(orig):
        return orig(*args, **kwargs)
    return None



_ENABLED = False


def token_runtime_enabled() -> bool:
    """旧 install() が走ったか。positions / force_cancel_loop が見る。"""
    return bool(_ENABLED)


def enable_token_runtime() -> bool:
    """旧 install()。関数差し替えは本文化済みなのでフラグを立てるだけ。"""
    global _INSTALLED, _ENABLED
    if _INSTALLED:
        return True
    _sync_global_token()
    _ENABLED = True
    _INSTALLED = True
    logger.warning("[KABU API TOKEN PATCH] enabled v5 (positions / force_cancel_loop headers are native)")
    return True


__all__ = [
    "enable_token_runtime",
    "token_runtime_enabled",
    "safe_get_headers",
    "get_positions_with_token",
    "sync_positions_with_token",
]
