import datetime as dt
import holidays
import logging
import datetime
import threading
import schedule
import time
from datetime import datetime, timedelta
#import ranking  # ranking.main() を使用するため

logger = logging.getLogger(__name__)
from utils_market import is_market_open, last_business_day
from utils_common import calculate_shares, format_float

from config import DB_PATH


def wait_until_9am():
    """
    翌日の午前9時まで待機します。
    """
    now = datetime.now()
    target_time = now.replace(hour=9, minute=0, second=0, microsecond=0)

    if now >= target_time:
        target_time += timedelta(days=1)

    time_to_wait = (target_time - now).total_seconds()
    print(f"9:00まで{time_to_wait}秒待機します。")
    time.sleep(time_to_wait)
    print("9:00になりました。スクリプトを開始します。")


def start_ranking_thread():
    import ranking  # ★ここに移動！
    def run_ranking_safe():
        try:
            print("🔁 ranking.main() スレッド開始")
            ranking.main()
        except Exception as e:
            print(f"❌ rankingスレッド内で例外発生: {e}")
            import traceback
            traceback.print_exc()

    thread = threading.Thread(target=run_ranking_safe, daemon=True)
    thread.start()

    print("✅ ranking.py をスレッドで起動しました。")


def format_price_dynamic(price):
    """株価を見やすく整形する"""
    if price is None:
        return "-"
    try:
        price = float(price)
        if price >= 1000:
            return f"{price:,.0f}"  # カンマ区切り
        else:
            return f"{price:.0f}"
    except:
        return str(price)


def format_volume(volume):
    """出来高をカンマ区切りで整形"""
    if volume is None:
        return "-"
    try:
        return f"{int(volume):,}"
    except:
        return str(volume)
