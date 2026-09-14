# ============================================================
# File   : trading/position/open_position_sync.py
# Version: V1.2-BROKER-AUTHORITATIVE-GUARD-NO-STALE-DB-RESYNC
# ------------------------------------------------------------
# 【目的】
#   既にエントリー済み/約定待ち/保有中の銘柄を Python 側の共通キャッシュへ同期する。
#
# 【重要修正 V1.2】
#   - broker API が正常に読めている場合、positions.db の古い残骸を再同期しない
#   - open_position_broker_merge_patch が 4324/6752 等の実建玉を正として反映した直後に、
#     旧 sync 参照から 9716 などのDB残骸が global_data.open_positions へ戻る事故を防止
#   - broker authoritative 中は DB scan のログは出しても、global_data/GC への publish は行わない
#
# 【重要修正 V1.1】
#   - status='OPEN' だけではなく、CLOSED/EXITED/CANCELED 以外も監視対象に含める
#   - 「エントリー済み3銘柄」なのに OPEN として1銘柄しか拾えない問題を緩和
#   - active_symbols protected / exit_loop / 二重エントリー防止に使えるようにする
#
# 【方針】
#   - qty > 0 の未クローズ行を読み込む
#   - avg_price が0でも、price があれば entry_price に使う
#   - broker authoritative 中は DB由来建玉を同期しない
# ============================================================

from __future__ import annotations

import copy
import datetime as dt
import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Tuple

from core.global_context.context import global_context as GC
from global_state import global_data

logger = logging.getLogger(__name__)

CLOSED_STATUSES = {
    "CLOSED",
    "CLOSE",
    "EXITED",
    "EXIT",
    "DONE",
    "CANCELED",
    "CANCELLED",
    "REJECTED",
    "FAILED",
    # 2026-09-14: 同ファイルの _osc_cleanup_stale_db_positions が付ける
    #   status。ここに無かったため「未知の status = 監視対象」に倒れ、
    #   決済済みの建玉行を決済側が採用し続け、9984 で 14:48〜15:29 に
    #   74 回 Code 8 (決済指定内容に誤りがあります) を送り続けた。
    "CLOSED_BROKER_NOT_OPEN",
}

OPEN_LIKE_STATUSES = {
    "OPEN",
    "HOLD",
    "HOLDING",
    "ENTRY",
    "ENTERED",
    "FILLED",
    "PARTIAL",
    "PARTIALLY_FILLED",
    "ORDERED",
    "PENDING",
    "PENDING_ENTRY",
    "ENTRY_SENT",
    "LIVE",
    "ACTIVE",
    "NEW",
    "",
}


def _normalize_symbol(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _normalize_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "LONG", "2", "信用買", "現物買", "BUY_CREDIT"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "信用売", "現物売", "SELL_CREDIT"}:
        return "SELL"
    return s


def _normalize_status(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _is_open_like_status(v: Any) -> bool:
    st = _normalize_status(v)
    if st in CLOSED_STATUSES or st.startswith("CLOSED"):
        return False
    if st in OPEN_LIKE_STATUSES:
        return True
    # 未知のstatusは、qtyありなら監視側に倒す。
    return True


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


# ============================================================
# P0-2: DB read failure != positions empty
# ------------------------------------------------------------
# load_open_positions_from_db() は互換のため空 dict を返し続けるが、
# 「読めなかった」ことをここに記録する。呼び出し元は db_read_ok() で
# 空と失敗を区別し、ENTRY 判定は必ず fail-closed 側へ倒す。
# ============================================================

_DB_READ_LOCK = threading.Lock()
_DB_READ_OK: bool = False
_DB_READ_ERROR: str = ""
_DB_READ_AT = None
_DB_READ_OK_AT = None


def _mark_db_read_ok() -> None:
    global _DB_READ_OK, _DB_READ_ERROR, _DB_READ_AT, _DB_READ_OK_AT
    with _DB_READ_LOCK:
        _DB_READ_OK = True
        _DB_READ_ERROR = ""
        _DB_READ_AT = dt.datetime.now()
        _DB_READ_OK_AT = _DB_READ_AT


def _mark_db_read_failed(err: str) -> None:
    global _DB_READ_OK, _DB_READ_ERROR, _DB_READ_AT
    with _DB_READ_LOCK:
        _DB_READ_OK = False
        _DB_READ_ERROR = str(err or "")
        _DB_READ_AT = dt.datetime.now()


def db_read_ok() -> bool:
    """直近の positions.db 読み取りが成功したか。"""
    with _DB_READ_LOCK:
        return bool(_DB_READ_OK)


def db_read_status() -> dict:
    with _DB_READ_LOCK:
        age = None
        if _DB_READ_OK_AT is not None:
            age = (dt.datetime.now() - _DB_READ_OK_AT).total_seconds()
        return {
            "ok": bool(_DB_READ_OK),
            "error": _DB_READ_ERROR,
            "read_at": _DB_READ_AT,
            "last_ok_at": _DB_READ_OK_AT,
            "last_ok_age_sec": age,
        }


def _ensure_global_open_positions() -> Dict[str, Dict[str, Any]]:
    try:
        positions = getattr(global_data, "open_positions", None)
        if isinstance(positions, dict):
            return positions
        positions = {}
        setattr(global_data, "open_positions", positions)
        return positions
    except Exception:
        logger.debug("[OPEN POSITION SYNC] ensure global_data.open_positions failed", exc_info=True)
        return {}


def _current_global_positions_copy() -> Dict[str, Dict[str, Any]]:
    try:
        positions = getattr(global_data, "open_positions", None)
        if isinstance(positions, dict):
            return dict(positions)
    except Exception:
        pass
    return {}


def _broker_authoritative_active() -> bool:
    """
    broker API が正常に読めている場合は、positions.db の古いOPEN残骸を再投入しない。

    open_position_broker_merge_patch 側が global_data.open_positions_source_mode / broker_read_ok を設定する。
    この関数は、古い関数参照から sync_open_positions_from_db が直接呼ばれても、DB残骸を publish しないための安全弁。
    """
    try:
        read_ok = bool(getattr(global_data, "open_positions_broker_read_ok", False))
        mode = str(getattr(global_data, "open_positions_source_mode", "") or "")
        if read_ok and mode.startswith("broker_credit_authoritative"):
            return True
    except Exception:
        pass
    return False


def _position_model_to_dict(p: Any) -> Dict[str, Any]:
    symbol = _normalize_symbol(getattr(p, "symbol", ""))
    side = _normalize_side(getattr(p, "side", ""))
    raw_status = _normalize_status(getattr(p, "status", "OPEN")) or "OPEN"

    entry_time = getattr(p, "entry_time", None) or getattr(p, "created_at", None)
    if entry_time is None:
        entry_time = dt.datetime.now()

    avg_price = _safe_float(getattr(p, "avg_price", 0.0), 0.0)
    price = _safe_float(getattr(p, "price", 0.0), 0.0)
    entry_price = avg_price if avg_price > 0 else price

    qty = _safe_int(getattr(p, "qty", 0), 0)

    return {
        "id": getattr(p, "id", None),
        "symbol": symbol,
        "symbolname": getattr(p, "symbolname", None),
        "side": side,
        "qty": qty,
        "quantity": qty,
        "avg_price": avg_price,
        "entry_price": entry_price,
        "price": price,
        "entry_time": entry_time,
        "created_at": getattr(p, "created_at", None),
        "updated_at": getattr(p, "updated_at", None),
        "status": raw_status,
        "exchange": getattr(p, "exchange", None) or 1,
        "margin_trade_type": getattr(p, "margin_trade_type", None),
        "account_type": getattr(p, "account_type", None),
        "hold_id": getattr(p, "hold_id", None),
        "execution_id": getattr(p, "execution_id", None),
        "_position_source": "DB.positions.non_closed",
    }


class PositionsDbUnavailable(RuntimeError):
    """positions.db が読めなかった。「建玉 0 件」とは決して同義ではない。"""


# ============================================================
# positions.db 読み取りの短期キャッシュ (2026-09-01)
# ------------------------------------------------------------
# 実測: 2026-09-01 12:53:05-12:55:37 の 2 分半で positions.db の
#       open 回数が約 153 増えていた (接続漏れは無し / outstanding=0)。
#
# 内訳は「同じ 1 回転の中で何度も読んでいた」こと:
#   exit_loop_5s 1 回転につき
#     _sync_db_open_positions_safe()          -> load_open_positions_from_db
#     PositionStateService._refresh()         -> load_open_positions_from_db_strict
#     forced_position_check / entry precheck  -> さらに数回
#   さらに schedule の二重駆動で exit_loop_5s 自体が 2 本走っていた。
#
# engine は NullPool なので 1 クエリ = NAS(SMB) への新規接続 1 本。
# NAS 上の SQLite で長時間接続を持ち回すのは危険なので、
# 接続プールを変えるのではなく「短時間の重複読み取りをまとめる」。
#
# TTL は kabu_api.rest_limits の positions ポリシーと同じ値を使う
# (数値をここに書かない)。TTL 内は同じスナップショットを返す。
# 失敗はキャッシュしない (エラーを配り回さない)。
# ============================================================

_DB_READ_CACHE_LOCK = threading.RLock()
_DB_READ_CACHE: Dict[str, Any] = {"value": None, "at": 0.0}
_DB_READ_STATS = {"reads": 0, "cache_hits": 0}


def _db_read_cache_ttl() -> float:
    try:
        from kabu_api import rest_limits

        return max(0.0, float(rest_limits.policy("positions").cache_ttl_sec))
    except Exception:
        return 1.0


def _db_read_cache_get() -> Optional[Dict[str, Dict[str, Any]]]:
    ttl = _db_read_cache_ttl()
    if ttl <= 0:
        return None
    with _DB_READ_CACHE_LOCK:
        value = _DB_READ_CACHE.get("value")
        if value is None:
            return None
        if (time.monotonic() - float(_DB_READ_CACHE.get("at") or 0.0)) >= ttl:
            return None
        _DB_READ_STATS["cache_hits"] += 1
        return {k: dict(v) for k, v in value.items()}


def _db_read_cache_put(value: Dict[str, Dict[str, Any]]) -> None:
    with _DB_READ_CACHE_LOCK:
        _DB_READ_CACHE["value"] = {k: dict(v) for k, v in (value or {}).items()}
        _DB_READ_CACHE["at"] = time.monotonic()
        _DB_READ_STATS["reads"] += 1


def invalidate_db_read_cache(*, reason: str = "") -> None:
    """建玉が変わった直後 (約定 / 返済 / 手動同期) に呼ぶ。"""
    with _DB_READ_CACHE_LOCK:
        _DB_READ_CACHE["value"] = None
        _DB_READ_CACHE["at"] = 0.0
    if reason:
        logger.debug("[OPEN POSITION SYNC] db read cache invalidated reason=%s", reason)


def db_read_cache_stats() -> dict:
    with _DB_READ_CACHE_LOCK:
        return {
            "db_reads": _DB_READ_STATS["reads"],
            "cache_hits": _DB_READ_STATS["cache_hits"],
            "ttl_sec": _db_read_cache_ttl(),
            "has_value": _DB_READ_CACHE.get("value") is not None,
        }


def reset_db_read_cache_for_test() -> None:
    with _DB_READ_CACHE_LOCK:
        _DB_READ_CACHE["value"] = None
        _DB_READ_CACHE["at"] = 0.0
        _DB_READ_STATS["reads"] = 0
        _DB_READ_STATS["cache_hits"] = 0


def load_open_positions_from_db_strict() -> Dict[str, Dict[str, Any]]:
    """positions テーブルから未クローズ建玉を読む。失敗は例外で伝える。

    P0-2: 失敗を握り潰して {} を返してはならない。
    DB read failure と positions empty は別物であり、
    混同すると
      - ENTRY が fail-open して二重建玉になる
      - EXIT が「建玉なし」と判断して損切りに到達しない
    という取り返しのつかない結果になる。

    握り潰し版が要る呼び出し元は load_open_positions_from_db() を使うこと
    (そちらも空 dict ではなく last-known を壊さない設計にしてある)。

    TTL 内の重複読み取りは同じスナップショットを返す
    (NAS 上の positions.db を 1 回転で何本も開かないため)。
    失敗はキャッシュしないので、障害は必ず例外として伝わる。
    """
    cached = _db_read_cache_get()
    if cached is not None:
        return cached

    try:
        from database import Session_position
        from database.models import Position
    except Exception as e:  # noqa: BLE001
        raise PositionsDbUnavailable(f"import DB modules failed: {e}") from e

    session = None
    try:
        session = Session_position()
        rows = session.query(Position).all()

        out: Dict[str, Dict[str, Any]] = {}
        skipped_closed = 0
        skipped_qty = 0
        skipped_price = 0

        for p in rows or []:
            symbol = _normalize_symbol(getattr(p, "symbol", ""))
            if not symbol:
                continue

            status = _normalize_status(getattr(p, "status", ""))
            if not _is_open_like_status(status):
                skipped_closed += 1
                continue

            d = _position_model_to_dict(p)

            if d.get("qty", 0) <= 0:
                skipped_qty += 1
                continue

            if d.get("entry_price", 0.0) <= 0:
                skipped_price += 1
                continue

            out[symbol] = d

        logger.debug(
            "[OPEN POSITION SYNC] strict DB scan rows=%d open_like=%d "
            "skipped_closed=%d skipped_qty=%d skipped_price=%d symbols=%s",
            len(rows or []), len(out), skipped_closed, skipped_qty, skipped_price,
            sorted(out.keys()),
        )
        _db_read_cache_put(out)
        return out

    except PositionsDbUnavailable:
        raise
    except Exception as e:  # noqa: BLE001
        raise PositionsDbUnavailable(f"{type(e).__name__}: {e}") from e

    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            logger.debug("[OPEN POSITION SYNC] session close failed", exc_info=True)


def load_open_positions_from_db() -> Dict[str, Dict[str, Any]]:
    """positions テーブルから未クローズ建玉/エントリー済み行を読む。"""
    try:
        from database import Session_position
        from database.models import Position
    except Exception:
        logger.debug("[OPEN POSITION SYNC] import DB modules failed", exc_info=True)
        _mark_db_read_failed("import DB modules failed")
        return {}

    session = None
    try:
        session = Session_position()
        rows = session.query(Position).all()

        out: Dict[str, Dict[str, Any]] = {}
        skipped_closed = 0
        skipped_qty = 0
        skipped_price = 0

        for p in rows or []:
            symbol = _normalize_symbol(getattr(p, "symbol", ""))
            if not symbol:
                continue

            status = _normalize_status(getattr(p, "status", ""))
            if not _is_open_like_status(status):
                skipped_closed += 1
                continue

            d = _position_model_to_dict(p)

            if d.get("qty", 0) <= 0:
                skipped_qty += 1
                continue

            # EXIT監視は現在値を別ソースから取るが、entry_priceが0だとrunnerで落ちるため、
            # avg_price/priceのどちらも0のものだけ除外する。
            if d.get("entry_price", 0.0) <= 0:
                skipped_price += 1
                continue

            out[symbol] = d

        try:
            logger.info(
                "[OPEN POSITION SYNC] DB scan rows=%d open_like=%d skipped_closed=%d skipped_qty=%d skipped_price=%d symbols=%s",
                len(rows or []),
                len(out),
                skipped_closed,
                skipped_qty,
                skipped_price,
                sorted(out.keys()),
            )
        except Exception:
            pass

        _mark_db_read_ok()
        return out

    except Exception as e:  # noqa: BLE001
        # P0-2: 空 dict を返すが、「読めなかった」ことを必ず記録する。
        # 呼び出し元は db_read_ok() で empty と failure を区別できる。
        logger.exception("[OPEN POSITION SYNC] load from DB failed")
        _mark_db_read_failed(f"{type(e).__name__}: {e}")
        return {}

    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            logger.debug("[OPEN POSITION SYNC] session close failed", exc_info=True)


def _sync_to_gc_positions(positions: Dict[str, Dict[str, Any]]) -> None:
    if not positions:
        return

    try:
        positions_obj = getattr(GC, "positions", None)
        if positions_obj is None:
            return

        for symbol, pos in positions.items():
            synced = False
            for method_name in ["set", "put", "add", "upsert", "update_position"]:
                fn = getattr(positions_obj, method_name, None)
                if callable(fn):
                    try:
                        fn(symbol, pos)
                        synced = True
                        break
                    except TypeError:
                        try:
                            fn(pos)
                            synced = True
                            break
                        except Exception:
                            pass
                    except Exception:
                        pass

            if synced:
                continue

            raw = getattr(positions_obj, "open_positions", None)
            if isinstance(raw, dict):
                raw[symbol] = pos
                continue

            raw = getattr(positions_obj, "positions", None)
            if isinstance(raw, dict):
                raw[symbol] = pos
                continue

    except Exception:
        logger.debug("[OPEN POSITION SYNC] sync to GC.positions failed", exc_info=True)


def _sync_open_positions_from_db_core(*, force_log: bool = False) -> Dict[str, Dict[str, Any]]:
    """DBの未クローズ建玉を global_data / GC に同期して返す。"""
    db_positions = load_open_positions_from_db()

    if _broker_authoritative_active():
        current = _current_global_positions_copy()
        try:
            global_data.open_positions_db_sync_skipped_broker_authoritative = True
            global_data.open_positions_db_sync_skipped_at = dt.datetime.now()
            global_data.open_positions_db_seen_count = len(db_positions)
            global_data.open_positions_db_seen_symbols = sorted(db_positions.keys())
        except Exception:
            pass
        if force_log or db_positions:
            logger.warning(
                "[OPEN POSITION SYNC] DB publish skipped because broker authoritative active db_count=%s db_symbols=%s current_symbols=%s",
                len(db_positions),
                sorted(db_positions.keys()),
                sorted(current.keys()),
            )
        return current

    gd_positions = _ensure_global_open_positions()

    changed = 0
    for symbol, pos in db_positions.items():
        before = gd_positions.get(symbol)
        if before != pos:
            changed += 1
        gd_positions[symbol] = pos

    if db_positions:
        for symbol in list(gd_positions.keys()):
            s = _normalize_symbol(symbol)
            if s and s not in db_positions:
                try:
                    src = str((gd_positions.get(symbol) or {}).get("_position_source") or "")
                    if src.startswith("DB.positions"):
                        gd_positions.pop(symbol, None)
                        changed += 1
                except Exception:
                    pass

    _sync_to_gc_positions(db_positions)

    try:
        global_data.open_positions_synced_at = dt.datetime.now()
        global_data.open_positions_synced_count = len(db_positions)
        global_data.open_positions_db_sync_skipped_broker_authoritative = False
    except Exception:
        pass

    if force_log or changed or db_positions:
        logger.warning(
            "[OPEN POSITION SYNC] synced open positions count=%s changed=%s symbols=%s",
            len(db_positions),
            changed,
            sorted(db_positions.keys()),
        )

    return db_positions


def is_symbol_in_open_position(symbol: Any, *, sync: bool = True) -> bool:
    """この銘柄を既に持っているか。

    P0-2 fail-closed: 建玉状態が確定できないときは True を返す。
    ENTRY 側はこの戻り値が True なら新規建玉を出さないので、
    「分からない」を「持っていない」に倒して二重建玉を出すことを防ぐ。
    """
    s = _normalize_symbol(symbol)
    if not s:
        return False

    # 建玉状態が unknown (DB も broker も読めない) なら保有扱いにする。
    try:
        from trading.position.position_state_service import get_positions_state

        state = get_positions_state(caller="is_symbol_in_open_position")
        if (not state.entry_allowed) and state.positions_known:
            # 状態自体は確定しているのに entry_allowed=False ならキャッシュが
            # 古いだけ。ここは二重建玉を防ぐゲートなので取り直す。
            # 逆に positions_known=False のときは取り直しても結論は変わらず
            # (DB/broker が読めていない)、REST/NAS を連打するだけなので行わない。
            state = get_positions_state(force=True, caller="is_symbol_in_open_position:verify")
        if not state.positions_known:
            logger.warning(
                "[OPEN POSITION SYNC] 建玉状態 未確定(%s)のため symbol=%s を保有扱いにする "
                "(fail-closed) db_ok=%s broker_ok=%s source=%s last_good_age=%.1fs",
                getattr(state, "entry_block_reason", "") or "unknown",
                s, state.db_ok, state.broker_ok, state.source_used,
                0.0 if state.last_good_age == float("inf") else state.last_good_age,
            )
            return True
        if s in {_normalize_symbol(k) for k in state.positions.keys()}:
            return True
    except Exception:
        logger.debug("[OPEN POSITION SYNC] position state service unavailable", exc_info=True)

    if sync:
        positions = sync_open_positions_from_db()
        # DB が読めていないなら空 dict は「建玉なし」ではない。
        if not positions and not db_read_ok():
            logger.warning(
                "[OPEN POSITION SYNC] positions.db 読み取り失敗中のため symbol=%s を "
                "保有扱いにする (fail-closed) err=%s",
                s, db_read_status().get("error", ""),
            )
            return True
    else:
        positions = getattr(global_data, "open_positions", None)
        if not isinstance(positions, dict):
            positions = {}

    if s in {_normalize_symbol(k) for k in positions.keys()}:
        return True

    for p in positions.values():
        try:
            if _normalize_symbol((p or {}).get("symbol")) == s:
                return True
        except Exception:
            logger.debug("[OPEN POSITION SYNC] symbol compare failed", exc_info=True)

    return False


__all__ = [
    "PositionsDbUnavailable",
    "load_open_positions_from_db",
    "load_open_positions_from_db_strict",
    "sync_open_positions_from_db",
    "is_symbol_in_open_position",
    "db_read_ok",
    "db_read_status",
]


# ============================================================
# sync_open_positions_from_db の3層ガード
# ------------------------------------------------------------
# 旧 core/startup の3パッチを本文化。実行時の合成は
#   native <- broker_merge <- stale_db_cleanup <- sync_throttle
# の3層で、いずれも有効だった (_ORIG* 突き合わせで実測)。同じ順序で重ねている。
#
#   1層目 _obm_*: broker 実建玉を正としてDBとマージ・publish
#   2層目 _osc_*: sync 後に stale なDB建玉を掃除
#   3層目 _ost_*: broker authoritative empty をTTLキャッシュして連続読取を抑制
#
# スコープは旧 install 導線 (いずれも main.py 経由) を再現する。
# ヘルパは3パッチのものを _obm_/_osc_/_ost_ で名前空間を分けて移植した
# (同名でも実装が異なるため流用しない)。
# ============================================================

import time as _ops_time

os.environ.setdefault("OPEN_POSITION_BROKER_AUTHORITATIVE", "1")
os.environ.setdefault("OPEN_POSITION_CLOSE_STALE_DB_WHEN_BROKER_EMPTY", "1")
os.environ.setdefault("OPEN_POSITION_ALLOW_DB_FALLBACK_ON_PARSE_SUSPICIOUS", "0")
os.environ.setdefault("OPEN_POSITION_KEEP_EXISTING_ON_PARSE_SUSPICIOUS", "0")

_OPS_CACHE: Dict[str, Any] = {"ts": 0.0, "result": None, "empty_authoritative": False}
_OPS_SCOPE = None

# --- 2層目 _osc_* の module global -------------------------------------
# 旧 core/startup/open_position_stale_db_cleanup_patch.py から本文化した際に
# 取り込み漏れていた module global。cooldown 状態と CLOSED ステータス名。
_OSC_CLOSED_STATUS = "CLOSED_BROKER_NOT_OPEN"
_OSC_LAST_CLEAN_AT: "dt.datetime | None" = None
# sync_open_positions_from_db は複数スレッドから呼ばれるため、cooldown判定と
# DB UPDATE をまとめて直列化する。取得できなければ他スレッドが実行中なのでスキップ。
_OSC_CLEAN_LOCK = threading.Lock()

def _obm_env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _obm_normalize_symbol(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _obm_text(v: Any) -> str:
    try:
        return str(v or "").strip()
    except Exception:
        return ""


def _obm_safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _obm_is_credit_position(pos: Dict[str, Any], *, source: str = "") -> bool:
    if not isinstance(pos, dict):
        return False
    src = _obm_text(source or pos.get("_position_source")).upper()
    mt = _obm_text(pos.get("margin_trade_type") or pos.get("MarginTradeType"))
    at = _obm_text(pos.get("account_type") or pos.get("AccountType"))
    pr = _obm_text(pos.get("product") or pos.get("Product"))
    side = _obm_text(pos.get("side") or pos.get("Side"))
    joined = f"{src} {mt} {at} {pr} {side}".upper()
    if any(x in joined for x in ("CASH", "現物", "GENBUTSU", "PRODUCT=1")):
        return False
    if "CREDIT_ONLY" in src:
        return True
    if pr == "2":
        return True
    if "信用" in joined or "MARGIN" in joined:
        return True
    if source.lower().startswith("db") and not mt:
        return False
    return bool(mt)


def _obm_ensure_open_positions() -> Dict[str, Dict[str, Any]]:
    try:
        from global_state import global_data
        d = getattr(global_data, "open_positions", None)
        if isinstance(d, dict):
            return d
        d = {}
        setattr(global_data, "open_positions", d)
        return d
    except Exception:
        logger.debug("[OPEN POSITION BROKER PATCH] ensure open_positions failed", exc_info=True)
        return {}


def _obm_existing_credit_positions() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        gd_positions = _obm_ensure_open_positions()
        for k, v in (gd_positions or {}).items():
            s = _obm_normalize_symbol(k or (v or {}).get("symbol"))
            if not s or not isinstance(v, dict):
                continue
            if not _obm_is_credit_position(v, source="existing"):
                continue
            out[s] = v
    except Exception:
        pass
    return out


def _obm_read_broker_positions_with_status() -> Tuple[Dict[str, Dict[str, Any]], bool, dict]:
    try:
        import trading.position.kabu_position_reader as reader
        rows = reader.read_kabu_open_positions() or {}
        try:
            status = reader.get_last_read_status() or {}
        except Exception:
            status = {}
        read_ok = bool(status.get("ok", True))
        out: Dict[str, Dict[str, Any]] = {}
        skipped_non_credit = 0
        for k, v in rows.items():
            s = _obm_normalize_symbol(k or (v or {}).get("symbol"))
            if not s or not isinstance(v, dict):
                continue
            if not _obm_is_credit_position(v, source="broker"):
                skipped_non_credit += 1
                continue
            out[s] = v
        if skipped_non_credit:
            logger.warning("[OPEN POSITION BROKER PATCH] broker non-credit skipped=%d", skipped_non_credit)
        return out, read_ok, status
    except Exception as e:
        logger.warning("[OPEN POSITION BROKER PATCH] broker reader failed err=%s", e, exc_info=True)
        return {}, False, {"ok": False, "error": str(e)}


def _obm_filter_db_credit_positions(db_positions: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    skipped = 0
    for k, v in (db_positions or {}).items():
        s = _obm_normalize_symbol(k or (v or {}).get("symbol"))
        if not s or not isinstance(v, dict):
            continue
        if not _obm_is_credit_position(v, source="db"):
            skipped += 1
            continue
        out[s] = v
    if skipped:
        logger.warning("[OPEN POSITION BROKER PATCH] db non-credit/stale skipped=%d", skipped)
    return out


def _obm_broker_parse_suspicious(*, broker_positions: Dict[str, Dict[str, Any]], broker_read_ok: bool, broker_status: dict) -> bool:
    if not broker_read_ok:
        return False
    if broker_positions:
        return False
    raw_count = _obm_safe_int(broker_status.get("raw_count"), 0)
    credit_candidates = _obm_safe_int(broker_status.get("credit_candidate_count"), 0)
    skipped_qty = _obm_safe_int(broker_status.get("skipped_qty"), 0)
    skipped_price = _obm_safe_int(broker_status.get("skipped_price"), 0)
    credit_open = _obm_safe_int(broker_status.get("credit_open_count"), 0)
    if raw_count > 0 and credit_candidates > 0 and credit_open == 0 and (skipped_qty + skipped_price) >= credit_candidates:
        return True
    if raw_count > 0 and credit_candidates == 0 and skipped_qty >= raw_count:
        return True
    return False


def _obm_clear_open_positions_dict(gd_positions: Dict[str, Dict[str, Any]]) -> None:
    for k in list(gd_positions.keys()):
        try:
            src = str((gd_positions.get(k) or {}).get("_position_source") or "")
            if (not src) or src.startswith("DB.positions") or src.startswith("KABU.positions") or src.startswith("BROKER") or src.startswith("EXISTING"):
                gd_positions.pop(k, None)
        except Exception:
            pass


def _obm_close_stale_db_positions(*, stale_symbols: set[str], reason: str) -> int:
    if not stale_symbols:
        return 0
    if not _obm_env_bool("OPEN_POSITION_CLOSE_STALE_DB_WHEN_BROKER_EMPTY", True):
        return 0
    try:
        from database import Session_position
        from database.models import Position
    except Exception:
        logger.exception("[OPEN POSITION BROKER PATCH] stale DB close import failed")
        return 0

    session = None
    changed = 0
    now = dt.datetime.now()
    try:
        session = Session_position()
        rows = session.query(Position).all()
        for p in rows or []:
            s = _obm_normalize_symbol(getattr(p, "symbol", ""))
            if not s or s not in stale_symbols:
                continue
            st = _obm_text(getattr(p, "status", "OPEN")).upper()
            if st in CLOSED_STATUSES:
                continue
            try:
                setattr(p, "status", "CLOSED")
            except Exception:
                pass
            for attr, value in (
                ("exit_reason", reason),
                ("close_reason", reason),
                ("updated_at", now),
                ("exit_time", now),
                ("closed_time", now),
                ("close_time", now),
            ):
                try:
                    if hasattr(p, attr):
                        setattr(p, attr, value)
                except Exception:
                    pass
            changed += 1
        if changed:
            session.commit()
            logger.warning(
                "[OPEN POSITION BROKER PATCH] closed stale DB positions by broker sync count=%d symbols=%s reason=%s",
                changed,
                sorted(stale_symbols),
                reason,
            )
        else:
            session.rollback()
    except Exception:
        try:
            if session is not None:
                session.rollback()
        except Exception:
            pass
        logger.exception("[OPEN POSITION BROKER PATCH] close stale DB positions failed symbols=%s", sorted(stale_symbols))
        return 0
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass
    return changed


def _obm_merge_and_publish(db_positions: Dict[str, Dict[str, Any]], broker_positions: Dict[str, Dict[str, Any]], *, broker_read_ok: bool, broker_status: dict | None = None) -> Dict[str, Dict[str, Any]]:
    db_credit = _obm_filter_db_credit_positions(db_positions)
    existing_credit = _obm_existing_credit_positions()
    broker_status = broker_status or {}
    parse_suspicious = _obm_broker_parse_suspicious(broker_positions=broker_positions, broker_read_ok=broker_read_ok, broker_status=broker_status)

    broker_authoritative = _obm_env_bool("OPEN_POSITION_BROKER_AUTHORITATIVE", True)
    allow_parse_suspicious_db_fallback = _obm_env_bool("OPEN_POSITION_ALLOW_DB_FALLBACK_ON_PARSE_SUSPICIOUS", False)
    keep_existing_on_parse_suspicious = _obm_env_bool("OPEN_POSITION_KEEP_EXISTING_ON_PARSE_SUSPICIOUS", False)

    stale_symbols: set[str] = set()

    if broker_read_ok and broker_authoritative:
        if broker_positions:
            merged = dict(broker_positions)
            source_mode = "broker_credit_authoritative"
            stale_symbols = set(db_credit.keys()) - set(broker_positions.keys())
        else:
            # 株ステーションが読めていて信用建玉0件なら、DB/既存を正に戻さない。
            merged = {}
            source_mode = "broker_credit_authoritative_empty_parse_suspicious_cleared" if parse_suspicious else "broker_credit_authoritative_empty_ok"
            stale_symbols = set(db_credit.keys()) | set(existing_credit.keys())
    elif broker_read_ok:
        if broker_positions:
            merged = dict(broker_positions)
            source_mode = "broker_credit_authoritative"
            stale_symbols = set(db_credit.keys()) - set(broker_positions.keys())
        elif parse_suspicious and allow_parse_suspicious_db_fallback and db_credit:
            merged = dict(db_credit)
            source_mode = "db_credit_fallback_broker_parse_suspicious"
        elif parse_suspicious and keep_existing_on_parse_suspicious and existing_credit:
            merged = dict(existing_credit)
            source_mode = "existing_credit_kept_broker_parse_suspicious"
        else:
            merged = {}
            source_mode = "broker_credit_authoritative_empty_parse_suspicious_no_fallback" if parse_suspicious else "broker_credit_authoritative_empty_ok"
            stale_symbols = set(db_credit.keys()) | set(existing_credit.keys())
    else:
        merged = dict(db_credit or existing_credit)
        source_mode = "db_credit_fallback_broker_read_failed"

    if stale_symbols and broker_read_ok:
        _obm_close_stale_db_positions(stale_symbols=stale_symbols, reason=f"BROKER_SYNC_NO_POSITION:{source_mode}")

    gd_positions = _obm_ensure_open_positions()
    before_keys = {_obm_normalize_symbol(k) for k in gd_positions.keys()}
    merged_keys = set(merged.keys())
    _obm_clear_open_positions_dict(gd_positions)

    for s, pos in merged.items():
        try:
            if isinstance(pos, dict):
                if source_mode.startswith("db_"):
                    pos.setdefault("_position_source", "DB.positions")
                elif source_mode.startswith("existing_"):
                    pos.setdefault("_position_source", "EXISTING.positions.parse_suspicious")
                else:
                    pos.setdefault("_position_source", "KABU.positions")
        except Exception:
            pass
        gd_positions[s] = pos

    try:
        from global_state import global_data
        global_data.open_positions_synced_at = dt.datetime.now()
        global_data.open_positions_synced_count = len(merged)
        global_data.open_positions_source_mode = source_mode
        global_data.open_positions_broker_read_ok = bool(broker_read_ok)
        global_data.open_positions_broker_parse_suspicious = bool(parse_suspicious)
        global_data.open_positions_broker_status = broker_status
        global_data.open_positions_db_fallback_on_parse_suspicious = bool(allow_parse_suspicious_db_fallback)
        global_data.open_positions_keep_existing_on_parse_suspicious = bool(keep_existing_on_parse_suspicious)
        global_data.open_positions_broker_authoritative = bool(broker_authoritative)
        global_data.open_positions_stale_db_symbols_closed = sorted(stale_symbols)
    except Exception:
        pass

    changed = before_keys != merged_keys

    # ------------------------------------------------------------
    # 2026-09-14: ENTRY 約定フック
    #   このシステムには約定イベントが無い。約定を最初に知るのはここで、
    #   ブローカー側に新しい建玉が現れた差分 (merged_keys - before_keys) が
    #   それに当たる。
    #
    #   trading/entry/fill_handler.py の on_entry_filled() は
    #   「ENTRY 約定後の唯一の処理点」として 2026-05-04 に書かれたが、
    #   呼び出し元が一度も実装されず (git log -S で作成コミットのみ)、
    #   さらに import 自体が ImportError になっていた。そのため
    #     * positions.db に OPEN 建玉が書かれない
    #       -> [PROTECTED PUSH] の保護対象が常に 0 件になり、保有銘柄が
    #          PUSH 登録から外れて価格が止まる
    #       -> 2026-09-14 に 4667 を 12 秒前の価格で利確判断し -5,300 円
    #     * 決済時に DB 建玉が見つからずブローカーから救出していた
    #   という状態だった。
    #
    #   ここで呼ぶことで OPEN 建玉が約定時点で記録される。
    #   注意: atr_1min は建玉 dict に無いので 0.0 になる。ExitContext の
    #   ATR が実値になるわけではない (fallback と同じ)。別途 ATR の
    #   供給源が必要。
    #
    #   絶対にこのフックで position sync を落とさないこと。例外は飲む。
    # ------------------------------------------------------------
    try:
        _notify_entry_filled_for_new_positions(before_keys, merged_keys, merged)
    except Exception:
        logger.exception("[ENTRY FILL HOOK] failed (position sync は継続する)")

    logger.warning(
        "[OPEN POSITION BROKER PATCH] merged credit open positions count=%d changed=%s mode=%s broker_read_ok=%s parse_suspicious=%s broker_authoritative=%s db_fallback_on_parse_suspicious=%s keep_existing_on_parse_suspicious=%s stale_closed=%s broker_status=%s db_count=%d db_credit=%d existing_credit=%d broker_count=%d symbols=%s",
        len(merged), changed, source_mode, broker_read_ok, parse_suspicious,
        broker_authoritative, allow_parse_suspicious_db_fallback, keep_existing_on_parse_suspicious,
        sorted(stale_symbols), broker_status, len(db_positions or {}), len(db_credit), len(existing_credit), len(broker_positions or {}), sorted(merged.keys()),
    )
    return merged


_ENTRY_FILL_NOTIFIED: set[str] = set()


def _notify_entry_filled_for_new_positions(before_keys, merged_keys, merged) -> None:
    """新しく現れた建玉について on_entry_filled() を一度だけ呼ぶ。

    建玉が消えたら記録も落として、次に同じ銘柄を建てたときに再通知できる
    ようにする。同じ建玉で二重に Position 行を作らないための冪等化。
    """
    if not _osc_env_bool("ENTRY_FILL_HOOK_ENABLED", True):
        return

    try:
        before = set(before_keys or ())
        after = set(merged_keys or ())
    except Exception:
        return

    # 消えた建玉は通知済みフラグを落とす
    for gone in (_ENTRY_FILL_NOTIFIED - after):
        _ENTRY_FILL_NOTIFIED.discard(gone)

    new_symbols = [s for s in (after - before) if s not in _ENTRY_FILL_NOTIFIED]
    if not new_symbols:
        return

    from trading.entry.fill_handler import on_entry_filled

    for sym in new_symbols:
        pos = (merged or {}).get(sym) or {}
        try:
            qty = int(float(pos.get("qty") or 0))
            price = float(pos.get("avg_price") or 0.0)
        except Exception:
            qty, price = 0, 0.0
        if qty <= 0 or price <= 0:
            logger.warning(
                "[ENTRY FILL HOOK] skip symbol=%s qty=%s price=%s (建玉の数量/価格が取れない)",
                sym, pos.get("qty"), pos.get("avg_price"),
            )
            continue
        _ENTRY_FILL_NOTIFIED.add(sym)
        logger.warning(
            "[ENTRY FILL HOOK] on_entry_filled symbol=%s side=%s qty=%s price=%s",
            sym, pos.get("side"), qty, price,
        )
        try:
            # 2026-09-14: 決済に必要な建玉の属性 (hold_id / 取引所 / 信用区分 /
            #   口座区分) も渡す。これが無いと DB 行が side=BUY・hold_id なしに
            #   なり、決済側が現物売りを送って 4001005 で失敗する。
            on_entry_filled(
                symbol=sym,
                symbolname=str(pos.get("symbolname") or ""),
                side=str(pos.get("side") or "BUY"),
                qty=qty,
                price=price,
                atr_1min=float(pos.get("atr_1min") or pos.get("atr") or 0.0),
                order_id=str(pos.get("order_id") or pos.get("execution_id") or ""),
                exchange=_osc_opt_int(pos.get("exchange")),
                margin_trade_type=_osc_opt_int(pos.get("margin_trade_type")),
                account_type=_osc_opt_int(pos.get("account_type")),
                hold_id=str(pos.get("hold_id") or pos.get("HoldID") or pos.get("execution_id") or ""),
                execution_id=str(pos.get("execution_id") or pos.get("ExecutionID") or ""),
            )
        except Exception:
            logger.exception("[ENTRY FILL HOOK] on_entry_filled raised symbol=%s", sym)


def _osc_opt_int(v):
    """建玉 dict の数値属性。無ければ None のまま (0 や 1 を勝手に補わない)。"""
    try:
        if v is None or str(v).strip() == "":
            return None
        return int(float(v))
    except Exception:
        return None


def _osc_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    except Exception:
        return bool(default)


def _osc_env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _osc_norm(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ""


def _osc_broker_authoritative_active() -> bool:
    try:
        from global_state import global_data
        read_ok = bool(getattr(global_data, "open_positions_broker_read_ok", False))
        mode = str(getattr(global_data, "open_positions_source_mode", "") or "")
        return bool(read_ok and mode.startswith("broker_credit_authoritative"))
    except Exception:
        return False


def _osc_current_broker_symbols() -> set[str]:
    try:
        from global_state import global_data
        positions = getattr(global_data, "open_positions", None)
        if isinstance(positions, dict):
            return {_osc_norm(k) for k in positions.keys() if _osc_norm(k)}
    except Exception:
        pass
    return set()


def _osc_status(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _osc_is_open_like_status(v: Any) -> bool:
    st = _osc_status(v)
    if st in {"CLOSED", "CLOSE", "EXITED", "EXIT", "DONE", "CANCELED", "CANCELLED", "REJECTED", "FAILED", _OSC_CLOSED_STATUS}:
        return False
    return True


def _osc_safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _osc_cleanup_stale_db_positions(broker_symbols: set[str] | None = None) -> int:
    if not _osc_env_bool("OPEN_POSITION_AUTO_CLOSE_STALE_DB", True):
        return 0
    if not _osc_broker_authoritative_active():
        return 0

    # 他スレッドが cleanup 中なら二重にDBを叩かない。
    if not _OSC_CLEAN_LOCK.acquire(blocking=False):
        return 0
    try:
        return _osc_cleanup_stale_db_positions_locked(broker_symbols=broker_symbols)
    finally:
        _OSC_CLEAN_LOCK.release()


def _osc_cleanup_stale_db_positions_locked(broker_symbols: set[str] | None = None) -> int:
    global _OSC_LAST_CLEAN_AT

    # 毎秒UPDATEを打たない。DB scanは走っても、cleanupは少し間引く。
    cooldown = max(1, _osc_env_int("OPEN_POSITION_STALE_DB_CLEAN_INTERVAL_SEC", 30))
    now = dt.datetime.now()
    last = _OSC_LAST_CLEAN_AT
    if last is not None:
        elapsed = (now - last).total_seconds()
        # システム時計が巻き戻った場合は cooldown を持ち越さない。
        if 0.0 <= elapsed < cooldown:
            return 0
    _OSC_LAST_CLEAN_AT = now

    # 2026-09-14: global_data.open_positions は GC.positions.snapshot_dict() を
    #   返す読み取り専用 property で、_obm_merge_and_publish はそのコピーに
    #   書いているだけなので常に空。結果、ブローカーに建玉があっても
    #   「broker_symbols=[]」で DB 行を数秒後に CLOSED_BROKER_NOT_OPEN にしていた
    #   (7013 / 8001 / 9984 で実測)。直前の merge が返したブローカー建玉の集合を
    #   呼び出し側から渡してもらい、それを正とする。渡されなければ従来どおり。
    if broker_symbols is None:
        broker_symbols = _osc_current_broker_symbols()
    else:
        broker_symbols = {_osc_norm(s) for s in broker_symbols if _osc_norm(s)}

    try:
        from database import Session_position
        from database.models import Position
    except Exception:
        logger.debug("[OPEN POSITION STALE DB CLEAN] import DB modules failed", exc_info=True)
        return 0

    session = None
    changed = 0
    changed_symbols: list[str] = []
    try:
        session = Session_position()
        rows = session.query(Position).all()
        for p in rows or []:
            symbol = _osc_norm(getattr(p, "symbol", ""))
            if not symbol:
                continue
            if symbol in broker_symbols:
                continue
            if not _osc_is_open_like_status(getattr(p, "status", "")):
                continue
            qty = _osc_safe_float(getattr(p, "qty", 0), 0.0)
            price = _osc_safe_float(getattr(p, "avg_price", 0), 0.0) or _osc_safe_float(getattr(p, "price", 0), 0.0)
            if qty <= 0 or price <= 0:
                continue

            try:
                setattr(p, "status", _OSC_CLOSED_STATUS)
            except Exception:
                pass
            try:
                setattr(p, "updated_at", now)
            except Exception:
                pass
            changed += 1
            changed_symbols.append(symbol)

        if changed:
            session.commit()
            logger.warning(
                "[OPEN POSITION STALE DB CLEAN] closed stale db positions count=%s symbols=%s broker_symbols=%s status=%s",
                changed,
                changed_symbols,
                sorted(broker_symbols),
                _OSC_CLOSED_STATUS,
            )
        return changed
    except Exception as e:
        try:
            if session is not None:
                session.rollback()
        except Exception:
            pass
        logger.exception("[OPEN POSITION STALE DB CLEAN] failed err=%s", e)
        return 0
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass


def _ost_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _ost_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _ost_is_broker_authoritative_empty(result: Any) -> bool:
    """global_data 状態と戻り値から broker authoritative empty を判定。"""
    try:
        if isinstance(result, dict) and len(result) > 0:
            return False

        from global_state import global_data

        mode = str(getattr(global_data, "open_positions_source_mode", "") or "")
        read_ok = bool(getattr(global_data, "open_positions_broker_read_ok", False))
        cnt = int(getattr(global_data, "open_positions_synced_count", 0) or 0)

        if read_ok and cnt == 0 and mode.startswith("broker_credit_authoritative_empty"):
            return True
        return False
    except Exception:
        return False


def _ost_clone_result(result: Any) -> Any:
    try:
        return copy.deepcopy(result)
    except Exception:
        return result


def _ops_scope_enabled() -> bool:
    global _OPS_SCOPE
    if _OPS_SCOPE is not None:
        return _OPS_SCOPE
    enabled = False
    try:
        import sys as _sys

        argv_text = " ".join(str(x) for x in _sys.argv).replace("\\", "/").lower()
        db_names = (
            "main_database.py", "data_collectors_runner.py", "db_prepare_runner.py",
            "push_receiver_runner.py", "yahoo_complement_runner.py",
            "summary_database_runner.py", "ranking_collector_runner.py",
        )
        if not any(n in argv_text for n in db_names):
            enabled = "main.py" in argv_text
    except Exception:
        enabled = False
    _OPS_SCOPE = enabled
    return enabled


def load_open_positions_from_broker():
    """旧 open_position_broker_merge_patch が target へ生やしていた入口。"""
    return _obm_read_broker_positions_with_status()[0]


def _sync_layer1_broker_merge(*, force_log: bool = False):
    try:
        db_positions = _sync_open_positions_from_db_core(force_log=force_log) or {}
    except Exception:
        logger.exception("[OPEN POSITION BROKER PATCH] original sync failed")
        db_positions = {}
    broker_positions, broker_read_ok, broker_status = _obm_read_broker_positions_with_status()
    return _obm_merge_and_publish(
        db_positions, broker_positions, broker_read_ok=broker_read_ok, broker_status=broker_status
    )


def _sync_layer2_stale_cleanup(*args, **kwargs):
    result = _sync_layer1_broker_merge(*args, **kwargs)
    try:
        # merge が返す dict はブローカー正のときブローカー建玉そのもの。
        # ブローカーが読めなかった回は DB フォールバックが返るので、その時は
        # 何も消さない (読めない = 消してよい根拠が無い)。
        broker_ok = bool(getattr(global_data, "open_positions_broker_read_ok", False))
        if broker_ok and isinstance(result, dict):
            _osc_cleanup_stale_db_positions(broker_symbols=set(result.keys()))
    except Exception:
        logger.exception("[OPEN POSITION STALE DB CLEAN] cleanup after sync failed")
    return result


def sync_open_positions_from_db(*, force_log: bool = False) -> Dict[str, Dict[str, Any]]:
    if not _ops_scope_enabled():
        return _sync_open_positions_from_db_core(force_log=force_log)

    if not _ost_env_bool("OPEN_POSITION_THROTTLE_ENABLED", True):
        return _sync_layer2_stale_cleanup(force_log=force_log)

    now = _ops_time.monotonic()
    throttle_sec = max(0.0, _ost_env_float("OPEN_POSITION_EMPTY_THROTTLE_SEC", 5.0))

    # force_log時は必ず実読取。
    if not force_log and _OPS_CACHE.get("empty_authoritative") and _OPS_CACHE.get("result") is not None:
        age = now - float(_OPS_CACHE.get("ts") or 0.0)
        if age < throttle_sec:
            if age < 0.1:
                logger.debug("[OPEN POSITION THROTTLE] cached empty hit age=%.3fs", age)
            return _ost_clone_result(_OPS_CACHE.get("result") or {})

    result = _sync_layer2_stale_cleanup(force_log=force_log) or {}
    empty_authoritative = _ost_is_broker_authoritative_empty(result)

    if empty_authoritative:
        _OPS_CACHE["ts"] = now
        _OPS_CACHE["result"] = _ost_clone_result(result or {})
        _OPS_CACHE["empty_authoritative"] = True
        logger.info("[OPEN POSITION THROTTLE] cache broker authoritative empty ttl=%.1fs", throttle_sec)
    else:
        _OPS_CACHE["ts"] = now
        _OPS_CACHE["result"] = None
        _OPS_CACHE["empty_authoritative"] = False

    return result
