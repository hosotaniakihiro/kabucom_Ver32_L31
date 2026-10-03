# ============================================================
# File   : trading/summary/yahoo_today_warmup.py
# 2026-10-03: 当日の Yahoo 1 分足で、PUSH 登録直後の銘柄の「登録前の足」を補う
# ------------------------------------------------------------
# PUSH は登録した時刻からしか足が無いので、登録直後の銘柄は MA25/MA75・RSI・MACD が
# まともな値になるまで 25〜75 分かかる。当日の Yahoo 1 分足 (X:\raw_data\yahoo\intraday\
# yahoo_1min_YYYYMMDD.db、約 20 分遅れ・ranking の銘柄) を「その銘柄の最初の足より前」だけ
# 指標計算に使う (保存はしない)。
#
# NAS の DB を銘柄指定で読むのは遅い (symbol IN で数十倍) ので、読み込みは裏のスレッドで行い、
# 呼び出し側 (1 分足 summary の構築・mtf_local_bars) は読み込み済みのキャッシュだけを使う (待たない)。
# 同じ銘柄は YAHOO_TODAY_WARMUP_REFRESH_SEC (既定 600 秒) ごとに読み直す (Yahoo 側に足が増えるため)。
# 止め方: YAHOO_TODAY_WARMUP_ENABLED=0
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from typing import Dict, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_cache: Dict[Tuple[str, int], Tuple[float, pd.DataFrame]] = {}   # (symbol, minutes) -> (loaded_at, bars)
_pending: set = set()
_worker_started = False
_day = ""


def enabled() -> bool:
    return str(os.getenv("YAHOO_TODAY_WARMUP_ENABLED", "1")).strip().lower() not in ("0", "false", "no", "off")


def _refresh_sec() -> float:
    try:
        return float(os.getenv("YAHOO_TODAY_WARMUP_REFRESH_SEC", "600") or 600)
    except Exception:
        return 600.0


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
                    now = time.time()
                    got = {}
                    if isinstance(df, pd.DataFrame) and not df.empty:
                        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
                        for s, g in df.dropna(subset=["datetime"]).groupby("symbol", sort=False):
                            got[str(s)] = g.sort_values("datetime").reset_index(drop=True)
                    with _lock:
                        for s in part:
                            _cache[(s, int(minutes))] = (now, got.get(s, pd.DataFrame()))
                    logger.info("[YAHOO TODAY WARMUP] loaded minutes=%s symbols=%s hit=%s elapsed=%.2fs",
                                minutes, len(part), len(got), time.time() - t0)
        except Exception:
            logger.debug("[YAHOO TODAY WARMUP] worker error", exc_info=True)
        time.sleep(2.0)


def get_before(symbol: str, minutes: int, before: Optional[pd.Timestamp]) -> pd.DataFrame:
    """当日の Yahoo minutes 分足のうち before より前の足 (キャッシュのみ・待たない)。
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
            _day = today
        hit = _cache.get(key)
        if hit is None or time.time() - hit[0] > _refresh_sec():
            _pending.add(key)
            if not _worker_started:
                _worker_started = True
                threading.Thread(target=_worker, name="yahoo-today-warmup", daemon=True).start()
        bars = hit[1] if hit else pd.DataFrame()
    if bars is None or bars.empty:
        return pd.DataFrame()
    if before is not None and not pd.isna(before):
        bars = bars[bars["datetime"] < pd.Timestamp(before)]
    return bars


__all__ = ["get_before", "enabled"]
