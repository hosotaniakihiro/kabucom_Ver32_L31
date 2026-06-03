from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_LOG_SKIP = None
_ORIG_RANGE_5M_FILTER = None
_ORIG_RANKING_ENTRY = None
_ORIG_RANKING_PIPELINE = None


HARD_PRUNE_REASONS = {
    "RANGE_5M_FILTER_NG",
    "ATR_1M_FILTER_NG",
    "ATR_FILTER_NG",
    "ENTRY_ROW_RANGE_NG",
    "DIRECTION_FILTER_NG",
    "FINAL_ENTRY_SAFETY_NG",
    "BOARD_GUARD_NG",
    "FRESH_QUOTE_NG",
    "SELL_CREDIT_GUARD_NG",
    "POSITION_FILTER_NG",
}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "disable", "disabled", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _norm(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _is_ranking_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    src = _norm(entry.get("source"))
    et = _norm(entry.get("entry_type"))
    mode = _norm(entry.get("ranking_entry_mode"))
    return src == "RANKING" or et == "RANKING" or mode.startswith("RANKING")


def _entry_side(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    side = _norm(entry.get("side") or entry.get("entry_decision") or entry.get("ai_side"))
    if side in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if side in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return side


def _entry_created_at(entry: Any) -> dt.datetime | None:
    if not isinstance(entry, dict):
        return None
    v = entry.get("created_at") or entry.get("created") or entry.get("timestamp")
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, str) and v.strip():
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return dt.datetime.strptime(v.strip(), fmt)
            except Exception:
                pass
        try:
            return dt.datetime.fromisoformat(v.strip())
        except Exception:
            return None
    return None


def _prune_ranking_pending(symbol: Any, reason: str, *, side: Any = None, max_remove: int | None = None) -> int:
    if not _env_bool("RANKING_PENDING_PRUNE_ON_FILTER_NG", True):
        return 0
    sym = _norm_symbol(symbol)
    side_n = _norm(side)
    try:
        from trading.entry.pending_manager import prune_entries, snapshot_root

        def _pred(s: str, entry: dict[str, Any]) -> bool:
            if sym and _norm_symbol(s) != sym:
                return False
            if not _is_ranking_entry(entry):
                return False
            if side_n and _entry_side(entry) != side_n:
                return False
            return True

        removed = prune_entries(
            _pred,
            reason=f"RANKING_FINAL_NG:{reason}",
            max_remove=max_remove,
        )
        if removed:
            logger.warning(
                "[RANKING PENDING CLEANUP] removed=%s symbol=%s side=%s reason=%s root=%s",
                removed,
                sym,
                side_n,
                reason,
                snapshot_root(),
            )
        return int(removed or 0)
    except Exception:
        logger.exception("[RANKING PENDING CLEANUP] failed symbol=%s side=%s reason=%s", sym, side_n, reason)
        return 0


def _cleanup_stale_ranking_pending(reason: str = "TTL") -> int:
    ttl = _env_float("RANKING_PENDING_TTL_SEC", 20.0)
    if ttl <= 0:
        return 0
    now = dt.datetime.now()
    try:
        from trading.entry.pending_manager import prune_entries, snapshot_root

        def _pred(_symbol: str, entry: dict[str, Any]) -> bool:
            if not _is_ranking_entry(entry):
                return False
            created = _entry_created_at(entry)
            if created is None:
                return False
            return (now - created).total_seconds() >= ttl

        removed = prune_entries(_pred, reason=f"RANKING_STALE:{reason}")
        if removed:
            logger.warning(
                "[RANKING PENDING STALE CLEANUP] removed=%s ttl=%.1fs reason=%s root=%s",
                removed,
                ttl,
                reason,
                snapshot_root(),
            )
        return int(removed or 0)
    except Exception:
        logger.exception("[RANKING PENDING STALE CLEANUP] failed reason=%s", reason)
        return 0


def _patched_log_skip(symbol: Any, skip_reason: Any = None, *args, **kwargs):
    # 既存対策: detail kwargs の reason と positional reason の衝突を避ける。
    if "reason" in kwargs:
        kwargs.setdefault("detail_reason", kwargs.pop("reason"))

    result = _ORIG_LOG_SKIP(symbol, skip_reason, *args, **kwargs)  # type: ignore[misc]

    try:
        reason = str(skip_reason or "").split()[0].strip()
        if reason in HARD_PRUNE_REASONS:
            # 最終評価で落ちたランキング候補は、再評価しても同じ理由で詰まりやすい。
            # max_pending=1 のため即削除して次のランキング候補へ回す。
            _prune_ranking_pending(symbol, reason, side=kwargs.get("side"))
    except Exception:
        logger.exception("[RANKING PENDING CLEANUP] _log_skip hook failed symbol=%s reason=%s", symbol, skip_reason)

    return result


def _patched_range_5m_filter(entry_row: Any = None, *args, **kwargs):
    # entry_controller からの range_5m_filter(entry_row) のみランキング専用閾値に緩和。
    try:
        if isinstance(entry_row, dict) and _is_ranking_entry(entry_row) and "min_pct" not in kwargs:
            kwargs["min_pct"] = _env_float("RANKING_ENTRY_RANGE_5M_MIN_PCT", 0.006)
    except Exception:
        logger.exception("[RANKING RANGE 5M PATCH] min_pct resolve failed")
    return _ORIG_RANGE_5M_FILTER(entry_row, *args, **kwargs)  # type: ignore[misc]


def _patched_ranking_entry(*args, **kwargs):
    _cleanup_stale_ranking_pending("before_ranking_entry")
    return _ORIG_RANKING_ENTRY(*args, **kwargs)  # type: ignore[misc]


def _patched_ranking_pipeline(*args, **kwargs):
    _cleanup_stale_ranking_pending("before_ranking_pipeline")
    return _ORIG_RANKING_PIPELINE(*args, **kwargs)  # type: ignore[misc]


def install() -> bool:
    global _INSTALLED, _ORIG_LOG_SKIP, _ORIG_RANGE_5M_FILTER, _ORIG_RANKING_ENTRY, _ORIG_RANKING_PIPELINE
    if _INSTALLED:
        return True
    try:
        os.environ.setdefault("RANKING_ENTRY_RANGE_5M_MIN_PCT", "0.006")
        os.environ.setdefault("RANKING_PENDING_PRUNE_ON_FILTER_NG", "1")
        os.environ.setdefault("RANKING_PENDING_TTL_SEC", "20")

        import trading.handlers.entry_controller as ec

        cur_log = getattr(ec, "_log_skip", None)
        if not callable(cur_log):
            logger.warning("[ENTRY LOG SKIP GUARD] _log_skip unavailable")
            return False
        if not getattr(cur_log, "_entry_log_skip_guard_v2", False):
            _ORIG_LOG_SKIP = cur_log
            _patched_log_skip._entry_log_skip_guard_v2 = True  # type: ignore[attr-defined]
            _patched_log_skip._original = cur_log  # type: ignore[attr-defined]
            ec._log_skip = _patched_log_skip

        cur_range = getattr(ec, "range_5m_filter", None)
        if callable(cur_range) and not getattr(cur_range, "_ranking_range_5m_min_pct_v1", False):
            _ORIG_RANGE_5M_FILTER = cur_range
            _patched_range_5m_filter._ranking_range_5m_min_pct_v1 = True  # type: ignore[attr-defined]
            _patched_range_5m_filter._original = cur_range  # type: ignore[attr-defined]
            ec.range_5m_filter = _patched_range_5m_filter

        try:
            import trading.ranking.entry_from_ranking as efr
            cur_entry = getattr(efr, "entry_from_ranking", None)
            if callable(cur_entry) and not getattr(cur_entry, "_ranking_stale_cleanup_v1", False):
                _ORIG_RANKING_ENTRY = cur_entry
                _patched_ranking_entry._ranking_stale_cleanup_v1 = True  # type: ignore[attr-defined]
                _patched_ranking_entry._original = cur_entry  # type: ignore[attr-defined]
                efr.entry_from_ranking = _patched_ranking_entry

            cur_pipeline = getattr(efr, "run_ranking_entry_pipeline", None)
            if callable(cur_pipeline) and not getattr(cur_pipeline, "_ranking_stale_cleanup_v1", False):
                _ORIG_RANKING_PIPELINE = cur_pipeline
                _patched_ranking_pipeline._ranking_stale_cleanup_v1 = True  # type: ignore[attr-defined]
                _patched_ranking_pipeline._original = cur_pipeline  # type: ignore[attr-defined]
                efr.run_ranking_entry_pipeline = _patched_ranking_pipeline
        except Exception:
            logger.exception("[RANKING PENDING STALE CLEANUP] ranking entry hook install failed")

        _INSTALLED = True
        logger.warning(
            "[ENTRY LOG SKIP GUARD] installed v2 ranking_pending_cleanup=True ranking_range_5m_min_pct=%s ttl=%ss",
            os.getenv("RANKING_ENTRY_RANGE_5M_MIN_PCT"),
            os.getenv("RANKING_PENDING_TTL_SEC"),
        )
        return True
    except Exception:
        logger.exception("[ENTRY LOG SKIP GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[ENTRY LOG SKIP GUARD] auto install failed")

__all__ = ["install"]
