# ============================================================
# File   : core/startup/order_exchange_sor_patch.py
# Version: V2-FORCE-ALL-SENDORDER-SOR-EXCHANGE-9
# ------------------------------------------------------------
# Purpose:
#   kabu Station order payload の Exchange を SOR=9 に統一する。
#   新規・返済・強制返済・再指値など、/sendorder に流れる payload は
#   送信直前でも必ず Exchange=9 に補正する。
# ============================================================

from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_TARGET_EXCHANGE = 9


def _target_exchange() -> int:
    """ユーザー方針: 全発注を SOR=9 に固定する。"""
    return _TARGET_EXCHANGE


def _force_env() -> None:
    # 既存コードがどの環境変数名を見ても 9 になるように寄せる。
    for name in (
        "ENTRY_ORDER_EXCHANGE",
        "KABU_ORDER_EXCHANGE",
        "ORDER_EXCHANGE",
        "EXCHANGE",
    ):
        os.environ[name] = str(_target_exchange())


def _safe_symbol(payload: Any) -> Any:
    try:
        if isinstance(payload, dict):
            return payload.get("Symbol") or payload.get("symbol")
    except Exception:
        pass
    return None


def _force_payload_exchange(payload: Any, *, label: str = "payload") -> Any:
    """dict payload の Exchange を最終的に SOR=9 へ上書きする。"""
    if not isinstance(payload, dict):
        return payload
    try:
        before = payload.get("Exchange")
        payload["Exchange"] = int(_target_exchange())
        if before != payload.get("Exchange"):
            logger.warning(
                "[ORDER EXCHANGE SOR] %s Exchange forced before=%s after=%s symbol=%s",
                label,
                before,
                payload.get("Exchange"),
                _safe_symbol(payload),
            )
    except Exception:
        logger.exception("[ORDER EXCHANGE SOR] %s Exchange force failed payload=%s", label, payload)
    return payload


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
        if callable(cur) and not getattr(cur, "_order_exchange_sor_v2", False):
            orig = getattr(cur, "_original", cur)

            @wraps(orig)
            def patched_make_payload(symbol: Any, side: Any, qty: Any, price: Any, *args, **kwargs):
                requested = kwargs.get("exchange", None)
                kwargs["exchange"] = _target_exchange()
                payload = orig(symbol, side, qty, price, *args, **kwargs)
                _force_payload_exchange(payload, label="buy_sell_entry._make_payload")
                try:
                    if requested not in (None, _target_exchange()):
                        logger.warning(
                            "[ORDER EXCHANGE SOR] requested exchange ignored symbol=%s side=%s requested=%s forced=%s",
                            symbol,
                            side,
                            requested,
                            _target_exchange(),
                        )
                except Exception:
                    pass
                return payload

            patched_make_payload._order_exchange_sor_v2 = True  # type: ignore[attr-defined]
            patched_make_payload._original = orig  # type: ignore[attr-defined]
            bse._make_payload = patched_make_payload
            logger.warning("[ORDER EXCHANGE SOR] patched buy_sell_entry._make_payload target_exchange=%s", target)
            ok = True
    except Exception:
        logger.exception("[ORDER EXCHANGE SOR] _make_payload patch failed")

    # 関数の default 引数も 9 へ寄せる。呼び出し側が明示 exchange=1 を渡しても _make_payload で最後に 9 へ強制する。
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
        if getattr(cur, "_order_exchange_sor_v2", False):
            return True
        orig = getattr(cur, "_original", cur)

        @wraps(orig)
        def patched_send_order_common(payload: Any, *args, **kwargs):
            payload = _force_payload_exchange(payload, label="send_order_common")
            return orig(payload, *args, **kwargs)

        patched_send_order_common._order_exchange_sor_v2 = True  # type: ignore[attr-defined]
        patched_send_order_common._original = orig  # type: ignore[attr-defined]
        so.send_order_common = patched_send_order_common

        # buy_sell_entry は関数を import 済みのことがあるため、参照も差し替える。
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


def _patch_requests_post_sendorder() -> bool:
    """
    最終・最終防衛。
    どこかのモジュールが send_order_common を通さず requests.post(.../sendorder, json=payload) した場合も、
    /sendorder の json payload だけ Exchange=9 にする。Discord等の通常POSTには触らない。
    """
    try:
        import requests
    except Exception:
        logger.exception("[ORDER EXCHANGE SOR] import requests failed")
        return False

    try:
        cur = getattr(requests, "post", None)
        if not callable(cur):
            return False
        if getattr(cur, "_order_exchange_sor_v2", False):
            return True
        orig = getattr(cur, "_original", cur)

        @wraps(orig)
        def patched_requests_post(url: Any, *args, **kwargs):
            try:
                url_s = str(url or "")
                if "/sendorder" in url_s.lower():
                    payload = kwargs.get("json", None)
                    if isinstance(payload, dict):
                        kwargs["json"] = _force_payload_exchange(payload, label="requests.post(/sendorder).json")
            except Exception:
                logger.exception("[ORDER EXCHANGE SOR] requests.post sendorder force failed")
            return orig(url, *args, **kwargs)

        patched_requests_post._order_exchange_sor_v2 = True  # type: ignore[attr-defined]
        patched_requests_post._original = orig  # type: ignore[attr-defined]
        requests.post = patched_requests_post
        logger.warning("[ORDER EXCHANGE SOR] patched requests.post /sendorder final-final defense target_exchange=%s", _target_exchange())
        return True
    except Exception:
        logger.exception("[ORDER EXCHANGE SOR] requests.post patch failed")
        return False


def install() -> bool:
    global _INSTALLED
    _force_env()
    ok1 = _patch_buy_sell_entry()
    ok2 = _patch_send_order_common()
    ok3 = _patch_requests_post_sendorder()
    _INSTALLED = bool(ok1 or ok2 or ok3)
    logger.warning(
        "[ORDER EXCHANGE SOR] installed v2 ok=%s target_exchange=%s buy_sell_entry=%s send_order_common=%s requests_post=%s",
        _INSTALLED,
        _target_exchange(),
        ok1,
        ok2,
        ok3,
    )
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[ORDER EXCHANGE SOR] auto install failed")


__all__ = ["install"]
