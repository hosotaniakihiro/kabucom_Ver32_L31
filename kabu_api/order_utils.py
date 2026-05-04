import json
import logging
import time
import requests
from configparser import ConfigParser
from kabu_api.utils import API_URL

logger = logging.getLogger(__name__)

# === 設定読込 ===
conf = ConfigParser()
conf.read("settings.ini", encoding="utf-8")
Password = conf.get("aukabu", "password", fallback=None)
APIPassword = conf.get("aukabu", "password", fallback=None)


# =========================================================
# 発注ペイロード構築
# =========================================================
def build_order_payload(
    *,
    symbol: str,
    exchange: int = 1,
    side: int,
    qty: int,
    price: float | int | None = None,
    order_type: str = "limit",  # ← デフォルト指値
    cash_margin: int = 2,       # 0=現物, 2=信用新規
    margin_trade_type: int = 0, # 0=制度信用, 1=一般信用
    fund_type: str = "11",      # 11=信用預り, 02=現物
    account_type: int = 4,
    expire_day: int = 1,
) -> dict | None:
    """
    kabuステーションAPI用の注文ペイロードを構築（指値専用）
    """
    try:
        if not Password:
            logger.error("⚠️ [警告] AAAkabuステーションAPI Password が未設定です。settings.ini または環境変数で指定してください。")
            return None

        # --- Symbol正規化 ---
        symbol_code = str(symbol).strip()
        if symbol_code.isdigit():
            symbol_code = str(int(symbol_code))  # 0186 → 186
        # A/B/C付きコード（信用識別）はそのまま保持

        # --- 指値専用設定 ---
        front_order_type = 20  # 20=指値
        order_price = float(price) if price and price > 0 else 1.0  # priceが0/Noneなら1円を代替

        # --- Payload構築 ---
        payload = {
            "Password": Password,
            "Symbol": symbol_code,
            "Exchange": exchange,
            "SecurityType": 1,            # 株式固定
            "Side": side,                 # 1=買, 2=売
            "CashMargin": cash_margin,    # 0=現物, 2=信用新規
            "MarginTradeType": margin_trade_type,
            "DelivType": 0,
            "FundType": fund_type,
            "AccountType": account_type,
            "Qty": int(qty),
            "Price": order_price,
            "ExpireDay": expire_day,
            "FrontOrderType": front_order_type,
        }

        logger.warning(f"📦 build_order_payload (limit only):\n{json.dumps(payload, ensure_ascii=False, indent=2)}")
        return payload

    except Exception as e:
        logger.error(f"❌ build_order_payload エラー: {symbol} - {e}", exc_info=True)
        return None




# =========================================================
# 発注送信（リトライ付き）
# =========================================================
def send_order_request(payload: dict | None, token: str) -> str | None:
    """
    kabuステーションAPIに注文を送信。
    - payload が None の場合はスキップ
    - 429 はリトライ
    - 400/500 はエラーログ出力
    """
    if payload is None:
        logger.error("❌ send_order_request 呼び出し時に payload=None のため発注スキップ")
        return None

    url = f"{API_URL}/sendorder"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}
    max_retries = 3

    for attempt in range(max_retries):
        try:
            response = requests.post(url, headers=headers, data=json.dumps(payload))
            status = response.status_code

            # --- 429 Too Many Requests ---
            if status == 429:
                wait_sec = 1.0 + attempt * 0.5
                logger.warning(f"⚠️ 発注リクエスト過多 (429) → {wait_sec:.1f}秒待機して再試行 ({attempt+1}/{max_retries})")
                time.sleep(wait_sec)
                continue

            # --- 400 or 500 ---
            if status >= 400:
                logger.error(f"❌ HTTPエラー: {status} for {payload.get('Symbol', '?')}")
                logger.error(f"⚙️ 送信Payload:\n{json.dumps(payload, ensure_ascii=False, indent=2)}")
                try:
                    logger.error(f"💬 レスポンス本文: {response.text}")
                except Exception:
                    pass
                time.sleep(0.8)
                continue

            # --- 正常系 ---
            data = response.json()
            if "OrderId" in data:
                logger.info(
                    f"✅ 注文成功: {payload['Symbol']} ({'買' if payload['Side']==1 else '売'}) "
                    f"{payload['Qty']}株 @ {payload['Price']}円"
                )
                return data["OrderId"]

            logger.error(f"❌ 注文失敗レスポンス: {data}")
            return None

        except Exception as e:
            logger.error(f"❌ send_order_request 例外: {payload.get('Symbol', '?')} - {e}", exc_info=True)
            logger.error(f"⚙️ 送信Payload:\n{json.dumps(payload, ensure_ascii=False, indent=2)}")
            time.sleep(1.0)

    logger.error(f"❌ 発注失敗（全リトライ不成立）: {payload.get('Symbol', '?')}")
    return None


def get_latest_bid_ask(symbol: str) -> tuple[float | None, float | None]:
    """
    当日の pushYYYYMMDD.db から指定銘柄の最新 Bid/Ask を取得
    - return: (bid_price, ask_price)
    """
    try:
        today = dt.datetime.now().strftime("%Y%m%d")
        db_path = f"y:/Stock_price_data/push{today}.db"
        conn = sqlite3.connect(db_path)

        query = f"""
        SELECT BidPrice, AskPrice
        FROM stream_data
        WHERE Symbol = '{symbol}'
        ORDER BY time DESC
        LIMIT 1
        """
        df = pd.read_sql_query(query, conn)
        conn.close()

        if df.empty:
            return None, None
        bid, ask = df.iloc[0]["BidPrice"], df.iloc[0]["AskPrice"]
        bid = float(bid) if pd.notna(bid) else None
        ask = float(ask) if pd.notna(ask) else None
        return bid, ask

    except Exception as e:
        print(f"⚠️ get_latest_bid_ask 失敗: {symbol} - {e}")
        return None, None