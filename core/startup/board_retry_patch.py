# ============================================================
# File   : core/startup/board_retry_patch.py
# Version: V1.1-BOARD-RETRY-4P5SEC-FOR-PUSH-ROTATION
# ------------------------------------------------------------
# 【目的】
#   A/B PUSHローテーション中、候補銘柄が反対面にいる瞬間だけ
#   bid/ask が取れず board_missing になる問題を緩和する。
#
# 【動作】
#   get_latest_bid_ask(symbol) が空なら、既定4.5秒待って再取得する。
#   現在のローテーションは「4.5秒登録 + 0.5秒インターバル」なので、
#   5.0秒待つとインターバル/切替境界に当たり、まだ板が取れない可能性が残る。
#
# 【環境変数】
#   ENTRY_BOARD_RETRY_ENABLED=1
#   ENTRY_BOARD_RETRY_WAIT_SEC=4.5
#   ENTRY_BOARD_RETRY_COUNT=1
#   ENTRY_BOARD_RETRY_EXTRA_WAIT_SEC=0.3
#   ENTRY_BOARD_RETRY_EXTRA_COUNT=1
#   ENTRY_BOARD_RETRY_SYMBOLS_ONLY_PENDING=0
# ============================================================

from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)
_PATCHED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
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


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        if s.endswith(".T"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _is_valid_board(board: Any) -> bool:
    try:
        if not isinstance(board, dict):
            return False
        bid = board.get("bid_price") or board.get("bid") or board.get("BidPrice")
        ask = board.get("ask_price") or board.get("ask") or board.get("AskPrice")
        return float(bid or 0) > 0 and float(ask or 0) > 0
    except Exception:
        return False


def _is_pending_or_candidate(symbol: str) -> bool:
    if not _env_bool("ENTRY_BOARD_RETRY_SYMBOLS_ONLY_PENDING", False):
        return True
    try:
        from global_state import global_data
        sym = _norm_symbol(symbol)
        pending = getattr(global_data, "pending_entries", {})
        if isinstance(pending, dict) and sym in {_norm_symbol(k) for k in pending.keys()}:
            return True
        for attr in ("recent_entry_symbols", "last_entry_candidates", "recent_ai_ok_symbols"):
            vals = getattr(global_data, attr, [])
            if isinstance(vals, dict):
                vals = vals.keys()
            if isinstance(vals, (list, tuple, set)) and sym in {_norm_symbol(x) for x in vals}:
                return True
    except Exception:
        pass
    return False


def _wrap_get_latest_bid_ask(original):
    if getattr(original, "_board_retry_v1", False) or getattr(original, "_board_retry_v11", False):
        return original

    def _get_latest_bid_ask_retry(symbol: Any, *args, **kwargs):
        board = original(symbol, *args, **kwargs)
        if _is_valid_board(board):
            return board

        if not _env_bool("ENTRY_BOARD_RETRY_ENABLED", True):
            return board

        sym = _norm_symbol(symbol)
        if not _is_pending_or_candidate(sym):
            return board

        retry_count = max(0, _env_int("ENTRY_BOARD_RETRY_COUNT", 1))
        wait_sec = max(0.0, _env_float("ENTRY_BOARD_RETRY_WAIT_SEC", 4.5))
        extra_count = max(0, _env_int("ENTRY_BOARD_RETRY_EXTRA_COUNT", 1))
        extra_wait_sec = max(0.0, _env_float("ENTRY_BOARD_RETRY_EXTRA_WAIT_SEC", 0.3))

        if retry_count <= 0 or wait_sec <= 0:
            return board

        last_board = board
        for i in range(1, retry_count + 1):
            logger.warning(
                "[BOARD RETRY] board missing symbol=%s retry=%s/%s wait=%.2fs reason=push_rotation_4p5s_gap_possible",
                sym,
                i,
                retry_count,
                wait_sec,
            )
            time.sleep(wait_sec)
            try:
                last_board = original(symbol, *args, **kwargs)
                if _is_valid_board(last_board):
                    logger.warning("[BOARD RETRY] board recovered symbol=%s retry=%s board=%s", sym, i, last_board)
                    return last_board
            except Exception:
                logger.debug("[BOARD RETRY] retry failed symbol=%s retry=%s", sym, i, exc_info=True)

        # 4.5秒直後が0.5秒インターバル/切替境界だった場合の短い追加確認
        for j in range(1, extra_count + 1):
            if extra_wait_sec <= 0:
                break
            logger.warning(
                "[BOARD RETRY] board still missing symbol=%s extra_retry=%s/%s wait=%.2fs reason=rotation_boundary_possible",
                sym,
                j,
                extra_count,
                extra_wait_sec,
            )
            time.sleep(extra_wait_sec)
            try:
                last_board = original(symbol, *args, **kwargs)
                if _is_valid_board(last_board):
                    logger.warning("[BOARD RETRY] board recovered on extra symbol=%s extra_retry=%s board=%s", sym, j, last_board)
                    return last_board
            except Exception:
                logger.debug("[BOARD RETRY] extra retry failed symbol=%s retry=%s", sym, j, exc_info=True)

        logger.warning("[BOARD RETRY] board still missing symbol=%s after retries=%s extra=%s", sym, retry_count, extra_count)
        return last_board

    _get_latest_bid_ask_retry._board_retry_v11 = True  # type: ignore[attr-defined]
    _get_latest_bid_ask_retry._original = original  # type: ignore[attr-defined]
    return _get_latest_bid_ask_retry


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    if not _env_bool("ENTRY_BOARD_RETRY_ENABLED", True):
        logger.warning("[BOARD RETRY] disabled by env")
        return False

    ok_any = False

    try:
        import utils_common
        orig = getattr(utils_common, "get_latest_bid_ask", None)
        if callable(orig):
            utils_common.get_latest_bid_ask = _wrap_get_latest_bid_ask(orig)
            ok_any = True
            logger.warning("[BOARD RETRY] patched utils_common.get_latest_bid_ask")
    except Exception:
        logger.exception("[BOARD RETRY] patch utils_common failed")

    try:
        import trading.handlers.entry_order_builder as eob
        orig = getattr(eob, "get_latest_bid_ask", None)
        if callable(orig):
            eob.get_latest_bid_ask = _wrap_get_latest_bid_ask(orig)
            ok_any = True
            logger.warning("[BOARD RETRY] patched entry_order_builder.get_latest_bid_ask")
    except Exception:
        logger.exception("[BOARD RETRY] patch entry_order_builder failed")

    _PATCHED = bool(ok_any)
    logger.warning(
        "[BOARD RETRY] installed=%s enabled=%s wait_sec=%.2f retry_count=%s extra_wait=%.2f extra_count=%s only_pending=%s",
        _PATCHED,
        _env_bool("ENTRY_BOARD_RETRY_ENABLED", True),
        _env_float("ENTRY_BOARD_RETRY_WAIT_SEC", 4.5),
        _env_int("ENTRY_BOARD_RETRY_COUNT", 1),
        _env_float("ENTRY_BOARD_RETRY_EXTRA_WAIT_SEC", 0.3),
        _env_int("ENTRY_BOARD_RETRY_EXTRA_COUNT", 1),
        _env_bool("ENTRY_BOARD_RETRY_SYMBOLS_ONLY_PENDING", False),
    )
    return _PATCHED


try:
    install()
except Exception:
    logger.exception("[BOARD RETRY] auto install failed")

__all__ = ["install"]
