# =========================================================
# tools/sync_excel_to_db.py（Ver14.16 重複統合対応）
# =========================================================
import os
import pandas as pd
import sqlite3
import logging
from configparser import ConfigParser

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# =========================================================
# Excel 読込処理
# =========================================================
def load_excel_symbols(path: str) -> pd.DataFrame:
    """Excelファイルから銘柄リストを読み込み、不要列・空行を除去"""
    if not os.path.exists(path):
        raise FileNotFoundError(f"Excelファイルが存在しません: {path}")

    df = pd.read_excel(path, dtype=str).fillna("")

    # ✅ 不要列削除（Unnamed）
    df = df.loc[:, ~df.columns.str.contains("^Unnamed", case=False)]

    # ✅ カラム名統一
    df.columns = [str(c).strip().lower() for c in df.columns]

    required_cols = {"symbol", "symbolname", "buy_target", "sell_target"}
    if not required_cols.issubset(df.columns):
        raise ValueError(f"Excelに必要な列が不足しています: {required_cols}")

    # ✅ 空行削除
    df = df[df["symbol"].astype(str).str.strip() != ""]

    # ✅ 正規化
    def normalize_flag(x):
        return 1 if str(x).strip() in ["〇", "○", "◯", "1", "✔", "✓", "true", "True"] else 0

    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["symbolname"] = df["symbolname"].astype(str).str.strip()
    df["buy_target"] = df["buy_target"].apply(normalize_flag)
    df["sell_target"] = df["sell_target"].apply(normalize_flag)

    # ✅ 重複symbolをマージ（BUY/SELL両方を最大値で統合）
    df = (
        df.groupby(["symbol", "symbolname"], as_index=False)
        .agg({"buy_target": "max", "sell_target": "max"})
    )

    logger.info(f"✅ Excel読込完了（不要列・重複統合済）: {len(df)}件 ({os.path.basename(path)})")
    return df[["symbol", "symbolname", "buy_target", "sell_target"]]

# =========================================================
# DB 更新処理
# =========================================================
def update_symbol_flags_db(df: pd.DataFrame, db_path: str):
    """DBにsymbol_flagsテーブルを再生成して保存"""
    if df is None or df.empty:
        raise ValueError("Excelから有効なデータを取得できませんでした。")

    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE IF EXISTS symbol_flags")
        conn.execute("""
            CREATE TABLE symbol_flags (
                symbol TEXT PRIMARY KEY,
                symbolname TEXT,
                buy_target INTEGER,
                sell_target INTEGER
            )
        """)
        df.to_sql("symbol_flags", conn, if_exists="append", index=False)

    logger.info(f"✅ symbol_flags DBを更新しました ({len(df)}件): {db_path}")

# =========================================================
# メイン同期関数
# =========================================================
def sync_excel_to_db():
    """Excel→symbol_flags.db 同期実行"""
    logger.info("📊 Excel→DB 同期開始（symbol_flags 更新）")

    conf = ConfigParser()
    conf.read("settings.ini", encoding="utf-8")

    base_path = conf.get("paths", "base_path", fallback="y:/stock_price_data/")
    excel_path = conf.get("paths", "excel_meigara", fallback="y:/kabu/kabu_station_API_meigara.xlsx")
    db_path = os.path.join(base_path, "symbol_flags.db")

    df = load_excel_symbols(excel_path)
    update_symbol_flags_db(df, db_path)

    logger.info("🎉 Excel→symbol_flags 同期完了！")

# =========================================================
# エントリポイント
# =========================================================
if __name__ == "__main__":
    try:
        sync_excel_to_db()
    except Exception as e:
        logger.error(f"❌ Excel→DB同期エラー: {e}", exc_info=True)
        logger.error(f"❌ 同期失敗: {e}")
