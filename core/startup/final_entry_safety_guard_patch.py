# ============================================================
# File   : core/startup/final_entry_safety_guard_patch.py
# Version: Ver09-BOARD-MISSING-RETRY-PENDING-KEEP
# ------------------------------------------------------------
# entry_controller._execute_best_candidate を runtime patch し、
# 発注直前の最終安全ガードを追加する。
#
# Ver09:
#   - board_missing では pending を削除しない。
#   - board_missing は retryable=True として item/entry/ai に明示する。
#   - 板なし発注は既定では許可しないが、次サイクルで再試行できるよう候補を残す。
#   - order false 時も reason=board_missing の場合は pending を pop しない。
#   - main.py は PUSH DB 保存しない方針を維持する。
#   - 優先度3「当日損失上限で新規停止」はユーザー要望により未実装。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIG_EXECUTE_BEST_CANDIDATE = None

_TRUE_SET = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE_SET = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


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
        return float(raw)
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
    # PUSHローテ中に対象銘柄が未登録でもREST /boardで補完する。
    os.environ.setdefault("ENTRY_BOARD_API_LOOKUP_ENABLED", "1")
    # 板なし発注は誤発注・価格未確定の原因になるため既定NG。
    os.environ.setdefault("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", "0")
    # Ver09: 板取得失敗は一時障害として扱う。候補を消さず次サイクルで再試行する。
    os.environ.setdefault("ENTRY_BOARD_MISSING_POP_PENDING", "0")
    os.environ.setdefault("ENTRY_BOARD_MISSING_RETRYABLE", "1")
    # order false は原則古いpending詰まり対策でpopするが、board_missingだけは下でpopしない。
    os.environ.setdefault("ENTRY_ORDER_FALSE_POP_PENDING", "1")
    os.environ.setdefault("ENTRY_BOARD_API_MAX_ATTEMPTS", "2")
    os.environ.setdefault("ENTRY_MAX_SPREAD_PCT", "0.20")
    os.environ.setdefault("ENTRY_MIN_BEST_BOARD_QTY", "0")


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


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


def _parse_hhmm(s: str, default_h: int, default_m: int) -> tuple[int, int]:
    try:
        hh, mm = str(s).strip().split(":", 1)
        return int(hh), int(mm)
    except Exception:
        return default_h, default_m


def _log_ng(reason: str, symbol: str, side: str, **detail) -> None:
    logger.warning(
        "[FINAL ENTRY SAFETY GUARD] NG symbol=%s side=%s reason=%s detail=%s",
        symbol,
        side,
        reason,
        detail,
    )


def _mark_skip(item: dict, reason: str, **detail) -> None:
    try:
        retryable = bool(detail.pop("retryable", False)) or reason == "board_missing"
        item["skip_reason"] = reason
        item["final_guard_skip_reason"] = reason
        item["final_guard_skip_detail"] = detail
        item["retryable"] = retryable
        item["final_guard_retryable"] = retryable
        entry = item.get("entry")
        if isinstance(entry, dict):
            entry["skip_reason"] = reason
            entry["final_guard_skip_reason"] = reason
            entry["retryable"] = retryable
            entry["final_guard_retryable"] = retryable
        row = item.get("entry_row")
        if isinstance(row, dict):
            row["skip_reason"] = reason
            row["final_guard_skip_reason"] = reason
            row["retryable"] = retryable
            row["final_guard_retryable"] = retryable
        ai = item.get("ai")
        if isinstance(ai, dict):
            ai["skip_reason"] = reason
            ai["final_guard_skip_reason"] = reason
            ai["retryable"] = retryable
            ai["final_guard_retryable"] = retryable
    except Exception:
        pass


def _pop_pending_entry(symbol: str, item: dict, reason: str) -> None:
    try:
        if reason == "board_missing" and not _env_bool("ENTRY_BOARD_MISSING_POP_PENDING", False):
            logger.warning(
                "[FINAL ENTRY SAFETY GUARD] PENDING_KEEP symbol=%s reason=%s retryable=True",
                symbol,
                reason,
            )
            return
        entry = item.get("entry") if isinstance(item, dict) else None
        if not isinstance(entry, dict):
            return
        from trading.entry.pending_manager import pop_entry, snapshot_root
        pop_entry(symbol, entry)
        logger.warning(
            "[FINAL ENTRY SAFETY GUARD] PENDING_POP symbol=%s reason=%s root_after=%s",
            symbol,
            reason,
            snapshot_root(),
        )
    except Exception:
        logger.exception("[FINAL ENTRY SAFETY GUARD] pending pop failed symbol=%s reason=%s", symbol, reason)


def _entry_time_guard(symbol: str, side: str) -> bool:
    if not _env_bool("ENTRY_TIME_GUARD_ENABLED", True):
        return True
    now = dt.datetime.now()
    now_t = now.time()
    bh, bm = _parse_hhmm(_env_str("ENTRY_NO_NEW_BEFORE", "09:05"), 9, 5)
    ah, am = _parse_hhmm(_env_str("ENTRY_NO_NEW_AFTER", "15:20"), 15, 20)
    before_t = dt.time(bh, bm)
    after_t = dt.time(ah, am)
    if now_t < before_t:
        _log_ng("time_before_allowed", symbol, side, now=now.strftime("%H:%M:%S"), no_new_before=before_t.strftime("%H:%M"))
        return False
    if now_t >= after_t:
        _log_ng("time_after_allowed", symbol, side, now=now.strftime("%H:%M:%S"), no_new_after=after_t.strftime("%H:%M"))
        return False
    if _env_bool("ENTRY_LUNCH_GUARD_ENABLED", True):
        sh, sm = _parse_hhmm(_env_str("ENTRY_LUNCH_BLOCK_START", "11:30"), 11, 30)
        eh, em = _parse_hhmm(_env_str("ENTRY_LUNCH_BLOCK_END", "12:30"), 12, 30)
        st = dt.time(sh, sm)
        et = dt.time(eh, em)
        if st <= now_t < et:
            _log_ng("time_lunch_block", symbol, side, now=now.strftime("%H:%M:%S"), lunch_start=st.strftime("%H:%M"), lunch_end=et.strftime("%H:%M"))
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
    logger.info(
        "[FINAL ENTRY SAFETY GUARD] LIQUIDITY_OK symbol=%s side=%s volume=%.0f turnover=%.0f min_volume=%.0f min_turnover=%.0f",
        symbol,
        side,
        volume,
        turnover,
        min_volume,
        min_turnover,
    )
    return True


def _same_symbol_loss_guard(symbol: str, side: str) -> bool:
    if not _env_bool("ENTRY_SAME_SYMBOL_LOSS_LOCK_ENABLED", True):
        return True
    try:
        from global_state import global_data
    except Exception:
        return True
    for attr in ("same_symbol_loss_locked_set", "entry_loss_locked_symbols", "daily_loss_locked_symbols", "symbol_loss_locked_set"):
        locked = getattr(global_data, attr, None)
        try:
            if isinstance(locked, (set, list, tuple)) and symbol in {str(x) for x in locked}:
                _log_ng("same_symbol_loss_locked", symbol, side, source=attr)
                return False
        except Exception:
            pass
    min_count = _safe_int(_env_float("ENTRY_SAME_SYMBOL_LOSS_LOCK_MIN_COUNT", 1.0), 1)
    for attr in ("symbol_loss_count_map", "daily_symbol_loss_count_map", "recent_symbol_loss_count_map"):
        mp = getattr(global_data, attr, None)
        if not isinstance(mp, dict):
            continue
        cnt = _safe_int(mp.get(symbol), 0)
        if cnt >= min_count:
            _log_ng("same_symbol_loss_count_locked", symbol, side, source=attr, loss_count=cnt, min_count=min_count)
            return False
    return True


def _recent_reverse_guard(row: dict, symbol: str, side: str) -> bool:
    if not _env_bool("ENTRY_RECENT_REVERSE_GUARD_ENABLED", True):
        return True
    pc3 = _safe_float(_first(row, ("price_change_3", "change_3", "ret_3", "return_3", "price_change_3s", "change_3s"), 0.0), 0.0)
    pc5 = _safe_float(_first(row, ("price_change_5", "change_5", "ret_5", "return_5", "price_change_5s", "change_5s"), 0.0), 0.0)
    pc10 = _safe_float(_first(row, ("price_change_10", "change_10", "ret_10", "return_10", "price_change_10s", "change_10s"), 0.0), 0.0)
    slope = _safe_float(_first(row, ("slope_5s", "recent_slope", "slope_atr_scaled", "score_slope", "slope"), 0.0), 0.0)

    def _as_pct(v: float) -> float:
        return v * 100.0 if abs(v) <= 1.0 else v

    pc3p, pc5p, pc10p = _as_pct(pc3), _as_pct(pc5), _as_pct(pc10)
    buy_min_3 = _env_float("ENTRY_BUY_MIN_RECENT_3_CHANGE_PCT", -0.05)
    buy_min_5 = _env_float("ENTRY_BUY_MIN_RECENT_5_CHANGE_PCT", -0.10)
    buy_min_10 = _env_float("ENTRY_BUY_MIN_RECENT_10_CHANGE_PCT", -0.15)
    sell_max_3 = _env_float("ENTRY_SELL_MAX_RECENT_3_CHANGE_PCT", 0.05)
    sell_max_5 = _env_float("ENTRY_SELL_MAX_RECENT_5_CHANGE_PCT", 0.10)
    sell_max_10 = _env_float("ENTRY_SELL_MAX_RECENT_10_CHANGE_PCT", 0.15)
    max_bad_slope = _env_float("ENTRY_RECENT_REVERSE_MAX_BAD_SLOPE", 0.12)
    if pc3 == 0 and pc5 == 0 and pc10 == 0 and slope == 0:
        logger.info("[FINAL ENTRY SAFETY GUARD] RECENT_REVERSE_SKIP symbol=%s side=%s reason=no_recent_data", symbol, side)
        return True
    if side == "BUY" and (pc3p < buy_min_3 or pc5p < buy_min_5 or pc10p < buy_min_10 or slope <= -max_bad_slope):
        _log_ng("recent_down_against_buy", symbol, side, pc3=pc3p, pc5=pc5p, pc10=pc10p, slope=slope)
        return False
    if side == "SELL" and (pc3p > sell_max_3 or pc5p > sell_max_5 or pc10p > sell_max_10 or slope >= max_bad_slope):
        _log_ng("recent_up_against_sell", symbol, side, pc3=pc3p, pc5=pc5p, pc10=pc10p, slope=slope)
        return False
    logger.info("[FINAL ENTRY SAFETY GUARD] RECENT_REVERSE_OK symbol=%s side=%s pc3=%.3f pc5=%.3f pc10=%.3f slope=%.6f", symbol, side, pc3p, pc5p, pc10p, slope)
    return True


def _extract_bid_ask_from_row(row: dict) -> tuple[float, float, float, float]:
    bid = _safe_float(_first(row, ("bid", "best_bid", "BidPrice", "bid_price"), 0.0), 0.0)
    ask = _safe_float(_first(row, ("ask", "best_ask", "AskPrice", "ask_price"), 0.0), 0.0)
    bid_qty = _safe_float(_first(row, ("bid_qty", "best_bid_qty", "BidQty", "bid_volume"), 0.0), 0.0)
    ask_qty = _safe_float(_first(row, ("ask_qty", "best_ask_qty", "AskQty", "ask_volume"), 0.0), 0.0)
    return bid, ask, bid_qty, ask_qty


def _try_get_bid_ask_from_api(symbol: str) -> tuple[float, float, float, float]:
    if not _env_bool("ENTRY_BOARD_API_LOOKUP_ENABLED", True):
        logger.info("[FINAL ENTRY SAFETY GUARD] BOARD_API_LOOKUP_SKIP symbol=%s enabled=0", symbol)
        return 0.0, 0.0, 0.0, 0.0
    attempts = max(1, min(3, _safe_int(os.getenv("ENTRY_BOARD_API_MAX_ATTEMPTS"), 2)))
    for i in range(attempts):
        try:
            from utils_common import get_latest_bid_ask
            res = get_latest_bid_ask(symbol)
            if isinstance(res, dict):
                bid = _safe_float(res.get("bid") or res.get("best_bid") or res.get("BidPrice") or res.get("bid_price"), 0.0)
                ask = _safe_float(res.get("ask") or res.get("best_ask") or res.get("AskPrice") or res.get("ask_price"), 0.0)
                bid_qty = _safe_float(res.get("bid_qty") or res.get("BidQty") or res.get("bid_volume"), 0.0)
                ask_qty = _safe_float(res.get("ask_qty") or res.get("AskQty") or res.get("ask_volume"), 0.0)
                if bid > 0 and ask > 0:
                    logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_API_OK symbol=%s attempt=%s bid=%.4f ask=%.4f", symbol, i + 1, bid, ask)
                    return bid, ask, bid_qty, ask_qty
            if isinstance(res, (list, tuple)) and len(res) >= 2:
                bid = _safe_float(res[0], 0.0)
                ask = _safe_float(res[1], 0.0)
                if bid > 0 and ask > 0:
                    logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_API_OK symbol=%s attempt=%s bid=%.4f ask=%.4f", symbol, i + 1, bid, ask)
                    return bid, ask, 0.0, 0.0
        except Exception as e:
            logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_API_NG symbol=%s attempt=%s/%s error=%s", symbol, i + 1, attempts, e)
        if i + 1 < attempts:
            time.sleep(max(0.05, _env_float("ENTRY_BOARD_API_RETRY_SLEEP_SEC", 0.25)))
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
    min_price = _env_float("ENTRY_ALLOW_WITHOUT_BOARD_MIN_PRICE", 200.0)
    min_volume = _env_float("ENTRY_ALLOW_WITHOUT_BOARD_MIN_VOLUME", 30000.0)
    min_turnover = _env_float("ENTRY_ALLOW_WITHOUT_BOARD_MIN_TURNOVER", 10000000.0)
    min_score = _env_float("ENTRY_ALLOW_WITHOUT_BOARD_MIN_SCORE", 0.90)
    if close < min_price or volume < min_volume or turnover < min_turnover or score < min_score:
        logger.warning(
            "[FINAL ENTRY SAFETY GUARD] BOARD_MISSING_FALLBACK_NG symbol=%s side=%s close=%.2f volume=%.0f turnover=%.0f score=%.3f",
            symbol, side, close, volume, turnover, score,
        )
        return False
    try:
        ai = item.get("ai")
        if isinstance(ai, dict):
            old_lot = _safe_float(ai.get("lot_multiplier"), 1.0)
            ratio = max(0.1, min(1.0, _env_float("ENTRY_BOARD_MISSING_QTY_RATIO", 0.5)))
            ai["lot_multiplier"] = max(0.1, old_lot * ratio)
            ai["board_missing_qty_ratio"] = ratio
            ai["board_missing_fallback"] = True
    except Exception:
        pass
    logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_MISSING_ALLOW_PROTECTED symbol=%s side=%s", symbol, side)
    return True


def _coerce_board_guard_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[dict, dict, str, str]:
    row = _row_to_dict(args[0] if len(args) >= 1 else kwargs.get("row"))
    item: dict = {}
    symbol: Any = kwargs.get("symbol")
    side: Any = kwargs.get("side")
    if len(args) >= 4:
        item = args[1] if isinstance(args[1], dict) else {}
        symbol = args[2]
        side = args[3]
    elif len(args) >= 3:
        item = kwargs.get("item") if isinstance(kwargs.get("item"), dict) else {}
        symbol = args[1]
        side = args[2]
    elif len(args) >= 2:
        if isinstance(args[1], dict):
            item = args[1]
        else:
            symbol = args[1]
    if not isinstance(item, dict):
        item = {}
    symbol = _norm_symbol(symbol or _first(row, ("symbol", "Symbol", "code", "銘柄コード"), ""))
    side = _norm_side(side or _first(row, ("side", "entry_decision", "ai_side"), ""))
    return row, item, symbol, side


def _board_guard(*args, **kwargs) -> bool:
    row, item, symbol, side = _coerce_board_guard_args(args, kwargs)
    if not symbol or side not in {"BUY", "SELL"}:
        _log_ng("board_guard_invalid_args", symbol, side, row_keys=list(row.keys()), item_keys=list(item.keys()))
        return False
    if not _env_bool("ENTRY_BOARD_GUARD_ENABLED", True):
        return True

    bid, ask, bid_qty, ask_qty = _extract_bid_ask_from_row(row)
    if bid <= 0 or ask <= 0:
        bid2, ask2, bidq2, askq2 = _try_get_bid_ask_from_api(symbol)
        bid = bid or bid2
        ask = ask or ask2
        bid_qty = bid_qty or bidq2
        ask_qty = ask_qty or askq2

    if bid <= 0 or ask <= 0:
        if _board_missing_fallback_ok(row, item, symbol, side):
            return True
        _mark_skip(item, "board_missing", bid=bid, ask=ask, retryable=True)
        _log_ng(
            "board_missing",
            symbol,
            side,
            bid=bid,
            ask=ask,
            retryable=True,
            pending_action="keep",
            message="板が取れないため今回の新規エントリーは見送り、pendingを残して次サイクルで再試行",
        )
        return False

    mid = (bid + ask) / 2.0
    spread_pct = ((ask - bid) / mid) * 100.0 if mid > 0 else 999.0
    max_spread = _env_float("ENTRY_MAX_SPREAD_PCT", 0.20)
    min_best_qty = _env_float("ENTRY_MIN_BEST_BOARD_QTY", 0.0)
    if spread_pct > max_spread:
        _mark_skip(item, "spread_too_wide", bid=bid, ask=ask, spread_pct=spread_pct)
        _log_ng("spread_too_wide", symbol, side, bid=bid, ask=ask, spread_pct=spread_pct, max_spread=max_spread)
        return False
    if side == "BUY" and ask_qty > 0 and ask_qty < min_best_qty:
        _mark_skip(item, "ask_board_too_thin", ask_qty=ask_qty)
        _log_ng("ask_board_too_thin", symbol, side, ask_qty=ask_qty, min_best_qty=min_best_qty, bid=bid, ask=ask)
        return False
    if side == "SELL" and bid_qty > 0 and bid_qty < min_best_qty:
        _mark_skip(item, "bid_board_too_thin", bid_qty=bid_qty)
        _log_ng("bid_board_too_thin", symbol, side, bid_qty=bid_qty, min_best_qty=min_best_qty, bid=bid, ask=ask)
        return False

    try:
        row["bid"] = bid
        row["ask"] = ask
        row["bid_qty"] = bid_qty
        row["ask_qty"] = ask_qty
        if isinstance(item, dict) and isinstance(item.get("entry_row"), dict):
            item["entry_row"].update({"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty})
    except Exception:
        pass
    logger.info("[FINAL ENTRY SAFETY GUARD] BOARD_OK symbol=%s side=%s bid=%.4f ask=%.4f spread_pct=%.4f bid_qty=%.0f ask_qty=%.0f", symbol, side, bid, ask, spread_pct, bid_qty, ask_qty)
    return True


def _patched_board_guard(*args, **kwargs) -> bool:
    return _board_guard(*args, **kwargs)


def _call_board_guard(row: dict, item: dict, symbol: str, side: str) -> bool:
    try:
        return bool(_board_guard(row, item, symbol, side))
    except TypeError:
        return bool(_board_guard(row, symbol, side))
    except Exception as e:
        logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_GUARD_ERROR symbol=%s side=%s error=%s", symbol, side, e)
        return _board_missing_fallback_ok(row, item, symbol, side)


def _apply_contrarian_half_size(item: dict, row: dict, symbol: str, side: str) -> None:
    if not _env_bool("ENTRY_CONTRARIAN_HALF_SIZE_ENABLED", True):
        return
    climax = bool(row.get("climax_reversal_exception") or row.get("contrarian_reversed"))
    ctype = str(row.get("climax_type") or row.get("reverse_reason") or "")
    if not climax and "contrarian" not in ctype and "climax" not in ctype:
        return
    ai = item.get("ai")
    if not isinstance(ai, dict):
        return
    ratio = _env_float("ENTRY_CONTRARIAN_QTY_RATIO", 0.5)
    old_lot = _safe_float(ai.get("lot_multiplier"), 1.0)
    new_lot = max(0.1, old_lot * ratio)
    ai["lot_multiplier"] = new_lot
    ai["contrarian_half_size"] = True
    ai["contrarian_qty_ratio"] = ratio
    logger.warning("[FINAL ENTRY SAFETY GUARD] CONTRARIAN_HALF_SIZE symbol=%s side=%s type=%s lot_multiplier %.3f -> %.3f ratio=%.3f", symbol, side, ctype, old_lot, new_lot, ratio)


def _guard_fail(item: dict, symbol: str, reason: str, *, pop: bool = False, **detail) -> bool:
    retryable = bool(detail.pop("retryable", False)) or reason == "board_missing"
    _mark_skip(item, reason, retryable=retryable, **detail)
    if reason == "board_missing":
        # Ver09: board_missing は一時的なREST/WS/ローテ問題として扱い、pendingを残す。
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
        if not _same_symbol_loss_guard(symbol, side):
            return _guard_fail(item, symbol, "same_symbol_loss_guard_ng")
        if not _recent_reverse_guard(row, symbol, side):
            return _guard_fail(item, symbol, "recent_reverse_guard_ng")
        if not _call_board_guard(row, item, symbol, side):
            return _guard_fail(
                item,
                symbol,
                "board_missing",
                pop=False,
                retryable=True,
            )
        _apply_contrarian_half_size(item, row, symbol, side)

        logger.info("[FINAL ENTRY SAFETY GUARD] ALL_OK symbol=%s side=%s", symbol, side)
        logger.warning("[FINAL ENTRY SAFETY GUARD] CALL_ORIG_START symbol=%s side=%s orig=%s boost_active=%s", symbol, side, getattr(_ORIG_EXECUTE_BEST_CANDIDATE, "__name__", repr(_ORIG_EXECUTE_BEST_CANDIDATE)), boost_active)
        ok = bool(_ORIG_EXECUTE_BEST_CANDIDATE(item, boost_active))
        elapsed = time.time() - started
        logger.warning("[FINAL ENTRY SAFETY GUARD] CALL_ORIG_DONE symbol=%s side=%s ok=%s elapsed=%.3fs", symbol, side, ok, elapsed)
        if not ok:
            reason = str(item.get("skip_reason") or item.get("final_guard_skip_reason") or "entry_controller_no_order")
            logger.warning("[FINAL ENTRY SAFETY GUARD] ORIG_RETURN_FALSE symbol=%s side=%s reason=%s elapsed=%.3fs", symbol, side, reason, elapsed)
            if reason == "board_missing":
                _pop_pending_entry(symbol, item, reason)
            elif _env_bool("ENTRY_ORDER_FALSE_POP_PENDING", True):
                _pop_pending_entry(symbol, item, reason)
        return ok
    except Exception:
        logger.exception("[FINAL ENTRY SAFETY GUARD] patched execute failed symbol=%s side=%s", symbol, side)
        return False


def _is_currently_wrapped() -> bool:
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "_execute_best_candidate", None)
        return bool(getattr(cur, "_final_entry_safety_guard_v09", False))
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
        if getattr(old, "_final_entry_safety_guard_v09", False):
            _INSTALLED = True
            return True
        if (getattr(old, "_final_entry_safety_guard", False) or getattr(old, "_final_entry_safety_guard_v08", False)) and _ORIG_EXECUTE_BEST_CANDIDATE is not None:
            old = _ORIG_EXECUTE_BEST_CANDIDATE
        _ORIG_EXECUTE_BEST_CANDIDATE = old
        _patched_execute_best_candidate._final_entry_safety_guard = True  # type: ignore[attr-defined]
        _patched_execute_best_candidate._final_entry_safety_guard_v08 = True  # type: ignore[attr-defined]
        _patched_execute_best_candidate._final_entry_safety_guard_v09 = True  # type: ignore[attr-defined]
        _patched_execute_best_candidate._original_execute_best_candidate = old  # type: ignore[attr-defined]
        ec._execute_best_candidate = _patched_execute_best_candidate
        _INSTALLED = True
        logger.warning(
            "[FINAL ENTRY SAFETY GUARD] installed v09 liquidity=%s min_volume=%.0f min_turnover=%.0f same_symbol_loss=%s recent_reverse=%s time_guard=%s board_guard=%s board_api_lookup=%s allow_without_board=%s board_missing_pop=%s board_missing_retryable=%s order_false_pop=%s daily_loss_guard=NOT_INSTALLED_BY_REQUEST",
            _env_bool("ENTRY_FINAL_LIQUIDITY_GUARD_ENABLED", True),
            _env_float("ENTRY_MIN_VOLUME", 30000.0),
            _env_float("ENTRY_MIN_TURNOVER", 10000000.0),
            _env_bool("ENTRY_SAME_SYMBOL_LOSS_LOCK_ENABLED", True),
            _env_bool("ENTRY_RECENT_REVERSE_GUARD_ENABLED", True),
            _env_bool("ENTRY_TIME_GUARD_ENABLED", True),
            _env_bool("ENTRY_BOARD_GUARD_ENABLED", True),
            _env_bool("ENTRY_BOARD_API_LOOKUP_ENABLED", True),
            _env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", False),
            _env_bool("ENTRY_BOARD_MISSING_POP_PENDING", False),
            _env_bool("ENTRY_BOARD_MISSING_RETRYABLE", True),
            _env_bool("ENTRY_ORDER_FALSE_POP_PENDING", True),
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
