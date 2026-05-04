# kabus_position_sync.py
# ────────────────────────────────────────────────
# kabuステーションAPIからポジションを取得し、
# SQLiteの positions テーブルと同期させるモジュール
# ────────────────────────────────────────────────

import urllib.request
import urllib.parse
import json
import logging
import datetime as dt
import pandas as pd

from database import Session_position
from database.models import Position   # Positionモデル
from database.crud import sync_positions_with_api  # DB同期用
from token_manager import get_valid_token   # ✅ トークンをここから取得
# === 同期ロジック ===


logger = logging.getLogger(__name__)



# === API URL ===
API_URL = "http://localhost:18080/kabusapi/positions"

# pandas 表示設定
pd.set_option("display.max_rows", None)
pd.set_option("display.max_columns", None)


# === API呼び出し ===
def fetch_positions_from_api(product: int = 0, symbol: str | None = None, side: str | None = None) -> list[dict]:
    """
    kabuステーションAPIから現在の保有ポジション一覧を取得
    - product: 0=すべて, 1=現物, 2=信用, ...
    - symbol:  銘柄コード（省略可）
    - side:    1=売, 2=買（省略可）
    """
    token = get_valid_token()  # ✅ 有効なトークンを取得（無効なら自動再発行）

    params = {"product": product, "addinfo": "false"}
    if symbol:
        params["symbol"] = symbol
    if side:
        params["side"] = side

    req = urllib.request.Request(f"{API_URL}?{urllib.parse.urlencode(params)}", method="GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-KEY", token)

    try:
        with urllib.request.urlopen(req) as res:
            if res.status != 200:
                logger.error(f"❌ APIステータス異常: {res.status} {res.reason}")
                return []
            content = json.loads(res.read())
            return content
    except Exception as e:
        logger.error(f"❌ ポジションAPI取得エラー: {e}")
        return []



import logging
import datetime as dt
from database import Session_position, Position
from kabu_api import get_positions  # ← APIからポジション取得する関数を想定

logger = logging.getLogger(__name__)

def sync_positions_job():
    """
    kabu.com API 側のポジションと SQLite の positions テーブルを同期するジョブ
    Excel は使用しない
    """
    session = Session_position()
    now = dt.datetime.now()

    try:
        # API側のポジション取得
        api_positions = get_positions() or []

        # DB側のポジション取得
        db_positions = {p.symbol: p for p in session.query(Position).filter(Position.status=="OPEN").all()}

        for p in api_positions:
            symbol = str(p.get("Symbol"))
            qty = int(p.get("LeavesQty", 0))
            price = float(p.get("Price", 0.0))
            side = p.get("Side", "")

            if symbol in db_positions:
                # 既存ポジションを更新
                pos = db_positions[symbol]
                pos.qty = qty
                pos.avg_price = price
                pos.side = side
                pos.updated_at = now
                logger.info(f"🔄 更新: {symbol} qty={qty} price={price}")
            else:
                # 新規ポジションを追加
                new_pos = Position(
                    symbol=symbol,
                    symbolname=p.get("SymbolName") or "不明",
                    qty=qty,
                    avg_price=price,
                    invested_amount=price * qty if price and qty else 0.0,  # ← 追加
                    side=side,
                    status="OPEN",
                    open_time=now
                )
                session.add(new_pos)

                logger.info(f"🆕 追加: {symbol} qty={qty} price={price}")

        session.commit()
        logger.info("✅ ポジション同期完了")

    except Exception as e:
        session.rollback()
        logger.error(f"❌ ポジション同期エラー: {e}", exc_info=True)
    finally:
        session.close()





# === 手動テスト用 ===
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sync_positions_job()
