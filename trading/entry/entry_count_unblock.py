# -*- coding: utf-8 -*-
"""
Runtime patch to prevent valid intraday entry candidates from being dropped too aggressively.

Fixes production symptoms seen in 2026-06-16 logs:
- SUMMARY_AI candidates dropped by SHORT_MTF_SLOPE_MISSING when only 1m slope is ready.
- TONOSAMA skipped because push summary is stale while raw PUSH is still receiving ticks.
- Entry pipeline blocked for a long time by board retry around PUSH A/B rotation boundary.
- API key mismatch recovery refreshes a token but downstream modules keep using the stale token.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-ENTRY-COUNT-UNBLOCK"
_INSTALLED = False
_RETRYING = False


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


def _publish_token(token: str) -> None:
    token = str(token or "").strip()
    if not token:
        return
    for key in ("KABU_API_KEY", "KABUSAPI_API_KEY", "KABUSAPI_TOKEN", "KABU_TOKEN", "X_API_KEY"):
        os.environ[key] = token
    for module_name, attr_name in (("global_state", "global_data"), ("core.global_context.context", "global_data")):
        try:
            mod = __import__(module_name, fromlist=[attr_name])
            gd = getattr(mod, attr_name, None)
            if gd is None:
                continue
            for name in ("kabu_api_key", "kabusapi_api_key", "api_key", "token", "kabu_token", "kabusapi_token"):
                try:
                    setattr(gd, name, token)
                except Exception:
                    pass
            headers = getattr(gd, "headers", None)
            if isinstance(headers, dict):
                headers["X-API-KEY"] = token
        except Exception:
            continue


def refresh_token_publish(ret):
    """旧 refresh_token_patched (token_manager.refresh_token 差し替え)。

    内側の層が返したトークンを各所へ配る。戻り値はそのまま返す。
    """
    if ret:
        _publish_token(str(ret))
    return ret


def get_valid_token_env_first(orig, *args, **kwargs):
    """旧 get_valid_token_patched (token_manager.get_valid_token 差し替え)。

    2026-09-01 修正:
      settings.ini が正 (canonical) で、env はその写し。
      旧実装は env を無条件に優先し、さらに token_manager.API_TOKEN を
      env の値で上書きしていた。/token を取り直して settings.ini を
      更新しても、env に古い token が残っているプロセスでは
        - get_valid_token() が古い token を返す
        - token_manager.API_TOKEN まで古い値に巻き戻される
        - requests フックが X-API-KEY に古い token を載せる
      となり、「発行したばかりの token なのに 4001009 APIキー不一致」に見えた。
      env は settings.ini に token が無いときのフォールバックに戻す。
    """
    settings_token = ""
    try:
        import token_manager

        settings_token = str(token_manager.read_settings_token() or "").strip()
    except Exception:
        settings_token = ""

    if len(settings_token) >= 16:
        try:
            import token_manager

            token_manager.API_TOKEN = settings_token
        except Exception:
            pass
        return settings_token

    token = None
    for env_name in ("KABUSAPI_TOKEN", "KABU_TOKEN", "KABU_API_KEY", "KABUSAPI_API_KEY", "X_API_KEY"):
        v = os.getenv(env_name)
        if v and len(str(v).strip()) >= 16:
            token = str(v).strip()
            break
    if token:
        try:
            import token_manager

            token_manager.API_TOKEN = token
        except Exception:
            pass
        return token
    return orig(*args, **kwargs)


def _apply_entry_defaults() -> None:
    if not _env_bool("ENTRY_COUNT_UNBLOCK_RELAX_SHORT_MTF", True):
        return
    # Logs showed SUMMARY_AI candidates were rejected because 3m/5m slopes were missing.
    # Use 1 available/aligned short slope as the default, while still blocking opposite slopes when present.
    os.environ["ENTRY_SHORT_MTF_REQUIRED"] = "1"
    os.environ["ENTRY_SHORT_MTF_MIN_AVAILABLE"] = os.getenv("ENTRY_SHORT_MTF_MIN_AVAILABLE_FORCE", "1")
    os.environ["ENTRY_SHORT_MTF_MIN_ALIGNED"] = os.getenv("ENTRY_SHORT_MTF_MIN_ALIGNED_FORCE", "1")
    os.environ.setdefault("ENTRY_SHORT_MTF_ZERO_NEUTRAL", "1")
    os.environ.setdefault("ENTRY_SHORT_MTF_DB_BACKFILL", "1")
    os.environ.setdefault("ENTRY_DAILY_MTF_OPTIONAL", "1")
    os.environ["ENTRY_ORDER_REQUIRE_MTF_DATA"] = "0"
    os.environ["ENTRY_ORDER_REQUIRE_5S_DATA"] = "0"

    # A/B PUSH rotation boundary can make board retry wait 5.6s per candidate. Keep a short retry only.
    os.environ["ENTRY_ORDER_BOARD_RETRY_SEC"] = os.getenv("ENTRY_ORDER_BOARD_RETRY_SEC_FORCE", "1.2")
    os.environ["ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC"] = os.getenv("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC_FORCE", "0.20")
    os.environ.setdefault("ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", "0")


def wait_fresh_push_summary_stale_failopen(orig, latest_age) -> bool:
    """旧 _patched_wait_fresh_push_summary_before_tonosama。

    push summary が古くても、行数が足りていれば fail-open で TONOSAMA を通す。
    """
    try:
        age, latest, rows = latest_age()
    except Exception:
        age, latest, rows = None, None, 0
    max_age = max(30.0, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_MAX_AGE_SEC", 180.0))
    # P0-4 stale_fail_open 監査: Entry の最終判断へ波及する経路は fail-close。
    # owner は fresh_summary_wait.install() が env へ 0 を明示する 1 か所。
    # ここは install 順に依存しないよう既定も 0 にする。
    stale_fail_open = _env_bool("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE", False)
    stale_max_age = max(max_age, _env_float("TONOSAMA_WAIT_PUSH_SUMMARY_STALE_FAIL_OPEN_MAX_AGE_SEC", 1800.0))
    min_rows = max(1, _env_int("TONOSAMA_WAIT_PUSH_SUMMARY_STALE_FAIL_OPEN_MIN_ROWS", 20))
    if age is not None and age <= max_age:
        return True
    if latest is not None and age is not None and stale_fail_open and age <= stale_max_age and int(rows or 0) >= min_rows:
        logger.warning(
            "[TONOSAMA ENTRY SCHEDULE] stale push summary fail-open latest=%s age=%.1fs rows=%s max_age=%.1fs stale_max_age=%.1fs patched=entry_count_unblock",
            latest,
            float(age),
            rows,
            max_age,
            stale_max_age,
        )
        return True
    return bool(orig())






_ENABLED = False


def entry_count_unblock_enabled() -> bool:
    """旧 install() が走ったか。token_manager / entry_exit.tasks が見る。"""
    return bool(_ENABLED)


def enable_entry_count_unblock() -> bool:
    """旧 install()。関数差し替えは本文化済みなので env 既定値だけ。

    旧構成にあった TONOSAMA wait のリトライスレッドは、対象モジュールが
    まだ import されていない場合に備えたものだったが、本文化で
    trading/entry_exit/tasks.py 自身が呼び出し時に分岐するため不要になった。
    """
    global _INSTALLED, _ENABLED
    if _INSTALLED:
        return True
    if not _env_bool("ENTRY_COUNT_UNBLOCK_PATCH_ENABLED", True):
        logger.warning("[ENTRY COUNT UNBLOCK] disabled by env")
        return False
    _apply_entry_defaults()
    os.environ["TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE"] = "1"
    os.environ.setdefault("TONOSAMA_WAIT_PUSH_SUMMARY_STALE_FAIL_OPEN_MAX_AGE_SEC", "1800")
    _ENABLED = True
    _INSTALLED = True
    logger.warning(
        "[ENTRY COUNT UNBLOCK] installed version=%s token_patch=native tonosama_patch=native short_mtf_min_available=%s short_mtf_min_aligned=%s board_retry_sec=%s stale_fail_open=%s stale_max_age=%s",
        VERSION,
        os.environ.get("ENTRY_SHORT_MTF_MIN_AVAILABLE"),
        os.environ.get("ENTRY_SHORT_MTF_MIN_ALIGNED"),
        os.environ.get("ENTRY_ORDER_BOARD_RETRY_SEC"),
        os.environ.get("TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE"),
        os.environ.get("TONOSAMA_WAIT_PUSH_SUMMARY_STALE_FAIL_OPEN_MAX_AGE_SEC"),
    )
    return True


__all__ = [
    "VERSION",
    "enable_entry_count_unblock",
    "entry_count_unblock_enabled",
    "refresh_token_publish",
    "get_valid_token_env_first",
    "wait_fresh_push_summary_stale_failopen",
]
