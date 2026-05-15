# ============================================================
# File   : core/startup/exit_trail_03_runtime_patch.py
# Version: V1.1-COST-AWARE
# ------------------------------------------------------------
# BUY : entry price -0.3% で返済
# BUY : entry後の最高値から -0.3% で返済
# SELL: entry price +0.3% で返済
# SELL: entry後の最安値から +0.3% で返済
#
# V1.1:
#   信用金利 0.01% と、返済成行の1ティック不利約定を考慮する。
#   表面利益ではなく effective_profit_pct をログ・一部利確判定に使う。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)
_INSTALLED = False


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _sym(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _state(symbol: str) -> Dict[str, Any]:
    try:
        from global_state import global_data
        root = getattr(global_data, "exit_trail_03_state", None)
        if not isinstance(root, dict):
            root = {}
            setattr(global_data, "exit_trail_03_state", root)
        s = _sym(symbol)
        d = root.get(s)
        if not isinstance(d, dict):
            d = {}
            root[s] = d
        return d
    except Exception:
        return {}


def _pos_get(pos: Any, *keys: str) -> Any:
    try:
        if isinstance(pos, dict):
            for k in keys:
                v = pos.get(k)
                if v not in (None, ""):
                    return v
        for k in keys:
            v = getattr(pos, k, None)
            if v not in (None, ""):
                return v
    except Exception:
        pass
    return None


def _tick_size(price: float) -> float:
    # 簡易版。細かい呼値単位は銘柄区分で変わるため、まずは1円を標準にする。
    # 必要なら環境変数 EXIT_MARKET_SLIPPAGE_TICK_SIZE_YEN で上書きする。
    return max(_env_float("EXIT_MARKET_SLIPPAGE_TICK_SIZE_YEN", 1.0), 0.0)


def _cost_params(current: float) -> tuple[float, float, float]:
    interest_pct = max(_env_float("EXIT_CREDIT_INTEREST_PCT", 0.01), 0.0)
    ticks = max(_env_float("EXIT_MARKET_SLIPPAGE_TICKS", 1.0), 0.0)
    tick_yen = _tick_size(current)
    return interest_pct, ticks, tick_yen


def _effective_exit_price(side: str, current: float) -> float:
    interest_pct, ticks, tick_yen = _cost_params(current)
    slip = ticks * tick_yen
    if side == "BUY":
        # BUY建玉の返済は売り。成行だと1ティック安く約定しやすい前提。
        return max(current - slip, 0.0)
    # SELL建玉の返済は買い。成行だと1ティック高く約定しやすい前提。
    return current + slip


def _profit_pct(side: str, entry: float, current: float) -> float:
    if entry <= 0 or current <= 0:
        return 0.0
    if side == "BUY":
        return (current - entry) / entry * 100.0
    return (entry - current) / entry * 100.0


def _effective_profit_pct(side: str, entry: float, current: float) -> float:
    if entry <= 0 or current <= 0:
        return 0.0
    interest_pct, _ticks, _tick_yen = _cost_params(current)
    effective_price = _effective_exit_price(side, current)
    return _profit_pct(side, entry, effective_price) - interest_pct


def _cost_detail(side: str, entry: float, current: float) -> Dict[str, Any]:
    interest_pct, ticks, tick_yen = _cost_params(current)
    effective_price = _effective_exit_price(side, current)
    raw_profit_pct = _profit_pct(side, entry, current)
    effective_profit_pct = _profit_pct(side, entry, effective_price) - interest_pct
    return {
        "interest_pct": interest_pct,
        "slippage_ticks": ticks,
        "tick_yen": tick_yen,
        "current_price": current,
        "effective_exit_price": effective_price,
        "raw_profit_pct": raw_profit_pct,
        "effective_profit_pct": effective_profit_pct,
    }


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        import trading.exit.exit_position_runner as r
    except Exception:
        logger.exception("[EXIT TRAIL 0.3 COST] import failed")
        return False

    r.ABSOLUTE_ENTRY_STOP_LOSS_PCT = 0.30
    r.ENTRY_TRAIL_RETRACE_EXIT_PCT = 0.30

    old_abs = getattr(r, "_judge_absolute_entry_stop_loss", None)
    old_trail = getattr(r, "_judge_entry_trail_retrace_exit", None)
    old_partial = getattr(r, "_judge_partial_profit_take", None)

    if callable(old_abs) and not getattr(old_abs, "_trail03_cost_wrapped", False):
        def abs03(*, symbol: str, side: str, entry_price: float, current_price: float):
            side_u = str(side or "").upper()
            entry = _f(entry_price)
            cur = _f(current_price)
            if entry <= 0 or cur <= 0 or side_u not in {"BUY", "SELL"}:
                return False, "", {}
            pct = 0.30
            cost = _cost_detail(side_u, entry, cur)
            effective_price = cost["effective_exit_price"]
            if side_u == "BUY":
                line = entry * 0.997
                adverse = (entry - effective_price) / entry * 100.0
                detail = {"side": side_u, "entry_price": entry, "line": line, "adverse_pct": adverse, "threshold_pct": pct, **cost}
                if effective_price <= line:
                    return True, f"ENTRY_PRICE_0P3_EXIT_BUY_COST entry={entry:.4f} effective={effective_price:.4f} current={cur:.4f} line={line:.4f}", detail
                return False, "", detail
            line = entry * 1.003
            adverse = (effective_price - entry) / entry * 100.0
            detail = {"side": side_u, "entry_price": entry, "line": line, "adverse_pct": adverse, "threshold_pct": pct, **cost}
            if effective_price >= line:
                return True, f"ENTRY_PRICE_0P3_EXIT_SELL_COST entry={entry:.4f} effective={effective_price:.4f} current={cur:.4f} line={line:.4f}", detail
            return False, "", detail
        abs03._trail03_cost_wrapped = True  # type: ignore[attr-defined]
        r._judge_absolute_entry_stop_loss = abs03

    if callable(old_trail) and not getattr(old_trail, "_trail03_cost_wrapped", False):
        def trail03(*, symbol: str, pos: Dict[str, Any], side: str, entry_price: float, current_price: float, ctx: Any):
            side_u = str(side or "").upper()
            entry = _f(entry_price)
            cur = _f(current_price)
            if entry <= 0 or cur <= 0 or side_u not in {"BUY", "SELL"}:
                return False, "", {}

            st = _state(symbol)
            old_high = _f(st.get("highest"))
            old_low = _f(st.get("lowest"))
            pos_high = _f(_pos_get(pos, "highest_since_entry", "highest", "high_since_entry"))
            pos_low = _f(_pos_get(pos, "lowest_since_entry", "lowest", "low_since_entry"))
            ctx_high = _f(getattr(ctx, "highest", 0.0)) if ctx is not None else 0.0
            ctx_low = _f(getattr(ctx, "lowest", 0.0)) if ctx is not None else 0.0

            high = max(x for x in [entry, cur, old_high, pos_high, ctx_high] if x > 0)
            low = min(x for x in [entry, cur, old_low, pos_low, ctx_low] if x > 0)

            st["highest"] = high
            st["lowest"] = low
            st["last_price"] = cur
            try:
                if isinstance(pos, dict):
                    pos["highest_since_entry"] = high
                    pos["lowest_since_entry"] = low
            except Exception:
                pass
            try:
                if ctx is not None:
                    setattr(ctx, "highest", high)
                    setattr(ctx, "lowest", low)
                    setattr(ctx, "last_price", cur)
            except Exception:
                pass

            cost = _cost_detail(side_u, entry, cur)
            effective_price = cost["effective_exit_price"]

            if side_u == "BUY":
                line = high * 0.997
                retrace = (high - effective_price) / high * 100.0 if high > 0 else 0.0
                detail = {"side": side_u, "entry_price": entry, "highest": high, "line": line, "retrace_pct": retrace, "threshold_pct": 0.30, **cost}
                if high > entry and effective_price <= line:
                    return True, f"HIGH_0P3_TRAIL_EXIT_BUY_COST high={high:.4f} effective={effective_price:.4f} current={cur:.4f} line={line:.4f}", detail
                return False, "", detail

            line = low * 1.003
            retrace = (effective_price - low) / low * 100.0 if low > 0 else 0.0
            detail = {"side": side_u, "entry_price": entry, "lowest": low, "line": line, "retrace_pct": retrace, "threshold_pct": 0.30, **cost}
            if low < entry and effective_price >= line:
                return True, f"LOW_0P3_TRAIL_EXIT_SELL_COST low={low:.4f} effective={effective_price:.4f} current={cur:.4f} line={line:.4f}", detail
            return False, "", detail
        trail03._trail03_cost_wrapped = True  # type: ignore[attr-defined]
        r._judge_entry_trail_retrace_exit = trail03

    if callable(old_partial) and not getattr(old_partial, "_partial_cost_wrapped", False):
        def partial_cost(*, symbol: str, pos: Dict[str, Any], side: str, entry_price: float, current_price: float, ctx: Any):
            side_u = str(side or "").upper()
            entry = _f(entry_price)
            cur = _f(current_price)
            if entry <= 0 or cur <= 0 or side_u not in {"BUY", "SELL"}:
                return False, "", {}
            try:
                enabled = bool(getattr(r, "PARTIAL_PROFIT_ENABLED", True))
                trigger = _f(getattr(r, "PARTIAL_PROFIT_TRIGGER_PCT", 0.20), 0.20)
                min_qty = int(_f(getattr(r, "PARTIAL_PROFIT_MIN_QTY", 200), 200))
                ratio = _f(getattr(r, "PARTIAL_PROFIT_RATIO", 0.50), 0.50)
                qty = int(_f(_pos_get(pos, "qty", "quantity"), 0))
                cost = _cost_detail(side_u, entry, cur)
                detail = {"side": side_u, "entry_price": entry, "qty": qty, "trigger_pct": trigger, "ratio": ratio, "min_qty": min_qty, **cost}
                if not enabled:
                    return False, "", detail
                if qty < min_qty:
                    return False, "", detail
                if cost["effective_profit_pct"] >= trigger:
                    return True, f"PARTIAL_PROFIT_TAKE_COST_AWARE effective_profit={cost['effective_profit_pct']:.3f}%>=trigger={trigger:.3f}% raw={cost['raw_profit_pct']:.3f}%", detail
                return False, "", detail
            except Exception:
                logger.exception("[PARTIAL PROFIT COST] failed symbol=%s", symbol)
                return old_partial(symbol=symbol, pos=pos, side=side, entry_price=entry_price, current_price=current_price, ctx=ctx)
        partial_cost._partial_cost_wrapped = True  # type: ignore[attr-defined]
        r._judge_partial_profit_take = partial_cost

    _INSTALLED = True
    logger.warning(
        "[EXIT TRAIL 0.3 COST] installed entry_line=0.3 trail_line=0.3 interest_pct=%.4f slippage_ticks=%.2f tick_yen=%.4f",
        _env_float("EXIT_CREDIT_INTEREST_PCT", 0.01),
        _env_float("EXIT_MARKET_SLIPPAGE_TICKS", 1.0),
        _env_float("EXIT_MARKET_SLIPPAGE_TICK_SIZE_YEN", 1.0),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[EXIT TRAIL 0.3 COST] auto install failed")


__all__ = ["install"]
