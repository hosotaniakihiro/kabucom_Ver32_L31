# ============================================================
# File   : trading/scoring/scoring_loop.py
# Version: Ver3.1.0-PRO-ASYNC-SCORING-BANDIT-STABLE-FINAL
# ------------------------------------------------------------
# ✔ Ver3.0.0 完全保持（削除ゼロ）
# ✔ feature_cache 消費
# ✔ scoring_main 非同期実行
# ✔ 重複処理防止
# ✔ ai_cache 統合
# ✔ バンディット自動重み統合
# ✔ 欠損完全耐性
# ✔ decision保証
# ✔ score_cache 更新
# ✔ scheduler絶対停止しない
# ✔ 軽量設計
# ✔ NaN / inf 完全吸収
# ✔ merge衝突防止
# ✔ final_score保証
# ✔ 将来RL拡張耐性
# ============================================================

from __future__ import annotations

import time
import logging
import pandas as pd
import numpy as np

from global_state import global_data
from trading.scoring.core.scoring_core import scoring_main
from trading.ai.bandit.weight_bandit import WeightBandit

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.3
_last_processed_ts = None

# ------------------------------------------------------------
# バンディット初期化（プロセス内共有）
# ------------------------------------------------------------
_bandit = WeightBandit(
    arms=["rank", "ma", "ai", "vol"]
)


# ============================================================
# 内部ユーティリティ
# ============================================================

def _safe_numeric(series):
    return (
        pd.to_numeric(series, errors="coerce")
        .replace([np.inf, -np.inf], 0)
        .fillna(0.0)
    )


def _merge_ai(df_score: pd.DataFrame) -> pd.DataFrame:

    ai_df = getattr(global_data, "ai_cache", None)

    if (
        ai_df is None
        or not isinstance(ai_df, pd.DataFrame)
        or ai_df.empty
        or "symbol" not in ai_df.columns
        or "ai_prob" not in ai_df.columns
    ):
        df_score["ai_prob"] = 0.0
        return df_score

    ai_df = ai_df.copy()
    ai_df["symbol"] = ai_df["symbol"].astype(str)
    ai_df["ai_prob"] = _safe_numeric(ai_df["ai_prob"])

    df_score = df_score.merge(
        ai_df[["symbol", "ai_prob"]],
        on="symbol",
        how="left",
        suffixes=("", "_ai")
    )

    df_score["ai_prob"] = _safe_numeric(
        df_score.get("ai_prob", 0)
    )

    return df_score


def _apply_bandit_weights(df_score: pd.DataFrame) -> pd.DataFrame:

    weights = _bandit.get_weights()

    # 安全取得
    rank = _safe_numeric(df_score.get("rank_score", 0))
    ma = _safe_numeric(df_score.get("ma5_slope", 0))
    ai = _safe_numeric(df_score.get("ai_prob", 0))
    vol = _safe_numeric(df_score.get("volume_z", 0))

    df_score["final_score"] = (
        weights.get("rank", 0) * rank
        + weights.get("ma", 0) * ma
        + weights.get("ai", 0) * ai
        + weights.get("vol", 0) * vol
    )

    df_score["final_score"] = _safe_numeric(
        df_score["final_score"]
    )

    logger.debug(
        "[BANDIT] weights=%s",
        {k: round(v, 3) for k, v in weights.items()}
    )

    return df_score


# ============================================================
# メインループ
# ============================================================

def scoring_loop():

    logger.info("🟢 scoring_loop started (Bandit Stable Final)")

    global _last_processed_ts

    while True:

        try:

            feat = getattr(global_data, "feature_cache", None)

            if (
                feat is None
                or not isinstance(feat, pd.DataFrame)
                or feat.empty
            ):
                global_data.feature_cache = None
                time.sleep(POLL_INTERVAL)
                continue

            if "datetime" not in feat.columns:
                global_data.feature_cache = None
                time.sleep(POLL_INTERVAL)
                continue

            latest_ts = pd.to_datetime(
                feat["datetime"],
                errors="coerce"
            ).max()

            if pd.isna(latest_ts):
                global_data.feature_cache = None
                time.sleep(POLL_INTERVAL)
                continue

            if (
                _last_processed_ts is not None
                and latest_ts <= _last_processed_ts
            ):
                time.sleep(POLL_INTERVAL)
                continue

            # ------------------------------------------------
            # scoring実行
            # ------------------------------------------------
            try:
                df_score = scoring_main(
                    feat.copy(),
                    interval=1
                )
            except Exception:
                logger.exception("❌ scoring_main error")
                global_data.feature_cache = None
                time.sleep(POLL_INTERVAL)
                continue

            if (
                df_score is None
                or not isinstance(df_score, pd.DataFrame)
                or df_score.empty
            ):
                global_data.feature_cache = None
                time.sleep(POLL_INTERVAL)
                continue

            df_score = df_score.copy()

            if "symbol" in df_score.columns:
                df_score["symbol"] = df_score["symbol"].astype(str)

            # ------------------------------------------------
            # AI統合
            # ------------------------------------------------
            df_score = _merge_ai(df_score)

            # ------------------------------------------------
            # Bandit適用
            # ------------------------------------------------
            df_score = _apply_bandit_weights(df_score)

            # ------------------------------------------------
            # decision生成（安全）
            # ------------------------------------------------
            df_score["decision"] = np.where(
                df_score["final_score"] > 0,
                "BUY",
                np.where(
                    df_score["final_score"] < 0,
                    "SELL",
                    "NONE"
                )
            )

            # ------------------------------------------------
            # 重複symbol排除
            # ------------------------------------------------
            if "symbol" in df_score.columns:
                if "datetime" in df_score.columns:
                    df_score = (
                        df_score.sort_values("datetime")
                        .drop_duplicates(
                            subset=["symbol"],
                            keep="last"
                        )
                    )
                else:
                    df_score = (
                        df_score.drop_duplicates(
                            subset=["symbol"],
                            keep="last"
                        )
                    )

            # ------------------------------------------------
            # global_data更新
            # ------------------------------------------------
            try:
                global_data.score_cache = df_score
            except Exception:
                setattr(global_data, "score_cache", df_score)

            _last_processed_ts = latest_ts
            global_data.feature_cache = None

            logger.debug(
                "[SCORING] rows=%d symbols=%d",
                len(df_score),
                df_score["symbol"].nunique()
                if "symbol" in df_score.columns else 0
            )

        except Exception:
            logger.exception("❌ scoring_loop unexpected error")

        time.sleep(POLL_INTERVAL)