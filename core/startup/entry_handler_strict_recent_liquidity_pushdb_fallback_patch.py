# -*- coding: utf-8 -*-
"""
Push DB fallback for entry_handler strict recent-liquidity guard.

When entry_handler's DB-backed recent-liquidity read is stale and the in-memory
GlobalContext summary does not contain the symbol, use the fresh pushYYYYMMDD.db
raw rows already read by summary_main_push_db_refresh_patch. This remains
fail-closed: only a fresh matching symbol row with positive volume and turnover
can bypass STRICT_FINAL_LIQ_STALE.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-PUSH-DB-FALLBACK-FOR-STRICT-FINAL-LIQ"
_INSTALLED = False
_ORIG_GLOBAL_CONTEXT_LIQ = None


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        if isinstance(v, str):
            s = v.strip().replace(",", "").replace("円", "").replace("株", "")
            if s == "" or s.lower() in {"none", "nan", "null", "<na>", "pd.na", "-", "－", "—"}:
                return float(default)
            v = s
        x = float(v)
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _parse_dt(v: Any) -> dt.datetime | None:
    try:
        if v is None:
            return None
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None)
        if hasattr(v, "to_pydatetime"):
            x = v.to_pydatetime()
            if isinstance(x, dt.datetime):
                return x.replace(tzinfo=None)
        s = str(v).strip()
        if not s or s.lower() in {"none", "nan", "nat", "null", "<na>"}:
            return None
        for fmt in (
            "%Y-%m-%d %H:%M:%S.%f",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
            "%Y%m%d %H:%M:%S",
            "%Y%m%d%H%M%S",
        ):
            try:
                return dt.datetime.strptime(s, fmt)
            except Exception:
                pass
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _row_value(row: dict[str, Any]) -> dict[str, Any] | None:
    volume = _f(
        row.get("volume") or row.get("latest_volume") or row.get("display_volume")
        or row.get("vol") or row.get("Volume") or row.get("TradingVolume") or row.get("出来高"),
        0.0,
    )
    price = _f(
        row.get("close_price") or row.get("price") or row.get("current_price") or row.get("close")
        or row.get("CurrentPrice") or row.get("Price") or row.get("現在値"),
        0.0,
    )
    turnover = _f(
        row.get("turnover") or row.get("trading_value") or row.get("display_turnover")
        or row.get("TradingValue") or row.get("売買代金"),
        0.0,
    )
    if turnover <= 0 and price > 0 and volume > 0:
        turnover = price * volume
    latest_dt = None
    for c in ("datetime", "received_at", "created_at", "updated_at", "time", "PriceTime", "current_price_time"):
        latest_dt = _parse_dt(row.get(c))
        if latest_dt is not None:
            break
    if volume <= 0 or turnover <= 0:
        return None
    return {"volume": volume, "price": price, "turnover": turnover, "dt": latest_dt}


def _push_db_liquidity_values(symbol: str, bars: int) -> dict[str, Any]:
    sym = _norm_symbol(symbol)
    try:
        import pandas as pd
        from core.startup import summary_main_push_db_refresh_patch as refresh

        reader = getattr(refresh, "_read_push_db_recent", None)
        if not callable(reader):
            return {"ok_read": False, "reason": "push_db_reader_missing", "source": "push_db_recent_raw"}
        df = reader()
        if not isinstance(df, pd.DataFrame) or df.empty:
            return {"ok_read": False, "reason": "push_db_empty", "source": "push_db_recent_raw"}
        cols = list(df.columns)
        sym_col = next((c for c in ("symbol", "code", "Symbol", "銘柄コード") if c in cols), None)
        if not sym_col:
            return {"ok_read": False, "reason": "push_db_symbol_col_missing", "source": "push_db_recent_raw", "cols": cols[:30]}
        s = df[sym_col].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        sub = df.loc[s == sym].copy()
        if sub.empty:
            return {"ok_read": False, "reason": "push_db_no_symbol", "source": "push_db_recent_raw", "symbol": sym, "rows": len(df)}
        dt_col = next((c for c in ("datetime", "received_at", "created_at", "updated_at", "time", "PriceTime", "current_price_time") if c in sub.columns), None)
        if dt_col:
            try:
                sub["__liq_dt"] = pd.to_datetime(sub[dt_col], errors="coerce").dt.tz_localize(None)
            except Exception:
                sub["__liq_dt"] = pd.to_datetime(sub[dt_col], errors="coerce")
            sub = sub.sort_values("__liq_dt")
        rows = []
        for _, r in sub.tail(max(1, int(bars))).iterrows():
            rv = _row_value(r.to_dict())
            if rv is not None:
                rows.append(rv)
        if not rows:
            return {"ok_read": False, "reason": "push_db_no_positive_liquidity", "source": "push_db_recent_raw", "symbol": sym, "symbol_rows": len(sub)}
        latest = rows[-1]
        latest_dt = latest.get("dt")
        return {
            "ok_read": True,
            "rows": len(rows),
            "source": "push_db_recent_raw",
            "latest_dt": latest_dt.isoformat(sep=" ") if isinstance(latest_dt, dt.datetime) else None,
            "latest_close": latest.get("price"),
            "latest_volume": latest.get("volume"),
            "latest_turnover": latest.get("turnover"),
            "volume_sum": float(sum(_f(x.get("volume"), 0.0) for x in rows)),
            "turnover_sum": float(sum(_f(x.get("turnover"), 0.0) for x in rows)),
            "symbol_rows": len(sub),
        }
    except Exception as e:
        logger.warning("[ENTRY FINAL LIQ PUSHDB FALLBACK] read failed symbol=%s error=%s version=%s", symbol, e, VERSION, exc_info=True)
        return {"ok_read": False, "reason": "push_db_exception", "error": str(e), "source": "push_db_recent_raw"}


def install() -> bool:
    global _INSTALLED, _ORIG_GLOBAL_CONTEXT_LIQ
    if _INSTALLED:
        return True
    try:
        from core.startup import entry_handler_strict_recent_liquidity_patch as base
        try:
            base.install()
        except Exception:
            logger.debug("[ENTRY FINAL LIQ PUSHDB FALLBACK] base install failed/ignored", exc_info=True)
        current = getattr(base, "_global_context_liquidity_values", None)
        if not callable(current):
            logger.warning("[ENTRY FINAL LIQ PUSHDB FALLBACK] target missing version=%s", VERSION)
            return False
        if getattr(current, "_pushdb_final_liq_fallback_v1", False):
            _INSTALLED = True
            return True
        _ORIG_GLOBAL_CONTEXT_LIQ = current

        def _wrapped(symbol: str, bars: int):
            orig_detail = None
            try:
                orig_detail = _ORIG_GLOBAL_CONTEXT_LIQ(symbol, bars)
                if isinstance(orig_detail, dict) and bool(orig_detail.get("ok_read")):
                    return orig_detail
            except Exception as e:
                orig_detail = {"ok_read": False, "reason": "orig_exception", "error": str(e), "source": "global_context"}
            push_detail = _push_db_liquidity_values(symbol, bars)
            if isinstance(push_detail, dict) and bool(push_detail.get("ok_read")):
                push_detail["original_global_context_detail"] = orig_detail
                logger.warning(
                    "[ENTRY FINAL LIQ PUSHDB FALLBACK] OK symbol=%s source=%s latest_dt=%s latest_volume=%s turnover_sum=%s version=%s",
                    symbol,
                    push_detail.get("source"),
                    push_detail.get("latest_dt"),
                    push_detail.get("latest_volume"),
                    push_detail.get("turnover_sum"),
                    VERSION,
                )
                return push_detail
            return {
                "ok_read": False,
                "reason": "global_context_and_push_db_ng",
                "source": "global_context_push_db_bridge",
                "global_context_detail": orig_detail,
                "push_db_detail": push_detail,
            }

        _wrapped._pushdb_final_liq_fallback_v1 = True  # type: ignore[attr-defined]
        _wrapped._original = current  # type: ignore[attr-defined]
        base._global_context_liquidity_values = _wrapped
        _INSTALLED = True
        logger.warning("[ENTRY FINAL LIQ PUSHDB FALLBACK] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[ENTRY FINAL LIQ PUSHDB FALLBACK] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY FINAL LIQ PUSHDB FALLBACK] auto install failed")


__all__ = ["VERSION", "install"]
