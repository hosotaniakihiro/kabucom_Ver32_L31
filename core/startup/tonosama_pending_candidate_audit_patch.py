# -*- coding: utf-8 -*-
"""
Audit TONOSAMA pending candidates into audit.candidate_history.

TONOSAMA (殿様イナゴ) already builds a rich `entry_conditions` payload when it
creates pending entries.  Historically that detail lived mainly inside the
in-memory pending entry, so later review could miss it if the normal audit path
only recorded a thin order phase.

This patch wraps trading.entry.tonosama.pending_writer.add_tonosama_pending().
When a TONOSAMA pending entry is accepted, it writes candidate_history with:

- source=TONOSAMA
- reason=entry_conditions.reason
- scores and side
- technical_snapshot = full JSON payload including entry_conditions, score,
  3m/5m volume surge, price change, 5s confirmation, streaks, wick/position,
  RSI/MACD/MTF/slope, AI reason, expire_at, created_at

It is fail-safe: audit write errors never block trading.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V1-TONOSAMA-PENDING-CANDIDATE-AUDIT"
_INSTALLED = False
_ORIGINAL_ADD = None


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        x = float(v)
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _json_default(v: Any):
    try:
        import pandas as pd  # type: ignore
        if isinstance(v, pd.Timestamp):
            if pd.isna(v):
                return None
            return v.to_pydatetime().isoformat()
    except Exception:
        pass
    try:
        import numpy as np  # type: ignore
        if isinstance(v, np.generic):
            return v.item()
    except Exception:
        pass
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    return str(v)


def _snapshot_for_entry(entry: dict[str, Any]) -> str:
    cond = entry.get("entry_conditions") or {}
    if not isinstance(cond, dict):
        cond = {"entry_conditions_raw": str(cond)}

    payload: dict[str, Any] = {
        "audit_kind": "TONOSAMA_PENDING_CANDIDATE",
        "version": VERSION,
        "source": "TONOSAMA",
        "entry_type": entry.get("entry_type") or "TONOSAMA",
        "symbol": _norm_symbol(entry.get("symbol")),
        "symbolname": entry.get("symbolname"),
        "side": str(entry.get("side") or cond.get("side") or "").upper(),
        "price": _safe_float(entry.get("price"), 0.0),
        "raw_score": _safe_float(entry.get("raw_score"), 0.0),
        "final_score": _safe_float(entry.get("final_score") or entry.get("score"), 0.0),
        "display_score": _safe_float(entry.get("display_score") or entry.get("final_score"), 0.0),
        "score": _safe_float(entry.get("score"), 0.0),
        "score_buy": _safe_float(entry.get("score_buy"), 0.0),
        "score_sell": _safe_float(entry.get("score_sell"), 0.0),
        "ai_prob": _safe_float(entry.get("ai_prob"), 0.0),
        "ai_reason": cond.get("ai_reason"),
        "reason": cond.get("reason"),
        "reason_code": cond.get("reason_code"),
        "created_at": entry.get("created_at"),
        "expire_at": entry.get("expire_at") or cond.get("expire_at"),
        "entry_conditions": cond,
        # Flat aliases for easy SQL/JSON inspection.
        "feature_dt": cond.get("feature_dt"),
        "five_sec_dt": cond.get("five_sec_dt"),
        "five_sec_dt_valid": cond.get("five_sec_dt_valid"),
        "surge_tf": cond.get("surge_tf"),
        "volume_surge_ratio_3m": cond.get("volume_surge_ratio_3m"),
        "volume_surge_ratio_5m": cond.get("volume_surge_ratio_5m"),
        "max_volume_surge_ratio": cond.get("max_volume_surge_ratio"),
        "price_change_pct_3m": cond.get("price_change_pct_3m"),
        "price_change_pct_5m": cond.get("price_change_pct_5m"),
        "max_price_change_pct": cond.get("max_price_change_pct"),
        "price_change_5s_pct": cond.get("price_change_5s_pct"),
        "latest_volume": cond.get("latest_volume"),
        "volume_3m": cond.get("volume_3m"),
        "volume_5m": cond.get("volume_5m"),
        "has_5sec_bar": cond.get("has_5sec_bar"),
        "latest_5sec_volume": cond.get("latest_5sec_volume"),
        "volume_surge_ratio_5s": cond.get("volume_surge_ratio_5s"),
        "is_5sec_confirm_ok": cond.get("is_5sec_confirm_ok"),
        "prev_3m_up_streak": cond.get("prev_3m_up_streak"),
        "prev_5m_up_streak": cond.get("prev_5m_up_streak"),
        "prev_3m_down_streak": cond.get("prev_3m_down_streak"),
        "prev_5m_down_streak": cond.get("prev_5m_down_streak"),
        "close_position_pct": cond.get("close_position_pct"),
        "upper_wick_pct": cond.get("upper_wick_pct"),
        "lower_wick_pct": cond.get("lower_wick_pct"),
        "slope": cond.get("slope"),
        "rsi": cond.get("rsi"),
        "macd": cond.get("macd"),
        "signal": cond.get("signal"),
        "mtf": cond.get("mtf"),
        "score_mtf": cond.get("score_mtf"),
    }
    text = json.dumps(payload, ensure_ascii=False, default=_json_default, separators=(",", ":"))
    max_len = int(float(os.environ.get("TONOSAMA_AUDIT_TECHNICAL_SNAPSHOT_MAX_CHARS", "20000")))
    if max_len > 0 and len(text) > max_len:
        text = text[:max_len] + "...TRUNCATED"
    return text


def _record_tonosama_candidate(entry: dict[str, Any]) -> None:
    try:
        if not isinstance(entry, dict):
            return
        source = str(entry.get("source") or "").strip().upper()
        entry_type = str(entry.get("entry_type") or "").strip().upper()
        if source != "TONOSAMA" and entry_type != "TONOSAMA":
            return
        cond = entry.get("entry_conditions") or {}
        if not isinstance(cond, dict):
            cond = {}
        from trading.audit_logging.recorder import record_candidate_event

        side = str(entry.get("side") or cond.get("side") or "").upper()
        final_score = _safe_float(entry.get("final_score") or entry.get("display_score") or entry.get("score"), 0.0)
        reason = str(cond.get("reason") or cond.get("ai_reason") or "TONOSAMA pending accepted")
        ai_reason = cond.get("ai_reason")
        ai_result = json.dumps(
            {
                "kind": "TONOSAMA",
                "ai_prob": _safe_float(entry.get("ai_prob"), 0.0),
                "ai_reason": ai_reason,
                "reason_code": cond.get("reason_code"),
            },
            ensure_ascii=False,
            default=_json_default,
            separators=(",", ":"),
        )
        record_candidate_event(
            datetime=dt.datetime.now().isoformat(timespec="seconds"),
            symbol=_norm_symbol(entry.get("symbol")),
            side=side,
            source="TONOSAMA",
            interval_min=0,
            score_buy=_safe_float(entry.get("score_buy"), 0.0),
            score_sell=_safe_float(entry.get("score_sell"), 0.0),
            score_total=final_score,
            final_score=final_score,
            ai_result=ai_result,
            reason=reason,
            technical_snapshot=_snapshot_for_entry(entry),
        )
        logger.info(
            "[TONOSAMA AUDIT] candidate_history recorded symbol=%s side=%s score=%s reason_code=%s",
            _norm_symbol(entry.get("symbol")), side, final_score, cond.get("reason_code"),
        )
    except Exception:
        logger.debug("[TONOSAMA AUDIT] record candidate failed", exc_info=True)


def install() -> bool:
    global _INSTALLED, _ORIGINAL_ADD
    if _INSTALLED:
        return True
    if os.environ.get("DISABLE_TONOSAMA_PENDING_CANDIDATE_AUDIT_PATCH", "").strip() == "1":
        logger.warning("[TONOSAMA AUDIT] pending candidate audit patch disabled by env")
        return False
    try:
        import trading.entry.tonosama.pending_writer as pw

        current = getattr(pw, "add_tonosama_pending", None)
        if current is None:
            return False
        if getattr(current, "_tonosama_audit_wrapped", False):
            _INSTALLED = True
            return True
        _ORIGINAL_ADD = current

        def _wrapped_add_tonosama_pending(entry: dict[str, Any]) -> bool:
            ok = False
            try:
                ok = bool(_ORIGINAL_ADD(entry))
                return ok
            finally:
                # Record only accepted pending entries, not rejected final-guard rows.
                if ok and _env_bool("TONOSAMA_AUDIT_RECORD_PENDING_ACCEPTED", True):
                    _record_tonosama_candidate(entry)

        setattr(_wrapped_add_tonosama_pending, "_tonosama_audit_wrapped", True)
        pw.add_tonosama_pending = _wrapped_add_tonosama_pending
        _INSTALLED = True
        logger.warning("[TONOSAMA AUDIT] pending candidate audit patch installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[TONOSAMA AUDIT] pending candidate audit patch install failed")
        return False


__all__ = ["VERSION", "install"]
