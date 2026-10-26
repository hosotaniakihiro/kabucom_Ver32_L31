# ============================================================
# File   : scheduler_jobs/summary/push_bar_backfill.py
# ------------------------------------------------------------
# 2026-10-26: PUSH から作った確定済みの1分足を、summary DB に無い分だけ書き足す。
#
# 【なぜ必要か】
#   job_summary(PUSH) の保存は summary_controller の履歴キャッシュ (merged-history)
#   から「銘柄ごとの最新1行」を取って upsert している。その履歴キャッシュは
#   多くの銘柄で1行しか無く更新もされないため、bar_cache が毎分 50 銘柄ぶんの
#   新しい足を作っていても、DB に届くのは 1 分あたり 11〜17 銘柄だけだった
#   (10/26 10:57〜10:59 実測: PUSH 受信 57 / 足ができた 49 / DB に保存 11)。
#   残りの分は約 20 分遅れの Yahoo 1 分足が後から埋めるので、その間
#   DB の1分足が止まって見え (6525 は 10:36 で停止)、DB から読む指標の
#   warmup も 20 本ほど抜けた履歴で計算されていた。
#
# 【方針】
#   毎 tick の保存のあとで、bar_cache の素の足のうち確定済み (今の分より前)
#   の直近 N 分を INSERT OR IGNORE で書く。既にある行 (指標つきの保存や Yahoo)
#   は上書きしない。Yahoo の補完や通常保存は upsert なので、あとから来た方で
#   置き換わる。値段の範囲は売買対象と同じ条件 (trade_universe_filter) に揃える。
#
# 【切り替え】
#   SUMMARY_PUSH_BAR_BACKFILL_ENABLED=0 で無効 (既定 1)
#   SUMMARY_PUSH_BAR_BACKFILL_MINUTES  直近何分を見るか (既定 10)
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

SOURCE = "push_bar_cache_1min"
_COLS = (
    "symbol", "symbolname", "datetime", "date", "time_range", "start_time", "end_time", "time",
    "source", "interval", "open", "high", "low", "close",
    "open_price", "high_price", "low_price", "close_price", "volume", "last_update",
    # 指標は付けない。スコアは 0 (中立) と未準備で入れ、この行からはシグナルが出ないようにする
    # (既存の行はスコアが NULL にならない前提で読まれている)。
    "score", "score_total", "final_score", "display_score", "score_buy", "score_sell",
    "buy_score", "sell_score", "technical_ready", "display_ready",
)
_NEUTRAL = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0)


def _enabled() -> bool:
    return str(os.getenv("SUMMARY_PUSH_BAR_BACKFILL_ENABLED", "1")).strip().lower() not in ("0", "false", "off", "no")


def _minutes() -> int:
    try:
        return max(1, int(float(os.getenv("SUMMARY_PUSH_BAR_BACKFILL_MINUTES", "10"))))
    except Exception:
        return 10


def _price_range() -> tuple[float, float]:
    try:
        from trading.summary.pipeline.trade_universe_filter import (
            resolve_pipeline_max_price,
            resolve_pipeline_min_price,
        )
        return float(resolve_pipeline_min_price()), float(resolve_pipeline_max_price())
    except Exception:
        return 150.0, 7000.0


def _today_db(now: dt.datetime) -> str:
    from trading.summary.indicators.indicator_calculator import _find_today_summary_db

    return _find_today_summary_db(now.strftime("%Y%m%d"))


def bars_to_rows(bars: pd.DataFrame, *, now: dt.datetime, since: Optional[dt.datetime]) -> list[tuple]:
    """素の足 -> INSERT 用の行。確定済み (now の分より前) で since 以降、当日、値段範囲内だけ。"""
    if bars is None or bars.empty:
        return []
    b = bars.copy()
    b["datetime"] = pd.to_datetime(b["datetime"], errors="coerce")
    b = b.dropna(subset=["symbol", "datetime", "close"])
    cur_slot = pd.Timestamp(now).floor("1min")
    b = b[(b["datetime"] < cur_slot) & (b["datetime"].dt.date == cur_slot.date())]
    if since is not None:
        b = b[b["datetime"] >= pd.Timestamp(since)]
    lo, hi = _price_range()
    b = b[(b["close"] > lo) & (b["close"] <= hi)]
    if b.empty:
        return []
    stamp = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for r in b.itertuples(index=False):
        t = r.datetime
        hm = t.strftime("%H:%M")
        hms = t.strftime("%H:%M:%S")
        o, h, l, c = (float(getattr(r, k)) for k in ("open", "high", "low", "close"))
        v = getattr(r, "volume", None)
        v = float(v) if v is not None and pd.notna(v) else 0.0
        name = getattr(r, "symbolname", "") or ""
        rows.append((
            str(r.symbol), str(name), t.strftime("%Y-%m-%d %H:%M:%S"), t.strftime("%Y-%m-%d"),
            f"{hm}-{hm}", hms, hms, hms, SOURCE, 1, o, h, l, c, o, h, l, c, v, stamp,
        ) + _NEUTRAL)
    return rows


def insert_missing(db_path: str, rows: list[tuple], *, timeout: float = 2.0) -> int:
    """(symbol, datetime) が無い行だけ入れる。入れた件数を返す。"""
    if not rows or not db_path:
        return 0
    con = sqlite3.connect(db_path, timeout=timeout)
    try:
        con.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        before = con.total_changes
        con.executemany(
            f"INSERT OR IGNORE INTO stock_summary_1min ({', '.join(_COLS)}) VALUES ({', '.join('?' * len(_COLS))})",
            rows,
        )
        con.commit()
        return con.total_changes - before
    finally:
        con.close()


def backfill_from_bar_cache(interval: int, now: Optional[dt.datetime] = None) -> int:
    """job_summary(PUSH) の保存のあとに呼ぶ。1分足だけ対象。"""
    if int(interval) != 1 or not _enabled():
        return 0
    now = now or dt.datetime.now()
    try:
        from trading.summary.engine.incremental import bar_cache

        bars = bar_cache.snapshot(1)
        since = pd.Timestamp(now).floor("1min") - pd.Timedelta(minutes=_minutes())
        rows = bars_to_rows(bars, now=now, since=since)
        if not rows:
            return 0
        db = _today_db(now)
        n = insert_missing(db, rows)
        logger.info(
            "[PUSH BAR BACKFILL] interval=1 candidates=%s inserted=%s symbols=%s since=%s db=%s",
            len(rows), n, len({r[0] for r in rows}), since, db,
        )
        return n
    except sqlite3.OperationalError as e:
        logger.warning("[PUSH BAR BACKFILL] db busy -> skip this tick err=%s", e)
        return 0
    except Exception:
        logger.exception("[PUSH BAR BACKFILL] failed")
        return 0
