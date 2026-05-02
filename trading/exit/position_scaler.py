# ============================================================
# File   : trading/exit/position_scaler.py
# Version: V1.0-FINAL-COLLAPSE-POSITION-SCALER
# ------------------------------------------------------------
# ✔ collapse強度→部分決済比率変換
# ✔ ロング/ショート共通
# ✔ ヒステリシス対応
# ✔ 最小発注単位対応フック
# ✔ 全決済判定
# ✔ 安全フェイル設計
# ✔ exit_loop高頻度呼び出し前提
# ============================================================

import logging
from threading import Lock

logger = logging.getLogger(__name__)


# ============================================================
# デフォルト閾値テーブル
# ============================================================

DEFAULT_SCALE_TABLE = [
    # (threshold, ratio)
    (0.80, 1.00),  # 全決済
    (0.60, 0.75),
    (0.40, 0.50),
    (0.25, 0.25),
]


# ============================================================
# PositionScaler
# ============================================================

class PositionScaler:
    """
    collapse_strength を決済比率に変換するエンジン
    """

    def __init__(
        self,
        scale_table=None,
        hysteresis_gap: float = 0.05,
    ):
        """
        scale_table:
            [(threshold, ratio), ...]
            thresholdは降順推奨

        hysteresis_gap:
            直前strengthとの差が小さい場合は
            連続部分決済を防ぐため無視
        """

        self.scale_table = scale_table or DEFAULT_SCALE_TABLE
        self.hysteresis_gap = hysteresis_gap

        # symbolごとの直前strength記録
        self._last_strength = {}
        self._lock = Lock()

    # ============================================================
    # 強度→比率変換
    # ============================================================

    def _strength_to_ratio(self, strength: float) -> float:

        if strength <= 0:
            return 0.0

        for threshold, ratio in self.scale_table:
            if strength >= threshold:
                return ratio

        return 0.0

    # ============================================================
    # 公開API
    # ============================================================

    def calculate_exit_ratio(
        self,
        symbol: str,
        collapse_strength: float,
    ) -> float:
        """
        入力:
            symbol
            collapse_strength (0〜1)

        出力:
            決済比率（0〜1）
        """

        try:
            if collapse_strength <= 0:
                return 0.0

            collapse_strength = float(collapse_strength)

            if collapse_strength < 0:
                collapse_strength = 0.0
            if collapse_strength > 1:
                collapse_strength = 1.0

            with self._lock:

                last = self._last_strength.get(symbol, 0.0)

                # ヒステリシス: 前回との差が小さいなら無視
                if abs(collapse_strength - last) < self.hysteresis_gap:
                    return 0.0

                ratio = self._strength_to_ratio(collapse_strength)

                # 記録更新
                if ratio > 0:
                    self._last_strength[symbol] = collapse_strength

                return ratio

        except Exception:
            logger.exception("[PositionScaler] calculate_exit_ratio failed")
            return 0.0

    # ============================================================
    # 最小単位調整（任意）
    # ============================================================

    def adjust_for_min_unit(
        self,
        current_qty: int,
        ratio: float,
        min_unit: int,
    ) -> int:
        """
        実際の発注数量へ変換

        例:
            100株保有
            ratio=0.5
            min_unit=100

            → 50株だが単位未満なので100に丸める
        """

        try:
            if ratio <= 0:
                return 0

            raw_qty = int(current_qty * ratio)

            if raw_qty <= 0:
                return 0

            if min_unit <= 0:
                return raw_qty

            # 単位切り上げ
            if raw_qty < min_unit:
                return min_unit

            # 単位丸め
            adjusted = (raw_qty // min_unit) * min_unit

            return max(adjusted, min_unit)

        except Exception:
            logger.exception("[PositionScaler] adjust_for_min_unit failed")
            return 0

    # ============================================================
    # 全決済判定
    # ============================================================

    @staticmethod
    def is_full_exit(ratio: float) -> bool:
        return ratio >= 1.0

    # ============================================================
    # symbolリセット（ポジションクローズ時）
    # ============================================================

    def reset_symbol(self, symbol: str):
        with self._lock:
            if symbol in self._last_strength:
                del self._last_strength[symbol]

    # ============================================================
    # 全リセット（市場終了時）
    # ============================================================

    def reset_all(self):
        with self._lock:
            self._last_strength.clear()