# ============================================================
# backtest16/engine/execution_engine.py
# ------------------------------------------------------------
# 約定シミュレータ(§8)。3方式を厳密に分離し、単一勝率に混ぜない。
#
#  EXECUTION_A (A_BOARD) : 板付きPUSHスナップショット。約定価格は気配(買=ask/売=bid)、
#                          数量は気配数量で部分/未約定を判定。板由来の近似であり
#                          取引所の実約定ではない(§6.4)。
#  EXECUTION_B (B_PUSH)  : PUSH現値イベント。シグナル後の最初の利用可能イベントで約定、
#                          スプレッド/スリッページはコストモデルで明示。
#  EXECUTION_C (C_OHLC)  : 確定1分足OHLC。シグナルは確定足で判定し、原則「次足始値」で
#                          エントリー。同一足でTP/SL両到達は保守的に損切り優先。
#
#  共通: 最大保有300秒到達時は利用可能な次イベントで決済。長時間の価格欠損・発注不能は
#        「正常決済」として利益計上しない(exit_reason=DATA_GAP/NOFILL)。
#  未来情報防止: エントリーは必ず signal_ts より後のイベント。同一イベントでの
#        シグナル成立＆理想約定は禁止。
# ============================================================

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd

from backtest16 import config as C
from backtest16 import cost_model, short_sale_rules as SR
from backtest16.features import SymbolDay


@dataclass
class Signal:
    strategy_id: str
    symbol: str
    date: dt.date
    side: str                 # "LONG" or "SHORT"
    signal_ts: pd.Timestamp
    ref_price: float          # シグナル根拠価格(ブレイク水準等)。記録用。
    meta: dict = None


@dataclass
class Trade:
    strategy_id: str
    symbol: str
    date: dt.date
    side: str
    exec_mode: str
    signal_ts: pd.Timestamp
    entry_ts: Optional[pd.Timestamp]
    entry_price: Optional[float]
    exit_ts: Optional[pd.Timestamp]
    exit_price: Optional[float]
    qty: int
    exit_reason: str          # TP/SL/TIME/EOD/NOFILL/DATA_GAP/TOO_EXPENSIVE
    gross_pnl: float
    net_pnl: float
    cost_total: float
    mae_pct: float            # 最大逆行(不利方向, %)
    mfe_pct: float            # 最大順行(有利方向, %)
    hold_seconds: float
    gap_pct: Optional[float]
    gap_bucket: str
    first5_bullish: Optional[bool]
    vwap_side: Optional[str]  # "ABOVE"/"BELOW"/None(entry時点)
    entry_hour: Optional[int]
    borrow_class: Optional[str]      # SHORTのみ
    short_reg_state: Optional[str]   # SHORTのみ
    filled: bool

    def as_row(self) -> dict:
        d = asdict(self)
        for k in ("signal_ts", "entry_ts", "exit_ts"):
            v = d.get(k)
            d[k] = v.isoformat() if isinstance(v, pd.Timestamp) else (str(v) if v is not None else None)
        d["date"] = str(d["date"])
        return d


# ------------------------------------------------------------
# TP/SL 水準
# ------------------------------------------------------------
def _levels(side: str, entry: float, tp_pct: float, sl_pct: float):
    if side == "LONG":
        return entry * (1 + tp_pct / 100.0), entry * (1 - sl_pct / 100.0)
    else:  # SHORT: 利確は下, 損切りは上
        return entry * (1 - tp_pct / 100.0), entry * (1 + sl_pct / 100.0)


def _excursion_pct(side: str, entry: float, hi: float, lo: float):
    """区間中の高安から MFE(順行)/MAE(逆行) を%で。"""
    if side == "LONG":
        mfe = (hi - entry) / entry * 100.0
        mae = (lo - entry) / entry * 100.0   # 負
    else:
        mfe = (entry - lo) / entry * 100.0
        mae = (entry - hi) / entry * 100.0
    return mae, mfe


def _vwap_side(sd: SymbolDay, ts: pd.Timestamp, price: float) -> Optional[str]:
    b = sd.confirmed_bars_asof(1, ts)
    if b is None or b.empty or "vwap" not in b.columns:
        return None
    vw = b.iloc[-1].get("vwap")
    if vw is None or pd.isna(vw) or vw <= 0:
        return None
    return "ABOVE" if price >= vw else "BELOW"


def _qty_for(entry_price: float, params: dict) -> int:
    budget = params.get("per_symbol_notional", C.PER_SYMBOL_NOTIONAL_YEN)
    unit = 100
    if entry_price <= 0:
        return 0
    raw = int(budget // entry_price)
    return (raw // unit) * unit


# ------------------------------------------------------------
# 本体
# ------------------------------------------------------------
def execute(sd: SymbolDay, sig: Signal, mode: str, params: dict) -> Optional[Trade]:
    tp_pct = params.get("take_profit_pct", C.TAKE_PROFIT_PCT)
    sl_pct = params.get("stop_loss_pct", C.STOP_LOSS_PCT)
    max_hold = params.get("max_hold_seconds", C.MAX_HOLD_SECONDS)

    if mode == "C_OHLC":
        return _execute_ohlc(sd, sig, tp_pct, sl_pct, max_hold, params)
    elif mode in ("B_PUSH", "A_BOARD"):
        return _execute_tick(sd, sig, mode, tp_pct, sl_pct, max_hold, params)
    raise ValueError(f"unknown mode {mode}")


def _base_trade(sd: SymbolDay, sig: Signal, mode: str, reason: str) -> Trade:
    from backtest16.features import gap_bucket
    return Trade(
        strategy_id=sig.strategy_id, symbol=sig.symbol, date=sig.date, side=sig.side,
        exec_mode=mode, signal_ts=sig.signal_ts, entry_ts=None, entry_price=None,
        exit_ts=None, exit_price=None, qty=0, exit_reason=reason, gross_pnl=0.0,
        net_pnl=0.0, cost_total=0.0, mae_pct=0.0, mfe_pct=0.0, hold_seconds=0.0,
        gap_pct=sd.gap_pct, gap_bucket=gap_bucket(sd.gap_pct),
        first5_bullish=sd.first5_bullish, vwap_side=None,
        entry_hour=None, borrow_class=None, short_reg_state=None, filled=False)


def _finalize(sd: SymbolDay, sig: Signal, mode: str, entry_ts, entry_price, exit_ts,
              exit_price, qty, reason, mae, mfe, params) -> Trade:
    from backtest16.features import gap_bucket
    credit_type = params.get("credit_type", "SEIDO")
    net, gross, costs = cost_model.net_pnl(
        sig.side, entry_price, exit_price, qty, hold_days=1, credit_type=credit_type)
    hold = (exit_ts - entry_ts).total_seconds() if (exit_ts is not None and entry_ts is not None) else 0.0
    return Trade(
        strategy_id=sig.strategy_id, symbol=sig.symbol, date=sig.date, side=sig.side,
        exec_mode=mode, signal_ts=sig.signal_ts, entry_ts=entry_ts, entry_price=entry_price,
        exit_ts=exit_ts, exit_price=exit_price, qty=qty, exit_reason=reason,
        gross_pnl=gross, net_pnl=net, cost_total=costs.total,
        mae_pct=mae, mfe_pct=mfe, hold_seconds=hold,
        gap_pct=sd.gap_pct, gap_bucket=gap_bucket(sd.gap_pct), first5_bullish=sd.first5_bullish,
        vwap_side=_vwap_side(sd, entry_ts, entry_price), entry_hour=entry_ts.hour if entry_ts else None,
        borrow_class=None, short_reg_state=None, filled=True)


# --- EXECUTION_C: 1分足OHLC ---
def _execute_ohlc(sd: SymbolDay, sig: Signal, tp_pct, sl_pct, max_hold, params) -> Optional[Trade]:
    b = sd.bars1
    if b is None or b.empty:
        return _base_trade(sd, sig, "C_OHLC", "DATA_GAP")
    b = b.sort_values("ts").reset_index(drop=True)
    # 次足始値エントリー: signal_ts 以降にラベルが始まる最初の足
    fut = b[b["ts"] >= sig.signal_ts]
    if fut.empty:
        return _base_trade(sd, sig, "C_OHLC", "DATA_GAP")
    entry_bar = fut.iloc[0]
    entry_ts = entry_bar["ts"]
    entry_price = float(entry_bar["open"])
    qty = _qty_for(entry_price, params)
    if qty < 100:
        t = _base_trade(sd, sig, "C_OHLC", "TOO_EXPENSIVE")
        return t
    tp, sl = _levels(sig.side, entry_price, tp_pct, sl_pct)
    deadline = entry_ts + pd.Timedelta(seconds=max_hold)
    hi_run, lo_run = entry_price, entry_price
    path = b[b["ts"] >= entry_ts]
    last_close, last_ts = entry_price, entry_ts
    for _, bar in path.iterrows():
        bar_end = bar["ts"] + pd.Timedelta(minutes=1)
        hi, lo = float(bar["high"]), float(bar["low"])
        hi_run, lo_run = max(hi_run, hi), min(lo_run, lo)
        last_close, last_ts = float(bar["close"]), bar_end
        hit_sl = (lo <= sl) if sig.side == "LONG" else (hi >= sl)
        hit_tp = (hi >= tp) if sig.side == "LONG" else (lo <= tp)
        # 同一足で両到達 → 保守的に損切り優先(§8)
        if hit_sl:
            mae, mfe = _excursion_pct(sig.side, entry_price, hi_run, lo_run)
            return _finalize(sd, sig, "C_OHLC", entry_ts, entry_price, bar_end, sl, qty, "SL", mae, mfe, params)
        if hit_tp:
            mae, mfe = _excursion_pct(sig.side, entry_price, hi_run, lo_run)
            return _finalize(sd, sig, "C_OHLC", entry_ts, entry_price, bar_end, tp, qty, "TP", mae, mfe, params)
        if bar_end >= deadline:
            mae, mfe = _excursion_pct(sig.side, entry_price, hi_run, lo_run)
            return _finalize(sd, sig, "C_OHLC", entry_ts, entry_price, bar_end, float(bar["close"]), qty, "TIME", mae, mfe, params)
    # 足が尽きた(前場引け等) → 最終足終値でEOD決済
    mae, mfe = _excursion_pct(sig.side, entry_price, hi_run, lo_run)
    return _finalize(sd, sig, "C_OHLC", entry_ts, entry_price, last_ts, last_close, qty, "EOD", mae, mfe, params)


# --- EXECUTION_B / A: PUSHスナップショット(現値 / 板) ---
def _execute_tick(sd: SymbolDay, sig: Signal, mode, tp_pct, sl_pct, max_hold, params) -> Optional[Trade]:
    tk = sd.ticks_after(sig.signal_ts)
    if tk is None or tk.empty:
        return _base_trade(sd, sig, mode, "NOFILL")
    first = tk.iloc[0]
    entry_ts = first["ts"]
    # 約定価格: Aは板(買=ask/売新規=bid), Bは現値
    if mode == "A_BOARD":
        if sig.side == "LONG":
            ep = SR.long_buy_fill_price_from_board(first)
        else:
            ep = SR.short_fill_price_from_board(first)
        if ep is None:
            ep = float(first["price"]) if pd.notna(first["price"]) else None
    else:
        ep = float(first["price"]) if pd.notna(first["price"]) else None
    if ep is None or ep <= 0:
        return _base_trade(sd, sig, mode, "NOFILL")
    entry_price = ep
    qty = _qty_for(entry_price, params)
    if qty < 100:
        return _base_trade(sd, sig, mode, "TOO_EXPENSIVE")
    tp, sl = _levels(sig.side, entry_price, tp_pct, sl_pct)
    deadline = entry_ts + pd.Timedelta(seconds=max_hold)
    hi_run, lo_run = entry_price, entry_price
    path = tk[tk["ts"] >= entry_ts]
    last_price, last_ts = entry_price, entry_ts
    for _, r in path.iterrows():
        px = float(r["price"]) if pd.notna(r["price"]) else None
        if px is None or px <= 0:
            continue
        hi_run, lo_run = max(hi_run, px), min(lo_run, px)
        last_price, last_ts = px, r["ts"]
        hit_sl = (px <= sl) if sig.side == "LONG" else (px >= sl)
        hit_tp = (px >= tp) if sig.side == "LONG" else (px <= tp)
        if hit_sl and hit_tp:
            # 1イベントで両側を跨ぐ(ギャップ) → 保守的に損切り
            mae, mfe = _excursion_pct(sig.side, entry_price, hi_run, lo_run)
            return _finalize(sd, sig, mode, entry_ts, entry_price, r["ts"], sl, qty, "SL", mae, mfe, params)
        if hit_sl:
            mae, mfe = _excursion_pct(sig.side, entry_price, hi_run, lo_run)
            return _finalize(sd, sig, mode, entry_ts, entry_price, r["ts"], sl, qty, "SL", mae, mfe, params)
        if hit_tp:
            mae, mfe = _excursion_pct(sig.side, entry_price, hi_run, lo_run)
            return _finalize(sd, sig, mode, entry_ts, entry_price, r["ts"], tp, qty, "TP", mae, mfe, params)
        if r["ts"] >= deadline:
            mae, mfe = _excursion_pct(sig.side, entry_price, hi_run, lo_run)
            return _finalize(sd, sig, mode, entry_ts, entry_price, r["ts"], px, qty, "TIME", mae, mfe, params)
    # ティックが尽きた → 最終値でEOD(価格欠損で時間切れを正常決済にしない)
    mae, mfe = _excursion_pct(sig.side, entry_price, hi_run, lo_run)
    reason = "EOD" if last_ts < deadline else "TIME"
    return _finalize(sd, sig, mode, entry_ts, entry_price, last_ts, last_price, qty, reason, mae, mfe, params)
