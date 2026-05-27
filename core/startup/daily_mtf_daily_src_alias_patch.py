# ============================================================
# File   : core/startup/daily_mtf_daily_src_alias_patch.py
# Version: V1-DAILY-MTF-DAILY-SRC-ALIAS-RESTORE
# ------------------------------------------------------------
# 目的:
#   attach_daily_ma_mtf_to_summary() が daily列既存DFへ merge した際、
#   daily側の値が daily_close_daily_src 等へ逃げ、既存の空daily_closeを
#   見て daily_hit=0 / score_mtf=0 になる問題を防ぐ。
#
# 背景:
#   2026-05-27 14:47ログ:
#     rows=47 では score_mtf=35 まで入る
#     rows=136 / 200 の履歴DFでは daily_hit=0 / score_mtf=0
#   DFに daily_close 等の空列が既にある場合、pandas merge の suffixes=("", "_daily_src") により
#   daily_df側の正しい値が daily_close_daily_src へ入り、本列 daily_close は空のまま残る。
#
# 対応:
#   trading.summary.mtf.daily_ma_mtf.attach_daily_ma_mtf_to_summary をラップし、
#   実行後に *_daily_src を本列へ優先復元して score_mtf を再計算する。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIGINAL_ATTACH = None

_DAILY_PAIRS = [
    ("daily_date", "daily_date_daily_src"),
    ("daily_close", "daily_close_daily_src"),
    ("daily_ma5", "daily_ma5_daily_src"),
    ("daily_ma25", "daily_ma25_daily_src"),
    ("daily_ma75", "daily_ma75_daily_src"),
    ("daily_ma5_slope", "daily_ma5_slope_daily_src"),
    ("daily_ma25_slope", "daily_ma25_slope_daily_src"),
    ("daily_ma75_slope", "daily_ma75_slope_daily_src"),
    ("daily_mtf_buy", "daily_mtf_buy_daily_src"),
    ("daily_mtf_sell", "daily_mtf_sell_daily_src"),
    ("daily_trend", "daily_trend_daily_src"),
]

_NUMERIC_COLS = {
    "daily_close", "daily_ma5", "daily_ma25", "daily_ma75",
    "daily_ma5_slope", "daily_ma25_slope", "daily_ma75_slope",
    "daily_mtf_buy", "daily_mtf_sell", "score_mtf", "mtf",
    "score_mtf_short", "score_mtf_daily", "score_buy", "score_sell",
}


def _num(s: Any, default: float = 0.0) -> pd.Series:
    try:
        return pd.to_numeric(s, errors="coerce").replace([float("inf"), -float("inf")], pd.NA).fillna(default)
    except Exception:
        return pd.Series(default)


def _valid_numeric(s: pd.Series) -> pd.Series:
    try:
        x = pd.to_numeric(s, errors="coerce")
        return x.notna() & (x != 0)
    except Exception:
        return pd.Series(False, index=s.index)


def _restore_daily_src_columns(df: pd.DataFrame, *, side: str = "auto", overwrite_score_mtf: bool = True) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    restored: dict[str, int] = {}

    for base, src in _DAILY_PAIRS:
        if src not in out.columns:
            continue
        if base not in out.columns:
            out[base] = out[src]
            restored[base] = len(out)
            continue

        if base in _NUMERIC_COLS:
            src_valid = _valid_numeric(out[src])
            base_valid = _valid_numeric(out[base])
            mask = src_valid & (~base_valid)
            if bool(mask.any()):
                out.loc[mask, base] = out.loc[mask, src]
                restored[base] = int(mask.sum())
        else:
            src_s = out[src].fillna("").astype(str)
            base_s = out[base].fillna("").astype(str)
            mask = (src_s.str.strip() != "") & (base_s.str.strip() == "")
            if bool(mask.any()):
                out.loc[mask, base] = out.loc[mask, src]
                restored[base] = int(mask.sum())

    for col in _NUMERIC_COLS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0.0)

    # daily_mtf_buy/sell が復元された後、score_mtf_daily/score_mtf/mtf を再計算する。
    if "daily_mtf_buy" in out.columns and "daily_mtf_sell" in out.columns:
        side_l = str(side or "auto").lower().strip()
        if side_l == "buy":
            daily_score = _num(out["daily_mtf_buy"], 0.0)
        elif side_l == "sell":
            daily_score = _num(out["daily_mtf_sell"], 0.0)
        elif "score_buy" in out.columns and "score_sell" in out.columns:
            buy = _num(out["score_buy"], 0.0)
            sell = _num(out["score_sell"], 0.0)
            daily_score = pd.Series(
                [s if sell.iloc[i] > buy.iloc[i] else b for i, (b, s) in enumerate(zip(_num(out["daily_mtf_buy"], 0.0), _num(out["daily_mtf_sell"], 0.0)))],
                index=out.index,
            )
        else:
            daily_score = _num(out["daily_mtf_buy"], 0.0)

        out["score_mtf_daily"] = daily_score.fillna(0.0)

        if "score_mtf_short" not in out.columns:
            if "score_mtf" in out.columns:
                out["score_mtf_short"] = _num(out["score_mtf"], 0.0)
            elif "mtf" in out.columns:
                out["score_mtf_short"] = _num(out["mtf"], 0.0)
            else:
                out["score_mtf_short"] = 0.0
        else:
            out["score_mtf_short"] = _num(out["score_mtf_short"], 0.0)

        if overwrite_score_mtf:
            out["score_mtf"] = _num(out["score_mtf_short"], 0.0) + _num(out["score_mtf_daily"], 0.0)
            out["mtf"] = out["score_mtf"]
            out["mtf_score"] = out["score_mtf"]

    daily_hit = int((_num(out.get("daily_close", pd.Series(0, index=out.index)), 0.0) > 0).sum()) if len(out) else 0
    score_mtf_nonzero = int((_num(out.get("score_mtf", pd.Series(0, index=out.index)), 0.0) != 0).sum()) if len(out) else 0
    if restored or daily_hit > 0:
        logger.warning(
            "[DAILY MTF SRC ALIAS PATCH] restored=%s rows=%s daily_hit=%s score_mtf_nonzero=%s",
            restored,
            len(out),
            daily_hit,
            score_mtf_nonzero,
        )
    return out


def _patched_attach_daily_ma_mtf_to_summary(summary_df: pd.DataFrame, daily_df: pd.DataFrame, *args, **kwargs) -> pd.DataFrame:
    out = _ORIGINAL_ATTACH(summary_df, daily_df, *args, **kwargs)
    try:
        side = kwargs.get("side", "auto")
        overwrite = bool(kwargs.get("overwrite_score_mtf", True))
        return _restore_daily_src_columns(out, side=side, overwrite_score_mtf=overwrite)
    except Exception:
        logger.exception("[DAILY MTF SRC ALIAS PATCH] restore failed")
        return out


def install() -> bool:
    global _PATCHED, _ORIGINAL_ATTACH
    if _PATCHED:
        return True
    try:
        import trading.summary.mtf.daily_ma_mtf as mod
        cur = getattr(mod, "attach_daily_ma_mtf_to_summary", None)
        if not callable(cur):
            logger.warning("[DAILY MTF SRC ALIAS PATCH] target attach_daily_ma_mtf_to_summary not callable")
            return False
        if getattr(cur, "_daily_mtf_src_alias_patch", False):
            _PATCHED = True
            return True
        _ORIGINAL_ATTACH = cur
        _patched_attach_daily_ma_mtf_to_summary._daily_mtf_src_alias_patch = True  # type: ignore[attr-defined]
        _patched_attach_daily_ma_mtf_to_summary._original = cur  # type: ignore[attr-defined]
        mod.attach_daily_ma_mtf_to_summary = _patched_attach_daily_ma_mtf_to_summary
        _PATCHED = True
        logger.warning("[DAILY MTF SRC ALIAS PATCH] installed")
        return True
    except Exception:
        logger.exception("[DAILY MTF SRC ALIAS PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[DAILY MTF SRC ALIAS PATCH] auto install failed")


__all__ = ["install"]
