# ============================================================
# File   : core/startup/board_missing_protected_allow_patch.py
# Version: V1-ALLOW-PROTECTED-BOARD-MISSING
# ------------------------------------------------------------
# Purpose:
#   board_retry_patch V1.4 が ENTRY_BOARD_MISSING_HARD_BLOCK=1 を既定にし、
#   final_entry_safety_guard_patch V04 の board_missing fallback を後段で潰す問題を解消する。
#
# 方針:
#   - board_retry_patch の後段で final_entry_safety_guard_patch._board_guard を再上書きする。
#   - 板 bid/ask が取れない場合でも、価格/出来高/売買代金/score が最低条件を満たすなら許可。
#   - 数量は ENTRY_BOARD_MISSING_QTY_RATIO で小ロット化する。
#   - 流動性/同一銘柄損失/時間/直近逆行ガードは final_entry_safety_guard 側で先に通過済み。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _force_env() -> None:
    os.environ["ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"] = "1"
    os.environ["ENTRY_BOARD_MISSING_HARD_BLOCK"] = "0"
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_VOLUME", "30000")
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_TURNOVER", "10000000")
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_PRICE", "200")
    os.environ.setdefault("ENTRY_ALLOW_WITHOUT_BOARD_MIN_SCORE", "0.90")
    os.environ.setdefault("ENTRY_BOARD_MISSING_QTY_RATIO", "0.50")


def _first(row: dict, keys: tuple[str, ...], default=None):
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _fallback_ok(fsg: Any, row: dict, symbol: str, side: str) -> bool:
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
            "[BOARD MISSING PROTECTED ALLOW] NG symbol=%s side=%s close=%.2f volume=%.0f turnover=%.0f score=%.3f limits price>=%.1f volume>=%.0f turnover>=%.0f score>=%.2f",
            symbol, side, close, volume, turnover, score, min_price, min_volume, min_turnover, min_score,
        )
        return False

    logger.warning(
        "[BOARD MISSING PROTECTED ALLOW] OK symbol=%s side=%s close=%.2f volume=%.0f turnover=%.0f score=%.3f qty_ratio=%s",
        symbol, side, close, volume, turnover, score, os.getenv("ENTRY_BOARD_MISSING_QTY_RATIO"),
    )
    return True


def install() -> bool:
    global _INSTALLED
    _force_env()
    try:
        import core.startup.final_entry_safety_guard_patch as fsg
    except Exception:
        logger.debug("[BOARD MISSING PROTECTED ALLOW] final_entry_safety_guard not ready", exc_info=True)
        return False

    try:
        cur = getattr(fsg, "_board_guard", None)
        if getattr(cur, "_board_missing_protected_allow_v1", False):
            _INSTALLED = True
            return True

        def _board_guard_allow(row: dict, symbol: str, side: str) -> bool:
            if not fsg._env_bool("ENTRY_BOARD_GUARD_ENABLED", True):
                return True
            bid, ask, bid_qty, ask_qty = fsg._extract_bid_ask_from_row(row)
            if bid <= 0 or ask <= 0:
                try:
                    bid2, ask2, bidq2, askq2 = fsg._try_get_bid_ask_from_api(symbol)
                except TypeError:
                    bid2, ask2, bidq2, askq2 = fsg._try_get_bid_ask_from_api(symbol, side=side, source="board_missing_protected_allow")
                bid = bid or bid2
                ask = ask or ask2
                bid_qty = bid_qty or bidq2
                ask_qty = ask_qty or askq2
            if bid <= 0 or ask <= 0:
                if _fallback_ok(fsg, row, symbol, side):
                    logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_MISSING_ALLOW_PROTECTED symbol=%s side=%s bid=%s ask=%s", symbol, side, bid, ask)
                    return True
                fsg._log_ng("board_missing", symbol, side, bid=bid, ask=ask, message="板が取れないため新規エントリー停止")
                return False
            mid = (bid + ask) / 2.0
            spread_pct = ((ask - bid) / mid) * 100.0 if mid > 0 else 999.0
            max_spread = fsg._env_float("ENTRY_MAX_SPREAD_PCT", 0.15)
            min_best_qty = fsg._env_float("ENTRY_MIN_BEST_BOARD_QTY", 100.0)
            if spread_pct > max_spread:
                fsg._log_ng("spread_too_wide", symbol, side, bid=bid, ask=ask, spread_pct=spread_pct, max_spread=max_spread)
                return False
            if side == "BUY" and ask_qty > 0 and ask_qty < min_best_qty:
                fsg._log_ng("ask_board_too_thin", symbol, side, ask_qty=ask_qty, min_best_qty=min_best_qty, bid=bid, ask=ask)
                return False
            if side == "SELL" and bid_qty > 0 and bid_qty < min_best_qty:
                fsg._log_ng("bid_board_too_thin", symbol, side, bid_qty=bid_qty, min_best_qty=min_best_qty, bid=bid, ask=ask)
                return False
            logger.info(
                "[FINAL ENTRY SAFETY GUARD] BOARD_OK symbol=%s side=%s bid=%.4f ask=%.4f spread_pct=%.4f bid_qty=%.0f ask_qty=%.0f",
                symbol, side, bid, ask, spread_pct, bid_qty, ask_qty,
            )
            return True

        _board_guard_allow._board_missing_protected_allow_v1 = True  # type: ignore[attr-defined]
        _board_guard_allow._original = cur  # type: ignore[attr-defined]
        fsg._board_guard = _board_guard_allow
        _INSTALLED = True
        logger.warning(
            "[BOARD MISSING PROTECTED ALLOW] installed v1 allow_without_board=%s hard_block=%s qty_ratio=%s",
            os.getenv("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"),
            os.getenv("ENTRY_BOARD_MISSING_HARD_BLOCK"),
            os.getenv("ENTRY_BOARD_MISSING_QTY_RATIO"),
        )
        return True
    except Exception:
        logger.exception("[BOARD MISSING PROTECTED ALLOW] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[BOARD MISSING PROTECTED ALLOW] auto install failed")


__all__ = ["install"]
