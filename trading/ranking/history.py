# ============================================================
# ranking/history.py
# ------------------------------------------------------------
# ランキング履歴から「一定時間内に登場した銘柄」を抽出
# ============================================================

import sqlite3
import datetime as dt
from pathlib import Path

from global_state import global_data


def get_symbols_appeared_within(minutes: int) -> set[str]:
    """
    指定分数以内にランキングに1回でも登場した銘柄
    """
    base = global_data.base_path
    today = global_data.today_str
    db_path = Path(base) / f"ranking{today}.db"

    since = dt.datetime.now() - dt.timedelta(minutes=minutes)

    sql = """
        SELECT DISTINCT symbol
        FROM ranking
        WHERE created_at >= ?
    """

    symbols = set()
    with sqlite3.connect(db_path) as conn:
        for (sym,) in conn.execute(sql, (since,)):
            symbols.add(str(sym))

    return symbols
