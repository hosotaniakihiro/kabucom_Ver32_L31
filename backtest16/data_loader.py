# ============================================================
# backtest16/data_loader.py
# ------------------------------------------------------------
# 既存SQLite DBの読み取り専用ローダ（§3/§5/§7）。
#
#  安全:
#   - すべて mode=ro。過去日(ライブ書込み無し)は immutable=1 で高速化(実測0.0-0.2s)。
#   - 既存DBへ書き込まない / スキーマを仮定しすぎず列を自動判定する。
#   - 当日(基準日)以降は config.EXCLUDE_DATES_FROM により既定で読まない。
#
#  種別の分離(§3 A〜F):
#   - push.stream_data … PUSHスナップショット(現値+板10本値+previousclose+opening_price)。
#     取引所の全約定ティックではない。約定モデルの入力(EXECUTION_A/B)。
#   - summary.stock_summary_{1,3,5}min … 確定足OHLCV+指標(vwap/ma5...)。構造判定/EXECUTION_C。
#   - ranking … 当時のランキング(ユニバース/ギャップ分類の補助)。
# ============================================================

from __future__ import annotations

import datetime as dt
import os
import sqlite3
from typing import Iterable, List, Optional, Sequence

import pandas as pd

from backtest16 import config as C


# ------------------------------------------------------------
# パス
# ------------------------------------------------------------
def push_db_path(d: dt.date) -> str:
    return os.path.join(C.PUSH_DIR, f"push{d:%Y%m%d}.db")


def summary_db_path(d: dt.date) -> str:
    return os.path.join(C.SUMMARY_DIR, f"summary{d:%Y%m%d}.db")


def ranking_db_path(d: dt.date) -> str:
    return os.path.join(C.RANKING_DIR, f"ranking{d:%Y%m%d}.db")


# ------------------------------------------------------------
# 読み取り専用接続
# ------------------------------------------------------------
def open_ro(path: str, *, immutable: bool = True, timeout: float = 15.0) -> sqlite3.Connection:
    """読み取り専用接続。immutable は「書込みの無い過去日ファイル」専用。
    存在しなければ FileNotFoundError。"""
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    uri = (C.SQLITE_RO_IMMUTABLE if immutable else C.SQLITE_RO).format(path=path)
    conn = sqlite3.connect(uri, uri=True, timeout=timeout)
    conn.execute("PRAGMA busy_timeout=3000;")
    conn.execute("PRAGMA query_only=1;")   # 二重の安全弁: このコネクションでは書けない
    return conn


def _is_past_day(d: dt.date) -> bool:
    return d < C.EXCLUDE_DATES_FROM


def _guard_date(d: dt.date) -> None:
    if not _is_past_day(d):
        raise ValueError(f"{d} は基準日以降(ライブ書込み中/未確定)のため既定で読み取り禁止")


def table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
    except Exception:
        return []


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
    ).fetchone()
    return row is not None


def _pick(cols: Sequence[str], *candidates: str) -> Optional[str]:
    """候補のうち最初に存在する列名を返す(大文字小文字を区別して厳密一致)。"""
    s = set(cols)
    for c in candidates:
        if c in s:
            return c
    # 小文字一致のフォールバック
    low = {c.lower(): c for c in cols}
    for c in candidates:
        if c.lower() in low:
            return low[c.lower()]
    return None


# ------------------------------------------------------------
# datetime 正規化
# ------------------------------------------------------------
def _to_datetime(series: pd.Series) -> pd.Series:
    """tz付きISO('YYYY-MM-DDTHH:MM:SS+09:00')やスペース区切りを高速・一貫パース。
    先頭19文字(秒まで)を取り tz を落として naive local にする。固定フォーマットで
    ベクトル化し、合わないものは NaT（dateutil フォールバックはしない=高速維持）。"""
    s = series.astype(str).str.slice(0, 19).str.replace("T", " ", regex=False)
    return pd.to_datetime(s, format="%Y-%m-%d %H:%M:%S", errors="coerce")


# ------------------------------------------------------------
# push: PUSHスナップショット(現値+板)
# ------------------------------------------------------------
# 取得する板の深さ(1=最良気配のみ。約定モデルは最良気配+数量で十分)。
_PUSH_BASE_COLS = ["symbol", "datetime", "price", "volume", "vwap",
                   "previousclose", "opening_price", "high_price", "low_price"]
_PUSH_BOARD_COLS = ["Sell1Price", "Sell1Qty", "Buy1Price", "Buy1Qty",
                    "Sell2Price", "Sell2Qty", "Buy2Price", "Buy2Qty"]


def load_push_day(
    d: dt.date,
    symbols: Optional[Iterable[str]] = None,
    start_t: dt.time = C.MORNING_OPEN if hasattr(C, "MORNING_OPEN") else dt.time(9, 0),
    end_t: dt.time = dt.time(11, 30),
    with_board: bool = True,
    table: str = "stream_data",
) -> pd.DataFrame:
    """指定日の PUSH スナップショットを [start_t, end_t] で返す。
    列: symbol, ts(datetime), price, volume, vwap, prev_close, opening_price,
        high_price, low_price, (板: ask1/ask1_qty/bid1/bid1_qty...)
    価格>0 のみ。"""
    _guard_date(d)
    path = push_db_path(d)
    conn = open_ro(path, immutable=True)
    try:
        cols = table_columns(conn, table)
        if not cols:
            return pd.DataFrame()
        sym_c = _pick(cols, "symbol", "Symbol", "code")
        dt_c = _pick(cols, "datetime", "current_price_time", "time")
        price_c = _pick(cols, "price", "current_price", "CurrentPrice")
        vol_c = _pick(cols, "volume", "trading_volume")
        vwap_c = _pick(cols, "vwap", "VWAP")
        prev_c = _pick(cols, "previousclose", "PreviousClose", "prev_close")
        open_c = _pick(cols, "opening_price", "OpeningPrice")
        high_c = _pick(cols, "high_price", "HighPrice")
        low_c = _pick(cols, "low_price", "LowPrice")

        want = [c for c in [sym_c, dt_c, price_c, vol_c, vwap_c, prev_c, open_c, high_c, low_c] if c]
        board_map = {}
        if with_board:
            for logical, cands in [
                ("ask1", ("Sell1Price",)), ("ask1_qty", ("Sell1Qty",)),
                ("bid1", ("Buy1Price",)), ("bid1_qty", ("Buy1Qty",)),
                ("ask2", ("Sell2Price",)), ("ask2_qty", ("Sell2Qty",)),
                ("bid2", ("Buy2Price",)), ("bid2_qty", ("Buy2Qty",)),
            ]:
                real = _pick(cols, *cands)
                if real:
                    board_map[logical] = real
                    want.append(real)

        sel = ", ".join(f'"{c}"' for c in want)
        where = []
        params: list = []
        if price_c:
            where.append(f'"{price_c}" > 0')
        # symbol フィルタ(UNIQUE(symbol,datetime) インデックスを使い全表走査を避ける=高速化の要)
        if symbols is not None:
            syms = [str(s) for s in symbols]
            if not syms:
                return pd.DataFrame()
            where.append(f'{sym_c} IN ({",".join("?" * len(syms))})')
            params.extend(syms)
        # 時刻フィルタ(time 列 'HH:MM:SS' があれば SQL 側で前場に絞る)
        time_col = _pick(cols, "time")
        if time_col:
            where.append(f'"{time_col}" BETWEEN ? AND ?')
            params.extend([start_t.strftime("%H:%M:%S"), end_t.strftime("%H:%M:%S")])
        q = f'SELECT {sel} FROM "{table}"'
        if where:
            q += " WHERE " + " AND ".join(where)
        df = pd.read_sql(q, conn, params=params if params else None)
    finally:
        conn.close()

    if df.empty:
        return df

    ren = {}
    if sym_c: ren[sym_c] = "symbol"
    if dt_c: ren[dt_c] = "ts"
    if price_c: ren[price_c] = "price"
    if vol_c: ren[vol_c] = "volume"
    if vwap_c: ren[vwap_c] = "vwap"
    if prev_c: ren[prev_c] = "prev_close"
    if open_c: ren[open_c] = "opening_price"
    if high_c: ren[high_c] = "high_price"
    if low_c: ren[low_c] = "low_price"
    for logical, real in board_map.items():
        ren[real] = logical
    df = df.rename(columns=ren)

    df["symbol"] = df["symbol"].astype(str)
    df["ts"] = _to_datetime(df["ts"])
    df = df.dropna(subset=["ts"])
    # 時間帯フィルタ
    tt = df["ts"].dt.time
    df = df[(tt >= start_t) & (tt <= end_t)]
    if symbols is not None:
        want_syms = {str(s) for s in symbols}
        df = df[df["symbol"].isin(want_syms)]
    for c in ["price", "volume", "vwap", "prev_close", "opening_price", "high_price", "low_price",
              "ask1", "ask1_qty", "bid1", "bid1_qty", "ask2", "ask2_qty", "bid2", "bid2_qty"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values(["symbol", "ts"]).reset_index(drop=True)


# ------------------------------------------------------------
# summary: 確定足 OHLCV + 指標
# ------------------------------------------------------------
def load_summary_day(
    d: dt.date,
    interval: int = 1,
    symbols: Optional[Iterable[str]] = None,
) -> pd.DataFrame:
    """stock_summary_{interval}min を返す。
    列: symbol, ts, open, high, low, close, volume, vwap, ma5, ma25, ma75, (end_time)"""
    _guard_date(d)
    table = f"stock_summary_{interval}min"
    path = summary_db_path(d)
    conn = open_ro(path, immutable=True)
    try:
        if not table_exists(conn, table):
            return pd.DataFrame()
        cols = table_columns(conn, table)
        sym_c = _pick(cols, "symbol")
        dt_c = _pick(cols, "datetime", "end_time", "time")
        o_c = _pick(cols, "open_price", "open")
        h_c = _pick(cols, "high_price", "high")
        l_c = _pick(cols, "low_price", "low")
        cl_c = _pick(cols, "close_price", "close")
        v_c = _pick(cols, "volume")
        vwap_c = _pick(cols, "vwap")
        end_c = _pick(cols, "end_time")
        start_c = _pick(cols, "start_time")
        want = [c for c in [sym_c, dt_c, o_c, h_c, l_c, cl_c, v_c, vwap_c,
                            _pick(cols, "ma5"), _pick(cols, "ma25"), _pick(cols, "ma75")] if c]
        want = list(dict.fromkeys(want))  # 重複除去・順序維持
        sel = ", ".join(f'"{c}"' for c in want)
        q = f'SELECT {sel} FROM "{table}"'
        params: list = []
        if symbols is not None:
            syms = [str(s) for s in symbols]
            if not syms:
                return pd.DataFrame()
            q += f' WHERE {sym_c} IN ({",".join("?" * len(syms))})'
            params.extend(syms)
        df = pd.read_sql(q, conn, params=params if params else None)
    finally:
        conn.close()
    if df.empty:
        return df
    ren = {sym_c: "symbol", o_c: "open", h_c: "high", l_c: "low", cl_c: "close", v_c: "volume"}
    if dt_c: ren[dt_c] = "ts"
    if vwap_c: ren[vwap_c] = "vwap"
    df = df.rename(columns={k: v for k, v in ren.items() if k})
    df["symbol"] = df["symbol"].astype(str)
    # ts = 足のラベル時刻(=足の開始時刻)。確定時刻は ts + interval 分 で別途算出する(§7)。
    df["ts"] = _to_datetime(df["ts"])
    df["interval_min"] = interval
    df = df.dropna(subset=["ts"])
    if symbols is not None:
        want_syms = {str(s) for s in symbols}
        df = df[df["symbol"].isin(want_syms)]
    for c in ["open", "high", "low", "close", "volume", "vwap", "ma5", "ma25", "ma75"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.sort_values(["symbol", "ts"]).reset_index(drop=True)


# ------------------------------------------------------------
# ranking: 当時のランキング(ユニバース/ギャップ分類の補助)
# ------------------------------------------------------------
def load_ranking_day(d: dt.date, table: str = "ranking") -> pd.DataFrame:
    _guard_date(d)
    path = ranking_db_path(d)
    try:
        conn = open_ro(path, immutable=True)
    except FileNotFoundError:
        return pd.DataFrame()
    try:
        if not table_exists(conn, table):
            return pd.DataFrame()
        df = pd.read_sql(f'SELECT * FROM "{table}"', conn)
    finally:
        conn.close()
    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str)
    return df


# ------------------------------------------------------------
# 監視ユニバース(当時の購読銘柄) — 現在のリストを過去へ無条件適用しない(§3)
# ------------------------------------------------------------
def day_universe(d: dt.date) -> List[str]:
    """当日の対象銘柄ユニバース(当時の監視銘柄)。
    優先: 購読履歴 → ランキング掲載銘柄 → push の distinct(最終手段・重い)。"""
    subs = subscription_symbols(d)
    if subs:
        return sorted(set(subs))
    rk = load_ranking_day(d)
    if not rk.empty and "symbol" in rk.columns:
        return sorted(set(rk["symbol"].astype(str)))
    # 最終手段: ranking_snapshot_1min などからも拾えなければ空
    return []


def subscription_symbols(d: dt.date) -> Optional[List[str]]:
    """当日の購読銘柄履歴。取れなければ None(=その日の push 全銘柄を使う)。"""
    path = C.SUBSCRIPTION_HISTORY_DB
    if not os.path.exists(path):
        return None
    try:
        conn = open_ro(path, immutable=True)
    except Exception:
        return None
    try:
        tbls = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        if not tbls:
            return None
        t = tbls[0]
        cols = table_columns(conn, t)
        sym_c = _pick(cols, "symbol", "Symbol", "code")
        date_c = _pick(cols, "date", "datetime", "day", "snapshot_date")
        if not sym_c:
            return None
        if date_c:
            q = f'SELECT DISTINCT "{sym_c}" FROM "{t}" WHERE substr("{date_c}",1,10)=? OR "{date_c}"=?'
            ymd1 = d.strftime("%Y-%m-%d")
            ymd2 = d.strftime("%Y%m%d")
            rows = conn.execute(q, (ymd1, ymd2)).fetchall()
        else:
            rows = conn.execute(f'SELECT DISTINCT "{sym_c}" FROM "{t}"').fetchall()
        syms = [str(r[0]) for r in rows if r[0] is not None]
        return syms or None
    except Exception:
        return None
    finally:
        conn.close()
