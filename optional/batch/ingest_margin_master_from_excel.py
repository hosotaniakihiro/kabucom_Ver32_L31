# ============================================================
# ingest_margin_master_from_excel.py
# ------------------------------------------------------------
# ・信用取引規制マスタ（Excel）を optional_data.db に取り込む
# ・既存ロジック維持（全削除 → 再投入）
# ・paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import sqlite3
import pandas as pd
from pathlib import Path
import logging

from config.paths import get_path

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# paths.py 経由パス（デフォルト）
# ------------------------------------------------------------
DB_OPTIONAL: Path = get_path("optional_db")
DEFAULT_EXCEL_PATH: Path = get_path("raw_kabu") / "kabu_station_API_meigara.xlsx"


# ------------------------------------------------------------
def ingest_margin_master_from_excel(
    db_path: Path | None = None,
    base_dir: Path | None = None,
):
    """
    信用取引マスタ（Excel）を DB に取り込む

    Args:
        db_path : optional_data.db の Path（省略時は paths.py）
        base_dir: Excel を探す基準ディレクトリ（省略時は paths.py）
    """

    db_path = Path(db_path) if db_path else DB_OPTIONAL
    excel_path = (
        Path(base_dir) / DEFAULT_EXCEL_PATH.name
        if base_dir else DEFAULT_EXCEL_PATH
    )

    logger.info("📥 ingest_margin_master_from_excel START")
    logger.info(f" DB = {db_path}")
    logger.info(f" EXCEL = {excel_path}")

    if not excel_path.exists():
        logger.warning(f"⚠ Excel not found: {excel_path}")
        return

    if not db_path.exists():
        logger.warning(f"⚠ DB not found: {db_path}")
        return

    # --------------------------------------------------------
    # Excel 読み込み
    # --------------------------------------------------------
    try:
        df = pd.read_excel(excel_path)
    except Exception:
        logger.exception("❌ Excel read failed")
        return

    if df.empty:
        logger.warning("⚠ Excel is empty")
        return

    # --------------------------------------------------------
    # 正規化（既存仕様維持）
    # --------------------------------------------------------
    df.columns = [str(c).strip() for c in df.columns]

    if "銘柄コード" not in df.columns:
        logger.error("❌ Excel に '銘柄コード' 列がありません")
        return

    df["symbol"] = df["銘柄コード"].astype(str).str.strip()
    df = df[df["symbol"] != ""]

    if df.empty:
        logger.warning("⚠ 有効な銘柄コードがありません")
        return

    # --------------------------------------------------------
    # DB 書き込み
    # --------------------------------------------------------
    try:
        with sqlite3.connect(db_path) as con:
            cur = con.cursor()

            # テーブル作成（既存互換）
            cur.execute("""
                CREATE TABLE IF NOT EXISTS margin_master (
                    symbol TEXT PRIMARY KEY,
                    margin_status TEXT,
                    note TEXT,
                    updated_at TEXT
                )
            """)

            # 全削除 → 再投入（既存挙動）
            cur.execute("DELETE FROM margin_master")

            for _, r in df.iterrows():
                cur.execute("""
                    INSERT OR REPLACE INTO margin_master
                    (symbol, margin_status, note, updated_at)
                    VALUES (?,?,?,datetime('now'))
                """, (
                    r.get("symbol"),
                    r.get("信用区分"),
                    r.get("備考"),
                ))

            con.commit()

        logger.info(f"✅ margin_master ingested rows={len(df)}")

    except Exception:
        logger.exception("❌ DB insert failed")


# ------------------------------------------------------------
# entry point（単体実行用）
# ------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ingest_margin_master_from_excel()
