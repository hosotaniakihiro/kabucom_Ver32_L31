# ============================================================
# File   : trading/position/open_position_sync.py
# Version: V1.1-PRODUCTION-OPEN-POSITION-SYNC-NON-CLOSED
# ------------------------------------------------------------
# 【目的】
#   既にエントリー済み/約定待ち/保有中の銘柄を Python 側の共通キャッシュへ同期する。
#
# 【重要修正 V1.1】
#   - status='OPEN' だけではなく、CLOSED/EXITED/CANCELED 以外も監視対象に含める
#   - 「エントリー済み3銘柄」なのに OPEN として1銘柄しか拾えない問題を緩和
#   - active_symbols protected / exit_loop / 二重エントリー防止に使えるようにする
#
# 【方針】
#   - qty > 0 の未クローズ行を読み込む
#   - avg_price が0でも、price があれば entry_price に使う
#   - global_data.open_positions に必ず同期する
#   - DBで見えている未クローズ銘柄は protected 対象にする
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict

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
    if st in CLOSED_STATUSES:
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


def load_open_positions_from_db() -> Dict[str, Dict[str, Any]]:
    """positions テーブルから未クローズ建玉/エントリー済み行を読む。"""
    try:
        from database import Session_position
        from database.models import Position
    except Exception:
        logger.debug("[OPEN POSITION SYNC] import DB modules failed", exc_info=True)
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

        return out

    except Exception:
        logger.exception("[OPEN POSITION SYNC] load from DB failed")
        return {}

    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass


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


def sync_open_positions_from_db(*, force_log: bool = False) -> Dict[str, Dict[str, Any]]:
    """DBの未クローズ建玉を global_data / GC に同期して返す。"""
    db_positions = load_open_positions_from_db()
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
    s = _normalize_symbol(symbol)
    if not s:
        return False

    if sync:
        positions = sync_open_positions_from_db()
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
            pass

    return False


__all__ = [
    "load_open_positions_from_db",
    "sync_open_positions_from_db",
    "is_symbol_in_open_position",
]
