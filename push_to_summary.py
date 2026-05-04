import pandas as pd
import os
import datetime as dt
from sqlalchemy import create_engine


def calculate_moving_averages(df, column='CurrentPrice', windows=[5, 25, 75]):
    for window in windows:
        df[f'ma{window}'] = df[column].rolling(window=window).mean()
    return df


def calculate_macd(df, column='CurrentPrice', short_window=12, long_window=26, signal_window=9):
    df['ema_short'] = df[column].ewm(span=short_window, adjust=False).mean()
    df['ema_long'] = df[column].ewm(span=long_window, adjust=False).mean()
    df['macd'] = df['ema_short'] - df['ema_long']
    df['signal'] = df['macd'].ewm(span=signal_window, adjust=False).mean()
    return df


def process_push_to_summary(date_str):
    push_db_path = f'sqlite:///y:/Stock_price_data/push{date_str}.db'
    summary_db_path = f'sqlite:///y:/Stock_price_data/summary{date_str}.db'

    if not os.path.exists(push_db_path.replace('sqlite:///', '')):
        print(f"Pushデータベースが見つかりません: {push_db_path}")
        return

    push_engine = create_engine(push_db_path)
    summary_engine = create_engine(summary_db_path)

    df = pd.read_sql("SELECT * FROM stream_data", con=push_engine)

    if df.empty:
        print("Pushデータが空です")
        return

    df['time'] = pd.to_datetime(df['time'])
    df = df.sort_values(by=['Symbol', 'time'])
    df['time_range'] = df['time'].dt.floor('5min')

    summary_df = df.groupby(['Symbol', 'time_range']).agg({
        'SymbolName': 'first',
        'CurrentPrice': ['first', 'max', 'min', 'last'],
        'TradingVolume': 'sum'
    }).reset_index()

    summary_df.columns = ['Symbol', 'time_range', 'SymbolName', 'open_price', 'high_price', 'low_price', 'close_price',
                          'volume']

    summary_df = calculate_moving_averages(summary_df, column='close_price')
    summary_df = calculate_macd(summary_df, column='close_price')

    summary_df.to_sql('stock_summary', con=summary_engine, if_exists='replace', index=False)
    print(f"Summaryデータベースを作成しました: {summary_db_path}")


if __name__ == "__main__":
#    current_date = dt.datetime.now().strftime('%Y%m%d')
    current_date = '20250131'
    process_push_to_summary(current_date)
