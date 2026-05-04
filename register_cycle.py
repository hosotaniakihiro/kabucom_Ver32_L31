# register_cycle.py
import json
import time
import urllib.request
import pandas as pd
import os
from configparser import ConfigParser

conf = ConfigParser()
conf.read("settings.ini", encoding="utf-8")

EXCEL_PATH = conf.get("paths", "excel_path", fallback="d:/kabu/kabu_station_API_meigara.xlsx")

BATCH_SIZE = 50
SLEEP_INTERVAL = 10  # 秒

def load_symbols_from_excel():
    if not os.path.exists(EXCEL_PATH):
        print(f"❌ Excelファイルが存在しません: {EXCEL_PATH}")
        return pd.DataFrame()

    try:
        df = pd.read_excel(EXCEL_PATH, header=None)
        df = df.iloc[:, :3]
        df.columns = ['code', 'name', 'sell_flag']
        df = df[df['code'].notna()]
        df['code'] = df['code'].astype(str).str.zfill(4)
        return df
    except Exception as e:
        print(f"❌ Excel読み込みエラー: {e}")
        return pd.DataFrame()

def get_next_batch(symbols, offset, batch_size=50):
    if not symbols:
        return []
    batch = symbols[offset:offset + batch_size]
    if len(batch) < batch_size:
        batch += symbols[:batch_size - len(batch)]
    return batch[:batch_size]

def register_batch(token, symbols_batch, verbose=False):
    if not symbols_batch:
        if verbose:
            print("⚠️ 空の銘柄リストです")
        return

    obj = {"Symbols": [{"Symbol": str(code).zfill(4), "Exchange": 1} for code in symbols_batch]}
    json_data = json.dumps(obj).encode("utf-8")

    if verbose:
        print("📤 登録リクエスト内容:", json.dumps(obj, indent=2))

    try:
        req = urllib.request.Request("http://localhost:18080/kabusapi/register", data=json_data, method="PUT")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-API-KEY", token)
        with urllib.request.urlopen(req) as res:
            if verbose:
                print("✅ 登録成功:", res.status)
    except urllib.error.HTTPError as e:
        if verbose:
            print(f"❌ HTTPエラー: {e.code} - {e.reason}")
            print("📄 エラーメッセージ:", e.read().decode())
        else:
            _ = e.read()  # ← エラー出力を抑えるために読み捨て
    except Exception as e:
        if verbose:
            print(f"❌ その他のエラー: {e}")



def unregister_all(token):
    try:
        req = urllib.request.Request("http://localhost:18080/kabusapi/unregister/all", method="PUT")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-API-KEY", token)
        urllib.request.urlopen(req)
    except Exception as e:
        print(f"❌ 全解除エラー: {e}")

def register_loop(token):
    offset = 0
    prev_symbols = []

    while True:
        df = load_symbols_from_excel()
        symbols = df['code'].tolist() if not df.empty else []

        if not symbols:
            print("⚠️ 銘柄コードが空です。10秒待機します…")
            time.sleep(10)
            continue

        added = list(set(symbols) - set(prev_symbols))
        removed = list(set(prev_symbols) - set(symbols))
        if added:
            print(f"➕ 新規追加された銘柄: {added}")
        if removed:
            print(f"➖ 削除された銘柄: {removed}")
        prev_symbols = symbols.copy()

        batch = get_next_batch(symbols, offset, BATCH_SIZE)
        register_batch(token, batch)
        time.sleep(SLEEP_INTERVAL)
        unregister_all(token)
        offset = (offset + BATCH_SIZE) % len(symbols)
