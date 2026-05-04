# ============================================================
# auto_cancel_monitor.py（Ver21 / VerH++ 完全安定版）
# ------------------------------------------------------------
# ・/orders を定期監視し、未約定注文を自動キャンセル
# ・成行/指値の未約定を確実にキャンセル（state=1,2）
# ・duplicate cancel 防止
# ・トークン切れも安全に処理
# ============================================================

import logging
import threading
import time
import requests
import datetime as dt

from token_manager import get_valid_token
from config import API_URL

logger = logging.getLogger(__name__)

_cancelled_orders = set()
_cancel_lock = threading.Lock()


# ------------------------------------------------------------
# /orders API 呼び出し
# ------------------------------------------------------------
def fetch_orders(token: str):
    url = f"{API_URL}/orders"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}

    for attempt in range(2):  # リトライ1回
        try:
            res = requests.get(url, headers=headers, timeout=10)
            res.raise_for_status()
            return res.json() or []
        except Exception as e:
            logger.error(f"❌ 注文一覧取得失敗({attempt+1}/2): {e}")
            time.sleep(1)

    return []


# ------------------------------------------------------------
# 注文キャンセル処理
# ------------------------------------------------------------
def cancel_one_order(order_id: str, token: str):
    url = f"{API_URL}/cancelorder"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}
    payload = {"OrderId": order_id}

    try:
        res = requests.post(url, headers=headers, json=payload, timeout=8)
        res.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"❌ キャンセル失敗 OrderID={order_id}: {e}")
        return False


# ------------------------------------------------------------
# 自動キャンセルメイン処理
# ------------------------------------------------------------
def auto_cancel_monitor(interval_sec: int = 30):
    logger.info(f"🌀 auto_cancel_monitor 起動（{interval_sec}秒間隔）")

    while True:
        try:
            token = get_valid_token()
            if not token:
                logger.error("❌ トークン取得失敗 → スキップ")
                continue

            orders = fetch_orders(token)

            for o in orders:
                order_id = o.get("OrderId")
                state = o.get("State")         # 1:受付 / 2:発注中 / 3:完了 / 4:エラー / 5:取消 / 6:約定
                leaves = o.get("LeavesQty")    # 残数量
                order_time = o.get("OrdTime")  # 20250113-145500形式

                if not order_id:
                    continue

                # -------------------------------
                # 重複キャンセル防止
                # -------------------------------
                with _cancel_lock:
                    if order_id in _cancelled_orders:
                        continue

                # -------------------------------
                # 完了・取消・約定は無視
                # -------------------------------
                if state in (3, 4, 5, 6):
                    continue

                # -------------------------------
                # 残数量が 0 → 完了扱い
                # -------------------------------
                if leaves == 0:
                    continue

                # -------------------------------
                # 未約定（state=1 or 2）
                # -------------------------------
                logger.warning(
                    f"⚠ 未約定 → 自動キャンセル: {order_id} "
                    f"(state={state}, leaves={leaves})"
                )

                ok = cancel_one_order(order_id, token)

                if ok:
                    with _cancel_lock:
                        _cancelled_orders.add(order_id)

        except Exception as e:
            logger.error(f"❌ auto_cancel_monitor 例外: {e}", exc_info=True)

        finally:
            time.sleep(interval_sec)


# ------------------------------------------------------------
# スレッド起動
# ------------------------------------------------------------
def start_auto_cancel_monitor(interval_sec: int = 30):
    t = threading.Thread(target=auto_cancel_monitor, args=(interval_sec,), daemon=True)
    t.start()
    logger.info("✅ auto_cancel_monitor スレッド開始済み")
