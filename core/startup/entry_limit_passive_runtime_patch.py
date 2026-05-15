# ============================================================
# File   : core/startup/entry_limit_passive_runtime_patch.py
# Version: V1.0-PASSIVE-ONE-TICK-LIMIT
# ------------------------------------------------------------
# SUMMARY_AI エントリー指値を以下へ変更する runtime patch。
#
#   BUY  : ask を基準に 1ティック下の指値
#   SELL : bid を基準に 1ティック上の指値
#
# 目的:
#   - 成行/攻め指値による不利約定を抑える
#   - 返済時の成行1ティック不利・信用金利を考慮し、入口を少し有利にする
#
# 注意:
#   - 約定率は下がる可能性がある
#   - 10秒未約定キャンセルと組み合わせる前提
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
        logger.exception("[ENTRY LIMIT PASSIVE] import failed")
        return False

    try:
        old_build = getattr(eob, "build_entry_order", None)
        get_tick_size = getattr(eob, "get_tick_size", None)
        round_price = getattr(eob, "_round_price", None)
        if not callable(old_build) or not callable(get_tick_size) or not callable(round_price):
            logger.warning("[ENTRY LIMIT PASSIVE] required functions not callable")
            return False

        # detailログにも -1 と出す。実価格計算は下の関数で強制する。
        setattr(eob, "SUMMARY_AGGRESSIVE_LIMIT_TICKS", -1)

        def _passive_one_tick_limit_price(base_price: float, side: str, ticks: int = -1) -> float:
            p = _safe_float(base_price, 0.0)
            if p <= 0:
                return p
            side_u = str(side or "").upper()
            rounded = round_price(p, side_u)
            tick = _safe_float(get_tick_size(rounded), 1.0)
            if tick <= 0:
                tick = 1.0

            if side_u == "BUY":
                # ask基準から1ティック下
                return max(tick, rounded - tick)
            if side_u == "SELL":
                # bid基準から1ティック上
                return rounded + tick
            return rounded

        _passive_one_tick_limit_price._entry_passive_one_tick = True  # type: ignore[attr-defined]
        setattr(eob, "_aggressive_limit_price", _passive_one_tick_limit_price)

        if not getattr(old_build, "_entry_passive_wrapped", False):
            def build_entry_order_passive(*args: Any, **kwargs: Any) -> Dict[str, Any]:
                ret = old_build(*args, **kwargs)
                try:
                    if isinstance(ret, dict) and ret.get("ok"):
                        detail = ret.get("detail")
                        if isinstance(detail, dict):
                            src = str(kwargs.get("source") or "").upper()
                            side = str(kwargs.get("side") or "").upper()
                            if src == "SUMMARY_AI" and detail.get("order_type") == "LIMIT":
                                detail["aggressive_limit_ticks"] = -1
                                detail["entry_limit_mode"] = "PASSIVE_ONE_TICK"
                                detail["price_rule"] = "BUY=ask-1tick / SELL=bid+1tick"
                                if detail.get("board"):
                                    detail["price_source"] = "board_bid_ask_passive_1tick"
                                else:
                                    detail["price_source"] = "summary_fallback_passive_1tick"
                                logger.warning(
                                    "[ENTRY LIMIT PASSIVE] symbol=%s side=%s price=%s base=%s rule=%s",
                                    kwargs.get("symbol"),
                                    side,
                                    detail.get("price"),
                                    detail.get("base_price"),
                                    detail.get("price_rule"),
                                )
                except Exception:
                    logger.debug("[ENTRY LIMIT PASSIVE] detail patch failed", exc_info=True)
                return ret

            build_entry_order_passive._entry_passive_wrapped = True  # type: ignore[attr-defined]
            build_entry_order_passive._original = old_build  # type: ignore[attr-defined]
            setattr(eob, "build_entry_order", build_entry_order_passive)

        _INSTALLED = True
        logger.warning("[ENTRY LIMIT PASSIVE] installed rule=BUY:ask-1tick SELL:bid+1tick")
        return True

    except Exception:
        logger.exception("[ENTRY LIMIT PASSIVE] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY LIMIT PASSIVE] auto install failed")


__all__ = ["install"]
