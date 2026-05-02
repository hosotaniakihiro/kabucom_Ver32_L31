# ============================================================
# File   : trading/tick/tick_feature_builder.py
# Version: V1.0-FINAL-TICK-FEATURE-BUILDER-LOWLATENCY
# ------------------------------------------------------------
# ✔ collapse専用高速特徴量生成
# ✔ 1s / 3s / 5s return
# ✔ 瞬間ボラ
# ✔ spread急変
# ✔ 出来高加速
# ✔ VWAP乖離（tick内）
# ✔ 下落連続検知
# ✔ 板バランス簡易指標
# ✔ numpy高速化
# ✔ 安全例外防御
# ============================================================

import numpy as np
import logging
import time

logger = logging.getLogger(__name__)


# ============================================================
# ユーティリティ
# ============================================================

def _safe_return(p_now, p_prev):
    if p_prev == 0:
        return 0.0
    return (p_now - p_prev) / p_prev


def _calc_vwap(prices, volumes):
    vol_sum = volumes.sum()
    if vol_sum == 0:
        return prices[-1]
    return (prices * volumes).sum() / vol_sum


# ============================================================
# メイン特徴量ビルダー
# ============================================================

def build_tick_features(ticks: list):
    """
    ticks: TickStateCache.get_all() から取得した list

    tick format:
        {
            "price": float,
            "bid": float,
            "ask": float,
            "volume": float,
            "ts": epoch秒
        }
    """

    try:
        if not ticks or len(ticks) < 5:
            return None

        prices = np.array([t["price"] for t in ticks], dtype=float)
        volumes = np.array([t.get("volume", 0) for t in ticks], dtype=float)
        bids = np.array([t.get("bid") or 0 for t in ticks], dtype=float)
        asks = np.array([t.get("ask") or 0 for t in ticks], dtype=float)
        timestamps = np.array([t["ts"] for t in ticks], dtype=float)

        p_now = prices[-1]

        # ========================================================
        # 時間窓インデックス取得
        # ========================================================

        now = timestamps[-1]

        idx_1s = np.where(now - timestamps <= 1.0)[0]
        idx_3s = np.where(now - timestamps <= 3.0)[0]
        idx_5s = np.where(now - timestamps <= 5.0)[0]

        # ========================================================
        # リターン
        # ========================================================

        ret_1s = 0.0
        ret_3s = 0.0
        ret_5s = 0.0

        if len(idx_1s) > 1:
            ret_1s = _safe_return(p_now, prices[idx_1s[0]])

        if len(idx_3s) > 1:
            ret_3s = _safe_return(p_now, prices[idx_3s[0]])

        if len(idx_5s) > 1:
            ret_5s = _safe_return(p_now, prices[idx_5s[0]])

        # ========================================================
        # 瞬間ボラ（標準偏差）
        # ========================================================

        vol_1s = 0.0
        if len(idx_1s) > 2:
            vol_1s = float(np.std(prices[idx_1s]))

        # ========================================================
        # tick速度（価格変化絶対値平均）
        # ========================================================

        tick_speed = float(np.mean(np.abs(np.diff(prices)))) if len(prices) > 1 else 0.0

        # ========================================================
        # spread急変
        # ========================================================

        spread_now = 0.0
        spread_mean = 0.0
        spread_jump = 0.0

        if bids[-1] > 0 and asks[-1] > 0:
            spread_now = asks[-1] - bids[-1]

        valid_spreads = asks - bids
        valid_spreads = valid_spreads[valid_spreads > 0]

        if len(valid_spreads) > 2:
            spread_mean = float(np.mean(valid_spreads))
            if spread_mean > 0:
                spread_jump = spread_now / spread_mean

        # ========================================================
        # 出来高加速
        # ========================================================

        vol_sum_1s = volumes[idx_1s].sum() if len(idx_1s) else 0
        vol_sum_3s = volumes[idx_3s].sum() if len(idx_3s) else 0

        volume_accel = 0.0
        if vol_sum_3s > 0:
            volume_accel = vol_sum_1s / vol_sum_3s

        # ========================================================
        # VWAP乖離
        # ========================================================

        vwap = _calc_vwap(prices, volumes)
        vwap_dev = _safe_return(p_now, vwap)

        # ========================================================
        # 下落連続検知
        # ========================================================

        diff = np.diff(prices)
        down_moves = np.sum(diff < 0)
        down_ratio = down_moves / len(diff) if len(diff) > 0 else 0.0

        # ========================================================
        # 板バランス簡易指標
        # ========================================================

        book_imbalance = 0.0
        if bids[-1] > 0 and asks[-1] > 0:
            book_imbalance = (bids[-1] - asks[-1]) / p_now

        # ========================================================
        # 下落加速
        # ========================================================

        downside_pressure = abs(ret_1s) * down_ratio

        # ========================================================
        # 出力
        # ========================================================

        return {
            "ret_1s": ret_1s,
            "ret_3s": ret_3s,
            "ret_5s": ret_5s,
            "volatility_1s": vol_1s,
            "tick_speed": tick_speed,
            "spread_jump": spread_jump,
            "volume_accel": volume_accel,
            "vwap_dev": vwap_dev,
            "down_ratio": down_ratio,
            "downside_pressure": downside_pressure,
            "book_imbalance": book_imbalance,
        }

    except Exception:
        logger.exception("[TickFeatureBuilder] build failed")
        return None