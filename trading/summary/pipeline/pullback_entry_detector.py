# ============================================================
# File   : trading/summary/pipeline/pullback_entry_detector.py
# Version: V1-PULLBACK-ENTRY-DETECTOR
# ------------------------------------------------------------
# 目的:
#   PUSH 1分/3分/5分サマリーから押し目買い・戻り売り候補を作る。
#
# BUY:
#   - 5分の大きな流れが上向き、または close > ma25。
#   - 直近高値から適度に押している。
#   - 3分/5分 MA5付近まで押した後、1分足がMA5上へ復帰。
#   - 反発足の出来高が直近平均以上。
#
# SELL:
#   - 5分の大きな流れが下向き、または close < ma25。
#   - 直近安値から適度に戻している。
#   - 3分/5分 MA5付近まで戻した後、1分足がMA5下へ復帰。
#   - 反落足の出来高が直近平均以上。
#
# 出力:
#   entry_pipeline.run_entry_pipeline() に渡せる row dict list。
# ============================================================
from __future__ import annotations

import logging
import math
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _sf(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ""


def _time_sort(df: pd.DataFrame) -> pd.DataFrame:
    try:
        d = df.copy()
        tc = next((c for c in ("datetime", "end_time", "time", "start_time") if c in d.columns), None)
        if tc:
            d["__dt"] = pd.to_datetime(d[tc], errors="coerce")
            d = d.sort_values(["symbol", "__dt"])
        return d
    except Exception:
        return df


def _summary_history(tf: int) -> pd.DataFrame:
    try:
        from global_state import global_data
        getter = getattr(global_data, "get_summary_history", None)
        df = getter(tf, source="push") if callable(getter) else None
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return pd.DataFrame()
        if "symbol" not in df.columns:
            return pd.DataFrame()
        d = df.copy()
        d["symbol"] = d["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        return _time_sort(d)
    except Exception:
        logger.debug("[PULLBACK ENTRY] summary history failed tf=%s", tf, exc_info=True)
        return pd.DataFrame()


def _last_n(df: pd.DataFrame, symbol: str, n: int) -> pd.DataFrame:
    try:
        d = df[df["symbol"].astype(str) == str(symbol)]
        if d.empty:
            return d
        return d.tail(n)
    except Exception:
        return pd.DataFrame()


def _col(row: pd.Series, names: tuple[str, ...], default: float = 0.0) -> float:
    for n in names:
        try:
            if n in row.index:
                v = row.get(n)
                if v is not None and str(v).strip() != "":
                    return _sf(v, default)
        except Exception:
            pass
    return float(default)


def _vol_avg(rows: pd.DataFrame, n: int = 3) -> float:
    try:
        if rows is None or rows.empty or "volume" not in rows.columns:
            return 0.0
        return float(pd.to_numeric(rows.tail(n)["volume"], errors="coerce").fillna(0).mean())
    except Exception:
        return 0.0


def _turnover(close: float, volume: float, row: pd.Series) -> float:
    t = _col(row, ("turnover", "trading_value", "売買代金"), 0.0)
    if t <= 0 and close > 0 and volume > 0:
        t = close * volume
    return t


def _near_ma(close: float, ma: float, pct: float) -> bool:
    if close <= 0 or ma <= 0:
        return False
    return abs(close - ma) / close * 100.0 <= pct


def _build_row(symbol: str, side: str, row1: pd.Series, row3: pd.Series | None, row5: pd.Series | None, *, score: float, reason: str, pullback_pct: float, vol_ratio: float) -> dict[str, Any]:
    close = _col(row1, ("close", "close_price", "price", "current_price"), 0.0)
    volume = _col(row1, ("volume", "trading_volume", "出来高"), 0.0)
    turnover = _turnover(close, volume, row1)
    out = row1.to_dict()
    out.update({
        "symbol": symbol,
        "side": side,
        "entry_decision": side,
        "source": "SUMMARY_AI",
        "entry_type": "PULLBACK_ENTRY",
        "strategy": "PULLBACK_ENTRY",
        "interval": 1,
        "score": score if side == "BUY" else -score,
        "final_score": score if side == "BUY" else -score,
        "buy_score": score if side == "BUY" else 0.0,
        "sell_score": score if side == "SELL" else 0.0,
        "close": close,
        "price": close,
        "volume": volume,
        "turnover": turnover,
        "pullback_pct": pullback_pct,
        "pullback_vol_ratio": vol_ratio,
        "reason": reason,
        "ai_reason": reason,
        "ai_allow": True,
        "allow": True,
        "lot_multiplier": _env_float("PULLBACK_ENTRY_LOT_RATIO", 0.5),
    })
    if row3 is not None:
        out["close_3m"] = _col(row3, ("close", "close_price", "price", "current_price"), 0.0)
        out["ma5_3m"] = _col(row3, ("ma5", "MA5"), 0.0)
    if row5 is not None:
        out["close_5m"] = _col(row5, ("close", "close_price", "price", "current_price"), 0.0)
        out["ma5_5m"] = _col(row5, ("ma5", "MA5"), 0.0)
        out["ma25_5m"] = _col(row5, ("ma25", "MA25"), 0.0)
    return out


def detect_pullback_entries(max_rows: int | None = None) -> list[dict[str, Any]]:
    if not _env_bool("PULLBACK_ENTRY_ENABLED", True):
        return []

    df1 = _summary_history(1)
    df3 = _summary_history(3)
    df5 = _summary_history(5)
    if df1.empty:
        return []

    max_rows = int(max_rows or _env_int("PULLBACK_ENTRY_MAX_CANDIDATES", 5))
    lookback = max(3, _env_int("PULLBACK_ENTRY_LOOKBACK_BARS", 8))
    near_ma_pct = _env_float("PULLBACK_ENTRY_NEAR_MA_PCT", 0.35)
    min_pb = _env_float("PULLBACK_ENTRY_MIN_PULLBACK_PCT", 0.25)
    max_pb = _env_float("PULLBACK_ENTRY_MAX_PULLBACK_PCT", 1.50)
    min_vol_ratio = _env_float("PULLBACK_ENTRY_MIN_REBOUND_VOL_RATIO", 0.80)
    min_price = _env_float("PULLBACK_ENTRY_MIN_PRICE", _env_float("ENTRY_MIN_PRICE", 200.0))
    min_volume = _env_float("PULLBACK_ENTRY_MIN_VOLUME", _env_float("ENTRY_STRICT_MIN_VOLUME", 30000.0))
    min_turnover = _env_float("PULLBACK_ENTRY_MIN_TURNOVER", _env_float("ENTRY_STRICT_MIN_TURNOVER", 10_000_000.0))

    out: list[dict[str, Any]] = []
    symbols = sorted(set(df1["symbol"].astype(str)))
    for symbol in symbols:
        try:
            h1 = _last_n(df1, symbol, lookback)
            if len(h1) < 3:
                continue
            row1 = h1.iloc[-1]
            prev1 = h1.iloc[-2]
            close = _col(row1, ("close", "close_price", "price", "current_price"), 0.0)
            prev_close = _col(prev1, ("close", "close_price", "price", "current_price"), 0.0)
            ma5_1 = _col(row1, ("ma5", "MA5"), 0.0)
            vol = _col(row1, ("volume", "trading_volume", "出来高"), 0.0)
            vol_avg = _vol_avg(h1.iloc[:-1], 3)
            vol_ratio = (vol / vol_avg) if vol_avg > 0 else 1.0
            turnover = _turnover(close, vol, row1)
            if close < min_price or vol < min_volume or turnover < min_turnover:
                continue

            h3 = _last_n(df3, symbol, 4) if not df3.empty else pd.DataFrame()
            h5 = _last_n(df5, symbol, 6) if not df5.empty else pd.DataFrame()
            row3 = h3.iloc[-1] if len(h3) else None
            row5 = h5.iloc[-1] if len(h5) else None

            ma5_3 = _col(row3, ("ma5", "MA5"), 0.0) if row3 is not None else 0.0
            close3 = _col(row3, ("close", "close_price", "price", "current_price"), 0.0) if row3 is not None else 0.0
            ma5_5 = _col(row5, ("ma5", "MA5"), 0.0) if row5 is not None else 0.0
            ma25_5 = _col(row5, ("ma25", "MA25"), 0.0) if row5 is not None else 0.0
            close5 = _col(row5, ("close", "close_price", "price", "current_price"), 0.0) if row5 is not None else 0.0

            highs = pd.to_numeric(h1.get("high", h1.get("close")), errors="coerce").fillna(0.0)
            lows = pd.to_numeric(h1.get("low", h1.get("close")), errors="coerce").fillna(0.0)
            recent_high = float(highs.max()) if len(highs) else close
            recent_low = float(lows.min()) if len(lows) else close

            # BUY 押し目: 上位足は上、直近高値から押し、MA5近辺から1m MA5上へ復帰。
            pb_buy = ((recent_high - close) / recent_high * 100.0) if recent_high > 0 else 0.0
            trend_buy = (close5 > ma25_5 > 0) or (close5 > ma5_5 > 0) or (close3 > ma5_3 > 0)
            ma_near_buy = _near_ma(close, ma5_1, near_ma_pct) or _near_ma(close, ma5_3, near_ma_pct) or _near_ma(close, ma5_5, near_ma_pct)
            rebound_buy = close > ma5_1 > 0 and close >= prev_close and vol_ratio >= min_vol_ratio
            if trend_buy and ma_near_buy and rebound_buy and min_pb <= pb_buy <= max_pb:
                score = 1.2 + min(1.0, vol_ratio / 3.0) + min(1.0, pb_buy / max_pb)
                out.append(_build_row(symbol, "BUY", row1, row3, row5, score=score, reason=f"PULLBACK_BUY rebound_ma5 pb={pb_buy:.2f}% vol_ratio={vol_ratio:.2f}", pullback_pct=pb_buy, vol_ratio=vol_ratio))

            # SELL 戻り売り: 上位足は下、直近安値から戻し、MA5近辺から1m MA5下へ復帰。
            pb_sell = ((close - recent_low) / recent_low * 100.0) if recent_low > 0 else 0.0
            trend_sell = (close5 < ma25_5 and ma25_5 > 0) or (close5 < ma5_5 and ma5_5 > 0) or (close3 < ma5_3 and ma5_3 > 0)
            ma_near_sell = _near_ma(close, ma5_1, near_ma_pct) or _near_ma(close, ma5_3, near_ma_pct) or _near_ma(close, ma5_5, near_ma_pct)
            rebound_sell = close < ma5_1 and ma5_1 > 0 and close <= prev_close and vol_ratio >= min_vol_ratio
            if trend_sell and ma_near_sell and rebound_sell and min_pb <= pb_sell <= max_pb:
                score = 1.2 + min(1.0, vol_ratio / 3.0) + min(1.0, pb_sell / max_pb)
                out.append(_build_row(symbol, "SELL", row1, row3, row5, score=score, reason=f"PULLBACK_SELL reject_ma5 pb={pb_sell:.2f}% vol_ratio={vol_ratio:.2f}", pullback_pct=pb_sell, vol_ratio=vol_ratio))
        except Exception:
            logger.debug("[PULLBACK ENTRY] symbol failed symbol=%s", symbol, exc_info=True)
            continue

    out.sort(key=lambda r: (float(r.get("score") or 0.0), float(r.get("turnover") or 0.0)), reverse=True)
    if out:
        logger.warning("[PULLBACK ENTRY] detected count=%s top=%s", len(out), [(r.get("symbol"), r.get("side"), round(float(r.get("score") or 0.0), 3), r.get("reason")) for r in out[:max_rows]])
    return out[:max_rows]


__all__ = ["detect_pullback_entries"]
