"""楽天 MarketSpeed II の RSS から株価・板・歩み値を読み、ローカルの SQLite に記録する (2026-10-29)。

売買には使わない。kabu ステーションの PUSH を補ったり、PUSH と比べたりするための記録だけ。

仕組み:
  起動中の Excel に COM でつなぎ、専用のブックを 1 冊足す (窓は隠す)。
  1 銘柄 1 行で =RssMarket("7203.T","現在値") などの式を並べ、interval 秒ごとに表をまとめて読む。
  前回と値が変わった銘柄の行だけを書く。終わるときは足したブックだけ閉じる (Excel は閉じない)。
  歩み値は 2 枚目のシートに 1 銘柄 4 列で =RssTickList(,"7203.T",300) を並べ、
  前回読んだ分と重なるところを探して、増えた行だけを書く。

歩み値の制限 (10-29 の試験で確認):
  1 銘柄で最新 300 本まで。項目は 時刻 (秒まで)・出来高・約定値 だけで、売り買いの区分は無い。
  1 回読む間に 300 本を超えて約定すると取りこぼす。そのときは前回とつながらないので gap=1 で書く。
  同じ秒・同じ値段・同じ株数の約定が並ぶことがあるので、重なりは最大 20 行の並びで照らし合わせる。

前提:
  MarketSpeed II にログインし、Excel を開いて「MarketSpeed II RSS」タブで接続しておく。
  RSS の接続は Excel ごと。プログラムから別の Excel を立ち上げると、アドインは読めても値は空のまま
  (10-29 の試験で確認)。

単位:
  RSS の売買代金は千円単位 (10-29 のトヨタ 125,412,077 = 約 1,254 億円)。円に直して書く。

出力:
  %LOCALAPPDATA%\\autostock\\rakuten_rss\\rssYYYYMMDD.db
    quotes  株価と板 (値が変わったときだけ)
    ticks   歩み値。古い順に書く。gap: 0=前回とつながった 1=つながらない (取りこぼしの可能性) 2=その銘柄の初回
            (Excel につなぎ直したあとも gap=2 になり、直前と同じ約定が重ねて入ることがある)
    rss_collector_YYYYMMDD.log  動きの記録

毎朝の起動:
  タスクスケジューラの autostock_rakuten_rss が平日 8:50 に pythonw で起動する。
  Excel がまだ無い・接続前でも、15:35 まで 30 秒ごとにつなぎ直す。
  NAS ではなくローカルに書く (NAS の SQLite は close 漏れや SMB 断で壊れやすい)。

銘柄 (2026-10-29 ユーザー判断「候補＋PUSH と同じ銘柄」):
  --from-candidates --from-push で、自動売買の候補を必ず入れ、残りの枠 (合計 50) を
  kabu の PUSH に今登録している銘柄で埋める。どちらも 1 分ごとに読み直す。
  PUSH の登録一覧はファイルに残っていないので、PUSH DB の stream_data に直近 3 分でデータが
  来た銘柄を新しい順に使う (datetime の索引で 1〜3ms。約定の無い銘柄は漏れる)。

例:
  python ops/rakuten_rss/rss_collector.py --symbols 7203 9984 --once
  python ops/rakuten_rss/rss_collector.py --from-candidates --from-push --until 15:35
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import sys
import time

logger = logging.getLogger("rakuten_rss")

CANDIDATES_JSON = os.path.expandvars(r"%LOCALAPPDATA%\autostock\runtime\entry_candidates.json")
OUT_DIR = os.path.expandvars(r"%LOCALAPPDATA%\autostock\rakuten_rss")
PUSH_DIR = os.path.expandvars(r"%LOCALAPPDATA%\autostock\push")
PUSH_WINDOW_MIN = 3

# (列名, RssMarket の項目名)。時刻は文字列のまま、それ以外は数値にする。
BASE_ITEMS = [
    ("price", "現在値"),
    ("price_time", "現在値詳細時刻"),
    ("change_pct", "前日比率"),
    ("open", "始値"),
    ("high", "高値"),
    ("low", "安値"),
    ("volume", "出来高"),
    ("trading_value", "売買代金"),
    ("vwap", "出来高加重平均"),  # 2026-10-30: "VWAP" では空が返る (7203 で確認)
    ("over_qty", "OVER気配数量"),
    ("under_qty", "UNDER気配数量"),
    ("mkt_sell_qty", "売成行数量"),
    ("mkt_buy_qty", "買成行数量"),
]
TEXT_COLS = {"price_time"}
SCALE = {"trading_value": 1000.0}   # 千円 -> 円
MAX_SYMBOLS = 50
TICK_MAX = 300           # RssTickList の表示本数の上限
TICK_BLOCK = 4           # 1 銘柄あたりの列数 (時刻・出来高・約定値 + 空き 1 列)
TICK_MATCH = 20          # 前回分との重なりを照らし合わせる行数
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}:\d{2}$")


def board_items(depth: int) -> list[tuple[str, str]]:
    out = []
    for i in range(1, depth + 1):
        out += [(f"ask_px_{i}", f"最良売気配値{i}"), (f"ask_qty_{i}", f"最良売気配数量{i}"),
                (f"bid_px_{i}", f"最良買気配値{i}"), (f"bid_qty_{i}", f"最良買気配数量{i}")]
    return out


def to_num(v, scale: float = 1.0):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) * scale
    s = str(v).replace(",", "").strip()
    try:
        return float(s) * scale
    except ValueError:
        return None


def tick_rows(block) -> list[tuple[str, float, float]]:
    """RssTickList の 1 銘柄分 (新しい順) から、時刻が入った行だけを取り出す。"""
    out = []
    for r in block:
        t = r[0]
        if isinstance(t, float):          # Excel が時刻を日の割合で返したとき
            sec = round(t * 86400)
            t = f"{sec // 3600:02d}:{sec // 60 % 60:02d}:{sec % 60:02d}"
        if not isinstance(t, str) or not _TIME_RE.match(t.strip()):
            continue
        vol, px = to_num(r[1]), to_num(r[2])
        if vol is None or px is None:
            continue
        out.append((t.strip(), vol, px))
    return out


def new_ticks(prev: list, cur: list) -> tuple[list, bool]:
    """前回 prev と今回 cur (どちらも新しい順) から、増えた行 (新しい順) と、前回とつながったかを返す。"""
    if not prev:
        return cur, False
    need = min(TICK_MATCH, len(prev))
    for k in range(len(cur)):
        m = min(need, len(cur) - k)
        if m < min(5, need):
            break
        if cur[k:k + m] == prev[:m]:
            return cur[:k], True
    return cur, False


class RssSheet:
    """起動中で RSS 接続済みの Excel に専用ブックを足し、RSS の式を並べて読む。"""

    def __init__(self, items: list[tuple[str, str]], tick_n: int = 0):
        import win32com.client

        self.items = items
        self.tick_n = tick_n
        self.symbols: list[str] = []
        try:
            self.xl = win32com.client.GetActiveObject("Excel.Application")
        except Exception:
            raise RuntimeError("起動中の Excel が無い。Excel を開いて「MarketSpeed II RSS」タブで接続しておく")
        if not any(a.Name.lower().startswith("marketspeed2_rss") for a in self.xl.AddIns2 if a.IsOpen):
            raise RuntimeError("開いている Excel に RSS アドインが読み込まれていない")
        self._close_leftovers()
        self.wb = self.xl.Workbooks.Add()
        self.wb.Windows(1).Visible = False
        self.ws = self.wb.Worksheets(1)
        # 1 枚目の A1 で前回の残りを見分けるので、歩み値のシートは後ろに足す
        self.wst = self.wb.Worksheets.Add(After=self.ws) if tick_n else None

    def _close_leftovers(self) -> None:
        """強制終了で残った前回の専用ブックを閉じる (隠したまま・未保存・A1 が RssMarket の式)。"""
        for wb in list(self.xl.Workbooks):
            with contextlib.suppress(Exception):
                if (wb.Path == "" and not wb.Windows(1).Visible
                        and str(wb.Worksheets(1).Range("A1").Formula).startswith("=RssMarket(")):
                    logger.info("前回残ったブック %s を閉じる", wb.Name)
                    wb.Close(False)

    def set_symbols(self, symbols: list[str]) -> None:
        if symbols == self.symbols:
            return
        self.ws.Cells.ClearContents()
        if symbols:
            rows = [[f'=RssMarket("{s}.T","{name}")' for _, name in self.items] for s in symbols]
            n = len(self.items)
            self.ws.Range(self.ws.Cells(1, 1), self.ws.Cells(len(symbols), n)).Formula = rows
        if self.wst is not None:
            self.wst.Cells.ClearContents()
            for i, s in enumerate(symbols):
                self.wst.Cells(1, 1 + i * TICK_BLOCK).Formula2 = f'=RssTickList(,"{s}.T",{self.tick_n})'
        self.symbols = list(symbols)
        logger.info("symbols=%d %s", len(symbols), ",".join(symbols))

    def read(self) -> list[tuple]:
        if not self.symbols:
            return []
        v = self.ws.Range(self.ws.Cells(1, 1), self.ws.Cells(len(self.symbols), len(self.items))).Value
        return [tuple(r) for r in v]

    def read_ticks(self) -> dict[str, list]:
        """銘柄ごとの歩み値 (新しい順)。1 行目は式、2 行目は見出しなので 3 行目から読む。"""
        if self.wst is None or not self.symbols:
            return {}
        w = len(self.symbols) * TICK_BLOCK
        v = self.wst.Range(self.wst.Cells(3, 1), self.wst.Cells(2 + self.tick_n, w)).Value
        return {s: tick_rows([r[i * TICK_BLOCK:i * TICK_BLOCK + 3] for r in v]) for i, s in enumerate(self.symbols)}

    def close(self) -> None:
        # 足したブックだけ閉じる。Excel 本体はユーザーのものなので閉じない
        with contextlib.suppress(Exception):
            self.wb.Close(False)


def open_db(path: str, cols: list[str]) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path, timeout=5.0)
    defs = ", ".join(f"{c} {'TEXT' if c in TEXT_COLS else 'REAL'}" for c in cols)
    con.execute(f"CREATE TABLE IF NOT EXISTS quotes (ts TEXT NOT NULL, symbol TEXT NOT NULL, {defs})")
    con.execute("CREATE INDEX IF NOT EXISTS ix_quotes_symbol_ts ON quotes(symbol, ts)")
    con.execute("CREATE TABLE IF NOT EXISTS ticks (ts TEXT NOT NULL, symbol TEXT NOT NULL, "
                "tick_time TEXT NOT NULL, volume REAL, price REAL, gap INTEGER)")
    con.execute("CREATE INDEX IF NOT EXISTS ix_ticks_symbol_time ON ticks(symbol, tick_time)")
    have = {r[1] for r in con.execute("PRAGMA table_info(quotes)")}
    for c in cols:
        if c not in have:
            con.execute(f"ALTER TABLE quotes ADD COLUMN {c} {'TEXT' if c in TEXT_COLS else 'REAL'}")
    con.commit()
    return con


def push_symbols(now: dt.datetime | None = None) -> list[str]:
    """kabu の PUSH に今登録している銘柄 (直近 PUSH_WINDOW_MIN 分にデータが来た順)。読み取り専用で開く。"""
    now = now or dt.datetime.now()
    db = os.path.join(PUSH_DIR, f"push{now:%Y%m%d}.db")
    if not os.path.exists(db):
        return []
    since = (now - dt.timedelta(minutes=PUSH_WINDOW_MIN)).strftime("%Y-%m-%dT%H:%M:%S")
    until = (now + dt.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)) as con:
            rows = con.execute(
                "SELECT symbol FROM stream_data INDEXED BY idx_stream_data_datetime "
                "WHERE datetime >= ? AND datetime < ? GROUP BY symbol ORDER BY MAX(datetime) DESC",
                (since, until)).fetchall()
    except Exception:
        logger.warning("PUSH DB を読めない %s", db, exc_info=True)
        return []
    return [str(r[0]) for r in rows]


def load_symbols(args) -> list[str]:
    syms: list[str] = list(args.symbols or [])
    if args.symbols_file:
        with open(args.symbols_file, encoding="utf-8") as f:
            syms += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
    if args.from_candidates:
        try:
            with open(CANDIDATES_JSON, encoding="utf-8") as f:
                syms += [str(s) for s in json.load(f).get("symbols", [])]
        except Exception:
            logger.warning("entry_candidates.json を読めない", exc_info=True)
    if args.from_push:
        syms += push_symbols()
    out = list(dict.fromkeys(s.strip().upper() for s in syms if s.strip()))
    if len(out) > MAX_SYMBOLS:
        logger.warning("銘柄が %d 件あるので先頭 %d 件だけにする", len(out), MAX_SYMBOLS)
        out = out[:MAX_SYMBOLS]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--symbols", nargs="*", help="銘柄コード (例: 7203 9984 285A)")
    ap.add_argument("--symbols-file", help="1 行 1 銘柄のテキスト")
    ap.add_argument("--from-candidates", action="store_true",
                    help="自動売買の entry_candidates.json の銘柄を使う (1 分ごとに読み直す)")
    ap.add_argument("--from-push", action="store_true",
                    help="kabu の PUSH に今登録している銘柄を使う (候補の後ろに足す。1 分ごとに読み直す)")
    ap.add_argument("--depth", type=int, default=10, help="板の本数 (0〜10)")
    ap.add_argument("--ticks", type=int, default=TICK_MAX, help=f"歩み値を読む本数 (0 で読まない、上限 {TICK_MAX})")
    ap.add_argument("--interval", type=float, default=1.0, help="読む間隔 (秒)")
    ap.add_argument("--until", default="15:35", help="この時刻で終わる (HH:MM)")
    ap.add_argument("--db", help="出力先 (既定 %%LOCALAPPDATA%%\\autostock\\rakuten_rss\\rssYYYYMMDD.db)")
    ap.add_argument("--once", action="store_true", help="値が出るまで待って 1 回分を表示して終わる (DB に書かない)")
    args = ap.parse_args()
    handlers = [logging.StreamHandler()]
    if not args.once:
        # タスクスケジューラから pythonw で動かすと画面が無いので、ファイルにも残す
        os.makedirs(OUT_DIR, exist_ok=True)
        handlers.append(logging.FileHandler(os.path.join(OUT_DIR, f"rss_collector_{dt.date.today():%Y%m%d}.log"),
                                            encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [RAKUTEN RSS] %(message)s", handlers=handlers)

    items = BASE_ITEMS + board_items(max(0, min(10, args.depth)))
    cols = [c for c, _ in items]
    symbols = load_symbols(args)
    if not symbols and not (args.from_candidates or args.from_push):
        ap.error("--symbols / --symbols-file / --from-candidates / --from-push のどれかで銘柄を指定する")

    if args.once:
        sheet = RssSheet(items, tick_n=max(0, min(TICK_MAX, args.ticks)))
        try:
            sheet.set_symbols(symbols)
            return show_once(sheet, cols)
        finally:
            sheet.close()

    # 朝は Excel の接続より先に起動することがある。途中で Excel が閉じられることもある。
    # どちらも until まで 30 秒ごとにつなぎ直す
    hh, mm = (int(x) for x in args.until.split(":"))
    logger.info("起動 pid=%d until=%s", os.getpid(), args.until)
    while (dt.datetime.now().hour, dt.datetime.now().minute) < (hh, mm):
        try:
            sheet = RssSheet(items, tick_n=max(0, min(TICK_MAX, args.ticks)))
        except Exception as e:
            logger.warning("Excel につなげない (%s)。30 秒後にやり直す", e)
            time.sleep(30)
            continue
        try:
            sheet.set_symbols(load_symbols(args))
            collect(sheet, cols, args)
        except ExcelLost:
            logger.warning("Excel から読めない状態が続いた。つなぎ直す")
        finally:
            sheet.close()
    return 0


class ExcelLost(Exception):
    pass


def show_once(sheet: RssSheet, cols: list[str]) -> int:
    for _ in range(30):
        rows = sheet.read()
        if any(r[0] not in (None, "") for r in rows):
            break
        time.sleep(1)
    else:
        print("30 秒待っても値が空のまま。MarketSpeed II にログインして RSS 接続を有効にしているか確かめる。")
        return 1
    for s, r in zip(sheet.symbols, rows):
        print(f"--- {s}")
        for c, v in zip(cols, r):
            print(f"  {c:14s} {v if c in TEXT_COLS else to_num(v, SCALE.get(c, 1.0))!r}")
    for s, tk in sheet.read_ticks().items():
        print(f"--- {s} 歩み値 {len(tk)} 本 (新しい順に 5 本)")
        for t in tk[:5]:
            print(f"  {t[0]}  {t[1]:>12,.0f}  {t[2]:,.1f}")
    return 0


def collect(sheet: RssSheet, cols: list[str], args) -> int:
    hh, mm = (int(x) for x in args.until.split(":"))
    db = args.db or os.path.join(OUT_DIR, f"rss{dt.date.today():%Y%m%d}.db")
    placeholders = ", ".join("?" * (len(cols) + 2))
    sql = f"INSERT INTO quotes (ts, symbol, {', '.join(cols)}) VALUES ({placeholders})"
    tsql = "INSERT INTO ticks (ts, symbol, tick_time, volume, price, gap) VALUES (?, ?, ?, ?, ?, ?)"
    last: dict[str, tuple] = {}
    last_ticks: dict[str, list] = {}
    n_ticks = n_gaps = 0
    next_reload = time.monotonic() + 60
    n_rows = 0
    fails = 0
    last_value_at = warned_at = time.monotonic()
    logger.info("start db=%s interval=%.1fs until=%s", db, args.interval, args.until)
    with contextlib.closing(open_db(db, cols)) as con:
        while True:
            now = dt.datetime.now()
            if (now.hour, now.minute) >= (hh, mm):
                break
            t0 = time.monotonic()
            try:
                if (args.from_candidates or args.from_push) and t0 >= next_reload:
                    sheet.set_symbols(load_symbols(args))
                    next_reload = t0 + 60
                rows = sheet.read()
                ticks = sheet.read_ticks()
                fails = 0
            except Exception as e:
                # セルを編集中などで Excel が COM の呼び出しを断ることがある。少し待ってやり直す。
                # 30 秒ほど続いたら Excel が閉じられたとみて、main でつなぎ直す
                fails += 1
                logger.warning("Excel から読めない (%d 回目) %s", fails, e)
                if fails >= 15:
                    raise ExcelLost() from e
                time.sleep(2)
                continue
            if any(r[0] not in (None, "") for r in rows):
                last_value_at = t0
            elif t0 - last_value_at > 120 and t0 - warned_at > 300:
                warned_at = t0
                logger.warning("値が %d 秒空のまま。Excel の「MarketSpeed II RSS」タブで接続しているか確かめる",
                               t0 - last_value_at)
            ts = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            batch = []
            for s, r in zip(sheet.symbols, rows):
                vals = tuple(None if v in (None, "") else (str(v) if c in TEXT_COLS else to_num(v, SCALE.get(c, 1.0)))
                             for c, v in zip(cols, r))
                if vals[0] is None or last.get(s) == vals:
                    continue
                last[s] = vals
                batch.append((ts, s) + vals)
            tbatch = []
            for s, cur in ticks.items():
                if not cur:                       # 式を置き直した直後などは空。前回分は残しておく
                    continue
                add, linked = new_ticks(last_ticks.get(s, []), cur)
                gap = 0 if linked else (1 if s in last_ticks else 2)
                if gap == 1:
                    n_gaps += 1
                    logger.warning("歩み値が前回とつながらない symbol=%s 今回 %d 本 (取りこぼしの可能性)", s, len(cur))
                last_ticks[s] = cur
                tbatch += [(ts, s, t, v, px, gap) for t, v, px in reversed(add)]
            for s in set(last_ticks) - set(sheet.symbols):
                del last_ticks[s]
            if batch or tbatch:
                con.executemany(sql, batch)
                con.executemany(tsql, tbatch)
                con.commit()
                n_rows += len(batch)
                n_ticks += len(tbatch)
            if n_rows and n_rows % 5000 < len(batch):
                logger.info("rows=%d ticks=%d gaps=%d", n_rows, n_ticks, n_gaps)
            time.sleep(max(0.0, args.interval - (time.monotonic() - t0)))
    logger.info("end rows=%d ticks=%d gaps=%d", n_rows, n_ticks, n_gaps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
