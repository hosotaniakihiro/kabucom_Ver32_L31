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
VERSION = "V3-SUMMARY-AI-EMP-HIGHLOW-MA-VWAP-GATE"
_INSTALLED = False
_WATCHER_STARTED = False
_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_HL_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_EXT_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


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
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") and s[:-2].isdigit() else s


def _side(v: Any) -> str:
    s = str(v or "BUY").strip().upper()
    return s if s in {"BUY", "SELL"} else "BUY"


def _db_path() -> str:
    base = os.environ.get("SUMMARY_DB_DIR", r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary")
    return os.environ.get("SUMMARY_DB_PATH", str(Path(base) / f"summary{dt.datetime.now().strftime('%Y%m%d')}.db"))


def _col(cols: set[str], names: tuple[str, ...]) -> str:
    return next((x for x in names if x in cols), "")


def _table_name(interval: int) -> str:
    return os.environ.get(f"SUMMARY_AI_HIGHLOW_TABLE_{interval}M", f"stock_summary_{interval}min")


def _load_rows(symbol: str, limit: int) -> list[dict[str, float]]:
    path = _db_path()
    table = os.environ.get("SUMMARY_AI_EMP_PROB_TABLE", "stock_summary_1min")
    if not symbol or not Path(path).exists():
        return []
    try:
        with sqlite3.connect(path, timeout=1.0) as con:
            con.execute("PRAGMA busy_timeout=1000")
            cols = {str(r[1]) for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()}
            c_sym = _col(cols, ("symbol", "code", "stock_code"))
            c_dt = _col(cols, ("datetime", "dt", "timestamp", "time"))
            c_cl = _col(cols, ("close", "close_price", "price", "current_price"))
            c_hi = _col(cols, ("high", "high_price")) or c_cl
            c_lo = _col(cols, ("low", "low_price")) or c_cl
            c_vo = _col(cols, ("volume", "vol", "trading_volume"))
            if not c_sym or not c_dt or not c_cl:
                return []
            vol_expr = c_vo if c_vo else "0"
            rows = con.execute(
                f'SELECT {c_dt},{c_cl},{c_hi},{c_lo},{vol_expr} FROM "{table}" WHERE CAST({c_sym} AS TEXT)=? ORDER BY {c_dt} DESC LIMIT ?',
                (symbol, max(30, limit)),
            ).fetchall()
        out = []
        for r in reversed(rows):
            close = _f(r[1])
            if close > 0:
                out.append({"close": close, "high": _f(r[2], close), "low": _f(r[3], close), "volume": _f(r[4])})
        return out
    except Exception:
        logger.debug("[SUMMARY AI EMP PROB] load failed symbol=%s", symbol, exc_info=True)
        return []


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
            c_cl = _col(cols, ("close", "close_price", "price", "current_price"))
            c_hi = _col(cols, ("high", "high_price")) or c_cl
            c_lo = _col(cols, ("low", "low_price")) or c_cl
            c_ma5 = _col(cols, ("ma5", "MA5", "sma5", "moving_average_5"))
            c_vwap = _col(cols, ("vwap", "VWAP"))
            if not c_sym or not c_dt or not c_cl:
                return []
            ma5_expr = c_ma5 if c_ma5 else "0"
            vwap_expr = c_vwap if c_vwap else "0"
            rows = con.execute(
                f'SELECT {c_dt},{c_cl},{c_hi},{c_lo},{ma5_expr},{vwap_expr} FROM "{table}" WHERE CAST({c_sym} AS TEXT)=? ORDER BY {c_dt} DESC LIMIT ?',
                (symbol, max(10, limit)),
            ).fetchall()
        out = []
        for r in reversed(rows):
            close = _f(r[1])
            if close > 0:
                out.append({"close": close, "high": _f(r[2], close), "low": _f(r[3], close), "ma5": _f(r[4]), "vwap": _f(r[5])})
        return out
    except Exception:
        logger.debug("[SUMMARY AI TF] load failed symbol=%s interval=%s", symbol, interval, exc_info=True)
        return []


def _current(item: dict[str, Any]) -> tuple[str, str, float]:
    merged: dict[str, Any] = {}
    if isinstance(item.get("source_row"), dict):
        merged.update(item["source_row"])
    if isinstance(item.get("ai_row"), dict):
        merged.update(item["ai_row"])
    merged.update(item)
    symbol = _sym(merged.get("symbol") or merged.get("Symbol") or merged.get("code"))
    side = _side(merged.get("side") or merged.get("ai_side") or merged.get("entry_decision"))
    volume = _f(merged.get("volume") or merged.get("ai_disp_volume") or merged.get("trading_volume"))
    return symbol, side, volume


def _stats(item: dict[str, Any]) -> dict[str, Any]:
    symbol, side, cur_volume = _current(item)
    window = max(1, _env_int("SUMMARY_AI_EMP_PROB_WINDOW_BARS", 5))
    lookback = max(window + 20, _env_int("SUMMARY_AI_EMP_PROB_LOOKBACK_BARS", 180))
    key = (symbol, side)
    now = time.time()
    cached = _CACHE.get(key)
    if cached and now - cached[0] <= _env_float("SUMMARY_AI_EMP_PROB_CACHE_TTL_SEC", 5.0):
        return dict(cached[1])
    rows = _load_rows(symbol, lookback)
    target_pct = abs(_env_float("SUMMARY_AI_EMP_PROB_TARGET_PCT", _env_float("EXIT_TAKE_PROFIT_PCT", 0.0020)))
    risk_pct = abs(_env_float("SUMMARY_AI_EMP_PROB_RISK_PCT", _env_float("EXIT_STOP_LOSS_PCT", 0.0030)))
    samples = target_first = risk_first = neither = 0
    for i in range(0, max(0, len(rows) - window - 1)):
        entry = _f(rows[i].get("close"))
        if entry <= 0:
            continue
        pv = _f(rows[i].get("volume"))
        if cur_volume > 0 and pv > 0:
            ratio = pv / cur_volume
            if ratio < _env_float("SUMMARY_AI_EMP_PROB_MIN_VOLUME_RATIO", 0.25) or ratio > _env_float("SUMMARY_AI_EMP_PROB_MAX_VOLUME_RATIO", 4.0):
                continue
        fut = rows[i + 1:i + 1 + window]
        if len(fut) < window:
            continue
        samples += 1
        t_i = r_i = 999
        for j, fr in enumerate(fut, start=1):
            hi = _f(fr.get("high")); lo = _f(fr.get("low"))
            if side == "BUY":
                if t_i == 999 and hi >= entry * (1.0 + target_pct):
                    t_i = j
                if r_i == 999 and lo <= entry * (1.0 - risk_pct):
                    r_i = j
            else:
                if t_i == 999 and lo <= entry * (1.0 - target_pct):
                    t_i = j
                if r_i == 999 and hi >= entry * (1.0 + risk_pct):
                    r_i = j
        if t_i != 999 and t_i <= r_i:
            target_first += 1
        elif r_i != 999:
            risk_first += 1
        else:
            neither += 1
    p_target = target_first / samples if samples else 0.0
    p_risk = risk_first / samples if samples else 0.0
    ev = p_target * target_pct - p_risk * risk_pct
    out = {"symbol": symbol, "side": side, "samples": samples, "p_target_5m": p_target, "p_risk_5m": p_risk, "expected_value": ev, "target_pct": target_pct, "risk_pct": risk_pct, "window_bars": window, "neither": neither, "rows": len(rows)}
    _CACHE[key] = (now, dict(out))
    return out


def _count_updates(rows: list[dict[str, float]], interval: int) -> dict[str, Any]:
    recent_n = max(1, _env_int("SUMMARY_AI_HIGHLOW_RECENT_LOOKBACK", 5))
    if len(rows) < 2:
        return {"interval": interval, "rows": len(rows), "high_updates": 0, "low_updates": 0, "recent_high_updates": 0, "recent_low_updates": 0, "missing": True}
    max_high = _f(rows[0].get("high"))
    min_low = _f(rows[0].get("low"), max_high)
    flags_high: list[int] = [0]
    flags_low: list[int] = [0]
    high_updates = 0
    low_updates = 0
    for r in rows[1:]:
        hi = _f(r.get("high"))
        lo = _f(r.get("low"), hi)
        is_high = hi > 0 and hi > max_high
        is_low = lo > 0 and lo < min_low
        if is_high:
            high_updates += 1
            max_high = hi
        else:
            max_high = max(max_high, hi)
        if is_low:
            low_updates += 1
            min_low = lo
        else:
            min_low = min(min_low, lo) if min_low > 0 and lo > 0 else min_low
        flags_high.append(1 if is_high else 0)
        flags_low.append(1 if is_low else 0)
    return {
        "interval": interval,
        "rows": len(rows),
        "high_updates": high_updates,
        "low_updates": low_updates,
        "recent_high_updates": sum(flags_high[-recent_n:]),
        "recent_low_updates": sum(flags_low[-recent_n:]),
        "recent_lookback": recent_n,
        "missing": False,
    }


def _highlow_stats(item: dict[str, Any]) -> dict[str, Any]:
    symbol, side, _ = _current(item)
    now = time.time()
    key = (symbol, side)
    cached = _HL_CACHE.get(key)
    if cached and now - cached[0] <= _env_float("SUMMARY_AI_HIGHLOW_CACHE_TTL_SEC", 5.0):
        return dict(cached[1])
    limit = max(20, _env_int("SUMMARY_AI_HIGHLOW_LOOKBACK_BARS", 120))
    st3 = _count_updates(_load_tf_rows(symbol, 3, limit), 3)
    st5 = _count_updates(_load_tf_rows(symbol, 5, limit), 5)
    out = {"symbol": symbol, "side": side, "tf3": st3, "tf5": st5}
    _HL_CACHE[key] = (now, dict(out))
    return out


def _dev_pct(price: float, base: float) -> float:
    return 0.0 if price <= 0 or base <= 0 else (price - base) / base


def _latest_dev(rows: list[dict[str, float]], interval: int) -> dict[str, Any]:
    if not rows:
        return {"interval": interval, "missing": True, "rows": 0}
    r = rows[-1]
    price = _f(r.get("close"))
    ma5 = _f(r.get("ma5"))
    vwap = _f(r.get("vwap"))
    return {
        "interval": interval,
        "missing": price <= 0 or (ma5 <= 0 and vwap <= 0),
        "rows": len(rows),
        "price": price,
        "ma5": ma5,
        "vwap": vwap,
        "ma5_dev": _dev_pct(price, ma5),
        "vwap_dev": _dev_pct(price, vwap),
    }


def _extension_stats(item: dict[str, Any]) -> dict[str, Any]:
    symbol, side, _ = _current(item)
    now = time.time()
    key = (symbol, side)
    cached = _EXT_CACHE.get(key)
    if cached and now - cached[0] <= _env_float("SUMMARY_AI_EXTENSION_CACHE_TTL_SEC", 5.0):
        return dict(cached[1])
    limit = max(10, _env_int("SUMMARY_AI_EXTENSION_LOOKBACK_BARS", 20))
    st3 = _latest_dev(_load_tf_rows(symbol, 3, limit), 3)
    st5 = _latest_dev(_load_tf_rows(symbol, 5, limit), 5)
    out = {"symbol": symbol, "side": side, "tf3": st3, "tf5": st5}
    _EXT_CACHE[key] = (now, dict(out))
    return out


def _add_reason(base: Any, extra: str) -> str:
    b = str(base or "").strip()
    return extra if not b else f"{b}|{extra}"


def _check(item: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    st = _stats(item)
    samples = int(st.get("samples") or 0)
    min_samples = _env_int("SUMMARY_AI_EMP_PROB_MIN_SAMPLES", 12)
    if samples < min_samples:
        reason = f"emp_prob_sample_low:{samples}<{min_samples}"
        if _env_bool("SUMMARY_AI_EMP_PROB_REQUIRE_MIN_SAMPLES", False):
            return False, reason, st
        return True, reason + ":fail_open", st
    min_p = _env_float("SUMMARY_AI_EMP_PROB_MIN_TARGET_PROB", 0.55)
    max_r = _env_float("SUMMARY_AI_EMP_PROB_MAX_RISK_PROB", 0.40)
    min_ev = _env_float("SUMMARY_AI_EMP_PROB_MIN_EXPECTED_VALUE", 0.0)
    p = _f(st.get("p_target_5m")); r = _f(st.get("p_risk_5m")); ev = _f(st.get("expected_value"))
    if p < min_p:
        return False, f"emp_prob_target_low:{p:.3f}<{min_p:.3f}", st
    if r > max_r:
        return False, f"emp_prob_risk_high:{r:.3f}>{max_r:.3f}", st
    if ev <= min_ev:
        return False, f"emp_prob_ev_low:{ev:.5f}<={min_ev:.5f}", st
    return True, f"emp_prob_ok:p={p:.3f} risk={r:.3f} ev={ev:.5f} n={samples}", st


def _check_highlow(item: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    if not _env_bool("SUMMARY_AI_HIGHLOW_UPDATE_GUARD_ENABLED", True):
        return True, "highlow_guard_disabled", {}
    st = _highlow_stats(item)
    side = str(st.get("side") or "BUY").upper()
    tf3 = st.get("tf3") if isinstance(st.get("tf3"), dict) else {}
    tf5 = st.get("tf5") if isinstance(st.get("tf5"), dict) else {}
    if bool(tf3.get("missing")) and bool(tf5.get("missing")):
        reason = "highlow_no_3m5m_data"
        if _env_bool("SUMMARY_AI_HIGHLOW_REQUIRE_DATA", False):
            return False, reason, st
        return True, reason + ":fail_open", st
    if side == "BUY":
        max3 = _env_int("SUMMARY_AI_BUY_MAX_DAY_HIGH_UPDATES_3M", 4)
        max5 = _env_int("SUMMARY_AI_BUY_MAX_DAY_HIGH_UPDATES_5M", 3)
        max_recent = _env_int("SUMMARY_AI_BUY_MAX_RECENT_HIGH_UPDATES", 2)
        if int(tf3.get("high_updates") or 0) >= max3:
            return False, f"high_update_3m_too_many:{int(tf3.get('high_updates') or 0)}>={max3}", st
        if int(tf5.get("high_updates") or 0) >= max5:
            return False, f"high_update_5m_too_many:{int(tf5.get('high_updates') or 0)}>={max5}", st
        if max(int(tf3.get("recent_high_updates") or 0), int(tf5.get("recent_high_updates") or 0)) >= max_recent:
            return False, f"recent_high_update_too_many:3m={int(tf3.get('recent_high_updates') or 0)} 5m={int(tf5.get('recent_high_updates') or 0)}>={max_recent}", st
        return True, f"high_update_ok:3m={int(tf3.get('high_updates') or 0)} 5m={int(tf5.get('high_updates') or 0)} recent3m={int(tf3.get('recent_high_updates') or 0)} recent5m={int(tf5.get('recent_high_updates') or 0)}", st
    max3 = _env_int("SUMMARY_AI_SELL_MAX_DAY_LOW_UPDATES_3M", 4)
    max5 = _env_int("SUMMARY_AI_SELL_MAX_DAY_LOW_UPDATES_5M", 3)
    max_recent = _env_int("SUMMARY_AI_SELL_MAX_RECENT_LOW_UPDATES", 2)
    if int(tf3.get("low_updates") or 0) >= max3:
        return False, f"low_update_3m_too_many:{int(tf3.get('low_updates') or 0)}>={max3}", st
    if int(tf5.get("low_updates") or 0) >= max5:
        return False, f"low_update_5m_too_many:{int(tf5.get('low_updates') or 0)}>={max5}", st
    if max(int(tf3.get("recent_low_updates") or 0), int(tf5.get("recent_low_updates") or 0)) >= max_recent:
        return False, f"recent_low_update_too_many:3m={int(tf3.get('recent_low_updates') or 0)} 5m={int(tf5.get('recent_low_updates') or 0)}>={max_recent}", st
    return True, f"low_update_ok:3m={int(tf3.get('low_updates') or 0)} 5m={int(tf5.get('low_updates') or 0)} recent3m={int(tf3.get('recent_low_updates') or 0)} recent5m={int(tf5.get('recent_low_updates') or 0)}", st


def _check_extension(item: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    if not _env_bool("SUMMARY_AI_EXTENSION_GUARD_ENABLED", True):
        return True, "extension_guard_disabled", {}
    st = _extension_stats(item)
    side = str(st.get("side") or "BUY").upper()
    tf3 = st.get("tf3") if isinstance(st.get("tf3"), dict) else {}
    tf5 = st.get("tf5") if isinstance(st.get("tf5"), dict) else {}
    if bool(tf3.get("missing")) and bool(tf5.get("missing")):
        reason = "extension_no_ma5_vwap_data"
        if _env_bool("SUMMARY_AI_EXTENSION_REQUIRE_DATA", False):
            return False, reason, st
        return True, reason + ":fail_open", st
    max_ma5_3 = _env_float("SUMMARY_AI_MAX_MA5_EXTENSION_3M", 0.0040)
    max_ma5_5 = _env_float("SUMMARY_AI_MAX_MA5_EXTENSION_5M", 0.0050)
    max_vwap = _env_float("SUMMARY_AI_MAX_VWAP_EXTENSION", 0.0060)
    checks = []
    for label, tf, ma_lim in (("3m", tf3, max_ma5_3), ("5m", tf5, max_ma5_5)):
        if not isinstance(tf, dict) or bool(tf.get("missing")):
            continue
        ma_dev = _f(tf.get("ma5_dev"))
        vw_dev = _f(tf.get("vwap_dev"))
        if side == "BUY":
            if _f(tf.get("ma5")) > 0 and ma_dev >= ma_lim:
                return False, f"ma5_extension_buy_{label}:{ma_dev:.4f}>={ma_lim:.4f}", st
            if _f(tf.get("vwap")) > 0 and vw_dev >= max_vwap:
                return False, f"vwap_extension_buy_{label}:{vw_dev:.4f}>={max_vwap:.4f}", st
        else:
            if _f(tf.get("ma5")) > 0 and ma_dev <= -ma_lim:
                return False, f"ma5_extension_sell_{label}:{ma_dev:.4f}<=-{ma_lim:.4f}", st
            if _f(tf.get("vwap")) > 0 and vw_dev <= -max_vwap:
                return False, f"vwap_extension_sell_{label}:{vw_dev:.4f}<=-{max_vwap:.4f}", st
        checks.append(f"{label}:ma5={ma_dev:.4f} vwap={vw_dev:.4f}")
    return True, "extension_ok:" + ",".join(checks), st


def _patch() -> bool:
    try:
        from trading.entry.summary_ai import ai_gate_runner as agr
        cur = getattr(agr, "run_ai_gate_for_candidates", None)
        if not callable(cur):
            return False
        if getattr(cur, "_summary_ai_emp_prob_gate_v3", False):
            return True

        @wraps(cur)
        def wrapped(*args: Any, **kwargs: Any):
            res = cur(*args, **kwargs)
            if not isinstance(res, list):
                return res
            if not any(_env_bool(k, True) for k in ("SUMMARY_AI_EMP_PROB_GATE_ENABLED", "SUMMARY_AI_HIGHLOW_UPDATE_GUARD_ENABLED", "SUMMARY_AI_EXTENSION_GUARD_ENABLED")):
                return res
            out = []
            blocked = passed = fail_open = highlow_blocked = extension_blocked = 0
            for item in res:
                if not isinstance(item, dict) or not bool(item.get("allow")):
                    out.append(item)
                    continue
                x = dict(item)
                ok = True
                reason = ""
                if _env_bool("SUMMARY_AI_EMP_PROB_GATE_ENABLED", True):
                    ok, reason, st = _check(x)
                    x["emp_prob"] = st
                    x["p_target_5m"] = st.get("p_target_5m")
                    x["p_risk_5m"] = st.get("p_risk_5m")
                    x["emp_expected_value"] = st.get("expected_value")
                    x["reason"] = _add_reason(x.get("reason"), reason)
                    if "fail_open" in reason:
                        fail_open += 1
                if ok and _env_bool("SUMMARY_AI_HIGHLOW_UPDATE_GUARD_ENABLED", True):
                    hl_ok, hl_reason, hl_st = _check_highlow(x)
                    x["highlow_update"] = hl_st
                    x["reason"] = _add_reason(x.get("reason"), hl_reason)
                    if "fail_open" in hl_reason:
                        fail_open += 1
                    if not hl_ok:
                        highlow_blocked += 1
                    ok = bool(hl_ok)
                    reason = hl_reason
                if ok and _env_bool("SUMMARY_AI_EXTENSION_GUARD_ENABLED", True):
                    ex_ok, ex_reason, ex_st = _check_extension(x)
                    x["extension_guard"] = ex_st
                    x["reason"] = _add_reason(x.get("reason"), ex_reason)
                    if "fail_open" in ex_reason:
                        fail_open += 1
                    if not ex_ok:
                        extension_blocked += 1
                    ok = bool(ex_ok)
                    reason = ex_reason
                if ok:
                    passed += 1
                else:
                    x["allow"] = False
                    blocked += 1
                    logger.warning("[SUMMARY AI EMP/HIGHLOW/EXT] AI_OK->NG symbol=%s side=%s reason=%s emp=%s highlow=%s extension=%s", x.get("symbol"), x.get("side"), reason, x.get("emp_prob"), x.get("highlow_update"), x.get("extension_guard"))
                out.append(x)
            logger.warning("[SUMMARY AI EMP/HIGHLOW/EXT] result total=%s passed=%s blocked=%s highlow_blocked=%s extension_blocked=%s fail_open=%s version=%s", len(res), passed, blocked, highlow_blocked, extension_blocked, fail_open, VERSION)
            return out

        wrapped._summary_ai_emp_prob_gate_v3 = True  # type: ignore[attr-defined]
        wrapped._summary_ai_emp_prob_gate_v2 = True  # type: ignore[attr-defined]
        wrapped._summary_ai_emp_prob_gate_v1 = True  # type: ignore[attr-defined]
        wrapped._original = cur  # type: ignore[attr-defined]
        agr.run_ai_gate_for_candidates = wrapped
        logger.warning("[SUMMARY AI EMP/HIGHLOW/EXT] patched version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI EMP/HIGHLOW/EXT] patch failed")
        return False


def _defaults() -> None:
    os.environ.setdefault("SUMMARY_AI_EMP_PROB_GATE_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_EMP_PROB_WINDOW_BARS", "5")
    os.environ.setdefault("SUMMARY_AI_EMP_PROB_LOOKBACK_BARS", "180")
    os.environ.setdefault("SUMMARY_AI_EMP_PROB_MIN_SAMPLES", "12")
    os.environ.setdefault("SUMMARY_AI_EMP_PROB_REQUIRE_MIN_SAMPLES", "0")
    os.environ.setdefault("SUMMARY_AI_EMP_PROB_MIN_TARGET_PROB", "0.55")
    os.environ.setdefault("SUMMARY_AI_EMP_PROB_MAX_RISK_PROB", "0.40")
    os.environ.setdefault("SUMMARY_AI_EMP_PROB_MIN_EXPECTED_VALUE", "0.0")
    os.environ.setdefault("SUMMARY_AI_HIGHLOW_UPDATE_GUARD_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_HIGHLOW_REQUIRE_DATA", "0")
    os.environ.setdefault("SUMMARY_AI_HIGHLOW_LOOKBACK_BARS", "120")
    os.environ.setdefault("SUMMARY_AI_HIGHLOW_RECENT_LOOKBACK", "5")
    os.environ.setdefault("SUMMARY_AI_BUY_MAX_DAY_HIGH_UPDATES_3M", "4")
    os.environ.setdefault("SUMMARY_AI_BUY_MAX_DAY_HIGH_UPDATES_5M", "3")
    os.environ.setdefault("SUMMARY_AI_BUY_MAX_RECENT_HIGH_UPDATES", "2")
    os.environ.setdefault("SUMMARY_AI_SELL_MAX_DAY_LOW_UPDATES_3M", "4")
    os.environ.setdefault("SUMMARY_AI_SELL_MAX_DAY_LOW_UPDATES_5M", "3")
    os.environ.setdefault("SUMMARY_AI_SELL_MAX_RECENT_LOW_UPDATES", "2")
    os.environ.setdefault("SUMMARY_AI_EXTENSION_GUARD_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_EXTENSION_REQUIRE_DATA", "0")
    os.environ.setdefault("SUMMARY_AI_EXTENSION_LOOKBACK_BARS", "20")
    os.environ.setdefault("SUMMARY_AI_MAX_MA5_EXTENSION_3M", "0.0040")
    os.environ.setdefault("SUMMARY_AI_MAX_MA5_EXTENSION_5M", "0.0050")
    os.environ.setdefault("SUMMARY_AI_MAX_VWAP_EXTENSION", "0.0060")


def _watch() -> None:
    for _ in range(max(1, _env_int("SUMMARY_AI_EMP_PROB_WATCH_LOOPS", 120))):
        _patch()
        time.sleep(max(0.5, _env_float("SUMMARY_AI_EMP_PROB_WATCH_INTERVAL", 1.0)))


def install() -> bool:
    global _INSTALLED, _WATCHER_STARTED
    if os.environ.get("DISABLE_SUMMARY_AI_EMP_PROB_GATE_PATCH", "").strip() == "1":
        return False
    _defaults()
    ok = _patch()
    if not _WATCHER_STARTED and _env_bool("SUMMARY_AI_EMP_PROB_WATCHER", True):
        _WATCHER_STARTED = True
        threading.Thread(target=_watch, name="summary-ai-emp-highlow-ext-watch", daemon=True).start()
    _INSTALLED = bool(ok or _WATCHER_STARTED)
    logger.warning("[SUMMARY AI EMP/HIGHLOW/EXT] installed ok=%s enabled=%s highlow=%s extension=%s min_p=%s max_risk=%s samples=%s buy_high_3m=%s buy_high_5m=%s max_ma5_3m=%s max_ma5_5m=%s max_vwap=%s version=%s", _INSTALLED, os.environ.get("SUMMARY_AI_EMP_PROB_GATE_ENABLED"), os.environ.get("SUMMARY_AI_HIGHLOW_UPDATE_GUARD_ENABLED"), os.environ.get("SUMMARY_AI_EXTENSION_GUARD_ENABLED"), os.environ.get("SUMMARY_AI_EMP_PROB_MIN_TARGET_PROB"), os.environ.get("SUMMARY_AI_EMP_PROB_MAX_RISK_PROB"), os.environ.get("SUMMARY_AI_EMP_PROB_MIN_SAMPLES"), os.environ.get("SUMMARY_AI_BUY_MAX_DAY_HIGH_UPDATES_3M"), os.environ.get("SUMMARY_AI_BUY_MAX_DAY_HIGH_UPDATES_5M"), os.environ.get("SUMMARY_AI_MAX_MA5_EXTENSION_3M"), os.environ.get("SUMMARY_AI_MAX_MA5_EXTENSION_5M"), os.environ.get("SUMMARY_AI_MAX_VWAP_EXTENSION"), VERSION)
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI EMP/HIGHLOW/EXT] auto install failed")


__all__ = ["VERSION", "install"]