# ============================================================
# yfinance_download_company_base.py
# ------------------------------------------------------------
# ・Yahoo Finance から銘柄別データを取得
# ・日付別ディレクトリに保存
# ・paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import pandas as pd
import yfinance as yf
from datetime import datetime
from pathlib import Path
import logging

from config.paths import get_path

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# paths.py 経由
# ------------------------------------------------------------
RAW_YAHOO_DIR: Path = get_path("raw_yahoo")
SYMBOL_LIST_FILE: Path = get_path("basic") / "AllSymbols" / "data_j.xls"


# ------------------------------------------------------------
def load_symbol_list() -> pd.DataFrame:
    """
    銘柄一覧（Excel）を読み込む
    """
    if not SYMBOL_LIST_FILE.exists():
        logger.error(f"❌ symbol list not found: {SYMBOL_LIST_FILE}")
        return pd.DataFrame()

    try:
        df = pd.read_excel(SYMBOL_LIST_FILE)
        return df
    except Exception:
        logger.exception("❌ failed to load symbol list")
        return pd.DataFrame()


# ------------------------------------------------------------
def download_company_data():
    """
    Yahoo Finance から銘柄別データを取得し保存
    """
    today = datetime.now().strftime("%Y%m%d")
    out_dir = RAW_YAHOO_DIR / today
    out_dir.mkdir(parents=True, exist_ok=True)

    df_symbols = load_symbol_list()
    if df_symbols.empty:
        logger.warning("⚠ symbol list empty")
        return

    # 既存仕様：銘柄コード列
    if "銘柄コード" not in df_symbols.columns:
        logger.error("❌ '銘柄コード' 列が見つかりません")
        return

    for _, row in df_symbols.iterrows():
        code = str(row["銘柄コード"]).strip()
        if not code or code == "nan":
            continue

        ticker = f"{code}.T"
        try:
            df = yf.download(
                ticker,
                interval="1m",
                period="1d",
                progress=False,
            )
        except Exception:
            logger.exception(f"❌ yahoo download failed: {ticker}")
            continue

        if df.empty:
            logger.warning(f"⚠ no data: {ticker}")
            continue

        df.reset_index(inplace=True)
        df["symbol"] = code

        out_file = out_dir / f"{code}.csv"
        try:
            df.to_csv(out_file, index=False)
            logger.info(f"✅ saved: {out_file}")
        except Exception:
            logger.exception(f"❌ failed to save: {out_file}")


# ------------------------------------------------------------
# entry point
# ------------------------------------------------------------
if __name__ == "__main__":
    download_company_data()
