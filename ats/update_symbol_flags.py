# ============================================================
# File   : update_symbol_flags.py
# ------------------------------------------------------------
# ✔ daily_watchlist → symbol_flags 反映
# ✔ DELETE禁止（フラグON/OFFのみ）
# ✔ LIGHT/ACTIVE が崩れない
# ============================================================

import sqlite3
import pandas as pd
from configparser import ConfigParser
from datetime import datetime
import logging
import os

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ============================================================
# 設定
# ============================================================
conf = ConfigParser()
conf.read("settings.ini", encoding="utf-8")

BASE_PATH = conf.get("paths", "basic_path", fallback="x:/Basic/")
FLAGS_DB = os.path.join(BASE_PATH, "symbol_flags.db")
WATCH_DB = os.path.join(BASE_PATH, "optional_data.db")  # daily_watchlist が入っているDB

# ============================================================
# メイン
# ============================================================
def main():
    logger.info("🚀 update_symbol_flags START")

    # --------------------------------------------------------
    # daily_watchlist 読み込み
    # --------------------------------------------------------
    with sqlite3.connect(WATCH_DB) as conn:
        df_watch = pd.read_sql("""
            SELECT symbol, buy_flag, sell_flag
            FROM daily_watchlist
        """, conn)

    if df_watch.empty:
        logger.warning("⚠ daily_watchlist empty → 全銘柄OFFのみ実行")
        df_watch = pd.DataFrame(columns=["symbol", "buy_flag", "sell_flag"])

    df_watch["symbol"] = df_watch["symbol"].astype(str)

    today_symbols = set(df_watch["symbol"].tolist())

    # --------------------------------------------------------
    # symbol_flags 更新
    # --------------------------------------------------------
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with sqlite3.connect(FLAGS_DB) as conn:
        cur = conn.cursor()

        # ① 全銘柄 OFF（DELETEしない！）
        cur.execute("""
            UPDATE symbol_flags
            SET buy_target = 0,
                sell_target = 0,
                updated_at = ?
        """, (now,))

        # ② 今日の監視銘柄だけ ON
        for _, r in df_watch.iterrows():
            cur.execute("""
                UPDATE symbol_flags
                SET buy_target = ?,
                    sell_target = ?,
                    updated_at = ?
                WHERE symbol = ?
            """, (
                int(r["buy_flag"]),
                int(r["sell_flag"]),
                now,
                r["symbol"],
            ))

        conn.commit()

    logger.info(
        f"✅ symbol_flags updated: "
        f"targets={len(today_symbols)}"
    )
    logger.info("🎉 update_symbol_flags END")


if __name__ == "__main__":
    main()
