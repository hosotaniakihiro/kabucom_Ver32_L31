# ============================================================
# File   : trading/ranking/tonosama/features.py
# Version: PRODUCTION-STABLE-REV1.1-DB-COLUMN-COMPAT
# Purpose:
#   ranking_snapshot_1min から殿様イナゴ特徴量を作成
#
# 対応DB列:
#   - datetime / snapshot_time
#   - symbol
#   - current_price / price / close / close_price
#   - trading_volume / volume
#   - price_delta_1m
#   - volume_delta_1m
#   - rank / rank_position
#   - ranking_type / category / rank_type
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _pick_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _to_num(s, default: float = 0.0) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").fillna(default)


def build_ranking_tonosama_features(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    # datetime がない場合は snapshot_time を使う
    if "datetime" not in out.columns:
        if "snapshot_time" in out.columns:
            out["datetime"] = out["snapshot_time"]
        else:
            logger.warning("[RANKING TONOSAMA FEATURES] missing datetime/snapshot_time")
            return pd.DataFrame()

    if "symbol" not in out.columns:
        logger.warning("[RANKING TONOSAMA FEATURES] missing symbol")
        return pd.DataFrame()

    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out["symbol"] = out["symbol"].astype(str)
    out = out.dropna(subset=["datetime", "symbol"])

    price_col = _pick_col(out, ["current_price", "price", "close", "close_price"])
    volume_col = _pick_col(out, ["trading_volume", "volume", "出来高"])
    price_delta_col = _pick_col(out, ["price_delta_1m"])
    volume_delta_col = _pick_col(out, ["volume_delta_1m"])
    rank_col = _pick_col(out, ["rank", "rank_position", "ranking", "順位"])
    category_col = _pick_col(out, ["category", "ranking_type", "rank_type", "type", "table_name"])

    if price_col is None:
        logger.warning("[RANKING TONOSAMA FEATURES] no price column")
        return pd.DataFrame()

    out["_price"] = _to_num(out[price_col])
    out["_volume"] = _to_num(out[volume_col]) if volume_col else 0.0
    out["_rank"] = _to_num(out[rank_col], 9999.0).astype(int) if rank_col else 9999
    out["_category"] = out[category_col].astype(str) if category_col else "UNKNOWN"

    out = out.sort_values(["symbol", "datetime"])
    g = out.groupby("symbol", group_keys=False)

    # --------------------------------------------------------
    # price change
    # DBに price_delta_1m がある場合はそれを優先
    # なければ current_price の差分から作る
    # --------------------------------------------------------
    if price_delta_col:
        out["price_delta_1m"] = _to_num(out[price_delta_col])
    else:
        out["price_delta_1m"] = g["_price"].diff(1).fillna(0.0)

    out["price_change_1m_pct"] = (
        out["price_delta_1m"] / g["_price"].shift(1).replace(0, np.nan) * 100.0
    )
    out["price_change_1m_pct"] = out["price_change_1m_pct"].replace([np.inf, -np.inf], np.nan).fillna(0.0)

    out["price_change_3m_pct"] = g["_price"].pct_change(3).replace([np.inf, -np.inf], np.nan).fillna(0.0) * 100.0
    out["price_change_5m_pct"] = g["_price"].pct_change(5).replace([np.inf, -np.inf], np.nan).fillna(0.0) * 100.0

    # --------------------------------------------------------
    # volume change
    # DBに volume_delta_1m がある場合はそれを優先
    # なければ trading_volume の差分から作る
    # --------------------------------------------------------
    if volume_delta_col:
        out["volume_delta_1m"] = _to_num(out[volume_delta_col])
    else:
        out["volume_delta_1m"] = g["_volume"].diff(1).fillna(0.0)

    out["volume_delta_1m"] = out["volume_delta_1m"].clip(lower=0.0)

    vol_ma5 = g["volume_delta_1m"].transform(
        lambda x: x.rolling(5, min_periods=1).mean()
    )

    out["volume_spike_ratio"] = out["volume_delta_1m"] / vol_ma5.replace(0, np.nan)
    out["volume_spike_ratio"] = out["volume_spike_ratio"].replace([np.inf, -np.inf], np.nan).fillna(0.0)

    # --------------------------------------------------------
    # rank
    # rank は数字が小さいほど上位なので、
    # 前回rank - 今回rank がプラスなら順位上昇
    # --------------------------------------------------------
    out["rank_prev_1m"] = g["_rank"].shift(1)
    out["rank_up_speed"] = (out["rank_prev_1m"] - out["_rank"]).fillna(0).astype(int)

    # 初登場
    out["first_appearance"] = g.cumcount() == 0

    # 高値からの位置
    recent_high_5m = g["_price"].transform(
        lambda x: x.rolling(5, min_periods=1).max()
    )
    out["from_recent_high_5m_pct"] = (
        out["_price"] / recent_high_5m.replace(0, np.nan) - 1.0
    ) * 100.0
    out["from_recent_high_5m_pct"] = out["from_recent_high_5m_pct"].replace([np.inf, -np.inf], np.nan).fillna(0.0)

    # 同一時刻・同一銘柄で複数ランキングに出ているか
    out["ranking_category_count"] = (
        out.groupby(["datetime", "symbol"])["_category"]
        .transform("nunique")
        .fillna(1)
        .astype(int)
    )

    # --------------------------------------------------------
    # 殿様スコア
    # --------------------------------------------------------
    out["ranking_tonosama_raw_score"] = (
        out["price_change_1m_pct"].fillna(0.0) * 2.0
        + out["price_change_3m_pct"].fillna(0.0) * 1.5
        + out["volume_spike_ratio"].fillna(0.0) * 2.0
        + out["rank_up_speed"].clip(lower=0).fillna(0.0) * 0.15
        + out["ranking_category_count"].fillna(1.0) * 1.5
    )

    logger.info(
        "[RANKING TONOSAMA FEATURES] rows=%s price_col=%s volume_col=%s "
        "price_delta_col=%s volume_delta_col=%s rank_col=%s category_col=%s "
        "price1m_nonzero=%s volume_delta_nonzero=%s spike_nonzero=%s",
        len(out),
        price_col,
        volume_col,
        price_delta_col,
        volume_delta_col,
        rank_col,
        category_col,
        int((out["price_change_1m_pct"].abs() > 0).sum()),
        int((out["volume_delta_1m"].abs() > 0).sum()),
        int((out["volume_spike_ratio"].abs() > 0).sum()),
    )

    return out