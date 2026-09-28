# -*- coding: utf-8 -*-
# ============================================================
# ops/reports/backtest_extract.py   (2026-09-28)
# ------------------------------------------------------------
# NAS の日次 DB から、バックテストに必要な列だけをローカルの 1 ファイルへ集める。
#
# なぜ必要か:
#   これまでの検証は「実際に取った 72 建玉・4 営業日」の再生でしかなく、
#   9/19 には 9/18 の 16 件だけで出口の設計を決めて 9/24・9/25 を全敗にした。
#   全銘柄・全時刻で判定をやり直せる土台を作る。
#
# 元データ (NAS):
#   X:\raw_data\kabu_station\ranking\ranking<YYYYMMDD>.db
#     ranking_snapshot_1min: symbol datetime current_price change_percentage
#                            trading_value volume rank rank_type
#     -> 210 日 (2026/01/13〜)
#   X:\raw_data\kabu_station\summary\summary<YYYYMMDD>.db
#     stock_summary_1min / _3min / _5min:
#       symbol datetime close high low volume ma5 ma25 ma75 rsi macd signal atr
#     -> 139 日 (2026/03/31〜)
#   エントリー判定に使っている指標が全部履歴に残っているので、
#   MTF ゲート (3分/5分の RSI・MACD) まで再現できる。
#
# 速度 (9/18 で実測):
#   ranking の対象銘柄抽出 12.7s / 必要列 0.1s
#   summary 1分足 37.0s / 3分・5分足 13.1s
#   -> 1 日約 50 秒。139 日で約 115 分。
#   NAS 越しの全表スキャンが重いので、銘柄を売買代金で絞って行数を減らす。
#
# 絞り込み:
#   当日の最大 trading_value が --min-tv (既定 10 億円) 以上の銘柄だけ。
#   9/18 は 1,171 銘柄 -> 147 銘柄、summary 1分足 453,949 行 -> 46,092 行。
#   この下限は 2026-09-28 に入れた ENTRY_MIN_TRADING_VALUE_HARD と揃えている
#   (薄い銘柄は決済の滑りで値幅を失うため、そもそも触らない方針)。
#
# 出力:
#   %LOCALAPPDATA%\autostock\backtest\backtest.db
#     rank1m  (day, symbol, datetime, price, change_pct, trading_value, volume, rank, rank_type)
#     bar1m   (day, symbol, datetime, close, high, low, volume, ma5, ma25, rsi, macd, signal, atr)
#     bar3m   (day, symbol, datetime, close, rsi, macd, signal, atr, ma5, ma25)
#     bar5m   (day, symbol, datetime, close, rsi, macd, signal, atr, ma5, ma25)
#     done    (day, kind, rows, extracted_at)   <- 途中で止めても再開できる
#
# 使い方:
#   # 稼働中でも安全 (NAS を読むだけ。API もローカル DB も触らない)
#   C:\autostock\venv\Scripts\python.exe C:\autostock\src\ops\reports\backtest_extract.py
#   C:\autostock\venv\Scripts\python.exe ...\backtest_extract.py --days 20 --from 20260901
#   C:\autostock\venv\Scripts\python.exe ...\backtest_extract.py --status
#
# 当日 (今日) の DB は書き込み中なので既定で除外する (--include-today で含める)。
# ============================================================
from __future__ import annotations

import argparse
import datetime as dt
import glob
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

NAS_RANKING = Path(r"X:\raw_data\kabu_station\ranking")
NAS_SUMMARY = Path(r"X:\raw_data\kabu_station\summary")

DEFAULT_MIN_TV = 1_000_000_000  # 10 億円


def out_db() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or "."
    d = Path(base) / "autostock" / "backtest"
    d.mkdir(parents=True, exist_ok=True)
    return d / "backtest.db"


def connect_out() -> sqlite3.Connection:
    c = sqlite3.connect(str(out_db()))
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript(
        """
        create table if not exists rank1m(
          day text, symbol text, datetime text, price real, change_pct real,
          trading_value real, volume real, rank integer, rank_type text);
        create table if not exists bar1m(
          day text, symbol text, datetime text, close real, high real, low real,
          volume real, ma5 real, ma25 real, rsi real, macd real, signal real, atr real);
        create table if not exists bar3m(
          day text, symbol text, datetime text, close real, rsi real, macd real,
          signal real, atr real, ma5 real, ma25 real);
        create table if not exists bar5m(
          day text, symbol text, datetime text, close real, rsi real, macd real,
          signal real, atr real, ma5 real, ma25 real);
        create table if not exists done(
          day text, kind text, rows integer, extracted_at text,
          primary key(day, kind));
        create index if not exists ix_r_day  on rank1m(day, symbol, datetime);
        create index if not exists ix_b1_day on bar1m(day, symbol, datetime);
        create index if not exists ix_b3_day on bar3m(day, symbol, datetime);
        create index if not exists ix_b5_day on bar5m(day, symbol, datetime);
        """
    )
    return c


def available_days() -> list[str]:
    """ranking と summary の両方がある日 (summary が無いと指標が作れない)。"""
    rk = {re.search(r"(\d{8})", os.path.basename(p)).group(1)
          for p in glob.glob(str(NAS_RANKING / "ranking2026*.db"))}
    sm = {re.search(r"(\d{8})", os.path.basename(p)).group(1)
          for p in glob.glob(str(NAS_SUMMARY / "summary2026*.db"))}
    return sorted(rk & sm)


def _copy_local(src: Path) -> str:
    """NAS の DB を一時ファイルへコピーしてパスを返す。

    SMB 越しのランダムアクセスが遅いので、まるごとコピーしてから読む方が
    圧倒的に速い (426MB で コピー 7.2s + 読み 0.8s に対し NAS 直読み 28.6s)。
    -wal がある場合も一緒に持ってくる。
    """
    d = Path(tempfile.gettempdir()) / "autostock_bt"
    d.mkdir(parents=True, exist_ok=True)
    dst = d / src.name
    shutil.copyfile(str(src), str(dst))
    for suf in ("-wal", "-shm"):
        s2 = Path(str(src) + suf)
        if s2.exists():
            try:
                shutil.copyfile(str(s2), str(dst) + suf)
            except Exception:
                pass
    return str(dst)


def _rm(p) -> None:
    if not p:
        return
    for suf in ("", "-wal", "-shm"):
        try:
            os.remove(str(p) + suf)
        except Exception:
            pass

def already_done(c: sqlite3.Connection, day: str) -> bool:
    n = c.execute("select count(*) from done where day=?", (day,)).fetchone()[0]
    return n >= 4


def extract_day(c: sqlite3.Connection, day: str, min_tv: float) -> tuple[bool, str]:
    rk = NAS_RANKING / ("ranking%s.db" % day)
    sm = NAS_SUMMARY / ("summary%s.db" % day)
    if not rk.exists() or not sm.exists():
        return False, "DB が無い"

    t0 = time.time()
    # --- ranking ---
    # 2026-09-28: NAS を直接読むと遅い。一時ファイルへコピーしてから開く。
    #   実測 (summary 426MB): NAS 直読み全行 28.6s / symbol in (228個) 667s /
    #   コピー+読み 8.1s (コピー 7.2 + 読み 0.8)。読み自体は 36 倍速い。
    tmp_rk = None
    try:
        tmp_rk = _copy_local(rk)
        rc = sqlite3.connect("file:%s?mode=ro" % tmp_rk, uri=True)
        all_r = rc.execute(
            "select symbol, datetime, current_price, change_percentage, "
            "trading_value, volume, rank, rank_type from ranking_snapshot_1min").fetchall()
        rc.close()
    except Exception as e:
        _rm(tmp_rk)
        return False, "ranking 読み失敗 %r" % (e,)
    _rm(tmp_rk)

    # 当日最大 trading_value が下限以上の銘柄だけを Python 側で選ぶ
    max_tv: dict[str, float] = {}
    for r in all_r:
        try:
            tv = float(r[4] or 0.0)
        except Exception:
            tv = 0.0
        s = str(r[0])
        if tv > max_tv.get(s, 0.0):
            max_tv[s] = tv
    keep = {s for s, tv in max_tv.items() if tv >= min_tv}
    if not keep:
        return False, "対象銘柄 0"
    syms = sorted(keep)
    rrows = [r for r in all_r if str(r[0]) in keep]

    c.execute("delete from rank1m where day=?", (day,))
    c.executemany(
        "insert into rank1m(day,symbol,datetime,price,change_pct,trading_value,volume,rank,rank_type) "
        "values (?,?,?,?,?,?,?,?,?)",
        [(day,) + tuple(r) for r in rrows])
    c.execute("insert or replace into done values (?,?,?,?)",
              (day, "rank1m", len(rrows), dt.datetime.now().isoformat(timespec="seconds")))
    c.commit()

    # --- summary ---
    specs = [
        ("bar1m", "stock_summary_1min",
         "symbol, datetime, close, high, low, volume, ma5, ma25, rsi, macd, signal, atr",
         "day,symbol,datetime,close,high,low,volume,ma5,ma25,rsi,macd,signal,atr"),
        ("bar3m", "stock_summary_3min",
         "symbol, datetime, close, rsi, macd, signal, atr, ma5, ma25",
         "day,symbol,datetime,close,rsi,macd,signal,atr,ma5,ma25"),
        ("bar5m", "stock_summary_5min",
         "symbol, datetime, close, rsi, macd, signal, atr, ma5, ma25",
         "day,symbol,datetime,close,rsi,macd,signal,atr,ma5,ma25"),
    ]
    tmp_sm = None
    try:
        tmp_sm = _copy_local(sm)
        sc = sqlite3.connect("file:%s?mode=ro" % tmp_sm, uri=True)
        for table, src, cols, ins_cols in specs:
            rows = [r for r in sc.execute("select %s from %s" % (cols, src))
                    if str(r[0]) in keep]
            c.execute("delete from %s where day=?" % table, (day,))
            q = "insert into %s(%s) values (%s)" % (
                table, ins_cols, ",".join("?" * (len(ins_cols.split(",")))))
            c.executemany(q, [(day,) + tuple(r) for r in rows])
            c.execute("insert or replace into done values (?,?,?,?)",
                      (day, table, len(rows),
                       dt.datetime.now().isoformat(timespec="seconds")))
            c.commit()
        sc.close()
    except Exception as e:
        _rm(tmp_sm)
        # summary20260924.db は database disk image is malformed。
        # 壊れた日はスキップして続ける。
        return False, "summary 読み失敗 %r" % (e,)
    _rm(tmp_sm)

    return True, "%d 銘柄 / rank %d 行 / %.0f 秒" % (len(syms), len(rrows), time.time() - t0)


def show_status(c: sqlite3.Connection) -> None:
    rows = list(c.execute(
        "select day, sum(case when kind='rank1m' then rows else 0 end), "
        "sum(case when kind='bar1m' then rows else 0 end), "
        "sum(case when kind='bar3m' then rows else 0 end), "
        "sum(case when kind='bar5m' then rows else 0 end), count(*) "
        "from done group by day order by day"))
    print("取得済み %d 日" % len(rows))
    print("日付       rank1m    bar1m   bar3m   bar5m  完了")
    for d, r, b1, b3, b5, n in rows:
        print("%s %8d %8d %7d %7d  %d/4" % (d, r, b1, b3, b5, n))
    tot = c.execute("select count(*) from bar1m").fetchone()[0]
    print()
    print("bar1m 合計 %d 行  DB サイズ %.0f MB" % (tot, out_db().stat().st_size / 1e6))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-tv", type=float, default=DEFAULT_MIN_TV,
                    help="当日最大売買代金の下限 (既定 10 億円)")
    ap.add_argument("--days", type=int, default=0, help="新しい方から N 日だけ")
    ap.add_argument("--from", dest="date_from", default="", help="YYYYMMDD 以降")
    ap.add_argument("--include-today", action="store_true",
                    help="当日 (書き込み中) も含める")
    ap.add_argument("--status", action="store_true", help="取得状況だけ表示")
    a = ap.parse_args()

    c = connect_out()
    if a.status:
        show_status(c)
        return 0

    days = available_days()
    today = dt.date.today().strftime("%Y%m%d")
    if not a.include_today:
        days = [d for d in days if d != today]
    if a.date_from:
        days = [d for d in days if d >= a.date_from]
    if a.days > 0:
        days = days[-a.days:]

    todo = [d for d in days if not already_done(c, d)]
    print("対象 %d 日 (未取得 %d 日)  出力 %s" % (len(days), len(todo), out_db()))
    print("下限売買代金 %.0f 億円" % (a.min_tv / 1e8))
    print()

    ok = ng = 0
    t_all = time.time()
    for i, d in enumerate(todo, 1):
        good, msg = extract_day(c, d, a.min_tv)
        if good:
            ok += 1
            print("[%3d/%3d] %s OK  %s" % (i, len(todo), d, msg), flush=True)
        else:
            ng += 1
            print("[%3d/%3d] %s NG  %s" % (i, len(todo), d, msg), flush=True)
    print()
    print("完了 %d 日 / 失敗 %d 日  所要 %.1f 分" % (ok, ng, (time.time() - t_all) / 60))
    show_status(c)
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
