from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_NORMAL = None
_ORIG_TONOSAMA = None


def _env_pct(name: str, default_pct: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return abs(float(default_pct)) / 100.0
        return abs(float(v)) / 100.0
    except Exception:
        return abs(float(default_pct)) / 100.0


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _get(pos: Any, name: str, default=None):
    try:
        return pos.get(name, default) if isinstance(pos, dict) else getattr(pos, name, default)
    except Exception:
        return default


def _set(pos: Any, name: str, value: Any) -> None:
    try:
        if isinstance(pos, dict):
            pos[name] = value
        else:
            setattr(pos, name, value)
    except Exception:
        pass


def _f(x, default=0.0) -> float:
    try:
        if x is None or x == "":
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def _symbol(pos: Any) -> str:
    return str(_get(pos, "symbol") or _get(pos, "Symbol") or _get(pos, "stock_code") or "").strip()


def _side(pos: Any) -> str:
    return str(_get(pos, "side") or _get(pos, "Side") or "BUY").upper()


def _is_sell(pos: Any) -> bool:
    s = _side(pos)
    return s.startswith("SELL") or s.startswith("SHORT")


def _entry(pos: Any) -> float:
    return _f(_get(pos, "avg_price") or _get(pos, "entry_price") or _get(pos, "price") or _get(pos, "current_price"), 0.0)


def _pnl(pos: Any, price: float) -> float:
    e = _entry(pos)
    if e <= 0 or price <= 0:
        return 0.0
    r = (price - e) / e
    return -r if _is_sell(pos) else r


def _parse_dt(v: Any):
    try:
        if isinstance(v, dt.datetime):
            return v
        if not v:
            return None
        return dt.datetime.fromisoformat(str(v).replace("T", " ").split(".")[0])
    except Exception:
        return None


def _age_sec(pos: Any, now: dt.datetime) -> float:
    for k in ("entry_time", "created_at", "open_time", "order_time"):
        t = _parse_dt(_get(pos, k))
        if t is not None:
            try:
                return max(0.0, (now - t).total_seconds())
            except Exception:
                pass
    return 9999.0


def _update_extreme(pos: Any, price: float) -> tuple[float, float, float, float]:
    e = _entry(pos)
    if e <= 0 or price <= 0:
        return price, price, 0.0, 0.0
    high = _f(_get(pos, "high_since_entry"), 0.0)
    low = _f(_get(pos, "low_since_entry"), 0.0)
    if high <= 0:
        high = max(e, price)
    if low <= 0:
        low = min(e, price)
    high = max(high, price)
    low = min(low, price)
    _set(pos, "high_since_entry", high)
    _set(pos, "low_since_entry", low)
    if _is_sell(pos):
        mfe = max(0.0, (e - low) / e)
        retrace = max(0.0, (price - low) / low) if low > 0 else 0.0
    else:
        mfe = max(0.0, (high - e) / e)
        retrace = max(0.0, (high - price) / high) if high > 0 else 0.0
    mfe = max(_f(_get(pos, "max_profit_rate"), 0.0), mfe)
    _set(pos, "max_profit_rate", mfe)
    _set(pos, "drawdown_from_extreme", retrace)
    return high, low, mfe, retrace


def _grace_stop_allowed(pos: Any, pnl: float, now: dt.datetime, *, tonosama: bool = False) -> bool:
    grace = _env_float("EXIT_INITIAL_STOP_GRACE_SEC", 30.0)
    hard_stop = _env_pct("EXIT_INITIAL_GRACE_HARD_STOP_PCT", 0.60 if not tonosama else 0.70)
    age = _age_sec(pos, now)
    if age < grace and abs(pnl) < hard_stop:
        logger.info(
            "[EXIT INITIAL GRACE] skip stop symbol=%s side=%s age=%.1fs grace=%.1fs pnl=%.4f%% hard=%.4f%%",
            _symbol(pos), _side(pos), age, grace, pnl * 100.0, hard_stop * 100.0,
        )
        return False
    return True


def _patched_normal(pos: Any, price: float, now: dt.datetime):
    pnl = _pnl(pos, price)
    stop = _env_pct("ABSOLUTE_ENTRY_STOP_LOSS_PCT", 0.35)
    if pnl <= -stop and _grace_stop_allowed(pos, pnl, now, tonosama=False):
        logger.warning("[EXIT STOP 0.35] symbol=%s side=%s pnl=%.4f%% price=%s entry=%s", _symbol(pos), _side(pos), pnl * 100.0, price, _entry(pos))
        return "NORMAL_STOP_0P35"
    high, low, mfe, retrace = _update_extreme(pos, price)
    trail_start = _env_pct("EXIT_PROFIT_TRAIL_START_PCT", 0.50)
    trail_dd = _env_pct("EXIT_PROFIT_TRAIL_DRAWDOWN_PCT", 0.30)
    if mfe >= trail_start and retrace >= trail_dd:
        logger.warning("[EXIT PROFIT TRAIL 0.5/0.3] symbol=%s side=%s mfe=%.4f%% retrace=%.4f%% high=%s low=%s price=%s", _symbol(pos), _side(pos), mfe * 100.0, retrace * 100.0, high, low, price)
        return "NORMAL_PROFIT_TRAIL_0P5_0P3"
    try:
        return _ORIG_NORMAL(pos, price, now) if callable(_ORIG_NORMAL) else None
    except Exception:
        logger.exception("[EXIT STOP/TRAIL PATCH] original normal failed symbol=%s", _symbol(pos))
        return None


def _patched_tonosama(pos: Any, price: float, now: dt.datetime):
    pnl = _pnl(pos, price)
    stop = _env_pct("TONOSAMA_STOP_LOSS_PCT", 0.40)
    if pnl <= -stop and _grace_stop_allowed(pos, pnl, now, tonosama=True):
        logger.warning("[EXIT STOP TONOSAMA 0.40] symbol=%s side=%s pnl=%.4f%% price=%s entry=%s", _symbol(pos), _side(pos), pnl * 100.0, price, _entry(pos))
        return "TONOSAMA_STOP_0P40"
    high, low, mfe, retrace = _update_extreme(pos, price)
    trail_start = _env_pct("EXIT_PROFIT_TRAIL_START_PCT", 0.50)
    trail_dd = _env_pct("EXIT_PROFIT_TRAIL_DRAWDOWN_PCT", 0.30)
    if mfe >= trail_start and retrace >= trail_dd:
        logger.warning("[EXIT PROFIT TRAIL TONOSAMA 0.5/0.3] symbol=%s side=%s mfe=%.4f%% retrace=%.4f%% high=%s low=%s price=%s", _symbol(pos), _side(pos), mfe * 100.0, retrace * 100.0, high, low, price)
        return "TONOSAMA_PROFIT_TRAIL_0P5_0P3"
    try:
        return _ORIG_TONOSAMA(pos, price, now) if callable(_ORIG_TONOSAMA) else None
    except Exception:
        logger.exception("[EXIT STOP/TRAIL PATCH] original tonosama failed symbol=%s", _symbol(pos))
        return None


def install() -> bool:
    global _INSTALLED, _ORIG_NORMAL, _ORIG_TONOSAMA
    if _INSTALLED:
        return True
    try:
        os.environ["ABSOLUTE_ENTRY_STOP_LOSS_PCT"] = "0.35"
        os.environ["TONOSAMA_STOP_LOSS_PCT"] = "0.40"
        os.environ["EXIT_INITIAL_STOP_GRACE_SEC"] = os.getenv("EXIT_INITIAL_STOP_GRACE_SEC", "30")
        os.environ["EXIT_INITIAL_GRACE_HARD_STOP_PCT"] = os.getenv("EXIT_INITIAL_GRACE_HARD_STOP_PCT", "0.60")
        os.environ["EXIT_PROFIT_TRAIL_START_PCT"] = "0.50"
        os.environ["EXIT_PROFIT_TRAIL_DRAWDOWN_PCT"] = "0.30"
        import trading.handlers.exit_handler as eh
        cur_n = getattr(eh, "check_normal_exit", None)
        cur_t = getattr(eh, "check_tonosama_exit", None)
        if not callable(cur_n) or not callable(cur_t):
            logger.warning("[EXIT STOP/TRAIL PATCH] target unavailable normal=%s tonosama=%s", callable(cur_n), callable(cur_t))
            return False
        if getattr(cur_n, "_exit_stop_grace_profitrun_v2", False):
            _INSTALLED = True
            return True
        _ORIG_NORMAL = getattr(cur_n, "_original", cur_n)
        _ORIG_TONOSAMA = getattr(cur_t, "_original", cur_t)
        _patched_normal._exit_stop_grace_profitrun_v2 = True  # type: ignore[attr-defined]
        _patched_normal._original = _ORIG_NORMAL  # type: ignore[attr-defined]
        _patched_tonosama._exit_stop_grace_profitrun_v2 = True  # type: ignore[attr-defined]
        _patched_tonosama._original = _ORIG_TONOSAMA  # type: ignore[attr-defined]
        eh.check_normal_exit = _patched_normal
        eh.check_tonosama_exit = _patched_tonosama
        _INSTALLED = True
        logger.warning(
            "[EXIT STOP/TRAIL PATCH] installed v2 initial_grace=%ss hard_stop=%s%% normal_stop=0.35%% tonosama_stop=0.40%% profit_trail_start=0.50%% profit_trail_dd=0.30%%",
            os.environ.get("EXIT_INITIAL_STOP_GRACE_SEC"),
            os.environ.get("EXIT_INITIAL_GRACE_HARD_STOP_PCT"),
        )
        return True
    except Exception:
        logger.exception("[EXIT STOP/TRAIL PATCH] install failed")
        return False
try:
    install()
except Exception:
    logger.exception("[EXIT STOP/TRAIL PATCH] auto install failed")
__all__ = ["install"]
