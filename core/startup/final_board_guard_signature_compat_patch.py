# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/final_board_guard_signature_compat_patch.py
# Version: V1-FINAL-BOARD-GUARD-SIGNATURE-COMPAT
# ------------------------------------------------------------
# Purpose:
#   - final_entry_safety_guard_patch._call_board_guard(row,item,symbol,side)
#     が4引数で呼ぶ一方、後段patchが3引数版 _patched_board_guard に戻して
#     TypeError: takes 3 positional arguments but 4 were given になる問題を防ぐ。
#   - 後段patchの再ラップにも負けないよう watcher で再適用する。
#   - board_missing 自体は従来通り retryable pending keep。板なし発注は有効化しない。
# ============================================================
from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V1-FINAL-BOARD-GUARD-SIGNATURE-COMPAT"
_WATCHER_STARTED = False
_INSTALLED = False


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _norm_side(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s in {"BUY", "LONG", "2", "買", "買い"}:
            return "BUY"
        if s in {"SELL", "SHORT", "1", "売", "売り"}:
            return "SELL"
        return s
    except Exception:
        return ""


def _row_to_dict(row: Any) -> dict:
    try:
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            return d if isinstance(d, dict) else {}
    except Exception:
        pass
    return {}


def _first(row: dict, keys: tuple[str, ...], default: Any = "") -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _as_4arg_board_guard(fn):
    def _compat(row: Any, item: dict | None = None, symbol: str | None = None, side: str | None = None, *args: Any, **kwargs: Any) -> bool:
        row_d = _row_to_dict(row)
        item_d = item if isinstance(item, dict) else {}
        symbol_s = _norm_symbol(symbol or _first(row_d, ("symbol", "Symbol", "code", "銘柄コード"), ""))
        side_s = _norm_side(side or _first(row_d, ("side", "entry_decision", "ai_side"), ""))
        try:
            return bool(fn(row_d, item_d, symbol_s, side_s, *args, **kwargs))
        except TypeError as e4:
            # 3引数版 board_guard(row, symbol, side) への互換fallback。
            try:
                logger.warning(
                    "[FINAL BOARD GUARD SIG COMPAT] fallback 4args->3args symbol=%s side=%s err=%s version=%s",
                    symbol_s,
                    side_s,
                    e4,
                    VERSION,
                )
                return bool(fn(row_d, symbol_s, side_s))
            except TypeError as e3:
                # 2引数/kwargs型なども最後に試す。
                try:
                    logger.warning(
                        "[FINAL BOARD GUARD SIG COMPAT] fallback 3args->kwargs symbol=%s side=%s err=%s version=%s",
                        symbol_s,
                        side_s,
                        e3,
                        VERSION,
                    )
                    return bool(fn(row_d, item=item_d, symbol=symbol_s, side=side_s))
                except Exception:
                    logger.exception(
                        "[FINAL BOARD GUARD SIG COMPAT] incompatible board_guard signature symbol=%s side=%s version=%s",
                        symbol_s,
                        side_s,
                        VERSION,
                    )
                    return False
        except Exception:
            logger.exception("[FINAL BOARD GUARD SIG COMPAT] board_guard failed symbol=%s side=%s version=%s", symbol_s, side_s, VERSION)
            return False

    _compat._final_board_guard_signature_compat_v1 = True  # type: ignore[attr-defined]
    _compat._original = fn  # type: ignore[attr-defined]
    return _compat


def _apply(reason: str = "install") -> bool:
    try:
        import core.startup.final_entry_safety_guard_patch as fsg
        cur = getattr(fsg, "_board_guard", None)
        if not callable(cur):
            logger.warning("[FINAL BOARD GUARD SIG COMPAT] target _board_guard missing reason=%s", reason)
            return False
        if getattr(cur, "_final_board_guard_signature_compat_v1", False):
            return True
        wrapped = _as_4arg_board_guard(cur)
        fsg._board_guard = wrapped
        fsg._patched_board_guard = wrapped
        logger.warning("[FINAL BOARD GUARD SIG COMPAT] applied reason=%s version=%s", reason, VERSION)
        return True
    except Exception:
        logger.exception("[FINAL BOARD GUARD SIG COMPAT] apply failed reason=%s", reason)
        return False


def _watcher() -> None:
    try:
        for i in range(90):
            time.sleep(1.0)
            _apply(reason=f"watcher:{i + 1}")
        logger.warning("[FINAL BOARD GUARD SIG COMPAT] watcher done version=%s", VERSION)
    except Exception:
        logger.exception("[FINAL BOARD GUARD SIG COMPAT] watcher failed")


def _start_watcher() -> None:
    global _WATCHER_STARTED
    if _WATCHER_STARTED:
        return
    _WATCHER_STARTED = True
    threading.Thread(target=_watcher, name="final-board-guard-signature-compat", daemon=True).start()
    logger.warning("[FINAL BOARD GUARD SIG COMPAT] watcher started version=%s", VERSION)


def install() -> bool:
    global _INSTALLED
    ok = _apply("install")
    _start_watcher()
    _INSTALLED = bool(ok)
    logger.warning("[FINAL BOARD GUARD SIG COMPAT] installed=%s version=%s", _INSTALLED, VERSION)
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[FINAL BOARD GUARD SIG COMPAT] auto install failed")


__all__ = ["VERSION", "install"]
