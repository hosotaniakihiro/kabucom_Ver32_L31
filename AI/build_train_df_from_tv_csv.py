# ============================================================
# AI/build_train_df_from_tv_csv.py
# ------------------------------------------------------------
# ✔ TradingView CSV 自動読込
# ✔ 銘柄混合
# ✔ 時間足ごとに学習DF生成
# ✔ 秒足 / 分足 両対応
# ✔ Windows Errno24 回避（プロセス分離）
# ✔ paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import time
import os
import sys
import pandas as pd
from pathlib import Path
import re
import subprocess

from config.paths import get_path

# ============================================================
# 入力ディレクトリ（TradingView CSV）
# ============================================================
SRC_DIR: Path = get_path("raw_tradingview")

# ============================================================
# 出力ディレクトリ（必ず作成）
# ============================================================
OUT_DIR: Path = get_path("ai_train_data") / "tv"
OUT_DIR.mkdir(parents=True, exist_ok=True)

# ============================================================
# 子プロセス用スクリプト（★絶対パス）
# ============================================================
SCRIPT_DIR = Path(__file__).resolve().parent
CHILD_SCRIPT = SCRIPT_DIR / "build_train_df_one_csv.py"


# ============================================================
# volume 正規化（TradingView CSV 吸収）
# ============================================================
def normalize_volume(df: pd.DataFrame) -> pd.DataFrame:
    """
    volume / Volume / tick_volume を吸収
    無い場合は 1.0 を入れる（学習継続優先）
    """
    if "volume" in df.columns:
        return df

    if "Volume" in df.columns:
        df["volume"] = df["Volume"]
        return df

    if "tick_volume" in df.columns:
        df["volume"] = df["tick_volume"]
        return df

    df["volume"] = 1.0
    return df


# ============================================================
# ファイル名パース
# ============================================================
def parse_filename(path: Path):
    """
    TSE_DLY_133A,5S.csv
    → symbol=133A, timeframe=5S, unit=sec
    """
    name = path.stem
    m = re.match(r"TSE_DLY_(.+?),(.+)", name)
    if not m:
        return None

    symbol = m.group(1)
    tf_raw = m.group(2)

    if tf_raw.endswith("S"):
        timeframe = tf_raw
        unit = "sec"
    else:
        timeframe = f"{tf_raw}M"
        unit = "min"

    return symbol, timeframe, unit


# ============================================================
# 特徴量生成
# ============================================================
def add_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    df["ret"] = df["close"].pct_change()
    df["body"] = (df["close"] - df["open"]) / df["open"]
    df["range"] = (df["high"] - df["low"]) / df["open"]

    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma5"]

    df["fast_ret"] = df["close"] / df["close"].shift(3) - 1

    return df


# ============================================================
# ラベル生成
# ============================================================
def add_label(df: pd.DataFrame, unit: str) -> pd.DataFrame:
    df = df.copy()

    if unit == "sec":
        df["y"] = df["close"].shift(-6) / df["close"] - 1
    else:
        df["y"] = df["close"].shift(-1) / df["close"] - 1

    return df


# ============================================================
# メイン処理（親プロセス）
# ============================================================
def main():
    if not SRC_DIR.exists():
        print(f"⚠ TradingView CSV dir not found: {SRC_DIR}")
        return

    files = [
        f for f in os.listdir(SRC_DIR)
        if f.startswith("TSE_DLY_")
           and f.endswith(".csv")
           and "(" not in f  # ★重複ファイル除外
    ]

    for i, fname in enumerate(files, 1):
        path = SRC_DIR / fname
        print(f"🚀 [{i}/{len(files)}] spawn {fname}")

        try:
            subprocess.run(
                [
                    sys.executable,
                    str(CHILD_SCRIPT),
                    str(path),
                    str(OUT_DIR),
                ],
                check=True,
                close_fds=True,   # ★超重要
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.CalledProcessError as e:
            print(f"❌ skip {fname} ({e})")

        # ★ Windows ハンドル回収猶予（必須）
        time.sleep(0.2)


# ============================================================
if __name__ == "__main__":
    main()
