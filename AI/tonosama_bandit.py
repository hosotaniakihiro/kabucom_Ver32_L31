# ============================================================
# File   : AI/tonosama_bandit.py
# Version: Ver1.0-FINAL-PRODUCTION-AGGRESSIVE-SCALP
# ------------------------------------------------------------
# ✔ Thompson Sampling Bandit
# ✔ ENTRY専用（副作用ゼロ）
# ✔ cluster / regime / source 別管理
# ✔ reward フィードバック対応
# ✔ confidence 重み連動
# ✔ スキャル攻撃型設計
# ✔ スレッド安全
# ✔ NaN / None 完全防御
# ============================================================

import math
import random
import threading
from collections import defaultdict

# ============================================================
# グローバル管理
# ============================================================

_lock = threading.Lock()

# key: (cluster, regime, source)
_bandit_state = defaultdict(lambda: {"alpha": 1.0, "beta": 1.0})


# ============================================================
# helpers
# ============================================================

def _safe_float(v, default=0.0):
    try:
        v = float(v)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def _normalize_key(cluster, regime, source):
    cluster = str(cluster or "DEFAULT")
    regime = str(regime or "NORMAL")
    source = str(source or "SUMMARY").upper()
    return (cluster, regime, source)


# ============================================================
# Thompson Sampling
# ============================================================

def _thompson_sample(alpha, beta):
    return random.betavariate(alpha, beta)


# ============================================================
# PUBLIC API
# ============================================================

def select_weight(cluster=None, regime=None, source=None):
    """
    ENTRY時に呼ばれる
    confidenceに掛ける重みを返す
    """

    key = _normalize_key(cluster, regime, source)

    with _lock:
        state = _bandit_state[key]
        alpha = state["alpha"]
        beta = state["beta"]

    score = _thompson_sample(alpha, beta)

    # 攻撃型倍率
    weight = 0.8 + (score * 1.4)   # 0.8〜2.2

    return {
        "weight": weight,
        "sample": score,
        "alpha": alpha,
        "beta": beta,
        "key": key,
    }


def update_reward(cluster=None, regime=None, source=None, reward=0.0):
    """
    EXIT後に呼ばれる
    reward > 0 → success
    reward <= 0 → failure
    """

    key = _normalize_key(cluster, regime, source)
    reward = _safe_float(reward)

    with _lock:
        state = _bandit_state[key]

        if reward > 0:
            state["alpha"] += min(1.0, reward)
        else:
            state["beta"] += min(1.0, abs(reward))


def get_state(cluster=None, regime=None, source=None):
    key = _normalize_key(cluster, regime, source)
    with _lock:
        return dict(_bandit_state[key])


def reset_bandit():
    global _bandit_state
    with _lock:
        _bandit_state = defaultdict(lambda: {"alpha": 1.0, "beta": 1.0})


# ============================================================
# OPTIONAL: confidence連動ユーティリティ
# ============================================================

def apply_bandit_to_confidence(
    confidence,
    cluster=None,
    regime=None,
    source=None,
):
    """
    entry_gate から呼び出す想定
    """

    confidence = _safe_float(confidence, 0.0)

    pack = select_weight(cluster, regime, source)
    weight = pack["weight"]

    new_conf = confidence * weight

    return new_conf, pack