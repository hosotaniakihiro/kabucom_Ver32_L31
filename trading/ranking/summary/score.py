# ============================================================
# File   : trading/ranking/summary/score.py
# Version: Ver1.5-PRODUCTION-RANKING-SUMMARY-BEST-RANK-REPAIR
# ------------------------------------------------------------
# ranking summary 用 score column 補完モジュール
# ------------------------------------------------------------
# ✔ score / score_total / final_score / display_score を保証
# ✔ score_buy / score_sell を保証
# ✔ ranking_score / rank_score 等から base score を復元
# ✔ 補修元score列が無い場合は No / Rank / ranking_rank / best_rank など順位列から score を生成
# ✔ RANKING_PRE_FILTER が見る ranking_score / ranking_momentum も0埋めから復元
# ✔ 値下がり/下降系 ranking type は SELL score として生成
# ✔ 文字列数値 / カンマ / 空文字 / <NA> を安全処理
# ✔ 重複列 DataFrame 化にも対応
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "Ver1.5-PRODUCTION-RANKING-SUMMARY-BEST-RANK-REPAIR"


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

RANK_COLUMNS = [
    "best_rank",
    "best_rank_position",
    "best_rank_agg",
    "rank_position",
    "ranking_position",
    "ranking_rank",
    "disp_ranking_rank",
    "rank",
    "Rank",
    "No",
    "no",
    "rank_no",
    "ranking_no",
    "順位",
    "AverageRanking",
]

TYPE_COLUMNS = [
    "ranking_type",
    "rank_type",
    "disp_ranking_type",
    "type",
    "Type",
    "CategoryName",
    "category_name",
    "name_type",
    "category",
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
                return _empty_float_series(df.index) if default is None else pd.Series(float(default), index=df.index, dtype="float64")
            s = s.iloc[:, 0]
        if getattr(s, "dtype", None) == object:
            s = (
                s.astype(str)
                .str.replace(",", "", regex=False)
                .str.replace("円", "", regex=False)
                .str.strip()
                .replace({"": np.nan, "None": np.nan, "none": np.nan, "NULL": np.nan, "null": np.nan, "nan": np.nan, "NaN": np.nan, "<NA>": np.nan, "pd.NA": np.nan})
            )
        out = pd.to_numeric(s, errors="coerce")
        if not isinstance(out, pd.Series):
            out = pd.Series(out, index=df.index, dtype="float64")
        out = out.astype("float64")
        if fill:
            out = out.fillna(np.nan if default is None else float(default))
        return out
    except Exception:
        logger.exception("[RANKING SUMMARY SCORE] numeric conversion failed col=%s", col)
        return _empty_float_series(df.index) if default is None else pd.Series(float(default), index=df.index, dtype="float64")


def _text_series(df: pd.DataFrame, candidates: Iterable[str]) -> pd.Series:
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.Series(dtype="object")
    for col in candidates:
        if col not in df.columns:
            continue
        try:
            s = df[col]
            if isinstance(s, pd.DataFrame):
                if s.shape[1] == 0:
                    continue
                s = s.iloc[:, 0]
            return s.fillna("").astype(str)
        except Exception:
            continue
    return pd.Series("", index=df.index, dtype="object")


def _combine_first_numeric(df: pd.DataFrame, candidates: Iterable[str], *, default: float = 0.0) -> pd.Series:
    if df is None or not isinstance(df, pd.DataFrame):
        return pd.Series(dtype="float64")
    out = _empty_float_series(df.index)
    for col in candidates:
        if col not in df.columns:
            continue
        s = _safe_numeric_series(df, col, default=None, fill=False)
        out = out.where(out.notna(), s)
    return out.fillna(float(default)).astype("float64")


def _combine_nonzero_numeric(df: pd.DataFrame, candidates: Iterable[str], *, default: float = 0.0) -> pd.Series:
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


def _rank_derived_score(df: pd.DataFrame) -> pd.Series:
    """ranking_score列が消えている時の最後の補修。順位から 50..1 点を作る。"""
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return pd.Series(dtype="float64")
    rank_s = _empty_float_series(df.index)
    used_col = None
    for col in RANK_COLUMNS:
        if col not in df.columns:
            continue
        s = _safe_numeric_series(df, col, default=None, fill=False)
        valid = s.notna() & s.gt(0)
        if valid.any():
            rank_s = s
            used_col = col
            break
    if used_col is None:
        return _zero_float_series(df.index)

    # rank=1 -> 50, rank=50 -> 1。50超えは1点に丸める。
    score = (51.0 - rank_s).clip(lower=1.0, upper=50.0).fillna(0.0)

    type_s = _text_series(df, TYPE_COLUMNS).str.lower()
    sell_mask = type_s.str.contains("値下|下落|下降|売|sell|short|decline|down", regex=True, na=False)
    buy_mask = type_s.str.contains("値上|上昇|買|buy|long|rise|up|急増|売買高|売買代金|tick", regex=True, na=False)

    # 方向不明のランキングはBUY側の候補として扱う。SELL系だけ負値にする。
    signed = score.copy()
    signed.loc[sell_mask & ~buy_mask] = -score.loc[sell_mask & ~buy_mask]

    try:
        logger.info(
            "[RANKING SUMMARY SCORE] rank-derived repair rank_col=%s rows=%d nonzero=%d sell_like=%d buy_like=%d version=%s",
            used_col,
            len(df),
            _nonzero_count(signed),
            int(sell_mask.sum()),
            int(buy_mask.sum()),
            VERSION,
        )
    except Exception:
        pass
    return signed.astype("float64")


def _ensure_prefilter_columns(x: pd.DataFrame, final_s: pd.Series, rank_score: pd.Series) -> pd.DataFrame:
    """Summary-AI RANKING_PRE_FILTER が直接見る列も0のままにしない。"""
    try:
        final_abs = pd.to_numeric(final_s, errors="coerce").fillna(0.0).abs().astype("float64")
        rank_abs = pd.to_numeric(rank_score, errors="coerce").fillna(0.0).abs().astype("float64")
        repair_abs = final_abs.where(final_abs.ne(0.0), rank_abs)
        if "ranking_score" in x.columns:
            x["ranking_score"] = _safe_numeric_series(x, "ranking_score", default=0.0, fill=True)
            x["ranking_score"] = _repair_zero_series(x["ranking_score"], repair_abs)
        else:
            x["ranking_score"] = repair_abs
        if "ranking_momentum" in x.columns:
            x["ranking_momentum"] = _safe_numeric_series(x, "ranking_momentum", default=0.0, fill=True)
            x["ranking_momentum"] = _repair_zero_series(x["ranking_momentum"], repair_abs)
        else:
            x["ranking_momentum"] = repair_abs
        # 既存pre-filterが price_delta_pct/rank_improve/volume_delta のいずれかも見る場合に備え、
        # 明示的な変化量が全欠損でも順位由来の rank_improve だけは0から補修する。
        if "rank_improve" in x.columns:
            x["rank_improve"] = _safe_numeric_series(x, "rank_improve", default=0.0, fill=True)
            x["rank_improve"] = _repair_zero_series(x["rank_improve"], repair_abs.clip(upper=50.0))
        else:
            x["rank_improve"] = repair_abs.clip(upper=50.0)
    except Exception:
        logger.exception("[RANKING SUMMARY SCORE] prefilter column repair failed version=%s", VERSION)
    return x


def ensure_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()

    x = df.copy()
    if x.empty:
        for col in SCORE_COLUMNS:
            if col not in x.columns:
                x[col] = pd.Series(dtype="float64")
        return x

    base_score = _combine_first_numeric(x, BASE_SCORE_CANDIDATES, default=0.0)
    repair_score = _combine_nonzero_numeric(x, RANKING_REPAIR_CANDIDATES, default=0.0)
    rank_score = _rank_derived_score(x)

    # ranking_score等が無ければ順位由来scoreを採用。
    if _nonzero_count(repair_score) == 0 and _nonzero_count(rank_score) > 0:
        repair_score = rank_score

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

    final_s = pd.to_numeric(x["final_score"], errors="coerce").fillna(0.0)
    if "score_buy" in x.columns:
        x["score_buy"] = _safe_numeric_series(x, "score_buy", default=0.0, fill=True)
        x["score_buy"] = _repair_zero_series(x["score_buy"], final_s.clip(lower=0.0))
    else:
        x["score_buy"] = final_s.clip(lower=0.0)

    if "score_sell" in x.columns:
        x["score_sell"] = _safe_numeric_series(x, "score_sell", default=0.0, fill=True)
        x["score_sell"] = _repair_zero_series(x["score_sell"], (-final_s).clip(lower=0.0))
    else:
        x["score_sell"] = (-final_s).clip(lower=0.0)

    if "score_slope" in x.columns:
        x["score_slope"] = _safe_numeric_series(x, "score_slope", default=0.0, fill=True)
    else:
        x["score_slope"] = _zero_float_series(x.index)

    if "score_mtf" in x.columns:
        x["score_mtf"] = _safe_numeric_series(x, "score_mtf", default=0.0, fill=True)
    else:
        x["score_mtf"] = _zero_float_series(x.index)

    x = _ensure_prefilter_columns(x, final_s, rank_score)

    for col in SCORE_COLUMNS:
        try:
            x[col] = pd.to_numeric(x[col], errors="coerce").fillna(0.0).astype("float64")
        except Exception:
            logger.exception("[RANKING SUMMARY SCORE] final normalize failed col=%s", col)
            x[col] = _zero_float_series(x.index)

    try:
        logger.info(
            "[RANKING SUMMARY SCORE] ensured rows=%d score_nonnull=%d score_nonzero=%d final_nonnull=%d buy_nonzero=%d sell_nonzero=%d repair_nonzero=%d rank_repair_nonzero=%d ranking_score_nonzero=%d ranking_momentum_nonzero=%d version=%s",
            len(x),
            int(pd.to_numeric(x["score"], errors="coerce").notna().sum()),
            int(pd.to_numeric(x["score"], errors="coerce").fillna(0.0).ne(0).sum()),
            int(pd.to_numeric(x["final_score"], errors="coerce").notna().sum()),
            int(pd.to_numeric(x["score_buy"], errors="coerce").fillna(0.0).ne(0).sum()),
            int(pd.to_numeric(x["score_sell"], errors="coerce").fillna(0.0).ne(0).sum()),
            _nonzero_count(repair_score),
            _nonzero_count(rank_score),
            _nonzero_count(x.get("ranking_score", _zero_float_series(x.index))),
            _nonzero_count(x.get("ranking_momentum", _zero_float_series(x.index))),
            VERSION,
        )
    except Exception:
        logger.exception("[RANKING SUMMARY SCORE] ensure score log failed")

    return x


def ensure_ranking_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    return ensure_score_columns(df)


def apply_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    return ensure_score_columns(df)


def normalize_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    return ensure_score_columns(df)


__all__ = [
    "SCORE_COLUMNS",
    "BASE_SCORE_CANDIDATES",
    "ensure_score_columns",
    "ensure_ranking_score_columns",
    "apply_score_columns",
    "normalize_score_columns",
]
