# ============================================================
# File   : core/startup/board_retry_patch.py
# Version: V1.7-QUIET-COOLDOWN-NO-REST-STORM
# ------------------------------------------------------------
# 板取得リトライを軽量化する。
# 429/API実行回数エラー・レジスト数エラー発生時は cooldown し、
# final_entry_safety_guard から候補ごとにRESTを連打しない。
# ============================================================
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)
_PATCHED = False
_SIDE_PATCHED = False
_MA5_OPENING_PATCHED = False
_DAILY_DUP_PATCHED = False
_REST_COOLDOWN_UNTIL = 0.0
_BOARD_CACHE: dict[str, tuple[float, Any]] = {}


def _env_bool(name: str, default: bool = True) -> bool:
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


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    if s.endswith(".T"):
        s = s[:-2]
    return s


def _unwrap_original(fn):
    seen: set[int] = set()
    cur = fn
    while callable(cur) and id(cur) not in seen:
        seen.add(id(cur))
        nxt = getattr(cur, "_original", None)
        if not callable(nxt) or nxt is cur:
            break
        cur = nxt
    return cur


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _extract_bid_ask(board: Any) -> tuple[float, float, float, float]:
    if not isinstance(board, dict):
        return 0.0, 0.0, 0.0, 0.0
    buy1 = board.get("Buy1") if isinstance(board.get("Buy1"), dict) else {}
    sell1 = board.get("Sell1") if isinstance(board.get("Sell1"), dict) else {}
    bid = _safe_float(board.get("bid_price") or board.get("bid") or board.get("best_bid") or board.get("BidPrice") or board.get("BestBid") or buy1.get("Price"), 0.0)
    ask = _safe_float(board.get("ask_price") or board.get("ask") or board.get("best_ask") or board.get("AskPrice") or board.get("BestAsk") or sell1.get("Price"), 0.0)
    bid_qty = _safe_float(board.get("bid_qty") or board.get("BidQty") or board.get("bid_volume") or board.get("BestBidQty") or buy1.get("Qty"), 0.0)
    ask_qty = _safe_float(board.get("ask_qty") or board.get("AskQty") or board.get("ask_volume") or board.get("BestAskQty") or sell1.get("Qty"), 0.0)
    return bid, ask, bid_qty, ask_qty


def _is_valid_board(board: Any) -> bool:
    bid, ask, _, _ = _extract_bid_ask(board)
    return bid > 0 and ask > 0


def _board_dict(symbol: str, bid: float, ask: float, bid_qty: float = 0.0, ask_qty: float = 0.0, source: str = "") -> dict[str, Any]:
    return {"symbol": _norm_symbol(symbol), "bid_price": bid, "ask_price": ask, "bid": bid, "ask": ask, "best_bid": bid, "best_ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty, "source": source or "board_retry"}


def _set_rest_cooldown(reason: str) -> None:
    global _REST_COOLDOWN_UNTIL
    sec = max(10.0, _env_float("ENTRY_BOARD_REST_ERROR_COOLDOWN_SEC", 60.0))
    _REST_COOLDOWN_UNTIL = time.time() + sec
    logger.warning("[BOARD RETRY REST] COOLDOWN_SET reason=%s sec=%.1f until=%.1f", reason, sec, _REST_COOLDOWN_UNTIL)


def _get_token() -> str:
    try:
        import token_manager
        token = token_manager.get_valid_token()
        if token:
            return str(token).strip()
    except Exception:
        logger.debug("[BOARD RETRY REST] token_manager.get_valid_token failed", exc_info=True)
    for key in ("KABU_API_TOKEN", "KABUSAPI_TOKEN", "AUKABU_TOKEN", "API_TOKEN", "TOKEN", "KABU_API_KEY", "X_API_KEY"):
        val = os.getenv(key)
        if val:
            return str(val).strip()
    return ""


def _fetch_board_rest(symbol: str, side: str = "", source: str = "") -> dict[str, Any] | None:
    if not _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", False):
        return None
    now = time.time()
    if now < _REST_COOLDOWN_UNTIL:
        logger.warning("[BOARD RETRY REST] COOLDOWN_SKIP symbol=%s side=%s source=%s remaining=%.1fs", symbol, side, source, _REST_COOLDOWN_UNTIL - now)
        return None
    sym = _norm_symbol(symbol)
    if not sym:
        return None
    cached = _BOARD_CACHE.get(sym)
    ttl = max(0.1, _env_float("ENTRY_BOARD_REST_CACHE_TTL_SEC", 2.0))
    if cached and now - cached[0] <= ttl:
        return cached[1]
    token = _get_token()
    if not token:
        return None
    try:
        import requests  # type: ignore
    except Exception:
        return None
    timeout = max(0.3, _env_float("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC", 1.0))
    exchanges = [x.strip() for x in str(os.getenv("ENTRY_BOARD_REST_EXCHANGES", "1")).split(",") if x.strip()] or ["1"]
    for ex in exchanges[:1]:
        url = f"http://localhost:18080/kabusapi/board/{sym}@{ex}"
        try:
            res = requests.get(url, headers={"X-API-KEY": token}, timeout=timeout)
            status = getattr(res, "status_code", None)
            if status != 200:
                logger.warning("[BOARD RETRY REST] REST_NG symbol=%s side=%s source=%s exchange=%s status=%s", sym, side, source, ex, status)
                if status in (400, 429):
                    _set_rest_cooldown(f"status_{status}")
                return None
            data = res.json()
            bid, ask, bid_qty, ask_qty = _extract_bid_ask(data)
            if bid > 0 and ask > 0:
                board = _board_dict(sym, bid, ask, bid_qty, ask_qty, source="rest_board")
                _BOARD_CACHE[sym] = (now, board)
                return board
        except Exception as exc:
            msg = repr(exc)
            logger.warning("[BOARD RETRY REST] REST_ERROR symbol=%s side=%s source=%s exchange=%s error=%s", sym, side, source, ex, msg)
            if "429" in msg or "4001006" in msg or "4002006" in msg or "API実行回数" in msg or "レジスト数" in msg:
                _set_rest_cooldown("exception_rate_or_register")
            return None
    return None


def _retry_fetch_board(original, symbol: Any, *args, source: str = "", side: str = "", **kwargs):
    sym = _norm_symbol(symbol)
    try:
        board = original(symbol, *args, **kwargs)
    except TypeError:
        board = original(symbol)
    if _is_valid_board(board):
        return board
    if not _env_bool("ENTRY_BOARD_RETRY_ENABLED", True):
        return _fetch_board_rest(sym, side=side, source=source) or board
    retry_count = max(0, min(_env_int("ENTRY_BOARD_RETRY_COUNT", 1), 1))
    wait_sec = max(0.0, min(_env_float("ENTRY_BOARD_RETRY_WAIT_SEC", 0.2), 0.5))
    last_board = board
    for i in range(1, retry_count + 1):
        if wait_sec <= 0:
            break
        logger.warning("[BOARD RETRY] board missing symbol=%s side=%s source=%s retry=%s/%s wait=%.2fs", sym, side, source, i, retry_count, wait_sec)
        time.sleep(wait_sec)
        try:
            last_board = original(symbol, *args, **kwargs)
        except TypeError:
            last_board = original(symbol)
        except Exception:
            logger.debug("[BOARD RETRY] retry failed symbol=%s retry=%s", sym, i, exc_info=True)
            continue
        if _is_valid_board(last_board):
            return last_board
    rest_board = _fetch_board_rest(sym, side=side, source=source)
    if _is_valid_board(rest_board):
        return rest_board
    logger.warning("[BOARD RETRY] board still missing symbol=%s side=%s source=%s after retries=%s rest_direct=%s", sym, side, source, retry_count, _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", False))
    return last_board


def _wrap_get_latest_bid_ask(original):
    original = _unwrap_original(original)
    if getattr(original, "_board_retry_v17", False):
        return original

    def _get_latest_bid_ask_retry(symbol: Any, *args, **kwargs):
        return _retry_fetch_board(original, symbol, *args, **kwargs)

    _get_latest_bid_ask_retry._board_retry_v17 = True  # type: ignore[attr-defined]
    _get_latest_bid_ask_retry._board_retry_v16 = True  # type: ignore[attr-defined]
    _get_latest_bid_ask_retry._original = original  # type: ignore[attr-defined]
    return _get_latest_bid_ask_retry


def _install_final_safety_side_aware_board() -> bool:
    global _SIDE_PATCHED
    if _SIDE_PATCHED:
        return True
    try:
        import core.startup.final_entry_safety_guard_patch as fsg

        def _try_get_bid_ask_from_api_side(symbol: str, side: str = "", source: str = "final_entry_safety_guard"):
            try:
                from utils_common import get_latest_bid_ask
                try:
                    res = get_latest_bid_ask(symbol, source=source, side=side)
                except TypeError:
                    res = get_latest_bid_ask(symbol)
                bid, ask, bid_qty, ask_qty = _extract_bid_ask(res)
                if bid > 0 and ask > 0:
                    return bid, ask, bid_qty, ask_qty
            except Exception:
                logger.debug("[BOARD RETRY] final guard get_latest_bid_ask failed symbol=%s side=%s", symbol, side, exc_info=True)
            # REST direct is intentionally disabled by default; only env enables it.
            rest = _fetch_board_rest(symbol, side=side, source=source)
            return _extract_bid_ask(rest)

        fsg._try_get_bid_ask_from_api = _try_get_bid_ask_from_api_side
        _SIDE_PATCHED = True
        logger.warning("[BOARD RETRY] patched final_entry_safety_guard board fetch v17 rest_direct=%s cooldown_until=%.1f", _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", False), _REST_COOLDOWN_UNTIL)
        return True
    except Exception:
        logger.exception("[BOARD RETRY] final_entry_safety_guard side-aware board patch failed")
        return False


def _install_ma5_opening_relax() -> bool:
    global _MA5_OPENING_PATCHED
    _MA5_OPENING_PATCHED = True
    return True


def _install_daily_src_duplicate_cleanup() -> bool:
    global _DAILY_DUP_PATCHED
    _DAILY_DUP_PATCHED = True
    return True


def install() -> bool:
    global _PATCHED
    # Default: do not use REST /board from this patch unless explicitly enabled.
    os.environ.setdefault("ENTRY_BOARD_REST_DIRECT_ENABLED", "0")
    os.environ.setdefault("ENTRY_BOARD_RETRY_COUNT", "1")
    os.environ.setdefault("ENTRY_BOARD_RETRY_WAIT_SEC", "0.2")
    try:
        import utils_common
        old = getattr(utils_common, "get_latest_bid_ask", None)
        if callable(old) and not getattr(old, "_board_retry_v17", False):
            utils_common.get_latest_bid_ask = _wrap_get_latest_bid_ask(old)
            _PATCHED = True
            logger.warning("[BOARD RETRY] patched utils_common.get_latest_bid_ask v17")
    except Exception:
        logger.exception("[BOARD RETRY] patch utils_common.get_latest_bid_ask failed")
    _install_final_safety_side_aware_board()
    _install_ma5_opening_relax()
    _install_daily_src_duplicate_cleanup()
    logger.warning("[BOARD RETRY] installed v17 patched=%s rest_direct=%s", _PATCHED, _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", False))
    return True


try:
    install()
except Exception:
    logger.exception("[BOARD RETRY] auto install failed")

__all__ = ["install"]
