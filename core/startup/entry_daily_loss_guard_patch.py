from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_RUN_ENTRY_PIPELINE = None
_ORIG_EXECUTE_BEST_CANDIDATE = None
_ORIG_PLACE_BUY_EC = None
_ORIG_PLACE_SELL_EC = None
_ORIG_PLACE_BUY_EH = None
_ORIG_PLACE_SELL_EH = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _today_range() -> tuple[dt.datetime, dt.datetime]:
    now = dt.datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + dt.timedelta(days=1)


def _dt_from_row(row: Any) -> dt.datetime | None:
    for name in ("trade_time", "exit_time", "created_at", "timestamp", "datetime", "time"):
        try:
            v = getattr(row, name, None)
            if isinstance(v, dt.datetime):
                return v
            if isinstance(v, str) and v.strip():
                try:
                    return dt.datetime.fromisoformat(v.strip())
                except Exception:
                    pass
        except Exception:
            pass
    return None


def _pnl_from_row(row: Any) -> float | None:
    for name in ("realized_pnl", "pnl", "profit", "profit_loss"):
        try:
            if hasattr(row, name):
                return _safe_float(getattr(row, name), 0.0)
        except Exception:
            pass
    return None


def daily_stats() -> dict[str, Any]:
    result = {"ok": False, "realized_pnl": 0.0, "exit_count": 0, "consecutive_losses": 0, "source": "none"}
    start, end = _today_range()
    rows: list[Any] = []

    try:
        from database import Session_position
        from database.models import TradeHistory
        sp = Session_position()
        try:
            rows = list(sp.query(TradeHistory).all())
            result["source"] = "TradeHistory"
        finally:
            sp.close()
    except Exception:
        logger.warning("[ENTRY DAILY GUARD] TradeHistory read failed", exc_info=True)
        rows = []

    pnl_items: list[tuple[dt.datetime, float]] = []
    for row in rows:
        t = _dt_from_row(row)
        if t is None or not (start <= t < end):
            continue
        pnl = _pnl_from_row(row)
        if pnl is None:
            continue
        action = str(getattr(row, "action", "") or "").upper()
        # actionが空の環境もあるため、pnl列があれば採用。ENTRYはpnl=0が多いので影響は小さい。
        if action and action not in {"EXIT", "CLOSE", "CLOSED", "返済"} and pnl == 0:
            continue
        pnl_items.append((t, pnl))

    pnl_items.sort(key=lambda x: x[0])
    consecutive = 0
    for _, pnl in reversed(pnl_items):
        if pnl < 0:
            consecutive += 1
        elif pnl > 0:
            break

    result.update(
        ok=True,
        realized_pnl=float(sum(p for _, p in pnl_items)),
        exit_count=len(pnl_items),
        consecutive_losses=consecutive,
    )
    return result


def should_block_entry() -> tuple[bool, str, dict[str, Any]]:
    if not _env_bool("ENTRY_DAILY_RISK_GUARD_ENABLED", True):
        return False, "DISABLED", {}
    if _env_bool("ENTRY_FORCE_DISABLE", False):
        return True, "ENTRY_FORCE_DISABLE", {}

    stats = daily_stats()
    max_losses = _env_int("ENTRY_DAILY_MAX_CONSECUTIVE_LOSSES", 3)
    max_loss = abs(_env_float("ENTRY_DAILY_MAX_REALIZED_LOSS_YEN", 3000.0))

    if int(stats.get("consecutive_losses", 0)) >= max_losses:
        return True, "DAILY_CONSECUTIVE_LOSS_STOP", stats
    if float(stats.get("realized_pnl", 0.0)) <= -max_loss:
        return True, "DAILY_REALIZED_LOSS_STOP", stats
    return False, "OK", stats


def _log_block(reason: str, stats: dict[str, Any], where: str) -> None:
    try:
        from global_state import global_data
        setattr(global_data, "entry_disabled_reason", reason)
        setattr(global_data, "entry_daily_guard_stats", stats)
    except Exception:
        pass
    logger.error("[ENTRY DAILY GUARD] BLOCK_NEW_ENTRY where=%s reason=%s stats=%s", where, reason, stats)


def _guard_or_none(where: str) -> bool:
    block, reason, stats = should_block_entry()
    if block:
        _log_block(reason, stats, where)
        return True
    logger.info("[ENTRY DAILY GUARD] pass where=%s stats=%s", where, stats)
    return False


def _patched_run_entry_pipeline(*args, **kwargs):
    if _guard_or_none("run_entry_pipeline"):
        return None
    return _ORIG_RUN_ENTRY_PIPELINE(*args, **kwargs)  # type: ignore[misc]


def _patched_execute_best_candidate(*args, **kwargs):
    if _guard_or_none("execute_best_candidate"):
        return False
    return _ORIG_EXECUTE_BEST_CANDIDATE(*args, **kwargs)  # type: ignore[misc]


def _patched_place_buy(*args, **kwargs):
    if _guard_or_none("place_entry_buy"):
        return None
    return _ORIG_PLACE_BUY_EC(*args, **kwargs)  # type: ignore[misc]


def _patched_place_sell(*args, **kwargs):
    if _guard_or_none("place_entry_sell"):
        return None
    return _ORIG_PLACE_SELL_EC(*args, **kwargs)  # type: ignore[misc]


def _install_into_entry_controller() -> bool:
    global _ORIG_RUN_ENTRY_PIPELINE, _ORIG_EXECUTE_BEST_CANDIDATE, _ORIG_PLACE_BUY_EC, _ORIG_PLACE_SELL_EC
    import trading.handlers.entry_controller as ec

    ok = True
    cur = getattr(ec, "run_entry_pipeline", None)
    if callable(cur) and not getattr(cur, "_daily_guard_v2", False):
        _ORIG_RUN_ENTRY_PIPELINE = getattr(cur, "_original", cur)
        _patched_run_entry_pipeline._daily_guard_v2 = True  # type: ignore[attr-defined]
        _patched_run_entry_pipeline._original = _ORIG_RUN_ENTRY_PIPELINE  # type: ignore[attr-defined]
        ec.run_entry_pipeline = _patched_run_entry_pipeline
    else:
        ok = ok and callable(cur)

    cur = getattr(ec, "_execute_best_candidate", None)
    if callable(cur) and not getattr(cur, "_daily_guard_v2", False):
        _ORIG_EXECUTE_BEST_CANDIDATE = getattr(cur, "_original", cur)
        _patched_execute_best_candidate._daily_guard_v2 = True  # type: ignore[attr-defined]
        _patched_execute_best_candidate._original = _ORIG_EXECUTE_BEST_CANDIDATE  # type: ignore[attr-defined]
        ec._execute_best_candidate = _patched_execute_best_candidate
    else:
        ok = ok and callable(cur)

    cur = getattr(ec, "place_entry_buy", None)
    if callable(cur) and not getattr(cur, "_daily_guard_v2", False):
        _ORIG_PLACE_BUY_EC = getattr(cur, "_original", cur)
        _patched_place_buy._daily_guard_v2 = True  # type: ignore[attr-defined]
        _patched_place_buy._original = _ORIG_PLACE_BUY_EC  # type: ignore[attr-defined]
        ec.place_entry_buy = _patched_place_buy
    else:
        ok = ok and callable(cur)

    cur = getattr(ec, "place_entry_sell", None)
    if callable(cur) and not getattr(cur, "_daily_guard_v2", False):
        _ORIG_PLACE_SELL_EC = getattr(cur, "_original", cur)
        _patched_place_sell._daily_guard_v2 = True  # type: ignore[attr-defined]
        _patched_place_sell._original = _ORIG_PLACE_SELL_EC  # type: ignore[attr-defined]
        ec.place_entry_sell = _patched_place_sell
    else:
        ok = ok and callable(cur)

    return ok


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("ENTRY_DAILY_RISK_GUARD_ENABLED", "1")
        os.environ.setdefault("ENTRY_DAILY_MAX_CONSECUTIVE_LOSSES", "3")
        os.environ.setdefault("ENTRY_DAILY_MAX_REALIZED_LOSS_YEN", "3000")
        os.environ.setdefault("ENTRY_FORCE_DISABLE", "0")

        ok = _install_into_entry_controller()
        _INSTALLED = True
        logger.warning(
            "[ENTRY DAILY GUARD] installed v2 ok=%s max_consecutive_losses=%s max_realized_loss_yen=%s force_disable=%s",
            ok,
            os.environ.get("ENTRY_DAILY_MAX_CONSECUTIVE_LOSSES"),
            os.environ.get("ENTRY_DAILY_MAX_REALIZED_LOSS_YEN"),
            os.environ.get("ENTRY_FORCE_DISABLE"),
        )
        return True
    except Exception:
        logger.exception("[ENTRY DAILY GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY DAILY GUARD] auto install failed")

__all__ = ["install", "should_block_entry", "daily_stats"]
