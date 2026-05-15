# ============================================================
# File   : core/startup/entry_limit_passive_runtime_patch.py
# Version: V1.1-BOARD-TOUCH-LIMIT
# ------------------------------------------------------------
# SUMMARY_AI エントリー指値を以下へ変更する runtime patch。
#
#   BUY  : ask 指値
#   SELL : bid 指値
#
# 目的:
#   - ask-1tick / bid+1tick で約定率が落ち、弱い銘柄だけ拾う可能性を下げる
#   - 2秒未約定キャンセルと組み合わせ、約定しないものはすぐ次候補へ回す
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)
_INSTALLED = False


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        import trading.handlers.entry_order_builder as eob
    except Exception:
        logger.exception("[ENTRY LIMIT BOARD TOUCH] import failed")
        return False

    try:
        old_build = getattr(eob, "build_entry_order", None)
        round_price = getattr(eob, "_round_price", None)
        if not callable(old_build) or not callable(round_price):
            logger.warning("[ENTRY LIMIT BOARD TOUCH] required functions not callable")
            return False

        # 0 = board base price そのまま。BUYはask、SELLはbid。
        setattr(eob, "SUMMARY_AGGRESSIVE_LIMIT_TICKS", 0)

        def _board_touch_limit_price(base_price: float, side: str, ticks: int = 0) -> float:
            p = _safe_float(base_price, 0.0)
            if p <= 0:
                return p
            side_u = str(side or "").upper()
            return round_price(p, side_u)

        _board_touch_limit_price._entry_board_touch = True  # type: ignore[attr-defined]
        setattr(eob, "_aggressive_limit_price", _board_touch_limit_price)

        if not getattr(old_build, "_entry_board_touch_wrapped", False):
            def build_entry_order_board_touch(*args: Any, **kwargs: Any) -> Dict[str, Any]:
                ret = old_build(*args, **kwargs)
                try:
                    if isinstance(ret, dict) and ret.get("ok"):
                        detail = ret.get("detail")
                        if isinstance(detail, dict):
                            src = str(kwargs.get("source") or "").upper()
                            side = str(kwargs.get("side") or "").upper()
                            if src == "SUMMARY_AI" and detail.get("order_type") == "LIMIT":
                                detail["aggressive_limit_ticks"] = 0
                                detail["entry_limit_mode"] = "BOARD_TOUCH"
                                detail["price_rule"] = "BUY=ask / SELL=bid"
                                if detail.get("board"):
                                    detail["price_source"] = "board_bid_ask_touch"
                                else:
                                    detail["price_source"] = "summary_fallback_touch"
                                logger.warning(
                                    "[ENTRY LIMIT BOARD TOUCH] symbol=%s side=%s price=%s base=%s rule=%s",
                                    kwargs.get("symbol"),
                                    side,
                                    detail.get("price"),
                                    detail.get("base_price"),
                                    detail.get("price_rule"),
                                )
                except Exception:
                    logger.debug("[ENTRY LIMIT BOARD TOUCH] detail patch failed", exc_info=True)
                return ret

            build_entry_order_board_touch._entry_board_touch_wrapped = True  # type: ignore[attr-defined]
            build_entry_order_board_touch._original = old_build  # type: ignore[attr-defined]
            setattr(eob, "build_entry_order", build_entry_order_board_touch)

        _INSTALLED = True
        logger.warning("[ENTRY LIMIT BOARD TOUCH] installed rule=BUY:ask SELL:bid")
        return True

    except Exception:
        logger.exception("[ENTRY LIMIT BOARD TOUCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY LIMIT BOARD TOUCH] auto install failed")


__all__ = ["install"]
