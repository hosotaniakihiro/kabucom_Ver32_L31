# ============================================================
# trading/ranking/ranking_aggregate_builder.py
# Ver1.3-EMPTY-SAFE-RANK-COLUMN-COMPAT
# ------------------------------------------------------------
# ✔ 複数ランキング種別を symbol 単位で統合
# ✔ 出現回数 / 最良順位 / 平均順位 を完全保持
# ✔ breadth（話題性の広さ）を固定スケールで安定評価
# ✔ strength（順位の強さ）を best + avg の複合で評価
# ✔ 空結果でも symbol / ranking_score_total などの列を保証
# ✔ rank_position / rank / ranking_position / position 列名揺れを吸収
# ✔ rank filter で全落ちした場合は件数上限でfallback
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Iterable

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_MAX_RANK = 50
WEIGHT_BREADTH = 0.4
WEIGHT_STRENGTH = 0.6

OUT_COLUMNS = [
    "symbol",
    "rank_types_count",
    "best_rank",
    "avg_rank",
    "breadth_score",
    "strength_score",
    "ranking_score_total",
]


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=OUT_COLUMNS)


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _first_existing_col(df: pd.DataFrame, names: Iterable[str]) -> str | None:
    if df is None or df.empty:
        return None
    lower = {str(c).lower(): c for c in df.columns}
    for n in names:
        if n in df.columns:
            return n
        hit = lower.get(str(n).lower())
        if hit is not None:
            return hit
    return None


def build_ranking_aggregate(
    ranking_df: pd.DataFrame,
    *,
    max_rank: int = DEFAULT_MAX_RANK,
) -> pd.DataFrame:
    """
    複数ランキングを symbol 単位で統合する。

    必須相当列:
        symbol
        rank_type / ranking_type / type / category
        rank_position / rank / ranking_position / position
    """

    if ranking_df is None or ranking_df.empty:
        logger.warning("[RANKING_AGG] input empty")
        return _empty()

    symbol_col = _first_existing_col(ranking_df, ("symbol", "code", "stock_code"))
    type_col = _first_existing_col(ranking_df, ("rank_type", "ranking_type", "type", "category", "ranking_name"))
    pos_col = _first_existing_col(ranking_df, ("rank_position", "ranking_position", "rank", "position", "順位"))

    missing = []
    if symbol_col is None:
        missing.append("symbol")
    if type_col is None:
        missing.append("rank_type")
    if pos_col is None:
        missing.append("rank_position")

    if missing:
        logger.error(
            "[RANKING_AGG] missing required columns=%s actual_columns=%s",
            missing,
            list(ranking_df.columns),
        )
        return _empty()

    df = pd.DataFrame({
        "symbol": ranking_df[symbol_col].astype(str).str.strip(),
        "rank_type": ranking_df[type_col].astype(str).str.strip(),
        "rank_position": pd.to_numeric(ranking_df[pos_col], errors="coerce"),
    })

    df = df.dropna(subset=["symbol", "rank_type", "rank_position"])
    df = df[(df["symbol"] != "") & (df["rank_type"] != "")]

    if df.empty:
        logger.warning("[RANKING_AGG] no rows after normalize actual_columns=%s", list(ranking_df.columns))
        return _empty()

    max_rank = _env_int("RANKING_AGG_MAX_RANK", int(max_rank or DEFAULT_MAX_RANK))
    filtered = df[df["rank_position"] <= max_rank]

    if filtered.empty:
        if _env_bool("RANKING_AGG_FALLBACK_WHEN_FILTER_EMPTY", True):
            fallback_top_n = _env_int("RANKING_AGG_FALLBACK_TOP_N_PER_TYPE", 80)
            filtered = (
                df.sort_values(["rank_type", "rank_position"], ascending=[True, True])
                .groupby("rank_type", group_keys=False)
                .head(fallback_top_n)
                .reset_index(drop=True)
            )
            logger.warning(
                "[RANKING_AGG] no rows after rank filter max_rank=%s -> fallback top_n_per_type=%s rows=%s rank_min=%s rank_max=%s",
                max_rank,
                fallback_top_n,
                len(filtered),
                df["rank_position"].min(),
                df["rank_position"].max(),
            )
        else:
            logger.warning(
                "[RANKING_AGG] no rows after rank filter max_rank=%s rank_min=%s rank_max=%s",
                max_rank,
                df["rank_position"].min(),
                df["rank_position"].max(),
            )
            return _empty()

    df = filtered

    total_rank_types = df["rank_type"].nunique()
    if total_rank_types <= 0:
        logger.warning("[RANKING_AGG] no rank_type detected")
        return _empty()

    agg = (
        df.groupby("symbol", as_index=False)
        .agg(
            rank_types_count=("rank_type", "nunique"),
            best_rank=("rank_position", "min"),
            avg_rank=("rank_position", "mean"),
        )
    )

    if agg.empty:
        logger.warning("[RANKING_AGG] aggregation result empty")
        return _empty()

    agg["breadth_score"] = (agg["rank_types_count"] / float(total_rank_types)).clip(0.0, 1.0)

    # fallbackでrankが50を超える場合でも score が全0にならないよう、実使用上限を広げる。
    score_rank_base = max(float(max_rank), float(agg["best_rank"].max()), 1.0)
    best_norm = 1.0 - (agg["best_rank"] / score_rank_base)
    avg_norm = 1.0 - (agg["avg_rank"] / score_rank_base)

    agg["strength_score"] = (0.6 * best_norm + 0.4 * avg_norm).clip(0.0, 1.0)
    agg["ranking_score_total"] = (
        WEIGHT_BREADTH * agg["breadth_score"]
        + WEIGHT_STRENGTH * agg["strength_score"]
    )

    agg = agg.sort_values("ranking_score_total", ascending=False).reset_index(drop=True)
    agg = agg.reindex(columns=OUT_COLUMNS)

    logger.info(
        "[RANKING_AGG] aggregated symbols=%d rank_types=%d max_rank=%s score_rank_base=%.1f rank_min=%s rank_max=%s",
        len(agg),
        total_rank_types,
        max_rank,
        score_rank_base,
        df["rank_position"].min(),
        df["rank_position"].max(),
    )
    return agg
