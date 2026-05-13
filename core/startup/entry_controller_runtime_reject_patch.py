# ============================================================
# File   : core/startup/entry_controller_runtime_reject_patch.py
# Version: PRODUCTION-ENTRY-ORDER-REJECT-PATCH-V5-CLEAN-100368-CACHE
# ------------------------------------------------------------
# 目的:
#   kabu APIで注文IDが空になった時に、entry_controller 側で原因を見える化する。
#
# 重要修正:
#   - Code=100368
#     「現在、株式信用新規の注文は抑止されております。」は、
#     銘柄個別の trade_restricted / SELL拒否キャッシュ扱いにしない。
#     その注文は失敗として扱うが、次候補・次サイクルは止めない。
#   - 起動直後に古い100368 SELL拒否キャッシュが残っていても自動削除する。
#   - Code=100033
#     「この銘柄のお取引は制限されています。」は銘柄個別制限として扱う。
#   - BUY/SELL とも信用新規のまま。現物化はしない。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False

KABU_CODE_CREDIT_NEW_ORDER_SUPPRESSED = "100368"
KABU_CODE_SYMBOL_TRADE_RESTRICTED = "100033"


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


def _trade_restrict_detail(ec: Any, symbol: str) -> dict:
    try:
        until = ec.global_data.trade_restricted.get(symbol)
        return {"until": until}
    except Exception:
        return {}


def _last_send_error() -> dict:
    try:
        from kabu_api.send_order import get_last_send_order_error

        err = get_last_send_order_error()
        return dict(err) if isinstance(err, dict) else {}
    except Exception:
        return {}


def _norm_code(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _is_100368_error(err: dict) -> bool:
    code = _norm_code(err.get("code"))
    msg = str(err.get("message") or "")
    if code == KABU_CODE_CREDIT_NEW_ORDER_SUPPRESSED:
        return True
    if "信用新規" in msg and "抑止" in msg:
        return True
    return False


def _is_100033_error(err: dict) -> bool:
    code = _norm_code(err.get("code"))
    msg = str(err.get("message") or "")
    if code == KABU_CODE_SYMBOL_TRADE_RESTRICTED:
        return True
    if "この銘柄" in msg and "取引" in msg and "制限" in msg:
        return True
    return False


def _same_order_error(err: dict, *, symbol: str, side: str) -> bool:
    """
    send_order.py の last_error が今回の注文に対応しているかを軽く確認する。
    side は BUY/SELL、send_order.py 側も BUY/SELL で保存する。
    """
    try:
        if not err:
            return False
        err_symbol = str(err.get("symbol") or "").strip()
        err_side = str(err.get("side") or "").strip().upper()
        return err_symbol == str(symbol) and err_side == str(side).upper()
    except Exception:
        return False


def _cleanup_stale_100368_sell_cache() -> int:
    """
    古いバージョンや起動時パッチで 100368 が sell_order_reject_cache に入っていた場合、
    起動直後に削除する。

    100368 は銘柄個別SELL NGではなく、API側の信用新規抑止応答として扱うため、
    キャッシュに残すと翌サイクル以降の候補が不当に消える。
    """
    removed = 0
    try:
        from global_state import global_data

        cache = getattr(global_data, "sell_order_reject_cache", None)
        if not isinstance(cache, dict):
            return 0

        for sym, rec in list(cache.items()):
            if not isinstance(rec, dict):
                continue
            code = _norm_code(rec.get("code"))
            msg = str(rec.get("message") or "")
            if code == KABU_CODE_CREDIT_NEW_ORDER_SUPPRESSED or ("信用新規" in msg and "抑止" in msg):
                cache.pop(sym, None)
                removed += 1

        if removed:
            logger.warning(
                "[ENTRY REJECT PATCH] stale 100368 sell reject cache cleaned removed=%s",
                removed,
            )
        return removed
    except Exception:
        logger.exception("[ENTRY REJECT PATCH] stale 100368 cache cleanup failed")
        return removed


def install() -> bool:
    global _PATCHED

    # 既にpatch済みでも、古い100368キャッシュだけは掃除する。
    _cleanup_stale_100368_sell_cache()

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

    if getattr(old_fn, "_runtime_reject_patched_v5", False):
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
                "⚠ ENTRY_QTY_ZERO_SKIP symbol=%s qty_raw=%s side=%s price=%s -> no order dispatch entry_diag=%s",
                symbol,
                qty,
                side,
                price,
                _short_dict(entry_row, ["source", "entry_type", "interval", "close", "price", "score_buy", "score_sell"]),
            )
            ec._log_skip(symbol, "ENTRY_QTY_ZERO", side=side, price=price, qty_raw=qty)
            return False

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
            last_err = _last_send_error()
            same_err = _same_order_error(last_err, symbol=symbol, side=side)

            # 100368は銘柄個別停止にしない。今回の注文は失敗だが、次候補・次サイクルは止めない。
            if same_err and _is_100368_error(last_err):
                ec.logger.warning(
                    "🚫 CREDIT_NEW_ORDER_API_REJECT_NO_LOCAL_SUPPRESS_CONFIRMED symbol=%s side=%s qty=%s order_type=%s price=%s last_error=%s",
                    symbol,
                    side,
                    order_qty,
                    order_type,
                    order_price,
                    last_err,
                )
                ec._log_skip(
                    symbol,
                    "CREDIT_NEW_ORDER_API_REJECT_NO_LOCAL_SUPPRESS",
                    side=side,
                    qty=order_qty,
                    order_type=order_type,
                    price=order_price,
                    code=last_err.get("code"),
                    message=last_err.get("message"),
                )
                return False

            # 100033等は銘柄個別制限。
            if same_err and _is_100033_error(last_err):
                detail = {"code": last_err.get("code"), "message": last_err.get("message")}
                ec.logger.warning(
                    "🚫 SYMBOL_TRADE_RESTRICTED_BY_KABU_API_CONFIRMED symbol=%s side=%s qty=%s order_type=%s price=%s detail=%s",
                    symbol,
                    side,
                    order_qty,
                    order_type,
                    order_price,
                    detail,
                )
                ec._log_skip(
                    symbol,
                    "SYMBOL_TRADE_RESTRICTED_BY_KABU_API",
                    side=side,
                    qty=order_qty,
                    order_type=order_type,
                    price=order_price,
                    **detail,
                )
                return False

            # 既存の trade_restricted は100033等だけが入る前提。
            try:
                if ec._is_symbol_trade_restricted(symbol):
                    detail = _trade_restrict_detail(ec, symbol)
                    ec.logger.warning(
                        "🚫 SYMBOL_TRADE_RESTRICTED_CONFIRMED symbol=%s side=%s qty=%s order_type=%s price=%s detail=%s",
                        symbol,
                        side,
                        order_qty,
                        order_type,
                        order_price,
                        detail,
                    )
                    ec._log_skip(
                        symbol,
                        "SYMBOL_TRADE_RESTRICTED",
                        side=side,
                        qty=order_qty,
                        order_type=order_type,
                        price=order_price,
                        **detail,
                    )
                    return False
            except Exception:
                ec.logger.exception("trade_restricted post-order check failed symbol=%s", symbol)

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
                    "🚫 BUY_ORDER_ID_EMPTY_DIAG symbol=%s qty=%s order_type=%s price=%s entry_diag=%s order_detail=%s ai_diag=%s possible=%s last_error=%s",
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
                    last_err,
                )

            ec.logger.warning(
                "⚠ ORDER_ID_EMPTY_NO_LONG_RESTRICT symbol=%s side=%s qty=%s order_type=%s price=%s last_error=%s -> retry allowed next cycle",
                symbol,
                side,
                order_qty,
                order_type,
                order_price,
                last_err,
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
    _execute_best_candidate_patched._runtime_reject_patched_v3 = True  # type: ignore[attr-defined]
    _execute_best_candidate_patched._runtime_reject_patched_v4 = True  # type: ignore[attr-defined]
    _execute_best_candidate_patched._runtime_reject_patched_v5 = True  # type: ignore[attr-defined]
    ec._execute_best_candidate = _execute_best_candidate_patched

    _PATCHED = True
    logger.warning(
        "[ENTRY REJECT PATCH] installed v5 target=trading.handlers.entry_controller._execute_best_candidate no_100368_cache=True clean_stale_cache=True"
    )
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY REJECT PATCH] auto install failed")
