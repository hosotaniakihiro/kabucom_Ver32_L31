# ============================================================
# File   : trading/entry/daily_risk_guard.py
# Version: Ver1.0-PRODUCTION-DAILY-LOSS-AND-REENTRY-GUARD
# ------------------------------------------------------------
# 【目的】
#   当日の負け方を見て、新規エントリーを止める安全弁。
#
# 【入れる制限】
#   1. 同一銘柄で1回負けたら、その日は再エントリー禁止
#   2. 1回または累計の銘柄損失が -1,500円以下なら即日ブラックリスト
#   3. 当日合計損益が -10,000円以下なら新規エントリー停止
#
# 【参照DB】
#   positions.db / Base_position 側の以下を読む。
#     - TradeHistory.pnl / TradeHistory.realized_pnl / TradeHistory.trade_time
#     - ExitLog.pnl / ExitLog.exit_time
#
# 【ENV】
#   DAILY_ENTRY_RISK_GUARD_ENABLED=1
#   DAILY_STOP_LOSS_YEN=-10000
#   SYMBOL_REENTRY_BLOCK_AFTER_LOSS=1
#   SYMBOL_LOSS_BLACKLIST_YEN=-1500
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DailyRiskDecision:
    allow: bool
    reason: str
    detail: Dict[str, Any]


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(v)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _normalize_symbol(v: Any) -> str:
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
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _as_datetime(v: Any) -> dt.datetime | None:
    try:
        if v is None or v == "":
            return None
        if isinstance(v, dt.datetime):
            return v
        if isinstance(v, dt.date):
            return dt.datetime.combine(v, dt.time.min)
        s = str(v).strip()
        if not s:
            return None
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                return dt.datetime.strptime(s[:19] if "%H" in fmt else s[:10], fmt)
            except Exception:
                pass
        return None
    except Exception:
        return None


def _today_bounds(now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    now = now or dt.datetime.now()
    start = dt.datetime.combine(now.date(), dt.time.min)
    end = start + dt.timedelta(days=1)
    return start, end


def _is_today(v: Any, *, start: dt.datetime, end: dt.datetime) -> bool:
    t = _as_datetime(v)
    if t is None:
        return False
    return start <= t < end


def _is_exit_like_action(v: Any) -> bool:
    s = str(v or "").strip().upper()
    if not s:
        return True
    return any(x in s for x in ("EXIT", "CLOSE", "CLOSED", "返済", "決済", "SELL", "BUY"))


def _collect_today_pnl_from_db() -> List[Dict[str, Any]]:
    """当日の実現損益行をできる限り広く集める。"""
    rows: List[Dict[str, Any]] = []
    start, end = _today_bounds()

    try:
        from database import Session_position
        from database.models import TradeHistory, ExitLog
    except Exception:
        logger.debug("[DAILY RISK GUARD] DB import failed", exc_info=True)
        return rows

    session = None
    try:
        session = Session_position()

        # TradeHistory
        try:
            q = session.query(TradeHistory).all()
            for r in q or []:
                t = getattr(r, "trade_time", None)
                if not _is_today(t, start=start, end=end):
                    continue
                action = getattr(r, "action", None)
                if not _is_exit_like_action(action):
                    continue
                pnl = _safe_float(getattr(r, "realized_pnl", None), None)  # type: ignore[arg-type]
                if pnl is None:
                    pnl = _safe_float(getattr(r, "pnl", None), 0.0)
                if pnl == 0:
                    continue
                rows.append(
                    {
                        "source": "trade_history",
                        "symbol": _normalize_symbol(getattr(r, "symbol", "")),
                        "symbolname": getattr(r, "symbolname", None),
                        "pnl": float(pnl),
                        "time": t,
                        "action": action,
                    }
                )
        except Exception:
            logger.debug("[DAILY RISK GUARD] read TradeHistory failed", exc_info=True)

        # ExitLog
        try:
            q = session.query(ExitLog).all()
            for r in q or []:
                t = getattr(r, "exit_time", None) or getattr(r, "created_at", None)
                if not _is_today(t, start=start, end=end):
                    continue
                pnl = _safe_float(getattr(r, "pnl", None), 0.0)
                if pnl == 0:
                    continue
                rows.append(
                    {
                        "source": "exit_log",
                        "symbol": _normalize_symbol(getattr(r, "symbol", "")),
                        "symbolname": None,
                        "pnl": float(pnl),
                        "time": t,
                        "action": "EXIT",
                    }
                )
        except Exception:
            logger.debug("[DAILY RISK GUARD] read ExitLog failed", exc_info=True)

    except Exception:
        logger.debug("[DAILY RISK GUARD] collect DB pnl failed", exc_info=True)

    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass

    return rows


def _dedupe_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: set[Tuple[str, str, float, str]] = set()
    out: List[Dict[str, Any]] = []
    for r in rows:
        sym = _normalize_symbol(r.get("symbol"))
        pnl = round(_safe_float(r.get("pnl"), 0.0), 4)
        t = str(r.get("time") or "")[:19]
        src = str(r.get("source") or "")
        key = (src, sym, pnl, t)
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
    return out


def get_today_risk_snapshot() -> Dict[str, Any]:
    rows = _dedupe_rows(_collect_today_pnl_from_db())
    total_pnl = sum(_safe_float(r.get("pnl"), 0.0) for r in rows)
    by_symbol: Dict[str, Dict[str, Any]] = {}

    for r in rows:
        sym = _normalize_symbol(r.get("symbol"))
        if not sym:
            continue
        d = by_symbol.setdefault(sym, {"pnl": 0.0, "loss_count": 0, "win_count": 0, "rows": []})
        pnl = _safe_float(r.get("pnl"), 0.0)
        d["pnl"] += pnl
        if pnl < 0:
            d["loss_count"] += 1
        elif pnl > 0:
            d["win_count"] += 1
        d["rows"].append(r)

    return {
        "enabled": _env_bool("DAILY_ENTRY_RISK_GUARD_ENABLED", True),
        "rows": rows,
        "row_count": len(rows),
        "total_pnl": total_pnl,
        "by_symbol": by_symbol,
        "daily_stop_loss_yen": _env_float("DAILY_STOP_LOSS_YEN", -10_000.0),
        "symbol_loss_blacklist_yen": _env_float("SYMBOL_LOSS_BLACKLIST_YEN", -1_500.0),
        "block_after_one_loss": _env_bool("SYMBOL_REENTRY_BLOCK_AFTER_LOSS", True),
        "read_at": dt.datetime.now(),
    }


def should_allow_new_entry(symbol: Any) -> DailyRiskDecision:
    s = _normalize_symbol(symbol)
    snap = get_today_risk_snapshot()

    if not snap.get("enabled"):
        return DailyRiskDecision(True, "disabled", {"symbol": s})

    total_pnl = _safe_float(snap.get("total_pnl"), 0.0)
    daily_stop = _safe_float(snap.get("daily_stop_loss_yen"), -10_000.0)
    if total_pnl <= daily_stop:
        return DailyRiskDecision(
            False,
            "daily_stop_loss_reached",
            {
                "symbol": s,
                "total_pnl": round(total_pnl, 2),
                "daily_stop_loss_yen": daily_stop,
                "row_count": snap.get("row_count"),
            },
        )

    by_symbol = snap.get("by_symbol") or {}
    sd = by_symbol.get(s) or {}
    sym_pnl = _safe_float(sd.get("pnl"), 0.0)
    loss_count = int(sd.get("loss_count") or 0)
    symbol_stop = _safe_float(snap.get("symbol_loss_blacklist_yen"), -1_500.0)

    if sym_pnl <= symbol_stop:
        return DailyRiskDecision(
            False,
            "symbol_loss_blacklist",
            {
                "symbol": s,
                "symbol_pnl": round(sym_pnl, 2),
                "symbol_loss_blacklist_yen": symbol_stop,
                "loss_count": loss_count,
            },
        )

    if bool(snap.get("block_after_one_loss")) and loss_count >= 1:
        return DailyRiskDecision(
            False,
            "symbol_reentry_block_after_loss",
            {
                "symbol": s,
                "symbol_pnl": round(sym_pnl, 2),
                "loss_count": loss_count,
            },
        )

    return DailyRiskDecision(
        True,
        "ok",
        {
            "symbol": s,
            "total_pnl": round(total_pnl, 2),
            "symbol_pnl": round(sym_pnl, 2),
            "loss_count": loss_count,
            "row_count": snap.get("row_count"),
        },
    )


def log_today_risk_snapshot(prefix: str = "[DAILY RISK GUARD]") -> None:
    try:
        snap = get_today_risk_snapshot()
        losers = {
            k: {"pnl": round(_safe_float(v.get("pnl"), 0.0), 2), "loss_count": v.get("loss_count", 0)}
            for k, v in (snap.get("by_symbol") or {}).items()
            if _safe_float(v.get("pnl"), 0.0) < 0
        }
        logger.warning(
            "%s enabled=%s rows=%s total_pnl=%.0f daily_stop=%.0f symbol_stop=%.0f block_after_one_loss=%s losers=%s",
            prefix,
            snap.get("enabled"),
            snap.get("row_count"),
            _safe_float(snap.get("total_pnl"), 0.0),
            _safe_float(snap.get("daily_stop_loss_yen"), -10_000.0),
            _safe_float(snap.get("symbol_loss_blacklist_yen"), -1_500.0),
            snap.get("block_after_one_loss"),
            losers,
        )
    except Exception:
        logger.debug("[DAILY RISK GUARD] snapshot log failed", exc_info=True)


__all__ = [
    "DailyRiskDecision",
    "get_today_risk_snapshot",
    "should_allow_new_entry",
    "log_today_risk_snapshot",
]
