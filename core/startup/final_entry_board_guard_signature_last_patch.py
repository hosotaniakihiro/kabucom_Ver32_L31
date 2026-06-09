# ============================================================
# File   : core/startup/final_entry_board_guard_signature_last_patch.py
# Version: V2-SIGNATURE-PREFLIGHT-COMPAT
# ------------------------------------------------------------
# 目的:
#   final_entry_safety_guard._board_guard が後段patchで3引数関数に戻され、
#   呼び出し側の _board_guard(row, item, symbol, side) で
#   TypeError: takes 3 positional arguments but 4 were given
#   が再発する問題を最後に吸収する。
#
# V2:
#   - 呼んでからTypeErrorで3引数再試行するのではなく、inspect.signatureで
#     4引数対応可否を先に判定する。
#   - これにより BOARD_GUARD_RETRY_3ARGS / takes 3 positional... の警告を抑制。
# ============================================================
from __future__ import annotations

import inspect
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_WATCHER = False
_ORIG = None


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or '').strip().upper()
        if s.endswith('.0') and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ''


def _norm_side(v: Any) -> str:
    s = str(v or '').strip().upper()
    if s in {'1', 'BUY', 'B', 'LONG'}:
        return 'BUY'
    if s in {'2', 'SELL', 'S', 'SHORT'}:
        return 'SELL'
    return s


def _first(row: Any, keys: tuple[str, ...], default: Any = None) -> Any:
    try:
        if isinstance(row, dict):
            for k in keys:
                v = row.get(k)
                if v not in (None, ''):
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
        # introspectionできないcallableは従来通り4引数を試す
        return True


def _make_adapter(orig):
    supports4 = _supports_four_args(orig)

    def _adapted_board_guard(row: Any, item: Any = None, symbol: Any = None, side: Any = None, *args: Any, **kwargs: Any) -> bool:
        # 互換呼び出し:
        #   _board_guard(row, symbol, side)
        #   _board_guard(row, item, symbol, side)
        row_obj = row if isinstance(row, dict) else {}
        sym = symbol
        sd = side
        if sd is None and symbol is not None:
            # 3引数形式で来た場合: row, symbol, side
            sd = symbol
            sym = item
        sym = _norm_symbol(sym or _first(row_obj, ('symbol', 'Symbol', 'code', '銘柄コード'), ''))
        sd = _norm_side(sd or _first(row_obj, ('side', 'entry_decision', 'ai_side'), ''))
        try:
            if supports4:
                return bool(orig(row, item, symbol, side, *args, **kwargs))
            return bool(orig(row, sym, sd))
        except TypeError as e4:
            # introspection不能・後段差し替え直後などの保険
            try:
                logger.warning('[FINAL BOARD GUARD SIGNATURE LAST] signature fallback retry 3args symbol=%s side=%s err=%s', sym, sd, e4)
                return bool(orig(row, sym, sd))
            except TypeError as e3:
                try:
                    logger.warning('[FINAL BOARD GUARD SIGNATURE LAST] 3args failed -> retry row-only symbol=%s side=%s err=%s', sym, sd, e3)
                    return bool(orig(row))
                except Exception:
                    logger.exception('[FINAL BOARD GUARD SIGNATURE LAST] board guard fallback failed symbol=%s side=%s', sym, sd)
                    return True
            except Exception:
                logger.exception('[FINAL BOARD GUARD SIGNATURE LAST] original 3args failed symbol=%s side=%s', sym, sd)
                return True
        except Exception:
            logger.exception('[FINAL BOARD GUARD SIGNATURE LAST] original failed symbol=%s side=%s', sym, sd)
            return True

    _adapted_board_guard._final_board_guard_signature_last_v2 = True  # type: ignore[attr-defined]
    _adapted_board_guard._final_board_guard_signature_last_v1 = True  # type: ignore[attr-defined]
    _adapted_board_guard._original = orig  # type: ignore[attr-defined]
    _adapted_board_guard._supports_four_args = supports4  # type: ignore[attr-defined]
    return _adapted_board_guard


def _patch_once() -> bool:
    global _INSTALLED, _ORIG
    try:
        import core.startup.final_entry_safety_guard_patch as fsg
        cur = getattr(fsg, '_board_guard', None) or getattr(fsg, '_patched_board_guard', None)
        if not callable(cur):
            return False
        if getattr(cur, '_final_board_guard_signature_last_v2', False):
            _INSTALLED = True
            return True
        _ORIG = cur
        adapted = _make_adapter(cur)
        fsg._board_guard = adapted
        fsg._patched_board_guard = adapted
        _INSTALLED = True
        logger.warning(
            '[FINAL BOARD GUARD SIGNATURE LAST] patched guard=%s supports4=%s',
            getattr(cur, '__name__', type(cur).__name__),
            getattr(adapted, '_supports_four_args', None),
        )
        return True
    except Exception:
        logger.exception('[FINAL BOARD GUARD SIGNATURE LAST] patch failed')
        return False


def _watch() -> None:
    for i in range(240):
        ok = _patch_once()
        if i in (0, 1, 5, 15, 30, 60, 120, 239):
            logger.warning('[FINAL BOARD GUARD SIGNATURE LAST] enforce i=%s ok=%s', i, ok)
        time.sleep(0.5)


def install() -> bool:
    global _WATCHER
    ok = _patch_once()
    if not _WATCHER:
        _WATCHER = True
        threading.Thread(target=_watch, name='final-board-guard-signature-last', daemon=True).start()
    logger.warning('[FINAL BOARD GUARD SIGNATURE LAST] installed v2 ok=%s watcher=%s', ok, _WATCHER)
    return bool(ok)


try:
    install()
except Exception:
    logger.exception('[FINAL BOARD GUARD SIGNATURE LAST] auto install failed')

__all__ = ['install']
