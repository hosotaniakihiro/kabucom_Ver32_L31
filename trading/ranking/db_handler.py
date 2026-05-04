import sqlite3, datetime as dt, pandas as pd
from database import Session_ranking

def save_data_to_db(data, table_name: str, db_path: str):
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            CREATE TABLE IF NOT EXISTS "{table_name}" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT,
                symbolname TEXT,
                current_price REAL,
                change_percentage REAL,
                trading_volume REAL,
                turnover REAL,
                created_at TEXT
            )
        """)

        rows = [
            (r.get("Symbol"), r.get("IssueName"), r.get("CurrentPrice"),
             r.get("ChangePercentage"), r.get("TradingVolume"), r.get("TurnoverValue"),
             dt.datetime.now().isoformat())
            for r in data.get("Ranking", [])
        ]

        if rows:
            cursor.executemany(
                f"""INSERT INTO "{table_name}"
                (symbol, symbolname, current_price, change_percentage, trading_volume, turnover, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", rows
            )
            conn.commit()
    finally:
        cursor.close()
        conn.close()

def get_top_symbols_from_ranking(db_path: str, ranking_type="値上がり率", market="TP", limit=50):
    table_name = f"{ranking_type}_{market}"
    try:
        conn = sqlite3.connect(db_path)
        df = pd.read_sql(f"""
            SELECT symbol, symbolname, price, change_percent,
                   trading_volume, turnover
            FROM "{table_name}"
            ORDER BY change_percent DESC
            LIMIT ?""", conn, params=(limit,))
        conn.close()
        return df
    except Exception as e:
        print(f"❌ get_top_symbols_from_ranking エラー: {e}")
        return pd.DataFrame()
def save_ranking_from_df(session, df: pd.DataFrame):
    """
    DataFrameをRankingテーブルに保存
    """
    if df is None or df.empty:
        return

    # カラム名を Ranking モデル用にマッピング
    rename_map = {
        "price": "current_price",
        "change_percent": "change_percentage",
        "trading_value": "turnover",
        "trading_volume": "trading_volume",
    }
    df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})

    # symbolname が無い場合は補完
    if "symbolname" not in df.columns:
        df["symbolname"] = df["symbol"].map(global_data.symbol_name_map)

    rows = []
    for _, row in df.iterrows():
        rows.append(Ranking(
            type=row.get("type", ""),
            market=row.get("market", "ALL"),
            no=row.get("no"),
            symbol=row.get("symbol"),
            symbolname=row.get("symbolname"),
            categoryname=row.get("categoryname"),
            current_price=row.get("current_price"),
            current_price_time=row.get("current_price_time"),
            change_percentage=row.get("change_percentage"),
            change_ratio=row.get("change_ratio"),
            trading_volume=row.get("trading_volume"),
            turnover=row.get("turnover"),
            trend=row.get("trend"),
            exchange_name=row.get("exchange_name"),
            average_ranking=row.get("average_ranking"),
        ))

    session.bulk_save_objects(rows)
