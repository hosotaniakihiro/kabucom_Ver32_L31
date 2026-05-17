# ============================================================
# File   : trading/backtest_replay/replay_backtest_engine_v2.py
# Version: Ver02-REPLAY-BACKTEST-ENGINE
# ------------------------------------------------------------
# 保存済みデータを使って、ENTRY条件・EXIT条件を変えた場合の
# 簡易リプレイバックテストを行う。
#
# 入力候補:
#   - trade_audit_YYYYMMDD.db / trade_audit_events
#   - runtime_state_YYYYMMDD.db / executions_runtime
#   - summaryYYYYMMDD.db / stock_summary_1min, 3min, 5min
#
# 目的:
#   - どのentry条件が勝ちやすいか
#   - spread / quality / momentum / AI confidence の閾値変更の影響
#   - exit条件変更の損益差
#
# 注意:
#   - 実注文は出さない。
#   - まずは軽量な監査DB中心のReplay。
# ============================================================

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Iterable, Optional

import pandas as pd

AUDIT_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\trade_audit'
RUNTIME_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\runtime_state'
SUMMARY_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary'
OUT_DIR = r'\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\backtest_replay'


@dataclass
class ReplayBacktestConfig:
    min_ai_confidence: float = 0.55
    min_quality_score: float = 70.0
    max_spread_pct: float = 0.20
    min_momentum_pct_buy: float = 0.03
    min_momentum_pct_sell: float = -0.03
    stop_loss_pct: float = 0.30
    take_profit_pct: float = 0.50
    trail_drop_pct: float = 0.30
    fixed_qty: int = 100
    fee_per_trade: float = 0.0


def _today() -> str:
    return datetime.now().strftime('%Y%m%d')


def _read_sqlite(path: str, table: str) -> pd.DataFrame:
    if not os.path.exists(path):
        return pd.DataFrame()
    try:
        with sqlite3.connect(path) as conn:
            return pd.read_sql_query(f'SELECT * FROM {table}', conn)
    except Exception:
        return pd.DataFrame()


def _safe_num(s, default: float = 0.0):
    return pd.to_numeric(s, errors='coerce').fillna(default)


class ReplayBacktestEngineV2:
    def __init__(self, trade_dates: Iterable[str], symbols: Optional[Iterable[str]] = None, config: Optional[ReplayBacktestConfig] = None):
        self.trade_dates = list(trade_dates)
        self.symbols = set(str(x) for x in symbols) if symbols else None
        self.config = config or ReplayBacktestConfig()

    def audit_path(self, trade_date: str) -> str:
        return os.path.join(AUDIT_DIR, f'trade_audit_{trade_date}.db')

    def runtime_path(self, trade_date: str) -> str:
        return os.path.join(RUNTIME_DIR, f'runtime_state_{trade_date}.db')

    def summary_path(self, trade_date: str) -> str:
        return os.path.join(SUMMARY_DIR, f'summary{trade_date}.db')

    def load_trade_audit(self, trade_date: str) -> pd.DataFrame:
        df = _read_sqlite(self.audit_path(trade_date), 'trade_audit_events')
        if df.empty:
            return df
        df['trade_date'] = trade_date
        if self.symbols is not None and 'symbol' in df.columns:
            df = df[df['symbol'].astype(str).isin(self.symbols)]
        return df.reset_index(drop=True)

    def load_executions(self, trade_date: str) -> pd.DataFrame:
        df = _read_sqlite(self.runtime_path(trade_date), 'executions_runtime')
        if df.empty:
            return df
        df['trade_date'] = trade_date
        if self.symbols is not None and 'symbol' in df.columns:
            df = df[df['symbol'].astype(str).isin(self.symbols)]
        return df.reset_index(drop=True)

    def load_summary(self, trade_date: str, interval: int = 1) -> pd.DataFrame:
        table = f'stock_summary_{int(interval)}min'
        df = _read_sqlite(self.summary_path(trade_date), table)
        if df.empty:
            return df
        df['trade_date'] = trade_date
        if self.symbols is not None and 'symbol' in df.columns:
            df = df[df['symbol'].astype(str).isin(self.symbols)]
        return df.reset_index(drop=True)

    def load_all_audit(self) -> pd.DataFrame:
        frames = [self.load_trade_audit(td) for td in self.trade_dates]
        frames = [x for x in frames if x is not None and not x.empty]
        return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()

    def load_all_executions(self) -> pd.DataFrame:
        frames = [self.load_executions(td) for td in self.trade_dates]
        frames = [x for x in frames if x is not None and not x.empty]
        return pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()

    def filter_entry_candidates(self, audit: pd.DataFrame) -> pd.DataFrame:
        if audit.empty:
            return audit
        x = audit.copy()
        if 'event_type' in x.columns:
            x = x[x['event_type'].astype(str) == 'ENTRY_DECISION']
        if x.empty:
            return x

        x['ai_confidence_n'] = _safe_num(x.get('ai_confidence', 0.0))
        x['quality_score_n'] = _safe_num(x.get('quality_score', 0.0))
        x['spread_pct_n'] = _safe_num(x.get('spread_pct', 0.0))
        x['momentum_pct_n'] = _safe_num(x.get('momentum_pct', 0.0))
        x['side_u'] = x.get('side', '').astype(str).str.upper()

        cfg = self.config
        cond = (
            (x['ai_confidence_n'] >= cfg.min_ai_confidence) &
            (x['quality_score_n'] >= cfg.min_quality_score) &
            ((x['spread_pct_n'] <= cfg.max_spread_pct) | (x['spread_pct_n'] <= 0))
        )
        buy_cond = (x['side_u'].str.startswith('BUY') & (x['momentum_pct_n'] >= cfg.min_momentum_pct_buy))
        sell_cond = (x['side_u'].str.startswith('SELL') & (x['momentum_pct_n'] <= cfg.min_momentum_pct_sell))
        x['replay_entry_ok'] = cond & (buy_cond | sell_cond | (x['momentum_pct_n'] == 0))
        return x.reset_index(drop=True)

    def estimate_pnl_from_executions(self, executions: pd.DataFrame) -> pd.DataFrame:
        """
        executions_runtime がある場合、同一symbolのENTRY/EXITを時系列で簡易ペアリングする。
        厳密な建玉単位ではなく、監査用の概算。
        """
        if executions.empty:
            return pd.DataFrame()
        x = executions.copy()
        if 'execution_time' in x.columns:
            x['execution_time_dt'] = pd.to_datetime(x['execution_time'], errors='coerce')
        else:
            x['execution_time_dt'] = pd.NaT
        x['qty_n'] = _safe_num(x.get('qty', 0)).astype(int)
        x['price_n'] = _safe_num(x.get('price', 0.0))
        x['side_u'] = x.get('side', '').astype(str).str.upper()
        x = x.sort_values(['symbol', 'execution_time_dt'])

        trades = []
        open_pos: dict[str, dict[str, Any]] = {}
        cfg = self.config

        for _, r in x.iterrows():
            sym = str(r.get('symbol', ''))
            side = str(r.get('side_u', ''))
            qty = int(r.get('qty_n', 0)) or cfg.fixed_qty
            price = float(r.get('price_n', 0.0))
            t = r.get('execution_time_dt')
            if not sym or price <= 0:
                continue

            is_exit = side.startswith('EXIT_')
            clean_side = side.replace('EXIT_', '')

            if not is_exit:
                open_pos[sym] = {'symbol': sym, 'side': clean_side, 'qty': qty, 'entry_price': price, 'entry_time': t}
                continue

            pos = open_pos.pop(sym, None)
            if not pos:
                continue
            entry_price = float(pos['entry_price'])
            entry_side = str(pos['side'])
            q = min(int(pos['qty']), qty) if qty > 0 else int(pos['qty'])
            gross = (price - entry_price) * q if entry_side.startswith('BUY') else (entry_price - price) * q
            net = gross - cfg.fee_per_trade
            trades.append({
                'symbol': sym,
                'side': entry_side,
                'entry_time': pos.get('entry_time'),
                'exit_time': t,
                'entry_price': entry_price,
                'exit_price': price,
                'qty': q,
                'gross_pnl': gross,
                'net_pnl': net,
            })

        return pd.DataFrame(trades)

    def summarize(self) -> dict:
        audit = self.load_all_audit()
        executions = self.load_all_executions()
        entries = self.filter_entry_candidates(audit)
        trades = self.estimate_pnl_from_executions(executions)

        out = {
            'trade_dates': self.trade_dates,
            'symbols': sorted(list(self.symbols)) if self.symbols else None,
            'config': asdict(self.config),
            'audit_rows': int(len(audit)),
            'execution_rows': int(len(executions)),
            'entry_candidates': int(len(entries)),
            'entry_ok': int(entries['replay_entry_ok'].sum()) if not entries.empty and 'replay_entry_ok' in entries.columns else 0,
            'trades': int(len(trades)),
            'gross_pnl': float(trades['gross_pnl'].sum()) if not trades.empty and 'gross_pnl' in trades.columns else 0.0,
            'net_pnl': float(trades['net_pnl'].sum()) if not trades.empty and 'net_pnl' in trades.columns else 0.0,
            'win_rate': float((trades['net_pnl'] > 0).mean()) if not trades.empty and 'net_pnl' in trades.columns else 0.0,
            'avg_pnl': float(trades['net_pnl'].mean()) if not trades.empty and 'net_pnl' in trades.columns else 0.0,
        }
        return out

    def export(self) -> dict:
        os.makedirs(OUT_DIR, exist_ok=True)
        key = f'{self.trade_dates[0]}_{self.trade_dates[-1]}' if self.trade_dates else _today()
        prefix = os.path.join(OUT_DIR, f'replay_backtest_v2_{key}')

        audit = self.load_all_audit()
        executions = self.load_all_executions()
        entries = self.filter_entry_candidates(audit)
        trades = self.estimate_pnl_from_executions(executions)
        summary = self.summarize()

        paths = {
            'audit_csv': f'{prefix}_audit.csv',
            'entries_csv': f'{prefix}_entries.csv',
            'executions_csv': f'{prefix}_executions.csv',
            'trades_csv': f'{prefix}_trades.csv',
            'summary_json': f'{prefix}_summary.json',
        }
        audit.to_csv(paths['audit_csv'], index=False, encoding='utf-8-sig')
        entries.to_csv(paths['entries_csv'], index=False, encoding='utf-8-sig')
        executions.to_csv(paths['executions_csv'], index=False, encoding='utf-8-sig')
        trades.to_csv(paths['trades_csv'], index=False, encoding='utf-8-sig')
        summary['paths'] = paths
        with open(paths['summary_json'], 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
        return summary


def run_replay_backtest_v2(trade_dates: Iterable[str], symbols: Optional[Iterable[str]] = None, config: Optional[ReplayBacktestConfig] = None) -> dict:
    return ReplayBacktestEngineV2(trade_dates=trade_dates, symbols=symbols, config=config).export()
