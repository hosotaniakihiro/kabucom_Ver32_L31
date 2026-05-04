# =========================================
# optional/batch/build_daily_watchlist.py
# Ver2.1.0-FINAL-WATCHLIST-100-FIXED-SAFE
# =========================================

import sqlite3
from datetime import date, timedelta
import jpholiday
import pandas as pd
from pathlib import Path

from config.paths import get_path


# -----------------------------------------
# paths.py 経由のパス
# -----------------------------------------
DB_OPTIONAL: Path = get_path("optional_db")
DB_FLAGS: Path = get_path("symbol_flags_db")
YORIMAE_DIR: Path = get_path("raw_yorimae_ranking")

if not DB_OPTIONAL.exists():
    raise FileNotFoundError(f"optional DB not found: {DB_OPTIONAL}")


# -----------------------------------------
# 営業日
# -----------------------------------------
def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and not jpholiday.is_holiday(d)


def get_latest_trading_day(d: date) -> str:
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


# -----------------------------------------
# 寄り前気配 CSV 読み込み
# -----------------------------------------
def read_csv_safe(path: Path) -> pd.DataFrame:
    for enc in ("utf-8-sig", "utf-8", "cp932"):
        try:
            return pd.read_csv(path, encoding=enc)
        except UnicodeDecodeError:
            continue
    raise UnicodeDecodeError("encoding", b"", 0, 1, "unknown encoding")


def load_yorimae_csv(trade_date: str):
    up: dict[str, float] = {}
    down: dict[str, float] = {}

    date_str = trade_date.replace("-", "")

    files = {
        "up": YORIMAE_DIR / f"ランキング_寄前気配上昇率上位{date_str}.csv",
        "down": YORIMAE_DIR / f"ランキング_寄前気配下落率上位{date_str}.csv",
    }

    for key, path in files.items():
        if not path.exists():
            continue

        try:
            df = read_csv_safe(path)
        except Exception:
            continue

        col_symbol = None
        col_pct = None

        for c in df.columns:
            if "銘柄" in c:
                col_symbol = c
            if "%" in c or "率" in c:
                col_pct = c

        if not col_symbol or not col_pct:
            continue

        for _, r in df.iterrows():
            try:
                sym = str(r[col_symbol]).strip()
                if not sym or sym == "nan":
                    continue
                pct = float(str(r[col_pct]).replace("%", "").strip())
            except Exception:
                continue

            if key == "up":
                up[sym] = pct
            else:
                down[sym] = pct

    return up, down


# -----------------------------------------
# watchlist 作成（🔥100銘柄保証）
# -----------------------------------------
def build_daily_watchlist(trade_date: str):

    rows: dict[str, dict] = {}

    def get(sym: str) -> dict:
        if sym not in rows:
            rows[sym] = {
                "symbol": sym,
                "symbolname": None,
                "date": trade_date,
                "buy_score": 0,
                "sell_score": 0,
                "reason_buy": [],
                "reason_sell": [],
            }
        return rows[sym]

    # -----------------------------------------
    # optional DB からイベント取得
    # -----------------------------------------
    with sqlite3.connect(DB_OPTIONAL) as con:
        cur = con.cursor()

        symbol_names = {
            r[0]: r[1] for r in cur.execute("""
                SELECT DISTINCT symbol, symbolname
                FROM news_events
                WHERE symbolname IS NOT NULL
            """)
        }

        # ①〜⑧ 既存ロジック（完全保持）
        for (sym,) in cur.execute("""
            SELECT DISTINCT symbol FROM news_events
            WHERE category='ichioshi' AND date=?
        """, (trade_date,)):
            r = get(sym)
            r["buy_score"] += 3
            r["reason_buy"].append("イチオシ決算")

        for (sym,) in cur.execute("""
            SELECT DISTINCT symbol FROM news_events
            WHERE category='morning' AND date=?
        """, (trade_date,)):
            r = get(sym)
            r["buy_score"] += 2
            r["reason_buy"].append("朝ニュース")

        for sym, comment in cur.execute("""
            SELECT symbol, comment FROM news_events
            WHERE category='tomorrow' AND date=?
        """, (trade_date,)):
            r = get(sym)
            if comment and any(k in comment for k in ("悪材料", "下方", "減益")):
                r["sell_score"] += 2
                r["reason_sell"].append("悪材料")
            else:
                r["buy_score"] += 1
                r["reason_buy"].append("好材料")

        for (sym,) in cur.execute("""
            SELECT DISTINCT symbol FROM news_events
            WHERE category='surprise' AND date=?
        """, (trade_date,)):
            r = get(sym)
            r["buy_score"] += 2
            r["reason_buy"].append("サプライズ決算")

        for (sym,) in cur.execute("""
            SELECT DISTINCT symbol FROM news_events
            WHERE category='stop_high' AND date=?
        """, (trade_date,)):
            r = get(sym)
            r["buy_score"] += 3
            r["reason_buy"].append("前日ストップ高")
            r["sell_score"] += 2
            r["reason_sell"].append("前日S高の反動")

        for (sym,) in cur.execute("""
            SELECT DISTINCT symbol FROM news_events
            WHERE category='rise_rate' AND date=?
        """, (trade_date,)):
            r = get(sym)
            r["buy_score"] += 2
            r["reason_buy"].append("前日上昇率上位")
            r["sell_score"] += 1
            r["reason_sell"].append("前日急騰の反動")

        for sym, diff in cur.execute("""
            SELECT symbol, pts_diff FROM pts_rank WHERE date=?
        """, (trade_date,)):
            r = get(sym)
            if diff is None:
                continue
            if diff > 0:
                r["buy_score"] += 1
                r["reason_buy"].append("PTS上昇")
            elif diff < 0:
                r["sell_score"] += 1
                r["reason_sell"].append("PTS下落")

        up, down = load_yorimae_csv(trade_date)

        for sym, pct in up.items():
            r = get(sym)
            if pct >= 3:
                r["buy_score"] += 2
                r["reason_buy"].append("寄前+3%")
            elif pct >= 1:
                r["buy_score"] += 1
                r["reason_buy"].append("寄前上昇")

        for sym, pct in down.items():
            r = get(sym)
            if pct <= -3:
                r["sell_score"] += 2
                r["reason_sell"].append("寄前-3%")
            elif pct <= -1:
                r["sell_score"] += 1
                r["reason_sell"].append("寄前下落")

        for r in rows.values():
            r["symbolname"] = symbol_names.get(r["symbol"])

    # =========================================================
    # 🔥 100銘柄固定保証（symbol_flags.db 使用）
    # =========================================================
    MIN_WATCHLIST = 100
    existing = set(rows.keys())

    if len(rows) < MIN_WATCHLIST and DB_FLAGS.exists():
        with sqlite3.connect(DB_FLAGS) as con_flag:
            cur_flag = con_flag.cursor()

            for (sym,) in cur_flag.execute("SELECT symbol FROM symbol_flags"):
                sym = str(sym)
                if sym not in existing:
                    rows[sym] = {
                        "symbol": sym,
                        "symbolname": None,
                        "date": trade_date,
                        "buy_score": 0,
                        "sell_score": 0,
                        "reason_buy": ["FILL"],
                        "reason_sell": [],
                    }
                    existing.add(sym)

                if len(rows) >= MIN_WATCHLIST:
                    break

    final_rows = list(rows.values())[:MIN_WATCHLIST]
    print(f"🔥 daily_watchlist final size = {len(final_rows)}")

    return final_rows


# -----------------------------------------
# 保存
# -----------------------------------------
def save_watchlist(rows: list[dict]):

    with sqlite3.connect(DB_OPTIONAL) as con:
        cur = con.cursor()

        for r in rows:
            cur.execute("""
                INSERT OR REPLACE INTO daily_watchlist
                (symbol, symbolname, date,
                 buy_score, sell_score,
                 reason_buy, reason_sell)
                VALUES (?,?,?,?,?,?,?)
            """, (
                r["symbol"],
                r["symbolname"],
                r["date"],
                r["buy_score"],
                r["sell_score"],
                " / ".join(r["reason_buy"] or []),
                " / ".join(r["reason_sell"] or []),
            ))

        con.commit()

    print(f"✅ daily_watchlist saved: {len(rows)}")


# -----------------------------------------
# main
# -----------------------------------------
if __name__ == "__main__":
    trade_date = get_latest_trading_day(date.today())
    rows = build_daily_watchlist(trade_date)
    save_watchlist(rows)