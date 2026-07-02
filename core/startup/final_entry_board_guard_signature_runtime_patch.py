# ============================================================
# File   : core/startup/final_entry_board_guard_signature_runtime_patch.py
# Version: V3-SUMMARY-AI-BOARD-DELEGATE-STABLE-WATCHER
# ------------------------------------------------------------
# 目的:
#   final_entry_safety_guard_patch._board_guard / _call_board_guard が
#   他runtime patchにより差し替わっても、4引数呼び出しで TypeError にしない。
#
# V3:
#   - SUMMARY_AI候補は板未取得で即 no_order にせず order_builder 側へ委譲。
#   - 同じ関数を1秒ごとに何度もwrapし直さない。
#   - watcherは差し替え検知時だけ再wrapし、安定後は早期終了。
#   - side/symbolの抽出を強化。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

VERSION = "V3-SUMMARY-AI-BOARD-DELEGATE-STABLE-WATCHER"
_INSTALLED = False
_WATCHER_STARTED = False
_LAST_BOARD_GUARD_ID: int | None = None
_LAST_CALL_GUARD_ID: int | None = None
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


def _norm_side(v: Any) -> str:
    s = _norm(v)
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _extract_item_dict(item: Any) -> dict:
    return item if isinstance(item, dict) else {}


def _extract_side(row: dict, item: dict, side: Any = None) -> str:
    entry = item.get("entry") if isinstance(item.get("entry"), dict) else {}
    ai = item.get("ai") if isinstance(item.get("ai"), dict) else {}
    for v in (
        side,
        item.get("side"),
        row.get("side"),
        row.get("entry_decision"),
        row.get("ai_side"),
        entry.get("side"),
        entry.get("entry_decision"),
        ai.get("side"),
        ai.get("entry_decision"),
    ):
        s = _norm_side(v)
        if s in {"BUY", "SELL"}:
            return s
    return _norm_side(side)


def _extract_symbol(row: dict, item: dict, symbol: Any = None) -> str:
    entry = item.get("entry") if isinstance(item.get("entry"), dict) else {}
    ai = item.get("ai") if isinstance(item.get("ai"), dict) else {}
    for v in (symbol, item.get("symbol"), row.get("symbol"), row.get("Symbol"), row.get("code"), entry.get("symbol"), ai.get("symbol")):
        s = str(v or "").strip()
        if s:
            if s.endswith(".0") and s[:-2].isdigit():
                s = s[:-2]
            return s
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
    try:
        return bool(fn(row, item, symbol, side))
    except TypeError as e4:
        try:
            return bool(fn(row, symbol, side))
        except TypeError:
            try:
                return bool(fn(row=row, item=item, symbol=symbol, side=side))
            except Exception:
                raise e4


def _wrap_board_guard(target: Any) -> bool:
    global _LAST_BOARD_GUARD_ID
    cur = getattr(target, "_board_guard", None)
    if not callable(cur):
        return False
    if getattr(cur, "_final_board_guard_signature_compat_v3", False):
        _LAST_BOARD_GUARD_ID = id(cur)
        return True
    if _LAST_BOARD_GUARD_ID == id(cur):
        return True

    original = cur

    def _compat_board_guard(row: dict, item: dict | None = None, symbol: str | None = None, side: str | None = None, *args, **kwargs) -> bool:
        row_d = _row_to_dict(row)
        item_d = _extract_item_dict(item)
        sym = _extract_symbol(row_d, item_d, symbol)
        sd = _extract_side(row_d, item_d, side)
        try:
            ok = _call_flexible(original, row_d, item_d, sym, sd)
            if ok:
                return True
            if _summary_ai_delegate_ok(row_d, item_d, sym, sd, "board_guard_false"):
                return True
            return False
        except Exception as e:
            logger.warning("[FINAL BOARD GUARD SIGNATURE] BOARD_GUARD_ERROR_COMPAT symbol=%s side=%s error=%s version=%s", sym, sd, e, VERSION)
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
    _compat_board_guard._final_board_guard_signature_compat_v3 = True  # type: ignore[attr-defined]
    _compat_board_guard._original = original  # type: ignore[attr-defined]
    target._board_guard = _compat_board_guard
    _LAST_BOARD_GUARD_ID = id(_compat_board_guard)
    logger.warning("[FINAL BOARD GUARD SIGNATURE] wrapped _board_guard original=%s version=%s summary_ai_delegate=%s", getattr(original, "__name__", type(original).__name__), VERSION, _env_bool("ENTRY_SUMMARY_AI_DELEGATE_BOARD_TO_ORDER_BUILDER", True))
    return True


def _wrap_call_board_guard(target: Any) -> bool:
    global _LAST_CALL_GUARD_ID
    cur = getattr(target, "_call_board_guard", None)
    if not callable(cur):
        return False
    if getattr(cur, "_final_board_guard_signature_call_v3", False):
        _LAST_CALL_GUARD_ID = id(cur)
        return True
    if _LAST_CALL_GUARD_ID == id(cur):
        return True

    def _compat_call_board_guard(row: dict, item: dict, symbol: str, side: str) -> bool:
        row_d = _row_to_dict(row)
        item_d = _extract_item_dict(item)
        sym = _extract_symbol(row_d, item_d, symbol)
        sd = _extract_side(row_d, item_d, side)
        try:
            bg = getattr(target, "_board_guard", None)
            ok = _call_flexible(bg, row_d, item_d, sym, sd) if callable(bg) else bool(cur(row_d, item_d, sym, sd))
            if ok:
                return True
            if _summary_ai_delegate_ok(row_d, item_d, sym, sd, "call_board_guard_false"):
                return True
            return False
        except Exception as e:
            logger.warning("[FINAL BOARD GUARD SIGNATURE] BOARD_GUARD_ERROR_COMPAT symbol=%s side=%s error=%s version=%s", sym, sd, e, VERSION)
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
    _compat_call_board_guard._final_board_guard_signature_call_v3 = True  # type: ignore[attr-defined]
    _compat_call_board_guard._original = cur  # type: ignore[attr-defined]
    target._call_board_guard = _compat_call_board_guard
    _LAST_CALL_GUARD_ID = id(_compat_call_board_guard)
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
    stable = 0
    last_pair: tuple[int | None, int | None] | None = None
    for i in range(20):
        ok = _patch_once()
        pair = (_LAST_BOARD_GUARD_ID, _LAST_CALL_GUARD_ID)
        stable = stable + 1 if ok and pair == last_pair else 0
        last_pair = pair
        if i in (0, 5, 10, 19):
            logger.warning("[FINAL BOARD GUARD SIGNATURE] enforce i=%s/20 ok=%s stable=%s version=%s", i, ok, stable, VERSION)
        if stable >= 3:
            logger.warning("[FINAL BOARD GUARD SIGNATURE] watcher stable exit i=%s version=%s", i, VERSION)
            return
        time.sleep(1.0)
    logger.warning("[FINAL BOARD GUARD SIGNATURE] watcher done version=%s", VERSION)


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
