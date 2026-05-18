# ============================================================
# File   : core/startup/entry_affordability_runtime_patch.py
# Version: PRODUCTION-AI-PRE-AFFORDABILITY-PATCH-V2-PRICE-RANGE-REASON
# ------------------------------------------------------------
# 目的:
#   AIに確認する前に、対象価格帯外/最低1単元でも予算上限を超える銘柄を除外する。
#
# 背景:
#   3000円以上7000円以下の銘柄にエントリーしたい。
#   以前は 50万円 / 100株 = 5000円上限として動き、5000〜7000円がAI前で落ちる可能性があった。
#
# 方針:
#   - trading.entry.entry_budget の共通設定を使う
#   - ENTRY_MIN_PRICE / ENTRY_MAX_PRICE / MAX_ENTRY_ONESHOT_YEN をログへ出す
#   - skipped理由を entry_budget 側の reason で出す
#   - candidates.build_summary_ai_entry_candidates をラップする
#   - runner.py が直接import済みの関数も差し替える
# ============================================================

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_PATCHED = False


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _pick_price_from_row(row: Any) -> float:
    try:
        for key in (
            "ai_disp_close",
            "close_price",
            "price",
            "current_price",
            "close",
            "last_price",
            "CurrentPrice",
        ):
            try:
                v = row.get(key)
            except Exception:
                v = None
            p = _safe_float(v, 0.0)
            if p > 0:
                return p
    except Exception:
        pass
    return 0.0


def _symbol_from_row(row: Any) -> str:
    try:
        s = str(row.get("symbol") or "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _filter_affordable_candidates_df(df, *, label: str):
    try:
        import pandas as pd
        from trading.entry.entry_budget import can_afford_min_lot, log_entry_budget_config
    except Exception:
        logger.exception("[ENTRY AFFORDABILITY PATCH] import failed")
        return df

    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return df

        log_entry_budget_config(prefix="[ENTRY AFFORDABILITY PATCH]")

        keep_idx: list[Any] = []
        skipped: list[dict[str, Any]] = []

        for idx, row in df.iterrows():
            price = _pick_price_from_row(row)
            ok, diag = can_afford_min_lot(price)
            if ok:
                keep_idx.append(idx)
                continue

            skipped.append(
                {
                    "symbol": _symbol_from_row(row),
                    "side": str(row.get("side") or row.get("ai_side") or ""),
                    "price": round(float(diag.get("price") or price or 0.0), 2),
                    "min_price": round(float(diag.get("min_price") or 0.0), 2),
                    "max_price": round(float(diag.get("max_price") or 0.0), 2),
                    "configured_max_price": round(float(diag.get("configured_max_price") or 0.0), 2),
                    "affordable_max_price": round(float(diag.get("affordable_max_price") or 0.0), 2),
                    "budget_yen": round(float(diag.get("budget_yen") or 0.0), 0),
                    "lot_size": int(diag.get("lot_size") or 0),
                    "min_notional": round(float(diag.get("min_notional") or 0.0), 0),
                    "reason": str(diag.get("reason") or "price_range_or_budget_ng"),
                }
            )

        if not skipped:
            return df

        out = df.loc[keep_idx].copy().reset_index(drop=True)
        logger.warning(
            "[ENTRY AFFORDABILITY PATCH] AI_PRE_FILTER label=%s before=%s after=%s skipped=%s",
            label,
            len(df),
            len(out),
            skipped[:80],
        )
        return out

    except Exception:
        logger.exception("[ENTRY AFFORDABILITY PATCH] filter failed label=%s", label)
        return df


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import trading.entry.summary_ai.candidates as cand_mod
    except Exception:
        logger.exception("[ENTRY AFFORDABILITY PATCH] candidates import failed")
        return False

    old_builder = getattr(cand_mod, "build_summary_ai_entry_candidates", None)
    if not callable(old_builder):
        logger.warning("[ENTRY AFFORDABILITY PATCH] build_summary_ai_entry_candidates not callable")
        return False

    if getattr(old_builder, "_entry_affordability_wrapped", False):
        _PATCHED = True
        return True

    def _build_summary_ai_entry_candidates_affordable(*args: Any, **kwargs: Any):
        df = old_builder(*args, **kwargs)
        return _filter_affordable_candidates_df(df, label="summary_ai_candidates_after_build_before_ai_gate")

    _build_summary_ai_entry_candidates_affordable._entry_affordability_wrapped = True  # type: ignore[attr-defined]
    _build_summary_ai_entry_candidates_affordable._original_builder = old_builder  # type: ignore[attr-defined]

    cand_mod.build_summary_ai_entry_candidates = _build_summary_ai_entry_candidates_affordable

    # runner.py は from .candidates import build_summary_ai_entry_candidates で関数を保持しているため、
    # runner 側の参照も差し替える。
    try:
        import trading.entry.summary_ai.runner as runner_mod
        setattr(runner_mod, "build_summary_ai_entry_candidates", _build_summary_ai_entry_candidates_affordable)
        logger.warning("[ENTRY AFFORDABILITY PATCH] runner.build_summary_ai_entry_candidates patched")
    except Exception:
        logger.debug("[ENTRY AFFORDABILITY PATCH] runner patch skipped", exc_info=True)

    _PATCHED = True
    logger.warning("[ENTRY AFFORDABILITY PATCH] installed ai_pre_affordability_filter=True price_range_reason=True")
    return True


try:
    install()
except Exception:
    logger.exception("[ENTRY AFFORDABILITY PATCH] auto install failed")
