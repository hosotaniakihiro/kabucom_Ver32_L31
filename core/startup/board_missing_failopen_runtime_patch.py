# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/board_missing_failopen_runtime_patch.py
# Version: V1.4-SUMMARY-AI-BOARD-MISSING-RESCUE
# ------------------------------------------------------------
# Purpose:
#   - PUSH A/B ローテーション境界などで板が一時的に取れない場合、
#     final_entry_safety_guard の流動性/score条件を満たす候補は
#     小ロットで fail-open する。
#   - main.py 側ではPUSH DB保存なしの方針を維持する。
#   - final_entry_safety_guard_patch が先に ENTRY_ALLOW_ENTRY_WITHOUT_BOARD=0 を
#     setdefault 済みでも、main runtime では保護fail-openを強制有効化する。
#   - SUMMARY_AI は row / item / entry / ai / entry_row のどこにscoreや流動性があっても拾う。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V1.4-SUMMARY-AI-BOARD-MISSING-RESCUE"
_INSTALLED = False

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
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


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _norm(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _row_to_dict_any(v: Any) -> dict[str, Any]:
    try:
        if isinstance(v, dict):
            return v
        if hasattr(v, "to_dict"):
            d = v.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _first_float(dicts: list[dict[str, Any]], keys: tuple[str, ...], default: float = 0.0) -> float:
    for d in dicts:
        if not isinstance(d, dict):
            continue
        for k in keys:
            try:
                if k in d:
                    val = d.get(k)
                    if val is not None and str(val).strip() != "":
                        f = _safe_float(val, default)
                        if f != 0 or default == 0.0:
                            return f
            except Exception:
                pass
    return float(default)


def _first_str(dicts: list[dict[str, Any]], keys: tuple[str, ...]) -> str:
    for d in dicts:
        if not isinstance(d, dict):
            continue
        for k in keys:
            try:
                val = d.get(k)
                if val is not None and str(val).strip() != "":
                    return str(val).strip()
            except Exception:
                pass
    return ""


def _candidate_dicts(row: Any, item: Any) -> list[dict[str, Any]]:
    row_d = _row_to_dict_any(row)
    item_d = item if isinstance(item, dict) else {}
    out: list[dict[str, Any]] = []
    for d in (
        row_d,
        item_d,
        _row_to_dict_any(item_d.get("entry")),
        _row_to_dict_any(item_d.get("entry_row")),
        _row_to_dict_any(item_d.get("row")),
        _row_to_dict_any(item_d.get("ai")),
    ):
        if isinstance(d, dict) and d not in out:
            out.append(d)
    return out


def _is_summary_ai_candidate(dicts: list[dict[str, Any]]) -> bool:
    source = _norm(_first_str(dicts, ("source", "pipeline_source", "src")))
    entry_type = _norm(_first_str(dicts, ("entry_type", "type", "strategy")))
    model = _norm(_first_str(dicts, ("model", "model_used")))
    reason = _norm(_first_str(dicts, ("reason", "ai_reason")))
    if "SUMMARY" in source or "SUMMARY" in entry_type or "SUMMARY" in reason:
        return True
    if entry_type in {"SUMMARY_AI", "AI_SUMMARY"}:
        return True
    if source in {"SUMMARY", "SUMMARY_AI", "PUSH", "AI"} and ("MTF" in model or "SUMMARY" in entry_type):
        return True
    return False


def _score_from_dicts(dicts: list[dict[str, Any]], side: str) -> float:
    side_s = _norm(side)
    if side_s == "BUY":
        preferred = ("score_buy", "buy_score", "ai_buy_score", "confidence", "priority")
    elif side_s == "SELL":
        preferred = ("score_sell", "sell_score", "ai_sell_score", "confidence", "priority")
    else:
        preferred = ("score", "score_total", "final_score", "display_score", "priority", "confidence")
    v = _first_float(dicts, preferred, 0.0)
    if v:
        return abs(v)
    return abs(_first_float(dicts, ("score", "score_total", "final_score", "display_score", "combined_score", "priority", "confidence"), 0.0))


def _summary_ai_without_board_ok(fsg: Any, row: Any, item: Any, symbol: str, side: str) -> bool:
    if not _env_bool("ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD", True):
        return False

    dicts = _candidate_dicts(row, item)
    if not _is_summary_ai_candidate(dicts):
        return False

    close = _first_float(dicts, ("close", "close_price", "price", "current_price", "CurrentPrice"), 0.0)
    volume = _first_float(dicts, ("volume", "Volume", "trading_volume", "TradingVolume", "出来高", "day_volume"), 0.0)
    turnover = _first_float(dicts, ("turnover", "trading_value", "TradingValue", "Turnover", "売買代金", "day_turnover"), 0.0)
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume
    score = _score_from_dicts(dicts, side)

    min_price = _env_float("ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD_MIN_PRICE", _env_float("ENTRY_ALLOW_WITHOUT_BOARD_MIN_PRICE", 200.0))
    min_volume = _env_float("ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD_MIN_VOLUME", _env_float("ENTRY_ALLOW_WITHOUT_BOARD_MIN_VOLUME", 30000.0))
    min_turnover = _env_float("ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD_MIN_TURNOVER", _env_float("ENTRY_ALLOW_WITHOUT_BOARD_MIN_TURNOVER", 10000000.0))
    min_score = _env_float("ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD_MIN_SCORE", 1.0)

    # SCOREが強いSUMMARY_AIは、PUSH rowでvolume列が欠けても売買代金が取れていれば救済する。
    # ただし価格・売買代金・scoreは必須。
    volume_ok = volume >= min_volume or (turnover >= min_turnover and _env_bool("ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD_ALLOW_VOLUME_MISSING", True))
    ok = close >= min_price and turnover >= min_turnover and score >= min_score and volume_ok
    if not ok:
        logger.warning(
            "[FINAL ENTRY SAFETY GUARD] SUMMARY_AI_BOARD_MISSING_RESCUE_NG symbol=%s side=%s close=%.2f volume=%.0f turnover=%.0f score=%.3f min_price=%.2f min_volume=%.0f min_turnover=%.0f min_score=%.3f source=%s entry_type=%s",
            symbol,
            side,
            close,
            volume,
            turnover,
            score,
            min_price,
            min_volume,
            min_turnover,
            min_score,
            _first_str(dicts, ("source", "pipeline_source")),
            _first_str(dicts, ("entry_type",)),
        )
        return False

    try:
        item_d = item if isinstance(item, dict) else {}
        ai = item_d.get("ai")
        if isinstance(ai, dict):
            old_lot = _safe_float(ai.get("lot_multiplier"), 1.0) or 1.0
            ratio = max(0.1, min(1.0, _env_float("ENTRY_SUMMARY_AI_BOARD_MISSING_QTY_RATIO", _env_float("ENTRY_BOARD_MISSING_QTY_RATIO", 0.35))))
            ai["lot_multiplier"] = max(0.1, old_lot * ratio)
            ai["board_missing_qty_ratio"] = ratio
            ai["board_missing_fallback"] = True
            ai["summary_ai_board_missing_rescue"] = True
        entry = item_d.get("entry")
        if isinstance(entry, dict):
            entry["board_missing_fallback"] = True
            entry["summary_ai_board_missing_rescue"] = True
    except Exception:
        pass

    logger.warning(
        "[FINAL ENTRY SAFETY GUARD] SUMMARY_AI_BOARD_MISSING_RESCUE_ALLOW symbol=%s side=%s close=%.2f volume=%.0f turnover=%.0f score=%.3f qty_ratio=%s version=%s",
        symbol,
        side,
        close,
        volume,
        turnover,
        score,
        os.getenv("ENTRY_SUMMARY_AI_BOARD_MISSING_QTY_RATIO", os.getenv("ENTRY_BOARD_MISSING_QTY_RATIO", "0.35")),
        VERSION,
    )
    return True


def _patch_summary_ai_board_missing_fallback(fsg: Any) -> bool:
    old = getattr(fsg, "_board_missing_fallback_ok", None)
    if getattr(old, "_summary_ai_board_missing_rescue_v14", False):
        return True

    def _board_missing_fallback_ok_v14(row: dict, item: dict, symbol: str, side: str) -> bool:
        try:
            if _summary_ai_without_board_ok(fsg, row, item, symbol, side):
                return True
        except Exception:
            logger.debug("[BOARD MISSING FAILOPEN] summary_ai board rescue check failed", exc_info=True)
        if callable(old):
            return bool(old(row, item, symbol, side))
        return False

    _board_missing_fallback_ok_v14._summary_ai_board_missing_rescue_v14 = True  # type: ignore[attr-defined]
    _board_missing_fallback_ok_v14._original = old  # type: ignore[attr-defined]
    fsg._board_missing_fallback_ok = _board_missing_fallback_ok_v14
    logger.warning("[BOARD MISSING FAILOPEN] summary_ai board missing fallback patched version=%s", VERSION)
    return True


def _patch_final_guard() -> bool:
    try:
        import core.startup.final_entry_safety_guard_patch as fsg
    except Exception:
        logger.exception("[BOARD MISSING FAILOPEN] import final_entry_safety_guard_patch failed")
        return False

    _patch_summary_ai_board_missing_fallback(fsg)

    if getattr(fsg, "_BOARD_MISSING_FAILOPEN_PATCHED_V14", False):
        return True

    old_board_guard = getattr(fsg, "_board_guard", None)
    old_patched_board_guard = getattr(fsg, "_patched_board_guard", None)

    def _board_guard_failopen(row: Any, item: Any = None, symbol: Any = None, side: Any = None, *args: Any, **kwargs: Any) -> bool:
        # 3引数互換: guard(row, symbol, side)
        if side is None and symbol is not None and not isinstance(item, dict):
            side = symbol
            symbol = item
            item = None

        row_d = fsg._row_to_dict(row)
        item_d = item if isinstance(item, dict) else {}
        symbol_s = fsg._norm_symbol(symbol or fsg._first(row_d, ("symbol", "Symbol", "code", "銘柄コード"), ""))
        side_s = fsg._norm_side(side or fsg._first(row_d, ("side", "entry_decision", "ai_side"), ""))

        if not fsg._env_bool("ENTRY_BOARD_GUARD_ENABLED", True):
            return True

        bid, ask, bid_qty, ask_qty = fsg._extract_bid_ask_from_row(row_d)
        if bid <= 0 or ask <= 0:
            try:
                bid2, ask2, bidq2, askq2 = fsg._try_get_bid_ask_from_api(symbol_s, side_s, "final_entry_safety_guard")
            except TypeError:
                try:
                    bid2, ask2, bidq2, askq2 = fsg._try_get_bid_ask_from_api(symbol_s)
                except Exception:
                    bid2 = ask2 = bidq2 = askq2 = 0.0
            except Exception:
                bid2 = ask2 = bidq2 = askq2 = 0.0
            bid = bid or bid2
            ask = ask or ask2
            bid_qty = bid_qty or bidq2
            ask_qty = ask_qty or askq2

        if bid <= 0 or ask <= 0:
            if _env_bool("ENTRY_BOARD_MISSING_HARD_BLOCK", False):
                fsg._log_ng("board_missing", symbol_s, side_s, bid=bid, ask=ask, message="板が取れないため新規エントリー停止")
                logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_MISSING_HARD_BLOCK symbol=%s side=%s bid=%s ask=%s", symbol_s, side_s, bid, ask)
                return False

            if fsg._env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", True):
                ok = False
                try:
                    ok = bool(fsg._board_missing_fallback_ok(row_d, item_d, symbol_s, side_s))
                except Exception:
                    logger.debug("[BOARD MISSING FAILOPEN] protected fallback check failed symbol=%s side=%s", symbol_s, side_s, exc_info=True)
                    ok = False

                if ok:
                    logger.warning(
                        "[FINAL ENTRY SAFETY GUARD] BOARD_MISSING_PROTECTED_FAILOPEN symbol=%s side=%s bid=%s ask=%s version=%s",
                        symbol_s,
                        side_s,
                        bid,
                        ask,
                        VERSION,
                    )
                    return True

            fsg._log_ng("board_missing", symbol_s, side_s, bid=bid, ask=ask, message="板が取れず、保護条件も未達のため新規エントリー停止")
            return False

        mid = (bid + ask) / 2.0
        spread_pct = ((ask - bid) / mid) * 100.0 if mid > 0 else 999.0
        max_spread = fsg._env_float("ENTRY_MAX_SPREAD_PCT", 0.15)
        min_best_qty = fsg._env_float("ENTRY_MIN_BEST_BOARD_QTY", 100.0)
        if spread_pct > max_spread:
            fsg._log_ng("spread_too_wide", symbol_s, side_s, bid=bid, ask=ask, spread_pct=spread_pct, max_spread=max_spread)
            return False
        if side_s == "BUY" and ask_qty > 0 and ask_qty < min_best_qty:
            fsg._log_ng("ask_board_too_thin", symbol_s, side_s, ask_qty=ask_qty, min_best_qty=min_best_qty, bid=bid, ask=ask)
            return False
        if side_s == "SELL" and bid_qty > 0 and bid_qty < min_best_qty:
            fsg._log_ng("bid_board_too_thin", symbol_s, side_s, bid_qty=bid_qty, min_best_qty=min_best_qty, bid=bid, ask=ask)
            return False
        logger.info(
            "[FINAL ENTRY SAFETY GUARD] BOARD_OK symbol=%s side=%s bid=%.4f ask=%.4f spread_pct=%.4f bid_qty=%.0f ask_qty=%.0f",
            symbol_s,
            side_s,
            bid,
            ask,
            spread_pct,
            bid_qty,
            ask_qty,
        )
        return True

    _board_guard_failopen._board_missing_failopen_v1 = True  # type: ignore[attr-defined]
    _board_guard_failopen._board_missing_failopen_v14 = True  # type: ignore[attr-defined]
    _board_guard_failopen._original_board_guard = old_board_guard  # type: ignore[attr-defined]
    _board_guard_failopen._original_patched_board_guard = old_patched_board_guard  # type: ignore[attr-defined]
    fsg._board_guard = _board_guard_failopen
    fsg._patched_board_guard = _board_guard_failopen
    fsg._BOARD_MISSING_FAILOPEN_PATCHED_V1 = True
    fsg._BOARD_MISSING_FAILOPEN_PATCHED_V14 = True
    logger.warning("[BOARD MISSING FAILOPEN] final_entry_safety_guard board guard patched version=%s", VERSION)
    return True


def _install_summary_ai_lock_retry() -> bool:
    try:
        from core.startup import summary_ai_lock_retry_runtime_patch as lr
        fn = getattr(lr, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[BOARD MISSING FAILOPEN] summary_ai_lock_retry installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[BOARD MISSING FAILOPEN] summary_ai_lock_retry install failed")
        return False


def _install_summary_entry_stale_rescue() -> bool:
    try:
        from core.startup import summary_entry_stale_pending_rescue_patch as sr
        fn = getattr(sr, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[BOARD MISSING FAILOPEN] summary_entry_stale_rescue installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[BOARD MISSING FAILOPEN] summary_entry_stale_rescue install failed")
        return False


def install() -> bool:
    global _INSTALLED
    # final_entry_safety_guard_patch が先に ENTRY_ALLOW_ENTRY_WITHOUT_BOARD=0 を setdefault しているため、
    # ここは setdefault ではなく明示代入にする。強制ブロックしたい場合だけ *_FORCE で止める。
    os.environ["ENTRY_BOARD_MISSING_HARD_BLOCK"] = os.getenv("ENTRY_BOARD_MISSING_HARD_BLOCK_FORCE", "0")
    os.environ["ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"] = os.getenv("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD_FORCE", "1")
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_VOLUME", "30000")
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_TURNOVER", "10000000")
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_PRICE", "200")
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_SCORE", "0.90")
    os.environ.setdefault("ENTRY_BOARD_MISSING_QTY_RATIO", "0.50")
    os.environ.setdefault("ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD", "1")
    os.environ.setdefault("ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD_MIN_SCORE", "1.00")
    os.environ.setdefault("ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD_MIN_TURNOVER", "10000000")
    os.environ.setdefault("ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD_MIN_VOLUME", "30000")
    os.environ.setdefault("ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD_ALLOW_VOLUME_MISSING", "1")
    os.environ.setdefault("ENTRY_SUMMARY_AI_BOARD_MISSING_QTY_RATIO", "0.35")
    os.environ.setdefault("ENTRY_FINAL_BOARD_RETRY_COUNT", "0")
    os.environ.setdefault("ENTRY_FINAL_BOARD_RETRY_EXTRA_COUNT", "1")
    os.environ.setdefault("ENTRY_FINAL_BOARD_RETRY_EXTRA_WAIT_SEC", "0.2")

    board_ok = _patch_final_guard()
    lock_retry_ok = _install_summary_ai_lock_retry()
    stale_rescue_ok = _install_summary_entry_stale_rescue()
    ok = bool(board_ok or lock_retry_ok or stale_rescue_ok)
    _INSTALLED = bool(ok)
    logger.warning(
        "[BOARD MISSING FAILOPEN] installed=%s board_ok=%s lock_retry_ok=%s stale_rescue_ok=%s hard_block=%s allow_without_board=%s summary_ai_allow=%s qty_ratio=%s summary_ai_qty_ratio=%s version=%s",
        ok,
        board_ok,
        lock_retry_ok,
        stale_rescue_ok,
        os.getenv("ENTRY_BOARD_MISSING_HARD_BLOCK"),
        os.getenv("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"),
        os.getenv("ENTRY_SUMMARY_AI_ALLOW_WITHOUT_BOARD"),
        os.getenv("ENTRY_BOARD_MISSING_QTY_RATIO"),
        os.getenv("ENTRY_SUMMARY_AI_BOARD_MISSING_QTY_RATIO"),
        VERSION,
    )
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[BOARD MISSING FAILOPEN] auto install failed")


__all__ = ["VERSION", "install"]
