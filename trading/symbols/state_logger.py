# ============================================================
# config/active_ai_time_threshold.py
# Ver1.0-FINAL-ACTIVE-AI-TIME-THRESHOLD
# ------------------------------------------------------------
# ✔ 時間帯別に ACTIVE_AI の threshold を切替
# ✔ 寄り付きノイズ対策 / 後場トレンド追従
# ✔ STEP9 対応
# ============================================================

import datetime as dt
from datetime import time
from typing import List, Tuple

# ============================================================
# 時間帯別 threshold 設定
# （start, end, threshold）
# ============================================================

ACTIVE_AI_TIME_THRESHOLD: List[Tuple[time, time, float]] = [
    # 寄り直後：ノイズ多 → 厳しめ
    (time(9, 0),  time(9, 30), 0.70),

    # 前場中盤
    (time(9, 30), time(11, 0), 0.65),

    # 前場後半〜昼休み前
    (time(11, 0), time(13, 0), 0.60),

    # 後場前半
    (time(13, 0), time(14, 30), 0.55),

    # 大引け前：トレンド継続を拾う
    (time(14, 30), time(15, 0), 0.50),
]

DEFAULT_THRESHOLD = 0.60


# ============================================================
# API
# ============================================================

def get_active_ai_threshold(now: dt.datetime) -> float:
    """
    現在時刻から ACTIVE_AI threshold を取得
    """
    t = now.time()
    for start, end, th in ACTIVE_AI_TIME_THRESHOLD:
        if start <= t < end:
            return th
    return DEFAULT_THRESHOLD