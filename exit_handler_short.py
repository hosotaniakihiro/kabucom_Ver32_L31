# exit_handler_short.py

from datetime import datetime
from database import SummarySession, Position
from should_exit_short_position import should_exit_short_position
from kabu_api import send_repayment_order  # 空売り返済の発注関数
from utils.alerts_util import send_discord_notify
from exit_handler import get_open_positions, mark_position_closed


def check_and_execute_short_exit(df_summary):
    """
    空売りポジションをチェックし、条件に該当すれば買い戻し（返済）注文を実行する。
    """
    session = SummarySession()
    positions = session.query(Position).filter_by(status='OPEN', side='SELL').all()

    for pos in positions:
        df_pos = df_summary[df_summary['symbol'] == pos.symbol]
        if df_pos.empty:
            continue

        lowest_price = df_pos['low_price'].min()
        should_exit, reasons = should_exit_short_position(pos.symbol, df_pos, pos.entry_price, lowest_price)

        if should_exit:
            print(f"📤 空売り返済シグナル: {pos.symbol} 理由: {reasons}")
            success, message = send_repayment_order(pos.symbol, pos.shares)

            if success:
                pos.exit_time = datetime.now()
                pos.exit_price = df_pos.iloc[-1]['close_price']
                pos.pnl = (pos.entry_price - pos.exit_price) * pos.shares  # 空売りなので逆
                pos.status = 'CLOSED'
                session.commit()

                notify_msg = f"💸 空売り返済完了: {pos.symbol}\n理由: {' / '.join(reasons)}\nPNL: {pos.pnl:.2f}"
                send_discord_notify(notify_msg)
            else:
                print(f"❌ 空売り返済失敗: {message}")

    session.close()
