# ============================================================
# AI/train/final/build_final_train_csv.py
# FINAL_DECISION 学習用 CSV 生成
# ------------------------------------------------------------
# ✔ EXIT_LOG × SUMMARY 統合
# ✔ EXIT直前 N 秒前の状態を学習
# ✔ GO / HOLD / DELAY を教師化
# ✔ FINAL_DECISION_AI と完全互換
# ✔ paths.py 前提（Y:/ 直書き禁止）
# ============================================================

import sqlite3
import pandas as pd
from pathlib import Path
from datetime import timedelta

from config.paths import get_path

# ============================================================
# PATH（paths.py 経由）
# ============================================================

EXIT_DB: Path = get_path("runtime_exit") / "exit_log.db"
SUMMARY_DIR: Path = get_path("runtime_summary")

OUT_DIR: Path = get_path("ai_train_data") / "final"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_CSV: Path = OUT_DIR / "final_train.csv"

# ============================================================
# ラベル定義
# ============================================================

LABEL_DELAY = 0   # 様子見
LABEL_HOLD  = 1   # HOLD
LABEL_GO    = 2   # EXIT

# ============================================================
# 時間設定
# ============================================================

LOOKBACK_SECONDS = [5, 10, 20, 30]   # EXIT何秒前を切り出すか
STRONG_LOSS_PCT = -0.30
GOOD_PROFIT_PCT = 0.30

# ============================================================
# ExitLog 読み込み
# ============================================================

def load_exit_logs():
    with sqlite3.connect(EXIT_DB) as con:
        return pd.read_sql(
            """
            SELECT
                symbol,
                exit_time,
                pnl_pct,
                holding_seconds,
                exit_reason
            FROM exit_log
            """,
            con,
            parse_dates=["exit_time"],
        )

# ============================================================
# 教師ラベル生成
# ============================================================

def make_label(pnl_pct: float, remaining_sec: int, exit_reason: str) -> int:
    """
    FINAL_DECISION 教師
    """
    reason = exit_reason or ""

    # 強制EXIT（即GO）
    if pnl_pct <= STRONG_LOSS_PCT or "STOP" in reason:
        return LABEL_GO

    # まだ伸ばせた → HOLD
    if pnl_pct >= GOOD_PROFIT_PCT and remaining_sec >= 20:
        return LABEL_HOLD

    # それ以外は様子見
    return LABEL_DELAY

# ============================================================
# メイン
# ============================================================

def main():

    print("🚀 build_final_train_csv START")

    exit_df = load_exit_logs()
    if exit_df.empty:
        print("❌ ExitLog empty")
        return

    rows = []

    for _, r in exit_df.iterrows():
        symbol = r["symbol"]
        exit_dt = r["exit_time"]

        summary_db = SUMMARY_DIR / f"summary{exit_dt:%Y%m%d}.db"
        if not summary_db.exists():
            continue

        with sqlite3.connect(summary_db) as con:
            for sec in LOOKBACK_SECONDS:
                snap_dt = exit_dt - timedelta(seconds=sec)

                snap = pd.read_sql(
                    """
                    SELECT
                        close_price,
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
                    params=[symbol, snap_dt],
                )

                if snap.empty:
                    continue

                # --------------------------------------------
                # EXIT直前時点の状態
                # --------------------------------------------
                hold_sec = max(int(r["holding_seconds"] - sec), 0)
                remaining_sec = sec
                pnl_pct = r["pnl_pct"]

                label = make_label(
                    pnl_pct=pnl_pct,
                    remaining_sec=remaining_sec,
                    exit_reason=r["exit_reason"],
                )

                rows.append({
                    # ----------------------------------------
                    # FINAL_AI 特徴量（完全互換）
                    # ----------------------------------------
                    "profit_rate": pnl_pct,
                    "drawdown_rate": min(pnl_pct, 0),
                    "hold_seconds": hold_sec,

                    "volume_speed": snap.iloc[0]["volume_speed"],
                    "volatility": snap.iloc[0]["volatility"],
                    "trend_strength": snap.iloc[0]["trend_strength"],

                    # ----------------------------------------
                    # 教師用（未来）
                    # ----------------------------------------
                    "remaining_hold_seconds": remaining_sec,

                    # ----------------------------------------
                    # 教師ラベル
                    # ----------------------------------------
                    "label": label,
                })

    df = pd.DataFrame(rows)
    if df.empty:
        print("⚠ FINAL train CSV empty")
        return

    df = df.fillna(0)
    df.to_csv(OUT_CSV, index=False, encoding="utf-8-sig")

    print("=" * 70)
    print("✅ FINAL TRAIN CSV GENERATED")
    print(f" rows : {len(df)}")
    print(f" path : {OUT_CSV}")
    print("📊 LABEL DISTRIBUTION")
    print(df["label"].value_counts().sort_index())
    print("=" * 70)


# ============================================================
if __name__ == "__main__":
    main()
