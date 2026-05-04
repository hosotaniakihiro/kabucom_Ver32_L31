# ============================================================
# utils_time.py（Ver24-FINAL-STABLE + YAHOO-RANGE）
# ------------------------------------------------------------
# ・Yahoo 遅延境界時刻の統一管理
# ・Yahoo 取得レンジ（前々営業日～遅延境界）
# ・PUSH / Yahoo / Summary 共通利用
# ・timezone 非依存（naive datetime）
# ============================================================

import datetime as dt
import logging
import jpholiday

logger = logging.getLogger(__name__)

# ============================================================
# 定数
# ============================================================

# Yahoo Finance の 1分足は約20分遅延
YAHOO_DELAY_MINUTES = 20

# 市場終了時刻（東証）
MARKET_CLOSE_TIME = dt.time(15, 30)

# 市場開始時刻（東証）
MARKET_OPEN_TIME = dt.time(9, 0)


# ============================================================
# Yahoo 遅延境界時刻
# ============================================================
def get_yahoo_border_time(
    now: dt.datetime | None = None
) -> dt.datetime:
    """
    Yahoo 1分足で「安全に取得できる最新時刻」を返す

    ・現在時刻 - 20分
    ・市場終了後は 15:30 で固定
    ・timezone は持たない（naive）
    """

    if now is None:
        now = dt.datetime.now()

    # 秒・マイクロ秒を落とす
    now = now.replace(second=0, microsecond=0)

    # 市場終了後
    if now.time() >= MARKET_CLOSE_TIME:
        border = dt.datetime.combine(now.date(), MARKET_CLOSE_TIME)
        logger.debug(f"[utils_time] Yahoo border (after close): {border}")
        return border

    # 市場時間内
    border = now - dt.timedelta(minutes=YAHOO_DELAY_MINUTES)
    logger.debug(f"[utils_time] Yahoo border (intraday): {border}")
    return border


# ============================================================
# 営業日を n 日さかのぼる
# ============================================================
def get_prev_business_day(
    base_date: dt.date,
    n: int = 1
) -> dt.date:
    """
    base_date から n 営業日前の日付を返す
    （土日・祝日を除外）
    """

    d = base_date
    cnt = 0

    while cnt < n:
        d -= dt.timedelta(days=1)
        if d.weekday() < 5 and not jpholiday.is_holiday(d):
            cnt += 1

    return d


# ============================================================
# Yahoo 取得レンジ（前々営業日～遅延境界）
# ============================================================
def get_yahoo_start_end_time(
    now: dt.datetime | None = None
) -> tuple[dt.datetime, dt.datetime]:
    """
    Yahoo から確実に取得できる時間レンジを返す

    start_dt:
      ・前々営業日の 09:00

    end_dt:
      ・min(現在時刻 - 20分, 当日 15:30)

    Returns
    -------
    (start_dt, end_dt) : tuple[datetime, datetime]
    """

    if now is None:
        now = dt.datetime.now()

    # Yahoo 遅延境界
    end_dt = get_yahoo_border_time(now)

    # 前々営業日の 09:00
    prev2 = get_prev_business_day(now.date(), n=2)
    start_dt = dt.datetime.combine(prev2, MARKET_OPEN_TIME)

    logger.debug(
        f"[utils_time] Yahoo range: {start_dt} -> {end_dt}"
    )

    return start_dt, end_dt


# ============================================================
# 現在の 1分バー終端時刻
# ============================================================
def get_current_minute_bar_time(
    now: dt.datetime | None = None
) -> dt.datetime:
    """
    現在の 1分足バーの終端時刻を返す
    （秒・マイクロ秒を切り捨て）

    Examples
    --------
    09:12:34 → 09:12:00
    """

    if now is None:
        now = dt.datetime.now()

    return now.replace(second=0, microsecond=0)


# ============================================================
# interval 分足のバー境界時刻
# ============================================================
def floor_time_to_interval(
    dt_value: dt.datetime,
    interval_min: int
) -> dt.datetime:
    """
    datetime を interval 分単位で floor する

    Examples
    --------
    09:14:xx, interval=3 → 09:12:00
    09:17:xx, interval=5 → 09:15:00
    """

    minute = (dt_value.minute // interval_min) * interval_min

    return dt_value.replace(
        minute=minute,
        second=0,
        microsecond=0
    )


# ============================================================
# 市場時間内判定
# ============================================================
def is_market_time(
    now: dt.datetime | None = None
) -> bool:
    """
    東証の市場時間内かどうかを判定
    """

    if now is None:
        now = dt.datetime.now()

    t = now.time()

    # 前場 09:00–11:30 / 後場 12:30–15:30
    return (
        (dt.time(9, 0) <= t <= dt.time(11, 30))
        or
        (dt.time(12, 30) <= t <= MARKET_CLOSE_TIME)
    )


# ============================================================
# 初期サマリー用 cutoff 時刻
# ============================================================
def get_initial_summary_cutoff(
    now: dt.datetime | None = None
) -> dt.datetime:
    """
    初期サマリー計算で使用する cutoff 時刻

    ・市場時間内 → 現在時刻
    ・市場終了後 → 15:30
    """

    if now is None:
        now = dt.datetime.now()

    if now.time() >= MARKET_CLOSE_TIME:
        return dt.datetime.combine(now.date(), MARKET_CLOSE_TIME)

    return now.replace(second=0, microsecond=0)
