import sqlite3
import pandas as pd
from pathlib import Path

# ============================================================
# パス設定
# ============================================================
EXCEL_PATH = Path(r"X:\Basic\taisyaku\20260105_list.xlsx")
DB_PATH = Path(r"X:\Basic\symbol_flags.db")
TABLE = "symbol_flags"

# ============================================================
# Excel 読み込み（★ヘッダ行を明示）
# ============================================================
df_excel = pd.read_excel(EXCEL_PATH, header=1)

print("📄 Excel columns:", list(df_excel.columns))

# 列名正規化
df_excel = df_excel.rename(columns={
    "銘柄コード": "symbol",
    "銘柄名": "symbolname",
})

# 必須列チェック
required = {"symbol", "symbolname"}
missing = required - set(df_excel.columns)
if missing:
    raise RuntimeError(f"❌ Excel に必要な列がありません: {missing}")

# 型正規化
df_excel["symbol"] = df_excel["symbol"].astype(str).str.strip()
df_excel["symbolname"] = df_excel["symbolname"].astype(str).str.strip()

# 空行除外
df_excel = df_excel[
    (df_excel["symbol"] != "") &
    (df_excel["symbolname"] != "") &
    (~df_excel["symbolname"].isin(["nan", "None"]))
]

print(f"📄 Excel valid rows: {len(df_excel)}")

# ============================================================
# DB 更新（NULL / 空のみ）
# ============================================================
conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

updated = 0

for _, row in df_excel.iterrows():
    cur.execute(
        f"""
        UPDATE {TABLE}
        SET symbolname = ?
        WHERE symbol = ?
          AND (symbolname IS NULL OR symbolname = '')
        """,
        (row["symbolname"], row["symbol"]),
    )

    if cur.rowcount:
        updated += cur.rowcount

conn.commit()
conn.close()

print(f"✅ symbolname updated rows: {updated}")
