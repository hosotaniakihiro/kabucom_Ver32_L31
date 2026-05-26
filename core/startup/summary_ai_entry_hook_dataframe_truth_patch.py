# ============================================================
# File   : core/startup/summary_ai_entry_hook_dataframe_truth_patch.py
# Version: V1.0-SUMMARY-AI-HOOK-DATAFRAME-TRUTH-GUARD
# ------------------------------------------------------------
# 【目的】
#   scheduler_jobs.summary.summary_ai_entry_hook_v20.run_summary_ai_entry_safe で
#
#     ValueError: The truth value of a DataFrame is ambiguous.
#
#   が出る問題を runtime patch で防止する。
#
# 【原因】
#   summary_ai runner の戻り値 dict に candidates / buy_candidates / ai_results 等が
#   pandas.DataFrame として入る場合がある。
#   hook側では以下のように or 評価しているため、DataFrame が bool 評価されて落ちる。
#
#     candidates = result_dict.get("candidates") or result_dict.get("buy_candidates") or []
#
# 【方針】
#   - summary_ai_entry_hook_v20._result_to_dict をラップする
#   - 戻り値dict内の DataFrame を records(list[dict]) に変換する
#   - list/tuple/None はそのまま安全な形に整える
#   - 元の run_summary_ai_entry_safe は変更しない
# ============================================================

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_RESULT_TO_DICT = None

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


def install() -> bool:
    global _PATCHED, _ORIGINAL_RESULT_TO_DICT

    if _PATCHED:
        return True

    try:
        import scheduler_jobs.summary.summary_ai_entry_hook_v20 as target

        cur = getattr(target, "_result_to_dict", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI HOOK DF TRUTH PATCH] target _result_to_dict not callable")
            return False
        if getattr(cur, "_summary_ai_hook_df_truth_patch", False):
            _PATCHED = True
            return True

        _ORIGINAL_RESULT_TO_DICT = cur
        _patched_result_to_dict._summary_ai_hook_df_truth_patch = True  # type: ignore[attr-defined]
        target._result_to_dict = _patched_result_to_dict

        _PATCHED = True
        logger.warning("[SUMMARY AI HOOK DF TRUTH PATCH] installed")
        return True
    except Exception:
        logger.exception("[SUMMARY AI HOOK DF TRUTH PATCH] install failed")
        return False


__all__ = ["install"]
