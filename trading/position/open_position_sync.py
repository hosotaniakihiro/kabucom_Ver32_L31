# ============================================================
# File   : trading/position/open_position_sync.py
# Version: V1.0-PRODUCTION-OPEN-POSITION-SYNC
# ------------------------------------------------------------
# 【目的】
#   既にエントリー済みの建玉を Python 側の共通キャッシュへ同期する。
#
# 【背景】
#   DB positions には OPEN 建玉があるのに、global_data.open_positions / GC.positions
#   が空だと、以下の問題が起きる。
#     - entry_pipeline が position_skip できず二重エントリー候補になる
#     - exit_loop_5s が監視対象なしで即終了する
#     - active_symbol_manager 側が保有銘柄を優先保持できない
#
# 【方針】
#   - positions テーブル status='OPEN' を読み込む
#   - global_data.open_positions に必ず同期する
#   - GC.positions に同期できるAPIがあれば同期する
#   - 失敗しても本体処理を落とさない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict

from core.global_context.context import global_context as GC
from global_state import global_data

logger = logging.getLogger(__name__)


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

    entry_time = getattr(p, "entry_time", None) or getattr(p, "created_at", None)
    if entry_time is None:
        entry_time = dt.datetime.now()

    return {
        "id": getattr(p, "id", None),
        "symbol": symbol,
        "symbolname": getattr(p, "symbolname", None),
        "side": side,
        "qty": _safe_int(getattr(p, "qty", 0), 0),
        "quantity": _safe_int(getattr(p, "qty", 0), 0),
        "avg_price": _safe_float(getattr(p, "avg_price", 0.0), 0.0),
        "entry_price": _safe_float(getattr(p, "avg_price", 0.0), 0.0),
        "price": _safe_float(getattr(p, "price", 0.0), 0.0),
        "entry_time": entry_time,
        "created_at": getattr(p, "created_at", None),
        "updated_at": getattr(p, "updated_at", None),
        "status": "OPEN",
        "exchange": getattr(p, "exchange", None) or 1,
        "margin_trade_type": getattr(p, "margin_trade_type", None),
        "account_type": getattr(p, "account_type", None),
        "hold_id": getattr(p, "hold_id", None),
        "execution_id": getattr(p, "execution_id", None),
        "_position_source": "DB.positions",
    }


def load_open_positions_from_db() -> Dict[str, Dict[str, Any]]:
    """positions テーブルから OPEN 建玉を読む。"""
    try:
        from database import Session_position
        from database.models import Position
    except Exception:
        logger.debug("[OPEN POSITION SYNC] import DB modules failed", exc_info=True)
        return {}

    session = None
    try:
        session = Session_position()
        rows = (
            session.query(Position)
            .filter(Position.status == "OPEN")
            .all()
        )

        out: Dict[str, Dict[str, Any]] = {}
        for p in rows or []:
            d = _position_model_to_dict(p)
            symbol = _normalize_symbol(d.get("symbol"))
            if not symbol:
                continue
            if d.get("qty", 0) <= 0:
                continue
            if d.get("avg_price", 0.0) <= 0:
                continue
            out[symbol] = d

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

        # 既存実装に合わせて、使えるメソッドがあればそこへ流す。
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
    """
    DBのOPEN建玉を global_data / GC に同期して返す。

    EXITループ前、ENTRYのpositionチェック前、銘柄選定前に呼んでよい。
    """
    db_positions = load_open_positions_from_db()
    gd_positions = _ensure_global_open_positions()

    changed = 0
    for symbol, pos in db_positions.items():
        before = gd_positions.get(symbol)
        if before != pos:
            changed += 1
        gd_positions[symbol] = pos

    # DBでは閉じているがメモリに残っているものは、DB同期時は消す。
    # ただしDBが一時的に読めなかった場合は db_positions が空になるため消さない。
    if db_positions:
        for symbol in list(gd_positions.keys()):
            s = _normalize_symbol(symbol)
            if s and s not in db_positions:
                try:
                    status = str((gd_positions.get(symbol) or {}).get("status") or "OPEN").upper()
                    if status == "OPEN" and (gd_positions.get(symbol) or {}).get("_position_source") == "DB.positions":
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
