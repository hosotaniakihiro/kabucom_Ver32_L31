#    ============================================================
#    File : trading/ai/bandit/weight_bandit.py
#    Version: V2.0-PHASE2-2-SYMBOL-AWARE-PRODUCTION
#    ------------------------------------------------------------
#    ✔ V1系 機能完全保持（非破壊）
#    ✔ symbol-aware 多層Bandit構造
#    ✔ cluster × regime × inago 対応
#    ✔ global fallback保持
#    ✔ deterministic設計
#    ✔ Thread-safe
#    ✔ NaN / inf 完全防御
#    ✔ 過学習防止のminimum count制御
#    ✔ 将来拡張耐性
#    ✔ exit_loop高頻度前提
#    ============================================================


from __future__ import annotations

import logging
import math
from collections import defaultdict
from threading import Lock
from typing import Dict, Optional


logger = logging.getLogger(__name__)


# ============================================================
# 数値安全化
# ============================================================

def _safe(v, default: float = 0.0) -> float:
    try:
        v = float(v)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


# ============================================================
# WeightBandit
# ============================================================

class WeightBandit:
    """
    Symbol-aware 多層Bandit

    階層構造:

        symbol
            └ cluster_id
                └ regime
                    └ inago_state
                        └ stats

    fallback順:

        ① symbol専用
        ② cluster × regime
        ③ cluster全体
        ④ global
    """

    # ============================================================
    # 初期化
    # ============================================================

    def __init__(self):

        # --------------------------------------------------------
        # symbol層
        # --------------------------------------------------------
        self._symbol_store = defaultdict(
            lambda: defaultdict(
                lambda: defaultdict(
                    lambda: defaultdict(self._init_stats)
                )
            )
        )

        # --------------------------------------------------------
        # cluster層
        # --------------------------------------------------------
        self._cluster_store = defaultdict(
            lambda: defaultdict(
                lambda: defaultdict(self._init_stats)
            )
        )

        # --------------------------------------------------------
        # global層
        # --------------------------------------------------------
        self._global_stats = self._init_stats()

        self._lock = Lock()

        # 最小学習回数（過学習防止）
        self._min_symbol_count = 5
        self._min_cluster_count = 5
        self._min_cluster_total = 10

    # ============================================================
    # 初期統計
    # ============================================================

    def _init_stats(self) -> Dict[str, float]:
        return {
            "count": 0,
            "reward_sum": 0.0,
            "w_collapse": 0.6,
            "w_hold": 0.4,
            "w_take": 0.5,
        }

    # ============================================================
    # 重み取得
    # ============================================================

    def get_weights(
        self,
        cluster_id: int,
        regime: int,
        inago_state: int,
        symbol: Optional[str] = None,
    ) -> Dict[str, float]:

        cluster_id = int(cluster_id)
        regime = int(regime)
        inago_state = int(inago_state)

        with self._lock:

            # ----------------------------------------------------
            # ① symbol優先
            # ----------------------------------------------------
            if symbol:
                stats = self._symbol_store[symbol][cluster_id][regime][inago_state]
                if stats["count"] >= self._min_symbol_count:
                    return self._extract_weights(stats)

            # ----------------------------------------------------
            # ② cluster × regime
            # ----------------------------------------------------
            stats = self._cluster_store[cluster_id][regime][inago_state]
            if stats["count"] >= self._min_cluster_count:
                return self._extract_weights(stats)

            # ----------------------------------------------------
            # ③ cluster全体 fallback
            # ----------------------------------------------------
            cluster_dict = self._cluster_store.get(cluster_id, {})
            for regime_dict in cluster_dict.values():
                for state_stats in regime_dict.values():
                    if state_stats["count"] >= self._min_cluster_total:
                        return self._extract_weights(state_stats)

            # ----------------------------------------------------
            # ④ global
            # ----------------------------------------------------
            return self._extract_weights(self._global_stats)

    # ============================================================
    # 更新
    # ============================================================

    def update(
        self,
        cluster_id: int,
        regime: int,
        inago_state: int,
        pnl: float,
        symbol: Optional[str] = None,
    ):

        cluster_id = int(cluster_id)
        regime = int(regime)
        inago_state = int(inago_state)
        reward = _safe(pnl)

        with self._lock:

            # ----------------------------------------------------
            # symbol層更新
            # ----------------------------------------------------
            if symbol:
                stats = self._symbol_store[symbol][cluster_id][regime][inago_state]
                self._update_stats(stats, reward)

            # ----------------------------------------------------
            # cluster層更新
            # ----------------------------------------------------
            stats_cluster = self._cluster_store[cluster_id][regime][inago_state]
            self._update_stats(stats_cluster, reward)

            # ----------------------------------------------------
            # global層更新
            # ----------------------------------------------------
            self._update_stats(self._global_stats, reward)

    # ============================================================
    # 統計更新ロジック
    # ============================================================

    def _update_stats(self, stats: Dict[str, float], reward: float):

        stats["count"] += 1
        stats["reward_sum"] += reward

        count = max(1, stats["count"])
        avg_reward = stats["reward_sum"] / count

        # --------------------------------------------------------
        # collapse重み調整
        # --------------------------------------------------------
        if avg_reward < 0:
            stats["w_collapse"] = min(0.95, stats["w_collapse"] + 0.02)
        else:
            stats["w_collapse"] = max(0.2, stats["w_collapse"] - 0.01)

        # --------------------------------------------------------
        # take重み
        # --------------------------------------------------------
        stats["w_take"] = min(
            1.0,
            max(0.1, 0.5 + avg_reward * 0.1)
        )

        # --------------------------------------------------------
        # hold重み
        # --------------------------------------------------------
        stats["w_hold"] = 1.0 - stats["w_collapse"]

    # ============================================================
    # 重み抽出
    # ============================================================

    def _extract_weights(self, stats: Dict[str, float]) -> Dict[str, float]:

        return {
            "w_collapse": _safe(stats["w_collapse"], 0.6),
            "w_hold": _safe(stats["w_hold"], 0.4),
            "w_take": _safe(stats["w_take"], 0.5),
        }

    # ============================================================
    # デバッグ情報
    # ============================================================

    def debug_info(self) -> Dict[str, int]:

        with self._lock:
            symbol_count = len(self._symbol_store)
            cluster_count = len(self._cluster_store)

        return {
            "symbol_layers": symbol_count,
            "cluster_layers": cluster_count,
            "global_count": self._global_stats["count"],
        }