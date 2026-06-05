# ============================================================
# File   : core/startup/board_retry_patch.py
# Version: V1.4-BOARD-MISSING-HARD-BLOCK
# ------------------------------------------------------------
# A/B PUSHローテーション中の板未取得を短時間リトライする。
#
# V1.4:
#   - final_entry_safety_guard で bid/ask が取れない場合、
#     ENTRY_ALLOW_ENTRY_WITHOUT_BOARD=1 が外部で立っていても、
#     ENTRY_BOARD_MISSING_HARD_BLOCK=1 をデフォルトとして新規エントリーを停止する。
#   - 板が0のまま BOARD_MISSING_ALLOW で進む危険な挙動を止める。
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
        bid = board.get("bid_price") or board.get("bid") or board.get("best_bid") or board.get("BidPrice")
        ask = board.get("ask_price") or board.get("ask") or board.get("best_ask") or board.get("AskPrice")
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
    try:
        board = original(symbol, *args, **kwargs)
    except TypeError:
        board = original(symbol)
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

    logger.warning("[BOARD RETRY] board still missing symbol=%s side=%s source=%s after retries=%s extra=%s", sym, side, source, retry_count, extra_count)
    return last_board


def _wrap_get_latest_bid_ask(original):
    original = _unwrap_original(original)
    if getattr(original, "_board_retry_v14", False):
        return original

    def _get_latest_bid_ask_retry(symbol: Any, *args, **kwargs):
        return _retry_fetch_board(original, symbol, *args, **kwargs)

    _get_latest_bid_ask_retry._board_retry_v14 = True  # type: ignore[attr-defined]
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

    _get_board_with_retry._board_retry_v14 = True  # type: ignore[attr-defined]
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
                if isinstance(res, dict):
                    return (
                        fsg._safe_float(res.get("bid") or res.get("best_bid") or res.get("BidPrice") or res.get("bid_price"), 0.0),
                        fsg._safe_float(res.get("ask") or res.get("best_ask") or res.get("AskPrice") or res.get("ask_price"), 0.0),
                        fsg._safe_float(res.get("bid_qty") or res.get("BidQty") or res.get("bid_volume"), 0.0),
                        fsg._safe_float(res.get("ask_qty") or res.get("AskQty") or res.get("ask_volume"), 0.0),
                    )
                if isinstance(res, (list, tuple)) and len(res) >= 2:
                    return fsg._safe_float(res[0], 0.0), fsg._safe_float(res[1], 0.0), 0.0, 0.0
            except Exception:
                logger.debug("[BOARD RETRY] final guard get_latest_bid_ask failed symbol=%s side=%s", symbol, side, exc_info=True)
            return 0.0, 0.0, 0.0, 0.0

        def _board_guard_side_aware(row: dict, symbol: str, side: str) -> bool:
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
                # Safety first: 板が取れない時は原則エントリー禁止。
                # 外部で ENTRY_ALLOW_ENTRY_WITHOUT_BOARD=1 が残っていても、
                # ENTRY_BOARD_MISSING_HARD_BLOCK=1(default) が優先される。
                if _env_bool("ENTRY_BOARD_MISSING_HARD_BLOCK", True):
                    fsg._log_ng("board_missing", symbol, side, bid=bid, ask=ask, message="板が取れないため新規エントリー停止")
                    logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_MISSING_HARD_BLOCK symbol=%s side=%s bid=%s ask=%s", symbol, side, bid, ask)
                    return False
                if fsg._env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", False):
                    logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_MISSING_ALLOW symbol=%s side=%s bid=%s ask=%s", symbol, side, bid, ask)
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

        fsg._try_get_bid_ask_from_api = _try_get_bid_ask_from_api_side
        fsg._board_guard = _board_guard_side_aware
        _SIDE_PATCHED = True
        logger.warning("[BOARD RETRY] patched final_entry_safety_guard board fetch with side/source hard_block=%s", _env_bool("ENTRY_BOARD_MISSING_HARD_BLOCK", True))
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
            if not isinstance(tf_detail, dict):
                continue
            if tf_detail.get("reason") == "not_enough_rows" and int(tf_detail.get("rows") or 0) < min_rows:
                return True
        return False
    except Exception:
        return False


def _wrap_opening_ma5_relax(fn, label: str):
    if not callable(fn) or getattr(fn, "_ma5_opening_relax_v14", False):
        return fn

    def _wrapped(*args, **kwargs):
        ret = fn(*args, **kwargs)
        if _env_bool("ENTRY_MA5_BREAKOUT_OPENING_FAIL_OPEN", True) and _ma5_opening_missing_only(ret):
            logger.warning("[MA5 BREAKOUT OPENING RELAX] fail-open label=%s reason=history_not_enough_rows ret=%s", label, ret)
            return None
        return ret

    _wrapped._ma5_opening_relax_v14 = True  # type: ignore[attr-defined]
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
        if not callable(old) or getattr(old, "_daily_src_cleanup_v14", False):
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

        _sanitize_df_daily_cleanup._daily_src_cleanup_v14 = True  # type: ignore[attr-defined]
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
    # 安全側の既定値。ユーザーが明示的に0にしない限り、板なしエントリーは禁止。
    os.environ.setdefault("ENTRY_BOARD_MISSING_HARD_BLOCK", "1")
    os.environ.setdefault("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", "0")

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
        "[BOARD RETRY] installed=%s enabled=%s wait_sec=%.2f retry_count=%s extra_wait=%.2f extra_count=%s only_pending=%s side_patch=%s ma5_opening_relax=%s daily_dup_cleanup=%s board_hard_block=%s allow_without_board=%s",
        _PATCHED,
        _env_bool("ENTRY_BOARD_RETRY_ENABLED", True),
        wait_sec,
        _env_int("ENTRY_BOARD_RETRY_COUNT", 1),
        extra_wait_sec,
        extra_count,
        _env_bool("ENTRY_BOARD_RETRY_SYMBOLS_ONLY_PENDING", False),
        side_ok,
        ma5_ok,
        dup_ok,
        _env_bool("ENTRY_BOARD_MISSING_HARD_BLOCK", True),
        _env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", False),
    )
    return _PATCHED


try:
    install()
except Exception:
    logger.exception("[BOARD RETRY] auto install failed")


__all__ = ["install"]
