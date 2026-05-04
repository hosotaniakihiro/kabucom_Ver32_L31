# ============================================================
# AI/build_train_csv_sell.py
# Ver24-FINAL-ABSOLUTE-STANDALONE
# ------------------------------------------------------------
# ✔ DBパス決め打ちなし
# ✔ ディレクトリ内の全DBを走査
# ✔ trade_history / entry_log / exit_log を自動検出
# ✔ 必ず「なぜ無理か」を表示
# ============================================================

import sqlite3
import pandas as pd
from pathlib import Path
import sys

# ============================================================
# 設定：DB探索ルート（ここだけ調整）
# ============================================================

SEARCH_DIRS = [
    Path("Y:/stock_data"),
    Path("Y:/stock_price_data"),
    Path("D:/script/python/kabu"),
]

OUT_CSV = Path("AI/train_data_sell.csv")

# ============================================================
# util
# ============================================================

def list_tables(db_path: Path) -> set[str]:
    try:
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {r[0] for r in cur.fetchall()}
        con.close()
        return tables
    except Exception:
        return set()


# ============================================================
# main
# ============================================================

def main():

    print("🔍 DB auto scan START")

    found = False

    for root in SEARCH_DIRS:
        if not root.exists():
            continue

        for db in root.rglob("*.db"):
            tables = list_tables(db)
            if not tables:
                continue

            print(f"\n📦 DB FOUND: {db}")
            print(f"   tables: {sorted(tables)}")

            con = sqlite3.connect(db)

            # --------------------------------------------
            # entry_log + exit_log（最優先）
            # --------------------------------------------
            if {"entry_log", "exit_log"} <= tables:
                print("🚀 using entry_log + exit_log")

                df = pd.read_sql(
                    """
                    SELECT
                        e.symbol,
                        e.is_buy,
                        e.entry_price,
                        e.qty,
                        e.entry_source,
                        e.trigger_type,
                        e.ranking_strength,
                        e.volume_speed,
                        e.volume_ratio,
                        x.exit_price,
                        x.pnl,
                        x.pnl_pct,
                        x.holding_seconds
                    FROM entry_log e
                    JOIN exit_log x
                      ON e.trade_id = x.trade_id
                    WHERE e.is_buy = 0
                    """,
                    con,
                )

                if not df.empty:
                    df["y"] = (df["pnl"] < 0).astype(int)
                    df = df.drop(columns=["pnl"])
                    found = True

            # --------------------------------------------
            # trade_history（フォールバック）
            # --------------------------------------------
            elif "trade_history" in tables:
                print("⚠ using trade_history")

                df = pd.read_sql(
                    """
                    SELECT
                        symbol,
                        side,
                        qty,
                        price,
                        pnl,
                        realized_pnl
                    FROM trade_history
                    WHERE side = 'SELL'
                    """,
                    con,
                )

                if not df.empty:
                    df["y"] = (df["pnl"] < 0).astype(int)
                    df = df.rename(columns={"price": "entry_price"})
                    df = df.drop(columns=["pnl", "realized_pnl"])
                    found = True

            con.close()

            if found:
                print(f"✅ training data extracted from {db}")
                break

        if found:
            break

    if not found:
        print("\n❌ 学習に使えるDBが見つかりません")
        print("👉 trade_history / entry_log が存在するDBを確認してください")
        sys.exit(1)

    # ------------------------------------------------
    # 保存
    # ------------------------------------------------
    df = df.fillna(0)
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT_CSV, index=False, encoding="utf-8")

    print("\n🎉 SELL 学習CSV 作成完了")
    print(f"📄 file : {OUT_CSV}")
    print(f"📊 rows : {len(df)}")
    print(df.head())


# ============================================================
# entry
# ============================================================

if __name__ == "__main__":
    main()
