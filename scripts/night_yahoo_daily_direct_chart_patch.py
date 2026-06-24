# ============================================================
# File   : scripts/night_yahoo_daily_direct_chart_patch.py
# Version: V1-NIGHT-YAHOO-DAILY-DIRECT-CHART-PATCH
# ------------------------------------------------------------
# 夜間の日足更新で yfinance.download が sitecustomize 等の影響で
# 空を返す場合に備え、Yahoo chart API を直接読む fetch 関数へ差し替える。
# ============================================================
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import pandas as pd

LOG = logging.getLogger("night_yahoo_daily_direct_chart_patch")
VERSION = "V1-NIGHT-YAHOO-DAILY-DIRECT-CHART-PATCH"
_PATCHED = False


def _normalize_symbol(value: Any) -> str:
    s = str(value or "").strip().upper()
    if not s or s in {"NAN", "NONE", "NULL", "-"}:
        return ""
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0"):
        s = s[:-2]
    return s.zfill(4) if s.isdigit() and len(s) < 4 else s


def _to_yahoo_ticker(symbol: str) -> str:
    s = _normalize_symbol(symbol)
    return f"{s}.T" if s else ""


def _period_to_start_ts(period: str) -> int:
    now = datetime.now(timezone.utc)
    p = str(period or "3y").strip().lower()
    try:
        if p.endswith("y"):
            days = int(float(p[:-1])) * 365 + 14
        elif p.endswith("mo"):
            days = int(float(p[:-2])) * 31 + 7
        elif p.endswith("d"):
            days = int(float(p[:-1])) + 2
        else:
            days = 365 * 3 + 14
    except Exception:
        days = 365 * 3 + 14
    return int((now - timedelta(days=days)).timestamp())


def _start_to_ts(start: Optional[str], period: str) -> int:
    if start:
        try:
            dt = pd.Timestamp(start).to_pydatetime().replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except Exception:
            pass
    return _period_to_start_ts(period)


def _chart_url(ticker: str, *, start: Optional[str], period: str) -> str:
    p1 = _start_to_ts(start, period)
    p2 = int((datetime.now(timezone.utc) + timedelta(days=1)).timestamp())
    q = urllib.parse.urlencode({
        "period1": p1,
        "period2": p2,
        "interval": "1d",
        "includePrePost": "false",
        "events": "history",
    })
    return f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}?{q}"


def _fetch_chart(ticker: str, *, start: Optional[str], period: str, timeout: float = 20.0) -> pd.DataFrame:
    url = _chart_url(ticker, start=start, period=period)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    chart = payload.get("chart") or {}
    err = chart.get("error")
    if err:
        raise RuntimeError(f"chart_error={err}")
    results = chart.get("result") or []
    if not results:
        return pd.DataFrame()
    r = results[0]
    ts = r.get("timestamp") or []
    quote = (((r.get("indicators") or {}).get("quote") or [{}])[0]) or {}
    adj = (((r.get("indicators") or {}).get("adjclose") or [{}])[0]) or {}
    if not ts:
        return pd.DataFrame()
    df = pd.DataFrame({
        "date": pd.to_datetime(ts, unit="s", utc=True).tz_convert("Asia/Tokyo").tz_localize(None).normalize(),
        "open": quote.get("open") or [],
        "high": quote.get("high") or [],
        "low": quote.get("low") or [],
        "close": quote.get("close") or [],
        "volume": quote.get("volume") or [],
    })
    adj_close = adj.get("adjclose") or None
    if adj_close is not None and len(adj_close) == len(df):
        df["adj_close"] = adj_close
    else:
        df["adj_close"] = df["close"]
    for c in ["open", "high", "low", "close", "adj_close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["date", "open", "high", "low", "close"]).copy()
    df["volume"] = df["volume"].fillna(0)
    return df


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    try:
        import scripts.night_yahoo_daily_update_batch as target
    except Exception:
        LOG.warning("[NIGHT YAHOO DIRECT CHART] target import failed", exc_info=True)
        return False

    original_fetch = getattr(target, "_fetch_daily_one", None)
    if not callable(original_fetch):
        LOG.warning("[NIGHT YAHOO DIRECT CHART] original _fetch_daily_one missing")
        return False

    def patched_fetch_daily_one(symbol: str, *, period: str, start: Optional[str]):
        ticker = _to_yahoo_ticker(symbol)
        errors: list[str] = []
        if ticker:
            try:
                raw = _fetch_chart(
                    ticker,
                    start=start,
                    period=period,
                    timeout=float(os.environ.get("NIGHT_YAHOO_DIRECT_TIMEOUT", "20")),
                )
                if raw is not None and not raw.empty:
                    raw["symbol"] = _normalize_symbol(symbol)
                    raw = raw.drop_duplicates(subset=["symbol", "date"], keep="last").sort_values("date")
                    LOG.info("[NIGHT YAHOO DIRECT CHART] ok symbol=%s ticker=%s rows=%s", symbol, ticker, len(raw))
                    return raw[["symbol", "date", "open", "high", "low", "close", "adj_close", "volume"]].copy()
                errors.append("chart_empty")
            except Exception as e:
                errors.append(f"chart_error={type(e).__name__}:{e}")
                if str(os.environ.get("NIGHT_YAHOO_DIRECT_ONLY", "0")).strip().lower() in {"1", "true", "yes", "on"}:
                    LOG.warning("[NIGHT YAHOO DIRECT CHART] no daily data symbol=%s ticker=%s reason=%s", symbol, ticker, ";".join(errors))
                    return pd.DataFrame()

        try:
            out = original_fetch(symbol, period=period, start=start)
            if out is not None and not out.empty:
                return out
            errors.append("original_empty")
        except Exception as e:
            errors.append(f"original_error={type(e).__name__}:{e}")

        LOG.warning("[NIGHT YAHOO DIRECT CHART] no daily data symbol=%s ticker=%s reason=%s", symbol, ticker, ";".join(errors))
        return pd.DataFrame()

    target._fetch_daily_one = patched_fetch_daily_one
    _PATCHED = True
    LOG.warning("[NIGHT YAHOO DIRECT CHART] installed version=%s", VERSION)
    return True
