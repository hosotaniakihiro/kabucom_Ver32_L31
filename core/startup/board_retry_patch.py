# ============================================================
# File   : core/startup/board_retry_patch.py
# Version: V1.9-SUMMARY-AI-REST-COOLDOWN-SHORT
# ------------------------------------------------------------
# 板取得リトライを軽量化する。
# 429/API実行回数エラー・レジスト数エラー発生時は cooldown し、
# final_entry_safety_guard から候補ごとにRESTを連打しない。
#
# V1.9:
#   - SUMMARY_AI の PUSHローテーション補完REST確認では、全銘柄共通60秒
#     cooldownが長すぎて10.5秒リトライを無効化していた。
#   - SUMMARY_AI/ORDER_BUILDER系だけ cooldown を短縮し、各候補で最低限
#     REST /board を再確認できるようにする。
#   - cooldown中でもキャッシュに有効板があれば返す。
#   - 板なし発注はしない。RESTでも板が無ければ従来通り hard block。
# ============================================================
from __future__ import annotations

import inspect
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


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _summary_ai_source(source: Any) -> bool:
    s = str(source or "").strip().upper()
    return s in {"SUMMARY_AI", "SUMMARY", "PUSH", "PUSH_SUMMARY", "SUMMARY_AI_ORDER_BUILDER", "SUMMARY_AI_REST_BOARD_CHECK"} or "SUMMARY_AI" in s


def _infer_context_side_source(side: str = "", source: str = "") -> tuple[str, str]:
    side_s = _norm_side(side)
    source_s = str(source or "").strip() or "final_entry_safety_guard"
    if side_s and source_s:
        return side_s, source_s
    try:
        frame = inspect.currentframe()
        for _ in range(10):
            frame = frame.f_back if frame is not None else None
            if frame is None:
                break
            loc = frame.f_locals
            cand = _norm_side(loc.get("side") or loc.get("entry_side") or loc.get("ai_side") or loc.get("sd"))
            row = loc.get("row") or loc.get("row_d") or loc.get("entry_row")
            item = loc.get("item") or loc.get("item_d")
            if not cand and isinstance(row, dict):
                cand = _norm_side(row.get("side") or row.get("entry_decision") or row.get("ai_side"))
            if not cand and isinstance(item, dict):
                cand = _norm_side(item.get("side"))
                if not cand and isinstance(item.get("entry"), dict):
                    cand = _norm_side(item["entry"].get("side") or item["entry"].get("entry_decision"))
                if not cand and isinstance(item.get("ai"), dict):
                    cand = _norm_side(item["ai"].get("side") or item["ai"].get("entry_decision"))
            if cand:
                side_s = cand
            src = loc.get("source") or loc.get("entry_source") or loc.get("pipeline_source")
            if not src and isinstance(row, dict):
                src = row.get("source") or row.get("entry_source") or row.get("entry_type")
            if not src and isinstance(item, dict):
                src = item.get("source") or item.get("entry_source") or item.get("entry_type")
            if src:
                source_s = str(src)
            if side_s and source_s:
                break
    except Exception:
        pass
    return side_s, source_s or "final_entry_safety_guard"


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
    return {
        "symbol": _norm_symbol(symbol),
        "bid_price": bid,
        "ask_price": ask,
        "bid": bid,
        "ask": ask,
        "best_bid": bid,
        "best_ask": ask,
        "bid_qty": bid_qty,
        "ask_qty": ask_qty,
        "source": source or "board_retry",
    }


def _cooldown_sec_for_source(source: str) -> float:
    if _summary_ai_source(source):
        return max(1.0, _env_float("SUMMARY_AI_BOARD_REST_ERROR_COOLDOWN_SEC", 2.0))
    return max(10.0, _env_float("ENTRY_BOARD_REST_ERROR_COOLDOWN_SEC", 60.0))


def _set_rest_cooldown(reason: str, source: str = "") -> None:
    global _REST_COOLDOWN_UNTIL
    sec = _cooldown_sec_for_source(source)
    _REST_COOLDOWN_UNTIL = time.time() + sec
    logger.warning("[BOARD RETRY REST] COOLDOWN_SET reason=%s source=%s sec=%.1f until=%.1f", reason, source, sec, _REST_COOLDOWN_UNTIL)


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
    side, source = _infer_context_side_source(side, source)
    if not _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", False):
        return None
    now = time.time()
    sym = _norm_symbol(symbol)
    if not sym:
        return None
    cached = _BOARD_CACHE.get(sym)
    ttl = max(0.1, _env_float("ENTRY_BOARD_REST_CACHE_TTL_SEC", 2.0))
    if cached and now - cached[0] <= ttl and _is_valid_board(cached[1]):
        return cached[1]
    if now < _REST_COOLDOWN_UNTIL:
        remaining = _REST_COOLDOWN_UNTIL - now
        if _summary_ai_source(source) and remaining > _env_float("SUMMARY_AI_BOARD_REST_MAX_COOLDOWN_SKIP_SEC", 2.5):
            logger.warning("[BOARD RETRY REST] COOLDOWN_TRIM source=%s old_remaining=%.1fs new_remaining=0.0s symbol=%s side=%s", source, remaining, sym, side)
            globals()["_REST_COOLDOWN_UNTIL"] = 0.0
        else:
            logger.warning("[BOARD RETRY REST] COOLDOWN_SKIP symbol=%s side=%s source=%s remaining=%.1fs", sym, side, source, remaining)
            return None
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
                    _set_rest_cooldown(f"status_{status}", source)
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
                _set_rest_cooldown("exception_rate_or_register", source)
            return None
    return None


def _retry_fetch_board(original, symbol: Any, *args, source: str = "", side: str = "", **kwargs):
    side, source = _infer_context_side_source(side, source)
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
    if getattr(original, "_board_retry_v19", False):
        return original

    def _get_latest_bid_ask_retry(symbol: Any, *args, **kwargs):
        return _retry_fetch_board(original, symbol, *args, **kwargs)

    _get_latest_bid_ask_retry._board_retry_v19 = True  # type: ignore[attr-defined]
    _get_latest_bid_ask_retry._board_retry_v18 = True  # type: ignore[attr-defined]
    _get_latest_bid_ask_retry._board_retry_v17 = True  # type: ignore[attr-defined]
    _get_latest_bid_ask_retry._board_retry_v16 = True  # type: ignore[attr-defined]
    _get_latest_bid_ask_retry._original = original  # type: ignore[attr-defined]
    return _get_latest_bid_ask_retry


def _install_final_safety_side_aware_board() -> bool:
    global _SIDE_PATCHED
    if _SIDE_PATCHED:
        return True
    try:
        import core.startup.final_entry_safety_guard_patch as fg
        fn = getattr(fg, "_try_get_bid_ask_from_api", None)
        if callable(fn) and not getattr(fn, "_board_retry_side_v19", False):
            orig = _unwrap_original(fn)

            def wrapped(symbol: str, *args, **kwargs):
                side = kwargs.pop("side", "") if "side" in kwargs else ""
                source = kwargs.pop("source", "") if "source" in kwargs else ""
                side, source = _infer_context_side_source(side, source)
                try:
                    out = orig(symbol, *args, side=side, source=source, **kwargs)
                except TypeError:
                    try:
                        out = orig(symbol, side=side, source=source)
                    except TypeError:
                        out = orig(symbol)
                if _is_valid_board(out):
                    return out
                return _fetch_board_rest(symbol, side=side, source=source) or out

            wrapped._board_retry_side_v19 = True  # type: ignore[attr-defined]
            wrapped._board_retry_side_v18 = True  # type: ignore[attr-defined]
            wrapped._original = orig  # type: ignore[attr-defined]
            fg._try_get_bid_ask_from_api = wrapped
            _SIDE_PATCHED = True
            logger.warning("[BOARD RETRY] patched final_entry_safety_guard _try_get_bid_ask_from_api side-aware v19")
        return True
    except Exception:
        logger.exception("[BOARD RETRY] patch final_entry_safety_guard side-aware failed")
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
    os.environ.setdefault("ENTRY_BOARD_REST_DIRECT_ENABLED", "0")
    os.environ.setdefault("ENTRY_BOARD_RETRY_COUNT", "1")
    os.environ.setdefault("ENTRY_BOARD_RETRY_WAIT_SEC", "0.2")
    os.environ.setdefault("SUMMARY_AI_BOARD_REST_ERROR_COOLDOWN_SEC", "2.0")
    os.environ.setdefault("SUMMARY_AI_BOARD_REST_MAX_COOLDOWN_SKIP_SEC", "2.5")
    try:
        import utils_common
        old = getattr(utils_common, "get_latest_bid_ask", None)
        if callable(old) and not getattr(old, "_board_retry_v19", False):
            utils_common.get_latest_bid_ask = _wrap_get_latest_bid_ask(old)
            _PATCHED = True
            logger.warning("[BOARD RETRY] patched utils_common.get_latest_bid_ask v19")
    except Exception:
        logger.exception("[BOARD RETRY] patch utils_common.get_latest_bid_ask failed")
    _install_final_safety_side_aware_board()
    _install_ma5_opening_relax()
    _install_daily_src_duplicate_cleanup()
    logger.warning("[BOARD RETRY] installed v19 patched=%s rest_direct=%s summary_ai_cooldown=%s", _PATCHED, _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", False), os.getenv("SUMMARY_AI_BOARD_REST_ERROR_COOLDOWN_SEC"))
    return True


try:
    install()
except Exception:
    logger.exception("[BOARD RETRY] auto install failed")


__all__ = ["install", "_fetch_board_rest"]
