# ============================================================
# File   : trading/entry/volume_direction_guard.py
# Version: V5-TONOSAMA-3M5M-CANDLE-GUARD-SOFT-MISSING
# ------------------------------------------------------------
# 目的:
#   SUMMARY / RANKING / TONOSAMA の3種類すべてのエントリーで、
#   出来高急増を単独評価せず、価格方向と直前トレンドを合わせて判定する。
#
# V5 Fix:
#   - TONOSAMA pending から entry_controller に渡る行では、3m/5m専用OHLC
#     が欠けることがある。その場合に _ohlc_from_row() が unsuffixed の
#     open/close を3m/5mとして流用し、
#       TONOSAMA_BUY_REJECT_NO_STRONG_3M5M_BULLISH_CANDLE
#     で hard reject していた。
#   - ログ上も vol=0.000 move=0.000 trend=0.500 で、実際には方向判定用
#     データ不足なのに「強い3m/5m陽線なし」として落としていた。
#   - TONOSAMAの3m/5m candle guardでは、まず interval専用OHLCだけを見る。
#     無ければ summary_loader から補完する。
#   - それでも directional evidence が弱い/欠損の場合は、既定で WARN 扱い
#     にして Tonosama dedicated gate / pending側の判定を優先する。
#
# Safety:
#   - 1m MA5方向ガード、出来高方向ガードは維持。
#   - 明確な同方向クライマックス/逆方向データがある場合の拒否は維持可能。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import time
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_RANKING_MA_CACHE: dict[str, tuple[float, dict[str, float]]] = {}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(raw)
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_str(v: Any, default: str = "") -> str:
    try:
        if v is None:
            return default
        return str(v).strip()
    except Exception:
        return default


def _first_num(row: dict, names: list[str], default: float = 0.0) -> float:
    for n in names:
        if n in row:
            val = _safe_float(row.get(n), None)  # type: ignore[arg-type]
            if val is not None:
                return float(val)
    return float(default)


def _symbol(row: dict) -> str:
    return _safe_str(row.get("symbol") or row.get("Symbol") or row.get("code") or row.get("銘柄コード"))


def _source(row: dict) -> str:
    return _safe_str(row.get("source") or row.get("entry_type") or row.get("pipeline_source")).upper()


def _is_tonosama(row: dict) -> bool:
    src = _source(row)
    et = _safe_str(row.get("entry_type")).upper()
    return src == "TONOSAMA" or et == "TONOSAMA"


def _uses_ranking_snapshot_ma5(row: dict) -> bool:
    if not _env_bool("ENTRY_RANKING_SNAPSHOT_MA5_GUARD", True):
        return False
    src = _source(row)
    et = _safe_str(row.get("entry_type")).upper()
    return src in {"RANKING", "TONOSAMA"} or et in {"RANKING", "TONOSAMA"}


def _infer_side(row: dict) -> str:
    side = _safe_str(row.get("side") or row.get("entry_decision") or row.get("signal") or "").upper()
    if side in {"BUY", "SELL"}:
        return side
    sb = _first_num(row, ["score_buy", "buy_score"], 0.0)
    ss = _first_num(row, ["score_sell", "sell_score"], 0.0)
    if ss > sb and ss > 0:
        return "SELL"
    if sb > ss and sb > 0:
        return "BUY"
    score = _first_num(row, ["score", "final_score", "display_score"], 0.0)
    return "SELL" if score < 0 else "BUY"


def _close_price(row: dict) -> float:
    return _first_num(row, ["close", "close_price", "current_price", "price", "ranking_price", "snapshot_price"], 0.0)


def _ranking_db_path() -> str:
    try:
        from ats.ats_ranking.db_path import resolve_ranking_db_path
        p = resolve_ranking_db_path()
        if p:
            return str(p)
    except Exception:
        pass
    today = dt.datetime.now().strftime("%Y%m%d")
    root = os.getenv("AUTOSTOCK_ROOT", r"\\192.168.0.22\AutoStockBuyAndSell")
    return os.path.join(root, "raw_data", "kabu_station", "ranking", f"ranking{today}.db")


def _find_price_col(conn: sqlite3.Connection, table: str) -> str | None:
    try:
        cols = [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
        for c in ["current_price", "price", "close", "close_price", "現在値", "現在値詳細", "last_price"]:
            if c in cols:
                return c
    except Exception:
        pass
    return None


def _ranking_snapshot_ma_from_db(symbol: str) -> dict[str, float]:
    if not symbol:
        return {}
    ttl = _env_float("ENTRY_RANKING_MA5_CACHE_TTL_SEC", 20.0)
    now = time.time()
    cached = _RANKING_MA_CACHE.get(symbol)
    if cached and now - cached[0] <= ttl:
        return dict(cached[1])
    try:
        db = _ranking_db_path()
        if not db or not os.path.exists(db):
            return {}
        limit = int(max(6, _env_float("ENTRY_RANKING_MA5_LOOKBACK_ROWS", 20.0)))
        with sqlite3.connect(db, timeout=1.0) as conn:
            table = "ranking_snapshot_1min"
            price_col = _find_price_col(conn, table)
            if not price_col:
                return {}
            rows = conn.execute(
                f"SELECT datetime, {price_col} FROM {table} WHERE symbol=? ORDER BY datetime DESC LIMIT ?",
                (str(symbol), limit),
            ).fetchall()
        if len(rows) < 2:
            return {}
        df = pd.DataFrame(rows, columns=["datetime", "price"])
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df = df.dropna(subset=["price"]).sort_values("datetime")
        if len(df) < 2:
            return {}
        prices = df["price"].tail(6).tolist()
        ma5 = float(pd.Series(prices[-5:]).mean()) if len(prices) >= 5 else float(pd.Series(prices).mean())
        prev_prices = prices[:-1]
        prev_ma5 = float(pd.Series(prev_prices[-5:]).mean()) if len(prev_prices) >= 2 else 0.0
        slope = ((ma5 - prev_ma5) / prev_ma5 * 100.0) if prev_ma5 > 0 else 0.0
        out = {"ma5": ma5, "prev_ma5": prev_ma5, "ma5_slope": slope, "source": "ranking_snapshot_db"}
        _RANKING_MA_CACHE[symbol] = (now, out)
        return out
    except Exception:
        logger.exception("[ENTRY VOLUME DIRECTION GUARD] ranking snapshot MA5 load failed symbol=%s", symbol)
        return {}


def _ranking_ma5_from_row(row: dict) -> dict[str, float]:
    ma5 = _first_num(row, ["ranking_ma5", "ranking_ma5_1m", "rank_ma5", "snapshot_ma5", "ranking_snapshot_ma5", "ma5_ranking", "ma5_ranking_snapshot"], 0.0)
    prev = _first_num(row, ["ranking_prev_ma5", "ranking_ma5_prev", "prev_ranking_ma5", "snapshot_prev_ma5", "prev_snapshot_ma5"], 0.0)
    slope = _first_num(row, ["ranking_ma5_slope", "ranking_ma5_slope_1m", "snapshot_ma5_slope", "ranking_slope_ma5"], 0.0)
    if abs(slope) <= 0 and ma5 > 0 and prev > 0:
        slope = (ma5 - prev) / prev * 100.0
    if ma5 > 0:
        return {"ma5": ma5, "prev_ma5": prev, "ma5_slope": slope, "source": "ranking_snapshot_row"}
    return {}


def _ranking_ma5(row: dict) -> dict[str, float]:
    if not _uses_ranking_snapshot_ma5(row):
        return {}
    row_ma = _ranking_ma5_from_row(row)
    if row_ma:
        return row_ma
    db_ma = _ranking_snapshot_ma_from_db(_symbol(row))
    if db_ma:
        return db_ma
    return {}


def _ma5_1m(row: dict) -> float:
    r = _ranking_ma5(row)
    if r.get("ma5", 0.0) > 0:
        return float(r["ma5"])
    return _first_num(row, ["ma5_1m", "ma5", "MA5", "sma5", "SMA5"], 0.0)


def _ma5_slope_1m(row: dict) -> float:
    r = _ranking_ma5(row)
    if r and abs(float(r.get("ma5_slope", 0.0))) > 0:
        return float(r.get("ma5_slope", 0.0))
    direct = _first_num(row, ["ma5_slope_1m", "ma5_slope", "slope_ma5_1m", "slope_ma5", "ma5_delta", "ma5_delta_1m"], 0.0)
    if abs(direct) > 0:
        return direct
    ma5 = _ma5_1m(row)
    prev_ma5 = _first_num(row, ["prev_ma5_1m", "prev_ma5", "ma5_prev", "ma5_1m_prev"], 0.0)
    r = _ranking_ma5(row)
    if r.get("prev_ma5", 0.0) > 0:
        prev_ma5 = float(r["prev_ma5"])
    if ma5 > 0 and prev_ma5 > 0:
        return (ma5 - prev_ma5) / prev_ma5 * 100.0
    return _first_num(row, ["slope_1m", "slope", "_slope"], 0.0)


def _evaluate_ma5_direction(row: dict, side: str) -> dict:
    if not _env_bool("ENTRY_1M_MA5_DIRECTION_GUARD", True):
        return {"ok": True, "reason": "MA5_GUARD_DISABLED", "action": "PASS"}
    close = _close_price(row)
    ma5 = _ma5_1m(row)
    rma = _ranking_ma5(row)
    ma_source = rma.get("source", "summary_row") if rma else "summary_row"
    if close <= 0 or ma5 <= 0:
        return {"ok": True, "reason": "MA5_DATA_MISSING_PASS", "action": "PASS", "close": close, "ma5": ma5, "ma5_source": ma_source}
    slope = _ma5_slope_1m(row)
    slope_eps = _env_float("ENTRY_1M_MA5_SLOPE_EPS", 0.0001)
    gap_eps_pct = _env_float("ENTRY_1M_MA5_PRICE_GAP_EPS_PCT", 0.0)
    gap_pct = (close - ma5) / ma5 * 100.0
    reject = False
    reason = "MA5_DIRECTION_OK"
    if side == "BUY" and slope < -slope_eps and gap_pct < -gap_eps_pct:
        reject = True
        reason = "BUY_REJECT_1M_MA5_DOWN_AND_PRICE_BELOW"
    elif side == "SELL" and slope > slope_eps and gap_pct > gap_eps_pct:
        reject = True
        reason = "SELL_REJECT_1M_MA5_UP_AND_PRICE_ABOVE"
    if reject and ma_source.startswith("ranking_snapshot"):
        reason += "_RANKING_SNAPSHOT"
    if reject and not _env_bool("ENTRY_1M_MA5_DIRECTION_REJECT", True):
        return {"ok": True, "reason": reason + "_WARN_ONLY", "action": "WARN", "side": side, "close": close, "ma5": ma5, "ma5_slope": slope, "price_ma5_gap_pct": gap_pct, "ma5_source": ma_source}
    return {"ok": not reject, "reason": reason, "action": "REJECT" if reject else "PASS", "side": side, "close": close, "ma5": ma5, "ma5_slope": slope, "price_ma5_gap_pct": gap_pct, "ma5_source": ma_source}


def _interval_specific_names(interval: int, base: str) -> list[str]:
    suffixes = [f"_{interval}m", f"{interval}m", f"_{interval}min", f"{interval}min"]
    out = [f"{base}{s}" for s in suffixes]
    if base == "open":
        out += [f"open_price{s}" for s in suffixes]
        out += [f"o{interval}"]
        if interval == 5:
            out += ["five_min_open"]
        if interval == 3:
            out += ["three_min_open"]
    elif base == "high":
        out += [f"high_price{s}" for s in suffixes]
        out += [f"h{interval}"]
        if interval == 5:
            out += ["five_min_high"]
        if interval == 3:
            out += ["three_min_high"]
    elif base == "low":
        out += [f"low_price{s}" for s in suffixes]
        out += [f"l{interval}"]
        if interval == 5:
            out += ["five_min_low"]
        if interval == 3:
            out += ["three_min_low"]
    elif base == "close":
        out += [f"close_price{s}" for s in suffixes]
        out += [f"c{interval}"]
        if interval == 5:
            out += ["five_min_close"]
        if interval == 3:
            out += ["three_min_close"]
    return out


def _ohlc_from_row(row: dict, interval: int, *, allow_unsuffixed: bool = True) -> tuple[float, float, float, float, str]:
    open_names = _interval_specific_names(interval, "open")
    high_names = _interval_specific_names(interval, "high")
    low_names = _interval_specific_names(interval, "low")
    close_names = _interval_specific_names(interval, "close")
    open_p = _first_num(row, open_names, 0.0)
    high_p = _first_num(row, high_names, 0.0)
    low_p = _first_num(row, low_names, 0.0)
    close_p = _first_num(row, close_names, 0.0)
    if open_p > 0 and close_p > 0:
        return open_p, high_p, low_p, close_p, "entry_row_interval_specific"
    if allow_unsuffixed:
        open_p = _first_num(row, ["open", "open_price"], 0.0)
        high_p = _first_num(row, ["high", "high_price"], 0.0)
        low_p = _first_num(row, ["low", "low_price"], 0.0)
        close_p = _first_num(row, ["close", "close_price", "current_price", "price"], 0.0)
        return open_p, high_p, low_p, close_p, "entry_row_unsuffixed"
    return 0.0, 0.0, 0.0, 0.0, "entry_row_interval_missing"


def _load_candle_from_summary(symbol: str, interval: int) -> tuple[float, float, float, float, str]:
    if not symbol:
        return 0.0, 0.0, 0.0, 0.0, "summary_no_symbol"
    try:
        from trading.entry.tonosama.summary_loader import load_merged_summary, normalize_summary_base
        raw = load_merged_summary(interval)
        x = normalize_summary_base(raw, interval=interval)
        if x is None or x.empty or "symbol" not in x.columns:
            return 0.0, 0.0, 0.0, 0.0, f"summary_{interval}m_empty"
        x = x.copy()
        x["symbol"] = x["symbol"].astype(str)
        x = x[x["symbol"] == str(symbol)]
        if x.empty:
            return 0.0, 0.0, 0.0, 0.0, f"summary_{interval}m_symbol_missing"
        if "datetime" in x.columns:
            x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
            x = x.sort_values("datetime")
        r = x.tail(1).iloc[0].to_dict()
        o, h, l, c, _src = _ohlc_from_row(r, interval, allow_unsuffixed=True)
        return o, h, l, c, f"summary_{interval}m"
    except Exception:
        logger.exception("[ENTRY VOLUME DIRECTION GUARD] %sm candle load failed symbol=%s", interval, symbol)
        return 0.0, 0.0, 0.0, 0.0, f"summary_{interval}m_error"


def _eval_candle(row: dict, side: str, interval: int) -> dict:
    # TONOSAMAでは unsuffixed の1m OHLCを3m/5mとして誤用しない。
    allow_unsuffixed = not _is_tonosama(row)
    open_p, high_p, low_p, close_p, source = _ohlc_from_row(row, interval, allow_unsuffixed=allow_unsuffixed)
    if open_p <= 0 or close_p <= 0:
        open_p, high_p, low_p, close_p, source = _load_candle_from_summary(_symbol(row), interval)
    if open_p <= 0 or close_p <= 0:
        return {"ok": False, "usable": False, "reason": f"TONOSAMA_{interval}M_CANDLE_MISSING", "interval": interval, "source": source}
    body_pct = (close_p - open_p) / open_p * 100.0
    abs_body_pct = abs(body_pct)
    min_body_pct = _env_float(f"TONOSAMA_{interval}M_CANDLE_MIN_BODY_PCT", _env_float("TONOSAMA_3M5M_CANDLE_MIN_BODY_PCT", 0.03))
    upper = max(0.0, high_p - max(open_p, close_p)) if high_p > 0 else 0.0
    lower = max(0.0, min(open_p, close_p) - low_p) if low_p > 0 else 0.0
    body = abs(close_p - open_p)
    wick_base = max(upper, lower, 0.000001)
    body_to_wick = body / wick_base
    min_body_to_wick = _env_float("TONOSAMA_3M5M_CANDLE_MIN_BODY_TO_WICK", 0.35)
    ok = True
    reason = f"TONOSAMA_{interval}M_CANDLE_OK"
    if side == "BUY":
        if body_pct <= 0:
            ok = False
            reason = f"TONOSAMA_BUY_REJECT_{interval}M_NOT_BULLISH"
        elif abs_body_pct < min_body_pct:
            ok = False
            reason = f"TONOSAMA_BUY_REJECT_{interval}M_BULL_BODY_TOO_SMALL"
        elif body_to_wick < min_body_to_wick:
            ok = False
            reason = f"TONOSAMA_BUY_REJECT_{interval}M_BODY_WEAK_VS_WICK"
    elif side == "SELL":
        if body_pct >= 0:
            ok = False
            reason = f"TONOSAMA_SELL_REJECT_{interval}M_NOT_BEARISH"
        elif abs_body_pct < min_body_pct:
            ok = False
            reason = f"TONOSAMA_SELL_REJECT_{interval}M_BEAR_BODY_TOO_SMALL"
        elif body_to_wick < min_body_to_wick:
            ok = False
            reason = f"TONOSAMA_SELL_REJECT_{interval}M_BODY_WEAK_VS_WICK"
    return {"ok": ok, "usable": True, "reason": reason, "interval": interval, "side": side, "open": open_p, "high": high_p, "low": low_p, "close": close_p, "body_pct": body_pct, "body_to_wick": body_to_wick, "source": source}


def _tonosama_soft_allow_candle(row: dict, side: str, eval3: dict, eval5: dict) -> tuple[bool, str]:
    if not _env_bool("TONOSAMA_3M5M_CANDLE_SOFT_ALLOW_IF_INCONCLUSIVE", True):
        return False, "disabled"
    # 3m/5m専用データが取れず、entry_rowの1m値だけで拒否していたケースを救済。
    usable = [e for e in (eval3, eval5) if e.get("usable")]
    interval_specific = [e for e in usable if str(e.get("source") or "").startswith("entry_row_interval_specific")]
    summary_src = [e for e in usable if str(e.get("source") or "").startswith("summary_")]
    if not usable:
        return True, "no_usable_candle_failopen"
    # summaryから取れていても、pending側のTonosama score/AIが強い場合はwarnに留める。
    score = abs(_first_num(row, ["score", "final_score", "display_score", "score_buy", "score_sell"], 0.0))
    vol = _volume_surge_ratio(row)
    move = abs(_price_change_pct(row))
    trend = abs(_trend_score(row))
    min_score = _env_float("TONOSAMA_3M5M_CANDLE_SOFT_MIN_SCORE", 2.0)
    # ログで vol=0/move=0 のような「entry_controller側で特徴量が落ちている」場合は、scoreだけで判定する。
    if score >= min_score and (not interval_specific or summary_src or (vol <= 0 and move <= 0 and trend <= 0.75)):
        return True, f"soft_score_pass score={score:.3f} vol={vol:.3f} move={move:.3f} trend={trend:.3f}"
    return False, f"soft_ng score={score:.3f} vol={vol:.3f} move={move:.3f} trend={trend:.3f}"


def _evaluate_tonosama_3m5m_candle(row: dict, side: str) -> dict:
    if not _env_bool("TONOSAMA_3M5M_CANDLE_GUARD", True):
        return {"ok": True, "reason": "TONOSAMA_3M5M_CANDLE_DISABLED", "action": "PASS"}
    if not _is_tonosama(row):
        return {"ok": True, "reason": "NOT_TONOSAMA", "action": "PASS"}
    eval3 = _eval_candle(row, side, 3)
    eval5 = _eval_candle(row, side, 5)
    usable = [e for e in [eval3, eval5] if e.get("usable")]
    passed = [e for e in usable if e.get("ok")]
    mode = _safe_str(os.getenv("TONOSAMA_3M5M_CANDLE_MODE") or "ANY").upper()
    if mode == "BOTH":
        ok = len(usable) >= 2 and len(passed) >= 2
    else:
        ok = len(passed) >= 1
    if not usable:
        if _env_bool("TONOSAMA_3M5M_CANDLE_FAIL_OPEN", True):
            return {"ok": True, "reason": "TONOSAMA_3M5M_CANDLE_MISSING_PASS", "action": "PASS", "candle_3m": eval3, "candle_5m": eval5}
        return {"ok": False, "reason": "TONOSAMA_REJECT_3M5M_CANDLE_MISSING", "action": "REJECT", "candle_3m": eval3, "candle_5m": eval5}
    if not ok:
        reason = "TONOSAMA_BUY_REJECT_NO_STRONG_3M5M_BULLISH_CANDLE" if side == "BUY" else "TONOSAMA_SELL_REJECT_NO_STRONG_3M5M_BEARISH_CANDLE"
        soft_ok, soft_reason = _tonosama_soft_allow_candle(row, side, eval3, eval5)
        if soft_ok:
            return {"ok": True, "reason": reason + "_WARN_ONLY_" + soft_reason, "action": "WARN", "mode": mode, "candle_3m": eval3, "candle_5m": eval5}
        if not _env_bool("TONOSAMA_3M5M_CANDLE_REJECT", True):
            return {"ok": True, "reason": reason + "_WARN_ONLY", "action": "WARN", "mode": mode, "candle_3m": eval3, "candle_5m": eval5}
        return {"ok": False, "reason": reason, "action": "REJECT", "mode": mode, "candle_3m": eval3, "candle_5m": eval5, "soft_reason": soft_reason}
    return {"ok": True, "reason": "TONOSAMA_3M5M_CANDLE_OK", "action": "PASS", "mode": mode, "candle_3m": eval3, "candle_5m": eval5}


def _price_change_pct(row: dict) -> float:
    direct = _first_num(row, ["_max_price_change_pct", "price_change_pct", "price_change_pct_1m", "price_change_1m_pct", "price_change_5s_pct"], 0.0)
    if abs(direct) > 0:
        return direct
    close = _close_price(row)
    prev = _first_num(row, ["prev_close", "prev_close_1m", "prev_close_3m", "prev_close_5m"], 0.0)
    if close > 0 and prev > 0:
        return (close - prev) / prev * 100.0
    open_p = _first_num(row, ["open", "open_price"], 0.0)
    if close > 0 and open_p > 0:
        return (close - open_p) / open_p * 100.0
    return 0.0


def _volume_surge_ratio(row: dict) -> float:
    vals = [_first_num(row, ["_max_volume_surge_ratio"], 0.0), _first_num(row, ["volume_surge_ratio", "volume_surge_ratio_1m"], 0.0), _first_num(row, ["volume_surge_ratio_3m"], 0.0), _first_num(row, ["volume_surge_ratio_5m"], 0.0), _first_num(row, ["volume_surge_ratio_5s"], 0.0)]
    return max([v for v in vals if v is not None] or [0.0])


def _trend_score(row: dict) -> float:
    score = 0.0
    slope = _first_num(row, ["_slope", "slope", "slope_1m", "score_slope", "slope_atr_scaled"], 0.0)
    slope_min = _env_float("ENTRY_VOLUME_DIRECTION_SLOPE_EPS", 0.001)
    if slope > slope_min:
        score += 1.0
    elif slope < -slope_min:
        score -= 1.0
    for name in ["prev_3m_up_streak", "prev_5m_up_streak", "up_streak", "prev_up_streak"]:
        if _first_num(row, [name], 0.0) >= 2:
            score += 1.0
            break
    for name in ["prev_3m_down_streak", "prev_5m_down_streak", "down_streak", "prev_down_streak"]:
        if _first_num(row, [name], 0.0) >= 2:
            score -= 1.0
            break
    d3 = _first_num(row, ["prev_3m_last_delta_pct", "price_change_pct_3m"], 0.0)
    d5 = _first_num(row, ["prev_5m_last_delta_pct", "price_change_pct_5m"], 0.0)
    delta_eps = _env_float("ENTRY_VOLUME_DIRECTION_DELTA_EPS", 0.05)
    if d3 > delta_eps or d5 > delta_eps:
        score += 0.75
    elif d3 < -delta_eps or d5 < -delta_eps:
        score -= 0.75
    close = _close_price(row)
    ma5 = _ma5_1m(row)
    if close > 0 and ma5 > 0:
        gap = (close - ma5) / ma5 * 100.0
        ma_gap_eps = _env_float("ENTRY_VOLUME_DIRECTION_MA_GAP_EPS", 0.05)
        if gap > ma_gap_eps:
            score += 0.5
        elif gap < -ma_gap_eps:
            score -= 0.5
    return score


def evaluate_volume_direction(row: dict) -> dict:
    if not _env_bool("ENTRY_VOLUME_DIRECTION_GUARD", True):
        return {"ok": True, "reason": "DISABLED", "action": "PASS"}
    if not isinstance(row, dict):
        return {"ok": True, "reason": "ROW_INVALID_PASS", "action": "PASS"}
    side = _infer_side(row)
    candle_eval = _evaluate_tonosama_3m5m_candle(row, side)
    if not candle_eval.get("ok", True):
        return {"ok": False, "reason": candle_eval.get("reason"), "action": "REJECT", "side": side, "candle_eval": candle_eval, "volume_surge_ratio": _volume_surge_ratio(row), "price_change_pct": _price_change_pct(row), "trend_score": _trend_score(row), "trend_direction": "TONOSAMA_3M5M_CANDLE_REJECT"}
    ma5_eval = _evaluate_ma5_direction(row, side)
    if not ma5_eval.get("ok", True):
        return {"ok": False, "reason": ma5_eval.get("reason"), "action": "REJECT", "side": side, "ma5_eval": ma5_eval, "candle_eval": candle_eval, "volume_surge_ratio": _volume_surge_ratio(row), "price_change_pct": _price_change_pct(row), "trend_score": _trend_score(row), "trend_direction": "MA5_REJECT"}
    vol = _volume_surge_ratio(row)
    min_vol = _env_float("ENTRY_VOLUME_DIRECTION_MIN_SURGE_RATIO", 2.0)
    if vol < min_vol:
        return {"ok": True, "reason": "VOLUME_SURGE_LOW", "action": "PASS", "side": side, "volume_surge_ratio": vol, "ma5_eval": ma5_eval, "candle_eval": candle_eval}
    move = _price_change_pct(row)
    move_eps = _env_float("ENTRY_VOLUME_DIRECTION_PRICE_EPS_PCT", 0.15)
    trend = _trend_score(row)
    trend_eps = _env_float("ENTRY_VOLUME_DIRECTION_TREND_EPS", 0.75)
    if move > move_eps:
        direction = "UP"
    elif move < -move_eps:
        direction = "DOWN"
    elif trend > trend_eps:
        direction = "UP_FROM_FLAT"
    elif trend < -trend_eps:
        direction = "DOWN_FROM_FLAT"
    else:
        direction = "FLAT_UNKNOWN"
    reject = False
    if side == "BUY" and direction in {"UP", "UP_FROM_FLAT"}:
        reject = True
        reason = "BUY_REJECT_VOLUME_SURGE_AFTER_UP_MOVE"
    elif side == "SELL" and direction in {"DOWN", "DOWN_FROM_FLAT"}:
        reject = True
        reason = "SELL_REJECT_VOLUME_SURGE_AFTER_DOWN_MOVE"
    else:
        reason = "VOLUME_DIRECTION_OK"
    if reject and not _env_bool("ENTRY_VOLUME_DIRECTION_REJECT", True):
        return {"ok": True, "reason": reason + "_WARN_ONLY", "action": "WARN", "side": side, "volume_surge_ratio": vol, "price_change_pct": move, "trend_score": trend, "trend_direction": direction, "ma5_eval": ma5_eval, "candle_eval": candle_eval}
    return {"ok": not reject, "reason": reason, "action": "REJECT" if reject else "PASS", "side": side, "volume_surge_ratio": vol, "price_change_pct": move, "trend_score": trend, "trend_direction": direction, "ma5_eval": ma5_eval, "candle_eval": candle_eval}


__all__ = ["evaluate_volume_direction"]
