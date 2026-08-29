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

# ------------------------------------------------------------
# 取得の健全性
# ------------------------------------------------------------
# get_positions() は「例外は絶対に投げない」契約なので、失敗しても
# キャッシュか空リストを返す。呼び出し側から見ると
#     取得に失敗した            -> []
#     本当に建玉がゼロだった    -> []
# が区別できない。実際 trading/exit/executor.py の closing reconcile は
# 例外が出なければ取得成功と扱うため、401 の直後に
#     [EXIT CLOSING RECONCILE] helper positions ok rows=0
# と記録していた（2026-08-29 の 401 障害で確認）。
#
# 戻り値の型を変えると 7 箇所の呼び出し元に影響が出るため、ここでは
# 契約を変えずに「直近の取得が信用できるか」を別途公開する。
# 判断に使いたい呼び出し元は positions_fetch_healthy() を見る。
_POS_FETCH_HEALTHY = True      # 直近の取得が信用できるか
_POS_LAST_FAILURE = ""         # "auth" / "http" / "network" / "bad_response" / ""
_POS_LAST_OK_TIME = 0.0        # 最後に本当に取得できた時刻


# ============================================================
# ① /positions API（retry + cache + 429 safe）
# ============================================================
def _get_positions_core():
    """
    kabuステーション /positions を安全に取得する。

    例外時は長いtracebackを出さず _POS_CACHE へフォールバックする
    (旧 core/startup/push_summary_realtime_patch.py から移設)。
    """
    try:
        return _get_positions_impl()
    except Exception as e:
        logger.warning("[kabu_api.positions] positions read skipped err=%s", e)
        return _POS_CACHE or []


def _get_positions_impl():
    """
    kabuステーション /positions を安全に取得する
    ・5秒キャッシュ
    ・timeout 10秒
    ・最大3回 retry（※429はretryしない）
    ・例外は絶対に投げない
    """

    global _POS_CACHE, _POS_CACHE_TIME
    global _POS_FETCH_HEALTHY, _POS_LAST_FAILURE, _POS_LAST_OK_TIME

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
                # 429 は正常な抑制信号なので失敗扱いにはしない。
                # ただしキャッシュが無いときの [] は「建玉ゼロ」ではなく
                # 「まだ一度も取れていない」なので、そこだけは不健全にする。
                if _POS_CACHE is None:
                    _POS_FETCH_HEALTHY = False
                    _POS_LAST_FAILURE = "rate_limited_no_cache"
                return _POS_CACHE or []

            res.raise_for_status()

            positions = res.json()
            if not isinstance(positions, list):
                logger.error(f"❌ /positions 不正レスポンス: {positions}")
                _POS_FETCH_HEALTHY = False
                _POS_LAST_FAILURE = "bad_response"
                return _POS_CACHE or []

            _POS_CACHE = positions
            _POS_CACHE_TIME = now
            _POS_FETCH_HEALTHY = True
            _POS_LAST_FAILURE = ""
            _POS_LAST_OK_TIME = now
            return positions

        except requests.exceptions.ReadTimeout:
            logger.warning(f"⚠ get_positions timeout retry={i+1}")
            time.sleep(0.5)

        except requests.exceptions.ConnectionError:
            logger.warning(f"⚠ get_positions connection error retry={i+1}")
            time.sleep(0.5)

        except requests.exceptions.HTTPError as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            _POS_FETCH_HEALTHY = False
            if status in (401, 403):
                # 認証失敗。これを一般例外に混ぜて traceback で流すと、
                # 呼び出し側は「例外が出なかった＝取得成功」と読み、
                # 建玉ゼロと区別できなくなる。専用の行にして目に見えるようにする。
                _POS_LAST_FAILURE = "auth"
                logger.error(
                    "❌ [POSITIONS AUTH FAILED] /positions が %s を返した。"
                    "建玉は取得できていない。戻り値は %s であり、建玉ゼロを意味しない。"
                    " 判断に使う側は positions_fetch_healthy() を見ること。",
                    status,
                    "直前のキャッシュ" if _POS_CACHE else "空リスト",
                )
            else:
                _POS_LAST_FAILURE = "http"
                logger.error("❌ get_positions HTTP error status=%s", status, exc_info=True)
            return _POS_CACHE or []

        except Exception:
            _POS_FETCH_HEALTHY = False
            _POS_LAST_FAILURE = "unexpected"
            logger.error("❌ get_positions unexpected error", exc_info=True)
            return _POS_CACHE or []

    _POS_FETCH_HEALTHY = False
    _POS_LAST_FAILURE = "network"
    logger.warning("⚠ get_positions failed after retries → use cache")
    return _POS_CACHE or []


def positions_fetch_healthy() -> bool:
    """直近の /positions 取得が信用できるか。

    False のとき、get_positions() が返した空リストは
    「建玉がゼロ」ではなく「取得できなかった」を意味する。
    建玉の有無で判断を分ける処理は、必ずこれを見ること。
    """
    return bool(_POS_FETCH_HEALTHY)


def positions_health() -> dict:
    """取得の健全性を詳しく返す（ログ・診断用）。"""
    return {
        "healthy": bool(_POS_FETCH_HEALTHY),
        "last_failure": _POS_LAST_FAILURE,
        "last_ok_time": _POS_LAST_OK_TIME,
        "has_cache": _POS_CACHE is not None,
        "cache_rows": len(_POS_CACHE) if isinstance(_POS_CACHE, list) else None,
    }


# ============================================================
# ② Position DB と完全同期（信用建玉のみ）
# ============================================================
def _sync_positions_from_kabus_core():
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


# ============================================================
# KABU API TOKEN RUNTIME
# (旧 core/startup/kabu_api_token_runtime_patch._patch_positions_module)
# ------------------------------------------------------------
# 旧構成:
#   get_positions              -> **完全置換** (token 解決 + 401/429 リトライ +
#                                 キャッシュ退避。素の実装は呼ばない)
#   sync_positions_from_kabus  -> token が取れないときだけ素の実装を呼ばない包み
# 素の実装は *_core として残し、有効時だけ委譲する。
# ============================================================


def _token_runtime():
    try:
        from kabu_api import token_runtime as _tr

        return _tr if _tr.token_runtime_enabled() else None
    except Exception:
        return None


def get_positions():
    _tr = _token_runtime()
    if _tr is None:
        return _get_positions_core()
    return _tr.get_positions_with_token()


def sync_positions_from_kabus():
    _tr = _token_runtime()
    if _tr is None:
        return _sync_positions_from_kabus_core()
    return _tr.sync_positions_with_token(_sync_positions_from_kabus_core)
