import pandas as pd
import json
from sqlalchemy import create_engine

# === DB 接続設定 ===
DB_PATH = "sqlite:///trading.db"  # ← あなたの環境に合わせて変更
engine = create_engine(DB_PATH)


def load_trades():
    """trades テーブルを DataFrame として読み込み"""
    df = pd.read_sql("SELECT * FROM trades", engine)
    # JSON文字列を辞書に変換
    if "reasons" in df.columns:
        df["reasons"] = df["reasons"].apply(lambda x: json.loads(x) if x else [])
    if "indicators" in df.columns:
        df["indicators"] = df["indicators"].apply(lambda x: json.loads(x) if x else {})
    return df


def analyze_winrate(df):
    """勝率・平均損益などを集計"""
    trades = df[df["side"] == "EXIT"].copy()
    if trades.empty:
        print("⚠️ EXITデータがありません")
        return

    trades["win"] = trades["pnl"] > 0
    win_rate = trades["win"].mean() * 100
    avg_pnl = trades["pnl"].mean()
    total_pnl = trades["pnl"].sum()

    print("\n=== 📊 トレード全体統計 ===")
    print(f"勝率: {win_rate:.2f}%")
    print(f"平均損益: {avg_pnl:.0f}円")
    print(f"合計損益: {total_pnl:.0f}円")
    print(f"トレード回数: {len(trades)}")


def analyze_by_reason(df):
    """シグナル（理由）ごとの勝率を集計"""
    trades = df[df["side"] == "EXIT"].copy()
    rows = []

    for _, row in trades.iterrows():
        for reason in row["reasons"]:
            rows.append({
                "reason": reason,
                "pnl": row["pnl"],
                "win": row["pnl"] > 0
            })

    if not rows:
        print("⚠️ シグナル別データがありません")
        return

    reason_df = pd.DataFrame(rows)
    result = reason_df.groupby("reason").agg(
        count=("pnl", "size"),
        win_rate=("win", "mean"),
        avg_pnl=("pnl", "mean"),
        total_pnl=("pnl", "sum"),
    ).reset_index()

    result["win_rate"] = (result["win_rate"] * 100).round(2)

    print("\n=== 🎯 シグナル別統計 ===")
    print(result.sort_values("win_rate", ascending=False))


def analyze_by_indicator(df):
    """テクニカル指標の条件別統計"""
    trades = df[df["side"] == "EXIT"].copy()
    rows = []

    for _, row in trades.iterrows():
        indicators = row.get("indicators", {})
        pnl = row["pnl"]
        win = pnl > 0

        if not indicators:
            continue

        # RSI が 30 以下
        if indicators.get("rsi") is not None and indicators["rsi"] <= 30:
            rows.append({"condition": "RSI<=30", "pnl": pnl, "win": win})

        # RSI が 70 以上
        if indicators.get("rsi") is not None and indicators["rsi"] >= 70:
            rows.append({"condition": "RSI>=70", "pnl": pnl, "win": win})

        # MACD
        if indicators.get("macd") is not None and indicators.get("signal") is not None:
            if indicators["macd"] > indicators["signal"]:
                rows.append({"condition": "MACD>Signal", "pnl": pnl, "win": win})
            else:
                rows.append({"condition": "MACD<Signal", "pnl": pnl, "win": win})

        # MA5 vs MA25
        if indicators.get("ma5") and indicators.get("ma25"):
            if indicators["ma5"] > indicators["ma25"]:
                rows.append({"condition": "MA5>MA25", "pnl": pnl, "win": win})
            else:
                rows.append({"condition": "MA5<=MA25", "pnl": pnl, "win": win})

    if not rows:
        print("⚠️ テクニカル指標データがありません")
        return

    ind_df = pd.DataFrame(rows)
    result = ind_df.groupby("condition").agg(
        count=("pnl", "size"),
        win_rate=("win", "mean"),
        avg_pnl=("pnl", "mean"),
        total_pnl=("pnl", "sum"),
    ).reset_index()

    result["win_rate"] = (result["win_rate"] * 100).round(2)

    print("\n=== 📈 テクニカル指標別統計 ===")
    print(result.sort_values("win_rate", ascending=False))


def analyze_by_symbol(df):
    """銘柄ごとの勝率・損益を集計"""
    trades = df[df["side"] == "EXIT"].copy()
    if trades.empty:
        print("⚠️ 銘柄別データがありません")
        return

    trades["win"] = trades["pnl"] > 0

    result = trades.groupby(["symbol", "symbolname"]).agg(
        count=("pnl", "size"),
        win_rate=("win", "mean"),
        avg_pnl=("pnl", "mean"),
        total_pnl=("pnl", "sum"),
    ).reset_index()

    result["win_rate"] = (result["win_rate"] * 100).round(2)

    print("\n=== 🏢 銘柄別統計 ===")
    print(result.sort_values("total_pnl", ascending=False))
import pandas as pd
import json
from sqlalchemy import create_engine

# === DB 接続設定 ===
DB_PATH = "sqlite:///trading.db"  # ← あなたの環境に合わせて変更
engine = create_engine(DB_PATH)


def load_trades():
    """trades テーブルを DataFrame として読み込み"""
    df = pd.read_sql("SELECT * FROM trades", engine)
    # JSON文字列を辞書に変換
    if "reasons" in df.columns:
        df["reasons"] = df["reasons"].apply(lambda x: json.loads(x) if x else [])
    if "indicators" in df.columns:
        df["indicators"] = df["indicators"].apply(lambda x: json.loads(x) if x else {})
    return df


def analyze_winrate(df):
    """勝率・平均損益などを集計"""
    trades = df[df["side"] == "EXIT"].copy()
    if trades.empty:
        print("⚠️ EXITデータがありません")
        return

    trades["win"] = trades["pnl"] > 0
    win_rate = trades["win"].mean() * 100
    avg_pnl = trades["pnl"].mean()
    total_pnl = trades["pnl"].sum()

    print("\n=== 📊 トレード全体統計 ===")
    print(f"勝率: {win_rate:.2f}%")
    print(f"平均損益: {avg_pnl:.0f}円")
    print(f"合計損益: {total_pnl:.0f}円")
    print(f"トレード回数: {len(trades)}")


def analyze_by_reason(df):
    """シグナル（理由）ごとの勝率を集計"""
    trades = df[df["side"] == "EXIT"].copy()
    rows = []

    for _, row in trades.iterrows():
        for reason in row["reasons"]:
            rows.append({
                "reason": reason,
                "pnl": row["pnl"],
                "win": row["pnl"] > 0
            })

    if not rows:
        print("⚠️ シグナル別データがありません")
        return

    reason_df = pd.DataFrame(rows)
    result = reason_df.groupby("reason").agg(
        count=("pnl", "size"),
        win_rate=("win", "mean"),
        avg_pnl=("pnl", "mean"),
        total_pnl=("pnl", "sum"),
    ).reset_index()

    result["win_rate"] = (result["win_rate"] * 100).round(2)

    print("\n=== 🎯 シグナル別統計 ===")
    print(result.sort_values("win_rate", ascending=False))


def analyze_by_indicator(df):
    """テクニカル指標の条件別統計"""
    trades = df[df["side"] == "EXIT"].copy()
    rows = []

    for _, row in trades.iterrows():
        indicators = row.get("indicators", {})
        pnl = row["pnl"]
        win = pnl > 0

        if not indicators:
            continue

        # RSI が 30 以下
        if indicators.get("rsi") is not None and indicators["rsi"] <= 30:
            rows.append({"condition": "RSI<=30", "pnl": pnl, "win": win})

        # RSI が 70 以上
        if indicators.get("rsi") is not None and indicators["rsi"] >= 70:
            rows.append({"condition": "RSI>=70", "pnl": pnl, "win": win})

        # MACD
        if indicators.get("macd") is not None and indicators.get("signal") is not None:
            if indicators["macd"] > indicators["signal"]:
                rows.append({"condition": "MACD>Signal", "pnl": pnl, "win": win})
            else:
                rows.append({"condition": "MACD<Signal", "pnl": pnl, "win": win})

        # MA5 vs MA25
        if indicators.get("ma5") and indicators.get("ma25"):
            if indicators["ma5"] > indicators["ma25"]:
                rows.append({"condition": "MA5>MA25", "pnl": pnl, "win": win})
            else:
                rows.append({"condition": "MA5<=MA25", "pnl": pnl, "win": win})

    if not rows:
        print("⚠️ テクニカル指標データがありません")
        return

    ind_df = pd.DataFrame(rows)
    result = ind_df.groupby("condition").agg(
        count=("pnl", "size"),
        win_rate=("win", "mean"),
        avg_pnl=("pnl", "mean"),
        total_pnl=("pnl", "sum"),
    ).reset_index()

    result["win_rate"] = (result["win_rate"] * 100).round(2)

    print("\n=== 📈 テクニカル指標別統計 ===")
    print(result.sort_values("win_rate", ascending=False))


def analyze_by_symbol(df):
    """銘柄ごとの勝率・損益を集計"""
    trades = df[df["side"] == "EXIT"].copy()
    if trades.empty:
        print("⚠️ 銘柄別データがありません")
        return

    trades["win"] = trades["pnl"] > 0

    result = trades.groupby(["symbol", "symbolname"]).agg(
        count=("pnl", "size"),
        win_rate=("win", "mean"),
        avg_pnl=("pnl", "mean"),
        total_pnl=("pnl", "sum"),
    ).reset_index()

    result["win_rate"] = (result["win_rate"] * 100).round(2)

    print("\n=== 🏢 銘柄別統計 ===")
    print(result.sort_values("total_pnl", ascending=False))


if __name__ == "__main__":
    df = load_trades()
    analyze_winrate(df)
    analyze_by_reason(df)
    analyze_by_indicator(df)
    analyze_by_symbol(df)


if __name__ == "__main__":
    df = load_trades()
    analyze_winrate(df)
    analyze_by_reason(df)
    analyze_by_indicator(df)
    analyze_by_symbol(df)
    analyze_by_date(df)   # ✅ 日付・曜日・月別統計
