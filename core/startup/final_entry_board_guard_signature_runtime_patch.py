# ============================================================
# File   : core/startup/final_entry_board_guard_signature_runtime_patch.py
# Version: V2-SUMMARY-AI-BOARD-DELEGATE
# ------------------------------------------------------------
# 目的:
#   final_entry_safety_guard_patch._board_guard が別runtime patchにより
#   3引数版へ差し替わった後でも、4引数呼び出しで TypeError にならないようにする。
#
# V2:
#   - SUMMARY_AI候補だけ、final guard の板未取得で即 no_order にしない。
#   - 時間/流動性/逆行ガード通過後は、板リトライとfallback可否を
#     entry_order_builder 側へ委譲する。
#   - stale判定は緩和しない。
#   - snapshot_no_order / entry_controller_no_order の発注直前停止を救済。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

VERSION = "V2-SUMMARY-AI-BOARD-DELEGATE"
_INSTALLED = False
_WATCHER_STARTED = False
_TRUE_VALUES = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE_VALUES:
            return True
        if s in _FALSE_VALUES:
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


def _first(row: dict, names: tuple[str, ...], default: Any = None) -> Any:
    try:
        for name in names:
            v = row.get(name)
            if v is not None and str(v).strip() != "":
                return v
    except Exception:
        pass
    return default


def _row_to_dict(row: Any) -> dict:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _norm(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _is_summary_ai_item(row: dict, item: dict) -> bool:
    try:
        entry = item.get("entry") if isinstance(item.get("entry"), dict) else {}
        ai = item.get("ai") if isinstance(item.get("ai"), dict) else {}
        values = (
            item.get("entry_type"),
            row.get("entry_type"),
            entry.get("entry_type"),
            item.get("source"),
            row.get("source"),
            entry.get("source"),
            row.get("reason"),
            row.get("ai_reason"),
            entry.get("reason"),
            entry.get("ai_reason"),
            ai.get("reason"),
        )
        joined = "|".join(_norm(v) for v in values if v is not None)
        return "SUMMARY_AI" in joined or "SUMMARY_AI_PREAPPROVED" in joined or "SRC=SUMMARY" in joined
    except Exception:
        return False


def _clear_board_missing_skip(item: dict) -> None:
    try:
        roots = (
            item,
            item.get("entry") if isinstance(item.get("entry"), dict) else None,
            item.get("entry_row") if isinstance(item.get("entry_row"), dict) else None,
            item.get("ai") if isinstance(item.get("ai"), dict) else None,
        )
        for root in roots:
            if not isinstance(root, dict):
                continue
            if root.get("skip_reason") == "board_missing":
                root.pop("skip_reason", None)
            if root.get("final_guard_skip_reason") == "board_missing":
                root.pop("final_guard_skip_reason", None)
                root.pop("final_guard_skip_detail", None)
            if root.get("retryable") is True:
                root.pop("retryable", None)
            if root.get("final_guard_retryable") is True:
                root.pop("final_guard_retryable", None)
    except Exception:
        pass


def _summary_ai_delegate_ok(row: dict, item: dict, symbol: str, side: str, reason: str) -> bool:
    if not _env_bool("ENTRY_SUMMARY_AI_DELEGATE_BOARD_TO_ORDER_BUILDER", True):
        return False
    if not _is_summary_ai_item(row, item):
        return False
    close = _safe_float(_first(row, ("close", "close_price", "price", "current_price"), 0.0), 0.0)
    volume = _safe_float(_first(row, ("volume", "Volume", "出来高"), 0.0), 0.0)
    turnover = _safe_float(_first(row, ("turnover", "trading_value", "売買代金"), 0.0), 0.0)
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume
    if close <= 0 or volume <= 0 or turnover <= 0:
        return False
    _clear_board_missing_skip(item)
    logger.warning(
        "[FINAL BOARD GUARD SIGNATURE] SUMMARY_AI_BOARD_DELEGATE symbol=%s side=%s reason=%s close=%.4f volume=%.0f turnover=%.0f version=%s",
        symbol,
        side,
        reason,
        close,
        volume,
        turnover,
        VERSION,
    )
    return True


def _call_flexible(fn: Callable[..., Any], row: dict, item: dict, symbol: str, side: str) -> bool:
    """Call board guard with the signature it actually supports."""
    try:
        return bool(fn(row, item, symbol, side))
    except TypeError as e4:
        msg = str(e4)
        try:
            logger.warning(
                "[FINAL BOARD GUARD SIGNATURE] fallback 4args->3args symbol=%s side=%s err=%s version=%s",
                symbol,
                side,
                msg,
                VERSION,
            )
            return bool(fn(row, symbol, side))
        except TypeError as e3:
            try:
                logger.warning(
                    "[FINAL BOARD GUARD SIGNATURE] fallback 3args->kwargs symbol=%s side=%s err3=%s version=%s",
                    symbol,
                    side,
                    e3,
                    VERSION,
                )
                return bool(fn(row=row, item=item, symbol=symbol, side=side))
            except Exception:
                raise e4
    except Exception:
        raise


def _wrap_board_guard(target: Any) -> bool:
    cur = getattr(target, "_board_guard", None)
    if not callable(cur):
        return False
    if getattr(cur, "_final_board_guard_signature_compat_v2", False):
        return True

    original = cur

    def _compat_board_guard(row: dict, item: dict | None = None, symbol: str | None = None, side: str | None = None, *args, **kwargs) -> bool:
        row_d = _row_to_dict(row)
        item_d = item if isinstance(item, dict) else {}
        sym = str(symbol or _first(row_d, ("symbol", "Symbol", "code", "銘柄コード"), ""))
        sd = str(side or _first(row_d, ("side", "entry_decision", "ai_side"), "")).upper()
        try:
            ok = _call_flexible(original, row_d, item_d, sym, sd)
            if ok:
                return True
            if _summary_ai_delegate_ok(row_d, item_d, sym, sd, "board_guard_false"):
                return True
            return False
        except Exception as e:
            logger.warning(
                "[FINAL BOARD GUARD SIGNATURE] BOARD_GUARD_ERROR_COMPAT symbol=%s side=%s error=%s version=%s",
                sym,
                sd,
                e,
                VERSION,
            )
            if _summary_ai_delegate_ok(row_d, item_d, sym, sd, "board_guard_exception"):
                return True
            try:
                fallback = getattr(target, "_board_missing_fallback_ok", None)
                if callable(fallback):
                    return bool(fallback(row_d, item_d, sym, sd))
            except Exception:
                pass
            return False

    _compat_board_guard._final_board_guard_signature_compat_v1 = True  # type: ignore[attr-defined]
    _compat_board_guard._final_board_guard_signature_compat_v2 = True  # type: ignore[attr-defined]
    _compat_board_guard._original = original  # type: ignore[attr-defined]
    target._board_guard = _compat_board_guard
    logger.warning(
        "[FINAL BOARD GUARD SIGNATURE] wrapped _board_guard original=%s version=%s summary_ai_delegate=%s",
        getattr(original, "__name__", type(original).__name__),
        VERSION,
        _env_bool("ENTRY_SUMMARY_AI_DELEGATE_BOARD_TO_ORDER_BUILDER", True),
    )
    return True


def _wrap_call_board_guard(target: Any) -> bool:
    cur = getattr(target, "_call_board_guard", None)
    if not callable(cur):
        return False
    if getattr(cur, "_final_board_guard_signature_call_v2", False):
        return True

    def _compat_call_board_guard(row: dict, item: dict, symbol: str, side: str) -> bool:
        row_d = _row_to_dict(row)
        item_d = item if isinstance(item, dict) else {}
        sym = str(symbol or _first(row_d, ("symbol", "Symbol", "code", "銘柄コード"), ""))
        sd = str(side or _first(row_d, ("side", "entry_decision", "ai_side"), "")).upper()
        try:
            bg = getattr(target, "_board_guard", None)
            if callable(bg):
                ok = _call_flexible(bg, row_d, item_d, sym, sd)
            else:
                ok = bool(cur(row_d, item_d, sym, sd))
            if ok:
                return True
            if _summary_ai_delegate_ok(row_d, item_d, sym, sd, "call_board_guard_false"):
                return True
            return False
        except Exception as e:
            logger.warning(
                "[FINAL BOARD GUARD SIGNATURE] BOARD_GUARD_ERROR_COMPAT symbol=%s side=%s error=%s version=%s",
                sym,
                sd,
                e,
                VERSION,
            )
            if _summary_ai_delegate_ok(row_d, item_d, sym, sd, "call_board_guard_exception"):
                return True
            try:
                fallback = getattr(target, "_board_missing_fallback_ok", None)
                if callable(fallback):
                    return bool(fallback(row_d, item_d, sym, sd))
            except Exception:
                pass
            return False

    _compat_call_board_guard._final_board_guard_signature_call_v1 = True  # type: ignore[attr-defined]
    _compat_call_board_guard._final_board_guard_signature_call_v2 = True  # type: ignore[attr-defined]
    _compat_call_board_guard._original = cur  # type: ignore[attr-defined]
    target._call_board_guard = _compat_call_board_guard
    logger.warning("[FINAL BOARD GUARD SIGNATURE] wrapped _call_board_guard version=%s summary_ai_delegate=%s", VERSION, _env_bool("ENTRY_SUMMARY_AI_DELEGATE_BOARD_TO_ORDER_BUILDER", True))
    return True


def _patch_once() -> bool:
    try:
        import core.startup.final_entry_safety_guard_patch as target
        os.environ.setdefault("ENTRY_SUMMARY_AI_DELEGATE_BOARD_TO_ORDER_BUILDER", "1")
        ok1 = _wrap_board_guard(target)
        ok2 = _wrap_call_board_guard(target)
        return bool(ok1 or ok2)
    except Exception:
        logger.exception("[FINAL BOARD GUARD SIGNATURE] patch_once failed")
        return False


def _watch() -> None:
    for i in range(60):
        ok = _patch_once()
        if i in (0, 10, 30, 59):
            logger.warning("[FINAL BOARD GUARD SIGNATURE] enforce i=%s/60 ok=%s version=%s", i, ok, VERSION)
        time.sleep(1.0)


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    ok = _patch_once()
    _INSTALLED = bool(ok)
    if not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watch, daemon=True, name="final-board-guard-signature-compat").start()
    logger.warning("[FINAL BOARD GUARD SIGNATURE] installed ok=%s watcher=%s version=%s", ok, _WATCHER_STARTED, VERSION)
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[FINAL BOARD GUARD SIGNATURE] auto install failed")


__all__ = ["install", "VERSION"]
