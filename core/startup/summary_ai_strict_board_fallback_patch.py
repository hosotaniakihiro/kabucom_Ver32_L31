# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_strict_board_fallback_patch.py
# Version: V1-SUMMARY-AI-STRICT-BOARD-LIMIT-FALLBACK
# ------------------------------------------------------------
# SUMMARY_AI が AI_OK → range/MTF/5s/liquidity を通過した後、
# 一時的に板が取れず STRICT_BOARD_MISSING だけで発注が止まる症状を救済する。
#
# 方針:
#   - 成行にはしない。
#   - ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY を恒久的に緩めない。
#   - STRICT_BOARD_MISSING の時だけ、同じ build_entry_order を一度だけ
#     close/current_price/vwap ベースの LIMIT fallback で再実行する。
#   - LOW_MOVE / ATR / HIGH_LOW / MTF / 5秒足 / 流動性 / qty は既存ロジックを維持。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-STRICT-BOARD-LIMIT-FALLBACK"
_INSTALLED = False
_WATCHER_STARTED = False
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _row_price(row: Any) -> float:
    try:
        if not isinstance(row, dict):
            return 0.0
        for k in ("close_price", "current_price", "price", "close", "vwap"):
            x = _safe_float(row.get(k), 0.0)
            if x > 0:
                return x
    except Exception:
        pass
    return 0.0


def _is_strict_board_missing(result: Any) -> bool:
    try:
        return isinstance(result, dict) and result.get("ok") is False and str(result.get("reason") or "").upper() == "STRICT_BOARD_MISSING"
    except Exception:
        return False


def _patch_once(reason: str) -> bool:
    try:
        from trading.handlers import entry_order_builder as eob
        cur = getattr(eob, "build_entry_order", None)
        if not callable(cur):
            return False
        if getattr(cur, "_summary_ai_strict_board_fallback_v1", False):
            return True

        orig = cur

        @wraps(orig)
        def wrapped_build_entry_order(*args: Any, **kwargs: Any):
            result = orig(*args, **kwargs)
            try:
                source = str(kwargs.get("source") or "").upper()
                symbol = str(kwargs.get("symbol") or "")
                side = str(kwargs.get("side") or "").upper()
                row = kwargs.get("entry_row")
                if source != "SUMMARY_AI" or not _is_strict_board_missing(result):
                    return result
                if not _env_bool("SUMMARY_AI_STRICT_BOARD_LIMIT_FALLBACK_ENABLED", True):
                    return result
                base_price = _row_price(row)
                if base_price <= 0:
                    logger.warning(
                        "[SUMMARY AI BOARD FALLBACK] skip no price symbol=%s side=%s result=%s version=%s",
                        symbol, side, result, VERSION,
                    )
                    return result

                old_env = os.environ.get("ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY")
                old_attr = getattr(eob, "ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", None)
                try:
                    os.environ["ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY"] = "0"
                    try:
                        setattr(eob, "ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", False)
                    except Exception:
                        pass
                    retry_result = orig(*args, **kwargs)
                finally:
                    if old_env is None:
                        try:
                            os.environ.pop("ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", None)
                        except Exception:
                            pass
                    else:
                        os.environ["ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY"] = str(old_env)
                    try:
                        setattr(eob, "ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", old_attr)
                    except Exception:
                        pass

                if isinstance(retry_result, dict) and retry_result.get("ok"):
                    try:
                        detail = retry_result.setdefault("detail", {})
                        if isinstance(detail, dict):
                            detail["board_fallback_after_strict_missing"] = True
                            detail["board_fallback_base_price"] = base_price
                            detail["board_fallback_version"] = VERSION
                    except Exception:
                        pass
                    logger.warning(
                        "[SUMMARY AI BOARD FALLBACK] recovered symbol=%s side=%s price=%s reason=%s version=%s",
                        symbol,
                        side,
                        retry_result.get("detail", {}).get("price") if isinstance(retry_result.get("detail"), dict) else None,
                        retry_result.get("reason"),
                        VERSION,
                    )
                    return retry_result

                logger.warning(
                    "[SUMMARY AI BOARD FALLBACK] retry still NG symbol=%s side=%s first=%s retry=%s version=%s",
                    symbol, side, result, retry_result, VERSION,
                )
                return result
            except Exception:
                logger.exception("[SUMMARY AI BOARD FALLBACK] wrapper failed; return original result")
                return result

        wrapped_build_entry_order._summary_ai_strict_board_fallback_v1 = True  # type: ignore[attr-defined]
        wrapped_build_entry_order._original = orig  # type: ignore[attr-defined]
        eob.build_entry_order = wrapped_build_entry_order
        try:
            import trading.handlers.entry_controller as ec
            ec.build_entry_order = wrapped_build_entry_order
        except Exception:
            pass
        logger.warning("[SUMMARY AI BOARD FALLBACK] patched reason=%s version=%s", reason, VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI BOARD FALLBACK] patch failed reason=%s", reason)
        return False


def _watcher() -> None:
    loops = int(max(1.0, _safe_float(os.getenv("SUMMARY_AI_BOARD_FALLBACK_WATCH_LOOPS"), 180.0)))
    sleep_sec = max(0.5, _safe_float(os.getenv("SUMMARY_AI_BOARD_FALLBACK_WATCH_SEC"), 1.0))
    for i in range(loops):
        try:
            _patch_once(f"watcher:{i}")
        except Exception:
            pass
        time.sleep(sleep_sec)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    if not _env_bool("SUMMARY_AI_STRICT_BOARD_LIMIT_FALLBACK_ENABLED", True):
        logger.warning("[SUMMARY AI BOARD FALLBACK] disabled by env")
        return False
    ok = _patch_once("install")
    _INSTALLED = bool(ok)
    if not _WATCHER_STARTED and _env_bool("SUMMARY_AI_BOARD_FALLBACK_WATCHER", True):
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="summary-ai-board-fallback-watch", daemon=True).start()
    logger.warning("[SUMMARY AI BOARD FALLBACK] installed ok=%s version=%s", ok, VERSION)
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI BOARD FALLBACK] auto install failed")


__all__ = ["install", "VERSION"]
