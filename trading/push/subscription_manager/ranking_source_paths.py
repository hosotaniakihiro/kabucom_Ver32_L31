# ============================================================
# File   : trading/push/subscription_manager/ranking_source_paths.py
# Function:
#   - ranking source 系のパス解決
#   - 今日の ranking DB 候補
#   - ATS usable ranking DB fallback
#   - SBI寄前CSVパス
#   - 登録銘柄履歴DBパス
# ------------------------------------------------------------
# Version: PRODUCTION-REV1.0-RANKING-SOURCE-PATHS
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


REGISTER_MAX_SYMBOLS = 100

RANKING_DB_BASE_DIRS = [
    r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking",
    r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\Ranking",
]

SBI_PREMARKET_DIR = (
    r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\SBI\SBI_yorimae_ranking"
)

SUBSCRIPTION_HISTORY_DB = (
    r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking"
    r"\push_subscription_symbols_history.db"
)

MARKET_OPEN = dt.time(9, 0)
OPENING_CSV_END = dt.time(9, 5)
MARKET_CLOSE = dt.time(15, 30)


def now_dt() -> dt.datetime:
    return dt.datetime.now()


def today_ymd(now: Optional[dt.datetime] = None) -> str:
    now = now or now_dt()
    return now.strftime("%Y%m%d")


def today_date_str(now: Optional[dt.datetime] = None) -> str:
    now = now or now_dt()
    return now.strftime("%Y-%m-%d")


def is_existing_file(path: str) -> bool:
    try:
        return bool(path) and os.path.exists(path)
    except Exception:
        return False


def is_opening_csv_window(now: Optional[dt.datetime] = None) -> bool:
    """
    寄り付き初期だけ SBI寄前CSV を最優先にする。
    8:00〜9:05 を対象にする。
    """
    now = now or now_dt()
    t = now.time()
    return dt.time(8, 0) <= t <= OPENING_CSV_END


def is_intraday(now: Optional[dt.datetime] = None) -> bool:
    """
    場中再起動判定用。
    昼休みもシステム上は場中扱いで履歴復元対象にする。
    """
    now = now or now_dt()
    t = now.time()
    return MARKET_OPEN <= t <= MARKET_CLOSE


def ranking_db_candidates(now: Optional[dt.datetime] = None) -> List[str]:
    ymd = today_ymd(now)
    return [os.path.join(base, f"ranking{ymd}.db") for base in RANKING_DB_BASE_DIRS]


def premarket_csv_paths(ymd: Optional[str] = None, now: Optional[dt.datetime] = None) -> Tuple[str, str]:
    ymd = ymd or today_ymd(now)

    gainer = os.path.join(
        SBI_PREMARKET_DIR,
        f"ランキング_寄前気配上昇率上位{ymd}.csv",
    )
    loser = os.path.join(
        SBI_PREMARKET_DIR,
        f"ランキング_寄前気配下落率上位{ymd}.csv",
    )
    return gainer, loser


def resolve_ranking_db_paths(now: Optional[dt.datetime] = None) -> List[str]:
    """
    ranking DB 候補を返す。

    方針:
      1. 今日の rankingYYYYMMDD.db を最優先
      2. ATS側 usable ranking DB を fallback として追加
    """
    db_paths: List[str] = []

    for path in ranking_db_candidates(now):
        if path and path not in db_paths:
            db_paths.append(path)

    try:
        from ats.ats_ranking import get_usable_ranking_db_path

        preferred = get_usable_ranking_db_path(force_refresh=False)
        if preferred and preferred not in db_paths:
            db_paths.append(preferred)

    except Exception:
        logger.exception("[SUB MANAGER] failed to import ats ranking db resolver")

    out: List[str] = []
    for p in db_paths:
        if p and p not in out:
            out.append(p)

    return out


def ensure_parent_dir(path: str) -> None:
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    except Exception:
        logger.exception("[SUB MANAGER] ensure_parent_dir failed path=%s", path)