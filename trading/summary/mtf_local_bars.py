# ============================================================
# trading/summary/mtf_local_bars.py
# ------------------------------------------------------------
# ローカル push DB (ティック) から、銘柄ごとの 3 分足 / 5 分足を作り、
# 指標 (ma5 / ma25 / ma75 / rsi / macd / signal / hist / vwap) を付ける。
#
# 2026-09-15 の実測: main.py が持つ 3m/5m の in-memory 履歴は
#   tf=3 rows=156 (50 銘柄で 3 本ずつ) / rsi=0 macd=0 (指標が無い)
# で、[MTF MA GATE] が午前中 119 件すべて却下 (rsi_nan / macd_nan /
# 6 分間同じ値の 5m 足) になっていた。summary DB (NAS) は遅く、
# 読めない時間帯があるので、ローカルに毎秒書かれている push DB
# (%LOCALAPPDATA%\autostock\push\pushYYYYMMDD.db、(symbol, datetime) 索引あり、
# 1 銘柄 2 万ティックで 0.08 秒) から自前で足を作る。
#
# 足の作り方は summary_incremental_engine と同じ:
#   open=最初のティック価格 / high=max / low=min / close=最後 / volume=累計出来高の差分
#   ma: rolling(min_periods=1) / rsi: ewm(alpha=1/14) / macd: ema12-ema26, signal ema9
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V1.3-MTF-LOCAL-BARS-RANKING-FALLBACK"

_CACHE_TTL_SEC = 10.0
_lock = threading.Lock()
_cache: Dict[Tuple[str, int], Tuple[float, pd.DataFrame]] = {}


def _push_db_path(day: Optional[dt.date] = None) -> Optional[Path]:
    d = (day or dt.date.today()).strftime("%Y%m%d")
    cands = []
    env_dir = os.getenv("PUSH_LOCAL_DB_DIR", "").strip()
    if env_dir:
        cands.append(Path(env_dir) / f"push{d}.db")
    la = os.getenv("LOCALAPPDATA", "")
    if la:
        cands.append(Path(la) / "autostock" / "push" / f"push{d}.db")
    cands.append(Path(r"X:\raw_data\kabu_station\push") / f"push{d}.db")
    for p in cands:
        try:
            if p.exists():
                return p
        except Exception:
            continue
    return None


def _norm_symbol(v) -> str:
    s = str(v or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


# 2026-09-17: ティックは (銘柄, 日) ごとに増分で読む。全日分を毎回読み直すと
#   1 銘柄 130ms (fetchall + strptime) で、50 銘柄の走査が 10 秒かかっていた。
#   最後に読んだ datetime 文字列より後だけを取り、結合して保持する。
_tick_lock = threading.Lock()
_tick_cache: Dict[Tuple[str, str, str], Tuple[str, pd.DataFrame]] = {}
_TICK_CACHE_MAX_SYMBOLS = 400


# ------------------------------------------------------------
# 2026-09-17 (A): PUSH 未登録の銘柄はローカル ranking DB の 1 分スナップショット
#   (ranking_snapshot_1min: 毎分・約 700 銘柄の現在値と累積出来高) から足を作る。
#   PUSH 登録枠は 50 で、候補の大半 (9/17 午前は却下 74 件中 56 件) が「足なし」で
#   落ちていた。高安は無いので open/high/low=close (ATR は終値ベースで控えめになる)。
#   env: MTF_RANKING_FALLBACK_ENABLED=0 で無効。
# ------------------------------------------------------------
_RANKING_MIN_PUSH_TICKS = 5


def _ranking_fallback_enabled() -> bool:
    v = os.getenv("MTF_RANKING_FALLBACK_ENABLED", "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _ranking_db_path_for(day: dt.date):
    try:
        from trading.ranking.ranking_db_location import ranking_db_path
        p = ranking_db_path(day)
        return p if p is not None and p.exists() else None
    except Exception:
        return None


_ranking_index_tried: set = set()


def _ensure_ranking_index(p) -> None:
    """symbol, datetime の索引が無いと 1 銘柄 0.1 秒の全表走査になるので、日に 1 回だけ作る。"""
    key = str(p)
    if key in _ranking_index_tried:
        return
    _ranking_index_tried.add(key)
    try:
        con = sqlite3.connect(str(p), timeout=2.0)
        try:
            con.execute("PRAGMA busy_timeout=2000")
            con.execute("CREATE INDEX IF NOT EXISTS idx_rs1_symbol_datetime ON ranking_snapshot_1min(symbol, datetime)")
            con.commit()
        finally:
            con.close()
        logger.info("[MTF LOCAL BARS] ranking index ensured %s", p)
    except Exception:
        logger.debug("[MTF LOCAL BARS] ranking index create failed %s", p, exc_info=True)


def _load_ranking_raw(symbol: str, day: dt.date, since: str, *, exclusive: bool) -> Optional[pd.DataFrame]:
    """ranking_snapshot_1min を tick 風の (datetime, price, volume, _raw_dt) にして返す。失敗は None。"""
    p = _ranking_db_path_for(day)
    if p is None:
        return None
    _ensure_ranking_index(p)
    op = ">" if exclusive else ">="
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1.0)
        try:
            con.execute("PRAGMA busy_timeout=1000")
            df = pd.read_sql_query(
                "SELECT datetime, current_price AS price, trading_volume AS volume FROM ranking_snapshot_1min "
                f"WHERE symbol=? AND datetime {op} ? ORDER BY datetime",
                con, params=(symbol, since),
            )
        finally:
            con.close()
    except Exception:
        logger.debug("[MTF LOCAL BARS] ranking read failed symbol=%s path=%s", symbol, p, exc_info=True)
        return None
    if df.empty:
        return df
    # 同じ分に rank_type ごとの行が重複するので 1 行にする
    df = df.drop_duplicates(subset=["datetime"], keep="last")
    df["_raw_dt"] = df["datetime"].astype(str)
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    return df.dropna(subset=["datetime", "price"])


def _load_incremental(symbol: str, day: dt.date, source: str) -> pd.DataFrame:
    """(銘柄, 日, 出所) ごとに増分で読んで結合・保持する。"""
    day_key = day.strftime("%Y%m%d")
    key = (symbol, day_key, source)
    with _tick_lock:
        hit = _tick_cache.get(key)
    if hit is None:
        since_raw = day.strftime("%Y-%m-%dT09:00") if source == "push" else day.strftime("%Y-%m-%d 09:00")
        base = pd.DataFrame()
    else:
        since_raw, base = hit
    if source == "ranking":
        new = _load_ranking_raw(symbol, day, since_raw, exclusive=hit is not None)
    else:
        new = _load_ticks_raw(symbol, day, since_raw, exclusive=hit is not None)
    if new is None or new.empty:
        return base
    df = pd.concat([base, new], ignore_index=True) if not base.empty else new
    try:
        last_raw = str(new["_raw_dt"].iloc[-1])
    except Exception:
        last_raw = since_raw
    df = df.drop(columns=["_raw_dt"], errors="ignore")
    try:
        df.attrs["source"] = source
    except Exception:
        pass
    with _tick_lock:
        if len(_tick_cache) >= _TICK_CACHE_MAX_SYMBOLS:
            _tick_cache.clear()
        _tick_cache[key] = (last_raw, df)
    return df


def _load_ticks(symbol: str, day: dt.date, now: Optional[dt.datetime] = None) -> pd.DataFrame:
    """push のティックを使い、無い/止まっている銘柄は ranking の 1 分値にフォールバックする。
    PUSH 登録から外れた銘柄は push の足が止まる (rotation) ので、最終ティックが
    MTF_RANKING_FALLBACK_STALE_MIN 分 (既定 3) より古ければ ranking 側を見る。"""
    push = _load_incremental(symbol, day, "push")
    if not _ranking_fallback_enabled():
        return push
    now = now or dt.datetime.now()
    use_ranking = False
    # 2026-09-18: push の「張り始め」を時間幅で見る。PUSH は登録上限 50 の
    #   ローテーションなので、再登録直後はティックが数本しか無い。数本だと
    #   3m/5m の足が 1〜2 本しか作れず、rolling(min_periods=1) で ma5==ma25 に
    #   なり RSI も中立埋め (50.0) になる。ranking には 9:00 からの 1 分値が
    #   全部あるので、幅が足りないうちはそちらから作る。
    push_span_short = False
    if push is not None and not push.empty:
        try:
            min_span = float(os.getenv("MTF_LOCAL_BARS_MIN_PUSH_SPAN_MIN", "30") or 30)
            if min_span > 0:
                _first = push["datetime"].iloc[0]
                _last = push["datetime"].iloc[-1]
                span_min = (_last - _first).total_seconds() / 60.0
                push_span_short = span_min < min_span
        except Exception:
            push_span_short = False
    if push is None or len(push) < _RANKING_MIN_PUSH_TICKS or push_span_short:
        use_ranking = True
    else:
        try:
            last = push["datetime"].iloc[-1]
            last = last.to_pydatetime() if hasattr(last, "to_pydatetime") else last
            stale_min = float(os.getenv("MTF_RANKING_FALLBACK_STALE_MIN", "3") or 3)
            if now.date() == day and (now - last) > dt.timedelta(minutes=stale_min):
                use_ranking = True
        except Exception:
            pass
    if not use_ranking:
        return push
    rk = _load_incremental(symbol, day, "ranking")
    if rk is None or len(rk) < 3:
        return push
    try:
        # 2026-09-18: span 不足で ranking に来たときはここで push に戻さない。
        #   ranking は 1 分刻みなので最終時刻は push より最大 1 分古く、
        #   この条件だけだと必ず push (痩せた足) に戻ってしまう。
        if (
            push is not None
            and not push_span_short
            and len(push) >= _RANKING_MIN_PUSH_TICKS
            and rk["datetime"].iloc[-1] <= push["datetime"].iloc[-1]
        ):
            return push  # ranking も止まっているなら push のまま
    except Exception:
        pass
    logger.info("[MTF LOCAL BARS] ranking fallback symbol=%s rows=%s push_ticks=%s span_short=%s", symbol, len(rk), 0 if push is None else len(push), push_span_short)
    return rk


def _load_ticks_raw(symbol: str, day: dt.date, since: str, *, exclusive: bool) -> Optional[pd.DataFrame]:
    """since より後 (exclusive=True なら「より大きい」) のティックを読む。失敗は None。"""
    p = _push_db_path(day)
    if p is None:
        return None
    op = ">" if exclusive else ">="
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1.0)
        try:
            con.execute("PRAGMA busy_timeout=1000")
            # 2026-09-16: 板の厚み (Buy1..10Qty / Sell1..10Qty) も取り、足の最終ティック時点の
            #   気配残量を bid_depth / ask_depth / bid1_qty / ask1_qty として持たせる
            #   (score_config.ini の bid_stack / ask_stack / board_pressure_* 用)。
            buy_cols = ", ".join(f"Buy{i}Qty" for i in range(1, 11))
            sell_cols = ", ".join(f"Sell{i}Qty" for i in range(1, 11))
            try:
                df = pd.read_sql_query(
                    f"SELECT datetime, price, volume, {buy_cols}, {sell_cols} FROM stream_data_raw "
                    f"WHERE symbol=? AND datetime {op} ? ORDER BY datetime",
                    con, params=(symbol, since),
                )
            except Exception:
                df = pd.read_sql_query(
                    "SELECT datetime, price, volume FROM stream_data_raw "
                    f"WHERE symbol=? AND datetime {op} ? ORDER BY datetime",
                    con, params=(symbol, since),
                )
        finally:
            con.close()
    except Exception:
        logger.debug("[MTF LOCAL BARS] tick read failed symbol=%s path=%s", symbol, p, exc_info=True)
        return None
    if df.empty:
        return df
    df["_raw_dt"] = df["datetime"].astype(str)
    try:
        df["datetime"] = pd.to_datetime(df["datetime"], format="ISO8601", errors="coerce")
    except Exception:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    try:
        df["datetime"] = df["datetime"].dt.tz_localize(None)
    except Exception:
        pass
    df["price"] = pd.to_numeric(df["price"], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    bq = [c for c in df.columns if c.startswith("Buy") and c.endswith("Qty")]
    sq = [c for c in df.columns if c.startswith("Sell") and c.endswith("Qty")]
    if bq and sq:
        df["bid_depth"] = df[bq].apply(pd.to_numeric, errors="coerce").fillna(0.0).sum(axis=1)
        df["ask_depth"] = df[sq].apply(pd.to_numeric, errors="coerce").fillna(0.0).sum(axis=1)
        df["bid1_qty"] = pd.to_numeric(df.get("Buy1Qty"), errors="coerce").fillna(0.0)
        df["ask1_qty"] = pd.to_numeric(df.get("Sell1Qty"), errors="coerce").fillna(0.0)
        df = df.drop(columns=bq + sq)
    return df.dropna(subset=["datetime", "price"])


def _add_indicators(bars: pd.DataFrame) -> pd.DataFrame:
    close = bars["close"]
    bars["ma5"] = close.rolling(5, min_periods=1).mean()
    bars["ma25"] = close.rolling(25, min_periods=1).mean()
    bars["ma75"] = close.rolling(75, min_periods=1).mean()
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / 14, adjust=False, min_periods=1).mean()
    avg_loss = loss.ewm(alpha=1 / 14, adjust=False, min_periods=1).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    bars["rsi"] = (100 - (100 / (1 + rs))).fillna(50.0)
    ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
    bars["macd"] = ema12 - ema26
    bars["signal"] = bars["macd"].ewm(span=9, adjust=False, min_periods=1).mean()
    bars["hist"] = bars["macd"] - bars["signal"]
    vol = bars["volume"].clip(lower=0)
    cum_pv = (close * vol).cumsum()
    cum_v = vol.cumsum().replace(0, np.nan)
    bars["vwap"] = (cum_pv / cum_v).fillna(close)
    bars["slope"] = close.pct_change().fillna(0.0)
    return bars


# ============================================================
# 2026-09-30: 前営業日の 3/5 分足を指標計算の前に連結する
# ------------------------------------------------------------
# MTF MA ゲート (close > 5MA > 25MA) はここで作る当日ティック由来の足だけで
# ma を出しており、rolling(min_periods=1) なので寄り直後の「25MA」は手元の
# 数本の平均だった (3 分足で本当の 25 本平均になるのは 10:15、5 分足 11:05)。
# 前営業日の summary DB (stock_summary_3min / _5min) から銘柄ごと直近 120 本を
# 起動後に 1 回だけ裏で読み、ma / rsi / macd の計算にだけ使って捨てる。
# vwap と slope は当日分だけで計算し直す (前日を混ぜない)。
# 読み込みが終わるまでは従来どおり当日分だけで計算する (待たない)。
# MTF_LOCAL_BARS_PREVDAY_ENABLED=0 で無効。
# ============================================================
_PREV_LOCK = threading.Lock()
# (tf, day) -> {symbol: 前日足}。読み終えた銘柄は (空でも) キーを持つ
_PREV_BARS: Dict[Tuple[int, str], Dict[str, pd.DataFrame]] = {}
_PREV_PENDING: set = set()          # (tf, day_iso, symbol)
_PREV_WORKER_STARTED = False
_PREV_BARS_PER_SYMBOL = 120


def _prevday_enabled() -> bool:
    v = str(os.getenv("MTF_LOCAL_BARS_PREVDAY_ENABLED", "1") or "1").strip().lower()
    return v not in ("0", "false", "no", "off")


def _prev_load_batch(tf: int, day: dt.date, syms: tuple) -> Dict[str, pd.DataFrame]:
    out: Dict[str, pd.DataFrame] = {s_: pd.DataFrame() for s_ in syms}
    from trading.summary.indicators import indicator_calculator as IC

    db = IC._find_prev_summary_db(day.strftime("%Y%m%d"))
    if not db:
        return out
    df = IC._load_prevday_warmup_per_symbol(db, f"stock_summary_{int(tf)}min", syms, _PREV_BARS_PER_SYMBOL)
    if not isinstance(df, pd.DataFrame) or df.empty:
        return out
    w = pd.DataFrame({
        "symbol": df["symbol"].map(_norm_symbol),
        "datetime": pd.to_datetime(df["datetime"], errors="coerce"),
    })
    for k in ("open", "high", "low", "close"):
        src = f"{k}_price" if f"{k}_price" in df.columns else k
        w[k] = pd.to_numeric(df[src], errors="coerce") if src in df.columns else np.nan
    w["volume"] = pd.to_numeric(df["volume"], errors="coerce") if "volume" in df.columns else 0.0
    w = w.dropna(subset=["datetime", "close"])
    w = w[(w["datetime"] < pd.Timestamp(day)) & (w["close"] > 0)]
    for sym, g in w.groupby("symbol", sort=False):
        out[str(sym)] = g.sort_values("datetime").reset_index(drop=True)
    return out


def _prev_worker() -> None:
    """要求された銘柄を 1 秒ごとにまとめて前日 DB から読む。"""
    while True:
        try:
            with _PREV_LOCK:
                pending = list(_PREV_PENDING)
                _PREV_PENDING.clear()
            groups: Dict[Tuple[int, str], list] = {}
            for tf, day_iso, sym in pending:
                groups.setdefault((tf, day_iso), []).append(sym)
            for (tf, day_iso), syms in groups.items():
                t0 = dt.datetime.now()
                try:
                    got = _prev_load_batch(tf, dt.date.fromisoformat(day_iso), tuple(sorted(set(syms))))
                except Exception:
                    logger.warning("[MTF LOCAL BARS PREVDAY] load failed tf=%s", tf, exc_info=True)
                    got = {s_: pd.DataFrame() for s_ in syms}
                with _PREV_LOCK:
                    _PREV_BARS.setdefault((tf, day_iso), {}).update(got)
                logger.info(
                    "[MTF LOCAL BARS PREVDAY] loaded tf=%s symbols=%s hit=%s elapsed=%.2fs",
                    tf, len(got), sum(1 for v in got.values() if not v.empty),
                    (dt.datetime.now() - t0).total_seconds(),
                )
        except Exception:
            logger.debug("[MTF LOCAL BARS PREVDAY] worker error", exc_info=True)
        import time as _t
        _t.sleep(1.0)


def _prev_bars_for(sym: str, tf: int, day: dt.date) -> Optional[pd.DataFrame]:
    global _PREV_WORKER_STARTED
    if tf not in (3, 5) or not _prevday_enabled():
        return None
    key = (int(tf), day.isoformat())
    with _PREV_LOCK:
        per = _PREV_BARS.get(key) or {}
        if sym in per:
            return per[sym]
        _PREV_PENDING.add((int(tf), day.isoformat(), sym))
        if not _PREV_WORKER_STARTED:
            _PREV_WORKER_STARTED = True
            threading.Thread(target=_prev_worker, name="mtf-prevday", daemon=True).start()
    # 読み終わるまでは当日分だけで計算する (待たない)
    return None


def _add_indicators_with_prevday(bars: pd.DataFrame, prev: Optional[pd.DataFrame]) -> pd.DataFrame:
    if prev is None or prev.empty or bars.empty:
        return _add_indicators(bars)
    p = prev[prev["datetime"] < bars["datetime"].min()]
    if p.empty:
        return _add_indicators(bars)
    p = p.copy()
    p["__prevday"] = True
    cur = bars.copy()
    cur["__prevday"] = False
    comb = pd.concat([p, cur], ignore_index=True, sort=False)
    comb = _add_indicators(comb)
    out = comb[~comb["__prevday"].astype(bool)].drop(columns=["__prevday"]).reset_index(drop=True)
    # vwap / slope は当日分だけで (前日との窓明けギャップを混ぜない)
    today_only = _add_indicators(bars.copy())
    out["vwap"] = today_only["vwap"].values
    out["slope"] = today_only["slope"].values
    return out


def get_bars(symbol, tf: int, now: Optional[dt.datetime] = None) -> pd.DataFrame:
    """当日の tf 分足 (指標付き)。列: symbol, datetime(足の開始), open, high, low, close,
    volume, tick_count, ma5, ma25, ma75, rsi, macd, signal, hist, vwap, slope, completed。
    空なら空の DataFrame。"""
    sym = _norm_symbol(symbol)
    tf = int(tf)
    now = now or dt.datetime.now()
    key = (sym, tf)
    ts = now.timestamp()
    with _lock:
        hit = _cache.get(key)
        if hit and ts - hit[0] <= _CACHE_TTL_SEC:
            return hit[1]
    ticks = _load_ticks(sym, now.date(), now)
    if ticks.empty:
        bars = pd.DataFrame()
    else:
        t = ticks.set_index("datetime")
        slot = t.index.floor(f"{tf}min")
        g = t.groupby(slot)
        agg = {
            "open": g["price"].first(),
            "high": g["price"].max(),
            "low": g["price"].min(),
            "close": g["price"].last(),
            "cum_volume": g["volume"].max(),
            "tick_count": g["price"].count(),
        }
        for k in ("bid_depth", "ask_depth", "bid1_qty", "ask1_qty"):
            if k in t.columns:
                agg[k] = g[k].last()
        bars = pd.DataFrame(agg)
        bars["volume"] = bars["cum_volume"].diff().fillna(bars["cum_volume"]).clip(lower=0)
        bars.index.name = "datetime"
        bars = bars.reset_index()
        bars["symbol"] = sym
        _prev = _prev_bars_for(sym, tf, now.date())
        # 2026-10-03: 前日分と当日の PUSH の足の間に、当日の Yahoo 足 (登録前の分) を挟む。
        #   読み込みは裏スレッド (trading.summary.yahoo_today_warmup)、ここはキャッシュだけ。
        try:
            from trading.summary import yahoo_today_warmup as _ytw
            _ytd = _ytw.get_before(sym, tf, bars["datetime"].min()) if not bars.empty else None
            if _ytd is not None and not _ytd.empty:
                _ytd = _ytd.copy()
                _ytd["symbol"] = sym
                _prev = pd.concat([x for x in (_prev, _ytd) if x is not None and not x.empty],
                                  ignore_index=True, sort=False)
                _prev = _prev.sort_values("datetime").drop_duplicates("datetime", keep="last").reset_index(drop=True)
        except Exception:
            logger.debug("[MTF LOCAL BARS] yahoo today splice failed symbol=%s tf=%s", sym, tf, exc_info=True)
        bars = _add_indicators_with_prevday(bars, _prev)
        # 旧 conditions_long/short のヘルパー (_is_bull 等) は *_price 列名を見る
        for k in ("open", "high", "low", "close"):
            bars[f"{k}_price"] = bars[k]
        bars["current_price"] = bars["close"]
        bars["completed"] = (bars["datetime"] + pd.Timedelta(minutes=tf)) <= now
        try:
            bars["bar_source"] = str(getattr(ticks, "attrs", {}).get("source") or "push")
        except Exception:
            bars["bar_source"] = "push"
    with _lock:
        _cache[key] = (ts, bars)
    return bars


def latest_bar(symbol, tf: int, now: Optional[dt.datetime] = None, *, completed_only: bool = False,
               max_age_bars: float = 2.0) -> Optional[pd.Series]:
    """最新足。古すぎる足 (最終ティックが今から tf×max_age_bars 分より前) は None。
    PUSH 登録から外れた銘柄は足が止まるので、そのまま判定に使うと
    朝の値で午後に判定してしまう (8001: 09:09 の足しか無い状態を実測)。"""
    now = now or dt.datetime.now()
    bars = get_bars(symbol, tf, now)
    if bars is None or bars.empty:
        return None
    if completed_only:
        bars = bars[bars["completed"]]
        if bars.empty:
            return None
    r = bars.iloc[-1]
    try:
        start = r["datetime"].to_pydatetime() if hasattr(r["datetime"], "to_pydatetime") else r["datetime"]
        if max_age_bars and start + dt.timedelta(minutes=tf * max_age_bars) < now:
            return None
    except Exception:
        pass
    return r


def is_stale(bars: pd.DataFrame, tf: int, now: Optional[dt.datetime] = None, max_age_bars: float = 2.0) -> bool:
    if bars is None or bars.empty:
        return True
    now = now or dt.datetime.now()
    try:
        start = bars.iloc[-1]["datetime"]
        start = start.to_pydatetime() if hasattr(start, "to_pydatetime") else start
        return start + dt.timedelta(minutes=tf * max_age_bars) < now
    except Exception:
        return True
