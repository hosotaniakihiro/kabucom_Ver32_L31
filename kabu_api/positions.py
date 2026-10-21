# ============================================================
# kabu_api/positions.py（Ver25-SHARED-POSITIONS-READ-CACHE）
# ------------------------------------------------------------
# ✔ /positions API 安定取得（共有 singleflight + cache + 429ガード）
# ✔ 2026-09-01: 実 GET を kabu_api.positions_read_cache へ集約。
#   旧実装は timeout 10 秒 x 3 回リトライで 1 呼び出し最大 31 秒ブロックし、
#   同じプロセスの kabu_position_reader (product=2) と二重に /positions を
#   叩いていた。timeout / TTL / backoff の数値は kabu_api.rest_limits が持つ。
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


def _classify_positions_error(err: str) -> str:
    """error 文字列の接頭辞から失敗の種類を 1 箇所で決める。

    "auth" が一度立つと以後の timeout まで auth に見える、といった
    取り違えを避けるため、毎回 error からだけ決める。
    """
    e = str(err or "")
    for kind in ("auth", "rate_limited", "http", "network", "bad_response", "token_missing", "unexpected"):
        if e.startswith(kind + ":"):
            return kind
    if "backoff" in e:
        # バックオフ中。理由は直前の失敗が握っている。
        for kind in ("auth", "rate_limited", "http", "network", "bad_response", "token_missing"):
            if (kind + ":") in e:
                return kind
        return "network"
    return "unexpected"


def _get_positions_http_once():
    """/positions を 1 回だけ GET する。(payload, ok, error) を返す。

    リトライしない。連続失敗時の待ちは kabu_api.positions_read_cache が
    指数バックオフで行う。
    旧実装は timeout 10 秒 x 3 回 + sleep で 1 呼び出し最大 31 秒ブロックし、
    その待ち時間そのものが kabu Station の同時接続を占有していた
    (2026-09-01 12:53-12:55 のログで実測)。
    """
    global _POS_FETCH_HEALTHY, _POS_LAST_FAILURE

    # 起動直後は global_data がまだ compat オブジェクトで、
    # token_value 属性そのものが生えていないことがある
    # (2026-09-02 09:04:32 実測:
    #  AttributeError: 'GlobalDataCompat' has no attribute 'token_value')。
    # これは「トークンがまだ配られていない」だけの一過性なので、
    # unexpected 扱いにして /positions をバックオフさせない。
    token = getattr(global_data, "token_value", None)
    if not token:
        logger.warning("⚠ get_positions: token 不在 → skip")
        return None, False, "token_missing:token unavailable"

    from kabu_api import rest_limits

    url = f"{API_URL}/positions"
    headers = {
        "Content-Type": "application/json",
        "X-API-KEY": token,
    }

    try:
        res = requests.get(
            url,
            headers=headers,
            timeout=rest_limits.policy("positions").timeout_sec,
        )
    except requests.exceptions.ReadTimeout:
        return None, False, "network:ReadTimeout"
    except requests.exceptions.ConnectionError:
        return None, False, "network:ConnectionError"
    except Exception as e:  # noqa: BLE001
        logger.error("❌ get_positions unexpected error", exc_info=True)
        return None, False, f"unexpected:{type(e).__name__}: {e}"

    # ------------------------------------------------
    # ★ 429 = 正常な抑制信号。token は取り直さない。
    # ------------------------------------------------
    if res.status_code == 429:
        logger.warning("⚠ /positions rate limited (429) → backoff (token refresh はしない)")
        return None, False, "rate_limited:429 Too Many Requests"

    if res.status_code in (401, 403):
        # 認証失敗。これを一般例外に混ぜて traceback で流すと、
        # 呼び出し側は「例外が出なかった＝取得成功」と読み、
        # 建玉ゼロと区別できなくなる。専用の行にして目に見えるようにする。
        logger.error(
            "❌ [POSITIONS AUTH FAILED] /positions が %s を返した。"
            "建玉は取得できていない。戻り値は空であり、建玉ゼロを意味しない。"
            " 判断に使う側は positions_fetch_healthy() を見ること。",
            res.status_code,
        )
        return None, False, f"auth:{res.status_code}"

    if res.status_code >= 400:
        return None, False, f"http:{res.status_code}"

    try:
        positions = res.json()
    except Exception as e:  # noqa: BLE001
        return None, False, f"bad_response:json decode failed: {e}"

    if not isinstance(positions, list):
        logger.error(f"❌ /positions 不正レスポンス: {positions}")
        return None, False, f"bad_response:type={type(positions).__name__}"

    return positions, True, ""


def _get_positions_impl():
    """
    kabuステーション /positions を安全に取得する。

    実 GET は kabu_api.positions_read_cache が 1 本化する
    (kabu_position_reader / PositionStateService と共有:
     singleflight + TTL + 失敗時の指数バックオフ)。
    ・例外は絶対に投げない (契約は従来どおり)
    ・失敗時は直前キャッシュか空リストを返す。
      呼び出し側は positions_fetch_healthy() で区別すること。
    """

    global _POS_CACHE, _POS_CACHE_TIME
    global _POS_FETCH_HEALTHY, _POS_LAST_FAILURE, _POS_LAST_OK_TIME

    from kabu_api import positions_read_cache

    res = positions_read_cache.get_positions_cached(
        _get_positions_http_once,
        key=positions_read_cache.KEY_ALL,
        caller="kabu_api.positions.get_positions",
    )

    if res.ok and isinstance(res.payload, list):
        now = time.time()
        _POS_CACHE = res.payload
        _POS_CACHE_TIME = now
        _POS_FETCH_HEALTHY = True
        _POS_LAST_FAILURE = ""
        _POS_LAST_OK_TIME = now
        return res.payload

    err = str(res.error or "")
    kind = _classify_positions_error(err)

    if kind == "rate_limited":
        # 429 は「呼びすぎ」であって障害ではない。キャッシュがあるなら
        # 古いが本物のデータなので健全のまま。キャッシュが無いときの [] は
        # 「建玉ゼロ」ではなく「まだ一度も取れていない」なので不健全にする。
        if _POS_CACHE is None:
            _POS_FETCH_HEALTHY = False
            _POS_LAST_FAILURE = "rate_limited_no_cache"
    elif kind == "token_missing":
        # token 未設定は起動直後に起こる。従来どおり健全性は変えない。
        pass
    else:
        _POS_FETCH_HEALTHY = False
        _POS_LAST_FAILURE = kind

    logger.warning(
        "⚠ get_positions failed source=%s kind=%s err=%s → use cache "
        "(戻り値の [] は「建玉ゼロ」ではない。positions_fetch_healthy() を見ること)",
        res.source, kind, err[:160],
    )
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

            # 2026-09-17: kabu の ExecutionDateTime は ISO 形式 ("2026-09-17T10:10:16+09:00") で来ることがあり、
            #   strptime が失敗して now() が入っていた (6590 の entry_time が再起動時刻 12:51 に化けた)。
            entry_time = None
            raw_dt = p.get("ExecutionDateTime")
            for parser in (
                lambda v: dt.datetime.strptime(v, "%Y/%m/%d %H:%M:%S"),
                lambda v: dt.datetime.fromisoformat(str(v)).replace(tzinfo=None),
                lambda v: dt.datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S"),
            ):
                try:
                    entry_time = parser(raw_dt)
                    break
                except Exception:
                    continue
            parsed_ok = entry_time is not None
            if entry_time is None:
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
            # 2026-09-17: 決済済み (LeavesQty=0) の行の qty を 0 に潰さない。建玉数量は損益計算に使う。
            #   entry_time も既存の値があればそれを残す (broker 側の時刻が取れた時だけ上書き)。
            if qty > 0:
                pos.qty           = qty
            elif not (pos.qty or 0):
                pos.qty           = qty
            pos.avg_price         = avg_price if avg_price > 0 else pos.avg_price
            pos.side              = side
            if pos.entry_time is None or parsed_ok:
                pos.entry_time    = entry_time
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
