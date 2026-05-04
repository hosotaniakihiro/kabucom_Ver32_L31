# ============================================================
# import_taisyaku_meigara.py
# ------------------------------------------------------------
# Excel → symbol_flags.db
# 空売り（貸借銘柄）判定用
# ============================================================

import sqlite3
import pandas as pd

# ------------------------------------------------------------
# 設定
# ------------------------------------------------------------
EXCEL_PATH = r"Y:\kabu\taisyakumeigara_20251201_list.xlsx"
DB_PATH = r"Y:\stock_price_data\symbol_flags.db"

# ------------------------------------------------------------
# Excel 読み込み（1–2行目はヘッダー）
# ------------------------------------------------------------
df = pd.read_excel(
    EXCEL_PATH,
    header=1,           # ← 2行目をヘッダーとして扱う
    usecols=[0, 1, 2, 3]
)

# ★ DB 構造に合わせる
df.columns = [
    "symbol",
    "symbolname",
    "market_type",   # ← 修正
    "credit_type"
]

# 銘柄コードを4桁文字列に統一
df["symbol"] = df["symbol"].astype(str).str.zfill(4)

# ------------------------------------------------------------
# short_ok 判定
# ------------------------------------------------------------
df["short_ok"] = df["credit_type"].apply(
    lambda x: 1 if isinstance(x, str) and "貸借" in x else 0
)

print("📊 読み込み件数:", len(df))
print(df["short_ok"].value_counts())

# ------------------------------------------------------------
# DB UPSERT
# ------------------------------------------------------------
conn = sqlite3.connect(DB_PATH)
cur = conn.cursor()

# --- UPDATE ---
sql = """
UPDATE symbol_flags
SET
    symbolname  = ?,
    market_type = ?,
    credit_type = ?,
    short_ok    = ?,
    updated_at  = CURRENT_TIMESTAMP
WHERE symbol = ?
"""

# --- INSERT ---
insert_sql = """
INSERT INTO symbol_flags
(symbol, symbolname, market_type, credit_type, short_ok, updated_at)
VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
"""

updated = 0
inserted = 0

for _, r in df.iterrows():
    params = (
        r["symbolname"],
        r["market_type"],
        r["credit_type"],
        int(r["short_ok"]),
        r["symbol"],
    )

    cur.execute(sql, params)

    if cur.rowcount == 0:
        cur.execute(
            insert_sql,
            (
                r["symbol"],
                r["symbolname"],
                r["market_type"],
                r["credit_type"],
                int(r["short_ok"]),
            )
        )
        inserted += 1
    else:
        updated += 1

conn.commit()
conn.close()

print(f"✅ 更新: {updated} 件 / 新規: {inserted} 件")
