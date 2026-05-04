# ============================================================
# kabu_api/positions.py（Ver24-FINAL-REV9-STABLE-429-SAFE）
# ------------------------------------------------------------
# ✔ /positions API 安定取得（retry + cache + 429ガード）
# ✔ 信用建玉のみ同期（現物除外）
# ✔ INSERT / UPDATE / CLOSE 完全ミラー
# ✔ kabuステーション過負荷防止
# ✔ ReadTimeout / 429 完全耐性
# ============================================================

import logging
import requests
import datetime as dt
import time

from global_state import global_data
from database import Session_position
from database.models import Position
from database.models import Position
print("DEBUG Position fields:", Position.__table__.columns.keys())

API_URL = "http://localhost:18080/kabusapi"
logger = logging.getLogger(__name__)

# ============================================================
# positions API キャッシュ
# ============================================================
_POS_CACHE = None
_POS_CACHE_TIME = 0.0
_POS_CACHE_TTL = 5.0   # 秒（429対策の要）


# ============================================================
# ① /positions API（retry + cache + 429 safe）
# ============================================================
def get_positions():
    """
    kabuステーション /positions を安全に取得する
    ・5秒キャッシュ
    ・timeout 10秒
    ・最大3回 retry（※429はretryしない）
    ・例外は絶対に投げない
    """

    global _POS_CACHE, _POS_CACHE_TIME

    now = time.time()

    # --------------------------------------------------------
    # キャッシュ有効
    # --------------------------------------------------------
    if _POS_CACHE is not None and (now - _POS_CACHE_TIME) < _POS_CACHE_TTL:
        return _POS_CACHE

    token = global_data.token_value
    if not token:
        logger.warning("⚠ get_positions: token 不在 → skip")
        return _POS_CACHE or []

    url = f"{API_URL}/positions"
    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": token,
    }

    for i in range(3):
        try:
            res = requests.get(
                url,
                headers=headers,
                timeout=10,
            )

            # ------------------------------------------------
            # ★ 429 = 正常な抑制信号（即スキップ）
            # ------------------------------------------------
            if res.status_code == 429:
                logger.warning("⚠ /positions rate limited (429) → skip this cycle")
                time.sleep(0.5)
                return _POS_CACHE or []

            res.raise_for_status()

            positions = res.json()
            if not isinstance(positions, list):
                logger.error(f"❌ /positions 不正レスポンス: {positions}")
                return _POS_CACHE or []

            _POS_CACHE = positions
            _POS_CACHE_TIME = now
            return positions

        except requests.exceptions.ReadTimeout:
            logger.warning(f"⚠ get_positions timeout retry={i+1}")
            time.sleep(0.5)

        except requests.exceptions.ConnectionError:
            logger.warning(f"⚠ get_positions connection error retry={i+1}")
            time.sleep(0.5)

        except Exception:
            logger.error("❌ get_positions unexpected error", exc_info=True)
            return _POS_CACHE or []

    logger.warning("⚠ get_positions failed after retries → use cache")
    return _POS_CACHE or []


# ============================================================
# ② Position DB と完全同期（信用建玉のみ）
# ============================================================
def sync_positions_from_kabus():
    """
    信用建玉のみを Position DB に完全ミラー同期する
    ※ 429 / API失敗時でもシステムは止めない
    """

    token = global_data.token_value
    if not token:
        logger.warning("⚠ sync_positions: token 不在 → skip")
        return

    api_positions = get_positions()
    if not api_positions:
        logger.debug("ℹ sync_positions: positions empty (or skipped)")
        return

    session = Session_position()

    try:
        db_positions = session.query(Position).all()
        db_map = {p.hold_id: p for p in db_positions if p.hold_id}
        api_seen = set()

        for p in api_positions:

            # ------------------------------
            # 現物は除外
            # ------------------------------
            if p.get("MarginTradeType", 0) == 0:
                continue

            hold_id = p.get("ExecutionID")
            if not hold_id:
                continue

            api_seen.add(hold_id)

            symbol      = p.get("Symbol")
            symbolname  = p.get("SymbolName")
            qty         = int(p.get("LeavesQty") or 0)
            avg_price   = float(p.get("Price") or 0.0)
            side        = "SELL_CREDIT" if str(p.get("Side")) == "1" else "BUY_CREDIT"
            margin_type = int(p.get("MarginTradeType") or 0)
            account     = int(p.get("AccountType") or 0)
            exchange    = int(p.get("Exchange") or 1)

            try:
                entry_time = dt.datetime.strptime(
                    p.get("ExecutionDateTime"), "%Y/%m/%d %H:%M:%S"
                )
            except Exception:
                entry_time = dt.datetime.now()

            # ------------------------------
            # 新規建玉
            # ------------------------------
            if hold_id not in db_map:
                pos = Position(
                    hold_id=hold_id,
                    execution_id=hold_id,
                    symbol=symbol,
                    symbolname=symbolname,
                    side=side,
                    qty=qty,
                    avg_price=avg_price,
                    entry_time=entry_time,
                    status="OPEN" if qty > 0 else "CLOSED",
                    exchange=exchange,
                    margin_trade_type=margin_type,
                    account_type=account,
                    exit_price=None,
                    close_time=None,
                )
                session.add(pos)
                logger.info(f"➕ 新規建玉: {symbol} hold_id={hold_id} qty={qty}")
                continue

            # ------------------------------
            # 既存建玉 UPDATE
            # ------------------------------
            pos = db_map[hold_id]
            pos.qty               = qty
            pos.avg_price         = avg_price
            pos.side              = side
            pos.entry_time        = entry_time
            pos.status            = "OPEN" if qty > 0 else "CLOSED"
            pos.exchange          = exchange
            pos.margin_trade_type = margin_type
            pos.account_type      = account

        # ------------------------------
        # API に無い建玉 → CLOSE
        # ------------------------------
        for hold_id, pos in db_map.items():
            if hold_id not in api_seen and pos.status == "OPEN":
                pos.status     = "CLOSED"
                pos.exit_price = pos.avg_price
                pos.close_time = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                logger.info(f"🧾 自動CLOSE: {pos.symbol} hold_id={hold_id}")

        session.commit()

    except Exception:
        session.rollback()
        logger.error("❌ Position DB 同期失敗", exc_info=True)

    finally:
        session.close()
