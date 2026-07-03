# -*- coding: utf-8 -*-
"""
Strict final recent-liquidity guard for entry_handler.

Purpose
-------
Even if RANKING / SUMMARY AI / TONOSAMA candidate filters are relaxed or a
rescue patch keeps a symbol alive, the actual order-dispatch layer must be
fail-closed for thin names.

V4:
- Prefer fresh in-memory merged PUSH summary when summaryYYYYMMDD.db returns a
  stale/latest-missing row. This fixes ORDER_BUILD_OK / ENTRY_DISPATCH being
  blocked by STRICT_FINAL_LIQ_STALE even though the current Summary-AI row and
  PUSH memory are fresh.
- The fallback is still strict: it requires positive latest volume, average
  volume, turnover, and a parseable recent datetime within max_age_sec.
- Pending-entry fallback remains as a second fallback.
- Board-missing hard block and boardless-order prohibition are unchanged.

V3:
- If summaryYYYYMMDD.db is stale, validate fresh in-memory merged summary first
  (push -> ranking -> unspecified completed fallback -> push-cache -> legacy),
  then fresh pending entries.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)
VERSION = "V4-FRESH-MERGED-PUSH-FIRST-ON-STALE-SUMMARY-DB"
_INSTALLED = False
_ORIGINAL = None


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


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


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0"):
            ss = s[:-2]
            if ss.isdigit():
                return ss
        if s.endswith(".T"):
            return s[:-2]
        return s
    except Exception:
        return ""


def _parse_dt(value: Any) -> Optional[dt.datetime]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    try:
        if hasattr(value, "to_pydatetime"):
            x = value.to_pydatetime()
            if isinstance(x, dt.datetime):
                return x.replace(tzinfo=None)
    except Exception:
        pass
    s = str(value).strip()
    if not s or s.lower() in {"nat", "none", "nan"}:
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
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _entry_timestamp(entry: dict[str, Any]) -> Optional[dt.datetime]:
    for key in ("updated_at", "created_at", "entry_created_at", "received_at", "recv_time", "timestamp", "datetime"):
        x = _parse_dt(entry.get(key))
        if x is not None:
            return x
    return None


def _row_value(row: Any, *keys: str, default: Any = None) -> Any:
    for key in keys:
        try:
            v = row.get(key, None) if hasattr(row, "get") else row[key]
            if v is not None and str(v).strip().lower() not in {"", "nan", "none", "nat"}:
                return v
        except Exception:
            pass
    return default


def _liquidity_values_from_rows(rows: list[dict[str, Any]], *, source: str) -> dict[str, Any]:
    good: list[dict[str, Any]] = []
    for e in rows:
        volume = _f(_row_value(e, "volume", "latest_volume", "display_volume", "vol"), 0.0)
        price = _f(_row_value(e, "close_price", "price", "current_price", "close", "last_price"), 0.0)
        turnover = _f(_row_value(e, "turnover", "trading_value", "display_turnover", "trading_value_yen", "sales_value"), 0.0)
        if turnover <= 0 and price > 0 and volume > 0:
            turnover = price * volume
        t = _entry_timestamp(e)
        if volume > 0 and turnover > 0:
            good.append({"volume": volume, "price": price, "turnover": turnover, "dt": t, "entry_type": e.get("entry_type"), "entry_source": e.get("source")})
    if not good:
        return {"ok_read": False, "reason": "no_positive_liquidity", "source": source, "input_rows": len(rows)}
    latest = good[-1]
    latest_dt = latest.get("dt")
    return {
        "ok_read": True,
        "rows": len(good),
        "source": source,
        "input_rows": len(rows),
        "latest_dt": latest_dt.isoformat(sep=" ") if isinstance(latest_dt, dt.datetime) else None,
        "latest_close": latest.get("price"),
        "latest_volume": latest.get("volume"),
        "latest_turnover": latest.get("turnover"),
        "volume_sum": float(sum(r["volume"] for r in good)),
        "turnover_sum": float(sum(r["turnover"] for r in good)),
        "entry_type": latest.get("entry_type"),
        "entry_source": latest.get("entry_source"),
    }


def _pending_entry_liquidity_values(symbol: str, bars: int) -> dict[str, Any]:
    sym = _norm_symbol(symbol)
    if not sym:
        return {"ok_read": False, "reason": "pending_symbol_missing", "source": "pending_entries"}
    try:
        from global_state import global_data
        root = getattr(global_data, "pending_entries", {}) or {}
        bucket = root.get(sym) or root.get(str(symbol)) or []
        if isinstance(bucket, dict):
            bucket = [bucket]
        entries = [e for e in list(bucket or []) if isinstance(e, dict)]
    except Exception as e:
        return {"ok_read": False, "reason": "pending_read_exception", "error": str(e), "source": "pending_entries"}
    if not entries:
        return {"ok_read": False, "reason": "pending_no_rows", "source": "pending_entries"}
    values = _liquidity_values_from_rows(entries[-max(1, int(bars)):], source="pending_entries_fresh")
    values["pending_rows"] = len(entries)
    return values


def _merged_push_liquidity_values(symbol: str, bars: int) -> dict[str, Any]:
    sym = _norm_symbol(symbol)
    if not sym:
        return {"ok_read": False, "reason": "memory_symbol_missing", "source": "merged_push"}
    try:
        import pandas as pd  # type: ignore
        from core.global_context import context as gc
    except Exception as e:
        return {"ok_read": False, "reason": "memory_import_exception", "error": str(e), "source": "merged_push"}

    last_error = None
    for src in ("push", "push-cache", None, "ranking", "legacy"):
        try:
            df = gc.get_merged_summary(tf=1, source=src) if src is not None else gc.get_merged_summary(tf=1)
            if df is None or not hasattr(df, "empty") or df.empty or "symbol" not in df.columns:
                continue
            work = df.copy()
            work["_sym_norm"] = work["symbol"].map(_norm_symbol)
            work = work[work["_sym_norm"] == sym].copy()
            if work.empty:
                continue
            time_col = None
            for c in ("datetime", "end_time", "timestamp", "updated_at", "created_at", "time"):
                if c in work.columns:
                    time_col = c
                    break
            if time_col:
                try:
                    work["_dt"] = pd.to_datetime(work[time_col], errors="coerce")
                    try:
                        work["_dt"] = work["_dt"].dt.tz_localize(None)
                    except Exception:
                        pass
                    work = work.sort_values("_dt", ascending=True, na_position="first")
                except Exception:
                    pass
            work = work.tail(max(1, int(bars)))
            rows: list[dict[str, Any]] = []
            for _, row in work.iterrows():
                d = dict(row)
                if time_col and "datetime" not in d:
                    d["datetime"] = d.get(time_col)
                rows.append(d)
            values = _liquidity_values_from_rows(rows, source=f"merged_summary_{src or 'auto'}")
            if bool(values.get("ok_read")):
                values["merged_source"] = src or "auto"
                values["memory_rows"] = int(len(work))
                return values
            last_error = values.get("reason")
        except Exception as e:
            last_error = str(e)
            continue
    return {"ok_read": False, "reason": f"memory_no_fresh_rows:{last_error or 'none'}", "source": "merged_push"}


def _strict_final_recent_liquidity_ok(symbol: str, side: str):
    import trading.handlers.entry_handler as eh

    if not _env_bool("ENTRY_HANDLER_STRICT_RECENT_LIQ_GUARD_ENABLED", True):
        if callable(_ORIGINAL):
            return _ORIGINAL(symbol, side)
        return True, "STRICT_FINAL_LIQ_DISABLED", {}

    bars = max(1, _env_int("ENTRY_HANDLER_RECENT_LIQ_BARS", 5))
    min_latest_volume = _env_float("ENTRY_HANDLER_STRICT_MIN_LATEST_VOLUME", _env_float("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", 3000.0))
    min_avg_volume = _env_float("ENTRY_HANDLER_STRICT_MIN_AVG_VOLUME", _env_float("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", 3000.0))
    min_turnover = _env_float("ENTRY_HANDLER_STRICT_MIN_TURNOVER_YEN", _env_float("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", 1000000.0))
    max_age_sec = _env_float("ENTRY_HANDLER_STRICT_MAX_AGE_SEC", 180.0)
    require_data = _env_bool("ENTRY_HANDLER_RECENT_LIQ_REQUIRE_DATA", True)

    try:
        v = eh._recent_liquidity_values(symbol, bars)
    except Exception as e:
        detail = {"symbol": symbol, "side": side, "bars": bars, "reason": "exception", "error": str(e)}
        return (False, "STRICT_FINAL_LIQ_READ_EXCEPTION", detail) if require_data else (True, "STRICT_FINAL_LIQ_READ_EXCEPTION_FAIL_OPEN", detail)

    def _detail_from(values: Any, *, fallback: bool = False, original_detail: dict[str, Any] | None = None) -> dict[str, Any]:
        rows = int(values.get("rows") or 0) if isinstance(values, dict) else 0
        volume_sum = _f(values.get("volume_sum") if isinstance(values, dict) else 0.0, 0.0)
        avg_volume = volume_sum / max(1, rows)
        latest_volume = _f(values.get("latest_volume") if isinstance(values, dict) else 0.0, 0.0)
        turnover_sum = _f(values.get("turnover_sum") if isinstance(values, dict) else 0.0, 0.0)
        latest_dt = _parse_dt(values.get("latest_dt") if isinstance(values, dict) else None)
        age_sec = (dt.datetime.now() - latest_dt).total_seconds() if latest_dt is not None else None
        out = {
            "symbol": symbol,
            "side": side,
            "bars": bars,
            "rows": rows,
            "latest_volume": latest_volume,
            "avg_volume": avg_volume,
            "volume_sum": volume_sum,
            "turnover_sum": turnover_sum,
            "latest_dt": values.get("latest_dt") if isinstance(values, dict) else None,
            "age_sec": age_sec,
            "min_latest_volume": min_latest_volume,
            "min_avg_volume": min_avg_volume,
            "min_turnover": min_turnover,
            "max_age_sec": max_age_sec,
            "fallback": fallback,
            **(values if isinstance(values, dict) else {}),
        }
        if original_detail is not None:
            out["original_detail"] = original_detail
        return out

    def _passes_strict_liq(d: dict[str, Any]) -> tuple[bool, str]:
        rows = int(d.get("rows") or 0)
        age = d.get("age_sec")
        latest_volume = _f(d.get("latest_volume"), 0.0)
        avg_volume = _f(d.get("avg_volume"), 0.0)
        turnover = _f(d.get("turnover_sum"), 0.0)
        if rows <= 0:
            return False, "NO_ROWS"
        if age is None:
            return False, "DATETIME_PARSE_NG"
        if float(age) > max_age_sec:
            return False, f"STALE:{float(age):.0f}>{max_age_sec:.0f}"
        if latest_volume < min_latest_volume:
            return False, f"LATEST_VOLUME_LOW:{latest_volume:.0f}<{min_latest_volume:.0f}"
        if avg_volume < min_avg_volume:
            return False, f"AVG_VOLUME_LOW:{avg_volume:.0f}<{min_avg_volume:.0f}"
        if turnover < min_turnover:
            return False, f"TURNOVER_LOW:{turnover:.0f}<{min_turnover:.0f}"
        return True, "OK"

    def _try_fresh_fallback(original_detail: dict[str, Any], *, reason_prefix: str):
        memory_v = _merged_push_liquidity_values(symbol, bars)
        memory_detail = _detail_from(memory_v, fallback=True, original_detail=original_detail)
        if isinstance(memory_v, dict) and bool(memory_v.get("ok_read")):
            ok, why = _passes_strict_liq(memory_detail)
            if ok:
                logger.warning(
                    "[ENTRY FINAL LIQ GUARD] stale/read-ng summary DB bypassed by fresh merged PUSH liquidity symbol=%s side=%s source=%s age=%.1f original_age=%s version=%s",
                    symbol, side, memory_detail.get("source"), float(memory_detail.get("age_sec") or -1), original_detail.get("age_sec"), VERSION,
                )
                return True, "STRICT_FINAL_LIQ_OK_MERGED_PUSH_FRESH", memory_detail
            memory_detail["fresh_fallback_reject"] = why

        pending_v = _pending_entry_liquidity_values(symbol, bars)
        pending_detail = _detail_from(pending_v, fallback=True, original_detail=original_detail)
        if isinstance(pending_v, dict) and bool(pending_v.get("ok_read")):
            ok, why = _passes_strict_liq(pending_detail)
            if ok:
                logger.warning(
                    "[ENTRY FINAL LIQ GUARD] stale/read-ng summary DB bypassed by fresh pending liquidity symbol=%s side=%s pending_age=%.1f original_age=%s version=%s",
                    symbol, side, float(pending_detail.get("age_sec") or -1), original_detail.get("age_sec"), VERSION,
                )
                return True, "STRICT_FINAL_LIQ_OK_PENDING_FRESH", pending_detail
            pending_detail["fresh_fallback_reject"] = why

        merged = dict(original_detail)
        merged["merged_push_fallback"] = memory_detail
        merged["pending_fallback"] = pending_detail
        return False, reason_prefix, merged

    detail = _detail_from(v)

    if not isinstance(v, dict) or not bool(v.get("ok_read")):
        ok, reason, fb_detail = _try_fresh_fallback(detail, reason_prefix=f"STRICT_FINAL_LIQ_READ_NG:{detail.get('reason')}")
        if ok:
            return ok, reason, fb_detail
        return (False, reason, fb_detail) if require_data else (True, "STRICT_FINAL_LIQ_READ_FAIL_OPEN", fb_detail)
    if int(detail.get("rows") or 0) <= 0:
        return False, "STRICT_FINAL_LIQ_NO_RECENT_ROWS", detail
    if _parse_dt(v.get("latest_dt")) is None:
        ok, reason, fb_detail = _try_fresh_fallback(detail, reason_prefix="STRICT_FINAL_LIQ_DATETIME_PARSE_NG")
        if ok:
            return ok, reason, fb_detail
        return False, reason, fb_detail

    if detail.get("age_sec") is not None and float(detail["age_sec"]) > max_age_sec:
        ok, reason, fb_detail = _try_fresh_fallback(detail, reason_prefix=f"STRICT_FINAL_LIQ_STALE:{float(detail.get('age_sec') or 0):.0f}>{max_age_sec:.0f}")
        if ok:
            return ok, reason, fb_detail
        return False, reason, fb_detail

    if _f(detail.get("latest_volume"), 0.0) < min_latest_volume:
        return False, f"STRICT_FINAL_LIQ_LATEST_VOLUME_LOW:{_f(detail.get('latest_volume'), 0.0):.0f}<{min_latest_volume:.0f}", detail
    if _f(detail.get("avg_volume"), 0.0) < min_avg_volume:
        return False, f"STRICT_FINAL_LIQ_AVG_VOLUME_LOW:{_f(detail.get('avg_volume'), 0.0):.0f}<{min_avg_volume:.0f}", detail
    if _f(detail.get("turnover_sum"), 0.0) < min_turnover:
        return False, f"STRICT_FINAL_LIQ_TURNOVER_LOW:{_f(detail.get('turnover_sum'), 0.0):.0f}<{min_turnover:.0f}", detail

    if callable(_ORIGINAL):
        ok, reason, orig_detail = _ORIGINAL(symbol, side)
        if not ok:
            merged = dict(detail)
            merged["original_detail"] = orig_detail
            return False, reason, merged

    return True, "STRICT_FINAL_LIQ_OK", detail


def install() -> bool:
    global _INSTALLED, _ORIGINAL
    if _INSTALLED:
        return True
    if os.environ.get("DISABLE_ENTRY_HANDLER_STRICT_RECENT_LIQ_PATCH", "").strip() == "1":
        logger.warning("[ENTRY HANDLER STRICT LIQ] disabled by env")
        return False
    try:
        os.environ.setdefault("ENTRY_HANDLER_RECENT_LIQ_GUARD_ENABLED", "1")
        os.environ.setdefault("ENTRY_HANDLER_RECENT_LIQ_REQUIRE_DATA", "1")
        os.environ.setdefault("ENTRY_HANDLER_RECENT_LIQ_BARS", "5")
        os.environ.setdefault("ENTRY_HANDLER_RECENT_LIQ_MIN_LATEST_VOLUME", "3000")
        os.environ.setdefault("ENTRY_HANDLER_STRICT_MIN_LATEST_VOLUME", "3000")
        os.environ.setdefault("ENTRY_HANDLER_STRICT_MIN_AVG_VOLUME", "3000")
        os.environ.setdefault("ENTRY_HANDLER_STRICT_MIN_TURNOVER_YEN", "1000000")
        os.environ.setdefault("ENTRY_HANDLER_STRICT_MAX_AGE_SEC", "180")

        import trading.handlers.entry_handler as eh

        current = getattr(eh, "_final_recent_liquidity_ok", None)
        if current is _strict_final_recent_liquidity_ok:
            _INSTALLED = True
            return True
        _ORIGINAL = getattr(current, "_original", current)
        eh._final_recent_liquidity_ok = _strict_final_recent_liquidity_ok
        _strict_final_recent_liquidity_ok._original = _ORIGINAL  # type: ignore[attr-defined]
        _INSTALLED = True
        logger.warning(
            "[ENTRY HANDLER STRICT LIQ] installed version=%s latest_vol>=%s avg_vol>=%s turnover>=%s max_age=%s merged_push_fallback=1 pending_fallback=1",
            VERSION,
            os.environ.get("ENTRY_HANDLER_STRICT_MIN_LATEST_VOLUME"),
            os.environ.get("ENTRY_HANDLER_STRICT_MIN_AVG_VOLUME"),
            os.environ.get("ENTRY_HANDLER_STRICT_MIN_TURNOVER_YEN"),
            os.environ.get("ENTRY_HANDLER_STRICT_MAX_AGE_SEC"),
        )
        return True
    except Exception:
        logger.exception("[ENTRY HANDLER STRICT LIQ] install failed")
        return False


__all__ = ["VERSION", "install"]
