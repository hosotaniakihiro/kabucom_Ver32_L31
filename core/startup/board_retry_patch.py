# ============================================================
# File   : core/startup/board_retry_patch.py
# Version: V1.2-BOARD-RETRY-4P5SEC-PLUS-0P3S-EXTRA
# ------------------------------------------------------------
# 【目的】
#   A/B PUSHローテーション中、候補銘柄が反対面にいる瞬間だけ
#   bid/ask が取れず board_missing になる問題を緩和する。
#
# 【動作】
#   SUMMARY_AI 発注直前の板取得で、まず 4.5秒待って再取得する。
#   4.5秒直後が「登録解除〜次登録」の境目に当たった場合に備え、
#   0.3秒刻みで追加確認できるようにする。
#
# 【既定値】
#   ENTRY_BOARD_RETRY_ENABLED=1
#   ENTRY_BOARD_RETRY_WAIT_SEC=4.5
#   ENTRY_BOARD_RETRY_COUNT=1
#   ENTRY_BOARD_RETRY_EXTRA_WAIT_SEC=0.3
#   ENTRY_BOARD_RETRY_EXTRA_COUNT=2
#   ENTRY_BOARD_RETRY_SYMBOLS_ONLY_PENDING=0
#
# 【互換】
#   entry_order_builder 側に直接 _get_board_with_retry がある場合は、
#   その関数をこの patch 側の 4.5秒 + 0.3秒追加確認版へ差し替える。
#   これにより 5.6秒待ちや二重待ちを避ける。
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


def _unwrap_original(fn):
    try:
        while callable(getattr(fn, "_original", None)):
            fn = getattr(fn, "_original")
    except Exception:
        pass
    return fn


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


def _retry_fetch_board(original, symbol: Any, *args, source: str = "", side: str = "", **kwargs):
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
    extra_count = max(0, _env_int("ENTRY_BOARD_RETRY_EXTRA_COUNT", 2))
    extra_wait_sec = max(0.0, _env_float("ENTRY_BOARD_RETRY_EXTRA_WAIT_SEC", 0.3))

    last_board = board

    for i in range(1, retry_count + 1):
        if wait_sec <= 0:
            break
        logger.warning(
            "[BOARD RETRY] board missing symbol=%s side=%s source=%s retry=%s/%s wait=%.2fs reason=push_rotation_4p5s",
            sym,
            side,
            source,
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
            "[BOARD RETRY] board still missing symbol=%s side=%s source=%s extra_retry=%s/%s wait=%.2fs reason=rotation_boundary_possible",
            sym,
            side,
            source,
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


def _wrap_get_latest_bid_ask(original):
    original = _unwrap_original(original)
    if getattr(original, "_board_retry_v12", False):
        return original

    def _get_latest_bid_ask_retry(symbol: Any, *args, **kwargs):
        return _retry_fetch_board(original, symbol, *args, **kwargs)

    _get_latest_bid_ask_retry._board_retry_v12 = True  # type: ignore[attr-defined]
    _get_latest_bid_ask_retry._original = original  # type: ignore[attr-defined]
    return _get_latest_bid_ask_retry


def _make_entry_order_builder_retry(original_get_latest_bid_ask):
    original_get_latest_bid_ask = _unwrap_original(original_get_latest_bid_ask)

    def _get_board_with_retry(symbol: str, *, source: str, side: str):
        return _retry_fetch_board(
            original_get_latest_bid_ask,
            symbol,
            source=str(source or ""),
            side=str(side or ""),
        )

    _get_board_with_retry._board_retry_v12 = True  # type: ignore[attr-defined]
    _get_board_with_retry._original = original_get_latest_bid_ask  # type: ignore[attr-defined]
    return _get_board_with_retry


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        logger.warning("[BOARD RETRY] already installed")
        return True

    if not _env_bool("ENTRY_BOARD_RETRY_ENABLED", True):
        logger.warning("[BOARD RETRY] disabled by env")
        return False

    ok_any = False

    wait_sec = _env_float("ENTRY_BOARD_RETRY_WAIT_SEC", 4.5)
    extra_wait_sec = _env_float("ENTRY_BOARD_RETRY_EXTRA_WAIT_SEC", 0.3)
    extra_count = _env_int("ENTRY_BOARD_RETRY_EXTRA_COUNT", 2)

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
            # 直接実装側の定数も4.5秒/0.3秒へ寄せる。
            setattr(eob, "ENTRY_ORDER_BOARD_RETRY_ENABLED", True)
            setattr(eob, "ENTRY_ORDER_BOARD_RETRY_SEC", wait_sec)
            setattr(eob, "ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", extra_wait_sec)
            setattr(eob, "ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", _env_bool("ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", False))

            # entry_order_builder 側に _get_board_with_retry がある場合は、
            # 5.6秒/0.4秒版ではなく、このpatchの 4.5秒 + 0.3秒追加確認版へ差し替える。
            eob.get_latest_bid_ask = _unwrap_original(orig)
            eob._get_board_with_retry = _make_entry_order_builder_retry(eob.get_latest_bid_ask)
            ok_any = True
            logger.warning("[BOARD RETRY] patched entry_order_builder._get_board_with_retry wait=%.2fs extra_wait=%.2fs extra_count=%s", wait_sec, extra_wait_sec, extra_count)
    except Exception:
        logger.exception("[BOARD RETRY] patch entry_order_builder failed")

    _PATCHED = bool(ok_any)
    logger.warning(
        "[BOARD RETRY] installed=%s enabled=%s wait_sec=%.2f retry_count=%s extra_wait=%.2f extra_count=%s only_pending=%s",
        _PATCHED,
        _env_bool("ENTRY_BOARD_RETRY_ENABLED", True),
        wait_sec,
        _env_int("ENTRY_BOARD_RETRY_COUNT", 1),
        extra_wait_sec,
        extra_count,
        _env_bool("ENTRY_BOARD_RETRY_SYMBOLS_ONLY_PENDING", False),
    )
    return _PATCHED


try:
    install()
except Exception:
    logger.exception("[BOARD RETRY] auto install failed")

__all__ = ["install"]
