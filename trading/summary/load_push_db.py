# ============================================================
# trading/summary/load_push_db.py
# ------------------------------------------------------------
# 指定日の push DB（pushYYYYMMDD.db）をロード
# paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import sqlite3
import pandas as pd
from pathlib import Path

from config.paths import get_path


def load_push_db(trade_date: str) -> pd.DataFrame:
    """
    pushYYYYMMDD.db を全件ロードする

    Parameters
    ----------
    trade_date : str
        YYYYMMDD 形式の日付文字列

    Returns
    -------
    pd.DataFrame
        stream_data テーブルの全件
        DB が存在しない場合は空 DataFrame
    """
    # --------------------------------------------------------
    # push DB パス（paths.py 経由）
    # --------------------------------------------------------
    push_dir: Path = get_path("raw_push")
    db_path: Path = push_dir / f"push{trade_date}.db"

    if not db_path.exists():
        return pd.DataFrame()

    con = sqlite3.connect(db_path)
    try:
        df = pd.read_sql(
            "SELECT * FROM stream_data",
            con,
            parse_dates=["datetime"]
        )
        return df
    finally:
        con.close()
