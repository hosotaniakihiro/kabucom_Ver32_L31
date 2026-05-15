# ============================================================
# File   : core/startup/exit_trail_03_runtime_patch.py
# Version: V1.0
# ------------------------------------------------------------
# BUY : entry price -0.3% で返済
# BUY : entry後の最高値から -0.3% で返済
# SELL: entry price +0.3% で返済
# SELL: entry後の最安値から +0.3% で返済
# ============================================================

from __future__ import annotations

import logging
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


def _profit_pct(side: str, entry: float, current: float) -> float:
    if entry <= 0 or current <= 0:
        return 0.0
    if side == "BUY":
        return (current - entry) / entry * 100.0
    return (entry - current) / entry * 100.0


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True

    try:
        import trading.exit.exit_position_runner as r
    except Exception:
        logger.exception("[EXIT TRAIL 0.3] import failed")
        return False

    r.ABSOLUTE_ENTRY_STOP_LOSS_PCT = 0.30
    r.ENTRY_TRAIL_RETRACE_EXIT_PCT = 0.30

    old_abs = getattr(r, "_judge_absolute_entry_stop_loss", None)
    old_trail = getattr(r, "_judge_entry_trail_retrace_exit", None)

    if callable(old_abs) and not getattr(old_abs, "_trail03_wrapped", False):
        def abs03(*, symbol: str, side: str, entry_price: float, current_price: float):
            side_u = str(side or "").upper()
            entry = _f(entry_price)
            cur = _f(current_price)
            if entry <= 0 or cur <= 0 or side_u not in {"BUY", "SELL"}:
                return False, "", {}
            pct = 0.30
            if side_u == "BUY":
                line = entry * 0.997
                adverse = (entry - cur) / entry * 100.0
                detail = {"side": side_u, "entry_price": entry, "current_price": cur, "line": line, "adverse_pct": adverse, "threshold_pct": pct, "current_profit_pct": _profit_pct(side_u, entry, cur)}
                if cur <= line:
                    return True, f"ENTRY_PRICE_0P3_EXIT_BUY entry={entry:.4f} current={cur:.4f} line={line:.4f}", detail
                return False, "", detail
            line = entry * 1.003
            adverse = (cur - entry) / entry * 100.0
            detail = {"side": side_u, "entry_price": entry, "current_price": cur, "line": line, "adverse_pct": adverse, "threshold_pct": pct, "current_profit_pct": _profit_pct(side_u, entry, cur)}
            if cur >= line:
                return True, f"ENTRY_PRICE_0P3_EXIT_SELL entry={entry:.4f} current={cur:.4f} line={line:.4f}", detail
            return False, "", detail
        abs03._trail03_wrapped = True  # type: ignore[attr-defined]
        r._judge_absolute_entry_stop_loss = abs03

    if callable(old_trail) and not getattr(old_trail, "_trail03_wrapped", False):
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

            if side_u == "BUY":
                line = high * 0.997
                retrace = (high - cur) / high * 100.0 if high > 0 else 0.0
                detail = {"side": side_u, "entry_price": entry, "current_price": cur, "highest": high, "line": line, "retrace_pct": retrace, "threshold_pct": 0.30, "current_profit_pct": _profit_pct(side_u, entry, cur)}
                if high > entry and cur <= line:
                    return True, f"HIGH_0P3_TRAIL_EXIT_BUY high={high:.4f} current={cur:.4f} line={line:.4f}", detail
                return False, "", detail

            line = low * 1.003
            retrace = (cur - low) / low * 100.0 if low > 0 else 0.0
            detail = {"side": side_u, "entry_price": entry, "current_price": cur, "lowest": low, "line": line, "retrace_pct": retrace, "threshold_pct": 0.30, "current_profit_pct": _profit_pct(side_u, entry, cur)}
            if low < entry and cur >= line:
                return True, f"LOW_0P3_TRAIL_EXIT_SELL low={low:.4f} current={cur:.4f} line={line:.4f}", detail
            return False, "", detail
        trail03._trail03_wrapped = True  # type: ignore[attr-defined]
        r._judge_entry_trail_retrace_exit = trail03

    _INSTALLED = True
    logger.warning("[EXIT TRAIL 0.3] installed entry_line=0.3 trail_line=0.3")
    return True


try:
    install()
except Exception:
    logger.exception("[EXIT TRAIL 0.3] auto install failed")


__all__ = ["install"]
