import duckdb
from database.session import summary_engine

db_path = summary_engine.db_path

conn = duckdb.connect(db_path)

for tf in [1,3,5]:
    conn.execute(f"DROP TABLE IF EXISTS stock_summary_{tf}min")

create_sql = """
CREATE TABLE stock_summary_{tf}min (
    id BIGINT,
    symbol VARCHAR NOT NULL,
    symbolname VARCHAR,
    datetime TIMESTAMP NOT NULL,
    date DATE,
    time_range VARCHAR,
    start_time TIME,
    end_time TIME,
    time TIME,
    source VARCHAR,
    open_price DOUBLE,
    high_price DOUBLE,
    low_price DOUBLE,
    close_price DOUBLE,
    volume DOUBLE,
    vwap DOUBLE,
    ma5 DOUBLE,
    ma25 DOUBLE,
    ma75 DOUBLE,
    ma5_conf DOUBLE,
    ma25_conf DOUBLE,
    ma75_conf DOUBLE,
    ma75_slope DOUBLE,
    volume_slope DOUBLE,
    vwap_slope DOUBLE,
    slope_atr_scaled DOUBLE,
    ema12 DOUBLE,
    ema26 DOUBLE,
    macd DOUBLE,
    signal DOUBLE,
    hist DOUBLE,
    rsi DOUBLE,
    rci DOUBLE,
    atr DOUBLE,
    bb_mid DOUBLE,
    bb_upper DOUBLE,
    bb_lower DOUBLE,
    bb_width DOUBLE,
    score_buy DOUBLE,
    score_sell DOUBLE,
    last_update TIMESTAMP,
    UNIQUE(symbol, datetime)
);
"""

for tf in [1,3,5]:
    conn.execute(create_sql.format(tf=tf))

conn.close()

print("DuckDB schema initialized correctly.")