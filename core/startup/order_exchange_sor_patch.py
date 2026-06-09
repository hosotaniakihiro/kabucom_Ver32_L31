# ============================================================
# File   : core/startup/order_exchange_sor_patch.py
# Version: V1-FORCE-SOR-EXCHANGE-9
# ------------------------------------------------------------
# Purpose:
#   kabu Station order payload の Exchange を SOR=9 に統一する。
#   entry_handler の直接指値、buy_sell_entry の最良気配指値、成行、逆指値を
#   すべて _make_payload 入口で強制補正する。
# ============================================================

from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _target_exchange() -> int:
    return _env_int("ENTRY_ORDER_EXCHANGE", _env_int("KABU_ORDER_EXCHANGE", 9))


def _patch_buy_sell_entry() -> bool:
    try:
        import kabu_api.buy_sell_entry as bse
    except Exception:
        logger.exception("[ORDER EXCHANGE SOR] import kabu_api.buy_sell_entry failed")
        return False

    target = _target_exchange()
    ok = False

    try:
        old_entry_exchange = getattr(bse, "ENTRY_EXCHANGE", None)
        old_order_exchange = getattr(bse, "ORDER_EXCHANGE", None)
        bse.ENTRY_EXCHANGE = target
        bse.ORDER_EXCHANGE = target
        bse.DEFAULT_EXCHANGE = target
        logger.warning(
            "[ORDER EXCHANGE SOR] constants forced ENTRY_EXCHANGE %s->%s ORDER_EXCHANGE %s->%s",
            old_entry_exchange,
            target,
            old_order_exchange,
            target,
        )
        ok = True
    except Exception:
        logger.exception("[ORDER EXCHANGE SOR] constants force failed")

    try:
        cur = getattr(bse, "_make_payload", None)
        if callable(cur) and not getattr(cur, "_order_exchange_sor_v1", False):
            orig = getattr(cur, "_original", cur)

            @wraps(orig)
            def patched_make_payload(symbol: Any, side: Any, qty: Any, price: Any, *args, **kwargs):
                requested = kwargs.get("exchange", None)
                kwargs["exchange"] = _target_exchange()
                payload = orig(symbol, side, qty, price, *args, **kwargs)
                try:
                    if isinstance(payload, dict):
                        before = payload.get("Exchange")
                        payload["Exchange"] = int(_target_exchange())
                        if before != payload.get("Exchange"):
                            logger.warning(
                                "[ORDER EXCHANGE SOR] payload Exchange forced symbol=%s side=%s requested=%s payload_before=%s payload_after=%s",
                                symbol,
                                side,
                                requested,
                                before,
                                payload.get("Exchange"),
                            )
                except Exception:
                    logger.exception("[ORDER EXCHANGE SOR] payload post-force failed symbol=%s", symbol)
                return payload

            patched_make_payload._order_exchange_sor_v1 = True  # type: ignore[attr-defined]
            patched_make_payload._original = orig  # type: ignore[attr-defined]
            bse._make_payload = patched_make_payload
            logger.warning("[ORDER EXCHANGE SOR] patched buy_sell_entry._make_payload target_exchange=%s", target)
            ok = True
    except Exception:
        logger.exception("[ORDER EXCHANGE SOR] _make_payload patch failed")

    # 関数のdefault引数も9へ寄せる。呼び出し側が明示exchange=1を渡しても _make_payload で最後に9へ強制する。
    for name in (
        "execute_buy_at_best_ask",
        "execute_short_at_best_bid",
        "execute_buy_market",
        "execute_sell_market",
        "execute_buy_stop",
        "execute_short_stop",
    ):
        try:
            fn = getattr(bse, name, None)
            defaults = getattr(fn, "__defaults__", None)
            if callable(fn) and defaults:
                # 各関数の最後の通常defaultが exchange=1 のため、最後だけ9へ置換する。
                d = list(defaults)
                if d:
                    old = d[-1]
                    d[-1] = target
                    fn.__defaults__ = tuple(d)
                    if old != target:
                        logger.warning("[ORDER EXCHANGE SOR] default forced %s exchange %s->%s", name, old, target)
                    ok = True
        except Exception:
            logger.debug("[ORDER EXCHANGE SOR] default patch skipped name=%s", name, exc_info=True)

    return ok


def _patch_send_order_common() -> bool:
    """最終防衛。別モジュールが直接 send_order_common(payload) を呼ぶ場合も Exchange=9 にする。"""
    try:
        import kabu_api.send_order as so
    except Exception:
        logger.exception("[ORDER EXCHANGE SOR] import kabu_api.send_order failed")
        return False

    try:
        cur = getattr(so, "send_order_common", None)
        if not callable(cur):
            return False
        if getattr(cur, "_order_exchange_sor_v1", False):
            return True
        orig = getattr(cur, "_original", cur)

        @wraps(orig)
        def patched_send_order_common(payload: Any, *args, **kwargs):
            try:
                if isinstance(payload, dict):
                    before = payload.get("Exchange")
                    payload["Exchange"] = int(_target_exchange())
                    if before != payload.get("Exchange"):
                        logger.warning(
                            "[ORDER EXCHANGE SOR] send_order_common payload Exchange forced before=%s after=%s symbol=%s",
                            before,
                            payload.get("Exchange"),
                            payload.get("Symbol"),
                        )
            except Exception:
                logger.exception("[ORDER EXCHANGE SOR] send_order_common force failed")
            return orig(payload, *args, **kwargs)

        patched_send_order_common._order_exchange_sor_v1 = True  # type: ignore[attr-defined]
        patched_send_order_common._original = orig  # type: ignore[attr-defined]
        so.send_order_common = patched_send_order_common

        # buy_sell_entry は関数を import 済みなので、参照も差し替える。
        try:
            import kabu_api.buy_sell_entry as bse
            bse.send_order_common = patched_send_order_common
        except Exception:
            pass

        logger.warning("[ORDER EXCHANGE SOR] patched send_order_common final defense target_exchange=%s", _target_exchange())
        return True
    except Exception:
        logger.exception("[ORDER EXCHANGE SOR] send_order_common patch failed")
        return False


def install() -> bool:
    global _INSTALLED
    os.environ["ENTRY_ORDER_EXCHANGE"] = str(_target_exchange())
    os.environ["KABU_ORDER_EXCHANGE"] = str(_target_exchange())
    ok1 = _patch_buy_sell_entry()
    ok2 = _patch_send_order_common()
    _INSTALLED = bool(ok1 or ok2)
    logger.warning(
        "[ORDER EXCHANGE SOR] installed v1 ok=%s target_exchange=%s buy_sell_entry=%s send_order_common=%s",
        _INSTALLED,
        _target_exchange(),
        ok1,
        ok2,
    )
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[ORDER EXCHANGE SOR] auto install failed")

__all__ = ["install"]
