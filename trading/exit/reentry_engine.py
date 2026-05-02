# ============================================================
# File   : trading/exit/reentry_engine.py
# Version: V1.0-FINAL-COLLAPSE-REENTRY-ENGINE
# ------------------------------------------------------------
# ✔ collapse起因exitのみ再エントリー対象
# ✔ AIモデル統合
# ✔ クールダウン制御
# ✔ 連続再エントリー防止
# ✔ Regimeフック対応
# ✔ 0〜1安全処理
# ✔ exit_loop高頻度呼び出し前提
# ============================================================

import time
import logging
from threading import Lock

logger = logging.getLogger(__name__)


class ReentryEngine:
    """
    崩壊後リバウンド再エントリー制御

    想定:
        - collapseでexitした銘柄のみ対象
        - AIモデルが反発確率を出す
        - クールダウン経過後のみ再参戦
    """

    def __init__(
        self,
        model,
        threshold: float = 0.80,
        cooldown_seconds: float = 60.0,
        max_reentries: int = 1,
    ):
        """
        model:
            reentry_model (predict(feature_dict) -> 0〜1)

        threshold:
            再エントリー許可スコア閾値

        cooldown_seconds:
            exit後この秒数は再参戦禁止

        max_reentries:
            同一ポジション内での最大再参戦回数
        """

        self.model = model
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self.max_reentries = max_reentries

        # symbol状態管理
        self._state = {}
        self._lock = Lock()

    # ============================================================
    # collapse exit登録
    # ============================================================

    def register_collapse_exit(self, symbol: str):
        """
        collapseでexitしたタイミングで呼ぶ
        """

        with self._lock:
            self._state[symbol] = {
                "last_exit_time": time.time(),
                "reentry_count": 0,
                "active": True,
            }

    # ============================================================
    # 再エントリー可否判定
    # ============================================================

    def should_reenter(
        self,
        symbol: str,
        feature_dict: dict,
        regime: str = None,
    ) -> bool:
        """
        feature_dict:
            崩壊後の反発特徴量

        regime:
            将来regime別制御用フック
        """

        try:
            with self._lock:
                state = self._state.get(symbol)

                if not state:
                    return False

                if not state["active"]:
                    return False

                # クールダウン確認
                elapsed = time.time() - state["last_exit_time"]
                if elapsed < self.cooldown_seconds:
                    return False

                # 再参戦回数制限
                if state["reentry_count"] >= self.max_reentries:
                    return False

            # AI推論（ロック外で実行）
            score = self.model.predict(feature_dict)

            if score is None:
                return False

            if score < 0:
                score = 0.0
            if score > 1:
                score = 1.0

            if score >= self.threshold:

                with self._lock:
                    state = self._state.get(symbol)
                    if not state:
                        return False

                    state["reentry_count"] += 1

                logger.info(
                    f"[ReentryEngine] reentry approved: "
                    f"{symbol} score={score:.3f}"
                )

                return True

            return False

        except Exception:
            logger.exception("[ReentryEngine] should_reenter failed")
            return False

    # ============================================================
    # ポジション完全終了時リセット
    # ============================================================

    def reset_symbol(self, symbol: str):
        with self._lock:
            if symbol in self._state:
                del self._state[symbol]

    # ============================================================
    # 市場終了時リセット
    # ============================================================

    def reset_all(self):
        with self._lock:
            self._state.clear()

    # ============================================================
    # デバッグ情報
    # ============================================================

    def debug_state(self, symbol: str = None):
        with self._lock:
            if symbol:
                return self._state.get(symbol)
            return dict(self._state)