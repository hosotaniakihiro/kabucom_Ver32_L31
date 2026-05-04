# ============================================================
# trading/ranking/entry_executor.py
# Ver26.1-EXECUTION-ONLY-FIXED-GUARDED
# ------------------------------------------------------------
# ✔ 発注・約定・DB登録・通知に専念
# ✔ 判定ロジックは一切持たない
# ✔ buy_sell.py の実装と完全整合
# ✔ list / dict / None を最終層で完全吸収
# ============================================================

import logging
import time
import configparser
from datetime import datetime
from typing import Any

from kabu_api.get_order_status import get_order_status
from kabu_api.get_board import get_best_quotes
from kabu_api.cancel_order import cancel_order

# ★ 実在関数
from kabu_api.buy_sell import execute_buy_order, execute_short_order

from token_manager import get_valid_token
from database import Session_position
from database.models import Position
from utils.alerts_util import send_discord_notify

logger = logging.getLogger(__name__)

# ============================================================
# 設定
# ============================================================

conf = configparser.ConfigParser()
conf.read("settings.ini", encoding="utf-8")
ENTRY_TIMEOUT = conf.getint("strategy", "entry_timeout", fallback=10)


# ============================================================
# 内部ユーティリティ（型ガード）
# ============================================================

def _normalize_text(value: Any, fallback: str = "") -> str:
    """
    symbolname / reason 用
    list / dict / None → str に強制
    """
    if value is None:
        return fallback

    if isinstance(value, str):
        return value

    if isinstance(value, dict):
        return value.get("reason") or value.get("name") or str(value)

    if isinstance(value, list):
        if not value:
            return fallback
        first = value[0]
        if isinstance(first, dict):
            return first.get("reason") or first.get("name") or str(first)
        return str(first)

    return str(value)


# ============================================================
# メイン：発注〜約定〜保存
# ============================================================

def confirm_and_entry(
    symbol: str,
    symbolname: Any,
    qty: int,
    is_buy: bool,
    reason: Any,
) -> bool:
    """
    板最良気配で発注し、ENTRY_TIMEOUT秒以内に約定した分のみを
    Position として保存する。

    ・価格は板から自動決定
    ・約定0はノーポジ
    """

    # --------------------------------------------------------
    # 🔒 最終正規化（ここが防波堤）
    # --------------------------------------------------------
    symbolname = _normalize_text(symbolname, fallback="UNKNOWN")
    reason = _normalize_text(reason, fallback="RANKING_ENTRY")

    try:
        # ----------------------------------------------------
        # トークン取得
        # ----------------------------------------------------
        token = get_valid_token()
        if not token:
            logger.error("❌ トークン取得失敗 → 発注中止")
            return False

        if qty <= 0:
            logger.warning(f"⚠️ 発注数量が不正: {symbol} qty={qty}")
            return False

        # ----------------------------------------------------
        # 板情報
        # ----------------------------------------------------
        best_ask, best_bid, ask_qty, bid_qty = get_best_quotes(symbol)
        if not best_ask or not best_bid:
            logger.error(f"❌ 板情報取得失敗: {symbol}")
            return False

        # ----------------------------------------------------
        # 発注
        # ----------------------------------------------------
        if is_buy:
            price = best_ask
            side = "BUY_CREDIT"
            action = "BUY"
            order_id = execute_buy_order(symbol, price, qty, token)
        else:
            price = best_bid
            side = "SELL_CREDIT"
            action = "SELL"
            order_id = execute_short_order(symbol, price, qty, token)

        # ----------------------------------------------------
        # 約定監視
        # ----------------------------------------------------
        filled_qty = 0
        remain_qty = qty

        for _ in range(ENTRY_TIMEOUT):
            time.sleep(1)
            status = get_order_status(order_id, token)
            if not status:
                continue

            filled_qty = status.get("ExecutionQuantity", 0)
            remain_qty = status.get("RemainingQuantity", qty)

            if remain_qty == 0:
                break

        # ----------------------------------------------------
        # 残キャンセル
        # ----------------------------------------------------
        if remain_qty > 0:
            cancel_order(order_id, token)
            logger.warning(f"⏹️ 残数量キャンセル: {symbol} 残={remain_qty}")

        # ----------------------------------------------------
        # 約定ゼロ
        # ----------------------------------------------------
        if filled_qty == 0:
            send_discord_notify(
                f"⏹️ **ENTRYキャンセル**\n"
                f"{symbol} {symbolname}\n"
                f"発注: {qty}株 / 約定: 0株\n"
                f"BestAsk={best_ask}, BestBid={best_bid}"
            )
            return False

        # ----------------------------------------------------
        # DB登録
        # ----------------------------------------------------
        session = Session_position()
        try:
            hold_id = f"{symbol}_{datetime.now():%Y%m%d%H%M%S}"

            pos = Position(
                symbol=symbol,
                symbolname=symbolname,
                side=side,
                qty=filled_qty,
                avg_price=price,
                invested_amount=filled_qty * price,
                order_id=order_id,
                hold_id=hold_id,
                status="OPEN",
                open_time=datetime.now(),
                reason_entry=reason,
            )

            session.add(pos)
            session.commit()

        except Exception as e:
            session.rollback()
            logger.error(f"❌ Position保存失敗: {symbol} {e}", exc_info=True)
            return False
        finally:
            session.close()

        # ----------------------------------------------------
        # 通知
        # ----------------------------------------------------
        send_discord_notify(
            f"🚨 **ランキング確定ENTRY**\n"
            f"アクション: {action}\n"
            f"銘柄: {symbol} {symbolname}\n"
            f"約定数量: {filled_qty}\n"
            f"価格: {price:.2f}\n"
            f"理由: {reason}\n"
            f"BestAsk={best_ask}({ask_qty}), BestBid={best_bid}({bid_qty})"
        )

        logger.info(
            f"✅ ENTRY成功: {symbol} {filled_qty}@{price:.2f} reason={reason}"
        )
        print(
            f"✅ ENTRY成功: {symbol} {filled_qty}@{price:.2f} {action} reason={reason}",
            flush=True
        )

        return True

    except Exception as e:
        logger.error(
            f"❌ confirm_and_entry 致命エラー: {symbol} {symbolname}: {e}",
            exc_info=True
        )
        return False
