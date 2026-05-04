# ============================================================
# scoring/state_tracker.py
# ------------------------------------------------------------
# ・symbol × signal_key のイベント発火管理
# ・再許可条件（高値更新 / 安値更新 / トレンド崩壊）対応
# ・ENTRY 系イベント専用（BONUS では使用しない）
# ・BUY / SELL 完全共通
# ============================================================

import threading
from typing import Dict, Tuple


class SignalState:
    """
    イベント型シグナルの発火状態を管理するクラス

    管理キー:
        (symbol, signal_key)

    用途:
        ・BUY / SELL ENTRY の「初回のみ」制御
        ・高値更新・安値更新・トレンドリセット等による再ENTRY許可
        ・BONUS 系シグナルでは使用しない
    """

    def __init__(self):
        # {(symbol, signal_key): True}
        self._state: Dict[Tuple[str, str], bool] = {}
        self._lock = threading.Lock()

    # --------------------------------------------------------
    # 🔥 初回 or 再許可判定
    # --------------------------------------------------------
    def is_first(
        self,
        symbol: str,
        key: str,
        *,
        allow_reentry: bool = False,
        reentry_condition: bool = False,
    ) -> bool:
        """
        初回発火 or 再許可条件付き発火を判定する

        Parameters
        ----------
        symbol : str
            銘柄コード
        key : str
            シグナルキー（ma5_ma25_cross 等）
        allow_reentry : bool
            再発火ロジックを有効にするか
        reentry_condition : bool
            高値更新・安値更新・トレンドリセット等の再許可条件

        Returns
        -------
        bool
            True  : 今回のバーで発火してよい
            False : 発火不可
        """
        if not symbol or not key:
            return False

        k = (str(symbol), str(key))

        with self._lock:
            # ----------------------------
            # 初回発火
            # ----------------------------
            if k not in self._state:
                self._state[k] = True
                return True

            # ----------------------------
            # 再ENTRY許可
            # ----------------------------
            if allow_reentry and reentry_condition:
                # 状態は保持したまま再発火を許可
                return True

            return False

    # --------------------------------------------------------
    # 🔄 個別リセット
    # --------------------------------------------------------
    def reset(self, symbol: str, key: str):
        """特定シグナルのみリセット"""
        if not symbol or not key:
            return

        with self._lock:
            self._state.pop((str(symbol), str(key)), None)

    # --------------------------------------------------------
    # 🔄 銘柄単位リセット
    # --------------------------------------------------------
    def reset_symbol(self, symbol: str):
        """
        銘柄に紐づく全イベントをリセット
        （引け後 / トレード完了後 / 日次切替など）
        """
        if not symbol:
            return

        symbol = str(symbol)

        with self._lock:
            for k in list(self._state.keys()):
                if k[0] == symbol:
                    self._state.pop(k, None)

    # --------------------------------------------------------
    # 🔄 全リセット
    # --------------------------------------------------------
    def reset_all(self):
        """
        全イベント状態をクリア
        （営業日切替 / 強制リセット用）
        """
        with self._lock:
            self._state.clear()

    # --------------------------------------------------------
    # 🔍 デバッグ用
    # --------------------------------------------------------
    def dump(self) -> Dict[Tuple[str, str], bool]:
        """現在の状態をコピーして返す（デバッグ用）"""
        with self._lock:
            return dict(self._state)


# ============================================================
# 🔥 グローバルインスタンス
# ============================================================
signal_state = SignalState()
