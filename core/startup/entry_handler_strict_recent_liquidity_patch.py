# -*- coding: utf-8 -*-
"""
Strict final recent-liquidity guard for entry_handler.

Purpose
-------
Even if RANKING / SUMMARY AI / TONOSAMA candidate filters are relaxed or a
rescue patch keeps a symbol alive, the actual order-dispatch layer must be
fail-closed for thin names.

V3:
- If summaryYYYYMMDD.db is stale, validate fresh in-memory merged summary first
  (push -> ranking -> unspecified completed fallback -> push-cache -> legacy),
  then fresh pending entries.
- This fixes the case where ORDER_BUILD_OK/ENTRY_DISPATCH are reached, but final
  send is blocked because entry_handler reads an old summary DB row while
  main.py already has fresh PUSH/ranking summary in global_context.
- Missing/low/stale liquidity still blocks. This is not a fail-open.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)
VERSION = "V3-GLOBAL-CONTEXT-FRESH-FALLBACK-ON-STALE-SUMMARY-DB"
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
    # created_at/updated_at represents when this candidate was built from the fresh
    # in-memory/PUSH path. Prefer it over the bar datetime if the summary DB is stale.
    for key in ("updated_at", "created_at", "entry_created_at", "received_at", "recv_time", "timestamp", "datetime"):
        x = _parse_dt(entry.get(key))
        if x is not None:
            return x
    return None


def _safe_col(row: Any, names: tuple[str, ...], default: Any = None) -> Any:
    try:
        for name in names:
            if isinstance(row, dict):
                v = row.get(name)
            else:
                v = row.get(name) if hasattr(row, "get") else None
            if v is not None and str(v).strip() != "":
                return v
    except Exception:
        pass
    return default


def _one_row_liquidity_values(row: Any, *, source: str) -> dict[str, Any]:
    volume = _f(_safe_col(row, ("volume", "latest_volume", "display_volume", "vol"), 0.0), 0.0)
    price = _f(_safe_col(row, ("close_price", "price", "current_price", "close", "last_price"), 0.0), 0.0)
    turnover = _f(_safe_col(row, ("turnover", "trading_value", "display_turnover", "sales_value"), 0.0), 0.0)
    if turnover <= 0 and price > 0 and volume > 0:
        turnover = price * volume
    latest_dt = _parse_dt(_safe_col(row, ("updated_at", "created_at", "entry_created_at", "received_at", "recv_time", "timestamp", "datetime", "end_time", "time"), None))
    if volume <= 0 or turnover <= 0:
        return {"ok_read": False, "reason": "row_no_positive_liquidity", "source": source, "latest_volume": volume, "latest_turnover": turnover}
    return {
        "ok_read": True,
        "rows": 1,
        "source": source,
        "latest_dt": latest_dt.isoformat(sep=" ") if isinstance(latest_dt, dt.datetime) else None,
        "latest_close": price,
        "latest_volume": volume,
        "latest_turnover": turnover,
        "volume_sum": float(volume),
        "turnover_sum": float(turnover),
    }


def _global_context_liquidity_values(symbol: str, bars: int) -> dict[str, Any]:
    """Read latest liquidity from main.py in-memory summaries.

    This is used only when entry_handler's DB-backed summary row is stale. It is
    fail-closed: it returns ok_read=True only for a matching symbol with positive
    volume and turnover, and the caller still applies age/volume/turnover checks.
    """
    sym = _norm_symbol(symbol)
    if not sym:
        return {"ok_read": False, "reason": "gc_symbol_missing", "source": "global_context"}
    try:
        from core.global_context import context as gc
    except Exception as e:
        return {"ok_read": False, "reason": "gc_import_exception", "error": str(e), "source": "global_context"}

    errors: list[str] = []
    # Prefer in-memory PUSH, then ranking, then GlobalContext's completed fallback.
    candidates = [
        ("global_context_merged_push", lambda: gc.get_merged_summary(tf=1, source="push")),
        ("global_context_merged_ranking", lambda: gc.get_merged_summary(tf=1, source="ranking")),
        ("global_context_merged_fallback", lambda: gc.get_merged_summary(tf=1, source=None)),
        ("global_context_merged_push_cache", lambda: gc.get_merged_summary(tf=1, source="push-cache")),
        ("global_context_merged_legacy", lambda: gc.get_merged_summary(tf=1, source="legacy")),
        ("global_context_push_df", lambda: gc.get_push_df()),
    ]
    for label, loader in candidates:
        try:
            df = loader()
            if df is None or not hasattr(df, "empty") or df.empty or "symbol" not in df.columns:
                continue
            work = df.copy()
            try:
                ss = work["symbol"].map(_norm_symbol)
            except Exception:
                ss = work["symbol"].astype(str).str.strip()
            rows = work.loc[ss == sym].copy()
            if rows.empty:
                continue
            time_col = None
            for c in ("updated_at", "created_at", "datetime", "end_time", "time"):
                if c in rows.columns:
                    time_col = c
                    break
            if time_col is not None:
                try:
                    rows["__liq_dt"] = rows[time_col].map(_parse_dt)
                    rows = rows.sort_values("__liq_dt", ascending=False, na_position="last")
                except Exception:
                    pass
            # Use up to bars latest rows when available, but most merged summaries
            # are already one latest row per symbol.
            picked = rows.head(max(1, int(bars)))
            values = []
            for _, r in picked.iterrows():
                v = _one_row_liquidity_values(r, source=label)
                if v.get("ok_read"):
                    values.append(v)
            if not values:
                continue
            latest = values[0]
            volume_sum = float(sum(_f(v.get("latest_volume"), 0.0) for v in values))
            turnover_sum = float(sum(_f(v.get("latest_turnover"), 0.0) for v in values))
            return {
                "ok_read": True,
                "rows": len(values),
                "source": label,
                "latest_dt": latest.get("latest_dt"),
                "latest_close": latest.get("latest_close"),
                "latest_volume": latest.get("latest_volume"),
                "latest_turnover": latest.get("latest_turnover"),
                "volume_sum": volume_sum,
                "turnover_sum": turnover_sum,
            }
        except Exception as e:
            errors.append(f"{label}:{e}")
    return {"ok_read": False, "reason": "gc_no_matching_fresh_rows", "source": "global_context", "errors": errors[:5]}


def _pending_entry_liquidity_values(symbol: str, bars: int) -> dict[str, Any]:
    """Validate liquidity from fresh pending rows when summary DB is stale."""
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

    rows = []
    for e in entries[-max(1, int(bars)):]:
        volume = _f(e.get("volume") or e.get("latest_volume") or e.get("display_volume") or e.get("vol"), 0.0)
        price = _f(e.get("close_price") or e.get("price") or e.get("current_price") or e.get("close"), 0.0)
        turnover = _f(e.get("turnover") or e.get("trading_value") or e.get("display_turnover"), 0.0)
        if turnover <= 0 and price > 0 and volume > 0:
            turnover = price * volume
        t = _entry_timestamp(e)
        if volume > 0 and turnover > 0:
            rows.append({"volume": volume, "price": price, "turnover": turnover, "dt": t, "entry_type": e.get("entry_type"), "source": e.get("source")})
    if not rows:
        return {"ok_read": False, "reason": "pending_no_positive_liquidity", "source": "pending_entries", "pending_rows": len(entries)}
    latest = rows[-1]
    latest_dt = latest.get("dt")
    return {
        "ok_read": True,
        "rows": len(rows),
        "source": "pending_entries_fresh",
        "pending_rows": len(entries),
        "latest_dt": latest_dt.isoformat(sep=" ") if isinstance(latest_dt, dt.datetime) else None,
        "latest_close": latest.get("price"),
        "latest_volume": latest.get("volume"),
        "latest_turnover": latest.get("turnover"),
        "volume_sum": float(sum(r["volume"] for r in rows)),
        "turnover_sum": float(sum(r["turnover"] for r in rows)),
        "entry_type": latest.get("entry_type"),
        "entry_source": latest.get("source"),
    }


def _strict_final_recent_liquidity_ok(symbol: str, side: str):
    import trading.handlers.entry_handler as eh

    if not _env_bool("ENTRY_HANDLER_STRICT_RECENT_LIQ_GUARD_ENABLED", True):
        if callable(_ORIGINAL):
            return _ORIGINAL(symbol, side)
        return True, "STRICT_FINAL_LIQ_DISABLED", {}

    bars = max(1, _env_int("ENTRY_HANDLER_RECENT_LIQ_BARS", 5))
    min_latest_volume = _env_float(
        "ENTRY_HANDLER_STRICT_MIN_LATEST_VOLUME",
        _env_float("WATCHLIST_RECENT_LIQ_MIN_LATEST_VOLUME", 3000.0),
    )
    min_avg_volume = _env_float(
        "ENTRY_HANDLER_STRICT_MIN_AVG_VOLUME",
        _env_float("WATCHLIST_RECENT_LIQ_MIN_AVG_VOLUME", 3000.0),
    )
    min_turnover = _env_float(
        "ENTRY_HANDLER_STRICT_MIN_TURNOVER_YEN",
        _env_float("WATCHLIST_RECENT_LIQ_MIN_TURNOVER_YEN", 1000000.0),
    )
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

    def _passes_liquidity(d: dict[str, Any]) -> tuple[bool, str]:
        age = d.get("age_sec")
        if age is None:
            return False, "DATETIME_PARSE_NG"
        if float(age) > max_age_sec:
            return False, f"STALE:{float(age):.0f}>{max_age_sec:.0f}"
        if _f(d.get("latest_volume"), 0.0) < min_latest_volume:
            return False, f"LATEST_VOLUME_LOW:{_f(d.get('latest_volume'), 0.0):.0f}<{min_latest_volume:.0f}"
        if _f(d.get("avg_volume"), 0.0) < min_avg_volume:
            return False, f"AVG_VOLUME_LOW:{_f(d.get('avg_volume'), 0.0):.0f}<{min_avg_volume:.0f}"
        if _f(d.get("turnover_sum"), 0.0) < min_turnover:
            return False, f"TURNOVER_LOW:{_f(d.get('turnover_sum'), 0.0):.0f}<{min_turnover:.0f}"
        return True, "OK"

    detail = _detail_from(v)

    if not isinstance(v, dict) or not bool(v.get("ok_read")):
        return (False, f"STRICT_FINAL_LIQ_READ_NG:{detail.get('reason')}", detail) if require_data else (True, "STRICT_FINAL_LIQ_READ_FAIL_OPEN", detail)
    if int(detail.get("rows") or 0) <= 0:
        return False, "STRICT_FINAL_LIQ_NO_RECENT_ROWS", detail
    if _parse_dt(v.get("latest_dt")) is None:
        return False, "STRICT_FINAL_LIQ_DATETIME_PARSE_NG", detail

    if detail.get("age_sec") is not None and float(detail["age_sec"]) > max_age_sec:
        # 1) Prefer fresh in-memory merged summary. This avoids false stale blocks
        # when the DB-backed recent-liquidity read is behind main.py memory state.
        gc_v = _global_context_liquidity_values(symbol, bars)
        gc_detail = _detail_from(gc_v, fallback=True, original_detail=detail)
        if isinstance(gc_v, dict) and bool(gc_v.get("ok_read")):
            ok_gc, reason_gc = _passes_liquidity(gc_detail)
            if ok_gc:
                logger.warning(
                    "[ENTRY FINAL LIQ GUARD] stale summary DB bypassed by fresh global_context liquidity symbol=%s side=%s gc_source=%s gc_age=%.1f summary_age=%.1f version=%s",
                    symbol,
                    side,
                    gc_detail.get("source"),
                    float(gc_detail.get("age_sec") or -1),
                    float(detail.get("age_sec") or -1),
                    VERSION,
                )
                return True, "STRICT_FINAL_LIQ_OK_GLOBAL_CONTEXT_FRESH", gc_detail
            logger.warning(
                "[ENTRY FINAL LIQ GUARD] global_context fallback rejected symbol=%s side=%s reason=%s detail=%s version=%s",
                symbol,
                side,
                reason_gc,
                {k: gc_detail.get(k) for k in ("source", "latest_dt", "age_sec", "latest_volume", "avg_volume", "turnover_sum")},
                VERSION,
            )

        # 2) Then try the fresh pending entry row itself.
        pending_v = _pending_entry_liquidity_values(symbol, bars)
        pending_detail = _detail_from(pending_v, fallback=True, original_detail=detail)
        if isinstance(pending_v, dict) and bool(pending_v.get("ok_read")):
            ok_pending, reason_pending = _passes_liquidity(pending_detail)
            if ok_pending:
                logger.warning(
                    "[ENTRY FINAL LIQ GUARD] stale summary DB bypassed by fresh pending liquidity symbol=%s side=%s pending_age=%.1f summary_age=%.1f version=%s",
                    symbol,
                    side,
                    float(pending_detail.get("age_sec") or -1),
                    float(detail.get("age_sec") or -1),
                    VERSION,
                )
                return True, "STRICT_FINAL_LIQ_OK_PENDING_FRESH", pending_detail
            logger.warning(
                "[ENTRY FINAL LIQ GUARD] pending fallback rejected symbol=%s side=%s reason=%s detail=%s version=%s",
                symbol,
                side,
                reason_pending,
                {k: pending_detail.get(k) for k in ("source", "latest_dt", "age_sec", "latest_volume", "avg_volume", "turnover_sum")},
                VERSION,
            )
        stale_detail = dict(detail)
        stale_detail["global_context_fallback"] = gc_detail
        stale_detail["pending_fallback"] = pending_detail
        return False, f"STRICT_FINAL_LIQ_STALE:{float(detail.get('age_sec') or 0):.0f}>{max_age_sec:.0f}", stale_detail

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
        _ORIGINAL = current
        eh._final_recent_liquidity_ok = _strict_final_recent_liquidity_ok
        _INSTALLED = True
        logger.warning(
            "[ENTRY HANDLER STRICT LIQ] installed version=%s latest_vol>=%s avg_vol>=%s turnover>=%s max_age=%s global_context_fallback=1 pending_fallback=1",
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
