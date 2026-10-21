# ============================================================
# backtest16/day_builder.py
# ------------------------------------------------------------
# SymbolDay(銘柄×日)の組み立て。
#  手順(ネットワーク負荷を抑える):
#   1) 前営業日 summary_5min から prev_close(最終close)/prev_high(高値max)
#   2) 当日 summary 1/3/5min を universe 分 読み(インデックス経由で高速)
#   3) まずティック無しで SymbolDay を作る(構造判定は確定足で可能)
#   4) シグナルが出た銘柄だけ push ティックを後付け → A/B 執行に使う
# ============================================================

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional, Tuple

import pandas as pd

from backtest16 import data_loader as DL
from backtest16 import market_calendar as CAL
from backtest16 import config as C
from backtest16.features import SymbolDay


def prev_trading_date(d: dt.date) -> Optional[dt.date]:
    days = CAL.available_data_days(DL.C.SUMMARY_DIR, "summary",
                                   d - dt.timedelta(days=15), d - dt.timedelta(days=1),
                                   exclude_from=C.EXCLUDE_DATES_FROM)
    return days[-1] if days else None


def prev_refs(prev_date: Optional[dt.date], symbols: List[str]) -> Tuple[dict, dict]:
    """前営業日の summary_5min から {symbol: last_close}, {symbol: day_high}。"""
    if prev_date is None:
        return {}, {}
    s5 = DL.load_summary_day(prev_date, 5, symbols=symbols)
    if s5.empty:
        return {}, {}
    s5 = s5.sort_values("ts")
    close_map = s5.groupby("symbol")["close"].last().to_dict()
    high_map = s5.groupby("symbol")["high"].max().to_dict()
    close_map = {k: float(v) for k, v in close_map.items() if pd.notna(v) and v > 0}
    high_map = {k: float(v) for k, v in high_map.items() if pd.notna(v) and v > 0}
    return close_map, high_map


def build_days(date: dt.date, symbols: List[str],
               prev_close_map: dict, prev_high_map: dict) -> Dict[str, SymbolDay]:
    """当日の確定足を読み、ティック無しの SymbolDay を銘柄別に作る。"""
    b1 = DL.load_summary_day(date, 1, symbols=symbols)
    b3 = DL.load_summary_day(date, 3, symbols=symbols)
    b5 = DL.load_summary_day(date, 5, symbols=symbols)
    out: Dict[str, SymbolDay] = {}
    if b1.empty:
        return out
    g1 = {s: df for s, df in b1.groupby("symbol")}
    g3 = {s: df for s, df in b3.groupby("symbol")} if not b3.empty else {}
    g5 = {s: df for s, df in b5.groupby("symbol")} if not b5.empty else {}
    empty = pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume", "vwap"])
    for sym, bars1 in g1.items():
        out[sym] = SymbolDay(
            symbol=str(sym), date=date,
            bars1=bars1, bars3=g3.get(sym, empty.copy()), bars5=g5.get(sym, empty.copy()),
            ticks=pd.DataFrame(columns=["symbol", "ts", "price"]),
            prev_close=prev_close_map.get(sym), prev_high=prev_high_map.get(sym),
        )
    return out


def attach_ticks(days: Dict[str, SymbolDay], date: dt.date, symbols: List[str]) -> int:
    """指定銘柄に push ティックを後付けする。戻り値=ティックが取れた銘柄数。"""
    if not symbols:
        return 0
    p = DL.load_push_day(date, symbols=symbols, start_t=C.SESSION_ENTRY_START,
                         end_t=dt.time(11, 30), with_board=True)
    if p.empty:
        return 0
    n = 0
    for sym, df in p.groupby("symbol"):
        if sym in days:
            days[sym].ticks = df.sort_values("ts").reset_index(drop=True)
            n += 1
    return n
