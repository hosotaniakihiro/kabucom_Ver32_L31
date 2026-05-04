# trading/ranking/analyzer.py
# ────────────────────────────────────────────────
# ランキング分析モジュール（名称マッピング + rank比較修正 完全版）
# ────────────────────────────────────────────────

import sqlite3
import pandas as pd
import logging
import datetime as dt
import os

from utils.alerts_util import send_discord_notify

logger = logging.getLogger(__name__)

# === ランキングDBパス（日付付き） ===
BASE_PATH = r"Y:\stock_price_data"
today_str = dt.datetime.now().strftime("%Y%m%d")
RANKING_DB_PATH = os.path.join(BASE_PATH, f"ranking{today_str}.db")

# === 市場区分 ===
EXCHANGE_DIVISIONS = ["ALL", "TP", "TS", "TG"]

# === 種別名称のマッピング ===
TYPE_NAME_MAP = {
    "出来高急増": "売買高急増",
    "出来高": "売買高急増",
    "ボリューム急増": "売買高急増",
    "volume_spike": "売買高急増",
}


def normalize_type_name(type_name: str) -> str:
    """誤名称をDBの正式名称に正規化する"""
    return TYPE_NAME_MAP.get(type_name, type_name)


def analyze_ranking(symbol: str, type_name: str, ex_key: str,
                    db_path: str = RANKING_DB_PATH, top_n: int = 20) -> dict:
    """個別の市場・ランキング種別ごとに分析"""

    # ★ 正規名称に変換
    type_name = normalize_type_name(type_name)

    table_name = f"{type_name}_{ex_key}"
    conn = sqlite3.connect(db_path)

    try:
        df = pd.read_sql(
            f"""
            SELECT rowid AS id, *, inserted_at
            FROM "{table_name}"
            WHERE symbol=?
            ORDER BY id DESC
            LIMIT 10
            """,
            conn,
            params=(symbol,)
        )
    except Exception as e:
        logger.error(f"❌ DB読み込み失敗: {table_name} {e}")
        return {"status": "ERROR", "market": ex_key, "symbol": symbol}
    finally:
        conn.close()

    if df.empty or len(df) < 2:
        return {"status": "NO_DATA", "market": ex_key, "symbol": symbol}

    df = df.reset_index(drop=True)
    df["rank"] = df.index + 1

    # 最新と1つ前
    latest = df.iloc[0]
    prev = df.iloc[1]

    # ここが今回の最重要ポイント！ dot access を禁止して [] にする
    latest_rank = int(latest["rank"])
    prev_rank = int(prev["rank"])

    result = {
        "status": "OK",
        "market": ex_key,
        "type": type_name,
        "symbol": symbol,
        "trend": "SAME",
        "consecutive_up": False,
        "first_time_topN": False,
        "rank_latest": latest_rank,   # ★ 他の処理で使えるよう追加
        "rank_prev": prev_rank,       # ★ 他の処理で使えるよう追加
    }

    # --- 順位変動 ---
    if latest_rank < prev_rank:
        result["trend"] = "UP"
    elif latest_rank > prev_rank:
        result["trend"] = "DOWN"

    # --- 連続上昇 ---
    ranks = df["rank"].astype(int).tolist()
    result["consecutive_up"] = all(
        ranks[i] < ranks[i - 1] for i in range(1, len(ranks))
    )

    # --- TOP N 初登場 ---
    result["first_time_topN"] = (
        latest_rank <= top_n and all(r > top_n for r in ranks[1:])
    )

    return result


def analyze_all_markets(symbol: str, type_name: str,
                        db_path: str = RANKING_DB_PATH, top_n: int = 20, notify: bool = True) -> list:
    """全市場 (ALL, TP, TS, TG) をまとめて判定"""

    # 正規名称に変換
    type_name = normalize_type_name(type_name)

    results = []

    for ex_key in EXCHANGE_DIVISIONS:
        res = analyze_ranking(symbol, type_name, ex_key, db_path=db_path, top_n=top_n)
        results.append(res)

        if notify and res.get("status") == "OK":
            msg = (
                f"📊 **ランキング分析** {type_name}_{ex_key}\n"
                f"銘柄: {res['symbol']} | 市場: {res['market']}\n"
                f"順位変動: {res['trend']}\n"
                f"連続上昇: {'✅' if res['consecutive_up'] else '❌'}\n"
                f"TOP{top_n} 初登場: {'🎉' if res['first_time_topN'] else '—'}"
            )
            send_discord_notify(msg)

    return results
