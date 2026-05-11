# ============================================================
# File   : core/startup/entry_controller_runtime_reject_patch.py
# Version: PRODUCTION-ENTRY-ORDER-REJECT-PATCH-V2-BUY-DIAG
# ------------------------------------------------------------
# 目的:
#   kabu API Code=100368/100033 等で注文IDが空になった時に、
#   entry_controller 側で原因を見える化する。
#
# 背景:
#   BUY候補が pending / entry_pipeline までは通っても、
#   最終注文段階で APPROVED=0 になるケースがある。
#   旧ログでは SELL 拒否以外の BUY 側 ORDER_ID_EMPTY の原因が追いにくい。
#
# 方針:
#   entry_controller._execute_best_candidate を runtime patch し、
#   ORDER_ID_EMPTY 時に BUY / SELL 別の診断ログを追加する。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False


def _short_dict(d: dict | None, keys: list[str]) -> dict:
    if not isinstance(d, dict):
        return {}
    out = {}
    for k in keys:
        try:
            if k in d:
                out[k] = d.get(k)
        except Exception:
            pass
    return out


def install() -> bool:
    global _PATCHED

    if _PATCHED:
        return True

    try:
        import trading.handlers.entry_controller as ec
        from AI.sell_order_reject_cache import is_sell_rejected, get_sell_reject_reason
    except Exception:
        logger.exception("[ENTRY REJECT PATCH] import failed")
        return False

    old_fn = getattr(ec, "_execute_best_candidate", None)
    if not callable(old_fn):
        logger.warning("[ENTRY REJECT PATCH] target _execute_best_candidate not found")
        return False

    if getattr(old_fn, "_runtime_reject_patched_v2", False):
        _PATCHED = True
        return True

    def _execute_best_candidate_patched(item: dict, boost_active: bool) -> bool:
        symbol = item["symbol"]
        entry_row = item["entry_row"]
        entry_type = item["entry_type"]
        side = item["side"]
        ai = item["ai"]

        price = ec._resolve_price(entry_row)

        qty = ec.calculate_entry_quantity(
            symbol=symbol,
            price=price,
            confidence=ai.get("confidence", 0.0),
            lot_multiplier=ai.get("lot_multiplier", 1.0),
            atr=entry_row.get("atr")
            or entry_row.get("atr_1m")
            or entry_row.get("atr_5m"),
        )

        ec.logger.info(
            "🧮 ENTRY_QTY_CALC symbol=%s side=%s price=%s confidence=%s lot_multiplier=%s qty_raw=%s boost_active=%s entry_diag=%s ai_diag=%s",
            symbol,
            side,
            price,
            ai.get("confidence", 0.0),
            ai.get("lot_multiplier", 1.0),
            qty,
            boost_active,
            _short_dict(entry_row, [
                "source", "entry_type", "interval", "close", "close_price", "price",
                "score", "score_buy", "score_sell", "buy_score", "sell_score",
                "score_total", "final_score", "display_score", "volume", "turnover",
            ]),
            _short_dict(ai, ["allow", "confidence", "lot_multiplier", "reason"]),
        )

        if qty <= 0:
            ec.logger.warning(
                "⚠ ENTRY_QTY_FALLBACK symbol=%s qty_raw=%s -> MIN_ENTRY_QTY=%s",
                symbol,
                qty,
                ec.MIN_ENTRY_QTY,
            )
            qty = ec.MIN_ENTRY_QTY

        if boost_active:
            qty = int(qty * ec.BOOST_SIZE_MULTIPLIER)
            if qty <= 0:
                qty = ec.MIN_ENTRY_QTY

        ec.logger.info(
            "🧮 ENTRY_QTY_FINAL symbol=%s side=%s qty_final=%s boost_active=%s",
            symbol,
            side,
            qty,
            boost_active,
        )

        order = ec.build_entry_order(
            symbol=symbol,
            side=side,
            source=entry_type,
            entry_row=entry_row,
            qty_override=qty,
        )

        if not isinstance(order, dict):
            ec._log_skip(symbol, "ORDER_BUILD_INVALID", side=side, order_type=type(order).__name__)
            return False

        if not order.get("ok"):
            ec.logger.warning(
                "🚫 ORDER_BUILD_NG_DETAIL symbol=%s side=%s order=%s entry_diag=%s",
                symbol,
                side,
                order,
                _short_dict(entry_row, ["source", "entry_type", "interval", "price", "close", "close_price", "score_buy", "score_sell"]),
            )
            ec._log_skip(symbol, "ORDER_BUILD_NG", side=side, detail=order)
            return False

        d = order.get("detail") or {}
        order_qty = ec._safe_int(d.get("qty"), qty)
        order_type = ec._safe_str(d.get("order_type"), "LIMIT").upper()
        order_price = d.get("price")

        ec.logger.info(
            "📝 ORDER_BUILD_OK symbol=%s side=%s qty=%s order_type=%s price=%s source=%s entry_type=%s order_detail=%s",
            symbol,
            side,
            order_qty,
            order_type,
            order_price,
            entry_row.get("source"),
            entry_type,
            d,
        )

        ec.logger.info(
            "📤 ENTRY_DISPATCH symbol=%s side=%s qty=%s order_type=%s price=%s handler=%s",
            symbol,
            side,
            order_qty,
            order_type,
            order_price,
            "place_entry_buy" if side == "BUY" else "place_entry_sell",
        )

        order_id = (
            ec.place_entry_buy(
                symbol,
                entry_row.get("symbolname"),
                order_price,
                ai.get("reason", ""),
                order_type,
                order_qty,
            )
            if side == "BUY"
            else ec.place_entry_sell(
                symbol,
                entry_row.get("symbolname"),
                order_price,
                ai.get("reason", ""),
                order_type,
                order_qty,
            )
        )

        if not order_id:
            if side == "SELL" and is_sell_rejected(symbol):
                reason = get_sell_reject_reason(symbol)
                ec.logger.warning(
                    "🚫 SELL_ORDER_REJECTED_BY_KABU_API symbol=%s qty=%s order_type=%s price=%s %s",
                    symbol,
                    order_qty,
                    order_type,
                    order_price,
                    reason,
                )
                ec._log_skip(
                    symbol,
                    "SELL_ORDER_REJECTED_BY_KABU_API",
                    side=side,
                    qty=order_qty,
                    order_type=order_type,
                    price=order_price,
                    reject_reason=reason,
                )
                return False

            if side == "BUY":
                ec.logger.warning(
                    "🚫 BUY_ORDER_ID_EMPTY_DIAG symbol=%s qty=%s order_type=%s price=%s entry_diag=%s order_detail=%s ai_diag=%s possible=%s",
                    symbol,
                    order_qty,
                    order_type,
                    order_price,
                    _short_dict(entry_row, [
                        "source", "entry_type", "interval", "close", "close_price", "price",
                        "score", "score_buy", "score_sell", "buy_score", "sell_score",
                        "score_total", "final_score", "display_score", "volume", "turnover",
                    ]),
                    d,
                    _short_dict(ai, ["allow", "confidence", "lot_multiplier", "reason"]),
                    "kabu_api_returned_empty_order_id_or_place_entry_buy_suppressed",
                )

            ec.logger.warning(
                "⚠ ORDER_ID_EMPTY_NO_LONG_RESTRICT symbol=%s side=%s qty=%s order_type=%s price=%s -> retry allowed next cycle",
                symbol,
                side,
                order_qty,
                order_type,
                order_price,
            )
            ec._log_skip(
                symbol,
                "ORDER_ID_EMPTY_RETRYABLE",
                side=side,
                qty=order_qty,
                order_type=order_type,
                price=order_price,
            )
            return False

        ec.global_data.add_entry_inflight(symbol, order_id, side)

        ec.logger.info(
            "🚀 ENTRY_APPROVED symbol=%s side=%s qty=%s order_type=%s price=%s priority=%.4f order_id=%s",
            symbol,
            side,
            order_qty,
            order_type,
            order_price,
            item.get("priority_score", 0.0),
            order_id,
        )
        return True

    _execute_best_candidate_patched._runtime_reject_patched = True  # type: ignore[attr-defined]
    _execute_best_candidate_patched._runtime_reject_patched_v2 = True  # type: ignore[attr-defined]
    ec._execute_best_candidate = _execute_best_candidate_patched

    _PATCHED = True
    logger.warning("[ENTRY REJECT PATCH] installed v2 target=trading.handlers.entry_controller._execute_best_candidate buy_diag=True")
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY REJECT PATCH] auto install failed")
