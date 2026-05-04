# ============================================================
# test_summary_full_pipeline.py（Ver23-FINAL / B案）
# ------------------------------------------------------------
# ・summary_loader → heavy → scoring → initialize_from_db
# ・save_summary_to_db を使わず直接 INSERT
# ・テーブル定義が軽量なので heavy/scoring カラムは削除して保存
# ============================================================

import os
import sqlite3
import datetime as dt
import pandas as pd
import sys
import pytest

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from trading.summary.summary_controller import summary_controller
import trading.summary.summary_loader as summary_loader
from indicators import add_heavy_indicators
from scoring.scoring_engine import apply_scoring
from global_state import global_data


# ============================================================
# テスト用 summaryDB（空）作成
# ============================================================
def create_test_summary_db(db_path):
    with sqlite3.connect(db_path) as conn:
        for table in ["stock_summary_1min", "stock_summary_3min", "stock_summary_5min"]:
            conn.execute(f"""
                CREATE TABLE {table} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT,
                    symbolname TEXT,
                    date DATE,
                    time_range TEXT,
                    start_time TIME,
                    end_time TIME,
                    time TIME,
                    open_price REAL,
                    high_price REAL,
                    low_price REAL,
                    close_price REAL,
                    volume REAL,
                    vwap REAL,
                    prev_close REAL,
                    last_update DATETIME
                );
            """)


# ============================================================
# フルパイプラインテスト
# ============================================================
def test_full_pipeline(tmp_path, monkeypatch):

    today = dt.date.today()
    prev = today - dt.timedelta(days=1)
    prev2 = today - dt.timedelta(days=2)

    db_today = tmp_path / f"summary{today:%Y%m%d}.db"
    db_prev = tmp_path / f"summary{prev:%Y%m%d}.db"
    db_prev2 = tmp_path / f"summary{prev2:%Y%m%d}.db"

    for p in [db_today, db_prev, db_prev2]:
        create_test_summary_db(p)

    # summary_loader の DB パスを tmp_path に差し替え
    monkeypatch.setattr(
        summary_loader,
        "_summary_db_path",
        lambda d: os.path.join(tmp_path, f"summary{d:%Y%m%d}.db")
    )

    # ============================================================
    # heavy + scoring データ作成
    # ============================================================
    df_base = pd.DataFrame([{
        "symbol": "1234",
        "symbolname": "テスト銘柄",
        "date": today,
        "time_range": "09:00 - 09:01",
        "start_time": dt.time(9, 0),
        "end_time": dt.time(9, 1),
        "time": dt.time(9, 0),
        "open_price": 100,
        "high_price": 110,
        "low_price": 90,
        "close_price": 105,
        "volume": 500,
        "vwap": 103,
        "prev_close": 98,
    }])

    df_h = add_heavy_indicators(df_base, 1)
    df_s = apply_scoring(df_h, 1)

    # ============================================================
    # heavy/scoring カラムを削除して DB に INSERT（B案）
    # ============================================================
    db_cols = [
        "symbol", "symbolname", "date", "time_range",
        "start_time", "end_time", "time",
        "open_price", "high_price", "low_price", "close_price",
        "volume", "vwap", "prev_close", "last_update"
    ]

    df_insert = df_s.filter(db_cols)

    with sqlite3.connect(db_today) as conn:
        df_insert.to_sql("stock_summary_1min", conn, if_exists="append", index=False)

    # ============================================================
    # summary_loader 動作テスト
    # ============================================================
    summary_dict = summary_loader.load_summary_from_db()
    df_loaded = summary_dict[1]

    assert not df_loaded.empty, "❌ summary_loader がデータを読み込めていない"
    assert "close_price" in df_loaded.columns

    global_data.set_push_df(pd.DataFrame())

    # ============================================================
    # initialize_from_db 動作確認
    # ============================================================
    summary_controller.initialize_from_db()

    assert not summary_controller.summary_1min.empty
    assert "score_buy" in summary_controller.summary_1min.columns
    assert "score_sell" in summary_controller.summary_1min.columns

    print("🔥 FULL PIPELINE TEST → 完全成功！")
