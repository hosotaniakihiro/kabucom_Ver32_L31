# ============================================================
# sbi_preopen_loader.py
# ------------------------------------------------------------
# ・SBI 寄り前気配 CSV 読み込み
# ・出来高 / 気配株数 / 前日比% を安全に正規化
# ・orderbook imbalance 算出
# ============================================================

import pandas as pd
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# ============================================================
# helper
# ============================================================

def calc_imbalance_series(buy: pd.Series, sell: pd.Series) -> pd.Series:
    """
    imbalance = (buy - sell) / (buy + sell)
    0割完全回避
    """
    denom = buy + sell
    return ((buy - sell) / denom.where(denom != 0, 1)).fillna(0.0)


def _to_int_series(s: pd.Series) -> pd.Series:
    """
    カンマ・欠損・空文字に耐える int 変換
    """
    return (
        s.astype(str)
         .str.replace(",", "", regex=False)
         .replace({"": "0", "nan": "0", "None": "0"})
         .astype(float)
         .fillna(0)
         .astype(int)
    )


def _to_float_series(s: pd.Series) -> pd.Series:
    """
    % / 欠損に耐える float 変換
    """
    return (
        s.astype(str)
         .str.replace("%", "", regex=False)
         .replace({"": "0", "nan": "0", "None": "0", "－": "0", "-": "0"})
         .astype(float)
         .fillna(0.0)
    )


def _read_csv_safe(path: Path) -> pd.DataFrame:
    """
    SBI CSV の encoding 揺れ対策
    """
    for enc in ("cp932", "utf-8-sig", "utf-8"):
        try:
            return pd.read_csv(path, encoding=enc)
        except Exception:
            continue
    raise UnicodeDecodeError("encoding", b"", 0, 1, "CSV encoding not supported")


# ============================================================
# main loader
# ============================================================

def load_sbi_csv(path: str | Path) -> pd.DataFrame:
    """
    return DataFrame:
      symbol, symbolname,
      pre_change_pct, pre_volume,
      buy_qty, sell_qty,
      imbalance
    """

    path = Path(path)
    if not path.exists():
        logger.warning(f"⚠ SBI CSV not found: {path}")
        return pd.DataFrame()

    try:
        df = _read_csv_safe(path)
    except Exception as e:
        logger.error(f"❌ CSV read failed: {e}")
        return pd.DataFrame()

    # --------------------------------------------------------
    # 列名正規化
    # --------------------------------------------------------
    df = df.rename(columns={
        "銘柄コード": "symbol",
        "銘柄名": "symbolname",
        "前日比（％）": "pre_change_pct",
        "出来高": "pre_volume",
        "買気配株数": "buy_qty",
        "売気配株数": "sell_qty",
    })

    required = {
        "symbol", "symbolname",
        "pre_change_pct",
        "pre_volume",
        "buy_qty", "sell_qty",
    }

    missing = required - set(df.columns)
    if missing:
        logger.error(f"❌ required columns missing: {missing}")
        return pd.DataFrame()

    # --------------------------------------------------------
    # symbol 正規化
    # --------------------------------------------------------
    df["symbol"] = (
        df["symbol"]
        .astype(str)
        .str.strip()
        .str.replace(".T", "", regex=False)
        .str.zfill(4)
    )

    # --------------------------------------------------------
    # 数値正規化
    # --------------------------------------------------------
    df["pre_volume"] = _to_int_series(df["pre_volume"])
    df["buy_qty"] = _to_int_series(df["buy_qty"])
    df["sell_qty"] = _to_int_series(df["sell_qty"])

    df["pre_change_pct"] = _to_float_series(df["pre_change_pct"])

    # --------------------------------------------------------
    # imbalance（高速ベクトル）
    # --------------------------------------------------------
    df["imbalance"] = calc_imbalance_series(
        df["buy_qty"], df["sell_qty"]
    )

    logger.info(f"✅ SBI CSV loaded rows={len(df)} from {path.name}")

    return df


# ============================================================
# standalone test
# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # テスト用（適宜変更）
    test_path = Path("sample_sbi_preopen.csv")
    df = load_sbi_csv(test_path)

    print(df.head())
    print("rows:", len(df))
