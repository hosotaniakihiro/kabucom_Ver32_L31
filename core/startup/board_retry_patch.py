# ============================================================
# File   : core/startup/board_retry_patch.py
# Version: V1.6-FINAL-GUARD-DIRECT-REST-BOARD-FALLBACK
# ------------------------------------------------------------
# A/B PUSHローテーション中の板未取得を短時間リトライする。
#
# V1.6:
#   - final_entry_safety_guard 側で実際に使われている board_retry_patch 内に、
#     kabu Station REST /board 直接fallbackを追加。
#   - utils_common.get_latest_bid_ask は PUSH限定のため、PUSHローテ外銘柄では
#     bid/ask が取れず board_missing になる。RESTで補完する。
#   - RESTでも bid/ask が取れない場合は従来通り hard block する。
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
    bid = _safe_float(
        board.get("bid_price")
        or board.get("bid")
        or board.get("best_bid")
        or board.get("BidPrice")
        or board.get("BestBid")
        or buy1.get("Price"),
        0.0,
    )
    ask = _safe_float(
        board.get("ask_price")
        or board.get("ask")
        or board.get("best_ask")
        or board.get("AskPrice")
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
    if not _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", True):
        return None
    sym = _norm_symbol(symbol)
    if not sym:
        return None
    token = _get_token()
    if not token:
        logger.warning("[BOARD RETRY REST] TOKEN_MISSING symbol=%s side=%s source=%s", sym, side, source)
        return None
    try:
        import requests  # type: ignore
    except Exception:
        logger.warning("[BOARD RETRY REST] requests import failed symbol=%s", sym)
        return None

    timeout = max(0.3, _env_float("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC", 1.5))
    exchanges = [x.strip() for x in str(os.getenv("ENTRY_BOARD_REST_EXCHANGES", "1")).split(",") if x.strip()]
    if not exchanges:
        exchanges = ["1"]

    for ex in exchanges:
        url = f"http://localhost:18080/kabusapi/board/{sym}@{ex}"
        try:
            res = requests.get(url, headers={"X-API-KEY": token}, timeout=timeout)
            status = getattr(res, "status_code", None)
            if status != 200:
                logger.warning("[BOARD RETRY REST] REST_NG symbol=%s side=%s source=%s exchange=%s status=%s", sym, side, source, ex, status)
                continue
            data = res.json()
            bid, ask, bid_qty, ask_qty = _extract_bid_ask(data)
            if bid > 0 and ask > 0:
                logger.warning(
                    "[BOARD RETRY REST] REST_BOARD_OK symbol=%s side=%s source=%s exchange=%s bid=%.4f ask=%.4f bid_qty=%.0f ask_qty=%.0f",
                    sym,
                    side,
                    source,
                    ex,
                    bid,
                    ask,
                    bid_qty,
                    ask_qty,
                )
                return _board_dict(sym, bid, ask, bid_qty, ask_qty, source="rest_board")
            logger.warning("[BOARD RETRY REST] REST_BOARD_EMPTY symbol=%s side=%s source=%s exchange=%s keys=%s", sym, side, source, ex, sorted(list(data.keys()))[:20] if isinstance(data, dict) else type(data).__name__)
        except Exception as exc:
            logger.warning("[BOARD RETRY REST] REST_ERROR symbol=%s side=%s source=%s exchange=%s error=%r", sym, side, source, ex, exc)
    return None


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
    sym = _norm_symbol(symbol)
    try:
        board = original(symbol, *args, **kwargs)
    except TypeError:
        board = original(symbol)
    if _is_valid_board(board):
        return board

    if not _env_bool("ENTRY_BOARD_RETRY_ENABLED", True):
        rest_board = _fetch_board_rest(sym, side=side, source=source)
        return rest_board or board

    if not _is_pending_or_candidate(sym):
        rest_board = _fetch_board_rest(sym, side=side, source=source)
        return rest_board or board

    retry_count = max(0, _env_int("ENTRY_BOARD_RETRY_COUNT", 1))
    wait_sec = max(0.0, _env_float("ENTRY_BOARD_RETRY_WAIT_SEC", 4.5))
    extra_count = max(0, _env_int("ENTRY_BOARD_RETRY_EXTRA_COUNT", 2))
    extra_wait_sec = max(0.0, _env_float("ENTRY_BOARD_RETRY_EXTRA_WAIT_SEC", 0.3))

    if str(source or "") == "final_entry_safety_guard" and _env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", False) and not _env_bool("ENTRY_BOARD_MISSING_HARD_BLOCK", True):
        retry_count = min(retry_count, max(0, _env_int("ENTRY_FINAL_BOARD_RETRY_COUNT", 0)))
        wait_sec = min(wait_sec, max(0.0, _env_float("ENTRY_FINAL_BOARD_RETRY_WAIT_SEC", 0.0)))
        extra_count = min(extra_count, max(0, _env_int("ENTRY_FINAL_BOARD_RETRY_EXTRA_COUNT", 1)))
        extra_wait_sec = min(extra_wait_sec, max(0.0, _env_float("ENTRY_FINAL_BOARD_RETRY_EXTRA_WAIT_SEC", 0.2)))

    last_board = board
    for i in range(1, retry_count + 1):
        if wait_sec <= 0:
            break
        logger.warning(
            "[BOARD RETRY] board missing symbol=%s side=%s source=%s retry=%s/%s wait=%.2fs reason=push_rotation_4p5s",
            sym, side, source, i, retry_count, wait_sec,
        )
        time.sleep(wait_sec)
        try:
            last_board = original(symbol, *args, **kwargs)
        except TypeError:
            try:
                last_board = original(symbol)
            except Exception:
                logger.debug("[BOARD RETRY] retry failed symbol=%s retry=%s", sym, i, exc_info=True)
                continue
        except Exception:
            logger.debug("[BOARD RETRY] retry failed symbol=%s retry=%s", sym, i, exc_info=True)
            continue
        if _is_valid_board(last_board):
            logger.warning("[BOARD RETRY] board recovered symbol=%s side=%s source=%s retry=%s board=%s", sym, side, source, i, last_board)
            return last_board

    for j in range(1, extra_count + 1):
        if extra_wait_sec <= 0:
            break
        logger.warning(
            "[BOARD RETRY] board still missing symbol=%s side=%s source=%s extra_retry=%s/%s wait=%.2fs reason=rotation_boundary_possible",
            sym, side, source, j, extra_count, extra_wait_sec,
        )
        time.sleep(extra_wait_sec)
        try:
            last_board = original(symbol, *args, **kwargs)
        except TypeError:
            try:
                last_board = original(symbol)
            except Exception:
                logger.debug("[BOARD RETRY] extra retry failed symbol=%s retry=%s", sym, j, exc_info=True)
                continue
        except Exception:
            logger.debug("[BOARD RETRY] extra retry failed symbol=%s retry=%s", sym, j, exc_info=True)
            continue
        if _is_valid_board(last_board):
            logger.warning("[BOARD RETRY] board recovered on extra symbol=%s side=%s source=%s extra_retry=%s board=%s", sym, side, source, j, last_board)
            return last_board

    rest_board = _fetch_board_rest(sym, side=side, source=source)
    if _is_valid_board(rest_board):
        return rest_board

    logger.warning("[BOARD RETRY] board still missing symbol=%s side=%s source=%s after retries=%s extra=%s rest_direct=%s", sym, side, source, retry_count, extra_count, _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", True))
    return last_board


def _wrap_get_latest_bid_ask(original):
    original = _unwrap_original(original)
    if getattr(original, "_board_retry_v16", False):
        return original

    def _get_latest_bid_ask_retry(symbol: Any, *args, **kwargs):
        return _retry_fetch_board(original, symbol, *args, **kwargs)

    _get_latest_bid_ask_retry._board_retry_v16 = True  # type: ignore[attr-defined]
    _get_latest_bid_ask_retry._board_retry_v15 = True  # type: ignore[attr-defined]
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

    _get_board_with_retry._board_retry_v16 = True  # type: ignore[attr-defined]
    _get_board_with_retry._board_retry_v15 = True  # type: ignore[attr-defined]
    _get_board_with_retry._original = original_get_latest_bid_ask  # type: ignore[attr-defined]
    return _get_board_with_retry


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
            rest = _fetch_board_rest(symbol, side=side, source=source)
            bid, ask, bid_qty, ask_qty = _extract_bid_ask(rest)
            return bid, ask, bid_qty, ask_qty

        def _board_guard_side_aware(row: dict, item: dict | str | None = None, symbol: str | None = None, side: str | None = None, *_, **__) -> bool:
            if side is None and symbol is not None:
                side = symbol
                symbol = item if isinstance(item, str) else str(item or "")
                item = None
            row = fsg._row_to_dict(row)
            symbol = fsg._norm_symbol(symbol or fsg._first(row, ("symbol", "Symbol", "code", "銘柄コード"), ""))
            side = fsg._norm_side(side or fsg._first(row, ("side", "entry_decision", "ai_side"), ""))
            if not fsg._env_bool("ENTRY_BOARD_GUARD_ENABLED", True):
                return True
            bid, ask, bid_qty, ask_qty = fsg._extract_bid_ask_from_row(row)
            if bid <= 0 or ask <= 0:
                bid2, ask2, bidq2, askq2 = _try_get_bid_ask_from_api_side(symbol, side=side, source="final_entry_safety_guard")
                bid = bid or bid2
                ask = ask or ask2
                bid_qty = bid_qty or bidq2
                ask_qty = ask_qty or askq2
            if bid <= 0 or ask <= 0:
                if _env_bool("ENTRY_BOARD_MISSING_HARD_BLOCK", True):
                    fsg._log_ng("board_missing", symbol, side, bid=bid, ask=ask, message="板が取れないため新規エントリー停止")
                    logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_MISSING_HARD_BLOCK symbol=%s side=%s bid=%s ask=%s rest_direct=%s", symbol, side, bid, ask, _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", True))
                    return False
                if fsg._env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", False):
                    logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_MISSING_ALLOW symbol=%s side=%s bid=%s ask=%s fast_fallback=True", symbol, side, bid, ask)
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
            try:
                row["bid"] = bid
                row["ask"] = ask
                row["bid_qty"] = bid_qty
                row["ask_qty"] = ask_qty
                if isinstance(item, dict) and isinstance(item.get("entry_row"), dict):
                    item["entry_row"].update({"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty})
            except Exception:
                pass
            logger.info(
                "[FINAL ENTRY SAFETY GUARD] BOARD_OK symbol=%s side=%s bid=%.4f ask=%.4f spread_pct=%.4f bid_qty=%.0f ask_qty=%.0f",
                symbol, side, bid, ask, spread_pct, bid_qty, ask_qty,
            )
            return True

        fsg._try_get_bid_ask_from_api = _try_get_bid_ask_from_api_side
        fsg._board_guard = _board_guard_side_aware
        fsg._patched_board_guard = _board_guard_side_aware
        _SIDE_PATCHED = True
        logger.warning("[BOARD RETRY] patched final_entry_safety_guard board fetch with side/source hard_block=%s signature=v16 rest_direct=%s", _env_bool("ENTRY_BOARD_MISSING_HARD_BLOCK", True), _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", True))
        return True
    except Exception:
        logger.exception("[BOARD RETRY] final_entry_safety_guard side-aware board patch failed")
        return False


def _ma5_opening_missing_only(ret: Any) -> bool:
    try:
        if not isinstance(ret, dict):
            return False
        if ret.get("ok", True) is not False:
            return False
        if str(ret.get("reason") or "") != "MA5_BREAKOUT_MISSING_OR_NOT_BROKEN":
            return False
        detail = ret.get("detail") or {}
        details = detail.get("details") or {}
        min_rows = max(1, _env_int("ENTRY_MA5_BREAKOUT_OPENING_MIN_ROWS", 2))
        for tf_detail in details.values():
            if isinstance(tf_detail, dict) and tf_detail.get("reason") == "not_enough_rows" and int(tf_detail.get("rows") or 0) < min_rows:
                return True
    except Exception:
        pass
    return False


def _wrap_opening_ma5_relax(fn, label: str):
    if not callable(fn) or getattr(fn, "_ma5_opening_relax_v16", False):
        return fn

    def _wrapped(*args, **kwargs):
        ret = fn(*args, **kwargs)
        if _env_bool("ENTRY_MA5_BREAKOUT_OPENING_FAIL_OPEN", True) and _ma5_opening_missing_only(ret):
            logger.warning("[MA5 BREAKOUT OPENING RELAX] fail-open label=%s reason=history_not_enough_rows ret=%s", label, ret)
            return None
        return ret

    _wrapped._ma5_opening_relax_v16 = True  # type: ignore[attr-defined]
    _wrapped._ma5_opening_relax_v15 = True  # type: ignore[attr-defined]
    _wrapped._original = fn  # type: ignore[attr-defined]
    return _wrapped


def _install_ma5_opening_relax() -> bool:
    global _MA5_OPENING_PATCHED
    if _MA5_OPENING_PATCHED:
        return True
    ok = False
    try:
        import trading.handlers.entry_order_builder as eob
        old = getattr(eob, "_summary_mtf_direction_guard", None)
        new = _wrap_opening_ma5_relax(old, "entry_order_builder")
        if new is not old:
            eob._summary_mtf_direction_guard = new
            ok = True
            logger.warning("[MA5 BREAKOUT OPENING RELAX] patched entry_order_builder")
    except Exception:
        logger.exception("[MA5 BREAKOUT OPENING RELAX] patch entry_order_builder failed")
    try:
        import core.startup.entry_limit_passive_runtime_patch as elp
        old2 = getattr(elp, "_summary_ai_strict_guard", None)
        new2 = _wrap_opening_ma5_relax(old2, "entry_limit_passive")
        if new2 is not old2:
            elp._summary_ai_strict_guard = new2
            ok = True
            logger.warning("[MA5 BREAKOUT OPENING RELAX] patched entry_limit_passive")
    except Exception:
        logger.exception("[MA5 BREAKOUT OPENING RELAX] patch entry_limit_passive failed")
    _MA5_OPENING_PATCHED = bool(ok)
    return _MA5_OPENING_PATCHED


def _install_daily_src_duplicate_cleanup() -> bool:
    global _DAILY_DUP_PATCHED
    if _DAILY_DUP_PATCHED:
        return True
    try:
        import pandas as pd
        import trading.summary.controller_utils as cu
        old = getattr(cu, "sanitize_df", None)
        if not callable(old) or getattr(old, "_daily_src_cleanup_v16", False):
            _DAILY_DUP_PATCHED = True
            return True

        def _sanitize_df_daily_cleanup(df):
            try:
                if isinstance(df, pd.DataFrame) and not df.empty:
                    daily_src_cols = [c for c in df.columns if str(c).endswith("_daily_src")]
                    if daily_src_cols:
                        df = df.loc[:, ~df.columns.duplicated(keep="last")].copy()
            except Exception:
                pass
            return old(df)

        _sanitize_df_daily_cleanup._daily_src_cleanup_v16 = True  # type: ignore[attr-defined]
        _sanitize_df_daily_cleanup._daily_src_cleanup_v15 = True  # type: ignore[attr-defined]
        _sanitize_df_daily_cleanup._original = old  # type: ignore[attr-defined]
        cu.sanitize_df = _sanitize_df_daily_cleanup
        _DAILY_DUP_PATCHED = True
        logger.warning("[BOARD RETRY] patched controller_utils.sanitize_df daily_src duplicate pre-cleanup")
        return True
    except Exception:
        logger.exception("[BOARD RETRY] daily_src duplicate cleanup patch failed")
        return False


def install() -> bool:
    global _PATCHED
    os.environ.setdefault("ENTRY_BOARD_MISSING_HARD_BLOCK", "1")
    os.environ.setdefault("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", "0")
    os.environ.setdefault("ENTRY_FINAL_BOARD_RETRY_COUNT", "0")
    os.environ.setdefault("ENTRY_FINAL_BOARD_RETRY_EXTRA_COUNT", "1")
    os.environ.setdefault("ENTRY_FINAL_BOARD_RETRY_EXTRA_WAIT_SEC", "0.2")
    os.environ.setdefault("ENTRY_BOARD_REST_DIRECT_ENABLED", "1")
    os.environ.setdefault("ENTRY_BOARD_REST_DIRECT_TIMEOUT_SEC", "1.5")
    os.environ.setdefault("ENTRY_BOARD_REST_EXCHANGES", "1")

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
            logger.warning("[BOARD RETRY] patched utils_common.get_latest_bid_ask v16 rest_direct=%s", _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", True))
    except Exception:
        logger.exception("[BOARD RETRY] patch utils_common failed")

    try:
        import trading.handlers.entry_order_builder as eob
        orig = getattr(eob, "get_latest_bid_ask", None)
        if callable(orig):
            setattr(eob, "ENTRY_ORDER_BOARD_RETRY_ENABLED", True)
            setattr(eob, "ENTRY_ORDER_BOARD_RETRY_SEC", wait_sec)
            setattr(eob, "ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", extra_wait_sec)
            setattr(eob, "ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", _env_bool("ENTRY_ORDER_REQUIRE_BOARD_FOR_SUMMARY", False))
            eob.get_latest_bid_ask = _unwrap_original(orig)
            eob._get_board_with_retry = _make_entry_order_builder_retry(eob.get_latest_bid_ask)
            ok_any = True
            logger.warning("[BOARD RETRY] patched entry_order_builder._get_board_with_retry wait=%.2fs extra_wait=%.2fs extra_count=%s", wait_sec, extra_wait_sec, extra_count)
    except Exception:
        logger.exception("[BOARD RETRY] patch entry_order_builder failed")

    side_ok = _install_final_safety_side_aware_board()
    ma5_ok = _install_ma5_opening_relax()
    dup_ok = _install_daily_src_duplicate_cleanup()

    _PATCHED = bool(ok_any or side_ok or ma5_ok or dup_ok)
    logger.warning(
        "[BOARD RETRY] installed=%s version=v16 enabled=%s wait_sec=%.2f retry_count=%s extra_wait=%.2f extra_count=%s final_retry=%s/%s only_pending=%s side_patch=%s ma5_opening_relax=%s daily_dup_cleanup=%s board_hard_block=%s allow_without_board=%s rest_direct=%s rest_exchanges=%s",
        _PATCHED,
        _env_bool("ENTRY_BOARD_RETRY_ENABLED", True),
        wait_sec,
        _env_int("ENTRY_BOARD_RETRY_COUNT", 1),
        extra_wait_sec,
        extra_count,
        _env_int("ENTRY_FINAL_BOARD_RETRY_COUNT", 0),
        _env_int("ENTRY_FINAL_BOARD_RETRY_EXTRA_COUNT", 1),
        _env_bool("ENTRY_BOARD_RETRY_SYMBOLS_ONLY_PENDING", False),
        side_ok,
        ma5_ok,
        dup_ok,
        _env_bool("ENTRY_BOARD_MISSING_HARD_BLOCK", True),
        _env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", False),
        _env_bool("ENTRY_BOARD_REST_DIRECT_ENABLED", True),
        os.getenv("ENTRY_BOARD_REST_EXCHANGES", "1"),
    )
    return _PATCHED


try:
    install()
except Exception:
    logger.exception("[BOARD RETRY] auto install failed")


__all__ = ["install"]
