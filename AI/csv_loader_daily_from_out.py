# ============================================================
# csv_loader_daily_from_out.py（DtypeWarning 完全回避版）
# ============================================================

from pathlib import Path
import pandas as pd

USE_COLUMNS = [
    "stock_code",
    "date",
    "close",
    "volume",

    "MA_25_Deviation_Pct",
    "MA_75_Deviation_Pct",
    "MA_200_Deviation_Pct",
    "Price_Position_Pct_1Y",
    "High_Pos_1Y",
    "Low_Pos_1Y",

    "ATR_14_Pct",
    "BB_Std",

    "Volume_Ratio_20D_MA",
    "turnover",

    "change_1d_pct",
    "change_5d_pct",
    "change_last_week_pct",
    "change_last_month_pct",

    "Limit_Up_Permanent",
    "Limit_Down_Permanent",
    "Limit_Up_Touched",
    "Limit_Down_Touched",
    "Expand_Limit_Up_Tomorrow",
    "Expand_Limit_Down_Tomorrow",
]

# ★ dtype を明示
DTYPES = {
    "stock_code": "string",
    "close": "float64",
    "volume": "float64",
    "MA_25_Deviation_Pct": "float64",
    "MA_75_Deviation_Pct": "float64",
    "MA_200_Deviation_Pct": "float64",
    "Price_Position_Pct_1Y": "float64",
    "High_Pos_1Y": "float64",
    "Low_Pos_1Y": "float64",
    "ATR_14_Pct": "float64",
    "BB_Std": "float64",
    "Volume_Ratio_20D_MA": "float64",
    "turnover": "float64",
    "change_1d_pct": "float64",
    "change_5d_pct": "float64",
    "change_last_week_pct": "float64",
    "change_last_month_pct": "float64",
    # フラグ系は一旦 string
    "Limit_Up_Permanent": "string",
    "Limit_Down_Permanent": "string",
    "Limit_Up_Touched": "string",
    "Limit_Down_Touched": "string",
    "Expand_Limit_Up_Tomorrow": "string",
    "Expand_Limit_Down_Tomorrow": "string",
}

def _flag_to_int(s):
    return s.astype(str).str.upper().isin(["TRUE", "1"]).astype(int)

def load_daily_all(csv_dir: Path) -> pd.DataFrame:
    dfs = []

    for path in csv_dir.glob("*.T.out.csv"):
        try:
            df = pd.read_csv(
                path,
                usecols=USE_COLUMNS,
                dtype=DTYPES,
                low_memory=False,   # ★重要
            )
        except Exception as e:
            print(f"⚠ 読み込み失敗: {path.name} {e}")
            continue

        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])

        df.rename(columns={"stock_code": "symbol"}, inplace=True)

        # フラグ系を 0/1 に変換
        for c in [
            "Limit_Up_Permanent",
            "Limit_Down_Permanent",
            "Limit_Up_Touched",
            "Limit_Down_Touched",
            "Expand_Limit_Up_Tomorrow",
            "Expand_Limit_Down_Tomorrow",
        ]:
            df[c] = _flag_to_int(df[c])

        dfs.append(df)

    if not dfs:
        return pd.DataFrame()

    return pd.concat(dfs, ignore_index=True)
