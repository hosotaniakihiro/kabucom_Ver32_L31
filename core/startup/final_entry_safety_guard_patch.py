# ============================================================
# File   : core/startup/final_entry_safety_guard_patch.py
# Version: Ver05-BOARD-GUARD-CALL-COMPAT
# ------------------------------------------------------------
# entry_controller._execute_best_candidate を runtime patch し、
# 発注直前の最終安全ガードを追加する。
#
# Ver04:
#   - PUSH登録保護済み/AI候補で、流動性OKの銘柄が board_missing だけで
#     連続停止していたため、板欠損時のfail-openを追加。
#   - ただし無条件ではなく、価格・出来高・売買代金・scoreが最低条件を満たす時だけ許可。
#   - 板欠損で許可した場合は小ロット化し、既存のentry_price_improvement/発注側に任せる。
#
# Ver05:
#   - _board_guard の呼び出し形式を 3引数/4引数 両対応にする。
#   - 古い runtime patch が _patched_board_guard(row, symbol, side) 形式で残っていても、
#     _board_guard(row, item, symbol, side) 形式で呼ばれても TypeError で落ちないようにする。
#
# 優先度3「当日損失上限で新規停止」は、ユーザー要望により未実装。
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


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
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


def _env_str(name: str, default: str) -> str:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return str(default)
        return str(v).strip()
    except Exception:
        return str(default)


def _force_default_env() -> None:
    # 板が取れないだけでAI候補が毎回止まるため、保護候補/流動性OKなら小ロットで許可する。
    os.environ.setdefault("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", "1")
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_VOLUME", "30000")
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_TURNOVER", "10000000")
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_PRICE", "200")
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_SCORE", "0.90")
    os.environ.setdefault("ENTRY_BOARD_MISSING_QTY_RATIO", "0.50")


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
        sh, sm = _parse_hhmm(_env_str("ENTRY_LUNCH_BLOCK_START", "11:25"), 11, 25)
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
    for attr in ("recent_realized_pnl_map", "daily_symbol_realized_pnl_map", "symbol_realized_pnl_map", "realized_pnl_by_symbol"):
        mp = getattr(global_data, attr, None)
        if not isinstance(mp, dict):
            continue
        pnl = _safe_float(mp.get(symbol), 0.0)
        if pnl < _env_float("ENTRY_SAME_SYMBOL_LOSS_LOCK_PNL_BELOW", 0.0):
            _log_ng("same_symbol_realized_loss", symbol, side, source=attr, pnl=pnl)
            return False
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
        _log_ng("recent_down_against_buy", symbol, side, pc3=pc3p, pc5=pc5p, pc10=pc10p, slope=slope, limits=(buy_min_3, buy_min_5, buy_min_10, -max_bad_slope))
        return False
    if side == "SELL" and (pc3p > sell_max_3 or pc5p > sell_max_5 or pc10p > sell_max_10 or slope >= max_bad_slope):
        _log_ng("recent_up_against_sell", symbol, side, pc3=pc3p, pc5=pc5p, pc10=pc10p, slope=slope, limits=(sell_max_3, sell_max_5, sell_max_10, max_bad_slope))
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
    try:
        from utils_common import get_latest_bid_ask
        res = get_latest_bid_ask(symbol)
        if isinstance(res, dict):
            return (
                _safe_float(res.get("bid") or res.get("best_bid") or res.get("BidPrice") or res.get("bid_price"), 0.0),
                _safe_float(res.get("ask") or res.get("best_ask") or res.get("AskPrice") or res.get("ask_price"), 0.0),
                _safe_float(res.get("bid_qty") or res.get("BidQty") or res.get("bid_volume"), 0.0),
                _safe_float(res.get("ask_qty") or res.get("AskQty") or res.get("ask_volume"), 0.0),
            )
        if isinstance(res, (list, tuple)) and len(res) >= 2:
            return _safe_float(res[0], 0.0), _safe_float(res[1], 0.0), 0.0, 0.0
    except Exception:
        logger.debug("[FINAL ENTRY SAFETY GUARD] get_latest_bid_ask failed symbol=%s", symbol, exc_info=True)
    return 0.0, 0.0, 0.0, 0.0


def _board_missing_fallback_ok(row: dict, item: dict, symbol: str, side: str) -> bool:
    if not _env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", True):
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
            "[FINAL ENTRY SAFETY GUARD] BOARD_MISSING_FALLBACK_NG symbol=%s side=%s close=%.2f volume=%.0f turnover=%.0f score=%.3f limits price>=%.1f volume>=%.0f turnover>=%.0f score>=%.2f",
            symbol, side, close, volume, turnover, score, min_price, min_volume, min_turnover, min_score,
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
    logger.warning(
        "[FINAL ENTRY SAFETY GUARD] BOARD_MISSING_ALLOW_PROTECTED symbol=%s side=%s close=%.2f volume=%.0f turnover=%.0f score=%.3f qty_ratio=%s",
        symbol, side, close, volume, turnover, score, os.getenv("ENTRY_BOARD_MISSING_QTY_RATIO"),
    )
    return True


def _board_guard(row: dict, item: dict | None = None, symbol: str | None = None, side: str | None = None, *_, **__) -> bool:
    """
    板ガード。

    互換性のため、以下の両方を許容する。
      - _board_guard(row, symbol, side)
      - _board_guard(row, item, symbol, side)

    旧runtime/別patchが 3引数形式を前提にしていても、ここで吸収して
    patched execute failed の TypeError を防ぐ。
    """
    if side is None and symbol is not None:
        # 旧形式: _board_guard(row, symbol, side)
        side = symbol
        symbol = item  # type: ignore[assignment]
        item = None

    row = _row_to_dict(row)
    item = item if isinstance(item, dict) else {}
    symbol = _norm_symbol(symbol or _first(row, ("symbol", "Symbol", "code", "銘柄コード"), ""))
    side = _norm_side(side or _first(row, ("side", "entry_decision", "ai_side"), ""))

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
        _log_ng("board_missing", symbol, side, bid=bid, ask=ask, message="板が取れないため新規エントリー停止")
        return False
    mid = (bid + ask) / 2.0
    spread_pct = ((ask - bid) / mid) * 100.0 if mid > 0 else 999.0
    max_spread = _env_float("ENTRY_MAX_SPREAD_PCT", 0.15)
    min_best_qty = _env_float("ENTRY_MIN_BEST_BOARD_QTY", 100.0)
    if spread_pct > max_spread:
        _log_ng("spread_too_wide", symbol, side, bid=bid, ask=ask, spread_pct=spread_pct, max_spread=max_spread)
        return False
    if side == "BUY" and ask_qty > 0 and ask_qty < min_best_qty:
        _log_ng("ask_board_too_thin", symbol, side, ask_qty=ask_qty, min_best_qty=min_best_qty, bid=bid, ask=ask)
        return False
    if side == "SELL" and bid_qty > 0 and bid_qty < min_best_qty:
        _log_ng("bid_board_too_thin", symbol, side, bid_qty=bid_qty, min_best_qty=min_best_qty, bid=bid, ask=ask)
        return False
    logger.info("[FINAL ENTRY SAFETY GUARD] BOARD_OK symbol=%s side=%s bid=%.4f ask=%.4f spread_pct=%.4f bid_qty=%.0f ask_qty=%.0f", symbol, side, bid, ask, spread_pct, bid_qty, ask_qty)
    return True


# 互換名。古いログ/別パッチで _patched_board_guard と表示される環境でも同じ実装を使う。
_patched_board_guard = _board_guard


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
            return False
        if not _entry_time_guard(symbol, side):
            return False
        if not _liquidity_guard(row, symbol, side):
            return False
        if not _same_symbol_loss_guard(symbol, side):
            return False
        if not _recent_reverse_guard(row, symbol, side):
            return False
        if not _board_guard(row, item, symbol, side):
            return False
        _apply_contrarian_half_size(item, row, symbol, side)

        logger.info("[FINAL ENTRY SAFETY GUARD] ALL_OK symbol=%s side=%s", symbol, side)
        logger.warning("[FINAL ENTRY SAFETY GUARD] CALL_ORIG_START symbol=%s side=%s orig=%s boost_active=%s", symbol, side, getattr(_ORIG_EXECUTE_BEST_CANDIDATE, "__name__", repr(_ORIG_EXECUTE_BEST_CANDIDATE)), boost_active)
        ok = bool(_ORIG_EXECUTE_BEST_CANDIDATE(item, boost_active))
        elapsed = time.time() - started
        logger.warning("[FINAL ENTRY SAFETY GUARD] CALL_ORIG_DONE symbol=%s side=%s ok=%s elapsed=%.3fs", symbol, side, ok, elapsed)
        if not ok:
            logger.warning("[FINAL ENTRY SAFETY GUARD] ORIG_RETURN_FALSE symbol=%s side=%s elapsed=%.3fs", symbol, side, elapsed)
        return ok
    except Exception:
        logger.exception("[FINAL ENTRY SAFETY GUARD] patched execute failed symbol=%s side=%s", symbol, side)
        return False


def _is_currently_wrapped() -> bool:
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, "_execute_best_candidate", None)
        return bool(getattr(cur, "_final_entry_safety_guard_v05", False))
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
        if getattr(old, "_final_entry_safety_guard_v05", False):
            _INSTALLED = True
            return True
        if getattr(old, "_final_entry_safety_guard", False) and _ORIG_EXECUTE_BEST_CANDIDATE is not None:
            old = _ORIG_EXECUTE_BEST_CANDIDATE
        _ORIG_EXECUTE_BEST_CANDIDATE = old
        _patched_execute_best_candidate._final_entry_safety_guard = True  # type: ignore[attr-defined]
        _patched_execute_best_candidate._final_entry_safety_guard_v05 = True  # type: ignore[attr-defined]
        _patched_execute_best_candidate._original_execute_best_candidate = old  # type: ignore[attr-defined]
        ec._execute_best_candidate = _patched_execute_best_candidate
        _INSTALLED = True
        logger.warning(
            "[FINAL ENTRY SAFETY GUARD] installed v05 liquidity=%s min_volume=%.0f min_turnover=%.0f same_symbol_loss=%s recent_reverse=%s time_guard=%s board_guard=%s allow_without_board=%s board_missing_qty_ratio=%.2f contrarian_half=%s qty_ratio=%.2f daily_loss_guard=NOT_INSTALLED_BY_REQUEST",
            _env_bool("ENTRY_FINAL_LIQUIDITY_GUARD_ENABLED", True),
            _env_float("ENTRY_MIN_VOLUME", 30000.0),
            _env_float("ENTRY_MIN_TURNOVER", 10000000.0),
            _env_bool("ENTRY_SAME_SYMBOL_LOSS_LOCK_ENABLED", True),
            _env_bool("ENTRY_RECENT_REVERSE_GUARD_ENABLED", True),
            _env_bool("ENTRY_TIME_GUARD_ENABLED", True),
            _env_bool("ENTRY_BOARD_GUARD_ENABLED", True),
            _env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", True),
            _env_float("ENTRY_BOARD_MISSING_QTY_RATIO", 0.5),
            _env_bool("ENTRY_CONTRARIAN_HALF_SIZE_ENABLED", True),
            _env_float("ENTRY_CONTRARIAN_QTY_RATIO", 0.5),
        )
        return True
    except Exception:
        logger.exception("[FINAL ENTRY SAFETY GUARD] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[FINAL ENTRY SAFETY GUARD] auto install failed")


__all__ = ["install"]
