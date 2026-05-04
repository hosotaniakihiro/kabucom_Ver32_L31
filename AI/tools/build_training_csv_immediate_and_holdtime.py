# ============================================================
# File   : AI/tools/build_training_csv_immediate_and_holdtime.py
# ------------------------------------------------------------
# ✔ 即益AI（①）+ HOLDTIME AI（③） 学習CSV生成
# ✔ ai_entry_events.db 起点（唯一の真実）
# ✔ summaryYYYYMMDD.db 日跨ぎ完全対応
# ✔ futureリーク完全防止
# ✔ 実運用データ構造完全準拠
# ============================================================

import sqlite3
import json
import pandas as pd
from pathlib import Path
from datetime import timedelta
import logging

from load_summary_1min import load_summary_1min

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
# 設定
# ============================================================

# ENTRYイベントDB（確定）
EVENT_DB = Path("AI/data/ai_entry_events.db")

# 出力先
OUT_DIR = Path("AI/train/data")
OUT_DIR.mkdir(parents=True, exist_ok=True)

OUT_IMMEDIATE = OUT_DIR / "train_immediate_profit.csv"
OUT_HOLDTIME  = OUT_DIR / "train_holdtime.csv"

# future 探索窓（共通）
LOOKAHEAD_MINUTES = 10


# ============================================================
# ENTRYイベント読み込み
# ============================================================

def load_entry_events() -> pd.DataFrame:
    if not EVENT_DB.exists():
        raise FileNotFoundError(f"ENTRY DB not found: {EVENT_DB}")

    conn = sqlite3.connect(EVENT_DB)
    df = pd.read_sql("SELECT * FROM entry_events", conn)
    conn.close()

    if df.empty:
        return df

    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime", "symbol", "side", "features_json"])

    return df.reset_index(drop=True)


# ============================================================
# メイン処理
# ============================================================

def main():
    df_events = load_entry_events()
    logger.info("Loaded entry events: %d", len(df_events))

    rows_immediate: list[dict] = []
    rows_holdtime: list[dict] = []

    for _, ev in df_events.iterrows():
        try:
            symbol = int(ev["symbol"])
            side = str(ev["side"]).upper()
            t0 = ev["datetime"]

            # features_json（学習と完全一致）
            features = json.loads(ev["features_json"])
            if not isinstance(features, dict) or not features:
                continue

            # ENTRY価格（features に必須）
            entry_price = features.get("entry_price")
            if entry_price is None:
                continue

            entry_price = float(entry_price)

            # future探索範囲
            t_end = t0 + timedelta(minutes=LOOKAHEAD_MINUTES)

            # ------------------------------------------------
            # summary 取得（日跨ぎ対応）
            # ------------------------------------------------
            df_future = load_summary_1min(
                symbol=symbol,
                start=t0,
                end=t_end,
            )

            if df_future is None or df_future.empty:
                continue

            # ------------------------------------------------
            # gain 計算（BUY / SELL 正方向）
            # ------------------------------------------------
            if side == "BUY":
                df_future["gain"] = (
                    (df_future["high"] - entry_price) / entry_price
                )
            elif side == "SELL":
                df_future["gain"] = (
                    (entry_price - df_future["low"]) / entry_price
                )
            else:
                continue

            if df_future["gain"].isna().all():
                continue

            # 最大 gain と時刻
            max_idx = df_future["gain"].idxmax()
            max_gain = float(df_future.loc[max_idx, "gain"])
            max_time = df_future.loc[max_idx, "datetime"]

            hold_seconds = int((max_time - t0).total_seconds())
            if hold_seconds < 0:
                continue

            # ------------------------------------------------
            # 共通 base
            # ------------------------------------------------
            base = {
                "symbol": symbol,
                "side": side,
            }

            # ------------------------------------------------
            # 即益AI（最大リターン）
            # ------------------------------------------------
            rows_immediate.append({
                **features,
                **base,
                "y_immediate_max_ret": max_gain,
            })

            # ------------------------------------------------
            # HOLDTIME AI（最大到達秒）
            # ------------------------------------------------
            rows_holdtime.append({
                **features,
                **base,
                "y_hold_seconds": hold_seconds,
            })

        except Exception as e:
            logger.warning("skip event due to error: %s", e)
            continue

    # ========================================================
    # CSV 出力
    # ========================================================

    if rows_immediate:
        df_i = pd.DataFrame(rows_immediate)
        df_i.to_csv(OUT_IMMEDIATE, index=False)
        logger.info(
            "Saved immediate profit CSV: %s (%d rows)",
            OUT_IMMEDIATE, len(df_i)
        )
    else:
        logger.warning("No immediate profit rows generated")

    if rows_holdtime:
        df_h = pd.DataFrame(rows_holdtime)
        df_h.to_csv(OUT_HOLDTIME, index=False)
        logger.info(
            "Saved holdtime CSV: %s (%d rows)",
            OUT_HOLDTIME, len(df_h)
        )
    else:
        logger.warning("No holdtime rows generated")


# ============================================================
# エントリーポイント
# ============================================================

if __name__ == "__main__":
    main()
