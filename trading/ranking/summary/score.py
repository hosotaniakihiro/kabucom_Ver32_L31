# ============================================================
# File   : trading/ranking/summary/score.py
# Version: Ver1.3-PRODUCTION-RANKING-SUMMARY-SCORE-REPAIR-ZERO
# ------------------------------------------------------------
# ranking summary 用 score column 補完モジュール
# ------------------------------------------------------------
# ✔ pd.Series(pd.NA, dtype="float64") を全面廃止
# ✔ np.nan ベースで float64 初期化
# ✔ score / score_total / final_score / display_score を保証
# ✔ score_buy / score_sell を保証
# ✔ score_slope / score_mtf を保証
# ✔ ranking_score / rank_score 等の候補列から base score を復元
# ✔ 既存 score 系列が全0なら ranking_score などの非ゼロ候補から修復
# ✔ 文字列数値 / カンマ / 空文字 / <NA> を安全処理
# ✔ 重複列 DataFrame 化にも対応
# ✔ 機能削除ゼロ
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


SCORE_COLUMNS = [
    "score",
    "score_total",
    "final_score",
    "display_score",
    "score_buy",
    "score_sell",
    "score_slope",
    "score_mtf",
]


BASE_SCORE_CANDIDATES = [
    "score",
    "score_total",
    "final_score",
    "display_score",
    "ranking_score",
    "rank_score",
    "ranking_momentum",
    "rank_momentum",
    "summary_score",
    "total_score",
    "score_rank_base",
    "best_rank_score",
    "rank_score_base",
]

RANKING_REPAIR_CANDIDATES = [
    "ranking_score",
    "rank_score",
    "ranking_momentum",
    "rank_momentum",
    "summary_score",
    "score_rank_base",
    "best_rank_score",
    "rank_score_base",
]


def _empty_float_series(index=None) -> pd.Series:
    return pd.Series(np.nan, index=index, dtype="float64")


def _zero_float_series(index=None) -> pd.Series:
    return pd.Series(0.0, index=index, dtype="float64")


def _safe_numeric_series(
    df: pd.DataFrame,
    col: str,
    *,
    default: float | None = None,
    fill: bool = False,
) -> pd.Series:
    """
    df[col] を安全に float64 Series に変換する。

    - 列が存在しない場合も scalar ではなく Series を返す
    - pd.NA + dtype=float64 問題を避ける
    - 重複列により DataFrame が返る場合は先頭列を採用
    - カンマ付き数値・空文字・<NA> を安全に処理する
    """
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.Series(dtype="float64")

    if col not in df.columns:
        if default is None:
            return _empty_float_series(df.index)
        return pd.Series(float(default), index=df.index, dtype="float64")

    try:
        s = df[col]

        if isinstance(s, pd.DataFrame):
            if s.shape[1] == 0:
                if default is None:
                    return _empty_float_series(df.index)
                return pd.Series(float(default), index=df.index, dtype="float64")
            s = s.iloc[:, 0]

        if getattr(s, "dtype", None) == object:
            s = (
                s.astype(str)
                .str.replace(",", "", regex=False)
                .str.replace("円", "", regex=False)
                .str.strip()
                .replace(
                    {
                        "": np.nan,
                        "None": np.nan,
                        "none": np.nan,
                        "NULL": np.nan,
                        "null": np.nan,
                        "nan": np.nan,
                        "NaN": np.nan,
                        "<NA>": np.nan,
                        "pd.NA": np.nan,
                    }
                )
            )

        out = pd.to_numeric(s, errors="coerce")

        if not isinstance(out, pd.Series):
            out = pd.Series(out, index=df.index, dtype="float64")

        out = out.astype("float64")

        if fill:
            if default is None:
                out = out.fillna(np.nan)
            else:
                out = out.fillna(float(default))

        return out

    except Exception:
        logger.exception("[RANKING SUMMARY SCORE] numeric conversion failed col=%s", col)
        if default is None:
            return _empty_float_series(df.index)
        return pd.Series(float(default), index=df.index, dtype="float64")


def _combine_first_numeric(
    df: pd.DataFrame,
    candidates: Iterable[str],
    *,
    default: float = 0.0,
) -> pd.Series:
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.Series(dtype="float64")

    out = _empty_float_series(df.index)

    for col in candidates:
        if col not in df.columns:
            continue

        s = _safe_numeric_series(df, col, default=None, fill=False)
        out = out.where(out.notna(), s)

    return out.fillna(float(default)).astype("float64")


def _combine_nonzero_numeric(
    df: pd.DataFrame,
    candidates: Iterable[str],
    *,
    default: float = 0.0,
) -> pd.Series:
    """0/NaNをスキップして、最初の非ゼロ候補を採用する。"""
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.Series(dtype="float64")

    out = pd.Series(float(default), index=df.index, dtype="float64")
    used = pd.Series(False, index=df.index, dtype="bool")

    for col in candidates:
        if col not in df.columns:
            continue
        s = _safe_numeric_series(df, col, default=None, fill=False)
        mask = (~used) & s.notna() & s.fillna(0.0).ne(0.0)
        if mask.any():
            out.loc[mask] = s.loc[mask].astype("float64")
            used.loc[mask] = True

    return out.astype("float64")


def _nonzero_count(s: pd.Series) -> int:
    try:
        return int(pd.to_numeric(s, errors="coerce").fillna(0.0).ne(0.0).sum())
    except Exception:
        return 0


def _repair_zero_series(current: pd.Series, repair: pd.Series) -> pd.Series:
    cur = pd.to_numeric(current, errors="coerce").fillna(0.0).astype("float64")
    rep = pd.to_numeric(repair, errors="coerce").fillna(0.0).astype("float64")
    mask = cur.eq(0.0) & rep.ne(0.0)
    if mask.any():
        cur.loc[mask] = rep.loc[mask]
    return cur.astype("float64")


def ensure_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    ranking summary 用の score 系列を安全に補完する。

    保証する列:
      - score
      - score_total
      - final_score
      - display_score
      - score_buy
      - score_sell
      - score_slope
      - score_mtf
    """
    if df is None:
        return pd.DataFrame()

    x = df.copy()

    if x.empty:
        for col in SCORE_COLUMNS:
            if col not in x.columns:
                x[col] = pd.Series(dtype="float64")
        return x

    base_score = _combine_first_numeric(
        x,
        BASE_SCORE_CANDIDATES,
        default=0.0,
    )
    repair_score = _combine_nonzero_numeric(
        x,
        RANKING_REPAIR_CANDIDATES,
        default=0.0,
    )

    if "score" in x.columns:
        x["score"] = _safe_numeric_series(x, "score", default=0.0, fill=True)
        x["score"] = _repair_zero_series(x["score"], repair_score)
    else:
        x["score"] = _repair_zero_series(base_score, repair_score)

    if "score_total" in x.columns:
        x["score_total"] = _safe_numeric_series(x, "score_total", default=0.0, fill=True)
        x["score_total"] = _repair_zero_series(x["score_total"], x["score"])
    else:
        x["score_total"] = x["score"]

    if "final_score" in x.columns:
        x["final_score"] = _safe_numeric_series(x, "final_score", default=0.0, fill=True)
        x["final_score"] = _repair_zero_series(x["final_score"], x["score_total"])
    else:
        x["final_score"] = x["score_total"]

    if "display_score" in x.columns:
        x["display_score"] = _safe_numeric_series(x, "display_score", default=0.0, fill=True)
        x["display_score"] = _repair_zero_series(x["display_score"], x["final_score"])
    else:
        x["display_score"] = x["final_score"]

    # 既存 score_buy / score_sell が全0のまま存在する場合、final_scoreから復元する。
    if "score_buy" in x.columns:
        x["score_buy"] = _safe_numeric_series(x, "score_buy", default=0.0, fill=True)
        x["score_buy"] = _repair_zero_series(
            x["score_buy"],
            pd.to_numeric(x["final_score"], errors="coerce").fillna(0.0).clip(lower=0.0),
        )
    else:
        x["score_buy"] = pd.to_numeric(x["final_score"], errors="coerce").fillna(0.0).clip(lower=0.0)

    if "score_sell" in x.columns:
        x["score_sell"] = _safe_numeric_series(x, "score_sell", default=0.0, fill=True)
        x["score_sell"] = _repair_zero_series(
            x["score_sell"],
            (-pd.to_numeric(x["final_score"], errors="coerce").fillna(0.0)).clip(lower=0.0),
        )
    else:
        x["score_sell"] = (-pd.to_numeric(x["final_score"], errors="coerce").fillna(0.0)).clip(lower=0.0)

    if "score_slope" in x.columns:
        x["score_slope"] = _safe_numeric_series(x, "score_slope", default=0.0, fill=True)
    else:
        x["score_slope"] = _zero_float_series(x.index)

    if "score_mtf" in x.columns:
        x["score_mtf"] = _safe_numeric_series(x, "score_mtf", default=0.0, fill=True)
    else:
        x["score_mtf"] = _zero_float_series(x.index)

    for col in SCORE_COLUMNS:
        try:
            x[col] = pd.to_numeric(x[col], errors="coerce").fillna(0.0).astype("float64")
        except Exception:
            logger.exception("[RANKING SUMMARY SCORE] final normalize failed col=%s", col)
            x[col] = _zero_float_series(x.index)

    try:
        logger.info(
            "[RANKING SUMMARY SCORE] ensured rows=%d score_nonnull=%d score_nonzero=%d final_nonnull=%d buy_nonzero=%d sell_nonzero=%d repair_nonzero=%d",
            len(x),
            int(pd.to_numeric(x["score"], errors="coerce").notna().sum()),
            int(pd.to_numeric(x["score"], errors="coerce").fillna(0.0).ne(0).sum()),
            int(pd.to_numeric(x["final_score"], errors="coerce").notna().sum()),
            int(pd.to_numeric(x["score_buy"], errors="coerce").fillna(0.0).ne(0).sum()),
            int(pd.to_numeric(x["score_sell"], errors="coerce").fillna(0.0).ne(0).sum()),
            _nonzero_count(repair_score),
        )
    except Exception:
        logger.exception("[RANKING SUMMARY SCORE] ensure score log failed")

    return x


def ensure_ranking_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    互換用 alias。
    旧コードが ensure_ranking_score_columns を import していても壊さない。
    """
    return ensure_score_columns(df)


def apply_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    互換用 alias。
    """
    return ensure_score_columns(df)


def normalize_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    互換用 alias。
    """
    return ensure_score_columns(df)


__all__ = [
    "SCORE_COLUMNS",
    "BASE_SCORE_CANDIDATES",
    "ensure_score_columns",
    "ensure_ranking_score_columns",
    "apply_score_columns",
    "normalize_score_columns",
]
