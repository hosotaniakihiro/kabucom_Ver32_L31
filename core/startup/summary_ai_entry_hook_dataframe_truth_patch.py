# ============================================================
# File   : core/startup/summary_ai_entry_hook_dataframe_truth_patch.py
# Version: V1.3-WEAK-SUMMARY-AI-AND-PUSH-LIQUIDITY-CAP
# ------------------------------------------------------------
# 【目的】
#   scheduler_jobs.summary.summary_ai_entry_hook_v20.run_summary_ai_entry_safe で
#
#     ValueError: The truth value of a DataFrame is ambiguous.
#
#   が出る問題を runtime patch で防止する。
#
# V1.1:
#   - final_entry_safety_guard_patch の _board_guard が別runtime patchにより
#     3引数版へ差し替わっても、4引数呼び出しで TypeError にならない
#     互換ラッパーを追加する。
#   - エントリー直前の板ガードで落ちて候補実行が止まる問題を防止する。
#
# V1.2:
#   - RANKING pending が既にある場合、古い ranking_snapshot_1min だけで
#     entry_controller が止まらないよう ranking_precheck_pending_failopen_patch も
#     起動時に同時installする。
#
# V1.3:
#   - SUMMARY/PUSH由来で rankScore=0 かつ 3m=0/5m=0 の弱いAI_OKをAI_NG化する。
#   - PUSH liquidity guard の rotation 全100件救済を解除し、50件程度まで削れるようにする。
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

        # reason文字列の 3m=0.00 / 5m=0.00 を強く見る。ログで問題になった形を直撃する。
        reason_has_zero_3m5m = ("3m=0.00" in reason or "3m=0" in reason) and ("5m=0.00" in reason or "5m=0" in reason)
        if reason_has_zero_3m5m and abs(rank_score) <= 1e-9:
            return True, "weak_summary_ai_zero_rank_and_zero_3m5m"

        # 列ベースでも、rank/MTF/3m/5mが全部無い場合は止める。
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
                # まず現在の実体が4引数対応ならそのまま通す。
                return bool(_ORIGINAL_FINAL_BOARD_GUARD(row, item, symbol, side))  # type: ignore[misc]
            except TypeError as e4:
                # 古い/別パッチの3引数版は row, symbol, side を想定して呼ぶ。
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

    if _PATCHED:
        return True and ok_board and ok_rank_precheck and ok_weak_ai and ok_liq_cap

    try:
        import scheduler_jobs.summary.summary_ai_entry_hook_v20 as target

        cur = getattr(target, "_result_to_dict", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI HOOK DF TRUTH PATCH] target _result_to_dict not callable")
            return bool(ok_board and ok_rank_precheck and ok_weak_ai and ok_liq_cap)
        if getattr(cur, "_summary_ai_hook_df_truth_patch", False):
            _PATCHED = True
            return True and ok_board and ok_rank_precheck and ok_weak_ai and ok_liq_cap

        _ORIGINAL_RESULT_TO_DICT = cur
        _patched_result_to_dict._summary_ai_hook_df_truth_patch = True  # type: ignore[attr-defined]
        target._result_to_dict = _patched_result_to_dict

        _PATCHED = True
        logger.warning(
            "[SUMMARY AI HOOK DF TRUTH PATCH] installed board_compat=%s ranking_precheck_failopen=%s weak_ai=%s liq_cap=%s",
            ok_board,
            ok_rank_precheck,
            ok_weak_ai,
            ok_liq_cap,
        )
        return True and ok_board and ok_rank_precheck and ok_weak_ai and ok_liq_cap
    except Exception:
        logger.exception("[SUMMARY AI HOOK DF TRUTH PATCH] install failed")
        return bool(ok_board and ok_rank_precheck and ok_weak_ai and ok_liq_cap)


__all__ = ["install"]
