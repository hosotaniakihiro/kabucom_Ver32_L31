# ============================================================
# backtest16/strategies/shorts.py
# ------------------------------------------------------------
# 空売り10戦略 SHORT_01〜SHORT_10(§5)。最大1シグナル/銘柄日。
# 空売りシグナル判定のみを行い、信用売建の執行可能性(区分A/B/C/D)・価格規制は
# エンジン/集計側で分離評価する(§5冒頭, §6)。TP=下落0.3% / SL=上昇0.3%。
# ============================================================

from __future__ import annotations

from typing import List, Optional

import pandas as pd

from backtest16 import config as C
from backtest16.features import SymbolDay, confirm_ts, volume_avg_prev, rolling_prev_extreme, range_high_low
from backtest16.strategies.base import Strategy, register, first_cross, gate, last_session_ts, _dt_on
from backtest16.engine.execution_engine import Signal


@register
class Short01GapUpLow(Strategy):
    id, side, name = "SHORT_01", "SHORT", "GU3%+初動5分足安値割れ"

    def detect(self, sd: SymbolDay, params) -> List[Signal]:
        gu = params.get("gap_up_pct", C.GAP_UP_PCT)
        if sd.gap_pct is None or sd.gap_pct < gu:
            return []
        if not sd.first5_ok or sd.first5_low is None:
            return []
        lock = _dt_on(sd.date, C.FIRST_BAR_LOCK)
        ts = first_cross(sd, sd.first5_low, "down", lock, last_session_ts(sd, params))
        if gate(sd, ts, params):
            return [self._sig(sd, ts, sd.first5_low, {"gap_pct": sd.gap_pct})]
        return []


@register
class Short02FailedBreakout(Strategy):
    id, side, name = "SHORT_02", "SHORT", "GU急騰失敗型"

    def detect(self, sd: SymbolDay, params) -> List[Signal]:
        gu = params.get("gap_up_pct", C.GAP_UP_PCT)
        if sd.gap_pct is None or sd.gap_pct < gu or sd.first5_high is None:
            return []
        lock = _dt_on(sd.date, C.FIRST_BAR_LOCK)
        end = last_session_ts(sd, params)
        bo = first_cross(sd, sd.first5_high, "up", lock, end)   # 一度は高値更新が必須(§5)
        if bo is None:
            return []
        b = sd.bars1
        if b is None or b.empty:
            return []
        b = b.sort_values("ts")
        # 高値更新後、価格が元の高値水準以下へ戻る → 戻りから3分以内に直近確定足安値を下抜け
        back_ts: Optional[pd.Timestamp] = None
        for _, bar in b.iterrows():
            cft = confirm_ts(bar["ts"], 1)
            if cft <= bo:
                continue
            if back_ts is None:
                if float(bar["close"]) <= sd.first5_high:
                    back_ts = cft
                continue
            if cft > back_ts + pd.Timedelta(minutes=3):   # 3分以内
                return []
            lvl = float(bar["low"])
            ts = first_cross(sd, lvl, "down", cft, end)
            if ts is not None and ts <= back_ts + pd.Timedelta(minutes=3) and gate(sd, ts, params):
                return [self._sig(sd, ts, lvl, {"breakout_high": sd.first5_high})]
        return []


@register
class Short03VwapRejection(Strategy):
    id, side, name = "SHORT_03", "SHORT", "VWAP反発失敗"

    def detect(self, sd: SymbolDay, params) -> List[Signal]:
        near = params.get("vwap_near_pct", C.VWAP_NEAR_PCT) / 100.0
        b1, b5 = sd.bars1, sd.bars5
        if b1 is None or b1.empty or b5 is None or b5.empty:
            return []
        b1 = b1.sort_values("ts")
        start = _dt_on(sd.date, C.SESSION_ENTRY_START)
        for _, bar in b1.iterrows():
            cft = confirm_ts(bar["ts"], 1)
            if cft < start:
                continue
            vw = bar.get("vwap")
            if vw is None or pd.isna(vw) or vw <= 0:
                continue
            close, high, low = float(bar["close"]), float(bar["high"]), float(bar["low"])
            # 価格がVWAPより下 + 5分足MA5下向き
            if close >= vw:
                continue
            if not _ma5_down(b5, cft):
                continue
            # VWAP±near まで戻る(高値がVWAP近辺) & 終値はVWAPを上回れない
            if not (high >= vw * (1 - near) and close < vw):
                continue
            # VWAPを大きく上抜けたら無効(高値がVWAP+nearを明確超過)
            if high > vw * (1 + near * 2):
                continue
            ts = first_cross(sd, low, "down", cft, last_session_ts(sd, params))
            if gate(sd, ts, params):
                return [self._sig(sd, ts, low, {"vwap": vw})]
        return []


@register
class Short04LowRetest(Strategy):
    id, side, name = "SHORT_04", "SHORT", "5分足安値割れ後の戻り売り"

    def detect(self, sd: SymbolDay, params) -> List[Signal]:
        if not sd.first5_ok or sd.first5_low is None:
            return []
        lvl = sd.first5_low
        near = params.get("vwap_near_pct", C.VWAP_NEAR_PCT) / 100.0
        lock = _dt_on(sd.date, C.FIRST_BAR_LOCK)
        end = last_session_ts(sd, params)
        bd = first_cross(sd, lvl, "down", lock, end)   # 安値を下抜け
        if bd is None:
            return []
        b = sd.bars1
        if b is None or b.empty:
            return []
        for _, bar in b.sort_values("ts").iterrows():
            cft = confirm_ts(bar["ts"], 1)
            if cft <= bd:
                continue
            high, close = float(bar["high"]), float(bar["close"])
            if high > lvl * (1 + C.RETEST_FAIL_PCT / 100.0):   # 0.20%超の上抜け=失敗
                return []
            touched = high >= lvl * (1 - near)
            reclaimed = close <= lvl
            if touched and reclaimed:
                ts = first_cross(sd, float(bar["low"]), "down", cft, end)
                if gate(sd, ts, params):
                    return [self._sig(sd, ts, float(bar["low"]), {"level": lvl})]
        return []


@register
class Short05GapDownLow(Strategy):
    id, side, name = "SHORT_05", "SHORT", "GD3%+初動5分足安値割れ"

    def detect(self, sd: SymbolDay, params) -> List[Signal]:
        gd = params.get("gap_down_pct", C.GAP_DOWN_PCT)
        if sd.gap_pct is None or sd.gap_pct > gd:
            return []
        if not sd.first5_ok or sd.first5_low is None:
            return []
        lock = _dt_on(sd.date, C.FIRST_BAR_LOCK)
        ts = first_cross(sd, sd.first5_low, "down", lock, last_session_ts(sd, params))
        if gate(sd, ts, params):
            return [self._sig(sd, ts, sd.first5_low, {"gap_pct": sd.gap_pct})]
        return []


@register
class Short06Range15Low(Strategy):
    id, side, name = "SHORT_06", "SHORT", "9:15安値ブレイク"

    def detect(self, sd: SymbolDay, params) -> List[Signal]:
        _, lo = range_high_low(sd.bars1, C.FIRST_BAR_START, C.RANGE15_END)
        if lo is None:
            return []
        after = _dt_on(sd.date, C.RANGE15_END)
        ts = first_cross(sd, lo, "down", after, last_session_ts(sd, params))
        if gate(sd, ts, params):
            return [self._sig(sd, ts, lo, {"range15_low": lo})]
        return []


@register
class Short07VolumeLow(Strategy):
    id, side, name = "SHORT_07", "SHORT", "出来高3倍+安値更新"

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
            avg = volume_avg_prev(b, i, lb)
            prev_lo = rolling_prev_extreme(b, i, lb, "low")
            if avg is None or prev_lo is None or avg <= 0:
                continue
            if float(bar["volume"]) >= mult * avg and float(bar["low"]) < prev_lo:
                if gate(sd, cft, params):
                    return [self._sig(sd, cft, float(bar["low"]), {"vol_mult": float(bar['volume'])/avg})]
        return []


@register
class Short08Ma5Bounce(Strategy):
    id, side, name = "SHORT_08", "SHORT", "3分足MA5初戻り売り"

    def detect(self, sd: SymbolDay, params) -> List[Signal]:
        near = C.MA5_NEAR_PCT / 100.0
        b = sd.bars3
        if b is None or b.empty:
            return []
        b = b.sort_values("ts").reset_index(drop=True)
        start = _dt_on(sd.date, C.SESSION_ENTRY_START)
        trend_idx = None
        for i in range(3, len(b)):
            bar = b.iloc[i]
            cft = confirm_ts(bar["ts"], 3)
            ma5 = bar.get("ma5")
            if ma5 is None or pd.isna(ma5) or ma5 <= 0:
                continue
            closes = b["close"].iloc[i-3:i+1].astype(float).tolist()
            falling3 = len(closes) == 4 and closes[1] < closes[0] and closes[2] < closes[1] and closes[3] < closes[2]
            ma5_prev = b.iloc[i-1].get("ma5")
            ma5_down = ma5_prev is not None and not pd.isna(ma5_prev) and ma5 < ma5_prev
            if trend_idx is None:
                if falling3 and ma5_down:
                    trend_idx = i
                continue
            if cft < start:
                continue
            high, close = float(bar["high"]), float(bar["close"])
            if high >= ma5 * (1 - near) and high <= ma5 * (1 + near + 0.001) and close <= ma5:
                ts = first_cross(sd, float(bar["low"]), "down", cft, last_session_ts(sd, params))
                if gate(sd, ts, params):
                    return [self._sig(sd, ts, float(bar["low"]), {"ma5": ma5})]
        return []


@register
class Short09VwapBreakdown(Strategy):
    id, side, name = "SHORT_09", "SHORT", "VWAP下抜け→戻り売り"

    def detect(self, sd: SymbolDay, params) -> List[Signal]:
        near = params.get("vwap_near_pct", C.VWAP_NEAR_PCT) / 100.0
        b = sd.bars1
        if b is None or b.empty:
            return []
        b = b.sort_values("ts").reset_index(drop=True)
        start = _dt_on(sd.date, C.SESSION_ENTRY_START)
        broke_down = False   # VWAPを上から下へ割ったか(SHORT_03との違い)
        for i in range(len(b)):
            bar = b.iloc[i]
            cft = confirm_ts(bar["ts"], 1)
            vw = bar.get("vwap")
            if vw is None or pd.isna(vw) or vw <= 0:
                continue
            close, high, low = float(bar["close"]), float(bar["high"]), float(bar["low"])
            if not broke_down:
                # 上で推移していて、確定足がVWAP下で終える = 下抜け成立
                if close < vw:
                    broke_down = True
                continue
            if cft < start:
                continue
            # 戻り: VWAP±near まで戻り、確定足が再びVWAP下で終える
            if high >= vw * (1 - near) and close < vw:
                ts = first_cross(sd, low, "down", cft, last_session_ts(sd, params))
                if gate(sd, ts, params):
                    return [self._sig(sd, ts, low, {"vwap": vw})]
        return []


@register
class Short10PriorHighFailure(Strategy):
    id, side, name = "SHORT_10", "SHORT", "前日高値突破失敗"

    def detect(self, sd: SymbolDay, params) -> List[Signal]:
        ph = sd.prev_high
        if ph is None or ph <= 0:   # 前日高値が取れない場合は推測しない(§5)
            return []
        b = sd.bars1
        if b is None or b.empty:
            return []
        b = b.sort_values("ts")
        end = last_session_ts(sd, params)
        # 当日、前日高値を1呼値以上突破
        exceeded_ts = first_cross(sd, ph, "up", _dt_on(sd.date, C.FIRST_BAR_START), end)
        if exceeded_ts is None:
            return []
        # その後、前日高値の下へ戻り、確定足終値が前日高値を下回る → その足安値割れで空売り
        for _, bar in b.iterrows():
            cft = confirm_ts(bar["ts"], 1)
            if cft <= exceeded_ts:
                continue
            if float(bar["close"]) < ph:
                ts = first_cross(sd, float(bar["low"]), "down", cft, end)
                if gate(sd, ts, params):
                    return [self._sig(sd, ts, float(bar["low"]), {"prior_high": ph})]
        return []


# ------------------------------------------------------------
def _ma5_down(b5: pd.DataFrame, now: pd.Timestamp) -> bool:
    """now 時点で確定済みの5分足MA5が下向きか。"""
    if b5 is None or b5.empty or "ma5" not in b5.columns:
        return False
    conf = b5["ts"] + pd.Timedelta(minutes=5)
    cb = b5[conf <= now].sort_values("ts")
    if len(cb) < 2:
        return False
    a, b = cb.iloc[-2].get("ma5"), cb.iloc[-1].get("ma5")
    if a is None or b is None or pd.isna(a) or pd.isna(b):
        return False
    return b < a
