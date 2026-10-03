# ============================================================
# File   : trading/summary/yahoo_today_warmup.py
# 2026-10-03: 当日の Yahoo 1 分足で、PUSH 登録直後の銘柄の「登録前の足」を補う
# ------------------------------------------------------------
# PUSH は登録した時刻からしか足が無いので、登録直後の銘柄は MA25/MA75・RSI・MACD が
# まともな値になるまで 25〜75 分かかる。当日の Yahoo 1 分足 (X:\raw_data\yahoo\intraday\
# yahoo_1min_YYYYMMDD.db、約 20 分遅れ・ranking の銘柄) を「その銘柄の最初の足より前」だけ
# 指標計算に使う (保存はしない)。
#
# 2026-10-03 (2): Yahoo は約 20 分遅れなので、Yahoo の最後の足と PUSH の最初の足の間に
#   5 分足で 4〜6 本の抜けができる。そこは当日の ranking DB (ranking_snapshot_1min、ほぼ 1 分ごとの
#   株価と累計出来高・ローカル) から作った足で埋める。始値/高値/安値は取れないので終値だけの粗い足。
#   10/2 実測: 場中に PUSH が始まった 339 銘柄のうち 302 銘柄 (89%) は直前 25 分に ranking の記録あり。
#   ranking にも無い抜けは、その銘柄だけ読み直しを YAHOO_TODAY_WARMUP_GAP_REFRESH_SEC (既定 120 秒) に縮める。
#   値を線でつないだ作り物の足は入れない。
#
# NAS の DB を銘柄指定で読むのは遅い (symbol IN で数十倍) ので、読み込みは裏のスレッドで行い、
# 呼び出し側 (1 分足 summary の構築・mtf_local_bars) は読み込み済みのキャッシュだけを使う (待たない)。
# 同じ銘柄は YAHOO_TODAY_WARMUP_REFRESH_SEC (既定 600 秒) ごとに読み直す (Yahoo 側に足が増えるため)。
# 止め方: YAHOO_TODAY_WARMUP_ENABLED=0 (ranking の穴埋めだけ止める: YAHOO_TODAY_WARMUP_RANKING_FILL=0)
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
import time
from typing import Dict, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# (symbol, minutes) -> (loaded_at, yahoo_bars, ranking_bars)
_cache: Dict[Tuple[str, int], Tuple[float, pd.DataFrame, pd.DataFrame]] = {}
_gap: Dict[Tuple[str, int], bool] = {}       # 前回の get_before で Yahoo の後ろに抜けが残っていたか
_pending: set = set()
_worker_started = False
_day = ""
_stats = {"calls": 0, "rank_filled": 0, "last_log": 0.0}


def enabled() -> bool:
    return str(os.getenv("YAHOO_TODAY_WARMUP_ENABLED", "1")).strip().lower() not in ("0", "false", "no", "off")


def _ranking_fill_enabled() -> bool:
    return str(os.getenv("YAHOO_TODAY_WARMUP_RANKING_FILL", "1")).strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)) or default)
    except Exception:
        return float(default)


def _refresh_sec() -> float:
    return _env_float("YAHOO_TODAY_WARMUP_REFRESH_SEC", 600.0)


def _gap_refresh_sec() -> float:
    return _env_float("YAHOO_TODAY_WARMUP_GAP_REFRESH_SEC", 120.0)


def _load_ranking_today(ymd: str, symbols: tuple, minutes: int) -> pd.DataFrame:
    """当日の ranking_snapshot_1min から minutes 分足を作る (終値だけの粗い足)。
    - 同じ分に ranking の種類ごとの行があり、出来高 0 の種類と実際の累計の種類が混ざるので、
      分ごとに最大値を取り、累計の最大値 (cummax) の差分を出来高にする。
    - スナップショットの hh:mm は「hh:mm-1 から始まる 1 分足の終値」に相当するので 1 分戻してから束ねる。"""
    from trading.ranking.ranking_db_location import ranking_db_path
    from trading.summary.indicators import indicator_calculator as IC

    path = ranking_db_path(dt.datetime.strptime(ymd, "%Y%m%d").date())
    if path is None or not os.path.exists(str(path)):
        return pd.DataFrame()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    try:
        ph = ",".join("?" * len(symbols))
        d = pd.read_sql_query(
            "SELECT symbol, datetime, MAX(COALESCE(NULLIF(current_price, 0), price)) AS close, "
            "MAX(MAX(COALESCE(trading_volume, 0), COALESCE(volume, 0))) AS cumvol "
            f"FROM ranking_snapshot_1min WHERE symbol IN ({ph}) GROUP BY symbol, datetime",
            conn, params=list(symbols))
    finally:
        conn.close()
    if d.empty:
        return d
    d["symbol"] = d["symbol"].astype(str)
    d["datetime"] = pd.to_datetime(d["datetime"], errors="coerce") - pd.Timedelta(minutes=1)
    d["close"] = pd.to_numeric(d["close"], errors="coerce")
    d = d.dropna(subset=["datetime", "close"])
    d = d[d["close"] > 0].sort_values(["symbol", "datetime"])
    if d.empty:
        return d
    cum = pd.to_numeric(d["cumvol"], errors="coerce").fillna(0).groupby(d["symbol"]).cummax()
    d["volume"] = cum.groupby(d["symbol"]).diff().fillna(0).clip(lower=0)
    d["open"] = d["close"]
    d["high"] = d["close"]
    d["low"] = d["close"]
    return IC._prevday_resample(d[["symbol", "datetime", "open", "high", "low", "close", "volume"]], int(minutes))


def _split_by_symbol(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    got: Dict[str, pd.DataFrame] = {}
    if isinstance(df, pd.DataFrame) and not df.empty:
        df = df.copy()
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        for s, g in df.dropna(subset=["datetime"]).groupby("symbol", sort=False):
            got[str(s)] = g.sort_values("datetime").reset_index(drop=True)
    return got


def _worker() -> None:
    from trading.summary.indicators import indicator_calculator as IC
    while True:
        try:
            with _lock:
                items = list(_pending)
                _pending.clear()
            groups: Dict[int, list] = {}
            for sym, minutes in items:
                groups.setdefault(minutes, []).append(sym)
            ymd = dt.date.today().strftime("%Y%m%d")
            for minutes, syms in groups.items():
                syms = sorted(set(syms))
                for i in range(0, len(syms), 40):
                    part = tuple(syms[i:i + 40])
                    t0 = time.time()
                    try:
                        df = IC._prevday_load_yahoo(ymd, part, int(minutes))
                    except Exception:
                        logger.warning("[YAHOO TODAY WARMUP] load failed minutes=%s", minutes, exc_info=True)
                        df = pd.DataFrame()
                    rk = pd.DataFrame()
                    if _ranking_fill_enabled():
                        try:
                            rk = _load_ranking_today(ymd, part, int(minutes))
                        except Exception:
                            logger.warning("[YAHOO TODAY WARMUP] ranking load failed minutes=%s", minutes, exc_info=True)
                    now = time.time()
                    got = _split_by_symbol(df)
                    got_rk = _split_by_symbol(rk)
                    with _lock:
                        for s in part:
                            _cache[(s, int(minutes))] = (now, got.get(s, pd.DataFrame()), got_rk.get(s, pd.DataFrame()))
                    logger.info("[YAHOO TODAY WARMUP] loaded minutes=%s symbols=%s hit=%s ranking_hit=%s elapsed=%.2fs",
                                minutes, len(part), len(got), len(got_rk), time.time() - t0)
        except Exception:
            logger.debug("[YAHOO TODAY WARMUP] worker error", exc_info=True)
        time.sleep(2.0)


def get_before(symbol: str, minutes: int, before: Optional[pd.Timestamp]) -> pd.DataFrame:
    """当日の minutes 分足のうち before より前の足 (キャッシュのみ・待たない)。
    Yahoo の足を基本に、Yahoo の最後の足より後ろ (約 20 分遅れの抜け) を ranking の足で埋める。
    無ければ空。足りない銘柄は裏で読み込みを予約する。"""
    global _worker_started, _day
    if not enabled():
        return pd.DataFrame()
    sym = str(symbol).strip()
    key = (sym, int(minutes))
    today = dt.date.today().isoformat()
    with _lock:
        if _day != today:
            _cache.clear()
            _gap.clear()
            _day = today
        hit = _cache.get(key)
        ttl = _gap_refresh_sec() if _gap.get(key) else _refresh_sec()
        if hit is None or time.time() - hit[0] > ttl:
            _pending.add(key)
            if not _worker_started:
                _worker_started = True
                threading.Thread(target=_worker, name="yahoo-today-warmup", daemon=True).start()
        ybars = hit[1] if hit else pd.DataFrame()
        rbars = hit[2] if hit else pd.DataFrame()
    b = pd.Timestamp(before) if before is not None and not pd.isna(before) else None
    if ybars is not None and not ybars.empty and b is not None:
        ybars = ybars[ybars["datetime"] < b]
    if ybars is None:
        ybars = pd.DataFrame()
    fill = pd.DataFrame()
    if b is not None and rbars is not None and not rbars.empty:
        lo = ybars["datetime"].max() if not ybars.empty else None
        m = rbars["datetime"] < b
        if lo is not None:
            m &= rbars["datetime"] > lo
        fill = rbars[m]
    # Yahoo の最後の足が before の 2 本前より古ければ「抜けあり」→ 次回は早めに読み直す
    if b is not None:
        last = ybars["datetime"].max() if not ybars.empty else None
        with _lock:
            _gap[key] = last is None or last < b - pd.Timedelta(minutes=2 * int(minutes))
    if fill.empty:
        return ybars
    out = pd.concat([x for x in (ybars, fill) if not x.empty], ignore_index=True, sort=False)
    out = out.sort_values("datetime").reset_index(drop=True)
    with _lock:
        _stats["calls"] += 1
        _stats["rank_filled"] += len(fill)
        if time.time() - _stats["last_log"] >= 300:
            _stats["last_log"] = time.time()
            logger.info("[YAHOO TODAY WARMUP] ranking gap fill calls=%s bars=%s (例 %s %s分足 %s本 %s〜%s)",
                        _stats["calls"], _stats["rank_filled"], sym, minutes, len(fill),
                        fill["datetime"].min(), fill["datetime"].max())
    return out


__all__ = ["get_before", "enabled"]
