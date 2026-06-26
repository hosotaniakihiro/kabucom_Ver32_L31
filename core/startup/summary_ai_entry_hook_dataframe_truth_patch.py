# ============================================================
# File   : core/startup/summary_ai_entry_hook_dataframe_truth_patch.py
# Version: V1.4-SUMMARY-AI-APPROVAL-DIAG-AND-PRICE200
# ------------------------------------------------------------
# 【目的】
#   scheduler_jobs.summary.summary_ai_entry_hook_v20.run_summary_ai_entry_safe で
#
#     ValueError: The truth value of a DataFrame is ambiguous.
#
#   が出る問題を runtime patch で防止する。
#
# V1.4:
#   - SUMMARY AI executor の approved 化前フィルタで、古い3000円下限を200円へ補正する。
#   - AI_OK が全落ちした時、daily risk / trade restriction / reject cache /
#     price range のどれで落ちたかを銘柄ごとに必ずログ出力する。
#   - 価格帯だけで候補が全滅する状態を避け、最終的な低流動性・板・日次リスクは
#     後段の entry guard に残す。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_RESULT_TO_DICT = None
_BOARD_COMPAT_PATCHED = False
_ORIGINAL_FINAL_BOARD_GUARD = None
_WEAK_SUMMARY_AI_PATCHED = False
_ORIGINAL_RUN_AI_GATE_FOR_CANDIDATES = None
_PUSH_LIQUIDITY_CAP_PATCHED = False
_SUMMARY_AI_APPROVAL_DIAG_PATCHED = False
_ORIGINAL_ENTRY_PRICE_BOUNDS = None

_KEYS_MAY_BE_DF = (
    "candidates",
    "buy_candidates",
    "sell_candidates",
    "ai_results",
    "ai_ok",
    "buy_ai_ok",
    "sell_ai_ok",
    "approved_rows",
)


def _df_to_records(v: Any) -> Any:
    try:
        import pandas as pd

        if isinstance(v, pd.DataFrame):
            if v.empty:
                return []
            # 重複カラムがあると to_dict で警告/欠落するため先頭だけ残す。
            try:
                if not v.columns.is_unique:
                    dup = [str(c) for c in v.columns[v.columns.duplicated()].unique().tolist()]
                    logger.warning(
                        "[SUMMARY AI HOOK DF TRUTH PATCH] duplicate columns before records dup=%s cols=%s rows=%s",
                        dup[:30],
                        len(v.columns),
                        len(v),
                    )
                    v = v.loc[:, ~v.columns.duplicated()].copy()
            except Exception:
                pass
            return v.to_dict(orient="records")

        if v is None:
            return []

        if isinstance(v, tuple):
            return list(v)

        return v
    except Exception:
        logger.exception("[SUMMARY AI HOOK DF TRUTH PATCH] value normalization failed type=%s", type(v).__name__)
        return [] if v is None else v


def _normalize_result_dict(d: Any) -> dict[str, Any]:
    if not isinstance(d, dict):
        return {}

    out = dict(d)
    converted: dict[str, int] = {}

    for key in _KEYS_MAY_BE_DF:
        if key in out:
            before = out.get(key)
            after = _df_to_records(before)
            out[key] = after
            try:
                if type(before).__name__ != type(after).__name__:
                    converted[key] = len(after) if hasattr(after, "__len__") else -1
            except Exception:
                converted[key] = -1

    if not isinstance(out.get("execution"), dict):
        out["execution"] = {} if out.get("execution") is None else {"raw_execution": str(out.get("execution"))[:500]}

    if converted:
        logger.warning("[SUMMARY AI HOOK DF TRUTH PATCH] normalized result DataFrame/list fields converted=%s", converted)

    return out


def _patched_result_to_dict(result: Any) -> dict[str, Any]:
    try:
        if callable(_ORIGINAL_RESULT_TO_DICT):
            d = _ORIGINAL_RESULT_TO_DICT(result)
        elif isinstance(result, dict):
            d = result
        else:
            d = {}
        return _normalize_result_dict(d)
    except Exception:
        logger.exception("[SUMMARY AI HOOK DF TRUTH PATCH] patched _result_to_dict failed")
        return {"candidates": [], "ai_results": [], "ai_ok": [], "sell_ai_ok": [], "execution": {"executed": False, "skip_reason": "result_to_dict_exception"}}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _append_reason(base: Any, extra: str) -> str:
    b = str(base or "").strip()
    return f"{b}|{extra}" if b else str(extra)


def _weak_summary_ai_ng(item: dict[str, Any]) -> tuple[bool, str]:
    """Return True when a SUMMARY/PUSH AI_OK is too weak to be trusted."""
    if not _env_bool("SUMMARY_AI_WEAK_SIGNAL_FILTER_ENABLED", True):
        return False, "disabled"
    try:
        source = str(item.get("source") or item.get("source_name") or "").upper()
        reason = str(item.get("reason") or "")
        ai_row = item.get("ai_row") if isinstance(item.get("ai_row"), dict) else {}
        source_row = item.get("source_row") if isinstance(item.get("source_row"), dict) else {}
        src_from_reason = "src=SUMMARY" in reason or "src=PUSH" in reason
        is_summary_like = src_from_reason or source in {"SUMMARY", "PUSH", "PUSH_SUMMARY", "SUMMARY_AI"}
        if not is_summary_like:
            return False, "not_summary_like"

        rank_score = _safe_float(ai_row.get("rankScore", ai_row.get("rank_score", source_row.get("rankScore", source_row.get("rank_score", 0.0)))), 0.0)
        score_mtf = max(
            abs(_safe_float(ai_row.get("score_mtf", source_row.get("score_mtf", 0.0)), 0.0)),
            abs(_safe_float(ai_row.get("mtf_score", source_row.get("mtf_score", 0.0)), 0.0)),
            abs(_safe_float(ai_row.get("mtf", source_row.get("mtf", 0.0)), 0.0)),
        )

        reason_has_zero_3m5m = ("3m=0.00" in reason or "3m=0" in reason) and ("5m=0.00" in reason or "5m=0" in reason)
        if reason_has_zero_3m5m and abs(rank_score) <= 1e-9:
            return True, "weak_summary_ai_zero_rank_and_zero_3m5m"

        v3 = max(
            abs(_safe_float(ai_row.get("score_3m", source_row.get("score_3m", 0.0)), 0.0)),
            abs(_safe_float(ai_row.get("score_total_3m", source_row.get("score_total_3m", 0.0)), 0.0)),
            abs(_safe_float(ai_row.get("final_score_3m", source_row.get("final_score_3m", 0.0)), 0.0)),
        )
        v5 = max(
            abs(_safe_float(ai_row.get("score_5m", source_row.get("score_5m", 0.0)), 0.0)),
            abs(_safe_float(ai_row.get("score_total_5m", source_row.get("score_total_5m", 0.0)), 0.0)),
            abs(_safe_float(ai_row.get("final_score_5m", source_row.get("final_score_5m", 0.0)), 0.0)),
        )
        if abs(rank_score) <= 1e-9 and score_mtf <= 1e-9 and v3 <= 1e-9 and v5 <= 1e-9:
            return True, "weak_summary_ai_no_rank_mtf_3m_5m"
        return False, "ok"
    except Exception:
        logger.exception("[SUMMARY AI WEAK FILTER] check failed")
        return False, "exception_fail_open"


def _install_summary_ai_weak_filter() -> bool:
    global _WEAK_SUMMARY_AI_PATCHED, _ORIGINAL_RUN_AI_GATE_FOR_CANDIDATES
    if _WEAK_SUMMARY_AI_PATCHED:
        return True
    try:
        import trading.entry.summary_ai.ai_gate_runner as target

        cur = getattr(target, "run_ai_gate_for_candidates", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI WEAK FILTER] target run_ai_gate_for_candidates not callable")
            return False
        if getattr(cur, "_summary_ai_weak_filter_v1", False):
            _WEAK_SUMMARY_AI_PATCHED = True
            return True
        _ORIGINAL_RUN_AI_GATE_FOR_CANDIDATES = cur

        def _patched_run_ai_gate_for_candidates(*args, **kwargs):
            results = _ORIGINAL_RUN_AI_GATE_FOR_CANDIDATES(*args, **kwargs)
            if not isinstance(results, list):
                return results
            blocked = 0
            for item in results:
                if not isinstance(item, dict) or not bool(item.get("allow")):
                    continue
                ng, why = _weak_summary_ai_ng(item)
                if not ng:
                    continue
                item["allow"] = False
                item["reason"] = _append_reason(item.get("reason"), why)
                blocked += 1
                logger.warning(
                    "[SUMMARY AI WEAK FILTER] AI_OK -> AI_NG symbol=%s side=%s reason=%s",
                    item.get("symbol"),
                    item.get("side") or item.get("ai_side"),
                    why,
                )
            if blocked:
                logger.warning("[SUMMARY AI WEAK FILTER] blocked weak AI_OK count=%s", blocked)
            return results

        _patched_run_ai_gate_for_candidates._summary_ai_weak_filter_v1 = True  # type: ignore[attr-defined]
        _patched_run_ai_gate_for_candidates._original = cur  # type: ignore[attr-defined]
        target.run_ai_gate_for_candidates = _patched_run_ai_gate_for_candidates
        _WEAK_SUMMARY_AI_PATCHED = True
        logger.warning("[SUMMARY AI WEAK FILTER] installed")
        return True
    except Exception:
        logger.exception("[SUMMARY AI WEAK FILTER] install failed")
        return False


def _install_push_liquidity_rotation_cap() -> bool:
    global _PUSH_LIQUIDITY_CAP_PATCHED
    if _PUSH_LIQUIDITY_CAP_PATCHED:
        return True
    try:
        import trading.push.subscription_manager.liquidity_guard as liq

        # 100件全救済は、薄商い銘柄まで復活させる。A/Bローテ維持には50件あれば足りる。
        liq.ROTATION_PRESERVE_FULL_POOL = False
        liq.ROTATION_MIN_SURVIVOR_COUNT = int(_env_float("PUSH_REGISTER_LIQUIDITY_ROTATION_MIN_SURVIVOR_COUNT_EFFECTIVE", 50.0))
        liq.ROTATION_MIN_SURVIVOR_RATIO = _env_float("PUSH_REGISTER_LIQUIDITY_ROTATION_MIN_SURVIVOR_RATIO_EFFECTIVE", 0.50)
        liq.MIN_SURVIVOR_COUNT = min(int(getattr(liq, "MIN_SURVIVOR_COUNT", 50)), 50)
        liq.MIN_SURVIVOR_RATIO = min(float(getattr(liq, "MIN_SURVIVOR_RATIO", 0.50)), 0.50)
        _PUSH_LIQUIDITY_CAP_PATCHED = True
        logger.warning(
            "[PUSH LIQUIDITY ROTATION CAP] installed preserve_full_pool=%s rotation_min_keep=%s rotation_min_ratio=%.3f",
            getattr(liq, "ROTATION_PRESERVE_FULL_POOL", None),
            getattr(liq, "ROTATION_MIN_SURVIVOR_COUNT", None),
            float(getattr(liq, "ROTATION_MIN_SURVIVOR_RATIO", 0.0)),
        )
        return True
    except Exception:
        logger.exception("[PUSH LIQUIDITY ROTATION CAP] install failed")
        return False


def _install_summary_ai_approval_diag_and_price_floor() -> bool:
    """Patch SUMMARY AI approved selection so price floor is 200 and skip reasons are visible."""
    global _SUMMARY_AI_APPROVAL_DIAG_PATCHED, _ORIGINAL_ENTRY_PRICE_BOUNDS
    if _SUMMARY_AI_APPROVAL_DIAG_PATCHED:
        return True
    try:
        import trading.entry.summary_ai.executor as exec_mod

        min_override = max(0.0, _env_float("SUMMARY_AI_APPROVAL_MIN_PRICE_OVERRIDE", 200.0))
        os.environ["SUMMARY_AI_ENTRY_MIN_PRICE"] = str(min_override)
        os.environ["ENTRY_MIN_PRICE"] = str(min_override)
        try:
            exec_mod.DEFAULT_MIN_PRICE_FOR_ENTRY = float(min_override)
        except Exception:
            pass

        cur_bounds = getattr(exec_mod, "_entry_price_bounds", None)
        if callable(cur_bounds) and not getattr(cur_bounds, "_summary_ai_price200_patch_v1", False):
            _ORIGINAL_ENTRY_PRICE_BOUNDS = cur_bounds

            def _patched_entry_price_bounds():
                min_price, max_price, diag = _ORIGINAL_ENTRY_PRICE_BOUNDS()
                try:
                    old_min = float(min_price or 0.0)
                    if min_override > 0 and (old_min <= 0 or old_min > min_override):
                        min_price = float(min_override)
                    if not isinstance(diag, dict):
                        diag = {"raw_diag": str(diag)}
                    diag = dict(diag)
                    diag["summary_ai_price200_patch"] = True
                    diag["old_min_price"] = old_min
                    diag["effective_min_price"] = float(min_price or 0.0)
                    diag["min_override"] = min_override
                except Exception:
                    pass
                return min_price, max_price, diag

            _patched_entry_price_bounds._summary_ai_price200_patch_v1 = True  # type: ignore[attr-defined]
            _patched_entry_price_bounds._original = cur_bounds  # type: ignore[attr-defined]
            exec_mod._entry_price_bounds = _patched_entry_price_bounds

        orig_filter = getattr(exec_mod, "_filter_blocked_ai_ok_items", None)
        if callable(orig_filter) and not getattr(orig_filter, "_summary_ai_approval_diag_v1", False):
            def _patched_filter_blocked_ai_ok_items(ok_items):
                try:
                    if not ok_items:
                        return []
                    kept = []
                    skipped = []
                    min_price, max_price, price_diag = exec_mod._entry_price_bounds()
                    for item in ok_items:
                        symbol = exec_mod._pick_symbol(item)
                        side = exec_mod._row_side(item)
                        price = exec_mod._pick_price(item)
                        reason = ""
                        detail: dict[str, Any] = {}

                        if price > 0 and min_price > 0 and price < min_price:
                            reason = "price_below_entry_min_price"
                            detail = {"price": price, "min_price": min_price, "max_price": max_price, "min_notional_100": round(price * 100, 1)}
                        elif price > 0 and max_price > 0 and price > max_price:
                            reason = "price_over_entry_max_price"
                            detail = {"price": price, "min_price": min_price, "max_price": max_price, "min_notional_100": round(price * 100, 1)}
                        else:
                            daily_blocked, daily_reason, daily_detail = exec_mod._daily_risk_block_reason(symbol, side)
                            if daily_blocked:
                                reason = str(daily_reason or "DAILY_RISK_BLOCK")
                                detail = {"source": "daily_risk_pre_approval", "detail": daily_detail}
                            else:
                                restricted, until = exec_mod._is_trade_restricted_symbol(symbol)
                                if restricted:
                                    reason = "trade_restricted"
                                    detail = {"until": str(until)}
                                else:
                                    sell_rejected, reject_reason = exec_mod._is_sell_reject_cached(symbol, side)
                                    if sell_rejected:
                                        reason = "sell_reject_cache"
                                        detail = {"detail": str(reject_reason)}

                        if reason:
                            row = {"symbol": symbol, "side": side, "reason": reason, **detail}
                            skipped.append(row)
                            logger.warning("[SUMMARY AI APPROVAL DIAG] skipped symbol=%s side=%s reason=%s detail=%s", symbol, side, reason, detail)
                        else:
                            kept.append(item)

                    counts: dict[str, int] = {}
                    for s in skipped:
                        r = str(s.get("reason") or "UNKNOWN")
                        counts[r] = counts.get(r, 0) + 1
                    if skipped:
                        logger.warning(
                            "[SUMMARY AI APPROVAL DIAG] filter result before=%s after=%s skipped=%s counts=%s min_price=%s max_price=%s price_diag=%s skipped_head=%s",
                            len(ok_items),
                            len(kept),
                            len(skipped),
                            counts,
                            min_price,
                            max_price,
                            price_diag,
                            skipped[:80],
                        )
                    else:
                        logger.info(
                            "[SUMMARY AI APPROVAL DIAG] filter passed before=%s after=%s min_price=%s max_price=%s price_diag=%s",
                            len(ok_items), len(kept), min_price, max_price, price_diag,
                        )
                    return kept
                except Exception:
                    logger.exception("[SUMMARY AI APPROVAL DIAG] patched filter failed; fallback original")
                    return orig_filter(ok_items)

            _patched_filter_blocked_ai_ok_items._summary_ai_approval_diag_v1 = True  # type: ignore[attr-defined]
            _patched_filter_blocked_ai_ok_items._original = orig_filter  # type: ignore[attr-defined]
            exec_mod._filter_blocked_ai_ok_items = _patched_filter_blocked_ai_ok_items

        _SUMMARY_AI_APPROVAL_DIAG_PATCHED = True
        logger.warning(
            "[SUMMARY AI APPROVAL DIAG] installed price_min_override=%.0f executor_default_min=%s",
            min_override,
            getattr(exec_mod, "DEFAULT_MIN_PRICE_FOR_ENTRY", None),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI APPROVAL DIAG] install failed")
        return False


def _install_final_entry_board_guard_compat() -> bool:
    """Keep final_entry_safety_guard_patch._board_guard callable with both 3 and 4 args.

    2026-06-05 の実行ログで、呼び出し側は _board_guard(row, item, symbol, side) なのに
    別パッチで3引数版の _patched_board_guard に差し替わり TypeError になっていた。
    ここで最終的に4引数互換ラッパーを被せ、同系統の差し替え順序でも落ちないようにする。
    """
    global _BOARD_COMPAT_PATCHED, _ORIGINAL_FINAL_BOARD_GUARD

    if _BOARD_COMPAT_PATCHED:
        return True

    try:
        import core.startup.final_entry_safety_guard_patch as target

        cur = getattr(target, "_board_guard", None)
        if not callable(cur):
            logger.warning("[FINAL ENTRY BOARD GUARD COMPAT] target _board_guard not callable")
            return False
        if getattr(cur, "_final_entry_board_guard_compat", False):
            _BOARD_COMPAT_PATCHED = True
            return True

        _ORIGINAL_FINAL_BOARD_GUARD = cur

        def _board_guard_compat(row: dict, item: dict | None = None, symbol: str | None = None, side: str | None = None, *args, **kwargs) -> bool:
            item = item if isinstance(item, dict) else {}
            symbol = str(symbol or "")
            side = str(side or "").upper()
            try:
                return bool(_ORIGINAL_FINAL_BOARD_GUARD(row, item, symbol, side))  # type: ignore[misc]
            except TypeError as e4:
                try:
                    logger.warning(
                        "[FINAL ENTRY BOARD GUARD COMPAT] fallback 4args->3args symbol=%s side=%s err=%s",
                        symbol,
                        side,
                        e4,
                    )
                    return bool(_ORIGINAL_FINAL_BOARD_GUARD(row, symbol, side))  # type: ignore[misc]
                except TypeError:
                    logger.exception(
                        "[FINAL ENTRY BOARD GUARD COMPAT] incompatible board_guard signature symbol=%s side=%s",
                        symbol,
                        side,
                    )
                    return False
            except Exception:
                logger.exception("[FINAL ENTRY BOARD GUARD COMPAT] board guard failed symbol=%s side=%s", symbol, side)
                return False

        _board_guard_compat._final_entry_board_guard_compat = True  # type: ignore[attr-defined]
        target._board_guard = _board_guard_compat
        _BOARD_COMPAT_PATCHED = True
        logger.warning("[FINAL ENTRY BOARD GUARD COMPAT] installed")
        return True
    except Exception:
        logger.exception("[FINAL ENTRY BOARD GUARD COMPAT] install failed")
        return False


def _install_ranking_precheck_pending_failopen() -> bool:
    try:
        import core.startup.ranking_precheck_pending_failopen_patch as patch
        fn = getattr(patch, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[SUMMARY AI HOOK DF TRUTH PATCH] ranking precheck pending failopen installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[SUMMARY AI HOOK DF TRUTH PATCH] ranking precheck pending failopen install failed")
        return False


def install() -> bool:
    global _PATCHED, _ORIGINAL_RESULT_TO_DICT

    ok_board = _install_final_entry_board_guard_compat()
    ok_rank_precheck = _install_ranking_precheck_pending_failopen()
    ok_weak_ai = _install_summary_ai_weak_filter()
    ok_liq_cap = _install_push_liquidity_rotation_cap()
    ok_approval_diag = _install_summary_ai_approval_diag_and_price_floor()

    if _PATCHED:
        return True and ok_board and ok_rank_precheck and ok_weak_ai and ok_liq_cap and ok_approval_diag

    try:
        import scheduler_jobs.summary.summary_ai_entry_hook_v20 as target

        cur = getattr(target, "_result_to_dict", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI HOOK DF TRUTH PATCH] target _result_to_dict not callable")
            return bool(ok_board and ok_rank_precheck and ok_weak_ai and ok_liq_cap and ok_approval_diag)
        if getattr(cur, "_summary_ai_hook_df_truth_patch", False):
            _PATCHED = True
            return True and ok_board and ok_rank_precheck and ok_weak_ai and ok_liq_cap and ok_approval_diag

        _ORIGINAL_RESULT_TO_DICT = cur
        _patched_result_to_dict._summary_ai_hook_df_truth_patch = True  # type: ignore[attr-defined]
        target._result_to_dict = _patched_result_to_dict

        _PATCHED = True
        logger.warning(
            "[SUMMARY AI HOOK DF TRUTH PATCH] installed board_compat=%s ranking_precheck_failopen=%s weak_ai=%s liq_cap=%s approval_diag=%s",
            ok_board,
            ok_rank_precheck,
            ok_weak_ai,
            ok_liq_cap,
            ok_approval_diag,
        )
        return True and ok_board and ok_rank_precheck and ok_weak_ai and ok_liq_cap and ok_approval_diag
    except Exception:
        logger.exception("[SUMMARY AI HOOK DF TRUTH PATCH] install failed")
        return bool(ok_board and ok_rank_precheck and ok_weak_ai and ok_liq_cap and ok_approval_diag)


__all__ = ["install"]
