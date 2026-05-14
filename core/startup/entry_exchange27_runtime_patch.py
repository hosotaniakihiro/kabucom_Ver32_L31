# ============================================================
# File   : core/startup/entry_exchange27_runtime_patch.py
# Version: V1-PRODUCTION-CREDIT-ENTRY-EXCHANGE27-GUARD
# ------------------------------------------------------------
# 目的:
#   信用新規エントリー注文で Exchange=1 が残っていても、
#   起動時に kabu_api.buy_sell_entry を monkey patch して
#   Exchange=27（東証+）へ補正する。
#
# 背景:
#   kabuステーション仕様上、通常時に東証=1を指定しての
#   株式信用新規注文は 100368 で抑止される。
#
# 対象:
#   kabu_api.buy_sell_entry から作られる信用新規payloadのみ。
#   返済注文ファイルは建玉市場に合わせる必要があるため触らない。
# ============================================================

from __future__ import annotations

import logging
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)

ENTRY_EXCHANGE = 27
_PATCHED = False


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import kabu_api.buy_sell_entry as bse
    except Exception:
        logger.exception("[ENTRY EXCHANGE27 PATCH] import failed")
        return False

    try:
        setattr(bse, "ENTRY_EXCHANGE", ENTRY_EXCHANGE)
    except Exception:
        logger.exception("[ENTRY EXCHANGE27 PATCH] set ENTRY_EXCHANGE failed")

    old_make_payload = getattr(bse, "_make_payload", None)
    if not callable(old_make_payload):
        logger.warning("[ENTRY EXCHANGE27 PATCH] _make_payload not callable")
        return False

    if getattr(old_make_payload, "_entry_exchange27_patched", False):
        _PATCHED = True
        return True

    @wraps(old_make_payload)
    def _make_payload_exchange27_guard(*args: Any, **kwargs: Any):
        # 信用新規エントリーは CashMargin=2 / DelivType=0。
        # buy_sell_entry._make_payload は DelivType を固定で0にしているため、
        # cash_margin=2 のpayloadは信用新規として Exchange=27 に補正する。
        cash_margin = kwargs.get("cash_margin", 2)
        old_exchange = kwargs.get("exchange", None)

        try:
            if int(cash_margin) == 2:
                if old_exchange != ENTRY_EXCHANGE:
                    logger.warning(
                        "[ENTRY EXCHANGE27 PATCH] override exchange old=%s new=%s reason=credit_entry",
                        old_exchange,
                        ENTRY_EXCHANGE,
                    )
                kwargs["exchange"] = ENTRY_EXCHANGE
        except Exception:
            kwargs["exchange"] = ENTRY_EXCHANGE

        payload = old_make_payload(*args, **kwargs)

        try:
            if isinstance(payload, dict) and int(payload.get("CashMargin", 0)) == 2:
                old_payload_exchange = payload.get("Exchange")
                if int(old_payload_exchange) != ENTRY_EXCHANGE:
                    logger.warning(
                        "[ENTRY EXCHANGE27 PATCH] payload exchange corrected old=%s new=%s symbol=%s",
                        old_payload_exchange,
                        ENTRY_EXCHANGE,
                        payload.get("Symbol"),
                    )
                    payload["Exchange"] = ENTRY_EXCHANGE
        except Exception:
            logger.exception("[ENTRY EXCHANGE27 PATCH] payload post correction failed")

        return payload

    _make_payload_exchange27_guard._entry_exchange27_patched = True  # type: ignore[attr-defined]
    _make_payload_exchange27_guard._original_make_payload = old_make_payload  # type: ignore[attr-defined]
    bse._make_payload = _make_payload_exchange27_guard

    # 既定値が exchange=1 の古い関数でも、内部の _make_payload で27へ補正される。
    _PATCHED = True
    logger.warning("[ENTRY EXCHANGE27 PATCH] installed ENTRY_EXCHANGE=%s", ENTRY_EXCHANGE)
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY EXCHANGE27 PATCH] auto install failed")
