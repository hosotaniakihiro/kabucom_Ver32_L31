# ============================================================
# File   : core/startup/summary_ai_entry_hook_dataframe_truth_patch.py
# Version: V1.2-FINAL-BOARD-AND-RANKING-PRECHECK-COMPAT
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
# ============================================================

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_RESULT_TO_DICT = None
_BOARD_COMPAT_PATCHED = False
_ORIGINAL_FINAL_BOARD_GUARD = None

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

    if _PATCHED:
        return True and ok_board and ok_rank_precheck

    try:
        import scheduler_jobs.summary.summary_ai_entry_hook_v20 as target

        cur = getattr(target, "_result_to_dict", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI HOOK DF TRUTH PATCH] target _result_to_dict not callable")
            return bool(ok_board and ok_rank_precheck)
        if getattr(cur, "_summary_ai_hook_df_truth_patch", False):
            _PATCHED = True
            return True and ok_board and ok_rank_precheck

        _ORIGINAL_RESULT_TO_DICT = cur
        _patched_result_to_dict._summary_ai_hook_df_truth_patch = True  # type: ignore[attr-defined]
        target._result_to_dict = _patched_result_to_dict

        _PATCHED = True
        logger.warning("[SUMMARY AI HOOK DF TRUTH PATCH] installed board_compat=%s ranking_precheck_failopen=%s", ok_board, ok_rank_precheck)
        return True and ok_board and ok_rank_precheck
    except Exception:
        logger.exception("[SUMMARY AI HOOK DF TRUTH PATCH] install failed")
        return bool(ok_board and ok_rank_precheck)


__all__ = ["install"]
