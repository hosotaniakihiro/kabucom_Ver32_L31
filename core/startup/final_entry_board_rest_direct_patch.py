# ============================================================
# File   : core/startup/final_entry_board_rest_direct_patch.py
# Version: V4.1-FORCE-ENABLE-LIMITED-REST-BOARD-FALLBACK
# ------------------------------------------------------------
# 目的:
#   SUMMARY_AI / TONOSAMA の発注直前に PUSH 板が無い場合、
#   必要最小限だけ REST /board を使って best bid/ask を補完する。
#
# V4.1:
#   - 旧V3/V3.1が起動時に ENTRY_BOARD_REST_DIRECT_ENABLED=0 を残していても、
#     このパッチが有効な限り 1 に戻す。
#   - 明示的に止める場合は DISABLE_ENTRY_BOARD_REST_DIRECT_PATCH=1 を使う。
#   - これにより installed v4 direct_enabled=False で板補完が動かない状態を防ぐ。
#
# V4:
#   - V3/V3.1 の「FORCE指定が無い限りREST板を常時OFF」をやめる。
#   - side/source 推定は V3.1 の仕組みを維持。
#   - 1銘柄ごとに board_client.fetch_board_snapshot(timeout短め, levels=5) で補完。
# ============================================================

from __future__ import annotations

import inspect
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

VERSION = "V4.1-FORCE-ENABLE-LIMITED-REST-BOARD-FALLBACK"
_INSTALLED = False
_WATCHER_STARTED = False

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


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _infer_context_side_source(side: str = "", source: str = "") -> tuple[str, str]:
    side_s = _norm_side(side)
    source_s = str(source or "").strip() or "final_entry_safety_guard"
    if side_s:
        return side_s, source_s
    try:
        frame = inspect.currentframe()
        for _ in range(6):
            frame = frame.f_back if frame is not None else None
            if frame is None:
                break
            loc = frame.f_locals
            cand = _norm_side(loc.get("side") or loc.get("entry_side") or loc.get("ai_side"))
            if cand:
                side_s = cand
            src = loc.get("source") or loc.get("entry_source")
            if src and not source_s:
                source_s = str(src)
            if side_s:
                break
    except Exception:
        pass
    return side_s, source_s or "final_entry_safety_guard"


def _extract_board_values(board: Any) -> tuple[float, float, float, float]:
    if not isinstance(board, dict):
        return 0.0, 0.0, 0.0, 0.0

    buy1 = board.get("Buy1") if isinstance(board.get("Buy1"), dict) else {}
    sell1 = board.get("Sell1") if isinstance(board.get("Sell1"), dict) else {}

    bid = _safe_float(
        board.get("bid") or board.get("best_bid") or board.get("BidPrice") or board.get("bid_price")
        or board.get("BestBid") or buy1.get("Price"),
        0.0,
    )
    ask = _safe_float(
        board.get("ask") or board.get("best_ask") or board.get("AskPrice") or board.get("ask_price")
        or board.get("BestAsk") or sell1.get("Price"),
        0.0,
    )
    bid_qty = _safe_float(
        board.get("bid_qty") or board.get("BidQty") or board.get("bid_volume") or board.get("BestBidQty")
        or buy1.get("Qty"),
        0.0,
    )
    ask_qty = _safe_float(
        board.get("ask_qty") or board.get("AskQty") or board.get("ask_volume") or board.get("BestAskQty")
        or sell1.get("Qty"),
        0.0,
    )
    if bid > 0 and ask > 0 and bid > ask:
        bid, ask = ask, bid
        bid_qty, ask_qty = ask_qty, bid_qty
    return bid, ask, bid_qty, ask_qty


def _patch_disabled() -> bool:
    return os.getenv("DISABLE_ENTRY_BOARD_REST_DIRECT_PATCH", "").strip() == "1"


def _set_default_env() -> None:
    if _patch_disabled():
        return
    # 旧V3が残した 0 を上書きする。明示停止は DISABLE_ENTRY_BOARD_REST_DIRECT_PATCH=1 に統一。
    os.environ["ENTRY_BOARD_REST_DIRECT_ENABLED"] = "1"
    os.environ.setdefault("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC", "0.6")
    os.environ.setdefault("ENTRY_BOARD_REST_EXCHANGES", "1")
    os.environ.setdefault("ENTRY_BOARD_REST_DIRECT_LEVELS", "5")


def _call_board_rest_via_client(symbol: str, *, side: str = "", source: str = "final_entry_safety_guard") -> tuple[float, float, float, float]:
    _set_default_env()
    if _patch_disabled() or not _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", True):
        return 0.0, 0.0, 0.0, 0.0

    sym = _norm_symbol(symbol)
    if not sym:
        return 0.0, 0.0, 0.0, 0.0

    try:
        from trading.board.board_client import fetch_board_snapshot
    except Exception:
        logger.debug("[FINAL ENTRY BOARD REST DIRECT] board_client import failed symbol=%s", sym, exc_info=True)
        return 0.0, 0.0, 0.0, 0.0

    timeout = max(0.25, _env_float("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC", 0.6))
    levels = max(1, _env_int("ENTRY_BOARD_REST_DIRECT_LEVELS", 5))
    exchanges = [x.strip() for x in str(os.getenv("ENTRY_BOARD_REST_EXCHANGES", "1")).split(",") if x.strip()] or ["1"]
    for ex in exchanges:
        try:
            snap = fetch_board_snapshot(sym, exchange=int(ex), timeout=timeout, levels=levels)
            bid, ask, bid_qty, ask_qty = _extract_board_values(snap)
            if bid > 0 and ask > 0:
                logger.warning(
                    "[FINAL ENTRY BOARD REST DIRECT] BOARD_CLIENT_OK symbol=%s side=%s source=%s exchange=%s bid=%.4f ask=%.4f bid_qty=%.0f ask_qty=%.0f cache=%s version=%s",
                    sym, side, source, ex, bid, ask, bid_qty, ask_qty,
                    bool(isinstance(snap, dict) and snap.get("cache_hit")), VERSION,
                )
                return bid, ask, bid_qty, ask_qty
        except Exception:
            logger.debug("[FINAL ENTRY BOARD REST DIRECT] board_client lookup failed symbol=%s exchange=%s", sym, ex, exc_info=True)
    return 0.0, 0.0, 0.0, 0.0


def _make_try_get_bid_ask_from_api():
    def _try_get_bid_ask_from_api(symbol: str, side: str = "", source: str = "final_entry_safety_guard") -> tuple[float, float, float, float]:
        _set_default_env()
        side, source = _infer_context_side_source(side, source)
        try:
            from utils_common import get_latest_bid_ask
            try:
                res = get_latest_bid_ask(symbol, source=source, side=side)
            except TypeError:
                res = get_latest_bid_ask(symbol)
            bid, ask, bid_qty, ask_qty = _extract_board_values(res)
            if bid > 0 and ask > 0:
                logger.warning("[FINAL ENTRY BOARD REST DIRECT] PUSH_BOARD_OK symbol=%s side=%s bid=%.4f ask=%.4f version=%s", _norm_symbol(symbol), side, bid, ask, VERSION)
                return bid, ask, bid_qty, ask_qty
        except Exception:
            logger.debug("[FINAL ENTRY BOARD REST DIRECT] push board lookup failed symbol=%s side=%s", symbol, side, exc_info=True)

        bid, ask, bid_qty, ask_qty = _call_board_rest_via_client(symbol, side=side, source=source)
        if bid <= 0 or ask <= 0:
            logger.warning(
                "[FINAL ENTRY BOARD REST DIRECT] REST_EMPTY symbol=%s side=%s source=%s direct_enabled=%s disabled=%s timeout=%.3f version=%s",
                _norm_symbol(symbol), side, source,
                _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", True),
                _patch_disabled(),
                _env_float("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC", 0.6),
                VERSION,
            )
        return bid, ask, bid_qty, ask_qty

    _try_get_bid_ask_from_api._final_entry_board_rest_direct_v41 = True  # type: ignore[attr-defined]
    _try_get_bid_ask_from_api._final_entry_board_rest_direct_v4 = True  # type: ignore[attr-defined]
    _try_get_bid_ask_from_api._final_entry_board_rest_direct_v31 = True  # type: ignore[attr-defined]
    _try_get_bid_ask_from_api._final_entry_board_rest_direct_v3 = True  # type: ignore[attr-defined]
    _try_get_bid_ask_from_api._final_entry_board_rest_direct_v2 = True  # type: ignore[attr-defined]
    _try_get_bid_ask_from_api._final_entry_board_rest_direct_v1 = True  # type: ignore[attr-defined]
    return _try_get_bid_ask_from_api


def _install_once(log_patch: bool = True) -> bool:
    global _INSTALLED
    try:
        _set_default_env()
        import core.startup.final_entry_safety_guard_patch as fsg
        cur = getattr(fsg, "_try_get_bid_ask_from_api", None)
        if getattr(cur, "_final_entry_board_rest_direct_v41", False):
            _INSTALLED = True
            return True
        fsg._try_get_bid_ask_from_api = _make_try_get_bid_ask_from_api()
        _INSTALLED = True
        if log_patch:
            logger.warning(
                "[FINAL ENTRY BOARD REST DIRECT] installed v4.1 direct_enabled=%s disabled=%s timeout=%.3f hard_block=%s allow_without_board=%s version=%s",
                _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", True),
                _patch_disabled(),
                _env_float("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC", 0.6),
                _env_bool("ENTRY_BOARD_MISSING_HARD_BLOCK", True),
                _env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", False),
                VERSION,
            )
        return True
    except Exception:
        logger.exception("[FINAL ENTRY BOARD REST DIRECT] install_once failed")
        return False


def _watcher() -> None:
    loops = max(1, _env_int("ENTRY_BOARD_REST_DIRECT_WATCH_LOOPS", 30))
    sleep_sec = max(0.5, _env_float("ENTRY_BOARD_REST_DIRECT_WATCH_SLEEP_SEC", 1.0))
    for i in range(loops):
        ok = _install_once(log_patch=False)
        if i in (0, loops - 1):
            logger.warning("[FINAL ENTRY BOARD REST DIRECT] enforce i=%s/%s ok=%s version=%s", i, loops, ok, VERSION)
        time.sleep(sleep_sec)


def install() -> bool:
    global _WATCHER_STARTED
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
