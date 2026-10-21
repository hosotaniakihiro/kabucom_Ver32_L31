# ============================================================
# backtest16/market_calendar.py
# ------------------------------------------------------------
# 東証の営業日・時間帯の判定（§7 未来情報防止 / 休場日・昼休み考慮）。
#
# 営業日の真実源は2段構え:
#   1) 休日CSV(holidays_cache.csv, 祝日) + 土日 で祝休日を判定
#   2) 実在する日次DBファイルの日付 = 実際に立会があった日（ground truth）
# 実行時は (2) を優先し、データのある日だけを対象にする。
# ============================================================

from __future__ import annotations

import csv
import datetime as dt
import os
from functools import lru_cache
from pathlib import Path
from typing import List, Set

# 前場/後場（東証）
MORNING_OPEN = dt.time(9, 0, 0)
MORNING_CLOSE = dt.time(11, 30, 0)
AFTERNOON_OPEN = dt.time(12, 30, 0)
AFTERNOON_CLOSE = dt.time(15, 0, 0)   # 2024/11以降は15:30だが本検証は前場のみ使用

_HOLIDAYS_CSV = str(Path(__file__).resolve().parent.parent / "holidays_cache.csv")

# 東証固有の休場日（祝日CSVに含まれない年末年始）。当検証窓(4-10月)には無いが明示。
_EXCHANGE_ONLY_CLOSED = {(1, 2), (1, 3), (12, 31)}  # (month, day)


@lru_cache(maxsize=1)
def _holiday_set() -> Set[dt.date]:
    out: Set[dt.date] = set()
    try:
        with open(_HOLIDAYS_CSV, encoding="utf-8") as f:
            r = csv.reader(f)
            next(r, None)  # ヘッダ
            for row in r:
                if not row:
                    continue
                try:
                    y, m, d = [int(x) for x in row[0].split("/")]
                    out.add(dt.date(y, m, d))
                except Exception:
                    continue
    except Exception:
        pass
    return out


def is_holiday(d: dt.date) -> bool:
    if d.weekday() >= 5:            # 土(5)・日(6)
        return True
    if (d.month, d.day) in _EXCHANGE_ONLY_CLOSED:
        return True
    return d in _holiday_set()


def is_trading_day(d: dt.date) -> bool:
    return not is_holiday(d)


def trading_days(start: dt.date, end: dt.date) -> List[dt.date]:
    """[start, end] の営業日(祝休日除く)。データ有無は考慮しない暦ベース。"""
    out: List[dt.date] = []
    cur = start
    while cur <= end:
        if is_trading_day(cur):
            out.append(cur)
        cur += dt.timedelta(days=1)
    return out


def available_data_days(dir_path: str, prefix: str, start: dt.date, end: dt.date,
                        exclude_from: dt.date | None = None) -> List[dt.date]:
    """ディレクトリ内の <prefix>YYYYMMDD.db から、実在する立会日を返す（ground truth）。
    exclude_from 以降(当日含む)は除外する（ライブ書込み中のため）。"""
    out: List[dt.date] = []
    try:
        for name in os.listdir(dir_path):
            if not name.startswith(prefix) or not name.endswith(".db"):
                continue
            stem = name[len(prefix):-3]
            if len(stem) != 8 or not stem.isdigit():
                continue
            try:
                d = dt.date(int(stem[:4]), int(stem[4:6]), int(stem[6:8]))
            except Exception:
                continue
            if d < start or d > end:
                continue
            if exclude_from is not None and d >= exclude_from:
                continue
            out.append(d)
    except Exception:
        pass
    return sorted(out)


def in_morning_session(t: dt.time) -> bool:
    return MORNING_OPEN <= t <= MORNING_CLOSE


def is_lunch_break(t: dt.time) -> bool:
    return MORNING_CLOSE < t < AFTERNOON_OPEN


def ymd(d: dt.date) -> str:
    return d.strftime("%Y%m%d")
