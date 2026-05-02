# ============================================================
# File   : trading/utils/session_time_utils.py
# Ver    : 1.0.1-FINAL-SESSION-TIME-UTILS-JP-MARKET
# ------------------------------------------------------------
# ✔ 日本株（JST）専用セッション時間ユーティリティ
# ✔ 前場 / 後場 / 寄り直後 / 引け前 判定
# ✔ summary / scoring / AI / entry_gate から安全に使用可能
# ✔ datetime / time / None 全対応
# ✔ 副作用ゼロ（純関数）
# ✔ ★ is_opening_session 後方互換対応（NEW）
# ============================================================

from __future__ import annotations

import datetime as dt
from typing import Literal


# ============================================================
# セッション定義（JST 前提）
# ============================================================

MORNING_START = dt.time(9, 0)
MORNING_END   = dt.time(11, 30)

AFTERNOON_START = dt.time(12, 30)
AFTERNOON_END   = dt.time(15, 30)

MARKET_OPEN_TIME  = MORNING_START
MARKET_CLOSE_TIME = AFTERNOON_END


# ============================================================
# 型定義
# ============================================================

SessionName = Literal[
    "pre_market",
    "opening",
    "morning",
    "lunch_break",
    "afternoon",
    "closing",
    "after_market",
]


# ============================================================
# 内部：datetime → time 安全変換
# ============================================================

def _to_time(t: dt.datetime | dt.time | None) -> dt.time | None:
    """
    datetime / time / None → time or None
    """
    if t is None:
        return None
    if isinstance(t, dt.datetime):
        return t.time()
    if isinstance(t, dt.time):
        return t
    return None


# ============================================================
# 市場が開いているか
# ============================================================

def is_market_open(now: dt.datetime | dt.time | None = None) -> bool:
    """
    市場取引時間内かどうか
    """
    t = _to_time(now) or dt.datetime.now().time()

    return (
        (MORNING_START <= t <= MORNING_END)
        or (AFTERNOON_START <= t <= AFTERNOON_END)
    )


# ============================================================
# 前場かどうか
# ============================================================

def is_morning_session(now: dt.datetime | dt.time | None = None) -> bool:
    t = _to_time(now) or dt.datetime.now().time()
    return MORNING_START <= t <= MORNING_END


# ============================================================
# 後場かどうか
# ============================================================

def is_afternoon_session(now: dt.datetime | dt.time | None = None) -> bool:
    t = _to_time(now) or dt.datetime.now().time()
    return AFTERNOON_START <= t <= AFTERNOON_END


# ============================================================
# 寄り直後かどうか（事故りやすい時間帯）
# ============================================================

def is_opening_phase(
    now: dt.datetime | dt.time | None = None,
    minutes: int = 30,
) -> bool:
    """
    寄り付き直後かどうか（デフォルト 30 分）
    """
    t = _to_time(now) or dt.datetime.now().time()

    opening_end = (
        dt.datetime.combine(dt.date.today(), MORNING_START)
        + dt.timedelta(minutes=minutes)
    ).time()

    return MORNING_START <= t < opening_end


# ============================================================
# ★ 後方互換：旧 API 名
# ============================================================

def is_opening_session(
    now: dt.datetime | dt.time | None = None,
) -> bool:
    """
    旧コード互換用
    - is_opening_phase(now, 60) と同義
    """
    return is_opening_phase(now, minutes=60)


# ============================================================
# 引け前かどうか
# ============================================================

def is_closing_phase(
    now: dt.datetime | dt.time | None = None,
    minutes: int = 15,
) -> bool:
    """
    引け前◯分かどうか
    """
    t = _to_time(now) or dt.datetime.now().time()

    closing_start = (
        dt.datetime.combine(dt.date.today(), MARKET_CLOSE_TIME)
        - dt.timedelta(minutes=minutes)
    ).time()

    return closing_start <= t <= MARKET_CLOSE_TIME


# ============================================================
# 昼休み中かどうか
# ============================================================

def is_lunch_break(now: dt.datetime | dt.time | None = None) -> bool:
    t = _to_time(now) or dt.datetime.now().time()
    return MORNING_END < t < AFTERNOON_START


# ============================================================
# セッション名取得（人間可読）
# ============================================================

def get_session_name(
    now: dt.datetime | dt.time | None = None,
) -> SessionName:
    """
    現在の市場セッション名を返す
    """

    t = _to_time(now) or dt.datetime.now().time()

    if t < MARKET_OPEN_TIME:
        return "pre_market"

    if is_opening_phase(t):
        return "opening"

    if is_morning_session(t):
        return "morning"

    if is_lunch_break(t):
        return "lunch_break"

    if is_afternoon_session(t):
        if is_closing_phase(t):
            return "closing"
        return "afternoon"

    return "after_market"


# ============================================================
# セッション係数（AI / scoring 用）
# ============================================================

def get_session_weight(
    now: dt.datetime | dt.time | None = None,
) -> float:
    """
    時間帯によるリスク係数
    （大きいほど慎重）
    """

    session = get_session_name(now)

    return {
        "pre_market":   1.5,
        "opening":      1.3,   # 寄り直後は慎重
        "morning":      1.0,
        "lunch_break":  1.2,
        "afternoon":    0.9,
        "closing":      1.1,
        "after_market": 1.5,
    }.get(session, 1.0)

def add_time_zone_label(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "end_time" not in df.columns:
        return df

    df = df.copy()
    df["end_time"] = pd.to_datetime(df["end_time"], errors="coerce")
    df = df.dropna(subset=["end_time"])

    def _zone(t: dt.time) -> str:
        if dt.time(9, 0) <= t < dt.time(9, 30):
            return "OPEN"
        if dt.time(9, 30) <= t < dt.time(11, 30):
            return "MORNING"
        if dt.time(12, 30) <= t < dt.time(14, 30):
            return "MIDDAY"
        return "LATE"

    df["time_zone"] = df["end_time"].dt.time.map(_zone)
    return df


# ============================================================
# Debug
# ============================================================

if __name__ == "__main__":
    now = dt.datetime.now()
    print("now:", now)
    print("market_open:", is_market_open(now))
    print("opening_phase:", is_opening_phase(now))
    print("opening_session(compat):", is_opening_session(now))
    print("session_name:", get_session_name(now))
    print("session_weight:", get_session_weight(now))
