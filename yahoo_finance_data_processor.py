import time
import sys
import pandas as pd
import requests
import os
import json
import sqlite3
import datetime as dt
from yahoo_finance_api2 import share as yapi2  # 使用されていないが、元のコードにあったため残す
from yahoo_finance_api2.exceptions import YahooFinanceError  # 使用されていないが、元のコードにあったため残す
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.trend import MACD
from ta.volatility import BollingerBands
import traceback  # エラーハンドリング用

# --- 設定 ---
OUTPUT_BASE_DIR = r"Y:\y_stock_data_price"
EXCEL_FILE_PATH = r"y:\kabu\data_j.xls"  # または "y:\\kabu\\meigaraichiran.xlsx"
DAYS_TO_FETCH_API = 1  # APIから取得する最新の日数。計算に必要な期間よりは短くて良い。
# ローカルデータを読み込む際の余裕日数。例えば75MAなら最低75本分のデータが必要なので、それに応じた日数を設定。
# 5分足の場合、1日あたり約60本なので、75本なら1.5日分程度のデータがあれば良い。
# しかし、市場の休業日なども考慮して、少し余裕を持たせることを推奨します。
DAYS_TO_LOAD_LOCAL = 3  # 過去N日分のローカルデータを読み込む (例: 75MAのために十分な期間)

def create_stock_summary_table_if_not_exists(conn, table_name='stock_summary'):
    create_table_sql = f"""
    CREATE TABLE IF NOT EXISTS {table_name} (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        time_range TEXT,
        symbol TEXT,
        symbolname TEXT,
        open_price REAL,
        high_price REAL,
        low_price REAL,
        close_price REAL,
        volume INTEGER,
        value INTEGER,
        ma5 REAL,
        ma25 REAL,
        ma75 REAL,
        ema12 REAL,
        ema26 REAL,
        macd REAL,
        signal REAL,
        rsi REAL,
        slowk REAL,
        slowd REAL,
        vwap REAL,
        bb_mavg REAL,
        bb_upper REAL,
        bb_lower REAL,
        date TEXT
    );
    """
    try:
        conn.execute(create_table_sql)
        conn.commit()
    except Exception as e:
        print(f"❌ テーブル作成中にエラーが発生しました: {e}")
        traceback.print_exc()

# --- ヘルパー関数 (変更あり・追加あり) ---

def calculate_moving_averages(df):
    """5MA, 25MA, 75MAを計算してデータフレームに追加"""
    # データをコピーしてから計算を行うことで、SettingWithCopyWarningを回避
    df_copy = df.copy()
    df_copy['ma5'] = df_copy['close_price'].rolling(window=5, min_periods=1).mean()
    df_copy['ma25'] = df_copy['close_price'].rolling(window=25, min_periods=1).mean()
    df_copy['ma75'] = df_copy['close_price'].rolling(window=75, min_periods=1).mean()
    return df_copy


def calculate_macd(df, short_window=12, long_window=26, signal_window=9):
    """MACDとシグナルを計算してデータフレームに追加"""
    df_copy = df.copy()
    df_copy['ema12'] = df_copy['close_price'].ewm(span=short_window, adjust=False).mean()
    df_copy['ema26'] = df_copy['close_price'].ewm(span=long_window, adjust=False).mean()
    df_copy['macd'] = df_copy['ema12'] - df_copy['ema26']
    df_copy['signal'] = df_copy['macd'].ewm(span=signal_window, adjust=False).mean()
    return df_copy


def calculate_rsi_stoch(df):
    """RSIとストキャスティクス (SlowK, SlowD) を計算してデータフレームに追加"""
    df_copy = df.copy()
    if {'close_price', 'high_price', 'low_price'}.issubset(df_copy.columns):
        try:
            rsi_calc = RSIIndicator(close=df_copy['close_price'], window=14)
            df_copy['rsi'] = rsi_calc.rsi()

            stoch = StochasticOscillator(
                high=df_copy['high_price'],
                low=df_copy['low_price'],
                close=df_copy['close_price'],
                window=14,
                smooth_window=3
            )
            df_copy['slowk'] = stoch.stoch()
            df_copy['slowd'] = stoch.stoch_signal()
        except Exception as e:
            print(f"⚠ テクニカル指標 (RSI/Stoch) 計算中にエラー: {e}")
            traceback.print_exc()
    else:
        print("⚠ RSI/Stoch 計算に必要なカラム (close_price, high_price, low_price) が存在しません。")
    return df_copy


def calculate_bollinger_bands(df):
    """ボリンジャーバンド（20期間, 2σ）を計算してデータフレームに追加"""
    df_copy = df.copy()
    try:
        bb = BollingerBands(close=df_copy['close_price'], window=20, window_dev=2)
        df_copy['bb_mavg'] = bb.bollinger_mavg().ffill()
        df_copy['bb_upper'] = bb.bollinger_hband().ffill()
        df_copy['bb_lower'] = bb.bollinger_lband().ffill()
    except Exception as e:
        print(f"⚠ ボリンジャーバンド計算中にエラー: {e}")
        traceback.print_exc()
    return df_copy
def calculate_vwap(df):
    """出来高加重平均価格 VWAP を計算してデータフレームに追加"""
    df_copy = df.copy()
    try:
        # VWAP = (累積約定価格×出来高の合計) ÷ (累積出来高)
        df_copy['cum_pv'] = (df_copy['close_price'] * df_copy['volume']).cumsum()
        df_copy['cum_volume'] = df_copy['volume'].cumsum()
        df_copy['vwap'] = df_copy['cum_pv'] / df_copy['cum_volume']
        # 不要な中間カラムを削除（必要に応じて残してもOK）
        df_copy.drop(columns=['cum_pv', 'cum_volume'], inplace=True)
    except Exception as e:
        print(f"⚠ VWAP計算中にエラー: {e}")
        traceback.print_exc()
    return df_copy


def get_data_batch(symbol, start_date_ts, end_date_ts, interval='5m'):
    """
    Yahoo Finance APIから指定期間のデータを一括取得し整形。
    ここではテクニカル指標の計算は行わない。
    """
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?period1={int(start_date_ts)}&period2={int(end_date_ts)}&interval={interval}"

    retries = 3
    for attempt in range(retries):
        try:
            response = requests.get(url, headers={'User-Agent': 'Mozilla/5.0'})
            if response.status_code == 429:
                wait_time = 10 * (2 ** attempt)
                print(f"リクエスト制限 (429)。{wait_time}秒待機します。")
                time.sleep(wait_time)
                continue
            response.raise_for_status()

            data = response.json()
            if 'chart' not in data or 'result' not in data['chart'] or not data['chart']['result']:
                return pd.DataFrame()

            result = data['chart']['result'][0]
            quote = result['indicators']['quote'][0]

            timestamps = result.get('timestamp', [])
            if not timestamps:
                return pd.DataFrame()

            df = pd.DataFrame({
                'time_range': pd.to_datetime(timestamps, unit='s') + pd.Timedelta(hours=9),  # JSTに変換
                'open_price': quote['open'],
                'high_price': quote['high'],
                'low_price': quote['low'],
                'close_price': quote['close'],
                'volume': quote['volume']
            })

            df = df.dropna()
            # 昼休み時間 (11:30 - 12:30) のデータを削除
            df = df[~((df['time_range'].dt.time >= dt.time(11, 30)) & (df['time_range'].dt.time < dt.time(12, 30)))]
            df['time_range'] = df['time_range'].dt.strftime("%Y-%m-%d %H:%M:%S")

            return df

        except (requests.exceptions.RequestException, ValueError) as e:
            print(f"データ取得エラー ({symbol}): {e}")
            if attempt < retries - 1:
                time.sleep(5)
            else:
                return pd.DataFrame()  # エラー時は空のDataFrameを返す
        except Exception as e:
            print(f"予期せぬAPI取得エラー ({symbol}): {e}")
            traceback.print_exc()
            return pd.DataFrame()


def load_local_data(stock_code, output_base_dir, days_to_load, table_name='stock_summary'):
    """
    ローカルのSQLite DBから過去N日分のデータを読み込む。
    各日のDBファイルからデータを集約する。
    """
    all_local_data = pd.DataFrame()
    code_str = str(stock_code).zfill(4)

    # 読み込み開始日を計算
    today = dt.date.today()
    for i in range(days_to_load + 5):  # 余裕を持って数日多めに遡る
        target_date = today - dt.timedelta(days=i)
        db_path = os.path.join(output_base_dir, f"summary{target_date.strftime('%Y%m%d')}.db")

        if os.path.exists(db_path):
            try:
                conn = sqlite3.connect(db_path)
                # 該当銘柄のデータのみを読み込む
                query = f"SELECT * FROM {table_name} WHERE symbol = '{code_str}' ORDER BY time_range ASC"
                daily_df = pd.read_sql_query(query, conn)
                conn.close()

                if not daily_df.empty:
                    all_local_data = pd.concat([all_local_data, daily_df], ignore_index=True)
            except Exception as e:
                print(f"❌ ローカルデータ読み込みエラー ({code_str}, {os.path.basename(db_path)}): {e}")
                traceback.print_exc()

    if not all_local_data.empty:
        # time_rangeをdatetimeオブジェクトに変換し、ソートして重複を削除
        all_local_data['time_range'] = pd.to_datetime(all_local_data['time_range'])
        all_local_data = all_local_data.sort_values(by='time_range').drop_duplicates(subset=['time_range'], keep='last')
        # 最新のDAYS_TO_LOAD_LOCAL日数分のデータに絞り込む（計算に必要な部分だけ残す）
        # 厳密には、テクニカル指標の最長期間 (例: 75MA) + API取得期間のデータがあれば十分
        # ここでは、最新から必要期間分だけを保持する
        # 具体的な計算に使う期間 + 余裕分の日数で絞る
        # 例えば、75MAの計算には最低75本のデータが必要。5分足だと1日約60本なので、2日分あれば十分。
        # しかし、計算の安定性や他の指標も考慮し、指定日数分をそのまま使う。
        all_local_data = all_local_data[
            all_local_data['time_range'] >= (dt.datetime.now() - dt.timedelta(days=days_to_load)).strftime(
                "%Y-%m-%d %H:%M:%S")]

    return all_local_data


def save_to_sqlite_daily(db_path, data_df_single_day, stock_code, stock_name, table_name='stock_summary'):
    """
    1日分のデータを指定されたDBパスに保存。
    既に存在するデータは上書き（または更新）することを考慮した。
    """
    try:
        db_dir = os.path.dirname(db_path)
        if not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)

        data_df_single_day['symbol'] = str(stock_code).zfill(4)
        data_df_single_day['symbolname'] = stock_name
        data_df_single_day['date'] = pd.to_datetime(data_df_single_day['time_range']).dt.strftime("%Y-%m-%d")

        conn = sqlite3.connect(db_path)
        create_stock_summary_table_if_not_exists(conn, table_name)
        cursor = conn.cursor()

        # 既存データを削除して新しいデータで置き換える (その銘柄・その日付のデータのみ)
        # これにより、同じ日の同じ銘柄の重複を防ぎ、再計算結果を反映できる
        # 厳密にはtime_rangeでuniqueだが、ここではdate+symbolで削除してappendとする
        delete_date = data_df_single_day['date'].iloc[0]  # その日の日付
        cursor.execute(f"DELETE FROM {table_name} WHERE symbol = ? AND date = ?",
                       (str(stock_code).zfill(4), delete_date))
        conn.commit()

        # 新しいデータを追加
        data_df_single_day.to_sql(table_name, conn, if_exists='append', index=False)
        conn.close()
        # print(f"  ✅ {stock_code} のデータを {os.path.basename(db_path)} に保存しました。")
    except sqlite3.Error as e:
        print(f"❌ SQLite保存エラー ({stock_code}, {os.path.basename(db_path)}): {e}")
        traceback.print_exc()
    except Exception as e:
        print(f"❌ その他のエラー ({stock_code}, {os.path.basename(db_path)}): {e}")
        traceback.print_exc()


def process_and_save_all_stock_data_batch(excel_file_path, output_base_dir, days_to_fetch_api, days_to_load_local):
    """
    Excelから銘柄リストを読み込み、ローカルデータとAPIデータを結合し、
    指標計算後、日ごとのDBファイルに分割して保存する。
    """
    try:
        df_excel = pd.read_excel(excel_file_path)
        valid_markets = ["プライム（内国株式）", "スタンダード（内国株式）", "グロース（内国株式）"]
        df_excel = df_excel[df_excel['市場・商品区分'].isin(valid_markets)]

        # --- APIリクエストの期間を決定 ---
        # APIから取得するのは最新のDAYS_TO_FETCH_API日数分
        end_date_for_api = dt.datetime.now().replace(hour=15, minute=0, second=0)  # 今日の終値時間まで
        start_date_for_api = (end_date_for_api - dt.timedelta(days=days_to_fetch_api)).replace(hour=9, minute=0,
                                                                                               second=0)  # N日前の開始時刻

        print(f"\n--- 株価データ取得とテクニカル指標計算を開始します ---")
        print(f"対象Excel: {os.path.basename(excel_file_path)}")
        print(f"出力ベースディレクトリ: {output_base_dir}")
        print(
            f"APIデータ取得期間: {start_date_for_api.strftime('%Y-%m-%d %H:%M:%S')} から {end_date_for_api.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"ローカルから過去 {days_to_load_local} 日分のデータを読み込み、結合して計算します。")

        processed_symbols_count = 0
        total_symbols = len(df_excel)

        for index, row in df_excel.iterrows():
            code = row['コード']
            name = row['銘柄名']
            yahoo_symbol = f"{code}.T"

            print(f"\n[{processed_symbols_count + 1}/{total_symbols}] 🚀 {name} ({code}) のデータを処理中...")

            # 1. ローカルから既存データを読み込む
            print(f"  - ローカルから過去 {days_to_load_local} 日分のデータを読み込み中...")
            local_data_df = load_local_data(code, output_base_dir, days_to_load_local)
            # print(f"    ローカルデータ件数: {len(local_data_df)}")

            # 2. Yahoo Finance APIから最新データを取得
            print(f"  - Yahoo Finance APIから最新 {days_to_fetch_api} 日分のデータを取得中...")
            api_data_df = get_data_batch(yahoo_symbol, start_date_for_api.timestamp(), end_date_for_api.timestamp(),
                                         interval='5m')
            # print(f"    APIデータ件数: {len(api_data_df)}")

            if api_data_df.empty and local_data_df.empty:
                print(f"❌ {name} ({code}): ローカルにもAPIにもデータがありませんでした。スキップします。")
                time.sleep(1)
                continue

            # 3. ローカルデータとAPIデータを結合し、重複を排除してソート
            combined_df = pd.DataFrame()
            if not local_data_df.empty and not api_data_df.empty:
                # time_rangeでdatetimeに変換 (load_local_dataでも行われるが念のため)
                local_data_df['time_range'] = pd.to_datetime(local_data_df['time_range'])
                api_data_df['time_range'] = pd.to_datetime(api_data_df['time_range'])
                combined_df = pd.concat([local_data_df, api_data_df], ignore_index=True)
            elif not local_data_df.empty:
                local_data_df['time_range'] = pd.to_datetime(local_data_df['time_range'])
                combined_df = local_data_df
            elif not api_data_df.empty:
                api_data_df['time_range'] = pd.to_datetime(api_data_df['time_range'])
                combined_df = api_data_df

            if combined_df.empty:
                print(f"❌ {name} ({code}): 結合するデータがありませんでした。スキップします。")
                time.sleep(1)
                continue

            # time_rangeを基準に重複を削除し、ソートする (新しいデータを優先)
            # 必須カラム以外は一旦削除して重複をsubsetで指定しやすくする
            # テクニカル指標カラムは再計算されるので削除してOK
            cols_to_keep = ['time_range', 'open_price', 'high_price', 'low_price', 'close_price', 'volume']
            combined_df = combined_df[cols_to_keep].copy()  # 必要なカラムのみ抽出してコピー

            combined_df = combined_df.drop_duplicates(subset=['time_range'], keep='last')
            combined_df = combined_df.sort_values(by='time_range').reset_index(drop=True)

            # time_rangeを文字列に戻す前に一旦計算する
            # テクニカル指標計算のためにカラムのデータ型を確認
            # print(f"結合後データ型: {combined_df.dtypes}")

            # 4. テクニカル指標の計算 (結合されたデータ全体に対して)
            print("  - テクニカル指標を計算中...")
            try:
                # pandasのTimestampをそのまま渡せるように、time_rangeはdatetimeのままにする
                combined_df = calculate_moving_averages(combined_df)
                combined_df = calculate_macd(combined_df)
                combined_df = calculate_rsi_stoch(combined_df)
                combined_df = calculate_bollinger_bands(combined_df)
                combined_df = calculate_vwap(combined_df)
                # SQLite保存用にtime_rangeを文字列に変換 (元のコードの形式に合わせる)
                combined_df['time_range'] = combined_df['time_range'].dt.strftime("%Y-%m-%d %H:%M:%S")

            except Exception as e:
                print(f"❌ {name} ({code}) のテクニカル指標計算中にエラーが発生しました: {e}")
                traceback.print_exc()
                time.sleep(1)
                continue

            # 5. 日付ごとにデータを分割し、DBに保存 (既存データは上書き更新)
            # 保存対象はAPIで取得した最新のデータ期間+その日の最終的な計算結果
            # すでにlocal_data_dfに計算結果がある古いデータは、通常は再保存の必要はない
            # ただし、テクニカル指標の計算結果は、最新のデータが追加されると過去の値も変わる可能性があるため、
            # 最低でもAPI取得期間+テクニカル指標の最長期間分は再保存する必要がある。
            # ここでは、最新のAPI取得期間の日付だけを保存対象とする。
            # または、`DAYS_TO_LOAD_LOCAL` の期間のデータを全て保存し直しても良い（推奨）。

            # 再計算された全期間のデータのうち、保存対象期間のデータのみを抽出
            # ここでは、`start_date_for_api` 以降のデータ、または `DAYS_TO_LOAD_LOCAL` 以降のデータ
            # 最も安全なのは、`DAYS_TO_LOAD_LOCAL` 期間全てを保存し直すこと。

            # 保存する日付リストを生成 (過去DAYS_TO_LOAD_LOCAL日数分)
            dates_to_save = [end_date_for_api.date() - dt.timedelta(days=i) for i in range(days_to_load_local)]
            dates_to_save = sorted(list(set(dates_to_save)), reverse=False)  # 重複除去&古い順に

            print(f"  - 計算結果を日ごとのDBに保存中 ({len(dates_to_save)}日分)...")
            for single_date in dates_to_save:
                # combined_df['time_range']は既に文字列なので、dt.dateに変換して比較
                daily_data_df = combined_df[pd.to_datetime(combined_df['time_range']).dt.date == single_date].copy()

                if not daily_data_df.empty:
                    date_str = single_date.strftime("%Y%m%d")
                    summary_db_path = os.path.join(output_base_dir, f"summary{date_str}.db")
                    save_to_sqlite_daily(summary_db_path, daily_data_df, code, name, table_name='stock_summary')
                # else:
                #     print(f"    - {single_date.strftime('%Y-%m-%d')} の保存データなし。")

            processed_symbols_count += 1
            print(f"✅ {name} ({code}) の処理完了。")
            time.sleep(1)  # APIリクエスト頻度を調整

        print(f"\n--- 全銘柄のデータ処理が完了しました ---")
        print(f"合計 {processed_symbols_count} 銘柄のデータを処理しました。")

    except FileNotFoundError:
        print(f"エラー: 指定されたExcelファイルが見つかりません - {excel_file_path}")
    except Exception as e:
        print(f"処理中に予期せぬエラーが発生しました: {e}")
        traceback.print_exc()


# --- メイン実行部分 ---
if __name__ == "__main__":
    os.makedirs(OUTPUT_BASE_DIR, exist_ok=True)
    # スクリプト実行前に、既存のDBファイルをすべて削除したい場合は、
    # 以下のコメントを外して使用してください。
    # shutil.rmtree(OUTPUT_BASE_DIR, ignore_errors=True) # 既存ディレクトリを削除
    # os.makedirs(OUTPUT_BASE_DIR, exist_ok=True) # 再作成

    process_and_save_all_stock_data_batch(EXCEL_FILE_PATH, OUTPUT_BASE_DIR, DAYS_TO_FETCH_API, DAYS_TO_LOAD_LOCAL)