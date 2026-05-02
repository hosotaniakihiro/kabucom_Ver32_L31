# ============================================================
# File   : trading/exit/collapse_engine.py
# Version: V2.1-ULTRA-HARDENED-V60-READY
# ------------------------------------------------------------
# ✔ V2.0 機能完全保持（削除ゼロ）
# ✔ Tick collapse統合
# ✔ Pre-collapse AI統合
# ✔ Regime collapse AI統合
# ✔ Collapse fusion統合
# ✔ PositionScaler統合
# ✔ ReentryEngine連携フック
# ✔ Bandit重み差し替え対応
# ✔ フェイルセーフ設計
# ✔ exit_loop高頻度呼び出し前提
# ✔ NaN / inf 完全防御
# ✔ regime型安全化
# ✔ 数値保証
# ✔ 再入耐性強化
# ✔ Thread完全安全
# ✔ 将来cluster拡張耐性
# ✔ attribution出力追加（後方互換）
# ✔ volatility正規化オプション（既定OFF）
# ✔ V60互換フック（無効時従来動作）
# ============================================================

from __future__ import annotations

import logging
import math
from threading import Lock

from trading.tick.tick_collapse_detector import detect_tick_collapse
from AI.collapse.collapse_fusion import fuse_collapse_scores

logger = logging.getLogger(__name__)


# ============================================================
# 数値安全化
# ============================================================

def _safe(v, default=0.0):
    try:
        if v is None:
            return default
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


# ============================================================
# CollapseEngine
# ============================================================

class CollapseEngine:
    """
    崩壊検知統合エンジン（高頻度安全版 / V60準備済）
    """

    # ============================================================
    # 初期化
    # ============================================================

    def __init__(
        self,
        tick_state_cache,
        pre_model,
        regime_model,
        position_scaler,
        reentry_engine=None,
        fusion_weights=(0.4, 0.3, 0.3),
        use_volatility_normalization=False,  # ★追加（既定False）
    ):

        self.tick_state_cache = tick_state_cache
        self.pre_model = pre_model
        self.regime_model = regime_model
        self.position_scaler = position_scaler
        self.reentry_engine = reentry_engine

        self.fusion_weights = tuple(_safe(w) for w in fusion_weights)
        self.use_volatility_normalization = bool(use_volatility_normalization)

        self._collapse_exit_flag = {}
        self._lock = Lock()

    # ============================================================
    # メイン評価
    # ============================================================

    def evaluate(
        self,
        symbol: str,
        regime,
        pre_feature_dict: dict,
        regime_feature_dict: dict,
        volatility: float | None = None,  # ★追加（後方互換）
        cluster_id: int | None = None,    # ★将来拡張用（未使用でも安全）
    ):
        """
        戻り値（後方互換維持 + attribution追加）:

        {
            "strength": float,
            "exit_ratio": float,
            "tick_strength": float,
            "pre_strength": float,
            "regime_strength": float,
            "attribution": {
                "tick": float,
                "pre": float,
                "regime": float,
            }
        }
        """

        try:

            # ----------------------------------------------------
            # regime型安全化
            # ----------------------------------------------------
            if regime is None:
                regime = 0

            # cluster_id は将来用（今は未使用だが保持）
            cluster_id = cluster_id if cluster_id is not None else 0

            # ----------------------------------------------------
            # ① Tick collapse
            # ----------------------------------------------------
            tick_strength = 0.0
            try:
                if self.tick_state_cache:
                    ticks = self.tick_state_cache.get_all(symbol)
                    if ticks:
                        tick_strength = _safe(
                            detect_tick_collapse(ticks)
                        )
            except Exception:
                logger.exception("[CollapseEngine] tick collapse error")

            # ----------------------------------------------------
            # ② Pre-collapse
            # ----------------------------------------------------
            pre_strength = 0.0
            try:
                if self.pre_model:
                    pre_strength = _safe(
                        self.pre_model.predict(pre_feature_dict or {})
                    )
            except Exception:
                logger.exception("[CollapseEngine] pre model error")

            # ----------------------------------------------------
            # ③ Regime collapse
            # ----------------------------------------------------
            regime_strength = 0.0
            try:
                if self.regime_model:
                    regime_strength = _safe(
                        self.regime_model.predict(
                            regime,
                            regime_feature_dict or {},
                        )
                    )
            except Exception:
                logger.exception("[CollapseEngine] regime model error")

            # ----------------------------------------------------
            # ④ Fusion
            # ----------------------------------------------------
            try:
                final_strength = _safe(
                    fuse_collapse_scores(
                        tick_strength=tick_strength,
                        pre_strength=pre_strength,
                        regime_strength=regime_strength,
                        weights=self.fusion_weights,
                    )
                )
            except Exception:
                logger.exception("[CollapseEngine] fusion error")
                final_strength = 0.0

            # ----------------------------------------------------
            # ⑤ Volatility 正規化（既定OFF）
            # ----------------------------------------------------
            if self.use_volatility_normalization:
                vol = _safe(volatility, 0.0)
                final_strength = _safe(
                    final_strength / (1.0 + vol)
                )

            # ----------------------------------------------------
            # ⑥ Position scaling
            # ----------------------------------------------------
            exit_ratio = 0.0
            try:
                if self.position_scaler:
                    exit_ratio = _safe(
                        self.position_scaler.calculate_exit_ratio(
                            symbol=symbol,
                            collapse_strength=final_strength,
                        )
                    )
            except Exception:
                logger.exception("[CollapseEngine] scaler error")

            # ----------------------------------------------------
            # ⑦ collapse起因exitフラグ管理
            # ----------------------------------------------------
            if exit_ratio > 0:
                with self._lock:
                    self._collapse_exit_flag[symbol] = True

                if self.reentry_engine:
                    try:
                        self.reentry_engine.register_collapse_exit(symbol)
                    except Exception:
                        logger.exception(
                            "[CollapseEngine] reentry register error"
                        )

            # ----------------------------------------------------
            # attribution（新規追加・後方互換）
            # ----------------------------------------------------
            attribution = {
                "tick": tick_strength,
                "pre": pre_strength,
                "regime": regime_strength,
            }

            return {
                "strength": final_strength,
                "exit_ratio": exit_ratio,
                "tick_strength": tick_strength,
                "pre_strength": pre_strength,
                "regime_strength": regime_strength,
                "attribution": attribution,
            }

        except Exception:
            logger.exception("[CollapseEngine] evaluate fatal")
            return {
                "strength": 0.0,
                "exit_ratio": 0.0,
                "tick_strength": 0.0,
                "pre_strength": 0.0,
                "regime_strength": 0.0,
                "attribution": {},
            }

    # ============================================================
    # collapse起因exit確認
    # ============================================================

    def was_collapse_exit(self, symbol: str) -> bool:
        with self._lock:
            return bool(self._collapse_exit_flag.get(symbol, False))

    # ============================================================
    # ポジション終了時リセット
    # ============================================================

    def reset_symbol(self, symbol: str):

        with self._lock:
            self._collapse_exit_flag.pop(symbol, None)

        try:
            if self.position_scaler:
                self.position_scaler.reset_symbol(symbol)
        except Exception:
            logger.exception("[CollapseEngine] scaler reset error")

        try:
            if self.reentry_engine:
                self.reentry_engine.reset_symbol(symbol)
        except Exception:
            logger.exception("[CollapseEngine] reentry reset error")

    # ============================================================
    # 市場終了時リセット
    # ============================================================

    def reset_all(self):

        with self._lock:
            self._collapse_exit_flag.clear()

        try:
            if self.position_scaler:
                self.position_scaler.reset_all()
        except Exception:
            logger.exception("[CollapseEngine] scaler reset_all error")

        try:
            if self.reentry_engine:
                self.reentry_engine.reset_all()
        except Exception:
            logger.exception("[CollapseEngine] reentry reset_all error")

    # ============================================================
    # 動的重み変更（Bandit用）
    # ============================================================

    def update_fusion_weights(self, new_weights: tuple):

        try:
            if (
                new_weights
                and isinstance(new_weights, tuple)
                and len(new_weights) == 3
            ):
                self.fusion_weights = tuple(
                    _safe(w, 0.0) for w in new_weights
                )
        except Exception:
            logger.exception("[CollapseEngine] weight update error")

    # ============================================================
    # デバッグ情報
    # ============================================================

    def debug_info(self):
        with self._lock:
            symbols = list(self._collapse_exit_flag.keys())

        return {
            "fusion_weights": self.fusion_weights,
            "collapse_exit_symbols": symbols,
            "volatility_normalization": self.use_volatility_normalization,
        }