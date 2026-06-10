# ============================================================
# File   : core/startup/final_entry_board_guard_signature_last_patch.py
# Version: V3-ADVISORY-AWARE-QUIET
# ------------------------------------------------------------
# 目的:
#   final_entry_safety_guard._board_guard が後段patchで3引数関数に戻され、
#   呼び出し側の _board_guard(row, item, symbol, side) で
#   TypeError: takes 3 positional arguments but 4 were given
#   が再発する問題を最後に吸収する。
#
# V3:
#   - entry_board_imbalance_advisory_patch の _advisory_board_guard は既に4引数互換なので
#     追加wrapしない。
#   - watcherを短縮し、install/enforceログを抑制。
#   - 後段patchにより3引数guardへ戻された場合だけadapterを差し込む。
# ============================================================
from __future__ import annotations

import inspect
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_WATCHER = False
_ORIG = None
_LAST_LOG_TS = 0.0


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ""


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"1", "BUY", "B", "LONG"}:
        return "BUY"
    if s in {"2", "SELL", "S", "SHORT"}:
        return "SELL"
    return s


def _first(row: Any, keys: tuple[str, ...], default: Any = None) -> Any:
    try:
        if isinstance(row, dict):
            for k in keys:
                v = row.get(k)
                if v not in (None, ""):
                    return v
    except Exception:
        pass
    return default


def _supports_four_args(func: Any) -> bool:
    try:
        sig = inspect.signature(func)
        positional = 0
        has_varargs = False
        for p in sig.parameters.values():
            if p.kind == p.VAR_POSITIONAL:
                has_varargs = True
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
                positional += 1
        if has_varargs:
            return True
        return positional >= 4
    except Exception:
        return True


def _is_advisory_guard(func: Any) -> bool:
    return bool(
        callable(func)
        and (
            getattr(func, "_board_imbalance_advisory_v3", False)
            or getattr(func, "_board_imbalance_advisory_v2", False)
        )
    )


def _is_ours(func: Any) -> bool:
    return bool(callable(func) and getattr(func, "_final_board_guard_signature_last_v3", False))


def _rate_log(msg: str, *args: Any) -> None:
    global _LAST_LOG_TS
    now = time.time()
    interval = max(5.0, _env_float("FINAL_BOARD_SIGNATURE_LOG_INTERVAL_SEC", 60.0))
    if now - _LAST_LOG_TS >= interval:
        _LAST_LOG_TS = now
        logger.warning(msg, *args)


def _make_adapter(orig):
    supports4 = _supports_four_args(orig)

    def _adapted_board_guard(row: Any, item: Any = None, symbol: Any = None, side: Any = None, *args: Any, **kwargs: Any) -> bool:
        row_obj = row if isinstance(row, dict) else {}
        sym = symbol
        sd = side
        if sd is None and symbol is not None:
            # 3引数形式で来た場合: row, symbol, side
            sd = symbol
            sym = item
        sym = _norm_symbol(sym or _first(row_obj, ("symbol", "Symbol", "code", "銘柄コード"), ""))
        sd = _norm_side(sd or _first(row_obj, ("side", "entry_decision", "ai_side"), ""))
        try:
            if supports4:
                return bool(orig(row, item, symbol, side, *args, **kwargs))
            return bool(orig(row, sym, sd))
        except TypeError as e4:
            try:
                _rate_log("[FINAL BOARD GUARD SIGNATURE LAST] signature fallback retry 3args symbol=%s side=%s err=%s", sym, sd, e4)
                return bool(orig(row, sym, sd))
            except TypeError as e3:
                try:
                    _rate_log("[FINAL BOARD GUARD SIGNATURE LAST] 3args failed -> retry row-only symbol=%s side=%s err=%s", sym, sd, e3)
                    return bool(orig(row))
                except Exception:
                    logger.exception("[FINAL BOARD GUARD SIGNATURE LAST] board guard fallback failed symbol=%s side=%s", sym, sd)
                    return True
            except Exception:
                logger.exception("[FINAL BOARD GUARD SIGNATURE LAST] original 3args failed symbol=%s side=%s", sym, sd)
                return True
        except Exception:
            logger.exception("[FINAL BOARD GUARD SIGNATURE LAST] original failed symbol=%s side=%s", sym, sd)
            return True

    _adapted_board_guard._final_board_guard_signature_last_v3 = True  # type: ignore[attr-defined]
    _adapted_board_guard._final_board_guard_signature_last_v2 = True  # type: ignore[attr-defined]
    _adapted_board_guard._final_board_guard_signature_last_v1 = True  # type: ignore[attr-defined]
    _adapted_board_guard._original = orig  # type: ignore[attr-defined]
    _adapted_board_guard._supports_four_args = supports4  # type: ignore[attr-defined]
    return _adapted_board_guard


def _patch_once() -> bool:
    global _INSTALLED, _ORIG
    try:
        import core.startup.final_entry_safety_guard_patch as fsg

        cur = getattr(fsg, "_board_guard", None) or getattr(fsg, "_patched_board_guard", None)
        if not callable(cur):
            return False

        # advisory guard は既に4引数互換。ここでwrapすると board_advisory と取り合いになるため触らない。
        if _is_advisory_guard(cur):
            _INSTALLED = True
            return True

        if _is_ours(cur):
            _INSTALLED = True
            return True

        if _supports_four_args(cur):
            # 既に4引数対応なら差し替え不要。fsg._patched_board_guard だけ同期する。
            if getattr(fsg, "_patched_board_guard", None) is not cur:
                try:
                    fsg._patched_board_guard = cur
                except Exception:
                    pass
            _INSTALLED = True
            return True

        _ORIG = cur
        adapted = _make_adapter(cur)
        fsg._board_guard = adapted
        fsg._patched_board_guard = adapted
        _INSTALLED = True
        _rate_log(
            "[FINAL BOARD GUARD SIGNATURE LAST] patched 3arg guard=%s supports4=%s",
            getattr(cur, "__name__", type(cur).__name__),
            getattr(adapted, "_supports_four_args", None),
        )
        return True
    except Exception:
        logger.exception("[FINAL BOARD GUARD SIGNATURE LAST] patch failed")
        return False


def _watch() -> None:
    loops = max(1, min(_env_int("FINAL_BOARD_SIGNATURE_ENFORCE_LOOPS", 30), 120))
    sleep_sec = max(1.0, min(_env_float("FINAL_BOARD_SIGNATURE_ENFORCE_SLEEP_SEC", 2.0), 10.0))
    for i in range(loops):
        ok = _patch_once()
        if i in (0, loops - 1):
            logger.warning("[FINAL BOARD GUARD SIGNATURE LAST] enforce v3 i=%s ok=%s", i, ok)
        time.sleep(sleep_sec)


def install() -> bool:
    global _WATCHER
    ok = _patch_once()
    if not _WATCHER:
        _WATCHER = True
        threading.Thread(target=_watch, name="final-board-guard-signature-last", daemon=True).start()
        logger.warning("[FINAL BOARD GUARD SIGNATURE LAST] installed v3 ok=%s watcher=True quiet=True", ok)
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[FINAL BOARD GUARD SIGNATURE LAST] auto install failed")

__all__ = ["install"]
