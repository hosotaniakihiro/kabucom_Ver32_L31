# ============================================================
# File   : load_summary_1min.py
# ------------------------------------------------------------
# ✔ kabu_station summaryYYYYMMDD.db 対応
# ✔ 日跨ぎ対応
# ✔ symbol / time range 指定可
# ✔ 即益AI / HOLDTIME 学習 両対応
# ============================================================

from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd
import logging

from load_raw_summary import load_raw_summary

logger = logging.getLogger(__name__)


# ============================================================
# summary DB ルート（★ 実環境に合わせる）
# ============================================================

SUMMARY_ROOT = Path(r"X:\raw_data\kabu_station\summary")


# ============================================================
# メインAPI
# ============================================================

def load_summary_1min(
    symbol: int | str,
    start: datetime,
    end: datetime,
) -> pd.DataFrame:
    """
    summaryYYYYMMDD.db から 1分足をロードする（安全版）

    Parameters
    ----------
    symbol : int | str
        銘柄コード
    start : datetime
        取得開始時刻（含まない）
    end : datetime
        取得終了時刻（含む）

    Returns
    -------
    pd.DataFrame
        columns:
            datetime, open, high, low, close, volume, symbol
        ※ 該当なしの場合 empty DataFrame
    """

    symbol = int(symbol)
    dfs: list[pd.DataFrame] = []

    cur_date = start.date()
    end_date = end.date()

    while cur_date <= end_date:
        db_path = SUMMARY_ROOT / f"summary{cur_date.strftime('%Y%m%d')}.db"

        if not db_path.exists():
            logger.debug("summary DB not found: %s", db_path)
            cur_date += timedelta(days=1)
            continue

        try:
            df = load_raw_summary(db_path)
        except Exception as e:
            logger.warning("failed to load %s: %s", db_path, e)
            cur_date += timedelta(days=1)
            continue

        if df is None or df.empty:
            cur_date += timedelta(days=1)
            continue

        # ----------------------------------------------------
        # 正規化
        # ----------------------------------------------------
        df = df.copy()

        # symbol 正規化
        df["symbol"] = pd.to_numeric(df["symbol"], errors="coerce").astype("Int64")

        # datetime 正規化
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

        df = df.dropna(subset=["symbol", "datetime"])

        # ----------------------------------------------------
        # フィルタ
        # ----------------------------------------------------
        df = df[
            (df["symbol"] == symbol)
            & (df["datetime"] > start)
            & (df["datetime"] <= end)
        ]

        if not df.empty:
            dfs.append(df)

        cur_date += timedelta(days=1)

    if not dfs:
        return pd.DataFrame()

    # --------------------------------------------------------
    # 結合・整形
    # --------------------------------------------------------
    df_all = (
        pd.concat(dfs, ignore_index=True)
        .sort_values("datetime")
        .reset_index(drop=True)
    )

    return df_all
