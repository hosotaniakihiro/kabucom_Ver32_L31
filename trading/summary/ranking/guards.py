# ============================================================
# File   : trading/summary/ranking/guards.py
# Ver    : PRODUCTION-STABLE-RANKING-GUARDS-V1.0
#          -RANKING-ONLY
# ------------------------------------------------------------
# ✔ RANKINGサマリー未計算判定 guard
# ✔ PUSH系依存なし
# ✔ best_rank / score / rsi / macd / slope / hist を見る
# ============================================================

from __future__ import annotations

from typing import Any

import pandas as pd


def _safe_copy_df(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    try:
        return pd.DataFrame(value).copy()
    except Exception:
        return pd.DataFrame()


def _to_num_series(df: pd.DataFrame, col: str) -> pd.Series:
    if not isinstance(df, pd.DataFrame) or df.empty or col not in df.columns:
        return pd.Series(dtype="float64")
    try:
        return pd.to_numeric(df[col], errors="coerce")
    except Exception:
        return pd.Series(dtype="float64")


def looks_uncomputed_ranking_df(df: pd.DataFrame) -> bool:
    """
    RANKINGサマリーが「形だけあるが未計算」に見える場合 True。

    主な想定:
      - score / score_buy / score_sell が全ゼロ
      - best_rank / rank_position も全欠損
      - rsi / macd / slope / hist も全欠損または実質ゼロ
      - rows はあるが ranking summary として育っていない
    """
    out = _safe_copy_df(df)
    if out.empty:
        return True

    rows = len(out)

    score_nonzero = int((_to_num_series(out, "score").fillna(0.0) != 0).sum()) if "score" in out.columns else 0
    score_buy_nonzero = int((_to_num_series(out, "score_buy").fillna(0.0) != 0).sum()) if "score_buy" in out.columns else 0
    score_sell_nonzero = int((_to_num_series(out, "score_sell").fillna(0.0) != 0).sum()) if "score_sell" in out.columns else 0

    best_rank_nonnull = 0
    for c in ("best_rank", "best_rank_value", "rank_position", "rank"):
        if c in out.columns:
            best_rank_nonnull = max(best_rank_nonnull, int(_to_num_series(out, c).notna().sum()))

    slope_nonnull = int(_to_num_series(out, "slope").notna().sum()) if "slope" in out.columns else 0
    rsi_nonnull = int(_to_num_series(out, "rsi").notna().sum()) if "rsi" in out.columns else 0
    macd_nonnull = int(_to_num_series(out, "macd").notna().sum()) if "macd" in out.columns else 0
    hist_nonnull = 0
    for c in ("hist", "macd_hist"):
        if c in out.columns:
            hist_nonnull = max(hist_nonnull, int(_to_num_series(out, c).notna().sum()))

    slope_nonzero = int((_to_num_series(out, "slope").fillna(0.0) != 0).sum()) if "slope" in out.columns else 0
    macd_nonzero = int((_to_num_series(out, "macd").fillna(0.0) != 0).sum()) if "macd" in out.columns else 0

    cond_scores_empty = (
        score_nonzero == 0
        and score_buy_nonzero == 0
        and score_sell_nonzero == 0
    )

    cond_rank_info_empty = best_rank_nonnull == 0

    cond_indicators_empty = (
        slope_nonnull == 0
        and rsi_nonnull == 0
        and macd_nonnull == 0
        and hist_nonnull == 0
    )

    # ranking らしい情報が全くない
    if rows > 0 and cond_scores_empty and cond_rank_info_empty and cond_indicators_empty:
        return True

    # score も best_rank もないなら未成熟扱い
    if rows > 0 and cond_scores_empty and cond_rank_info_empty:
        return True

    # 指標類が完全空で、scoreもゼロ、slope/macdも動いていない
    if rows > 0 and cond_scores_empty and slope_nonzero == 0 and macd_nonzero == 0:
        return True

    return False