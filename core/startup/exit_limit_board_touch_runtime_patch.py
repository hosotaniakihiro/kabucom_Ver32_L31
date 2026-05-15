# ============================================================
# File   : core/startup/exit_limit_board_touch_runtime_patch.py
# Version: V1.0-EXIT-BOARD-TOUCH-LIMIT
# ------------------------------------------------------------
# 目的:
#   EXIT返済注文を成行固定から、板タッチ指値へ変更する。
#
# 背景:
#   成行EXITは約定優先だが、BUY建玉の返済売りはbidへ、
#   SELL建玉の返済買いはaskへぶつかるため、スプレッド分不利になりやすい。
#   さらに板が薄い場合は複数ティック滑る。
#
# 方式:
#   BUY建玉返済  close_side=SELL -> bid指値
#   SELL建玉返済 close_side=BUY  -> ask指値
#
# 注意:
#   bid/askが取れない場合は安全のため従来通り成行にfallbackする。
#
# 環境変数:
#   EXIT_LIMIT_BOARD_TOUCH_ENABLED=1
#   EXIT_LIMIT_FALLBACK_MARKET_IF_NO_BOARD=1
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        return float(default) if x != x else x
    except Exception:
        return float(default)


def _get_bid_ask(symbol: str) -> tuple[float, float]:
    # 既存utilityを優先。失敗してもEXIT本体を止めない。
    try:
        from utils_common import get_latest_bid_ask
        ret = get_latest_bid_ask(str(symbol))
        if isinstance(ret, dict):
            bid = _f(ret.get("bid") or ret.get("BidPrice") or ret.get("best_bid"), 0.0)
            ask = _f(ret.get("ask") or ret.get("AskPrice") or ret.get("best_ask"), 0.0)
            return bid, ask
        if isinstance(ret, (tuple, list)) and len(ret) >= 2:
            return _f(ret[0], 0.0), _f(ret[1], 0.0)
    except Exception:
        logger.debug("[EXIT LIMIT BOARD TOUCH] utils_common.get_latest_bid_ask failed symbol=%s", symbol, exc_info=True)

    # push最新tick fallback
    try:
        from global_state import global_data
        tick = global_data.get_latest_tick(str(symbol))
        if isinstance(tick, dict):
            bid = _f(tick.get("BidPrice") or tick.get("bid") or tick.get("best_bid"), 0.0)
            ask = _f(tick.get("AskPrice") or tick.get("ask") or tick.get("best_ask"), 0.0)
            return bid, ask
    except Exception:
        logger.debug("[EXIT LIMIT BOARD TOUCH] global tick bid/ask failed symbol=%s", symbol, exc_info=True)

    return 0.0, 0.0


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.exit.executor as ex
    except Exception:
        logger.exception("[EXIT LIMIT BOARD TOUCH] import trading.exit.executor failed")
        return False

    old_builder = getattr(ex, "_build_kabu_close_payload", None)
    if not callable(old_builder):
        logger.warning("[EXIT LIMIT BOARD TOUCH] _build_kabu_close_payload not callable")
        return False

    if not getattr(old_builder, "_exit_limit_board_touch_wrapped_v1", False):
        def wrapped_build_kabu_close_payload(*, symbol: str, close_side: str, qty: float, exchange: int = 1):
            payload = old_builder(symbol=symbol, close_side=close_side, qty=qty, exchange=exchange)
            if not _env_bool("EXIT_LIMIT_BOARD_TOUCH_ENABLED", True):
                return payload

            bid, ask = _get_bid_ask(str(symbol))
            close_side_u = str(close_side or "").upper()
            limit_price = 0.0
            rule = ""

            if close_side_u == "SELL" and bid > 0:
                # BUY建玉の返済売り。成行で板を下に食わないよう、best bidへ指値。
                limit_price = bid
                rule = "SELL_CLOSE_AT_BID"
            elif close_side_u == "BUY" and ask > 0:
                # SELL建玉の返済買い。成行で板を上に食わないよう、best askへ指値。
                limit_price = ask
                rule = "BUY_CLOSE_AT_ASK"

            if limit_price > 0:
                payload["FrontOrderType"] = 20
                payload["Price"] = float(limit_price)
                logger.warning(
                    "[EXIT LIMIT BOARD TOUCH] market->limit symbol=%s close_side=%s rule=%s bid=%s ask=%s price=%s qty=%s",
                    symbol, close_side_u, rule, bid, ask, limit_price, qty,
                )
                return payload

            if _env_bool("EXIT_LIMIT_FALLBACK_MARKET_IF_NO_BOARD", True):
                logger.warning(
                    "[EXIT LIMIT BOARD TOUCH] bid/ask missing -> fallback MARKET symbol=%s close_side=%s bid=%s ask=%s",
                    symbol, close_side_u, bid, ask,
                )
                return payload

            logger.warning(
                "[EXIT LIMIT BOARD TOUCH] bid/ask missing and market fallback disabled symbol=%s close_side=%s",
                symbol, close_side_u,
            )
            return payload

        wrapped_build_kabu_close_payload._exit_limit_board_touch_wrapped_v1 = True  # type: ignore[attr-defined]
        wrapped_build_kabu_close_payload._original = old_builder  # type: ignore[attr-defined]
        ex._build_kabu_close_payload = wrapped_build_kabu_close_payload

    _INSTALLED = True
    logger.warning("[EXIT LIMIT BOARD TOUCH] installed rule=BUY-position close SELL:bid / SELL-position close BUY:ask")
    return True


try:
    install()
except Exception:
    logger.exception("[EXIT LIMIT BOARD TOUCH] auto install failed")

__all__ = ["install"]
