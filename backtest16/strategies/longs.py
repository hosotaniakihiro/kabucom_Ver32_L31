# ============================================================
# backtest16/strategies/longs.py
# ------------------------------------------------------------
# 買い6戦略 LONG_01〜LONG_06(§4)。各戦略は最大1シグナル/銘柄日(最初の成立)。
# すべて確定足/ティックの "その時刻まで" の情報のみ参照(§7)。
# ============================================================

from __future__ import annotations

from typing import List

import pandas as pd

from backtest16 import config as C
from backtest16.features import SymbolDay, confirm_ts, volume_avg_prev, rolling_prev_extreme, range_high_low
from backtest16.strategies.base import Strategy, register, first_cross, gate, vwap_at, last_session_ts, _dt_on
from backtest16.engine.execution_engine import Signal


@register
class Long01GapUpBreak(Strategy):
    id, side, name = "LONG_01", "LONG", "GU3%+初動5分足高値ブレイク"

    def detect(self, sd: SymbolDay, params) -> List[Signal]:
        gu = params.get("gap_up_pct", C.GAP_UP_PCT)
        if sd.gap_pct is None or sd.gap_pct < gu:
            return []
        if not sd.first5_ok or sd.first5_high is None:   # 初回5分足が不完全なら除外(§4)
            return []
        lock = _dt_on(sd.date, C.FIRST_BAR_LOCK)
        ts = first_cross(sd, sd.first5_high, "up", lock, last_session_ts(sd, params))
        if not gate(sd, ts, params):
            return []
        return [self._sig(sd, ts, sd.first5_high, {"gap_pct": sd.gap_pct})]


@register
class Long02VwapPullback(Strategy):
    id, side, name = "LONG_02", "LONG", "VWAP押し目反発"

    def detect(self, sd: SymbolDay, params) -> List[Signal]:
        near = params.get("vwap_near_pct", C.VWAP_NEAR_PCT) / 100.0
        b = sd.bars1
        if b is None or b.empty:
            return []
        b = b.sort_values("ts").reset_index(drop=True)
        start = _dt_on(sd.date, C.SESSION_ENTRY_START)
        for i in range(len(b)):
            bar = b.iloc[i]
            cft = confirm_ts(bar["ts"], 1)
            if cft < start:
                continue
            vw = bar.get("vwap"); ma5 = bar.get("ma5"); ma25 = bar.get("ma25")
            if any(x is None or pd.isna(x) or x <= 0 for x in [vw, ma5, ma25]):
                continue
            close, open_, high, low = float(bar["close"]), float(bar["open"]), float(bar["high"]), float(bar["low"])
            # 上昇トレンド
            if not (close > ma5 and ma5 > ma25):
                continue
            # VWAP±near に接近(足の安値がVWAP近辺)かつ 0.10%超の明確な下抜けが無い
            if not (low <= vw * (1 + near) and low >= vw * (1 - C.VWAP_FAIL_PCT / 100.0)):
                continue
            # 確定足がVWAP上の陽線
            if not (close > vw and close >= open_):
                continue
            ts = first_cross(sd, high, "up", cft, last_session_ts(sd, params))
            if gate(sd, ts, params):
                return [self._sig(sd, ts, high, {"vwap": vw})]
        return []


@register
class Long03BreakRetest(Strategy):
    id, side, name = "LONG_03", "LONG", "5分足高値ブレイク後の押し目"

    def detect(self, sd: SymbolDay, params) -> List[Signal]:
        if not sd.first5_ok or sd.first5_high is None:
            return []
        lvl = sd.first5_high
        near = params.get("vwap_near_pct", C.VWAP_NEAR_PCT) / 100.0  # ±0.15%
        lock = _dt_on(sd.date, C.FIRST_BAR_LOCK)
        bo = first_cross(sd, lvl, "up", lock, last_session_ts(sd, params))  # 突破
        if bo is None:
            return []
        b = sd.bars1
        if b is None or b.empty:
            return []
        b = b.sort_values("ts")
        # 突破後、確定足が水準±near に戻り(押し目)かつ 0.20%超の下抜け無し、終値が水準上に復帰
        for _, bar in b.iterrows():
            cft = confirm_ts(bar["ts"], 1)
            if cft <= bo:
                continue
            low, close = float(bar["low"]), float(bar["close"])
            if low < lvl * (1 - C.RETEST_FAIL_PCT / 100.0):   # 0.20%超下抜け=失敗
                return []
            touched = low <= lvl * (1 + near)
            reclaimed = close >= lvl
            if touched and reclaimed:
                ts = first_cross(sd, float(bar["high"]), "up", cft, last_session_ts(sd, params))
                if gate(sd, ts, params):
                    return [self._sig(sd, ts, float(bar["high"]), {"level": lvl})]
        return []


@register
class Long04Range15Break(Strategy):
    id, side, name = "LONG_04", "LONG", "9:15高値ブレイク"

    def detect(self, sd: SymbolDay, params) -> List[Signal]:
        hi, _ = range_high_low(sd.bars1, C.FIRST_BAR_START, C.RANGE15_END)
        if hi is None:
            return []
        after = _dt_on(sd.date, C.RANGE15_END)
        ts = first_cross(sd, hi, "up", after, last_session_ts(sd, params))
        if gate(sd, ts, params):
            return [self._sig(sd, ts, hi, {"range15_high": hi})]
        return []


@register
class Long05VolumeBreak(Strategy):
    id, side, name = "LONG_05", "LONG", "出来高3倍+高値更新"

    def detect(self, sd: SymbolDay, params) -> List[Signal]:
        mult = params.get("volume_spike_mult", C.VOLUME_SPIKE_MULT)
        lb = C.VOLUME_LOOKBACK_BARS
        b = sd.bars1
        if b is None or b.empty:
            return []
        b = b.sort_values("ts").reset_index(drop=True)
        start = _dt_on(sd.date, C.SESSION_ENTRY_START)
        for i in range(lb, len(b)):
            bar = b.iloc[i]
            cft = confirm_ts(bar["ts"], 1)
            if cft < start:
                continue
            avg = volume_avg_prev(b, i, lb)           # 評価足は含めない(§7)
            prev_hi = rolling_prev_extreme(b, i, lb, "high")
            if avg is None or prev_hi is None or avg <= 0:
                continue
            if float(bar["volume"]) >= mult * avg and float(bar["high"]) > prev_hi:
                if gate(sd, cft, params):             # 確定時点でシグナル→次足始値で約定
                    return [self._sig(sd, cft, float(bar["high"]), {"vol_mult": float(bar['volume'])/avg})]
        return []


@register
class Long06Ma5Pullback(Strategy):
    id, side, name = "LONG_06", "LONG", "3分足MA5初押し反発"

    def detect(self, sd: SymbolDay, params) -> List[Signal]:
        near = C.MA5_NEAR_PCT / 100.0
        b = sd.bars3
        if b is None or b.empty:
            return []
        b = b.sort_values("ts").reset_index(drop=True)
        start = _dt_on(sd.date, C.SESSION_ENTRY_START)
        trend_established_idx = None
        for i in range(3, len(b)):
            bar = b.iloc[i]
            cft = confirm_ts(bar["ts"], 3)
            ma5 = bar.get("ma5")
            if ma5 is None or pd.isna(ma5) or ma5 <= 0:
                continue
            closes = b["close"].iloc[i-3:i+1].astype(float).tolist()
            rising3 = len(closes) == 4 and closes[1] > closes[0] and closes[2] > closes[1] and closes[3] > closes[2]
            ma5_prev = b.iloc[i-1].get("ma5")
            ma5_up = ma5_prev is not None and not pd.isna(ma5_prev) and ma5 > ma5_prev
            if trend_established_idx is None:
                if rising3 and ma5_up:
                    trend_established_idx = i
                continue
            # トレンド成立後の最初のMA5付近への押し
            if cft < start:
                continue
            low, close = float(bar["low"]), float(bar["close"])
            if low <= ma5 * (1 + near) and low >= ma5 * (1 - near - 0.001) and close >= ma5:
                ts = first_cross(sd, float(bar["high"]), "up", cft, last_session_ts(sd, params))
                if gate(sd, ts, params):
                    return [self._sig(sd, ts, float(bar["high"]), {"ma5": ma5})]
        return []
