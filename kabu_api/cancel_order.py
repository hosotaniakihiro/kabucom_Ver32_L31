# ============================================================
# File   : kabu_api/cancel_order.py
# Version: V1.1-PRODUCTION-CANCEL-ORDER-COMMON
# ------------------------------------------------------------
# kabuステーションAPI /cancelorder を呼び出す共通モジュール。
#
# 用途:
#   - エントリー注文を出して一定秒数たっても約定しない場合に取消する
#
# kabu API:
#   PUT /cancelorder
#   body: {"Password": "...", "OrderId": "..."}
# ============================================================

from __future__ import annotations

import configparser
import logging
from typing import Any

import requests

from token_manager import get_valid_token

logger = logging.getLogger(__name__)

API_URL = "http://localhost:18080/kabusapi"

conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
Password = conf.get("aukabu", "password", fallback="")


def cancel_order_common(order_id: str, *, symbol: str = "", reason: str = "") -> bool:
    """
    kabuステーションAPI /cancelorder を呼ぶ。

    戻り値:
      True:
        取消APIが正常終了した

      False:
        取消失敗、すでに約定済み、OrderId不正、通信エラー等
    """
    oid = str(order_id or "").strip()
    sym = str(symbol or "").strip()

    if not oid:
        logger.warning(
            "[CANCEL ORDER] skip empty order_id symbol=%s reason=%s",
            sym,
            reason,
        )
        return False

    token = get_valid_token()
    if not token:
        logger.error(
            "[CANCEL ORDER] token unavailable symbol=%s order_id=%s reason=%s",
            sym,
            oid,
            reason,
        )
        return False

    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": token,
    }

    payload = {
        "Password": Password,
        "OrderId": oid,
    }

    url = f"{API_URL}/cancelorder"

    try:
        logger.warning(
            "[CANCEL ORDER] request symbol=%s order_id=%s reason=%s",
            sym,
            oid,
            reason,
        )

        res = requests.put(url, json=payload, headers=headers, timeout=5)

        try:
            data: Any = res.json()
        except Exception:
            data = res.text

        if res.status_code != 200:
            logger.warning(
                "[CANCEL ORDER] http_ng symbol=%s order_id=%s status=%s response=%s reason=%s",
                sym,
                oid,
                res.status_code,
                data,
                reason,
            )
            return False

        result = None
        if isinstance(data, dict):
            result = data.get("Result")

        ok = False
        try:
            ok = int(result) == 0
        except Exception:
            # kabuSの応答差異に備え、200応答でResultが無い場合も成功扱いに寄せる
            ok = True

        if ok:
            logger.warning(
                "[CANCEL ORDER] success symbol=%s order_id=%s response=%s reason=%s",
                sym,
                oid,
                data,
                reason,
            )
            return True

        logger.warning(
            "[CANCEL ORDER] api_ng symbol=%s order_id=%s response=%s reason=%s",
            sym,
            oid,
            data,
            reason,
        )
        return False

    except Exception:
        logger.exception(
            "[CANCEL ORDER] exception symbol=%s order_id=%s reason=%s",
            sym,
            oid,
            reason,
        )
        return False


def cancel_order(order_id: str, token: str | None = None):
    """
    旧コード互換 wrapper。
    token 引数は互換のため残すが、実際は get_valid_token() を利用する。
    """
    return cancel_order_common(order_id, reason="legacy_cancel_order_wrapper")


__all__ = ["cancel_order_common", "cancel_order"]
