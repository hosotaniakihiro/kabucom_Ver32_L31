# ============================================================
# build_sell_train_csv.py
# SELL AI 学習用 CSV 生成（market + sell_mode 対応）
# ------------------------------------------------------------
# ✔ EXIT_LOG × SUMMARY × PUSH 統合
# ✔ SELL 判断が正解だったかを教師化
# ✔ paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import sqlite3
from pathlib import Path
import pandas as pd
import datetime as dt

from config.paths import get_path

# ============================================================
# PATH（paths.py 経由）
# ============================================================

SUMMARY_DIR: Path = get_path("runtime_summary")
PUSH_DIR: Path    = get_path("runtime_push")
EXIT_DB: Path     = get_path("runtime_exit") / "exit_log.db"

OUTPUT_CSV: Path = get_path("ai_train_data") / "sell" / "sell_train.csv"

# ============================================================
# PARAMS
# ============================================================

LOOKAHEAD_SEC = 60
REVERSAL_PCT = 0.20   # EXIT後に −0.2% 以上逆行したら EXIT 正解

# ============================================================
# sell_mode 定義
# ============================================================

SELL_MODE_MAP = {
    "TAKE_PROFIT": 0,
    "STOP": 1,
    "TRAIL": 2,
}

# ============================================================
# ExitLog 読み込み
# ============================================================

def load_exit_logs() -> pd.DataFrame:
    if not EXIT_DB.exists():
        print(f"❌ Exit DB not found: {EXIT_DB}")
        return pd.DataFrame()

    with sqlite3.connect(EXIT_DB) as con:
        return pd.read_sql(
            """
            SELECT
                symbol,
                exit_time,
                exit_price,
                pnl_pct,
                holding_seconds,
                exit_reason
            FROM exit_log
            """,
            con,
            parse_dates=["exit_time"],
        )

# ============================================================
# EXIT直前の market 状態取得（1分足）
# ============================================================

def load_market_features(symbol: str, exit_dt: dt.datetime) -> dict:
    summary_db = SUMMARY_DIR / f"summary{exit_dt:%Y%m%d}.db"
    if not summary_db.exists():
        return {}

    with sqlite3.connect(summary_db) as con:
        df = pd.read_sql(
            """
            SELECT
                volume_speed,
                volatility,
                trend_strength
            FROM stock_summary_1min
            WHERE symbol=?
              AND datetime<=?
            ORDER BY datetime DESC
            LIMIT 1
            """,
            con,
            params=[symbol, exit_dt],
        )

    if df.empty:
        return {}

    r = df.iloc[0]
    return {
        "volume_speed": float(r.get("volume_speed", 0) or 0),
        "volatility": float(r.get("volatility", 0) or 0),
        "trend_strength": float(r.get("trend_strength", 0) or 0),
    }

# ============================================================
# EXIT後の価格（正解ラベル用）
# ============================================================

def get_price_after_exit(symbol: str, exit_dt: dt.datetime):
    push_db = PUSH_DIR / f"push{exit_dt:%Y%m%d}.db"
    if not push_db.exists():
        return None

    with sqlite3.connect(push_db) as con:
        df = pd.read_sql(
            """
            SELECT price
            FROM stream_data
            WHERE symbol=?
              AND datetime>=?
              AND datetime<=?
            ORDER BY datetime
            """,
            con,
            params=[
                symbol,
                exit_dt,
                exit_dt + dt.timedelta(seconds=LOOKAHEAD_SEC),
            ],
        )

    if df.empty:
        return None

    return float(df.iloc[-1]["price"])

# ============================================================
# exit_reason → sell_mode_code
# ============================================================

def parse_sell_mode(exit_reason: str) -> int:
    if not exit_reason:
        return -1

    r = exit_reason.upper()
    if "STOP" in r:
        return SELL_MODE_MAP["STOP"]
    if "TRAIL" in r:
        return SELL_MODE_MAP["TRAIL"]
    if "TAKE_PROFIT" in r or "TP" in r:
        return SELL_MODE_MAP["TAKE_PROFIT"]

    return -1

# ============================================================
# MAIN
# ============================================================

def main():

    df_exit = load_exit_logs()
    if df_exit.empty:
        print("⚠ ExitLog empty")
        return

    print(f"📌 ExitLog rows={len(df_exit)}")
    rows = []

    for _, r in df_exit.iterrows():

        symbol = r["symbol"]
        exit_dt = r["exit_time"]
        exit_price = r["exit_price"]

        # --------------------------------------------
        # market 特徴量（EXIT直前）
        # --------------------------------------------
        market = load_market_features(symbol, exit_dt)

        # --------------------------------------------
        # EXIT後の価格（教師）
        # --------------------------------------------
        price_after = get_price_after_exit(symbol, exit_dt)
        if price_after is None:
            continue

        move_pct = (price_after - exit_price) / exit_price * 100
        label = 1 if move_pct <= -REVERSAL_PCT else 0

        sell_mode_code = parse_sell_mode(r.get("exit_reason"))

        rows.append({
            # ===== SELL 特徴量 =====
            "profit_rate": r["pnl_pct"],
            "drawdown_rate": min(r["pnl_pct"], 0),
            "hold_seconds": r["holding_seconds"],

            "volume_speed": market.get("volume_speed", 0),
            "volatility": market.get("volatility", 0),
            "trend_strength": market.get("trend_strength", 0),

            "sell_mode_code": sell_mode_code,

            # ===== 教師 =====
            "label": label,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        print("⚠ sell_train.csv empty")
        return

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_CSV, index=False)

    print("=" * 60)
    print("✅ SELL TRAIN CSV GENERATED")
    print(f" rows : {len(df)}")
    print(f" path : {OUTPUT_CSV}")
    print("=" * 60)


if __name__ == "__main__":
    main()
