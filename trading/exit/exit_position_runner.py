# ============================================================
# File   : trading/exit/exit_position_runner.py
# Version: V1.7-PARTIAL-PROFIT-TAKE-TRAIL
# ------------------------------------------------------------
# 【概要】
#   1銘柄分のEXIT判定を担当。
#
# 【利大修正】
#   - +0.20%到達で半分だけ一部利確。
#   - 残り建玉はOPENのまま残し、高値/安値トレーリングで伸ばす。
#   - 一部利確後は同じ建玉で二重に一部利確しない。
#
# 【最優先損切り】
#   BUY : 現在値 <= 建値 * 0.997 で即返済
#   SELL: 現在値 >= 建値 * 1.003 で即返済
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Dict, Optional, Tuple

from core.global_context.context import global_context as GC
from global_state import global_data
from trading.exit.ai_exit_runner import apply_ai_exit_if_needed
from trading.exit.early_profit_guard import judge_early_profit_guard
from trading.exit.exit_features import build_exit_features_safe, inject_daily_features_safe
from trading.exit.exit_finalize import finalize_exit
from trading.exit.exit_context import ExitContext
from trading.exit.exit_price_source import get_latest_exit_price
from trading.exit.exit_state_machine import manage_exit
from trading.exit.exit_utils import safe_float
from trading.exit.partial_profit_executor import execute_partial_profit
from trading.exit.tonosama_exit_runner import apply_tonosama_exit_if_needed

logger = logging.getLogger(__name__)


# ============================================================
# EXIT 設定
# ============================================================

ABSOLUTE_ENTRY_STOP_LOSS_PCT = float(os.getenv("ABSOLUTE_ENTRY_STOP_LOSS_PCT", "0.30"))
THREE_MIN_PROFIT_ESCAPE_SEC = int(float(os.getenv("THREE_MIN_PROFIT_ESCAPE_SEC", "180")))
THREE_MIN_PROFIT_ESCAPE_TARGET_PCT = float(os.getenv("THREE_MIN_PROFIT_ESCAPE_TARGET_PCT", "0.35"))
THREE_MIN_PROFIT_ESCAPE_MIN_CURRENT_PCT = float(os.getenv("THREE_MIN_PROFIT_ESCAPE_MIN_CURRENT_PCT", "0.00"))
THREE_MIN_FLAT_EXIT_ABS_PCT = float(os.getenv("THREE_MIN_FLAT_EXIT_ABS_PCT", "0.10"))
ENTRY_TRAIL_RETRACE_EXIT_PCT = float(os.getenv("ENTRY_TRAIL_RETRACE_EXIT_PCT", "0.25"))

PARTIAL_PROFIT_ENABLED = str(os.getenv("PARTIAL_PROFIT_ENABLED", "1")).lower() not in {"0", "false", "no", "off"}
PARTIAL_PROFIT_TRIGGER_PCT = float(os.getenv("PARTIAL_PROFIT_TRIGGER_PCT", "0.20"))
PARTIAL_PROFIT_RATIO = float(os.getenv("PARTIAL_PROFIT_RATIO", "0.50"))
PARTIAL_PROFIT_MIN_QTY = int(float(os.getenv("PARTIAL_PROFIT_MIN_QTY", "200")))

_THREE_MIN_ESCAPE_MARK_ATTR = "three_min_profit_escape_fired"
_TRAIL_RETRACE_MARK_ATTR = "entry_trail_retrace_exit_fired"
_FLAT_EXIT_MARK_ATTR = "three_min_flat_exit_fired"
_PARTIAL_PROFIT_MARK_ATTR = "partial_profit_taken"


def _pos_get(pos: Dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        try:
            if isinstance(pos, dict) and name in pos:
                return pos.get(name)
            if hasattr(pos, name):
                return getattr(pos, name)
        except Exception:
            continue
    return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _resolve_entry_price(pos: Dict[str, Any]) -> float:
    """建値専用価格。CurrentPrice/current_priceは現在値なので使わない。"""
    preferred_keys = (
        "avg_price",
        "entry_price",
        "AveragePrice",
        "average_price",
        "AvgPrice",
        "ExecutionPrice",
        "execution_price",
        "filled_price",
        "contract_price",
        "hold_price",
        "entry_order_price",
        "entry_fill_price",
    )
    for key in preferred_keys:
        v = safe_float(_pos_get(pos, key, default=0.0), 0.0)
        if v > 0:
            return v

    try:
        src = str(_pos_get(pos, "_position_source", default="") or "").upper()
        if "DB" in src:
            v = safe_float(_pos_get(pos, "Price", "price", default=0.0), 0.0)
            if v > 0:
                return v
    except Exception:
        pass
    return 0.0


def _normalize_side(side: Any) -> str:
    raw = side
    s = str(side or "").upper().strip()
    buy_values = {"BUY", "BUY_CREDIT", "LONG", "L", "2", "02", "20", "B", "信用買", "買", "買建", "買い", "新規買", "返済売"}
    sell_values = {"SELL", "SELL_CREDIT", "SHORT", "S", "1", "01", "10", "信用売", "売", "売建", "売り", "新規売", "返済買"}
    if s in buy_values:
        return "BUY"
    if s in sell_values:
        return "SELL"
    try:
        if isinstance(raw, dict):
            for key in ("side", "Side", "trade_side", "position_side", "order_side"):
                if key in raw:
                    return _normalize_side(raw.get(key))
    except Exception:
        pass
    return s


def _fallback_entry_time(pos: Dict[str, Any], now: dt.datetime) -> dt.datetime:
    raw = _pos_get(pos, "entry_time", "created_at", "timestamp", default=None)
    if isinstance(raw, dt.datetime):
        try:
            if raw.tzinfo is not None:
                return raw.replace(tzinfo=None)
        except Exception:
            pass
        return raw
    try:
        s = str(raw or "").strip()
        if not s:
            return now
        s = s.replace("T", " ").split("+", 1)[0]
        if s.endswith("Z"):
            s = s[:-1]
        return dt.datetime.fromisoformat(s)
    except Exception:
        return now


def _resolve_exit_ctx(symbol: str, pos: Dict[str, Any], *, side: str, entry_price: float, now: dt.datetime) -> Any:
    ctx = None
    try:
        if hasattr(GC, "ai") and GC.ai and hasattr(GC.ai, "get_exit_ctx"):
            ctx = GC.ai.get_exit_ctx(symbol)
            if ctx is not None:
                return ctx
    except Exception:
        logger.debug("[EXIT] GC.ai ctx lookup failed symbol=%s", symbol, exc_info=True)

    try:
        legacy = getattr(global_data, "exit_ctx", None)
        if isinstance(legacy, dict):
            ctx = legacy.get(symbol)
            if ctx is not None:
                try:
                    if hasattr(GC, "ai") and GC.ai and hasattr(GC.ai, "set_exit_ctx"):
                        GC.ai.set_exit_ctx(symbol, ctx)
                except Exception:
                    pass
                return ctx
    except Exception:
        logger.debug("[EXIT] legacy ctx lookup failed symbol=%s", symbol, exc_info=True)

    atr = safe_float(_pos_get(pos, "atr_1min", "atr", default=0.0), 0.0)
    entry_time = _fallback_entry_time(pos, now)
    try:
        ctx = ExitContext(symbol=str(symbol), side=side, entry_price=float(entry_price), atr_1min=max(float(atr), 0.0), entry_time=entry_time)
        try:
            if hasattr(GC, "ai") and GC.ai and hasattr(GC.ai, "set_exit_ctx"):
                GC.ai.set_exit_ctx(symbol, ctx)
        except Exception:
            pass
        logger.warning("[EXIT] fallback ExitContext created symbol=%s side=%s entry=%.4f atr=%.4f entry_time=%s", symbol, side, entry_price, atr, entry_time)
        return ctx
    except Exception:
        logger.exception("[EXIT] fallback ExitContext create failed symbol=%s", symbol)
        return None


def _calc_current_profit_pct(*, side: str, entry_price: float, current_price: float) -> float:
    if entry_price <= 0 or current_price <= 0:
        return 0.0
    if side == "BUY":
        return (current_price - entry_price) / entry_price * 100.0
    return (entry_price - current_price) / entry_price * 100.0


def _get_mfe_pct(ctx: Any, current_profit_pct: float) -> float:
    try:
        mfe_pct = float(getattr(ctx, "mfe_pct", 0.0) or 0.0)
    except Exception:
        mfe_pct = 0.0
    if current_profit_pct > mfe_pct:
        mfe_pct = current_profit_pct
    return mfe_pct


def _already_marked(symbol: str, ctx: Any, pos: Dict[str, Any], attr: str, set_name: str) -> bool:
    try:
        if bool(getattr(ctx, attr, False)):
            return True
    except Exception:
        pass
    try:
        if isinstance(pos, dict) and bool(pos.get(attr)):
            return True
    except Exception:
        pass
    try:
        mark = getattr(global_data, set_name, None)
        if isinstance(mark, set) and str(symbol) in mark:
            return True
    except Exception:
        pass
    return False


def _mark_fired(symbol: str, ctx: Any, pos: Dict[str, Any], attr: str, set_name: str) -> None:
    try:
        if ctx is not None:
            setattr(ctx, attr, True)
    except Exception:
        pass
    try:
        if isinstance(pos, dict):
            pos[attr] = True
    except Exception:
        pass
    try:
        mark = getattr(global_data, set_name, None)
        if not isinstance(mark, set):
            mark = set()
            setattr(global_data, set_name, mark)
        mark.add(str(symbol))
    except Exception:
        pass


def _judge_absolute_entry_stop_loss(*, symbol: str, side: str, entry_price: float, current_price: float) -> Tuple[bool, str, Dict[str, Any]]:
    try:
        if entry_price <= 0 or current_price <= 0 or side not in {"BUY", "SELL"}:
            return False, "", {}
        threshold_pct = float(ABSOLUTE_ENTRY_STOP_LOSS_PCT)
        threshold_ratio = threshold_pct / 100.0
        current_profit_pct = _calc_current_profit_pct(side=side, entry_price=entry_price, current_price=current_price)

        if side == "BUY":
            stop_price = entry_price * (1.0 - threshold_ratio)
            adverse_pct = (entry_price - current_price) / entry_price * 100.0
            detail = {"side": side, "entry_price": entry_price, "current_price": current_price, "stop_price": stop_price, "adverse_pct": adverse_pct, "threshold_pct": threshold_pct, "current_profit_pct": current_profit_pct}
            if current_price <= stop_price:
                reason = f"ABSOLUTE_ENTRY_STOP_LOSS_BUY entry={entry_price:.4f} current={current_price:.4f} stop={stop_price:.4f} adverse={adverse_pct:.3f}%>=threshold={threshold_pct:.3f}%"
                return True, reason, detail
            return False, "", detail

        stop_price = entry_price * (1.0 + threshold_ratio)
        adverse_pct = (current_price - entry_price) / entry_price * 100.0
        detail = {"side": side, "entry_price": entry_price, "current_price": current_price, "stop_price": stop_price, "adverse_pct": adverse_pct, "threshold_pct": threshold_pct, "current_profit_pct": current_profit_pct}
        if current_price >= stop_price:
            reason = f"ABSOLUTE_ENTRY_STOP_LOSS_SELL entry={entry_price:.4f} current={current_price:.4f} stop={stop_price:.4f} adverse={adverse_pct:.3f}%>=threshold={threshold_pct:.3f}%"
            return True, reason, detail
        return False, "", detail
    except Exception:
        logger.exception("[ABSOLUTE_ENTRY_STOP_LOSS] judge failed symbol=%s", symbol)
        return False, "", {}


def _judge_partial_profit_take(*, symbol: str, pos: Dict[str, Any], side: str, entry_price: float, current_price: float, ctx: Any) -> Tuple[bool, str, Dict[str, Any]]:
    """+0.20%到達で半分だけ利確。すでに一部利確済みなら何もしない。"""
    try:
        if not PARTIAL_PROFIT_ENABLED:
            return False, "", {}
        if _already_marked(symbol, ctx, pos, _PARTIAL_PROFIT_MARK_ATTR, "partial_profit_taken_symbols"):
            return False, "", {}
        qty = _safe_int(_pos_get(pos, "qty", "quantity", default=0), 0)
        if qty < int(PARTIAL_PROFIT_MIN_QTY):
            return False, "", {"qty": qty, "min_qty": PARTIAL_PROFIT_MIN_QTY}
        current_profit_pct = _calc_current_profit_pct(side=side, entry_price=entry_price, current_price=current_price)
        detail = {"side": side, "entry_price": entry_price, "current_price": current_price, "current_profit_pct": current_profit_pct, "trigger_pct": PARTIAL_PROFIT_TRIGGER_PCT, "ratio": PARTIAL_PROFIT_RATIO, "qty": qty}
        if current_profit_pct >= PARTIAL_PROFIT_TRIGGER_PCT:
            reason = f"PARTIAL_PROFIT_TAKE profit={current_profit_pct:.3f}%>=trigger={PARTIAL_PROFIT_TRIGGER_PCT:.3f}% ratio={PARTIAL_PROFIT_RATIO:.2f}"
            return True, reason, detail
        return False, "", detail
    except Exception:
        logger.exception("[PARTIAL PROFIT] judge failed symbol=%s", symbol)
        return False, "", {}


def _judge_entry_trail_retrace_exit(*, symbol: str, pos: Dict[str, Any], side: str, entry_price: float, current_price: float, ctx: Any) -> Tuple[bool, str, Dict[str, Any]]:
    try:
        if _already_marked(symbol, ctx, pos, _TRAIL_RETRACE_MARK_ATTR, "entry_trail_retrace_exit_fired_symbols"):
            return False, "", {}
        if not ctx or entry_price <= 0 or current_price <= 0:
            return False, "", {}
        if side == "BUY":
            high = safe_float(getattr(ctx, "highest", entry_price), entry_price)
            if high <= entry_price:
                return False, "", {}
            retrace_pct = (high - current_price) / high * 100.0
            current_profit_pct = _calc_current_profit_pct(side=side, entry_price=entry_price, current_price=current_price)
            detail = {"side": side, "entry_price": entry_price, "current_price": current_price, "highest": high, "retrace_pct": retrace_pct, "threshold_pct": ENTRY_TRAIL_RETRACE_EXIT_PCT, "current_profit_pct": current_profit_pct}
            if retrace_pct >= ENTRY_TRAIL_RETRACE_EXIT_PCT:
                reason = f"ENTRY_TRAIL_RETRACE_EXIT_BUY high={high:.4f} current={current_price:.4f} retrace={retrace_pct:.3f}%>=threshold={ENTRY_TRAIL_RETRACE_EXIT_PCT:.3f}%"
                return True, reason, detail
            return False, "", detail
        if side == "SELL":
            low = safe_float(getattr(ctx, "lowest", entry_price), entry_price)
            if low >= entry_price:
                return False, "", {}
            retrace_pct = (current_price - low) / low * 100.0 if low > 0 else 0.0
            current_profit_pct = _calc_current_profit_pct(side=side, entry_price=entry_price, current_price=current_price)
            detail = {"side": side, "entry_price": entry_price, "current_price": current_price, "lowest": low, "retrace_pct": retrace_pct, "threshold_pct": ENTRY_TRAIL_RETRACE_EXIT_PCT, "current_profit_pct": current_profit_pct}
            if retrace_pct >= ENTRY_TRAIL_RETRACE_EXIT_PCT:
                reason = f"ENTRY_TRAIL_RETRACE_EXIT_SELL low={low:.4f} current={current_price:.4f} retrace={retrace_pct:.3f}%>=threshold={ENTRY_TRAIL_RETRACE_EXIT_PCT:.3f}%"
                return True, reason, detail
            return False, "", detail
        return False, "", {}
    except Exception:
        logger.exception("[ENTRY_TRAIL_RETRACE_EXIT] judge failed symbol=%s", symbol)
        return False, "", {}


def _judge_three_min_exit_rules(*, symbol: str, pos: Dict[str, Any], side: str, entry_price: float, current_price: float, ctx: Any, now: dt.datetime) -> Tuple[bool, str, Dict[str, Any], str, str]:
    try:
        if not ctx or entry_price <= 0 or current_price <= 0:
            return False, "", {}, "", ""
        try:
            hold_sec = int(ctx.holding_seconds(now)) if hasattr(ctx, "holding_seconds") else 0
        except Exception:
            hold_sec = 0
        if hold_sec < THREE_MIN_PROFIT_ESCAPE_SEC:
            return False, "", {"hold_sec": hold_sec, "need_sec": THREE_MIN_PROFIT_ESCAPE_SEC}, "", ""
        current_profit_pct = _calc_current_profit_pct(side=side, entry_price=entry_price, current_price=current_price)
        mfe_pct = _get_mfe_pct(ctx, current_profit_pct)
        base_detail = {"hold_sec": hold_sec, "need_sec": THREE_MIN_PROFIT_ESCAPE_SEC, "entry_price": entry_price, "current_price": current_price, "side": side, "current_profit_pct": current_profit_pct, "mfe_pct": mfe_pct, "target_pct": THREE_MIN_PROFIT_ESCAPE_TARGET_PCT, "flat_abs_pct": THREE_MIN_FLAT_EXIT_ABS_PCT, "min_current_pct": THREE_MIN_PROFIT_ESCAPE_MIN_CURRENT_PCT}
        if not _already_marked(symbol, ctx, pos, _FLAT_EXIT_MARK_ATTR, "three_min_flat_exit_fired_symbols"):
            if abs(current_profit_pct) <= THREE_MIN_FLAT_EXIT_ABS_PCT:
                reason = f"THREE_MIN_FLAT_EXIT hold={hold_sec}s current={current_profit_pct:.3f}% within=±{THREE_MIN_FLAT_EXIT_ABS_PCT:.3f}%"
                return True, reason, base_detail, _FLAT_EXIT_MARK_ATTR, "three_min_flat_exit_fired_symbols"
        if not _already_marked(symbol, ctx, pos, _THREE_MIN_ESCAPE_MARK_ATTR, "three_min_profit_escape_fired_symbols"):
            if mfe_pct < THREE_MIN_PROFIT_ESCAPE_TARGET_PCT and current_profit_pct > THREE_MIN_PROFIT_ESCAPE_MIN_CURRENT_PCT:
                reason = f"THREE_MIN_PROFIT_ESCAPE hold={hold_sec}s mfe={mfe_pct:.3f}%<target={THREE_MIN_PROFIT_ESCAPE_TARGET_PCT:.3f}% current={current_profit_pct:.3f}%>min={THREE_MIN_PROFIT_ESCAPE_MIN_CURRENT_PCT:.3f}%"
                return True, reason, base_detail, _THREE_MIN_ESCAPE_MARK_ATTR, "three_min_profit_escape_fired_symbols"
        return False, "", base_detail, "", ""
    except Exception:
        logger.exception("[THREE_MIN_EXIT_RULES] judge failed symbol=%s", symbol)
        return False, "", {}, "", ""


def evaluate_collapse(symbol: str, regime: int, features: Dict[str, Any], side: str) -> Tuple[float, int, Optional[str]]:
    collapse_prob = 0.0
    inago_state = 0
    full_reason = None
    try:
        if not hasattr(GC, "ai") or not GC.ai:
            return collapse_prob, inago_state, full_reason
        collapse_engine = GC.ai.get_collapse_engine()
        if collapse_engine:
            result = collapse_engine.evaluate(symbol=symbol, regime=regime, pre_feature_dict=features, regime_feature_dict=features)
            collapse_prob = safe_float(result.get("strength"))
            if result.get("exit_ratio", 0) >= 1.0:
                full_reason = "COLLAPSE_ENGINE_FULL"
                return collapse_prob, inago_state, full_reason
        collapse_model = GC.ai.get_collapse_model()
        if collapse_model:
            legacy_prob = collapse_model.predict_proba(features, side=side)
            collapse_prob = max(collapse_prob, safe_float(legacy_prob))
        inago_model = GC.ai.get_inago_model()
        if inago_model:
            ignite_prob, exhaust_prob = inago_model.predict(features)
            ignite_prob = safe_float(ignite_prob)
            exhaust_prob = safe_float(exhaust_prob)
            if ignite_prob > 0.7:
                inago_state = 1
            elif exhaust_prob > 0.6:
                inago_state = 2
    except Exception:
        logger.exception("[COLLAPSE/INAGO_ERROR]")
    return collapse_prob, inago_state, full_reason


def evaluate_rl(symbol: str, regime: int, cluster_id: int, inago_state: int, pnl: float):
    try:
        if not hasattr(GC, "ai") or not GC.ai:
            return None, None
        rl_agent = GC.ai.get_rl_agent()
        if not rl_agent:
            return None, None
        rl_state = rl_agent.encode_state(regime, cluster_id, inago_state, pnl)
        rl_action = rl_agent.select_action(rl_state)
        return rl_action, rl_state
    except Exception:
        logger.exception("[RL_ERROR]")
        return None, None


def run_exit_for_position(*, symbol: str, pos: Dict[str, Any], now: dt.datetime, regime: int, boost_active: bool) -> bool:
    try:
        entry_price = _resolve_entry_price(pos)
        side = _normalize_side(_pos_get(pos, "side", "Side", "trade_side", "position_side", "order_side"))
        if not entry_price:
            logger.warning("[EXIT] skip no entry_price symbol=%s pos_keys=%s pos=%s reason=current_price_not_used_as_entry", symbol, list(pos.keys()) if isinstance(pos, dict) else type(pos), pos)
            return False
        if side not in ("BUY", "SELL"):
            logger.warning("[EXIT] skip invalid side symbol=%s side=%s pos_keys=%s", symbol, side, list(pos.keys()) if isinstance(pos, dict) else type(pos))
            return False
        price, bar5s = get_latest_exit_price(symbol)
        if not price:
            logger.warning("[EXIT] skip no latest price symbol=%s", symbol)
            return False
        pnl = price - entry_price if side == "BUY" else entry_price - price
        pnl = safe_float(pnl)

        abs_stop, abs_reason, abs_detail = _judge_absolute_entry_stop_loss(symbol=symbol, side=side, entry_price=entry_price, current_price=price)
        if abs_stop:
            logger.warning("[ABSOLUTE_ENTRY_STOP_LOSS] EXIT_RETRY symbol=%s detail=%s reason=%s", symbol, abs_detail, abs_reason)
            finalize_exit(symbol=symbol, price=price, reason=abs_reason, cluster_id=0, regime=regime, inago_state=0, pnl=pnl, collapse_prob=0.0, ctx=None)
            return True

        logger.warning("[ABSOLUTE_ENTRY_STOP_LOSS] check symbol=%s side=%s entry=%.4f price=%.4f pnl=%.4f current_profit=%.4f%% threshold=%.4f%% pos_source=%s", symbol, side, entry_price, price, pnl, _calc_current_profit_pct(side=side, entry_price=entry_price, current_price=price), ABSOLUTE_ENTRY_STOP_LOSS_PCT, _pos_get(pos, "_position_source", default=""))

        ctx = _resolve_exit_ctx(symbol, pos, side=side, entry_price=entry_price, now=now)
        if not ctx:
            logger.warning("[EXIT] skip ctx unavailable symbol=%s side=%s entry=%.4f", symbol, side, entry_price)
            return False
        try:
            if hasattr(ctx, "update_price"):
                ctx.update_price(float(price))
        except Exception:
            logger.debug("[EXIT] ctx.update_price failed symbol=%s price=%s", symbol, price, exc_info=True)

        features = build_exit_features_safe(ctx, price, pnl)
        features = inject_daily_features_safe(symbol, features)
        try:
            cluster_id = GC.positions.get_cluster(symbol) or 0
        except Exception:
            cluster_id = 0

        partial_exit, partial_reason, partial_detail = _judge_partial_profit_take(symbol=symbol, pos=pos, side=side, entry_price=entry_price, current_price=price, ctx=ctx)
        if partial_exit:
            logger.warning("[PARTIAL PROFIT] TAKE_CHECK symbol=%s detail=%s reason=%s", symbol, partial_detail, partial_reason)
            ok = execute_partial_profit(symbol=symbol, pos=pos, reason=partial_reason, exit_price=price, ratio=PARTIAL_PROFIT_RATIO)
            if ok:
                _mark_fired(symbol, ctx, pos, _PARTIAL_PROFIT_MARK_ATTR, "partial_profit_taken_symbols")
                logger.warning("[PARTIAL PROFIT] TAKE_DONE symbol=%s reason=%s", symbol, partial_reason)
                return True
            logger.warning("[PARTIAL PROFIT] TAKE_FAILED continue normal exit symbol=%s reason=%s", symbol, partial_reason)

        trail_exit, trail_reason, trail_detail = _judge_entry_trail_retrace_exit(symbol=symbol, pos=pos, side=side, entry_price=entry_price, current_price=price, ctx=ctx)
        if trail_exit:
            logger.warning("[ENTRY_TRAIL_RETRACE_EXIT] EXIT symbol=%s detail=%s reason=%s", symbol, trail_detail, trail_reason)
            _mark_fired(symbol, ctx, pos, _TRAIL_RETRACE_MARK_ATTR, "entry_trail_retrace_exit_fired_symbols")
            finalize_exit(symbol=symbol, price=price, reason=trail_reason, cluster_id=cluster_id, regime=regime, inago_state=0, pnl=pnl, collapse_prob=0.0, ctx=ctx)
            return True

        three_min_exit, three_min_reason, three_min_detail, mark_attr, mark_set_name = _judge_three_min_exit_rules(symbol=symbol, pos=pos, side=side, entry_price=entry_price, current_price=price, ctx=ctx, now=now)
        if three_min_exit:
            logger.warning("[THREE_MIN_EXIT_RULE] EXIT symbol=%s detail=%s reason=%s", symbol, three_min_detail, three_min_reason)
            if mark_attr and mark_set_name:
                _mark_fired(symbol, ctx, pos, mark_attr, mark_set_name)
            finalize_exit(symbol=symbol, price=price, reason=three_min_reason, cluster_id=cluster_id, regime=regime, inago_state=0, pnl=pnl, collapse_prob=0.0, ctx=ctx)
            return True

        early_exit, early_reason = judge_early_profit_guard(symbol=symbol, pos=pos, side=side, entry_price=entry_price, current_price=price, ctx=ctx, now=now, bar5s=bar5s)
        if early_exit:
            finalize_exit(symbol=symbol, price=price, reason=early_reason, cluster_id=cluster_id, regime=regime, inago_state=0, pnl=pnl, collapse_prob=0.0, ctx=ctx)
            return True

        collapse_prob, inago_state, full_reason = evaluate_collapse(symbol, regime, features, side)
        features["collapse_prob"] = collapse_prob
        if apply_tonosama_exit_if_needed(symbol=symbol, pos=pos, side=side, price=price, entry_price=entry_price, features=features, ctx=ctx, now=now, cluster_id=cluster_id, regime=regime, inago_state=inago_state, pnl=pnl, collapse_prob=collapse_prob, bar5s=bar5s):
            return True
        if full_reason or collapse_prob > 0.85:
            finalize_exit(symbol=symbol, price=price, reason=full_reason or "COLLAPSE_EXIT", cluster_id=cluster_id, regime=regime, inago_state=inago_state, pnl=pnl, collapse_prob=collapse_prob, ctx=ctx)
            return True
        if apply_ai_exit_if_needed(symbol=symbol, side=side, price=price, entry_price=entry_price, pnl=pnl, features=features, ctx=ctx, now=now, cluster_id=cluster_id, regime=regime, inago_state=inago_state, collapse_prob=collapse_prob):
            return True
        if boost_active:
            atr = getattr(ctx, "atr_1min", 0.0)
            if atr and pnl > atr * 2:
                return False
        rl_action, rl_state = evaluate_rl(symbol, regime, cluster_id, inago_state, pnl)
        if rl_action in ("EXIT", "TAKE"):
            finalize_exit(symbol=symbol, price=price, reason=f"RL_{rl_action}", cluster_id=cluster_id, regime=regime, inago_state=inago_state, pnl=pnl, collapse_prob=collapse_prob, ctx=ctx, rl_state=rl_state)
            return True
        action, reason = manage_exit(ctx=ctx, price=price, now=now)
        if action == "EXIT":
            finalize_exit(symbol=symbol, price=price, reason=reason, cluster_id=cluster_id, regime=regime, inago_state=inago_state, pnl=pnl, collapse_prob=collapse_prob, ctx=ctx, rl_state=rl_state)
            return True
        return False
    except Exception:
        logger.exception("[EXIT_SYMBOL_FATAL] symbol=%s", symbol)
        return False


__all__ = ["run_exit_for_position", "evaluate_collapse", "evaluate_rl"]
