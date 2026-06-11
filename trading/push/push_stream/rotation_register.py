# ============================================================
# File   : trading/push/push_stream/rotation_register.py
# Version: PRODUCTION-STABLE-REV4-PUSH-ROTATION-REGISTER-DESIGNED-CLEAR
# ------------------------------------------------------------
# PUSH A/Bローテーション用の登録委譲API。
#
# Responsibilities:
#   - 50銘柄登録を subscription_manager refresh_callable へ委譲
#   - WebSocket へ ws.send で直接 register しない
#   - kabu Station 公式ひな形どおり、登録/解除は HTTP PUT API へ分離
#   - ユーザー設計どおり rotation_* は以下で運用する:
#       A登録 -> 4.8秒保持 -> unregister_all -> 0.2秒待機 -> B登録
#       B登録 -> 4.8秒保持 -> unregister_all -> 0.2秒待機 -> A登録
#   - timeout付き登録で rotation worker を止めない
#
# Env override:
#   PUSH_ROTATION_REGISTER_FORCE=0/1
#   PUSH_ROTATION_CLEAR_FIRST=0/1
#   PUSH_ROTATION_UNREGISTER_FIRST=0/1
#   PUSH_ROTATION_WAIT_AFTER_CLEAR_SEC=0.2
# ============================================================

from __future__ import annotations

import concurrent.futures
import logging
import os
from typing import Any, Iterable, Sequence

from . import state
from .rotation_settings import (
    DEFAULT_REGISTER_CHUNK_SIZE,
    REGISTER_TIMEOUT_SEC,
    UNREGISTER_TO_REGISTER_WAIT_SEC,
)
from .rotation_symbols import clean_symbol_list
from .transport import _call_refresh

logger = logging.getLogger(__name__)

VERSION = "PRODUCTION-STABLE-REV4-PUSH-ROTATION-REGISTER-DESIGNED-CLEAR"


def _env_bool(name: str, default: bool) -> bool:
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


def refresh_result_to_ok(result: Any) -> bool:
    if result is None:
        return False

    if isinstance(result, bool):
        return result

    if isinstance(result, (int, float)):
        return result > 0

    if isinstance(result, dict):
        for key in ("ok", "success", "registered_ok"):
            if key in result:
                return bool(result.get(key))
        for key in ("registered", "count", "size", "n"):
            if key in result:
                try:
                    return int(result.get(key) or 0) > 0
                except Exception:
                    pass
        return len(result) > 0

    if isinstance(result, (list, tuple, set)):
        return len(result) > 0

    if isinstance(result, str):
        s = result.strip().lower()
        if not s:
            return False
        if s in {"ok", "true", "success", "done"}:
            return True
        if s in {"false", "ng", "error", "failed", "none"}:
            return False
        return True

    return bool(result)


def register_symbols(symbols: Iterable[str], force: bool = False, **kwargs: Any) -> bool:
    """
    kabu Station への実登録は subscription_manager の refresh_callable に委譲する。

    重要:
      - ここでは WebSocket ws.send による直接 register はしない。
      - 公式ひな形どおり、register/unregister/unregister_all は HTTP PUT API 側で行う。
      - rotation_* ではユーザー指定どおり unregister_all -> 0.2秒 -> register を使う。
      - on_open/reconnect は subscription_manager 側で register-only にできる。
    """

    cleaned, raw_count, filler_count, invalid_count = clean_symbol_list(symbols)
    items = cleaned[:DEFAULT_REGISTER_CHUNK_SIZE]

    if not items:
        logger.warning(
            "[push_stream] register_symbols skipped: empty_after_filter raw=%d filler=%d invalid=%d",
            raw_count,
            filler_count,
            invalid_count,
        )
        return False
    if not callable(state._refresh_callable):
        logger.warning(
            "[push_stream] register_symbols skipped: refresh callable missing size=%d head=%s",
            len(items),
            items[:10],
        )
        return False

    reason = str(kwargs.get("reason") or "rotation")
    label = str(kwargs.get("label") or reason.replace("rotation_", "").upper())

    # force引数より環境変数を優先。実際の rotation_* clear 方針は subscription_manager 側でも強制する。
    effective_force = _env_bool("PUSH_ROTATION_REGISTER_FORCE", bool(force and False))
    clear_first = _env_bool("PUSH_ROTATION_CLEAR_FIRST", False)
    unregister_first = _env_bool("PUSH_ROTATION_UNREGISTER_FIRST", False)
    wait_after_clear = _env_float("PUSH_ROTATION_WAIT_AFTER_CLEAR_SEC", 0.0)
    unregister_wait = _env_float("PUSH_ROTATION_UNREGISTER_WAIT_SEC", wait_after_clear)

    try:
        logger.warning(
            "[push_stream] refresh call reason=%s size=%d head=%s force=%s clear_first=%s unregister_first=%s wait_after_clear=%.3fs",
            reason,
            len(items),
            items[:10],
            effective_force,
            clear_first,
            unregister_first,
            wait_after_clear,
        )

        result = _call_refresh(
            force=effective_force,
            reason=reason,
            clear_first=clear_first,
            unregister_first=unregister_first,
            wait_after_clear_sec=wait_after_clear,
            unregister_wait_sec=unregister_wait,
            symbols=items,
            codes=items,
            items=items,
        )

        ok = refresh_result_to_ok(result)
        logger.info(
            "[push_stream] refresh result reason=%s label=%s ok=%s result_type=%s result=%r size=%d",
            reason,
            label,
            ok,
            type(result).__name__ if result is not None else "NoneType",
            result,
            len(items),
        )
        return ok

    except Exception:
        logger.exception(
            "[push_stream] refresh call failed reason=%s size=%d",
            reason,
            len(items),
        )
        return False


def run_one_batch(*, label: str, symbols: Sequence[str]) -> bool:
    cleaned, raw_count, filler_count, invalid_count = clean_symbol_list(symbols)
    cleaned = cleaned[:DEFAULT_REGISTER_CHUNK_SIZE]

    if not cleaned:
        logger.warning(
            "[push_stream] rotation %s skipped: empty_after_filter raw=%d filler=%d invalid=%d",
            label,
            raw_count,
            filler_count,
            invalid_count,
        )
        return False
    reason = f"rotation_{label}"
    return register_symbols(cleaned, force=False, reason=reason, label=label)


def run_one_batch_with_timeout(
    *,
    label: str,
    symbols: Sequence[str],
    timeout_sec: float = REGISTER_TIMEOUT_SEC,
) -> bool:
    cleaned, raw_count, filler_count, invalid_count = clean_symbol_list(symbols)
    cleaned = cleaned[:DEFAULT_REGISTER_CHUNK_SIZE]

    if not cleaned:
        logger.warning(
            "[push_stream] rotation %s timeout-wrapper skipped empty raw=%d filler=%d invalid=%d",
            label,
            raw_count,
            filler_count,
            invalid_count,
        )
        return False
    logger.info(
        "[push_stream] rotation %s register dispatch timeout=%.3fs size=%d head=%s",
        label,
        timeout_sec,
        len(cleaned),
        cleaned[:10],
    )

    ex = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix=f"push-rotation-register-{label}",
    )
    future = ex.submit(run_one_batch, label=label, symbols=cleaned)

    try:
        ok = bool(future.result(timeout=max(0.1, float(timeout_sec))))
        logger.info(
            "[push_stream] rotation %s register returned ok=%s timeout=%.3fs size=%d",
            label,
            ok,
            timeout_sec,
            len(cleaned),
        )
        return ok

    except concurrent.futures.TimeoutError:
        logger.warning(
            "[push_stream] rotation %s register timeout %.3fs -> force continue size=%d",
            label,
            timeout_sec,
            len(cleaned),
        )
        return False

    except Exception:
        logger.exception(
            "[push_stream] rotation %s register exception -> force continue size=%d",
            label,
            len(cleaned),
        )
        return False
