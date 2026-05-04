# ===============================================================
# build_mtf_5min_summary.py
# ---------------------------------------------------------------
# ・Y:\y_stock_data_price の summaryYYYYMMDD.db
# ・テーブル stock_summary（PUSHサマリーの5分足）
# ・欠損 or 壊れたDBは読み飛ばす
# ===============================================================

import os
import glob
import sqlite3
import pandas as pd
import re


DB_DIR = r"Y:\y_stock_data_price"


# ===============================================================
# ★ DBが有効かチェック（この stock_summary 用）
# ===============================================================
def is_valid_summary_db(path):
    try:
        conn = sqlite3.connect(path)
        df = pd.read_sql("SELECT * FROM stock_summary LIMIT 5", conn)
        conn.close()

        # 必須カラム
        required = {
            "time_range", "symbol", "open_price", "high_price",
            "low_price", "close_price", "volume", "date"
        }

        if not required.issubset(df.columns):
            return False

        # date が壊れてないか
        if df["date"].isna().any():
            return False

        # time_range が "09:00 - 09:05" 形式か
        if df["time_range"].isna().any():
            return False

        if not df["time_range"].astype(str).str.contains(" - ").all():
            return False

        return True

    except Exception:
        return False


# ===============================================================
# ★ stock_summary → 5分足 DataFrame を作る
# ===============================================================
def load_5min_from_any(path):
    try:
        conn = sqlite3.connect(path)

        # テーブル一覧取得
        tables = pd.read_sql(
            "SELECT name FROM sqlite_master WHERE type='table'",
            conn
        )["name"].tolist()

        df = None

        # ① stock_summary（PUSH 5分足）優先
        if "stock_summary" in tables:
            df = pd.read_sql("SELECT * FROM stock_summary", conn)
            df["__type"] = "push"

        # ② stock_summary_5min（Yahoo 5分足）
        elif "stock_summary_5min" in tables:
            df = pd.read_sql("SELECT * FROM stock_summary_5min", conn)
            df["__type"] = "yahoo"

        conn.close()

        if df is None or df.empty:
            return None

        # ================================
        # PUSH型データ処理
        # ================================
        if df["__type"].iloc[0] == "push":

            # time_range → start_time
            tr = (
                df["time_range"]
                .astype(str)
                .str.replace("：", ":", regex=False)
                .str.split(" - ").str[0]
                .str.strip()
            )

            # time 正規化（9:0 → 09:00 / 0900 → 09:00）
            def fix_time(x):
                x = str(x).strip()
                if re.fullmatch(r"\d{4}", x):
                    return x[:2] + ":" + x[2:]
                m = re.fullmatch(r"(\d{1,2}):(\d{1,2})", x)
                if m:
                    return m.group(1).zfill(2) + ":" + m.group(2).zfill(2)
                return None

            df["start_time"] = tr.apply(fix_time)

            # date 正規化
            df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")

            # datetime 作成
            df["datetime"] = pd.to_datetime(
                df["date"] + " " + df["start_time"],
                format="%Y-%m-%d %H:%M",
                errors="coerce",
            )

        # ================================
        # Yahoo型データ処理
        # ================================
        else:
            # date 正規化
            df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")

            # time が "09:00" or "900" の場合統一
            t = df["time"].astype(str).str.replace("：", ":", regex=False)

            def fix_time_yahoo(x):
                x = str(x).strip()
                # 0900 → 09:00
                if re.fullmatch(r"\d{4}", x):
                    return x[:2] + ":" + x[2:]
                # 9:0 → 09:00
                m = re.fullmatch(r"(\d{1,2}):(\d{1,2})", x)
                if m:
                    return m.group(1).zfill(2) + ":" + m.group(2).zfill(2)
                return None

            df["start_time"] = t.apply(fix_time_yahoo)

            df["datetime"] = pd.to_datetime(
                df["date"] + " " + df["start_time"],
                format="%Y-%m-%d %H:%M",
                errors="coerce",
            )

        # ===================================
        # 不良行除去 & 整形
        # ===================================
        df = df.dropna(subset=["datetime"])
        df["symbol"] = pd.to_numeric(df["symbol"], errors="coerce").astype("Int64")

        df = df.sort_values(["symbol", "datetime"]).reset_index(drop=True)

        return df

    except Exception as e:
        print(f"⚠ DB読込失敗: {path} → {e}")
        return None




# ===============================================================
# ★ 全ての有効DBを読み込み → 1つの5分足 DF にまとめる
# ===============================================================
def load_all_5min():
    files = sorted(glob.glob(os.path.join(DB_DIR, "summary*.db")))

    dfs = []
    for f in files:
        df = load_5min_from_any(f)

        if df is None:
            print(f"⚠ 読込不可DBスキップ: {f}")
            continue

        if df.empty:
            print(f"⚠ 空5分足DBスキップ: {f}")
            continue

        dfs.append(df)

    if not dfs:
        raise ValueError("❌ 5分足DBが1件も読み込めませんでした")

    return pd.concat(dfs, ignore_index=True)


# ===============================================================
# ★ AI学習用データセット作成
# ===============================================================
def build_summary_5min_dataset():
    print("📘 PUSHサマリー5分足 読み込み中...")
    df = load_all_5min()

    # 特徴量に使うカラム（あなたの stock_summary 構造に完全対応）
    FEATURES = [
        "open_price", "high_price", "low_price", "close_price",
        "volume", "vwap",
        "ma5", "ma25", "ma75",
        "macd", "signal", "rsi", "rci",
        "slowk", "slowd",
        "bb_upper", "bb_lower"
    ]

    # future close（次の5分）
    df = df.sort_values(["symbol", "datetime"])
    df["future_close"] = df.groupby("symbol")["close_price"].shift(-1)
    df = df.dropna(subset=["future_close"])

    df["y"] = (df["future_close"] > df["close_price"]).astype(int)

    X = df[FEATURES].fillna(0)
    y = df["y"]

    return X, y, FEATURES
# ===============================================================
# build_mtf_5min_summary.py
# ---------------------------------------------------------------
# ・Y:\y_stock_data_price の summaryYYYYMMDD.db
# ・テーブル stock_summary（PUSHサマリーの5分足）
# ・欠損 or 壊れたDBは読み飛ばす
# ===============================================================



DB_DIR = r"Y:\y_stock_data_price"


# ===============================================================
# ★ DBが有効かチェック（この stock_summary 用）
# ===============================================================
def is_valid_summary_db(path):
    try:
        conn = sqlite3.connect(path)
        df = pd.read_sql("SELECT * FROM stock_summary LIMIT 3", conn)
        conn.close()
    except Exception:
        return False

    # 必須カラム（あなたのPRAGMA通りの最低限）
    required = {"time_range", "symbol", "open_price", "close_price", "date"}

    if not required.issubset(df.columns):
        return False

    # date が最低限入っている
    if df["date"].isna().all():
        return False

    # time_range が最低限入っている（" - " が無い行があっても許容）
    if df["time_range"].isna().all():
        return False

    return True


# ===============================================================
# ★ stock_summary → 5分足 DataFrame を作る
# ===============================================================
def load_5min_summary(path):
    try:
        conn = sqlite3.connect(path)
        df = pd.read_sql("SELECT * FROM stock_summary", conn)
        conn.close()

        if df.empty:
            return None

        # =====================================================
        # ① time_range → start_time（前処理込み）
        # =====================================================
        tr = (
            df["time_range"]
            .astype(str)
            .str.replace("：", ":", regex=False)        # 全角コロン修正
            .str.replace("〜", "-", regex=False)       # 変な波ダッシュ
            .str.replace("~", "-", regex=False)
            .str.strip()
        )

        # "09:00 - 09:05" or "09:00-09:05" のどちらも対応
        tr = tr.str.replace(" - ", "-", regex=False).str.replace(" – ", "-", regex=False)
        tr = tr.str.split("-").str[0].str.strip()

        # 時刻が "9:0" や "0900" の場合も補正
        def fix_time(x):
            x = str(x).strip()
            x = x.replace("：", ":")
            # "0900" → "09:00"
            if re.fullmatch(r"\d{4}", x):
                return x[:2] + ":" + x[2:]
            # "9:0" → "09:00"
            m = re.fullmatch(r"(\d{1,2}):(\d{1,2})", x)
            if m:
                h = m.group(1).zfill(2)
                m2 = m.group(2).zfill(2)
                return f"{h}:{m2}"
            return None

        df["start_time"] = tr.apply(fix_time)

        # =====================================================
        # ② date を完全正規化（異常値全部対応）
        # =====================================================
        d = (
            df["date"]
            .astype(str)
            .str.strip()
            .str.replace("/", "-", regex=False)
            .str.replace("．", "-", regex=False)          # 句点
            .str.replace("。", "-", regex=False)
        )

        # "20251125" → "2025-11-25"
        def fix_date(x):
            x = x.strip()
            # 8桁数字
            if re.fullmatch(r"\d{8}", x):
                return f"{x[:4]}-{x[4:6]}-{x[6:8]}"
            # pandas に任せる（coerce）
            return x

        d = d.apply(fix_date)
        df["date"] = pd.to_datetime(d, errors="coerce").dt.strftime("%Y-%m-%d")

        # =====================================================
        # ③ datetime を format 指定で作る（これで Warning 完全ゼロ）
        # =====================================================
        # まず、生の date + time を作る
        combined = df["date"].astype(str) + " " + df["start_time"].astype(str)

        # ここでフォーマットが絶対に揃っている状態
        df["datetime"] = pd.to_datetime(
            combined,
            format="%Y-%m-%d %H:%M",
            errors="coerce"
        )

        # 変換できなかったもの除外
        df = df.dropna(subset=["datetime"])

        # =====================================================
        # ④ symbol の整形
        # =====================================================
        df["symbol"] = pd.to_numeric(df["symbol"], errors="coerce").astype("Int64")

        # =====================================================
        # ⑤ 並び替え
        # =====================================================
        df = df.sort_values(["symbol", "datetime"]).reset_index(drop=True)

        return df

    except Exception as e:
        print(f"⚠ DB読込失敗: {path} → {e}")
        return None


# ===============================================================
# ★ 全ての有効DBを読み込み → 1つの5分足 DF にまとめる
# ===============================================================
def load_all_5min():
    files = sorted(glob.glob(os.path.join(DB_DIR, "summary*.db")))

    valid = []
    for f in files:
        if is_valid_summary_db(f):
            valid.append(f)
        else:
            print(f"⚠ 無効DBスキップ: {f}")

    if not valid:
        raise ValueError("❌ 有効な5分足 summaryDB が1件もありません")

    dfs = []
    for f in valid:
        df = load_5min_summary(f)

        # -----------------------------------------------------
        # 🚨 メモリ爆発防止の3つのフィルタ
        # -----------------------------------------------------
        if df is None:
            continue

        # 行ゼロ → 無視
        if df.empty:
            print(f"⚠ 空5分足DFスキップ: {f}")
            continue

        # datetime が無い行は削除（全滅の場合はスキップ）
        df = df.dropna(subset=["datetime"])
        if df.empty:
            print(f"⚠ datetime不良のためスキップ: {f}")
            continue

        # 必要カラムチェック（最低限）
        required = ["symbol", "datetime", "open_price", "close_price"]
        if not set(required).issubset(df.columns):
            print(f"⚠ カラム不足のためスキップ: {f}")
            continue

        dfs.append(df)

    # 最終チェック
    if not dfs:
        raise ValueError("❌ すべてのDBが破損していたため、データ無し")

    # -----------------------------------------------------
    # concat 時の FutureWarning & メモリ爆発問題を回避
    # -----------------------------------------------------
    dfs = [d for d in dfs if not d.empty]

    return pd.concat(dfs, ignore_index=True)



# ===============================================================
# ★ AI学習用データセット作成
# ===============================================================
def build_summary_5min_dataset():
    print("📘 PUSHサマリー5分足 読み込み中...")
    df = load_all_5min()

    # 特徴量に使うカラム（あなたの stock_summary 構造に完全対応）
    FEATURES = [
        "open_price", "high_price", "low_price", "close_price",
        "volume", "vwap",
        "ma5", "ma25", "ma75",
        "macd", "signal", "rsi", "rci",
        "slowk", "slowd",
        "bb_upper", "bb_lower"
    ]

    # future close（次の5分）
    df = df.sort_values(["symbol", "datetime"])
    df["future_close"] = df.groupby("symbol")["close_price"].shift(-1)
    df = df.dropna(subset=["future_close"])

    df["y"] = (df["future_close"] > df["close_price"]).astype(int)

    X = df[FEATURES].fillna(0)
    y = df["y"]

    return X, y, FEATURES
