# -*- coding: utf-8 -*-
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
import time
from functools import wraps
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-SUMMARY-AI-QUALITY-EXTRA-GUARDS"
_INSTALLED = False
_WATCHER_STARTED = False
_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def _env_bool(k: str, d: bool = True) -> bool:
    v = os.environ.get(k)
    if v is None or str(v).strip() == "":
        return d
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enabled"}


def _env_float(k: str, d: float) -> float:
    try:
        v = os.environ.get(k)
        return d if v is None or str(v).strip() == "" else float(str(v).replace(",", ""))
    except Exception:
        return d


def _env_int(k: str, d: int) -> int:
    try:
        v = os.environ.get(k)
        return d if v is None or str(v).strip() == "" else int(float(str(v).replace(",", "")))
    except Exception:
        return d


def _f(v: Any, d: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return d
        x = float(str(v).replace(",", ""))
        return d if x != x else x
    except Exception:
        return d


def _sym(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _side(v: Any) -> str:
    s = str(v or "BUY").strip().upper()
    return s if s in {"BUY", "SELL"} else "BUY"


def _db_path() -> str:
    base = os.environ.get("SUMMARY_DB_DIR", r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary")
    return os.environ.get("SUMMARY_DB_PATH", str(Path(base) / f"summary{dt.datetime.now().strftime('%Y%m%d')}.db"))


def _table_name(interval: int) -> str:
    return os.environ.get(f"SUMMARY_AI_QUALITY_TABLE_{interval}M", f"stock_summary_{interval}min")


def _col(cols: set[str], names: tuple[str, ...]) -> str:
    return next((x for x in names if x in cols), "")


def _current(item: dict[str, Any]) -> tuple[str, str]:
    merged: dict[str, Any] = {}
    if isinstance(item.get("source_row"), dict):
        merged.update(item["source_row"])
    if isinstance(item.get("ai_row"), dict):
        merged.update(item["ai_row"])
    merged.update(item)
    return _sym(merged.get("symbol") or merged.get("Symbol") or merged.get("code")), _side(merged.get("side") or merged.get("ai_side") or merged.get("entry_decision"))


def _load_tf_rows(symbol: str, interval: int, limit: int) -> list[dict[str, float]]:
    path = _db_path()
    table = _table_name(interval)
    if not symbol or not Path(path).exists():
        return []
    try:
        with sqlite3.connect(path, timeout=1.0) as con:
            con.execute("PRAGMA busy_timeout=1000")
            cols = {str(r[1]) for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()}
            c_sym = _col(cols, ("symbol", "code", "stock_code"))
            c_dt = _col(cols, ("datetime", "dt", "timestamp", "time"))
            c_op = _col(cols, ("open", "open_price"))
            c_cl = _col(cols, ("close", "close_price", "price", "current_price"))
            c_hi = _col(cols, ("high", "high_price")) or c_cl
            c_lo = _col(cols, ("low", "low_price")) or c_cl
            c_vo = _col(cols, ("volume", "vol", "trading_volume"))
            c_ma5 = _col(cols, ("ma5", "MA5", "sma5", "moving_average_5"))
            c_vwap = _col(cols, ("vwap", "VWAP"))
            if not c_sym or not c_dt or not c_cl:
                return []
            op_expr = c_op if c_op else c_cl
            vol_expr = c_vo if c_vo else "0"
            ma5_expr = c_ma5 if c_ma5 else "0"
            vwap_expr = c_vwap if c_vwap else "0"
            rows = con.execute(
                f'SELECT {c_dt},{op_expr},{c_cl},{c_hi},{c_lo},{vol_expr},{ma5_expr},{vwap_expr} FROM "{table}" WHERE CAST({c_sym} AS TEXT)=? ORDER BY {c_dt} DESC LIMIT ?',
                (symbol, max(10, limit)),
            ).fetchall()
        out = []
        for r in reversed(rows):
            close = _f(r[2])
            if close > 0:
                out.append({"open": _f(r[1], close), "close": close, "high": _f(r[3], close), "low": _f(r[4], close), "volume": _f(r[5]), "ma5": _f(r[6]), "vwap": _f(r[7])})
        return out
    except Exception:
        logger.debug("[SUMMARY AI QUALITY] load failed symbol=%s interval=%s", symbol, interval, exc_info=True)
        return []


def _candle_stats(r: dict[str, float]) -> dict[str, float]:
    op = _f(r.get("open")); cl = _f(r.get("close")); hi = _f(r.get("high")); lo = _f(r.get("low"))
    rng = max(0.0, hi - lo)
    upper = max(0.0, hi - max(op, cl))
    lower = max(0.0, min(op, cl) - lo)
    body = abs(cl - op)
    return {
        "range": rng,
        "upper_wick_ratio": 0.0 if rng <= 0 else upper / rng,
        "lower_wick_ratio": 0.0 if rng <= 0 else lower / rng,
        "body_ratio": 0.0 if rng <= 0 else body / rng,
        "close_pos": 0.5 if rng <= 0 else (cl - lo) / rng,
    }


def _pct(a: float, b: float) -> float:
    return 0.0 if b <= 0 else (a - b) / b


def _avg(vals: list[float]) -> float:
    vals = [v for v in vals if v > 0]
    return sum(vals) / len(vals) if vals else 0.0


def _quality_stats(item: dict[str, Any]) -> dict[str, Any]:
    symbol, side = _current(item)
    now = time.time()
    key = (symbol, side)
    cached = _CACHE.get(key)
    if cached and now - cached[0] <= _env_float("SUMMARY_AI_QUALITY_CACHE_TTL_SEC", 5.0):
        return dict(cached[1])
    lookback = max(10, _env_int("SUMMARY_AI_QUALITY_LOOKBACK_BARS", 20))
    rows1 = _load_tf_rows(symbol, 1, lookback)
    rows3 = _load_tf_rows(symbol, 3, lookback)
    rows5 = _load_tf_rows(symbol, 5, lookback)
    out = {"symbol": symbol, "side": side, "tf1_rows": len(rows1), "tf3_rows": len(rows3), "tf5_rows": len(rows5), "tf1": {}, "tf3": {}, "tf5": {}}
    for label, rows in (("tf1", rows1), ("tf3", rows3), ("tf5", rows5)):
        if not rows:
            out[label] = {"missing": True}
            continue
        last = rows[-1]
        prev = rows[-2] if len(rows) >= 2 else rows[-1]
        cs = _candle_stats(last)
        price = _f(last.get("close"))
        ma5 = _f(last.get("ma5"))
        vwap = _f(last.get("vwap"))
        vols = [_f(x.get("volume")) for x in rows[-6:-1]]
        avg_vol = _avg(vols)
        out[label] = {
            "missing": False,
            "price": price,
            "open": _f(last.get("open")),
            "high": _f(last.get("high")),
            "low": _f(last.get("low")),
            "volume": _f(last.get("volume")),
            "avg_volume_5": avg_vol,
            "volume_ratio_5": 0.0 if avg_vol <= 0 else _f(last.get("volume")) / avg_vol,
            "price_change_1": _pct(price, _f(prev.get("close"))),
            "ma5": ma5,
            "vwap": vwap,
            "ma5_dev": _pct(price, ma5),
            "vwap_dev": _pct(price, vwap),
            **cs,
        }
    _CACHE[key] = (now, dict(out))
    return out


def _add_reason(base: Any, extra: str) -> str:
    b = str(base or "").strip()
    return extra if not b else f"{b}|{extra}"


def _check_wick(st: dict[str, Any]) -> tuple[bool, str]:
    if not _env_bool("SUMMARY_AI_WICK_GUARD_ENABLED", True):
        return True, "wick_guard_disabled"
    side = str(st.get("side") or "BUY").upper()
    min_range = _env_float("SUMMARY_AI_WICK_MIN_RANGE_PCT", 0.0015)
    max_wick = _env_float("SUMMARY_AI_MAX_ADVERSE_WICK_RATIO", 0.45)
    for label in ("tf3", "tf5"):
        tf = st.get(label) if isinstance(st.get(label), dict) else {}
        if not tf or bool(tf.get("missing")):
            continue
        rng_pct = 0.0 if _f(tf.get("price")) <= 0 else _f(tf.get("range")) / _f(tf.get("price"))
        if rng_pct < min_range:
            continue
        if side == "BUY" and _f(tf.get("upper_wick_ratio")) >= max_wick:
            return False, f"upper_wick_buy_{label}:{_f(tf.get('upper_wick_ratio')):.3f}>={max_wick:.3f}"
        if side == "SELL" and _f(tf.get("lower_wick_ratio")) >= max_wick:
            return False, f"lower_wick_sell_{label}:{_f(tf.get('lower_wick_ratio')):.3f}>={max_wick:.3f}"
    return True, "wick_ok"


def _check_volume_climax(st: dict[str, Any]) -> tuple[bool, str]:
    if not _env_bool("SUMMARY_AI_VOLUME_CLIMAX_GUARD_ENABLED", True):
        return True, "volume_climax_guard_disabled"
    side = str(st.get("side") or "BUY").upper()
    min_move = _env_float("SUMMARY_AI_CLIMAX_MIN_PRICE_MOVE_3M", 0.0060)
    min_vratio = _env_float("SUMMARY_AI_CLIMAX_MIN_VOLUME_RATIO", 3.0)
    tf3 = st.get("tf3") if isinstance(st.get("tf3"), dict) else {}
    tf5 = st.get("tf5") if isinstance(st.get("tf5"), dict) else {}
    if not tf3 or bool(tf3.get("missing")):
        return True, "volume_climax_no_3m_data:fail_open"
    chg = _f(tf3.get("price_change_1"))
    vr = _f(tf3.get("volume_ratio_5"))
    near_high = _f(tf3.get("close_pos")) >= _env_float("SUMMARY_AI_CLIMAX_HIGH_CLOSE_POS", 0.70)
    near_low = _f(tf3.get("close_pos")) <= _env_float("SUMMARY_AI_CLIMAX_LOW_CLOSE_POS", 0.30)
    ma_ext = max(_f(tf3.get("ma5_dev")), _f(tf5.get("ma5_dev")) if isinstance(tf5, dict) else 0.0)
    if side == "BUY" and chg >= min_move and vr >= min_vratio and near_high:
        return False, f"buy_volume_climax:chg={chg:.4f} vr={vr:.2f} ma_ext={ma_ext:.4f}"
    if side == "SELL" and chg <= -min_move and vr >= min_vratio and near_low:
        return False, f"sell_volume_climax:chg={chg:.4f} vr={vr:.2f} ma_ext={ma_ext:.4f}"
    return True, f"volume_climax_ok:chg={chg:.4f} vr={vr:.2f}"


def _check_pullback_position(st: dict[str, Any]) -> tuple[bool, str]:
    if not _env_bool("SUMMARY_AI_PULLBACK_POSITION_GUARD_ENABLED", True):
        return True, "pullback_position_guard_disabled"
    side = str(st.get("side") or "BUY").upper()
    tf1 = st.get("tf1") if isinstance(st.get("tf1"), dict) else {}
    tf3 = st.get("tf3") if isinstance(st.get("tf3"), dict) else {}
    if not tf1 or not tf3 or bool(tf1.get("missing")) or bool(tf3.get("missing")):
        return True, "pullback_position_no_data:fail_open"
    max_chase = _env_float("SUMMARY_AI_PULLBACK_MAX_1M_MA5_DEV", 0.0030)
    min_rebound = _env_float("SUMMARY_AI_PULLBACK_MIN_REBOUND_POS", 0.45)
    dev = _f(tf1.get("ma5_dev"))
    close_pos = _f(tf1.get("close_pos"), 0.5)
    if side == "BUY":
        if dev > max_chase and close_pos > 0.75:
            return False, f"pullback_buy_chase_1m:ma5_dev={dev:.4f} close_pos={close_pos:.2f}"
        if close_pos < min_rebound and _f(tf3.get("ma5_dev")) > 0:
            return False, f"pullback_buy_not_rebounded:close_pos={close_pos:.2f}<{min_rebound:.2f}"
    else:
        if dev < -max_chase and close_pos < 0.25:
            return False, f"pullback_sell_chase_1m:ma5_dev={dev:.4f} close_pos={close_pos:.2f}"
        if close_pos > (1.0 - min_rebound) and _f(tf3.get("ma5_dev")) < 0:
            return False, f"pullback_sell_not_rebounded:close_pos={close_pos:.2f}>{1.0 - min_rebound:.2f}"
    return True, f"pullback_position_ok:1m_ma5_dev={dev:.4f} close_pos={close_pos:.2f}"


def _check_quality(item: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    st = _quality_stats(item)
    if bool((st.get("tf1") or {}).get("missing")) and bool((st.get("tf3") or {}).get("missing")) and bool((st.get("tf5") or {}).get("missing")):
        reason = "quality_no_summary_data"
        if _env_bool("SUMMARY_AI_QUALITY_REQUIRE_DATA", False):
            return False, reason, st
        return True, reason + ":fail_open", st
    reasons = []
    for fn in (_check_wick, _check_volume_climax, _check_pullback_position):
        ok, reason = fn(st)
        reasons.append(reason)
        if not ok:
            return False, reason, st
    return True, ";".join(reasons), st


def _patch() -> bool:
    try:
        from trading.entry.summary_ai import ai_gate_runner as agr
        cur = getattr(agr, "run_ai_gate_for_candidates", None)
        if not callable(cur):
            return False
        if getattr(cur, "_summary_ai_quality_extra_guard_v1", False):
            return True

        @wraps(cur)
        def wrapped(*args: Any, **kwargs: Any):
            res = cur(*args, **kwargs)
            if not isinstance(res, list) or not _env_bool("SUMMARY_AI_QUALITY_EXTRA_GUARD_ENABLED", True):
                return res
            out = []
            passed = blocked = fail_open = 0
            for item in res:
                if not isinstance(item, dict) or not bool(item.get("allow")):
                    out.append(item)
                    continue
                ok, reason, st = _check_quality(item)
                x = dict(item)
                x["quality_extra_guard"] = st
                x["reason"] = _add_reason(x.get("reason"), reason)
                if "fail_open" in reason:
                    fail_open += 1
                if ok:
                    passed += 1
                else:
                    x["allow"] = False
                    blocked += 1
                    logger.warning("[SUMMARY AI QUALITY] AI_OK->NG symbol=%s side=%s reason=%s stats=%s", x.get("symbol"), x.get("side"), reason, st)
                out.append(x)
            logger.warning("[SUMMARY AI QUALITY] result total=%s passed=%s blocked=%s fail_open=%s version=%s", len(res), passed, blocked, fail_open, VERSION)
            return out

        wrapped._summary_ai_quality_extra_guard_v1 = True  # type: ignore[attr-defined]
        wrapped._original = cur  # type: ignore[attr-defined]
        agr.run_ai_gate_for_candidates = wrapped
        logger.warning("[SUMMARY AI QUALITY] patched version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI QUALITY] patch failed")
        return False


def _defaults() -> None:
    os.environ.setdefault("SUMMARY_AI_QUALITY_EXTRA_GUARD_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_QUALITY_REQUIRE_DATA", "0")
    os.environ.setdefault("SUMMARY_AI_QUALITY_LOOKBACK_BARS", "20")
    os.environ.setdefault("SUMMARY_AI_WICK_GUARD_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_WICK_MIN_RANGE_PCT", "0.0015")
    os.environ.setdefault("SUMMARY_AI_MAX_ADVERSE_WICK_RATIO", "0.45")
    os.environ.setdefault("SUMMARY_AI_VOLUME_CLIMAX_GUARD_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_CLIMAX_MIN_PRICE_MOVE_3M", "0.0060")
    os.environ.setdefault("SUMMARY_AI_CLIMAX_MIN_VOLUME_RATIO", "3.0")
    os.environ.setdefault("SUMMARY_AI_CLIMAX_HIGH_CLOSE_POS", "0.70")
    os.environ.setdefault("SUMMARY_AI_CLIMAX_LOW_CLOSE_POS", "0.30")
    os.environ.setdefault("SUMMARY_AI_PULLBACK_POSITION_GUARD_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_PULLBACK_MAX_1M_MA5_DEV", "0.0030")
    os.environ.setdefault("SUMMARY_AI_PULLBACK_MIN_REBOUND_POS", "0.45")


def _watch() -> None:
    for _ in range(max(1, _env_int("SUMMARY_AI_QUALITY_WATCH_LOOPS", 120))):
        _patch()
        time.sleep(max(0.5, _env_float("SUMMARY_AI_QUALITY_WATCH_INTERVAL", 1.0)))


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    if os.environ.get("DISABLE_SUMMARY_AI_QUALITY_EXTRA_GUARD_PATCH", "").strip() == "1":
        return False
    _defaults()
    ok = _patch()
    if not _WATCHER_STARTED and _env_bool("SUMMARY_AI_QUALITY_WATCHER", True):
        _WATCHER_STARTED = True
        threading.Thread(target=_watch, name="summary-ai-quality-extra-watch", daemon=True).start()
    _INSTALLED = bool(ok or _WATCHER_STARTED)
    logger.warning("[SUMMARY AI QUALITY] installed ok=%s wick=%s climax=%s pullback=%s max_wick=%s climax_move=%s climax_vr=%s pullback_ma5=%s version=%s", _INSTALLED, os.environ.get("SUMMARY_AI_WICK_GUARD_ENABLED"), os.environ.get("SUMMARY_AI_VOLUME_CLIMAX_GUARD_ENABLED"), os.environ.get("SUMMARY_AI_PULLBACK_POSITION_GUARD_ENABLED"), os.environ.get("SUMMARY_AI_MAX_ADVERSE_WICK_RATIO"), os.environ.get("SUMMARY_AI_CLIMAX_MIN_PRICE_MOVE_3M"), os.environ.get("SUMMARY_AI_CLIMAX_MIN_VOLUME_RATIO"), os.environ.get("SUMMARY_AI_PULLBACK_MAX_1M_MA5_DEV"), VERSION)
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI QUALITY] auto install failed")


__all__ = ["VERSION", "install"]