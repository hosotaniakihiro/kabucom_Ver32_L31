# ============================================================
# File   : core/startup/final_entry_safety_guard_patch.py
# Version: Ver11-BOARD-GUARD-SIGNATURE-TOLERANT
# ------------------------------------------------------------
# 発注直前の安全ガード。
# Ver11:
#   - _board_guard が他パッチにより 3引数版へ差し替わっても落とさない
#   - _call_board_guard で 4引数/3引数/2引数/1引数を順に試す
#   - TypeError を board_missing 扱いにしない
#   - board_missing は pending を残して次サイクル再試行
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_EXECUTE_BEST_CANDIDATE: Callable[..., Any] | None = None
_BOARD_COOLDOWN_UNTIL = 0.0
_BOARD_CACHE: dict[str, tuple[float, tuple[float, float, float, float]]] = {}
_TRUE_SET = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE_SET = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}
_ORIGINAL_ATTRS = (
    "_final_entry_safety_guard_original",
    "_original_execute_best_candidate",
    "_entry_execute_timeout_guard_original",
    "_original",
)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE_SET:
            return True
        if s in _FALSE_SET:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return float(default)
        return float(str(raw).replace(",", ""))
    except Exception:
        return float(default)


def _env_str(name: str, default: str) -> str:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return str(default)
        return str(raw).strip()
    except Exception:
        return str(default)


def _force_default_env() -> None:
    os.environ.setdefault("ENTRY_BOARD_API_LOOKUP_ENABLED", "0")
    os.environ.setdefault("ENTRY_BOARD_REST_DIRECT_ENABLED", "0")
    os.environ.setdefault("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", "0")
    os.environ.setdefault("ENTRY_BOARD_MISSING_POP_PENDING", "0")
    os.environ.setdefault("ENTRY_BOARD_MISSING_RETRYABLE", "1")
    os.environ.setdefault("ENTRY_ORDER_FALSE_POP_PENDING", "1")
    os.environ.setdefault("ENTRY_BOARD_API_MAX_ATTEMPTS", "1")
    os.environ.setdefault("ENTRY_MAX_SPREAD_PCT", "0.20")
    os.environ.setdefault("ENTRY_MIN_BEST_BOARD_QTY", "0")


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _first(row: dict, keys: tuple[str, ...], default=None):
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _row_to_dict(row: Any) -> dict:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _unwrap_true_original(fn: Callable[..., Any]) -> Callable[..., Any]:
    seen: set[int] = set()
    cur: Any = fn
    while callable(cur) and id(cur) not in seen:
        seen.add(id(cur))
        nxt = None
        for attr in _ORIGINAL_ATTRS:
            cand = getattr(cur, attr, None)
            if callable(cand) and cand is not cur:
                nxt = cand
                break
        if nxt is None:
            break
        cur = nxt
    return cur if callable(cur) else fn


def _parse_hhmm(s: str, default_h: int, default_m: int) -> tuple[int, int]:
    try:
        hh, mm = str(s).strip().split(":", 1)
        return int(hh), int(mm)
    except Exception:
        return default_h, default_m


def _log_ng(reason: str, symbol: str, side: str, **detail) -> None:
    logger.warning("[FINAL ENTRY SAFETY GUARD] NG symbol=%s side=%s reason=%s detail=%s", symbol, side, reason, detail)


def _mark_skip(item: dict, reason: str, **detail) -> None:
    try:
        retryable = bool(detail.pop("retryable", False)) or reason == "board_missing"
        roots = (
            item,
            item.get("entry") if isinstance(item.get("entry"), dict) else None,
            item.get("entry_row") if isinstance(item.get("entry_row"), dict) else None,
            item.get("ai") if isinstance(item.get("ai"), dict) else None,
        )
        for root in roots:
            if isinstance(root, dict):
                root["skip_reason"] = reason
                root["final_guard_skip_reason"] = reason
                root["final_guard_skip_detail"] = detail
                root["retryable"] = retryable
                root["final_guard_retryable"] = retryable
    except Exception:
        pass


def _pop_pending_entry(symbol: str, item: dict, reason: str) -> None:
    try:
        if reason == "board_missing" and not _env_bool("ENTRY_BOARD_MISSING_POP_PENDING", False):
            logger.warning("[FINAL ENTRY SAFETY GUARD] PENDING_KEEP symbol=%s reason=%s retryable=True", symbol, reason)
            return
        entry = item.get("entry") if isinstance(item, dict) else None
        if not isinstance(entry, dict):
            return
        from trading.entry.pending_manager import pop_entry, snapshot_root
        pop_entry(symbol, entry)
        logger.warning("[FINAL ENTRY SAFETY GUARD] PENDING_POP symbol=%s reason=%s root_after=%s", symbol, reason, snapshot_root())
    except Exception:
        logger.exception("[FINAL ENTRY SAFETY GUARD] pending pop failed symbol=%s reason=%s", symbol, reason)


def _entry_time_guard(symbol: str, side: str) -> bool:
    if not _env_bool("ENTRY_TIME_GUARD_ENABLED", True):
        return True
    now = dt.datetime.now().time()
    bh, bm = _parse_hhmm(_env_str("ENTRY_NO_NEW_BEFORE", "09:05"), 9, 5)
    ah, am = _parse_hhmm(_env_str("ENTRY_NO_NEW_AFTER", "15:20"), 15, 20)
    if now < dt.time(bh, bm):
        _log_ng("time_before_allowed", symbol, side, now=now.strftime("%H:%M:%S"))
        return False
    if now >= dt.time(ah, am):
        _log_ng("time_after_allowed", symbol, side, now=now.strftime("%H:%M:%S"))
        return False
    if _env_bool("ENTRY_LUNCH_GUARD_ENABLED", True):
        sh, sm = _parse_hhmm(_env_str("ENTRY_LUNCH_BLOCK_START", "11:30"), 11, 30)
        eh, em = _parse_hhmm(_env_str("ENTRY_LUNCH_BLOCK_END", "12:30"), 12, 30)
        if dt.time(sh, sm) <= now < dt.time(eh, em):
            _log_ng("time_lunch_block", symbol, side, now=now.strftime("%H:%M:%S"))
            return False
    return True


def _liquidity_guard(row: dict, symbol: str, side: str) -> bool:
    if not _env_bool("ENTRY_FINAL_LIQUIDITY_GUARD_ENABLED", True):
        return True
    close = _safe_float(_first(row, ("close", "close_price", "price", "current_price"), 0.0), 0.0)
    volume = _safe_float(_first(row, ("volume", "Volume", "出来高"), 0.0), 0.0)
    turnover = _safe_float(_first(row, ("turnover", "trading_value", "売買代金"), 0.0), 0.0)
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume
    min_volume = _env_float("ENTRY_MIN_VOLUME", 30000.0)
    min_turnover = _env_float("ENTRY_MIN_TURNOVER", 10000000.0)
    if volume <= 0:
        _log_ng("volume_missing", symbol, side, volume=volume, turnover=turnover, close=close)
        return False
    if volume < min_volume:
        _log_ng("low_volume", symbol, side, volume=volume, min_volume=min_volume, turnover=turnover)
        return False
    if turnover < min_turnover:
        _log_ng("low_turnover", symbol, side, turnover=turnover, min_turnover=min_turnover, volume=volume, close=close)
        return False
    logger.info("[FINAL ENTRY SAFETY GUARD] LIQUIDITY_OK symbol=%s side=%s volume=%.0f turnover=%.0f min_volume=%.0f min_turnover=%.0f", symbol, side, volume, turnover, min_volume, min_turnover)
    return True


def _recent_reverse_guard(row: dict, symbol: str, side: str) -> bool:
    if not _env_bool("ENTRY_RECENT_REVERSE_GUARD_ENABLED", True):
        return True
    slope = _safe_float(_first(row, ("slope_5s", "recent_slope", "slope_atr_scaled", "score_slope", "slope"), 0.0), 0.0)
    max_bad_slope = _env_float("ENTRY_RECENT_REVERSE_MAX_BAD_SLOPE", 0.12)
    if side == "BUY" and slope <= -max_bad_slope:
        _log_ng("recent_down_against_buy", symbol, side, slope=slope)
        return False
    if side == "SELL" and slope >= max_bad_slope:
        _log_ng("recent_up_against_sell", symbol, side, slope=slope)
        return False
    logger.info("[FINAL ENTRY SAFETY GUARD] RECENT_REVERSE_OK symbol=%s side=%s slope=%.6f", symbol, side, slope)
    return True


def _extract_bid_ask_from_row(row: dict) -> tuple[float, float, float, float]:
    bid = _safe_float(_first(row, ("bid", "best_bid", "BidPrice", "bid_price"), 0.0), 0.0)
    ask = _safe_float(_first(row, ("ask", "best_ask", "AskPrice", "ask_price"), 0.0), 0.0)
    bid_qty = _safe_float(_first(row, ("bid_qty", "best_bid_qty", "BidQty", "bid_volume"), 0.0), 0.0)
    ask_qty = _safe_float(_first(row, ("ask_qty", "best_ask_qty", "AskQty", "ask_volume"), 0.0), 0.0)
    return bid, ask, bid_qty, ask_qty


def _try_get_bid_ask_from_api(symbol: str) -> tuple[float, float, float, float]:
    global _BOARD_COOLDOWN_UNTIL
    if not _env_bool("ENTRY_BOARD_API_LOOKUP_ENABLED", False):
        return 0.0, 0.0, 0.0, 0.0
    now = time.time()
    if now < _BOARD_COOLDOWN_UNTIL:
        logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_API_COOLDOWN_SKIP symbol=%s remaining=%.1fs", symbol, _BOARD_COOLDOWN_UNTIL - now)
        return 0.0, 0.0, 0.0, 0.0
    ttl = max(0.1, _env_float("ENTRY_BOARD_API_CACHE_TTL_SEC", 2.0))
    cached = _BOARD_CACHE.get(symbol)
    if cached and now - cached[0] <= ttl:
        return cached[1]
    try:
        from utils_common import get_latest_bid_ask
        res = get_latest_bid_ask(symbol)
        bid, ask, bid_qty, ask_qty = 0.0, 0.0, 0.0, 0.0
        if isinstance(res, dict):
            bid = _safe_float(res.get("bid") or res.get("best_bid") or res.get("BidPrice") or res.get("bid_price"), 0.0)
            ask = _safe_float(res.get("ask") or res.get("best_ask") or res.get("AskPrice") or res.get("ask_price"), 0.0)
            bid_qty = _safe_float(res.get("bid_qty") or res.get("BidQty") or res.get("bid_volume"), 0.0)
            ask_qty = _safe_float(res.get("ask_qty") or res.get("AskQty") or res.get("ask_volume"), 0.0)
        elif isinstance(res, (list, tuple)) and len(res) >= 2:
            bid, ask = _safe_float(res[0], 0.0), _safe_float(res[1], 0.0)
        if bid > 0 and ask > 0:
            _BOARD_CACHE[symbol] = (now, (bid, ask, bid_qty, ask_qty))
            logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_API_OK symbol=%s bid=%.4f ask=%.4f", symbol, bid, ask)
            return bid, ask, bid_qty, ask_qty
    except Exception as e:
        msg = repr(e)
        if "429" in msg or "4001006" in msg or "4002006" in msg or "API実行回数" in msg or "レジスト数" in msg:
            _BOARD_COOLDOWN_UNTIL = time.time() + max(10.0, _env_float("ENTRY_BOARD_API_ERROR_COOLDOWN_SEC", 60.0))
            logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_API_COOLDOWN_SET symbol=%s error=%s until=%.1f", symbol, msg, _BOARD_COOLDOWN_UNTIL)
        else:
            logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_API_NG symbol=%s error=%s", symbol, msg)
    return 0.0, 0.0, 0.0, 0.0


def _board_missing_fallback_ok(row: dict, item: dict, symbol: str, side: str) -> bool:
    if not _env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", False):
        return False
    close = _safe_float(_first(row, ("close", "close_price", "price", "current_price"), 0.0), 0.0)
    volume = _safe_float(_first(row, ("volume", "Volume", "出来高"), 0.0), 0.0)
    turnover = _safe_float(_first(row, ("turnover", "trading_value", "売買代金"), 0.0), 0.0)
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume
    score = abs(_safe_float(_first(row, ("score", "score_total", "final_score", "display_score", "score_sell", "score_buy"), 0.0), 0.0))
    if close < _env_float("ENTRY_ALLOW_WITHOUT_BOARD_MIN_PRICE", 200.0):
        return False
    if volume < _env_float("ENTRY_ALLOW_WITHOUT_BOARD_MIN_VOLUME", 30000.0):
        return False
    if turnover < _env_float("ENTRY_ALLOW_WITHOUT_BOARD_MIN_TURNOVER", 10000000.0):
        return False
    if score < _env_float("ENTRY_ALLOW_WITHOUT_BOARD_MIN_SCORE", 0.90):
        return False
    logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_MISSING_ALLOW_PROTECTED symbol=%s side=%s", symbol, side)
    return True


def _board_guard(row: dict, item: dict | None = None, symbol: str | None = None, side: str | None = None, *_, **__) -> bool:
    item = item if isinstance(item, dict) else {}
    row = _row_to_dict(row)
    symbol = _norm_symbol(symbol or _first(row, ("symbol", "Symbol", "code", "銘柄コード"), ""))
    side = _norm_side(side or _first(row, ("side", "entry_decision", "ai_side"), ""))
    if not _env_bool("ENTRY_BOARD_GUARD_ENABLED", True):
        return True
    bid, ask, bid_qty, ask_qty = _extract_bid_ask_from_row(row)
    if bid <= 0 or ask <= 0:
        bid2, ask2, bidq2, askq2 = _try_get_bid_ask_from_api(symbol)
        bid, ask, bid_qty, ask_qty = bid or bid2, ask or ask2, bid_qty or bidq2, ask_qty or askq2
    if bid <= 0 or ask <= 0:
        if _board_missing_fallback_ok(row, item, symbol, side):
            return True
        _mark_skip(item, "board_missing", bid=bid, ask=ask, retryable=True)
        _log_ng("board_missing", symbol, side, bid=bid, ask=ask, retryable=True, pending_action="keep")
        return False
    mid = (bid + ask) / 2.0
    spread_pct = ((ask - bid) / mid) * 100.0 if mid > 0 else 999.0
    if spread_pct > _env_float("ENTRY_MAX_SPREAD_PCT", 0.20):
        _mark_skip(item, "spread_too_wide", bid=bid, ask=ask, spread_pct=spread_pct)
        _log_ng("spread_too_wide", symbol, side, bid=bid, ask=ask, spread_pct=spread_pct)
        return False
    try:
        row.update({"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty})
        if isinstance(item.get("entry_row"), dict):
            item["entry_row"].update({"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty})
    except Exception:
        pass
    logger.info("[FINAL ENTRY SAFETY GUARD] BOARD_OK symbol=%s side=%s bid=%.4f ask=%.4f spread_pct=%.4f", symbol, side, bid, ask, spread_pct)
    return True


def _patched_board_guard(*args, **kwargs) -> bool:
    return _board_guard(*args, **kwargs)


def _call_board_guard(row: dict, item: dict, symbol: str, side: str) -> bool:
    """Call _board_guard even if another startup patch replaced it with an older 3-arg function."""
    guard = _board_guard
    attempts = (
        (row, item, symbol, side),
        (row, symbol, side),
        (row, item, symbol),
        (row, item),
        (row,),
    )
    last_type_error: TypeError | None = None
    for args in attempts:
        try:
            ok = bool(guard(*args))
            if args != attempts[0]:
                logger.warning(
                    "[FINAL ENTRY SAFETY GUARD] BOARD_GUARD_SIGNATURE_COMPAT symbol=%s side=%s argc=%s ok=%s",
                    symbol,
                    side,
                    len(args),
                    ok,
                )
            return ok
        except TypeError as e:
            last_type_error = e
            continue
        except Exception as e:
            logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_GUARD_ERROR symbol=%s side=%s error=%s", symbol, side, e)
            return _board_missing_fallback_ok(row, item, symbol, side)
    logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_GUARD_SIGNATURE_ERROR symbol=%s side=%s error=%s", symbol, side, last_type_error)
    return _board_missing_fallback_ok(row, item, symbol, side)


def _guard_fail(item: dict, symbol: str, reason: str, *, pop: bool = False, **detail) -> bool:
    retryable = bool(detail.pop("retryable", False)) or reason == "board_missing"
    _mark_skip(item, reason, retryable=retryable, **detail)
    if reason == "board_missing":
        _pop_pending_entry(symbol, item, reason)
        return False
    if pop:
        _pop_pending_entry(symbol, item, reason)
    return False


def _patched_execute_best_candidate(item: dict, boost_active: bool) -> bool:
    if not callable(_ORIG_EXECUTE_BEST_CANDIDATE):
        logger.error("[FINAL ENTRY SAFETY GUARD] original _execute_best_candidate unavailable")
        return False
    started = time.time()
    symbol = ""
    side = ""
    try:
        symbol = _norm_symbol(item.get("symbol"))
        row = _row_to_dict(item.get("entry_row"))
        side = _norm_side(item.get("side") or _first(row, ("side", "entry_decision", "ai_side"), ""))
        if side not in {"BUY", "SELL"}:
            _log_ng("unknown_side", symbol, side, item_keys=list(item.keys()))
            return _guard_fail(item, symbol, "unknown_side")
        if not _entry_time_guard(symbol, side):
            return _guard_fail(item, symbol, "time_guard_ng")
        if not _liquidity_guard(row, symbol, side):
            return _guard_fail(item, symbol, "liquidity_guard_ng")
        if not _recent_reverse_guard(row, symbol, side):
            return _guard_fail(item, symbol, "recent_reverse_guard_ng")
        if not _call_board_guard(row, item, symbol, side):
            return _guard_fail(item, symbol, "board_missing", pop=False, retryable=True)
        orig = _unwrap_true_original(_ORIG_EXECUTE_BEST_CANDIDATE)
        logger.info("[FINAL ENTRY SAFETY GUARD] ALL_OK symbol=%s side=%s", symbol, side)
        logger.warning("[FINAL ENTRY SAFETY GUARD] CALL_ORIG_START symbol=%s side=%s orig=%s boost_active=%s", symbol, side, getattr(orig, "__name__", repr(orig)), boost_active)
        ok = bool(orig(item, boost_active))
        elapsed = time.time() - started
        logger.warning("[FINAL ENTRY SAFETY GUARD] CALL_ORIG_DONE symbol=%s side=%s ok=%s elapsed=%.3fs", symbol, side, ok, elapsed)
        if not ok:
            reason = str(item.get("skip_reason") or item.get("final_guard_skip_reason") or "entry_controller_no_order")
            if reason == "board_missing" or _env_bool("ENTRY_ORDER_FALSE_POP_PENDING", True):
                _pop_pending_entry(symbol, item, reason)
        return ok
    except Exception:
        logger.exception("[FINAL ENTRY SAFETY GUARD] patched execute failed symbol=%s side=%s", symbol, side)
        return False


def _is_currently_wrapped() -> bool:
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "_execute_best_candidate", None)
        return bool(getattr(cur, "_final_entry_safety_guard_v11", False))
    except Exception:
        return False


def install() -> bool:
    global _INSTALLED, _ORIG_EXECUTE_BEST_CANDIDATE
    _force_default_env()
    try:
        import trading.handlers.entry_controller as ec
        if _INSTALLED and _is_currently_wrapped():
            return True
        old = getattr(ec, "_execute_best_candidate", None)
        if not callable(old):
            logger.error("[FINAL ENTRY SAFETY GUARD] target _execute_best_candidate unavailable")
            return False
        if getattr(old, "_final_entry_safety_guard_v11", False):
            _INSTALLED = True
            return True
        _ORIG_EXECUTE_BEST_CANDIDATE = _unwrap_true_original(old)
        _patched_execute_best_candidate._final_entry_safety_guard = True  # type: ignore[attr-defined]
        _patched_execute_best_candidate._final_entry_safety_guard_v10 = True  # type: ignore[attr-defined]
        _patched_execute_best_candidate._final_entry_safety_guard_v11 = True  # type: ignore[attr-defined]
        _patched_execute_best_candidate._final_entry_safety_guard_original = _ORIG_EXECUTE_BEST_CANDIDATE  # type: ignore[attr-defined]
        _patched_execute_best_candidate._original_execute_best_candidate = _ORIG_EXECUTE_BEST_CANDIDATE  # type: ignore[attr-defined]
        ec._execute_best_candidate = _patched_execute_best_candidate
        _INSTALLED = True
        logger.warning(
            "[FINAL ENTRY SAFETY GUARD] installed v11 original=%s board_api=%s rest_direct=%s allow_without_board=%s signature_tolerant=True",
            getattr(_ORIG_EXECUTE_BEST_CANDIDATE, "__name__", repr(_ORIG_EXECUTE_BEST_CANDIDATE)),
            _env_bool("ENTRY_BOARD_API_LOOKUP_ENABLED", False),
            _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", False),
            _env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", False),
        )
        return True
    except Exception:
        logger.exception("[FINAL ENTRY SAFETY GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[FINAL ENTRY SAFETY GUARD] auto install failed")


__all__ = ["install", "_board_guard", "_patched_board_guard", "_call_board_guard"]
