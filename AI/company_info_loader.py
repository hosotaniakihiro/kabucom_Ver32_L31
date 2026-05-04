# ============================================================
# company_info_loader.py
# ------------------------------------------------------------
# ・company_info.db から銘柄の静的特徴量を読み込む
# ・AI学習 / 推論用に安全な数値特徴量へ変換
# ・日足 / 分足 / 秒足 共通
# ・paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import sqlite3
import pandas as pd
import numpy as np
from pathlib import Path

from config.paths import get_path

# ============================================================
# DB パス（paths.py 経由）
# ============================================================

DB_PATH: Path = get_path("raw_stock_data") / "company_info.db"


# ============================================================
# 内部関数：時価総額 → bucket
# ============================================================

def _market_cap_to_bucket(cap):
    """
    時価総額を帯に変換
    """
    if pd.isna(cap):
        return -1
    if cap < 3e10:          # 〜300億
        return 0
    elif cap < 1e11:        # 300〜1000億
        return 1
    elif cap < 5e11:        # 1000〜5000億
        return 2
    elif cap < 2e12:        # 5000億〜2兆
        return 3
    else:                   # 2兆〜
        return 4


# ============================================================
# メイン：company_info 読み込み
# ============================================================

def load_company_features() -> pd.DataFrame:
    """
    return:
        DataFrame[
            symbol,
            market_cap_bucket,
            employees_log,
            shares_log,
            sector_id,
            industry_id
        ]
    """

    if not DB_PATH.exists():
        raise FileNotFoundError(f"company_info.db not found: {DB_PATH}")

    con = sqlite3.connect(DB_PATH)

    df = pd.read_sql(
        """
        SELECT
            stock_code AS symbol,
            market_cap,
            shares_outstanding,
            employees,
            sector,
            industry
        FROM company_info
        """,
        con
    )

    con.close()

    # --------------------------------------------------------
    # 時価総額 → bucket
    # --------------------------------------------------------
    df["market_cap_bucket"] = df["market_cap"].apply(_market_cap_to_bucket)

    # --------------------------------------------------------
    # 規模系（log変換）
    # --------------------------------------------------------
    df["employees_log"] = np.log1p(df["employees"].fillna(0))
    df["shares_log"] = np.log1p(df["shares_outstanding"].fillna(0))

    # --------------------------------------------------------
    # 業種（ラベルエンコード）
    # LightGBM は label encoding を理解する
    # --------------------------------------------------------
    df["sector_id"] = df["sector"].astype("category").cat.codes
    df["industry_id"] = df["industry"].astype("category").cat.codes

    # --------------------------------------------------------
    # AI に渡す最終列だけ残す
    # --------------------------------------------------------
    out = df[
        [
            "symbol",
            "market_cap_bucket",
            "employees_log",
            "shares_log",
            "sector_id",
            "industry_id",
        ]
    ].copy()

    # 念のため型を固定
    out["symbol"] = out["symbol"].astype(str)
    out["market_cap_bucket"] = out["market_cap_bucket"].astype(int)
    out["sector_id"] = out["sector_id"].astype(int)
    out["industry_id"] = out["industry_id"].astype(int)

    return out


# ============================================================
if __name__ == "__main__":
    df = load_company_features()
    print(df.head())
    print("rows:", len(df))
