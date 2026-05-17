# ============================================================
# File   : trading/entry/smart_entry_timing.py
# Version: Ver01-SMART-ENTRY-TIMING
# ------------------------------------------------------------
# AI_OK 後、即エントリーせず、5秒足・直近価格変化・勢いを確認する。
# 2500〜7000円・70万円運用では、数秒の逆行でも損失が大きくなるため、
# 入る瞬間の品質を判定する安全弁。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Iterable

import pandas as pd

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == '':
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in {'1', 'true', 'yes', 'y', 'on', 'ok', 'enable', 'enabled'}:
        return True
    if s in {'0', 'false', 'no', 'n', 'off', 'ng', 'disable', 'disabled', ''}:
        return False
    return bool(default)


ENABLE_SMART_ENTRY_TIMING = _env_bool('ENABLE_SMART_ENTRY_TIMING', True)
SMART_ENTRY_LOOKBACK_BARS = int(_env_float('SMART_ENTRY_LOOKBACK_BARS', 3))
SMART_ENTRY_MIN_MOMENTUM_PCT = _env_float('SMART_ENTRY_MIN_MOMENTUM_PCT', 0.03)
SMART_ENTRY_MAX_REVERSE_PCT = _env_float('SMART_ENTRY_MAX_REVERSE_PCT', 0.08)
SMART_ENTRY_MAX_RANGE_PCT = _env_float('SMART_ENTRY_MAX_RANGE_PCT', 0.80)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == '':
            return default
        return float(v)
    except Exception:
        return default


def _normalize_bars(bars: Any) -> pd.DataFrame:
    """
    5秒足 DataFrame / list[dict] / dict を DataFrame へ正規化する。
    必須列: close。あれば open/high/low/datetime も使う。
    """
    try:
        if bars is None:
            return pd.DataFrame()
        if isinstance(bars, pd.DataFrame):
            df = bars.copy()
        elif isinstance(bars, list):
            df = pd.DataFrame(bars)
        elif isinstance(bars, dict):
            if 'bars' in bars and isinstance(bars.get('bars'), list):
                df = pd.DataFrame(bars.get('bars'))
            else:
                df = pd.DataFrame([bars])
        else:
            return pd.DataFrame()

        # 列名ゆれ対応
        rename = {}
        for c in df.columns:
            lc = str(c).lower()
            if lc in {'current_price', 'price', 'last_price'}:
                rename[c] = 'close'
            elif lc in {'close_price'}:
                rename[c] = 'close'
            elif lc in {'open_price'}:
                rename[c] = 'open'
            elif lc in {'high_price'}:
                rename[c] = 'high'
            elif lc in {'low_price'}:
                rename[c] = 'low'
        if rename:
            df = df.rename(columns=rename)

        if 'datetime' in df.columns:
            df['datetime'] = pd.to_datetime(df['datetime'], errors='coerce')
            df = df.sort_values('datetime')
        elif 'bucket_time' in df.columns:
            df['datetime'] = pd.to_datetime(df['bucket_time'], errors='coerce')
            df = df.sort_values('datetime')

        for c in ('open', 'high', 'low', 'close'):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors='coerce').fillna(0.0)

        return df.reset_index(drop=True)
    except Exception:
        logger.exception('[SMART ENTRY] normalize bars failed')
        return pd.DataFrame()


def calc_smart_entry_timing(symbol: str, side: str, bars: Any) -> tuple[bool, dict]:
    """
    直近5秒足から今入るべきかを判定する。

    BUY:
      - 直近 momentum がプラス
      - 直近高値から大きく失速していない
      - 足レンジが大きすぎない

    SELL:
      - 直近 momentum がマイナス
      - 直近安値から大きく戻していない
      - 足レンジが大きすぎない
    """
    if not ENABLE_SMART_ENTRY_TIMING:
        return True, {'reason': 'SMART_ENTRY_TIMING_DISABLED'}

    side_u = str(side or '').upper()
    df = _normalize_bars(bars)

    if df.empty or 'close' not in df.columns:
        return False, {
            'symbol': str(symbol),
            'side': side_u,
            'reason': 'NO_5S_BARS',
            'ok': False,
        }

    lookback = max(2, int(SMART_ENTRY_LOOKBACK_BARS))
    x = df.tail(lookback).copy()
    if len(x) < 2:
        return False, {
            'symbol': str(symbol),
            'side': side_u,
            'reason': 'INSUFFICIENT_5S_BARS',
            'bars': len(x),
            'ok': False,
        }

    first = _safe_float(x['close'].iloc[0], 0.0)
    last = _safe_float(x['close'].iloc[-1], 0.0)
    high = _safe_float(x['high'].max() if 'high' in x.columns else x['close'].max(), last)
    low = _safe_float(x['low'].min() if 'low' in x.columns else x['close'].min(), last)

    if first <= 0 or last <= 0:
        return False, {
            'symbol': str(symbol),
            'side': side_u,
            'reason': 'INVALID_5S_PRICE',
            'first': first,
            'last': last,
            'ok': False,
        }

    momentum_pct = (last - first) / first * 100.0
    range_pct = (high - low) / last * 100.0 if last > 0 else 999.0

    reverse_pct = 0.0
    if side_u == 'BUY':
        reverse_pct = (high - last) / high * 100.0 if high > 0 else 0.0
    elif side_u == 'SELL':
        reverse_pct = (last - low) / low * 100.0 if low > 0 else 0.0
    else:
        return False, {
            'symbol': str(symbol),
            'side': side_u,
            'reason': 'UNKNOWN_SIDE',
            'ok': False,
        }

    detail = {
        'symbol': str(symbol),
        'side': side_u,
        'bars': int(len(x)),
        'first_close': first,
        'last_close': last,
        'high': high,
        'low': low,
        'momentum_pct': momentum_pct,
        'reverse_pct': reverse_pct,
        'range_pct': range_pct,
        'min_momentum_pct': SMART_ENTRY_MIN_MOMENTUM_PCT,
        'max_reverse_pct': SMART_ENTRY_MAX_REVERSE_PCT,
        'max_range_pct': SMART_ENTRY_MAX_RANGE_PCT,
    }

    if range_pct > SMART_ENTRY_MAX_RANGE_PCT:
        detail['reason'] = 'RANGE_TOO_WIDE'
        detail['ok'] = False
        return False, detail

    if reverse_pct > SMART_ENTRY_MAX_REVERSE_PCT:
        detail['reason'] = 'REVERSING_TOO_MUCH'
        detail['ok'] = False
        return False, detail

    if side_u == 'BUY' and momentum_pct < SMART_ENTRY_MIN_MOMENTUM_PCT:
        detail['reason'] = 'BUY_MOMENTUM_TOO_WEAK'
        detail['ok'] = False
        return False, detail

    if side_u == 'SELL' and momentum_pct > -SMART_ENTRY_MIN_MOMENTUM_PCT:
        detail['reason'] = 'SELL_MOMENTUM_TOO_WEAK'
        detail['ok'] = False
        return False, detail

    detail['reason'] = 'OK'
    detail['ok'] = True
    return True, detail


def is_smart_entry_timing_ok(symbol: str, side: str, bars: Any) -> tuple[bool, dict]:
    ok, detail = calc_smart_entry_timing(symbol, side, bars)
    if ok:
        logger.info('[SMART ENTRY] OK symbol=%s side=%s detail=%s', symbol, side, detail)
    else:
        logger.warning('[SMART ENTRY] NG symbol=%s side=%s detail=%s', symbol, side, detail)
    return ok, detail
