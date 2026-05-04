# ============================================================
# trading/ranking/volume_checker.py
# Ver26-RANKING-TRIGGER-ONLY
# ------------------------------------------------------------
# ✔ 出来高急増を「検知」するだけ
# ✔ ENTRYは絶対に行わない
# ✔ ranking_trigger に委譲して PUSH確定に回す
# ============================================================

import logging
import pandas as pd
import time
import datetime as dt
from collections import defaultdict, deque

from utils.alerts_util import send_discord_notify
from trading.ranking.analyzer import analyze_all_markets
from trading.ranking.ranking_trigger import trigger_ranking_entry
from settings import RANKING_COOL_TIME_MINUTES

logger = logging.getLogger(__name__)

# ============================================================
# グローバル管理
# ============================================================
last_ranking_state = {}                     # symbol → {volume, time}
volume_history = defaultdict(lambda: deque(maxlen=10))
spike_state = defaultdict(int)
last_trigger_time = defaultdict(float)

COOL_TIME_SEC = RANKING_COOL_TIME_MINUTES * 60


# ============================================================
# 🔥 累積出来高 → 1分換算出来高速度
# ============================================================
def calc_volume_speed_from_ranking(
    symbol: str,
    volume_now: float,
    now_time: dt.datetime
) -> float:
    """
    ランキングの累積出来高（TradingVolume）から
    1分換算出来高速度を算出
    """

    prev = last_ranking_state.get(symbol)
    last_ranking_state[symbol] = {
        "volume": volume_now,
        "time": now_time,
    }

    if not prev:
        return 0.0

    delta = max(volume_now - prev["volume"], 0)
    minutes = (now_time - prev["time"]).total_seconds() / 60
    if minutes <= 0:
        return 0.0

    return delta / minutes


# ============================================================
# ランキング強度評価（0〜）
# ============================================================
def evaluate_ranking_strength(symbol: str, type_name: str) -> int:
    """
    analyzer.py を用いてランキングの「質」を数値化
    """

    try:
        results = analyze_all_markets(symbol, type_name, notify=False)
    except Exception:
        return 0

    strength = 0
    markets_ok = 0
    biggest_delta = 0

    for r in results:
        if r.get("status") != "OK":
            continue

        if r.get("consecutive_up"):
            strength += 1
            markets_ok += 1

        if r.get("first_time_topN"):
            strength += 1
            markets_ok += 1

        latest = r.get("rank_latest")
        prev = r.get("rank_prev")
        if latest is not None and prev is not None:
            biggest_delta = max(biggest_delta, prev - latest)

    if biggest_delta >= 20:
        strength += 2
    elif biggest_delta >= 10:
        strength += 1

    if markets_ok >= 2:
        strength += 2

    return strength


# ============================================================
# 出来高急増検知 → ranking_trigger へ
# ============================================================
def detect_intraday_volume_spike(
    symbol: str,
    symbolname: str,
    cum_vol: float,
    price: float,
    type_name: str,
    market: str = "ALL",
    multiplier: float = 3.0,
    turnover_threshold: float = 5_000_000,
) -> bool:
    """
    出来高急増を検知したら ENTRYせず trigger_ranking_entry に渡す
    """

    now = dt.datetime.now()

    # ----------------------------------------
    # 1分換算出来高
    # ----------------------------------------
    vol_speed = calc_volume_speed_from_ranking(symbol, cum_vol, now)
    volume_history[symbol].append(vol_speed)

    # 安定待ち
    if len(volume_history[symbol]) < 6:
        return False

    avg_vol = sum(list(volume_history[symbol])[-6:-1]) / 5
    if avg_vol <= 0 or vol_speed < avg_vol * multiplier:
        return False

    # ----------------------------------------
    # 2段階スパイク確定
    # ----------------------------------------
    if spike_state[symbol] == 0:
        spike_state[symbol] = 1
        return False

    spike_state[symbol] = 0

    turnover = vol_speed * price
    if turnover < turnover_threshold:
        return False

    # ----------------------------------------
    # ランキング強度
    # ----------------------------------------
    ranking_strength = evaluate_ranking_strength(symbol, type_name)
    if ranking_strength < 3:
        return False

    # ----------------------------------------
    # Discord 通知
    # ----------------------------------------
    send_discord_notify(
        f"🚀 **ランキング出来高急増 ENTRY候補**\n"
        f"銘柄: {symbol} {symbolname}\n"
        f"種別: {type_name} / 市場: {market}\n"
        f"1分換算出来高: {vol_speed:,.0f}\n"
        f"倍率: {vol_speed / avg_vol:.2f}倍\n"
        f"売買代金: {turnover / 1_000_000:.1f} 百万円\n"
        f"ランキング強度: {ranking_strength}\n"
        f"➡ PUSH確定待ち"
    )

    # ----------------------------------------
    # ranking_trigger へ委譲
    # ----------------------------------------
    reason = f"出来高急増 + ランキング強度({ranking_strength})"

    trigger_ranking_entry(
        symbol=symbol,
        symbolname=symbolname,
        type_name=type_name,
        ranking_strength=ranking_strength,
        volume_speed=vol_speed,
        reason=reason,
        market=market,
    )

    return True


# ============================================================
# メイン：ランキングDFからチェック
# ============================================================
def check_volume_from_ranking(
    df: pd.DataFrame,
    exch_name: str = "ALL",
    limit: int | None = None,
):
    """
    scheduler から呼ばれる入口
    """

    now_ts = time.time()

    # クールタイム
    if now_ts - last_trigger_time[exch_name] < COOL_TIME_SEC:
        return

    if limit:
        df = df.head(limit)

    for _, row in df.iterrows():

        symbol = row.get("symbol")
        symbolname = row.get("symbolname", "")
        price = row.get("current_price")
        cum_vol = row.get("trading_volume")
        type_name = row.get("type_name", "ランキング")

        if not symbol or price is None or cum_vol is None:
            continue

        try:
            detect_intraday_volume_spike(
                symbol=symbol,
                symbolname=symbolname,
                cum_vol=float(cum_vol),
                price=float(price),
                type_name=type_name,
                market=exch_name,
            )
        except Exception as e:
            logger.error(f"❌ volume_checker error: {symbol} {e}", exc_info=True)

    last_trigger_time[exch_name] = now_ts
