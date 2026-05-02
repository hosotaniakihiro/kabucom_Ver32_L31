# ============================================================
# File   : trading/scoring/core/scoring_warmup_base.py
# Version: 1.0-FINAL-WARMUP-STABLE-NUMERIC-SAFE
# ------------------------------------------------------------
# ✔ 起動直後 SEED_ONLY 用 疑似スコア生成
# ✔ ranking / push 非依存
# ✔ 数値のみ（dict混入完全禁止）
# ✔ score_total / score_buy / score_sell / score_reasons 完全生成
# ✔ 1min / 3min / 5min 全対応
# ✔ 欠損 / ゼロ除算 完全防御
# ============================================================

from __future__ import annotations

import numpy as np
import pandas as pd


# ============================================================
# 安全な正規化関数
# ============================================================

def _safe_div(a, b):
    return a / np.where(b == 0, np.nan, b)


# ============================================================
# warmup scoring
# ============================================================

def scoring_warmup_base(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    df = df.copy()

    # --------------------------------------------------------
    # スコア初期化
    # --------------------------------------------------------
    score = pd.Series(0.0, index=df.index)

    # ========================================================
    # 1️⃣ MAクロス方向
    # ========================================================
    if {"ma5", "ma25"}.issubset(df.columns):
        ma_diff = (df["ma5"] - df["ma25"]).fillna(0)
        score += np.sign(ma_diff) * 2.0

    # ========================================================
    # 2️⃣ RSI 位置
    # ========================================================
    if "rsi" in df.columns:
        rsi = df["rsi"].fillna(50)
        score += (rsi - 50.0) / 10.0  # ±5程度

    # ========================================================
    # 3️⃣ MACD ヒストグラム
    # ========================================================
    if "macd_hist" in df.columns:
        macd_hist = df["macd_hist"].fillna(0)
        score += macd_hist

    # ========================================================
    # 4️⃣ ボリンジャーバンド位置
    # ========================================================
    if {"close", "bb_upper", "bb_lower"}.issubset(df.columns):
        width = (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)
        pos = _safe_div(df["close"] - df["bb_lower"], width)
        pos = pos.clip(0, 1).fillna(0.5)
        score += (pos - 0.5) * 4.0  # ±2

    # ========================================================
    # 5️⃣ 出来高ブースト（急増検知）
    # ========================================================
    if "volume" in df.columns:
        vol = df["volume"].fillna(0)
        vol_mean = vol.rolling(20, min_periods=1).mean()
        vol_ratio = _safe_div(vol, vol_mean).fillna(1.0)
        score += (vol_ratio - 1.0).clip(-1.5, 3.0)

    # ========================================================
    # 6️⃣ ATR ブレイク補正
    # ========================================================
    if {"close", "atr"}.issubset(df.columns):
        atr = df["atr"].replace(0, np.nan)
        atr_ratio = _safe_div(df["close"].diff().abs(), atr).fillna(0)
        score += atr_ratio.clip(0, 2)

    # ========================================================
    # スコア正規化
    # ========================================================
    score = score.fillna(0.0)

    # 極端値制限
    score = score.clip(-15, 15)

    df["score_total"] = score.astype(float)

    # ========================================================
    # BUY / SELL 分離（数値のみ）
    # ========================================================
    df["score_buy"] = df["score_total"].apply(
        lambda x: float(x) if x > 0 else 0.0
    )

    df["score_sell"] = df["score_total"].apply(
        lambda x: float(-x) if x < 0 else 0.0
    )

    # ========================================================
    # 理由（固定文字列）
    # ========================================================
    df["score_reasons"] = "warmup"

    return df