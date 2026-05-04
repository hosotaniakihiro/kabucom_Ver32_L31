# monitor_orders.py
import time
import requests
import logging
import traceback
from datetime import datetime
from configparser import ConfigParser

from token_manager import get_valid_token
from utils_market import is_market_open
from database import Session_position
from database.models import Position, CanceledOrderLog
from cancel_order import cancel_order
from kabu_api.utils import API_URL
from trade_logic import close_credit_position
from kabu_api.get_current_price import get_current_price
from utils.alerts_util import send_discord_notify_embed, get_webhook_url

logger = logging.getLogger(__name__)

# ===== 設定読み込み =====
conf = ConfigParser(interpolation=None)
conf.read("settings.ini", encoding="utf-8")

CHECK_INTERVAL = conf.getint("orders", "check_interval", fallback=5)
TRAILING_STOP_PCT = conf.getfloat("orders", "trailing_stop_pct", fallback=0.02)
CANCEL_AFTER_SEC = 10   # ✅ 未約定をキャンセルする秒数（全銘柄共通）
PROFIT_TRAIL_GAP = 0.003  # 0.3% 戻りで利確

def update_positions_from_orders():
    """注文一覧を取得し、約定反映・未約定キャンセルを行う"""
    token = get_valid_token()
    url = f"{API_URL}/orders"
    headers = {"X-API-KEY": token}

    try:
        res = requests.get(url, headers=headers, timeout=5)
        res.raise_for_status()
        orders = res.json()

        if not isinstance(orders, list):
            logger.warning("[注文監視] ⚠️ 注文一覧がリストではありません")
            return

        session = Session_position()

        for o in orders:
            order_id = o.get("OrderId")
            symbol = o.get("Symbol")
            state = str(o.get("State"))   # 1=発注中, 5=約定
            qty = int(o.get("Qty", 0))
            price = float(o.get("Price", 0) or 0)

            # --- 約定済みの更新 ---
            if state == "5":
                pos = session.query(Position).filter(
                    Position.symbol == symbol,
                    Position.order_id == order_id,
                    Position.status.in_(["OPEN", "CLOSING"])
                ).first()

                if pos:
                    pos.exit_price = price
                    pos.exit_time = datetime.now()
                    pos.status = "CLOSED"
                    if pos.side == "BUY_CREDIT":
                        pos.pnl = (price - pos.avg_price) * pos.qty
                    else:
                        pos.pnl = (pos.avg_price - price) * pos.qty
                    session.commit()

                    # ✅ Discord Embed通知（約定反映）
                    embed = {
                        "title": "✅ 約定反映",
                        "description": "ポジションが約定されました",
                        "color": 0x00FF00,  # 緑
                        "fields": [
                            {"name": "銘柄コード", "value": str(symbol), "inline": True},
                            {"name": "株数", "value": f"{qty}", "inline": True},
                            {"name": "約定価格", "value": f"{price:.2f}円", "inline": True},
                            {"name": "PNL", "value": f"{pos.pnl:+,.0f}円", "inline": True},
                            {"name": "OrderId", "value": order_id, "inline": False},
                        ],
                        "timestamp": datetime.utcnow().isoformat()
                    }
                    send_discord_notify_embed(embed, get_webhook_url())

            # --- 未約定キャンセル（新規エントリーのみ） ---
            elif state == "1":  # 発注中
                cash_margin = str(o.get("CashMargin", ""))  # "3"=信用新規
                if cash_margin != "3":
                    continue  # EXIT注文などは対象外

                order_time = o.get("OrderDateTime")
                if not order_time:
                    continue
                try:
                    dt_order = datetime.fromisoformat(order_time)
                except Exception:
                    continue

                elapsed = (datetime.now() - dt_order).total_seconds()
                if elapsed > CANCEL_AFTER_SEC:
                    resp = cancel_order(order_id, token)
                    if resp:
                        clog = CanceledOrderLog(
                            order_id=order_id,
                            symbol=symbol,
                            qty=qty,
                            price=price,
                            reason="未約定キャンセル(新規エントリー)",
                        )
                        session.add(clog)
                        session.commit()

                        # ✅ Discord Embed通知（未約定キャンセル）
                        embed = {
                            "title": "🛑 未約定キャンセル",
                            "description": "新規エントリー注文がキャンセルされました",
                            "color": 0xFF0000,  # 赤
                            "fields": [
                                {"name": "銘柄コード", "value": str(symbol), "inline": True},
                                {"name": "株数", "value": f"{qty}", "inline": True},
                                {"name": "価格", "value": f"{price:.2f}円", "inline": True},
                                {"name": "OrderId", "value": order_id, "inline": False},
                                {"name": "経過時間", "value": f"{elapsed:.0f}秒", "inline": True},
                            ],
                            "timestamp": datetime.utcnow().isoformat()
                        }
                        send_discord_notify_embed(embed, get_webhook_url())

        session.close()

    except Exception as e:
        logger.error(f"[注文監視] ❌ エラー: {e}", exc_info=True)
        traceback.print_exc()

def monitor_orders():
    """定期的に注文監視を行うメインループ"""
    print(f"🔍 注文・約定監視を開始（{CHECK_INTERVAL}秒ごと）...")

    try:
        while True:
            if is_market_open():
                # 注文一覧を確認（約定更新・未約定キャンセル）
                update_positions_from_orders()

                # 含み損カット（-0.3%以下で即EXIT）
                check_loss_cut_positions()

                # 含み益トレーリング（最高益から0.3%戻りで利確EXIT）
                check_profit_trailing_positions()

            else:
                print("⏸ 市場クローズ中")

            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        print("⛔️ 監視終了")

def update_trailing_stops(token=None, drop_pct: float = None):
    """保有ポジションに対してトレーリングストップを更新"""
    drop_pct = drop_pct if drop_pct is not None else TRAILING_STOP_PCT
    session = Session_position()
    try:
        positions = session.query(Position).filter(Position.status == "OPEN").all()
        for pos in positions:
            latest_price = get_current_price(pos.symbol, token=token, use_api=False)
            if not latest_price:
                continue

            # 初期値設定
            if not hasattr(pos, "high_price") or pos.high_price is None:
                pos.high_price = latest_price
                session.commit()
                continue

            # 高値更新
            if latest_price > pos.high_price:
                pos.high_price = latest_price
                session.commit()

            # トレーリングストップ判定
            if latest_price <= pos.high_price * (1 - drop_pct):
                success = close_credit_position(pos.symbol, pos.qty, price=latest_price)
                if success:
                    # ✅ Discord Embed通知（トレーリングSTOP EXIT）
                    embed = {
                        "title": "🔻 トレーリングストップ発動",
                        "description": "保有ポジションがトレーリングストップによりEXITしました",
                        "color": 0xFFA500,  # オレンジ
                        "fields": [
                            {"name": "銘柄コード", "value": str(pos.symbol), "inline": True},
                            {"name": "株数", "value": f"{pos.qty}", "inline": True},
                            {"name": "EXIT価格", "value": f"{latest_price:.2f}円", "inline": True},
                            {"name": "最高値", "value": f"{pos.high_price:.2f}円", "inline": True},
                            {"name": "閾値", "value": f"-{drop_pct*100:.1f}%", "inline": True},
                        ],
                        "timestamp": datetime.utcnow().isoformat()
                    }
                    send_discord_notify_embed(embed, get_webhook_url())
    except Exception as e:
        logger.error(f"❌ update_trailing_stops エラー: {e}", exc_info=True)
    finally:
        session.close()

def check_profit_trailing_positions(token=None):
    """含み益の最高値から一定割合戻ったら利確EXIT"""
    session = Session_position()
    try:
        positions = session.query(Position).filter(Position.status == "OPEN").all()
        for pos in positions:
            latest_price = get_current_price(pos.symbol, token=token, use_api=False)
            if not latest_price or pos.avg_price <= 0:
                continue

            # 損益率を算出
            pnl_ratio = (latest_price - pos.avg_price) / pos.avg_price
            if pos.side == "SELL_CREDIT":
                pnl_ratio *= -1  # 売りポジションは逆符号にする

            # === 含み益最高値の記録 ===
            if not hasattr(pos, "max_profit_ratio") or pos.max_profit_ratio is None:
                pos.max_profit_ratio = pnl_ratio
            else:
                if pnl_ratio > pos.max_profit_ratio:
                    pos.max_profit_ratio = pnl_ratio

            session.commit()

            # === 利確判定 ===
            if pos.max_profit_ratio > 0 and pnl_ratio <= pos.max_profit_ratio - PROFIT_TRAIL_GAP:
                success = close_credit_position(pos.symbol, pos.qty, price=latest_price)
                if success:
                    embed = {
                        "title": "💰 利確EXIT",
                        "description": "含み益が最高値から戻ったため利確しました",
                        "color": 0x00BFFF,  # 青
                        "fields": [
                            {"name": "銘柄コード", "value": str(pos.symbol), "inline": True},
                            {"name": "建値", "value": f"{pos.avg_price:.2f}円", "inline": True},
                            {"name": "現在値", "value": f"{latest_price:.2f}円", "inline": True},
                            {"name": "最高益率", "value": f"{pos.max_profit_ratio*100:.2f}%", "inline": True},
                            {"name": "現在損益率", "value": f"{pnl_ratio*100:.2f}%", "inline": True},
                            {"name": "数量", "value": str(pos.qty), "inline": True},
                        ],
                        "timestamp": datetime.utcnow().isoformat()
                    }
                    send_discord_notify_embed(embed, get_webhook_url())
    except Exception as e:
        logger.error(f"❌ check_profit_trailing_positions エラー: {e}", exc_info=True)
    finally:
        session.close()
