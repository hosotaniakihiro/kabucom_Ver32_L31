# ============================================================
# File   : core/startup/board_passive_entry_price_patch.py
# Version: Ver01-PASSIVE-BOARD-WALL-ENTRY-PRICE
# ------------------------------------------------------------
# entry_order_builder.build_entry_order の戻り値を包み、
# LIMIT注文の価格だけ「厚い板の1ティック手前」へ補正する。
#
# - BUY : 厚い売り板の1ティック下
# - SELL: 厚い買い板の1ティック上
# - 板が取れない/厚い壁がない場合は従来価格を維持
# - BUYで従来より高くなる、SELLで従来より安くなる補正はしない
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_BUILD_ENTRY_ORDER = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _norm_side(side: Any) -> str:
    s = str(side or "").strip().upper()
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _patched_build_entry_order(*args, **kwargs):
    ret = _ORIG_BUILD_ENTRY_ORDER(*args, **kwargs) if callable(_ORIG_BUILD_ENTRY_ORDER) else None
    if not _env_bool("ENTRY_BOARD_PASSIVE_PRICE_ENABLED", True):
        return ret
    try:
        if not isinstance(ret, dict) or ret.get("ok") is not True:
            return ret
        detail = ret.get("detail")
        if not isinstance(detail, dict):
            return ret
        if str(detail.get("order_type") or "").upper() != "LIMIT":
            return ret

        symbol = kwargs.get("symbol")
        side = _norm_side(kwargs.get("side"))
        if not symbol or side not in {"BUY", "SELL"}:
            return ret
        old_price = float(detail.get("price") or 0.0)
        if old_price <= 0:
            return ret

        from trading.board.board_signal import suggest_passive_entry_price
        passive = suggest_passive_entry_price(
            str(symbol),
            side=side,
            exchange=_env_int("ENTRY_BOARD_EXCHANGE", _env_int("EXIT_BOARD_WALL_EXCHANGE", 1)),
        )
        if not passive:
            return ret
        new_price = float(passive.get("price") or 0.0)
        if new_price <= 0:
            return ret

        # 不利方向の補正は避ける。
        if side == "BUY" and new_price > old_price:
            return ret
        if side == "SELL" and new_price < old_price:
            return ret

        detail["original_price"] = detail.get("price")
        detail["original_base_price"] = detail.get("base_price")
        detail["price"] = new_price
        detail["base_price"] = passive.get("base_price") or detail.get("base_price")
        detail["price_source"] = "board_passive_one_tick_before_wall"
        detail["board_passive_price"] = passive
        logger.warning(
            "[BOARD PASSIVE ENTRY PRICE PATCH] symbol=%s side=%s price=%s -> %s wall_price=%s wall_qty=%s",
            symbol,
            side,
            detail.get("original_price"),
            new_price,
            passive.get("wall_price"),
            passive.get("wall_qty"),
        )
        return ret
    except Exception:
        logger.debug("[BOARD PASSIVE ENTRY PRICE PATCH] failed", exc_info=True)
        return ret


def install() -> bool:
    global _INSTALLED, _ORIG_BUILD_ENTRY_ORDER
    if _INSTALLED:
        return True
    try:
        import trading.handlers.entry_order_builder as eob
        cur = getattr(eob, "build_entry_order", None)
        if not callable(cur):
            logger.warning("[BOARD PASSIVE ENTRY PRICE PATCH] build_entry_order unavailable")
            return False
        if getattr(cur, "_board_passive_entry_price_patch", False):
            _INSTALLED = True
            return True
        _ORIG_BUILD_ENTRY_ORDER = cur
        _patched_build_entry_order._board_passive_entry_price_patch = True  # type: ignore[attr-defined]
        _patched_build_entry_order._original = cur  # type: ignore[attr-defined]
        eob.build_entry_order = _patched_build_entry_order
        _INSTALLED = True
        logger.warning(
            "[BOARD PASSIVE ENTRY PRICE PATCH] installed enabled=%s levels=%s min_wall_qty=%s wall_multiplier=%s",
            _env_bool("ENTRY_BOARD_PASSIVE_PRICE_ENABLED", True),
            os.getenv("ENTRY_BOARD_PASSIVE_WALL_LEVELS", "10"),
            os.getenv("ENTRY_BOARD_PASSIVE_MIN_WALL_QTY", "1500"),
            os.getenv("ENTRY_BOARD_PASSIVE_WALL_MULTIPLIER", "2.5"),
        )
        return True
    except Exception:
        logger.exception("[BOARD PASSIVE ENTRY PRICE PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[BOARD PASSIVE ENTRY PRICE PATCH] auto install failed")


__all__ = ["install"]
