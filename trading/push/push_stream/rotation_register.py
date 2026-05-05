# ============================================================
# File   : trading/push/push_stream/rotation_register.py
# Version: PRODUCTION-STABLE-REV2-PUSH-ROTATION-REGISTER-INDEPENDENT
# ------------------------------------------------------------
# PUSH A/Bローテーション用の登録委譲API。
#
# Responsibilities:
#   - 50銘柄登録を subscription_manager refresh_callable へ委譲
#   - 登録前に clear_first / unregister_first を指定
#   - 全解除後 0.2秒待機してから登録
#   - timeout付き登録で rotation worker を止めない
#
# Notes:
#   - 旧 rotation.py へ依存しない独立実装。
# ============================================================

from __future__ import annotations

import concurrent.futures
import logging
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

VERSION = "PRODUCTION-STABLE-REV2-PUSH-ROTATION-REGISTER-INDEPENDENT"


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
      - ここでは WebSocket ws.send による register はしない
      - clear_first=True / unregister_first=True で、毎回 全解除 -> 0.2秒待機 -> 登録
    """
    del force

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

    try:
        logger.info(
            "[push_stream] refresh call reason=%s size=%d head=%s clear_first=True wait_after_clear=%.3fs",
            reason,
            len(items),
            items[:10],
            UNREGISTER_TO_REGISTER_WAIT_SEC,
        )

        result = _call_refresh(
            force=True,
            reason=reason,
            clear_first=True,
            unregister_first=True,
            wait_after_clear_sec=UNREGISTER_TO_REGISTER_WAIT_SEC,
            unregister_wait_sec=UNREGISTER_TO_REGISTER_WAIT_SEC,
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
    return register_symbols(cleaned, force=True, reason=reason, label=label)


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

    finally:
        try:
            ex.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            ex.shutdown(wait=False)
        except Exception:
            logger.debug("[push_stream] rotation %s executor shutdown failed", label, exc_info=True)


__all__ = [
    "VERSION",
    "REGISTER_TIMEOUT_SEC",
    "UNREGISTER_TO_REGISTER_WAIT_SEC",
    "refresh_result_to_ok",
    "register_symbols",
    "run_one_batch",
    "run_one_batch_with_timeout",
]
