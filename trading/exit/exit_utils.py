# ============================================================
# File   : trading/exit/exit_utils.py
# Version: V1.3-SPLIT-EXIT-UTILS-BROKER-POSITION-MERGE
# ------------------------------------------------------------
# EXIT系共通ユーティリティ。
#
# 重要修正:
#   - GC.positions / global_data.open_positions に加え、DB positions(status=OPEN) も見る
#   - exit_loop_5s が監視対象なしで即終了する問題を防ぐ
#   - DBから読んだ建玉を global_data.open_positions / GC.positions へ同期する
#   - V1.3: kabu Station 実信用建玉も毎回マージする
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
from typing import Any, Dict, Optional

from core.global_context.context import global_context as GC
from global_state import global_data

logger = logging.getLogger(__name__)


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def safe_float_or_none(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except Exception:
        return None


def safe_int_or_none(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        if isinstance(v, bool):
            return int(v)
        fv = float(v)
        if math.isnan(fv) or math.isinf(fv):
            return None
        return int(fv)
    except Exception:
        return None


def safe_bool(v: Any, default: bool = False) -> bool:
    try:
        if v is None:
            return bool(default)
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "on", "y", "ok"}:
            return True
        if s in {"0", "false", "no", "off", "n", "ng", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def safe_bool_or_none(v: Any) -> Optional[bool]:
    try:
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "on", "y", "ok", "break", "broken", "below"}:
            return True
        if s in {"0", "false", "no", "off", "n", "ng", "", "none", "above"}:
            return False
        return None
    except Exception:
        return None


def safe_str(v: Any, default: str = "") -> str:
    try:
        if v is None:
            return default
        return str(v)
    except Exception:
        return default


def normalize_symbol(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def dict_get_any(d: Any, *names: str, default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default
    for name in names:
        try:
            if name in d:
                return d.get(name)
        except Exception:
            pass
    try:
        lower_map = {str(k).lower(): k for k in d.keys()}
        for name in names:
            real_key = lower_map.get(str(name).lower())
            if real_key is not None:
                return d.get(real_key)
    except Exception:
        pass
    return default


def attr_get_any(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    for name in names:
        try:
            if hasattr(obj, name):
                return getattr(obj, name)
        except Exception:
            pass
    return default


def _position_to_dict(pos: Any) -> Dict[str, Any]:
    if pos is None:
        return {}
    if isinstance(pos, dict):
        return dict(pos)

    out: Dict[str, Any] = {}
    for name in [
        "symbol", "Symbol", "symbolname", "side", "Side", "qty", "quantity", "LeavesQty",
        "avg_price", "entry_price", "AveragePrice", "AvgPrice", "ExecutionPrice", "Price",
        "entry_time", "created_at", "updated_at", "status", "atr", "atr_1min",
        "order_id", "entry_order_id", "ranking_lost_minutes",
        "tonosama_first_tp_done", "tonosama_second_tp_done",
        "exchange", "margin_trade_type", "account_type", "hold_id", "execution_id",
        "current_price", "CurrentPrice",
    ]:
        try:
            if hasattr(pos, name):
                out[name] = getattr(pos, name)
        except Exception:
            pass
    return out


def _normalize_broker_side(pos: Dict[str, Any]) -> str:
    """kabu StationのSide/TradeType系をBUY/SELLへ寄せる。"""
    raw = dict_get_any(pos, "side", "Side", "position_side", "trade_side", "SellBuy", default="")
    s = str(raw or "").strip().upper()
    if s in {"BUY", "LONG", "2", "02", "20", "B", "信用買", "買", "買建"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "01", "10", "S", "信用売", "売", "売建"}:
        return "SELL"
    return s


def _normalize_broker_position(symbol: str, pos: Dict[str, Any], *, source: str) -> Dict[str, Any]:
    out = dict(pos)
    out["symbol"] = normalize_symbol(out.get("symbol") or out.get("Symbol") or symbol)
    out.setdefault("status", "OPEN")
    out["_position_source"] = source

    side = _normalize_broker_side(out)
    if side in {"BUY", "SELL"}:
        out["side"] = side

    qty = dict_get_any(out, "qty", "quantity", "LeavesQty", "HoldQty", "Qty", default=None)
    if qty not in (None, ""):
        out["qty"] = qty

    entry_price = dict_get_any(
        out,
        "entry_price", "avg_price", "AveragePrice", "AvgPrice", "ExecutionPrice", "Price", "price",
        default=None,
    )
    if entry_price not in (None, ""):
        out.setdefault("entry_price", entry_price)
        out.setdefault("avg_price", entry_price)

    current_price = dict_get_any(out, "current_price", "CurrentPrice", "PresentPrice", "last_price", default=None)
    if current_price not in (None, ""):
        out["current_price"] = current_price

    return out


def _merge_position_map(dst: Dict[str, Dict[str, Any]], src: Any, *, source: str) -> None:
    if not isinstance(src, dict):
        return

    for key, value in list(src.items()):
        try:
            pos = _position_to_dict(value)
            symbol = normalize_symbol(pos.get("symbol") or pos.get("Symbol") or key)
            if not symbol:
                continue

            status = str(pos.get("status") or "OPEN").upper()
            if status in {"CLOSED", "DONE", "CANCELLED", "CANCELED"}:
                continue

            pos = _normalize_broker_position(symbol, pos, source=source)

            if symbol in dst:
                merged = dict(dst[symbol])
                for k, v in pos.items():
                    if v is not None and v != "":
                        merged[k] = v
                dst[symbol] = merged
            else:
                dst[symbol] = pos

        except Exception:
            logger.debug("[EXIT POS MERGE] skip key=%s source=%s", key, source, exc_info=True)


def _snapshot_gc_positions() -> Dict[str, Any]:
    try:
        positions_obj = getattr(GC, "positions", None)
        if positions_obj is None:
            return {}

        for method_name in ["snapshot_open", "snapshot_dict", "get_open_positions", "to_dict"]:
            try:
                fn = getattr(positions_obj, method_name, None)
                if callable(fn):
                    ret = fn() or {}
                    if isinstance(ret, dict) and ret:
                        return ret
            except Exception:
                logger.debug("[EXIT POS] GC.positions.%s failed", method_name, exc_info=True)

        for attr in ["open_positions", "positions"]:
            try:
                ret = getattr(positions_obj, attr, None)
                if isinstance(ret, dict) and ret:
                    return dict(ret)
            except Exception:
                pass
    except Exception:
        logger.exception("[POSITION_SNAPSHOT_ERROR] GC.positions")
    return {}


def _sync_db_open_positions_safe() -> Dict[str, Dict[str, Any]]:
    try:
        from trading.position.open_position_sync import sync_open_positions_from_db
        return sync_open_positions_from_db(force_log=False) or {}
    except Exception:
        logger.exception("[EXIT POS] DB open position sync failed")
        return {}


def _read_broker_open_positions_safe() -> Dict[str, Dict[str, Any]]:
    """kabu Station の実信用建玉を直接読む。DB/メモリに無くてもEXIT監視対象へ入れる。"""
    try:
        from trading.position.kabu_position_reader import read_kabu_open_positions
        rows = read_kabu_open_positions() or {}
        if not isinstance(rows, dict):
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for key, value in rows.items():
            if not isinstance(value, dict):
                continue
            symbol = normalize_symbol(value.get("symbol") or value.get("Symbol") or key)
            if not symbol:
                continue
            out[symbol] = _normalize_broker_position(symbol, value, source="KABU.positions.credit_only.exit_loop_direct")
        return out
    except Exception:
        logger.exception("[EXIT POS] broker open position read failed")
        return {}


def _publish_open_positions_to_memory(positions: Dict[str, Dict[str, Any]]) -> None:
    if not positions:
        return
    try:
        gd = getattr(global_data, "open_positions", None)
        if not isinstance(gd, dict):
            gd = {}
            setattr(global_data, "open_positions", gd)
        for symbol, pos in positions.items():
            gd[symbol] = pos
    except Exception:
        logger.debug("[EXIT POS] publish to global_data failed", exc_info=True)

    try:
        positions_obj = getattr(GC, "positions", None)
        if positions_obj is not None:
            for method_name in ["upsert", "set", "add", "set_position"]:
                fn = getattr(positions_obj, method_name, None)
                if callable(fn):
                    for symbol, pos in positions.items():
                        try:
                            fn(symbol, pos)
                        except TypeError:
                            try:
                                fn(pos)
                            except Exception:
                                pass
                    break
    except Exception:
        logger.debug("[EXIT POS] publish to GC.positions failed", exc_info=True)


def get_open_positions_safe() -> Dict[str, Dict[str, Any]]:
    """
    EXIT監視対象の open positions を安全に取得する。

    優先して全ソースをマージする:
      1. DB positions(status=OPEN)
      2. GC.positions
      3. global_data.open_positions
      4. kabu Station 実信用建玉
    """
    merged: Dict[str, Dict[str, Any]] = {}

    try:
        _merge_position_map(merged, _sync_db_open_positions_safe(), source="DB.positions")
    except Exception:
        logger.exception("[POSITION_SNAPSHOT_ERROR] merge DB.positions")

    try:
        _merge_position_map(merged, _snapshot_gc_positions(), source="GC.positions")
    except Exception:
        logger.exception("[POSITION_SNAPSHOT_ERROR] merge GC.positions")

    try:
        gd_pos = getattr(global_data, "open_positions", None)
        _merge_position_map(merged, gd_pos, source="global_data.open_positions")
    except Exception:
        logger.exception("[POSITION_SNAPSHOT_ERROR] merge global_data.open_positions")

    try:
        broker_pos = _read_broker_open_positions_safe()
        _merge_position_map(merged, broker_pos, source="KABU.positions.credit_only.exit_loop_direct")
    except Exception:
        logger.exception("[POSITION_SNAPSHOT_ERROR] merge broker positions")

    try:
        _publish_open_positions_to_memory(merged)
    except Exception:
        logger.debug("[EXIT POS] publish merged positions failed", exc_info=True)

    if merged:
        logger.info("[EXIT LOOP] open positions detected count=%s symbols=%s", len(merged), sorted(merged.keys()))
    else:
        logger.warning("[EXIT LOOP] no open positions from DB/GC/global_data/broker")

    return merged


def get_holding_seconds_safe(ctx: Any, now: dt.datetime) -> int:
    try:
        if ctx is not None and hasattr(ctx, "holding_seconds"):
            return int(ctx.holding_seconds(now))
    except Exception:
        pass
    return 0


def get_feature_value(features: Dict[str, Any], ctx: Any, *names: str, default: float = 0.0) -> float:
    for name in names:
        try:
            if isinstance(features, dict) and name in features:
                return safe_float(features.get(name), default)
        except Exception:
            pass
    for name in names:
        try:
            if ctx is not None and hasattr(ctx, name):
                return safe_float(getattr(ctx, name), default)
        except Exception:
            pass
    return default


def get_feature_value_or_none(features: Dict[str, Any], ctx: Any, pos: Optional[Dict[str, Any]], *names: str) -> Optional[float]:
    for name in names:
        try:
            if isinstance(features, dict) and name in features:
                v = safe_float_or_none(features.get(name))
                if v is not None:
                    return v
        except Exception:
            pass
    for name in names:
        try:
            if ctx is not None and hasattr(ctx, name):
                v = safe_float_or_none(getattr(ctx, name))
                if v is not None:
                    return v
        except Exception:
            pass
    for name in names:
        try:
            if isinstance(pos, dict) and name in pos:
                v = safe_float_or_none(pos.get(name))
                if v is not None:
                    return v
        except Exception:
            pass
    return None


def get_feature_int_or_none(features: Dict[str, Any], ctx: Any, pos: Optional[Dict[str, Any]], *names: str) -> Optional[int]:
    for name in names:
        try:
            if isinstance(features, dict) and name in features:
                v = safe_int_or_none(features.get(name))
                if v is not None:
                    return v
        except Exception:
            pass
    for name in names:
        try:
            if ctx is not None and hasattr(ctx, name):
                v = safe_int_or_none(getattr(ctx, name))
                if v is not None:
                    return v
        except Exception:
            pass
    for name in names:
        try:
            if isinstance(pos, dict) and name in pos:
                v = safe_int_or_none(pos.get(name))
                if v is not None:
                    return v
        except Exception:
            pass
    return None


def get_feature_bool_or_none(features: Dict[str, Any], ctx: Any, pos: Optional[Dict[str, Any]], *names: str) -> Optional[bool]:
    for name in names:
        try:
            if isinstance(features, dict) and name in features:
                v = safe_bool_or_none(features.get(name))
                if v is not None:
                    return v
        except Exception:
            pass
    for name in names:
        try:
            if ctx is not None and hasattr(ctx, name):
                v = safe_bool_or_none(getattr(ctx, name))
                if v is not None:
                    return v
        except Exception:
            pass
    for name in names:
        try:
            if isinstance(pos, dict) and name in pos:
                v = safe_bool_or_none(pos.get(name))
                if v is not None:
                    return v
        except Exception:
            pass
    return None


__all__ = [
    "safe_float",
    "safe_float_or_none",
    "safe_int_or_none",
    "safe_bool",
    "safe_bool_or_none",
    "safe_str",
    "normalize_symbol",
    "dict_get_any",
    "attr_get_any",
    "get_open_positions_safe",
    "get_holding_seconds_safe",
    "get_feature_value",
    "get_feature_value_or_none",
    "get_feature_int_or_none",
    "get_feature_bool_or_none",
]
