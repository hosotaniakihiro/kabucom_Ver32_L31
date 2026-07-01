# ============================================================
# File   : core/startup/final_entry_board_rest_direct_patch.py
# Version: V2-FAILOPEN-AWARE-REST-THROTTLE
# ------------------------------------------------------------
# 目的:
#   SUMMARY_AI / TONOSAMA の発注直前に PUSH 板が無い場合の REST /board fallback。
#
# V2:
#   - ENTRY_ALLOW_ENTRY_WITHOUT_BOARD=1 のとき、final_entry_safety_guard では
#     REST /board を叩き続けず、0を返して board_missing_failopen_runtime_patch の
#     保護fail-openへ流す。
#   - 429(API実行回数エラー) / 4002006(レジスト数エラー) が出たら短時間 cooldown し、
#     同じエラーを候補ごとに連打しない。
#   - token refresh は行わず、settings.ini の token を token_manager.get_valid_token() から読む。
# ============================================================

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V2-FAILOPEN-AWARE-REST-THROTTLE"
_INSTALLED = False
_WATCHER_STARTED = False
_COOLDOWN_UNTIL = 0.0
_COOLDOWN_REASON = ""

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
        return float(str(raw).replace(",", ""))
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return int(default)
        return int(float(str(raw).replace(",", "")))
    except Exception:
        return int(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    if s.endswith(".T"):
        s = s[:-2]
    return s


def _extract_board_values(board: Any) -> tuple[float, float, float, float]:
    if not isinstance(board, dict):
        return 0.0, 0.0, 0.0, 0.0

    buy1 = board.get("Buy1") if isinstance(board.get("Buy1"), dict) else {}
    sell1 = board.get("Sell1") if isinstance(board.get("Sell1"), dict) else {}

    bid = _safe_float(
        board.get("bid")
        or board.get("best_bid")
        or board.get("BidPrice")
        or board.get("bid_price")
        or board.get("BestBid")
        or buy1.get("Price"),
        0.0,
    )
    ask = _safe_float(
        board.get("ask")
        or board.get("best_ask")
        or board.get("AskPrice")
        or board.get("ask_price")
        or board.get("BestAsk")
        or sell1.get("Price"),
        0.0,
    )
    bid_qty = _safe_float(
        board.get("bid_qty")
        or board.get("BidQty")
        or board.get("bid_volume")
        or board.get("BestBidQty")
        or buy1.get("Qty"),
        0.0,
    )
    ask_qty = _safe_float(
        board.get("ask_qty")
        or board.get("AskQty")
        or board.get("ask_volume")
        or board.get("BestAskQty")
        or sell1.get("Qty"),
        0.0,
    )
    if bid > 0 and ask > 0 and bid > ask:
        bid, ask = ask, bid
        bid_qty, ask_qty = ask_qty, bid_qty
    return bid, ask, bid_qty, ask_qty


def _get_token() -> str:
    try:
        import token_manager
        token = token_manager.get_valid_token()
        return str(token or "").strip()
    except Exception:
        logger.debug("[FINAL ENTRY BOARD REST DIRECT] token_manager.get_valid_token failed", exc_info=True)
    for key in ("KABU_API_TOKEN", "KABUSAPI_TOKEN", "AUKABU_TOKEN", "API_TOKEN", "TOKEN", "KABU_API_KEY", "X_API_KEY"):
        val = os.getenv(key)
        if val:
            return str(val).strip()
    return ""


def _cooldown_active() -> bool:
    try:
        return time.monotonic() < float(_COOLDOWN_UNTIL)
    except Exception:
        return False


def _set_cooldown(reason: str) -> None:
    global _COOLDOWN_UNTIL, _COOLDOWN_REASON
    sec = max(1.0, _env_float("ENTRY_BOARD_REST_DIRECT_ERROR_COOLDOWN_SEC", 20.0))
    _COOLDOWN_UNTIL = time.monotonic() + sec
    _COOLDOWN_REASON = str(reason or "error")
    logger.warning("[FINAL ENTRY BOARD REST DIRECT] cooldown start reason=%s sec=%.1f version=%s", _COOLDOWN_REASON, sec, VERSION)


def _failopen_should_skip_rest(source: str) -> bool:
    if not _env_bool("ENTRY_BOARD_REST_SKIP_WHEN_FAILOPEN", True):
        return False
    if not _env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", False):
        return False
    src = str(source or "").lower()
    return "final_entry_safety_guard" in src or src in {"", "summary", "summary_ai"}


def _call_board_rest(symbol: str) -> tuple[float, float, float, float]:
    if not _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", True):
        return 0.0, 0.0, 0.0, 0.0
    sym = _norm_symbol(symbol)
    if not sym:
        return 0.0, 0.0, 0.0, 0.0
    if _cooldown_active():
        logger.warning("[FINAL ENTRY BOARD REST DIRECT] cooldown skip symbol=%s reason=%s version=%s", sym, _COOLDOWN_REASON, VERSION)
        return 0.0, 0.0, 0.0, 0.0
    token = _get_token()
    if not token:
        logger.warning("[FINAL ENTRY BOARD REST DIRECT] TOKEN_MISSING symbol=%s", sym)
        return 0.0, 0.0, 0.0, 0.0

    timeout = max(0.3, _env_float("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC", 0.6))
    exchanges = [x.strip() for x in str(os.getenv("ENTRY_BOARD_REST_EXCHANGES", "1")).split(",") if x.strip()]
    if not exchanges:
        exchanges = ["1"]

    try:
        import requests  # type: ignore
    except Exception:
        logger.warning("[FINAL ENTRY BOARD REST DIRECT] requests import failed symbol=%s", sym)
        return 0.0, 0.0, 0.0, 0.0

    for ex in exchanges:
        url = f"http://localhost:18080/kabusapi/board/{sym}@{ex}"
        try:
            res = requests.get(url, headers={"X-API-KEY": token}, timeout=timeout)
            status = getattr(res, "status_code", None)
            if status != 200:
                text_head = str(getattr(res, "text", ""))[:180]
                logger.warning("[FINAL ENTRY BOARD REST DIRECT] REST_NG symbol=%s exchange=%s status=%s text=%s", sym, ex, status, text_head)
                if status in {400, 429}:
                    _set_cooldown(f"status_{status}")
                continue
            try:
                data = res.json()
            except Exception:
                logger.warning("[FINAL ENTRY BOARD REST DIRECT] REST_JSON_NG symbol=%s exchange=%s text_head=%s", sym, ex, str(getattr(res, 'text', ''))[:120])
                continue
            bid, ask, bid_qty, ask_qty = _extract_board_values(data)
            if bid > 0 and ask > 0:
                logger.warning(
                    "[FINAL ENTRY BOARD REST DIRECT] REST_BOARD_OK symbol=%s exchange=%s bid=%.4f ask=%.4f bid_qty=%.0f ask_qty=%.0f version=%s",
                    sym,
                    ex,
                    bid,
                    ask,
                    bid_qty,
                    ask_qty,
                    VERSION,
                )
                return bid, ask, bid_qty, ask_qty
            logger.warning("[FINAL ENTRY BOARD REST DIRECT] REST_BOARD_EMPTY symbol=%s exchange=%s keys=%s", sym, ex, sorted(list(data.keys()))[:20] if isinstance(data, dict) else type(data).__name__)
        except Exception as exc:
            logger.warning("[FINAL ENTRY BOARD REST DIRECT] REST_ERROR symbol=%s exchange=%s error=%r", sym, ex, exc)
            _set_cooldown("exception")
    return 0.0, 0.0, 0.0, 0.0


def _make_try_get_bid_ask_from_api():
    def _try_get_bid_ask_from_api(symbol: str, side: str = "", source: str = "final_entry_safety_guard") -> tuple[float, float, float, float]:
        try:
            from utils_common import get_latest_bid_ask
            try:
                res = get_latest_bid_ask(symbol, source=source, side=side)
            except TypeError:
                res = get_latest_bid_ask(symbol)
            bid, ask, bid_qty, ask_qty = _extract_board_values(res)
            if bid > 0 and ask > 0:
                logger.warning("[FINAL ENTRY BOARD REST DIRECT] PUSH_BOARD_OK symbol=%s bid=%.4f ask=%.4f version=%s", _norm_symbol(symbol), bid, ask, VERSION)
                return bid, ask, bid_qty, ask_qty
        except Exception:
            logger.debug("[FINAL ENTRY BOARD REST DIRECT] push board lookup failed symbol=%s", symbol, exc_info=True)

        if _failopen_should_skip_rest(source):
            logger.warning(
                "[FINAL ENTRY BOARD REST DIRECT] REST_SKIP_FAILOPEN symbol=%s side=%s source=%s allow_without_board=%s version=%s",
                _norm_symbol(symbol),
                side,
                source,
                os.getenv("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"),
                VERSION,
            )
            return 0.0, 0.0, 0.0, 0.0
        return _call_board_rest(symbol)

    _try_get_bid_ask_from_api._final_entry_board_rest_direct_v1 = True  # type: ignore[attr-defined]
    _try_get_bid_ask_from_api._final_entry_board_rest_direct_v2 = True  # type: ignore[attr-defined]
    return _try_get_bid_ask_from_api


def _install_once(log_patch: bool = True) -> bool:
    global _INSTALLED
    try:
        import core.startup.final_entry_safety_guard_patch as fsg
        fn = _make_try_get_bid_ask_from_api()
        fsg._try_get_bid_ask_from_api = fn
        _INSTALLED = True
        if log_patch:
            logger.warning(
                "[FINAL ENTRY BOARD REST DIRECT] installed v2 rest_direct=%s skip_when_failopen=%s exchanges=%s timeout=%.2fs cooldown_sec=%.1f allow_without_board=%s version=%s",
                _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", True),
                _env_bool("ENTRY_BOARD_REST_SKIP_WHEN_FAILOPEN", True),
                os.getenv("ENTRY_BOARD_REST_EXCHANGES", "1"),
                _env_float("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC", 0.6),
                _env_float("ENTRY_BOARD_REST_DIRECT_ERROR_COOLDOWN_SEC", 20.0),
                os.getenv("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"),
                VERSION,
            )
        return True
    except Exception:
        logger.exception("[FINAL ENTRY BOARD REST DIRECT] install_once failed")
        return False


def _watcher() -> None:
    loops = max(1, _env_int("ENTRY_BOARD_REST_DIRECT_WATCH_LOOPS", 20))
    sleep_sec = max(0.5, _env_float("ENTRY_BOARD_REST_DIRECT_WATCH_SLEEP_SEC", 1.0))
    for i in range(loops):
        ok = _install_once(log_patch=False)
        if i in (0, loops - 1):
            logger.warning("[FINAL ENTRY BOARD REST DIRECT] enforce i=%s/%s ok=%s version=%s", i, loops, ok, VERSION)
        time.sleep(sleep_sec)


def install() -> bool:
    global _WATCHER_STARTED
    os.environ.setdefault("ENTRY_BOARD_REST_SKIP_WHEN_FAILOPEN", "1")
    os.environ.setdefault("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC", "0.6")
    os.environ.setdefault("ENTRY_BOARD_REST_DIRECT_ERROR_COOLDOWN_SEC", "20")
    ok = _install_once(log_patch=True)
    if not _WATCHER_STARTED:
        _WATCHER_STARTED = True
        threading.Thread(target=_watcher, name="final-entry-board-rest-direct-enforcer", daemon=True).start()
    return bool(ok)


try:
    install()
except Exception:
    logger.exception("[FINAL ENTRY BOARD REST DIRECT] auto install failed")


__all__ = ["install", "VERSION"]
