# kabu_api/get_best_quotes.py（Ver14.2 安定発注対応）
# =========================================================
import requests
import logging
import time
from token_manager import API_URL, get_valid_token

logger = logging.getLogger(__name__)

# --- 前回成功した市場をキャッシュ（銘柄単位） ---
_last_success_exchange = {}

def get_best_quotes(symbol: str, retries: int = 3, wait: float = 2.0):
    """
    板情報を取得（kabuステーション /board/{symbol}@{exchange} 対応）
    - ATS登録済み銘柄のみ取得対象
    - 板データ未更新 or APIエラー時は指数バックオフ付きで再試行
    - 正常時は dict 形式（AskPrice, BidPrice, exchange を含む）を返す
    """
    try:
        # =========================================================
        # 🔹 ATS未登録銘柄はスキップ
        # =========================================================
        try:
            from global_state import global_data
            registered = getattr(global_data, "registered_symbols", set())
            if registered and symbol not in registered:
                logger.debug(f"⏩ {symbol} はATS未登録のため板取得スキップ")
                return None
        except Exception:
            pass

        # =========================================================
        # 🔹 トークン / 設定
        # =========================================================
        token = get_valid_token()
        headers = {"Content-Type": "application/json", "X-API-KEY": token}
        exchanges = [1, 3, 5]  # 東証プライム / スタンダード / グロース

        # 直近成功した市場を優先
        if symbol in _last_success_exchange:
            exchanges = [_last_success_exchange[symbol]] + [x for x in exchanges if x != _last_success_exchange[symbol]]

        # =========================================================
        # 🔹 取得リトライループ
        # =========================================================
        for attempt in range(1, retries + 1):
            for exch in exchanges:
                url = f"{API_URL}/board/{symbol}@{exch}"

                try:
                    res = requests.get(url, headers=headers, timeout=3)
                    status = res.status_code

                    if status == 429:
                        logger.debug(f"⚠️ {symbol}@{exch} API制限(429) → {wait * attempt:.1f}s待機")
                        time.sleep(wait * attempt)
                        continue
                    elif status != 200:
                        logger.debug(f"[{symbol}@{exch}] HTTP {status}: {res.text[:80]}")
                        continue

                    data = res.json()
                    if not data or "Symbol" not in data:
                        continue

                    ask = data.get("AskPrice")
                    bid = data.get("BidPrice")

                    # --- 数値変換＆妥当性チェック ---
                    try:
                        ask = float(ask) if ask is not None else None
                        bid = float(bid) if bid is not None else None
                    except ValueError:
                        ask, bid = None, None

                    if (ask is None or ask <= 0) and (bid is None or bid <= 0):
                        logger.debug(f"⚠️ {symbol}@{exch} 板データ未更新 (Ask={ask}, Bid={bid})")
                        time.sleep(wait)
                        continue

                    # --- 正常終了 ---
                    exch_name = {1: "東証プライム", 3: "東証スタンダード", 5: "東証グロース"}.get(exch, "不明市場")
                    logger.info(f"✅ 板情報取得成功: {symbol} ({exch_name}) Ask={ask} Bid={bid}")
                    data["exchange"] = exch
                    _last_success_exchange[symbol] = exch
                    return data

                except requests.exceptions.RequestException as e:
                    logger.warning(f"⚠️ {symbol}@{exch} 通信例外: {e}")
                    time.sleep(wait * attempt)
                    continue

            # --- リトライインターバル ---
            time.sleep(wait * attempt)

        logger.warning(f"⏩ {symbol} 板情報取得失敗（全市場×{retries}回リトライ後）")
        return None

    except Exception as e:
        logger.error(f"❌ get_best_quotes 例外: {e}", exc_info=True)
        return None
