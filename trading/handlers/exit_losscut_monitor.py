#trading/hander/exit_losscut_monitor.py

import time
import logging
from kabu_api.get_current_price import get_current_price
from OLD20251008.entry_handler import send_repayment_order
from database import Session_position
from database.models import Position, TradeHistory
from utils.alerts_util import send_discord_notify_embed_exit

logger = logging.getLogger(__name__)

LOSSCUT_THRESHOLD = 0.3  # 損切（含み損）閾値 [%]
TRAILING_GAP = 0.3       # トレーリング閾値 [%]
CHECK_INTERVAL_SEC = 10  # チェック間隔（秒）

# 銘柄ごとの最高値追跡
highest_price_dict = {}


def monitor_exit_conditions_loop():
    """
    含み損・トレーリングストップを10秒ごとに監視し、EXITを自動実行する。
    """
    print(f"🔍 EXIT監視ループ開始 (間隔={CHECK_INTERVAL_SEC}s, 損切={LOSSCUT_THRESHOLD}%, トレーリング={TRAILING_GAP}%)")
    session = Session_position()

    while True:
        try:
            open_positions = session.query(Position).filter_by(status="OPEN").all()
            if not open_positions:
                time.sleep(CHECK_INTERVAL_SEC)
                continue

            for pos in open_positions:
                symbol = pos.symbol
                side = pos.side
                avg_price = float(pos.avg_price)
                qty = pos.qty

                # === 現在価格 ===
                cur_price = get_current_price(symbol)
                if not cur_price:
                    logger.warning(f"⚠️ {symbol} 現在値取得失敗 → スキップ")
                    continue

                # === BUY建玉のトレーリング管理 ===
                if side == "BUY_CREDIT":
                    # 最高値を更新
                    prev_high = highest_price_dict.get(symbol, avg_price)
                    if cur_price > prev_high:
                        highest_price_dict[symbol] = cur_price
                        logger.debug(f"📈 {symbol} 最高値更新: {cur_price}")
                        continue  # 更新のみ

                    # 含み損チェック
                    pnl_pct = (cur_price - avg_price) / avg_price * 100
                    if pnl_pct <= -LOSSCUT_THRESHOLD:
                        reason = f"損切り {pnl_pct:.2f}%"
                        execute_exit(session, pos, symbol, side, qty, cur_price, reason)
                        continue

                    # トレーリングストップ発動
                    highest_price = highest_price_dict.get(symbol, avg_price)
                    drop_pct = (cur_price - highest_price) / highest_price * 100
                    if drop_pct <= -TRAILING_GAP:
                        reason = f"トレーリング反落（最高値-{TRAILING_GAP}%）"
                        execute_exit(session, pos, symbol, side, qty, cur_price, reason)
                        continue

                # === SELL建玉（空売り）の場合 ===
                elif side == "SELL_CREDIT":
                    prev_low = highest_price_dict.get(symbol, avg_price)
                    if cur_price < prev_low:
                        highest_price_dict[symbol] = cur_price
                        continue

                    pnl_pct = (avg_price - cur_price) / avg_price * 100
                    if pnl_pct <= -LOSSCUT_THRESHOLD:
                        reason = f"損切り {pnl_pct:.2f}%"
                        execute_exit(session, pos, symbol, side, qty, cur_price, reason)
                        continue

                    lowest_price = highest_price_dict.get(symbol, avg_price)
                    rebound_pct = (lowest_price - cur_price) / lowest_price * 100
                    if rebound_pct <= -TRAILING_GAP:
                        reason = f"トレーリング反発（最安値+{TRAILING_GAP}%）"
                        execute_exit(session, pos, symbol, side, qty, cur_price, reason)
                        continue

            time.sleep(CHECK_INTERVAL_SEC)

        except Exception as e:
            session.rollback()
            logger.error(f"❌ monitor_exit_conditions_loop エラー: {e}", exc_info=True)
            time.sleep(5)

    session.close()


def execute_exit(session, pos, symbol, side, qty, cur_price, reason):
    """
    損切・トレーリング時の共通EXIT処理
    - 信用返済注文を送信
    - Position / TradeHistory 更新
    - Discord通知送信
    """
    try:
        print(f"💥 [EXIT] {symbol} {reason} | 現在値={cur_price} | 数量={qty}", flush=True)
        logger.info(f"[EXIT] {symbol} {reason} | 価格={cur_price}")

        # --- 返済注文（BUY/SELL両対応） ---
        send_repayment_order(symbol, qty, side, pos.hold_id)

        # --- DB更新（Position + TradeHistory登録） ---
        pos.status = "CLOSED"

        session.add(
            TradeHistory(
                symbol=symbol,
                symbolname=pos.symbolname,
                side=side,
                action="EXIT",
                qty=qty,
                price=cur_price,
                reason=reason,
                position_id=pos.id,
            )
        )
        session.commit()

        # --- Discord通知 ---
        send_discord_notify_embed_exit(
            symbol=symbol,
            reason=reason,
            price=cur_price,
            qty=qty,
            pnl=None,
            hold_duration=None,
        )

        logger.info(f"✅ EXIT完了: {symbol} | 理由: {reason} | 価格={cur_price}")
        print(f"✅ EXIT完了: {symbol} 理由={reason}", flush=True)

    except Exception as e:
        session.rollback()
        logger.error(f"❌ execute_exit エラー: {symbol} {e}", exc_info=True)

