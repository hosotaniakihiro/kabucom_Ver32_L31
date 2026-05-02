# ============================================================
# trading/utils/session_time_utils.py
# ------------------------------------------------------------
# Session / Market Time Utilities (JP Stock Market)
#
# ✔ 日本株（東証）セッション判定
# ✔ 前場 / 後場 / 昼休み / 時間外 を明確に区別
# ✔ ranking / entry / AI 前段フィルタで安全に使用可能
# ✔ datetime naive / aware 混在を許容
# ✔ 祝日・土日スキップ対応（外部 holiday 判定は委譲）
# ============================================================

import datetime as dt
from typing import Optional

# ------------------------------------------------------------
# JP market session definition
# ------------------------------------------------------------
MORNING_START = dt.time(9, 0)
MORNING_END   = dt.time(11, 30)

AFTERNOON_START = dt.time(12, 30)
AFTERNOON_END   = dt.time(15, 30)

# ------------------------------------------------------------
# helper
# ------------------------------------------------------------
def _to_datetime(ts: Optional[dt.datetime | dt.date]) -> dt.datetime:
    """
    date / datetime / None を datetime に正規化
    None の場合は now()
    """
    if ts is None:
        return dt.datetime.now()

    if isinstance(ts, dt.date) and not isinstance(ts, dt.datetime):
        return dt.datetime.combine(ts, dt.time.min)

    if isinstance(ts, dt.datetime):
        # tz-aware → naive に統一
        if ts.tzinfo is not None:
            return ts.replace(tzinfo=None)
        return ts

    raise TypeError(f"unsupported time type: {type(ts)}")


# ------------------------------------------------------------
def is_weekday(ts: Optional[dt.datetime] = None) -> bool:
    """
    平日判定（月〜金）
    ※ 祝日判定はここでは行わない
    """
    ts = _to_datetime(ts)
    return ts.weekday() < 5


# ------------------------------------------------------------
def is_morning_session(ts: Optional[dt.datetime] = None) -> bool:
    """
    前場（09:00 - 11:30）
    """
    ts = _to_datetime(ts)
    t = ts.time()
    return MORNING_START <= t < MORNING_END


# ------------------------------------------------------------
def is_afternoon_session(ts: Optional[dt.datetime] = None) -> bool:
    """
    後場（12:30 - 15:30）
    """
    ts = _to_datetime(ts)
    t = ts.time()
    return AFTERNOON_START <= t < AFTERNOON_END


# ------------------------------------------------------------
def is_lunch_break(ts: Optional[dt.datetime] = None) -> bool:
    """
    昼休み（11:30 - 12:30）
    """
    ts = _to_datetime(ts)
    t = ts.time()
    return MORNING_END <= t < AFTERNOON_START


# ------------------------------------------------------------
def is_market_open(ts: Optional[dt.datetime] = None) -> bool:
    """
    市場が開いているか（前場 or 後場）
    """
    if not is_weekday(ts):
        return False

    return is_morning_session(ts) or is_afternoon_session(ts)


# ------------------------------------------------------------
def is_market_closed(ts: Optional[dt.datetime] = None) -> bool:
    """
    市場クローズ判定
    """
    return not is_market_open(ts)


# ------------------------------------------------------------
def is_before_market(ts: Optional[dt.datetime] = None) -> bool:
    """
    寄り付き前（09:00 以前）
    """
    ts = _to_datetime(ts)
    return ts.time() < MORNING_START


# ------------------------------------------------------------
def is_after_market(ts: Optional[dt.datetime] = None) -> bool:
    """
    引け後（15:30 以降）
    """
    ts = _to_datetime(ts)
    return ts.time() >= AFTERNOON_END


# ------------------------------------------------------------
def get_market_session(ts: Optional[dt.datetime] = None) -> str:
    """
    現在の市場セッションを文字列で返す

    Returns:
        "MORNING" | "AFTERNOON" | "LUNCH" | "BEFORE" | "AFTER" | "CLOSED"
    """
    ts = _to_datetime(ts)

    if not is_weekday(ts):
        return "CLOSED"

    if is_morning_session(ts):
        return "MORNING"

    if is_afternoon_session(ts):
        return "AFTERNOON"

    if is_lunch_break(ts):
        return "LUNCH"

    if is_before_market(ts):
        return "BEFORE"

    if is_after_market(ts):
        return "AFTER"

    return "CLOSED"


# ------------------------------------------------------------
def is_tradable_time(ts: Optional[dt.datetime] = None) -> bool:
    """
    売買判断をしてよい時間か（entry / exit 用）

    - 市場オープン中のみ True
    """
    return is_market_open(ts)


# ------------------------------------------------------------
def is_ranking_active_time(ts: Optional[dt.datetime] = None) -> bool:
    """
    ランキング更新・ランキングエントリーを許可する時間

    設計思想：
    - 前場・後場のみ
    - 昼休み・時間外は除外
    """
    return is_market_open(ts)


# ------------------------------------------------------------
def seconds_until_market_close(ts: Optional[dt.datetime] = None) -> int:
    """
    引けまでの残り秒数（市場外の場合は 0）
    """
    ts = _to_datetime(ts)

    if not is_market_open(ts):
        return 0

    if is_morning_session(ts):
        end_dt = dt.datetime.combine(ts.date(), MORNING_END)
    else:
        end_dt = dt.datetime.combine(ts.date(), AFTERNOON_END)

    return max(int((end_dt - ts).total_seconds()), 0)


# ------------------------------------------------------------
def seconds_from_market_open(ts: Optional[dt.datetime] = None) -> int:
    """
    寄り付きからの経過秒数（市場外は 0）
    """
    ts = _to_datetime(ts)

    if not is_market_open(ts):
        return 0

    if is_morning_session(ts):
        start_dt = dt.datetime.combine(ts.date(), MORNING_START)
    else:
        start_dt = dt.datetime.combine(ts.date(), AFTERNOON_START)

    return max(int((ts - start_dt).total_seconds()), 0)


# ------------------------------------------------------------
# CLI debug
# ------------------------------------------------------------
if __name__ == "__main__":
    now = dt.datetime.now()

    print("now:", now)
    print("weekday:", is_weekday(now))
    print("market_open:", is_market_open(now))
    print("session:", get_market_session(now))
    print("tradable:", is_tradable_time(now))
    print("ranking_active:", is_ranking_active_time(now))
    print("sec_from_open:", seconds_from_market_open(now))
    print("sec_to_close:", seconds_until_market_close(now))
