# ============================================================
# summary_ai_evaluator.py
# (Ver27-FINAL-SUMMARY-AI-EVALUATOR)
# ------------------------------------------------------------
# ✔ SUMMARY 専用 AI 評価ロジック
# ✔ buy_score / sell_score を付与（非破壊）
# ✔ reason / reason_scores 完全対応
# ✔ ENTRY 判定とは完全分離
# ✔ calculator.py Ver27 / analysis_logger 完全対応
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# メイン API
# ============================================================
def evaluate_summary_ai(
    summary_df: pd.DataFrame,
    *,
    enable_sell: bool = False,
) -> pd.DataFrame:
    """
    SUMMARY DataFrame に AI 的な buy_score / sell_score を付与する

    Parameters
    ----------
    summary_df : DataFrame
        calculate_summary() の戻り値
    enable_sell : bool
        sell_score も評価する場合 True

    Returns
    -------
    DataFrame
        score / reasons が付与された summary_df（copy）
    """

    if summary_df is None or summary_df.empty:
        return summary_df

    df = summary_df.copy()

    # --------------------------------------------------------
    # 必須カラムチェック
    # --------------------------------------------------------
    required_cols = [
        "close_price",
        "volume",
        "ma75",
        "indicator_ready",
    ]

    for c in required_cols:
        if c not in df.columns:
            logger.warning(f"[SUMMARY_AI] missing column: {c}")
            return df

    # --------------------------------------------------------
    # 初期化（calculator.py で作られていない場合の保険）
    # --------------------------------------------------------
    for col, default in [
        ("buy_score", 0.0),
        ("sell_score", 0.0),
        ("buy_reasons", ""),
        ("sell_reasons", ""),
        ("buy_reason_scores", None),
        ("sell_reason_scores", None),
    ]:
        if col not in df.columns:
            df[col] = default

    if "buy_reason_scores" in df.columns:
        df["buy_reason_scores"] = df["buy_reason_scores"].apply(
            lambda x: x if isinstance(x, dict) else {}
        )

    if "sell_reason_scores" in df.columns:
        df["sell_reason_scores"] = df["sell_reason_scores"].apply(
            lambda x: x if isinstance(x, dict) else {}
        )

    # ========================================================
    # BUY 評価ロジック（SUMMARY 用）
    # ========================================================
    for idx, r in df.iterrows():
        if not bool(r.get("indicator_ready", False)):
            continue

        score = 0.0
        reasons = []
        reason_scores = {}

        close_price = r.get("close_price")
        ma5 = r.get("ma5")
        ma25 = r.get("ma25")
        ma75 = r.get("ma75")
        rsi = r.get("rsi")
        volume = r.get("volume")

        # ----------------------------------------------------
        # MA 配列（トレンド）
        # ----------------------------------------------------
        if pd.notna(ma5) and pd.notna(ma25) and pd.notna(ma75):
            if ma5 > ma25 > ma75:
                score += 2.0
                reasons.append("MA_順上昇")
                reason_scores["ma_trend"] = 2.0

        # ----------------------------------------------------
        # 終値が MA75 より上
        # ----------------------------------------------------
        if pd.notna(close_price) and pd.notna(ma75):
            if close_price > ma75:
                score += 1.5
                reasons.append("C>MA75")
                reason_scores["price_above_ma75"] = 1.5

        # ----------------------------------------------------
        # RSI 過熱していない
        # ----------------------------------------------------
        if pd.notna(rsi):
            if 40 <= rsi <= 65:
                score += 1.0
                reasons.append("RSI適正")
                reason_scores["rsi_ok"] = 1.0
            elif rsi > 75:
                score -= 1.0
                reasons.append("RSI過熱")
                reason_scores["rsi_overheat"] = -1.0

        # ----------------------------------------------------
        # 出来高
        # ----------------------------------------------------
        if pd.notna(volume):
            if volume >= 1_000_000:
                score += 0.5
                reasons.append("出来高十分")
                reason_scores["volume_ok"] = 0.5

        # ----------------------------------------------------
        # スコア反映
        # ----------------------------------------------------
        df.at[idx, "buy_score"] = round(score, 2)
        df.at[idx, "buy_reasons"] = ",".join(reasons)
        df.at[idx, "buy_reason_scores"] = reason_scores

    # ========================================================
    # SELL 評価（オプション）
    # ========================================================
    if enable_sell:
        for idx, r in df.iterrows():
            if not bool(r.get("indicator_ready", False)):
                continue

            score = 0.0
            reasons = []
            reason_scores = {}

            close_price = r.get("close_price")
            ma25 = r.get("ma25")
            rsi = r.get("rsi")

            if pd.notna(close_price) and pd.notna(ma25):
                if close_price < ma25:
                    score += 1.5
                    reasons.append("C<MA25")
                    reason_scores["price_below_ma25"] = 1.5

            if pd.notna(rsi) and rsi >= 75:
                score += 1.0
                reasons.append("RSI過熱")
                reason_scores["rsi_overheat"] = 1.0

            df.at[idx, "sell_score"] = round(score, 2)
            df.at[idx, "sell_reasons"] = ",".join(reasons)
            df.at[idx, "sell_reason_scores"] = reason_scores

    return df