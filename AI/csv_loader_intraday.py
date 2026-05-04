# ============================================================
# csv_loader_intraday.py
# ------------------------------------------------------------
# ・TSE_DLY_<SYMBOL>,<INTERVAL>.csv を読み込む
# ・分足 / 秒足 共通
# ・4000銘柄対応（interval 単位で全結合）
# ============================================================

from pathlib import Path
import pandas as pd
import re

# ファイル名解析用
FILENAME_RE = re.compile(
    r"TSE_DLY_(?P<symbol>[^,]+),(?P<interval>\d+s?|\d+)\.csv"
)

def load_intraday_all(csv_dir: Path, interval: str) -> pd.DataFrame:
    """
    interval 例:
      "1"   -> 1分足
      "5"   -> 5分足
      "60"  -> 1時間足
      "1s"  -> 1秒足
      "5s"  -> 5秒足
      "10s" -> 10秒足
    """
    dfs = []

    for path in csv_dir.glob(f"TSE_DLY_*,{interval}.csv"):
        m = FILENAME_RE.match(path.name)
        if not m:
            continue

        symbol = m.group("symbol")

        try:
            df = pd.read_csv(path)
        except Exception as e:
            print(f"⚠ 読み込み失敗: {path.name} ({e})")
            continue

        # 必須カラムチェック
        required_cols = {"datetime", "open", "high", "low", "close", "volume"}
        if not required_cols.issubset(df.columns):
            print(f"⚠ 必須カラム不足: {path.name}")
            continue

        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime"])
        df = df.sort_values("datetime")

        df["symbol"] = symbol
        dfs.append(df)

    if not dfs:
        return pd.DataFrame()

    return pd.concat(dfs, ignore_index=True)
