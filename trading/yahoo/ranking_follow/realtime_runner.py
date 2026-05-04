# ============================================================
# File   : trading/yahoo/ranking_follow/realtime_runner.py
# Version: PRODUCTION-STABLE-YAHOO-RANKING-FOLLOW-RUNNER-REV1.0
# ------------------------------------------------------------
# Purpose:
#   当日ランキングに入った銘柄を対象に、Yahoo 1分足を差分取得し、
#   raw DB / raw DF / summary DB / summary DF を差分更新する。
#
# Important:
#   - 毎分フル再読込・フル再計算しない
#   - yahoo_tracking_state の last_* を水位として使う
#   - 計算に必要な直近ヒストリだけ DB から読み足す
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from .delta_window import make_download_window, make_summary_window
from .df_cache import merge_raw_1m, merge_summary
from .technical_calc import add_technicals, filter_calc_output_window, normalize_ohlcv

logger = logging.getLogger(__name__)

DEFAULT_MIN_PRICE = 200.0
HISTORY_MINUTES_FOR_INDICATORS = 450


def _today() -> dt.date:
    return dt.datetime.now().date()


def _to_path(p: Any) -> Path:
    return Path(str(p))


def _resolve_yahoo_db_path(trade_date: dt.date | str) -> Path:
    try:
        from database.paths.yahoo_paths import get_yahoo_1min_db_path
        return _to_path(get_yahoo_1min_db_path(trade_date))
    except Exception:
        d = pd.Timestamp(trade_date).strftime("%Y%m%d")
        return Path(r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\yahoo\intraday") / f"yahoo_1min_{d}.db"


def _resolve_ranking_db_path(trade_date: dt.date | str) -> Optional[Path]:
    try:
        from database.paths.ranking_paths import get_ranking_db_path
        return _to_path(get_ranking_db_path(trade_date))
    except Exception:
        d = pd.Timestamp(trade_date).strftime("%Y%m%d")
        p = Path(r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking") / f"ranking{d}.db"
        return p if p.exists() else p


def _read_sql(db_path: Path, sql: str, params: Iterable[Any] = ()) -> pd.DataFrame:
    if not db_path.exists():
        return pd.DataFrame()
    with sqlite3.connect(str(db_path), timeout=30) as con:
        con.execute("PRAGMA busy_timeout=30000")
        return pd.read_sql_query(sql, con, params=list(params))


def _max_dt_in_yahoo_db(symbol: str, trade_date: dt.date | str) -> Optional[pd.Timestamp]:
    db = _resolve_yahoo_db_path(trade_date)
    df = _read_sql(
        db,
        "SELECT MAX(datetime) AS max_dt FROM yahoo_1min WHERE symbol=? AND date=?",
        [str(symbol), pd.Timestamp(trade_date).strftime("%Y-%m-%d")],
    )
    if df.empty or pd.isna(df.loc[0, "max_dt"]):
        return None
    return pd.Timestamp(df.loc[0, "max_dt"])


def _read_yahoo_history(symbol: str, trade_date: dt.date | str, start, end) -> pd.DataFrame:
    db = _resolve_yahoo_db_path(trade_date)
    return _read_sql(
        db,
        """
        SELECT *
          FROM yahoo_1min
         WHERE symbol = ?
           AND datetime >= ?
           AND datetime <= ?
         ORDER BY datetime
        """,
        [str(symbol), pd.Timestamp(start).strftime("%Y-%m-%d %H:%M:%S"), pd.Timestamp(end).strftime("%Y-%m-%d %H:%M:%S")],
    )


def _download_yahoo_1m(symbol: str, start, end) -> pd.DataFrame:
    """既存Yahooクライアントがあれば利用。なければ空DFを返す。"""
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)
    ticker = f"{str(symbol).replace('.T', '')}.T"

    candidate_calls = [
        ("trading.yahoo.yahoo_download_client", "download_yahoo_1min"),
        ("trading.yahoo.yahoo_download_client", "download_1min"),
        ("trading.yahoo.yahoo_client", "download_yahoo_1min"),
    ]
    for mod_name, fn_name in candidate_calls:
        try:
            mod = __import__(mod_name, fromlist=[fn_name])
            fn = getattr(mod, fn_name, None)
            if callable(fn):
                try:
                    df = fn(symbol=ticker, start=start_ts, end=end_ts)
                except TypeError:
                    try:
                        df = fn(ticker, start_ts, end_ts)
                    except TypeError:
                        df = fn(ticker)
                return normalize_ohlcv(df, source="yahoo_ranking_follow")
        except Exception as e:
            logger.debug("[YAHOO RANKING FOLLOW] existing downloader skipped %s.%s err=%s", mod_name, fn_name, e)

    try:
        import yfinance as yf
        # yfinance は interval=1m の start/end が不安定なため period=1d で取り、範囲で切る。
        raw = yf.download(ticker, period="1d", interval="1m", progress=False, auto_adjust=False, threads=False)
        if raw is None or raw.empty:
            return pd.DataFrame()
        raw = raw.reset_index()
        dt_col = "Datetime" if "Datetime" in raw.columns else "Date"
        out = pd.DataFrame({
            "symbol": str(symbol),
            "datetime": pd.to_datetime(raw[dt_col], errors="coerce").dt.tz_localize(None),
            "open": raw.get("Open"),
            "high": raw.get("High"),
            "low": raw.get("Low"),
            "close": raw.get("Close"),
            "volume": raw.get("Volume"),
            "source": "yahoo_ranking_follow",
        })
        out = normalize_ohlcv(out, source="yahoo_ranking_follow")
        out = out[(out["datetime"] >= start_ts) & (out["datetime"] <= end_ts)].copy()
        return out.reset_index(drop=True)
    except Exception as e:
        logger.warning("[YAHOO RANKING FOLLOW] download failed symbol=%s start=%s end=%s err=%s", symbol, start, end, e)
        return pd.DataFrame()


def _upsert_yahoo_raw(df: pd.DataFrame, trade_date: dt.date | str) -> int:
    if df is None or df.empty:
        return 0
    try:
        from database.upsert.yahoo_1min_upsert import upsert_yahoo_1min_rows
        return int(upsert_yahoo_1min_rows(df, trade_date=trade_date))
    except TypeError:
        from database.upsert.yahoo_1min_upsert import upsert_yahoo_1min_rows
        return int(upsert_yahoo_1min_rows(df))
    except Exception:
        logger.exception("[YAHOO RANKING FOLLOW] raw upsert failed rows=%s", len(df))
        return 0


def _upsert_summary(df: pd.DataFrame, interval: int) -> int:
    if df is None or df.empty:
        return 0
    try:
        from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary
        return int(bulk_upsert_summary(df, interval=interval, latest_only=False, save_reason=f"yahoo_ranking_follow_{interval}m"))
    except TypeError:
        try:
            from trading.summary.persistence.summary_saver_bulk import bulk_upsert_summary
            return int(bulk_upsert_summary(df, interval))
        except Exception:
            logger.exception("[YAHOO RANKING FOLLOW] summary upsert failed interval=%s rows=%s", interval, len(df))
            return 0
    except Exception:
        logger.exception("[YAHOO RANKING FOLLOW] summary upsert failed interval=%s rows=%s", interval, len(df))
        return 0


def _sync_symbols_from_ranking(trade_date: dt.date | str, *, min_price: float) -> int:
    try:
        from database.crud.crud_yahoo_tracking_state import sync_tracking_symbols_from_ranking_db
        ranking_db = _resolve_ranking_db_path(trade_date)
        try:
            return int(sync_tracking_symbols_from_ranking_db(ranking_db_path=ranking_db, trade_date=trade_date, min_price=min_price))
        except TypeError:
            return int(sync_tracking_symbols_from_ranking_db(ranking_db, trade_date))
    except Exception:
        logger.exception("[YAHOO RANKING FOLLOW] sync tracking symbols failed")
        return 0


def _get_tracking_symbols(trade_date: dt.date | str) -> List[Dict[str, Any]]:
    try:
        from database.crud.crud_yahoo_tracking_state import get_active_tracking_symbols
        rows = get_active_tracking_symbols(trade_date=trade_date)
        if isinstance(rows, pd.DataFrame):
            return rows.to_dict("records")
        return list(rows or [])
    except Exception:
        logger.exception("[YAHOO RANKING FOLLOW] get tracking symbols failed")
        return []


def _update_download_state(symbol: str, trade_date: dt.date | str, last_dt) -> None:
    try:
        from database.crud.crud_yahoo_tracking_state import update_last_yahoo_downloaded_at
        update_last_yahoo_downloaded_at(symbol=symbol, trade_date=trade_date, last_yahoo_downloaded_at=last_dt)
    except TypeError:
        try:
            from database.crud.crud_yahoo_tracking_state import update_last_yahoo_downloaded_at
            update_last_yahoo_downloaded_at(symbol, trade_date, last_dt)
        except Exception:
            logger.exception("[YAHOO RANKING FOLLOW] update download state failed symbol=%s", symbol)
    except Exception:
        logger.exception("[YAHOO RANKING FOLLOW] update download state failed symbol=%s", symbol)


def _update_summary_state(symbol: str, trade_date: dt.date | str, last_dt, *, interval: int = 1) -> None:
    fn_name = "update_last_summary_calculated_at"
    if interval == 3:
        fn_name = "update_last_3min_calculated_at"
    elif interval == 5:
        fn_name = "update_last_5min_calculated_at"
    try:
        mod = __import__("database.crud.crud_yahoo_tracking_state", fromlist=[fn_name])
        fn = getattr(mod, fn_name)
        try:
            fn(symbol=symbol, trade_date=trade_date, last_summary_calculated_at=last_dt)
        except TypeError:
            try:
                fn(symbol=symbol, trade_date=trade_date, last_dt=last_dt)
            except TypeError:
                fn(symbol, trade_date, last_dt)
    except Exception:
        logger.exception("[YAHOO RANKING FOLLOW] update summary state failed symbol=%s interval=%s", symbol, interval)


def _calculate_symbol_summary_delta(symbol: str, trade_date: dt.date | str, last_summary_at) -> pd.DataFrame:
    yahoo_latest = _max_dt_in_yahoo_db(symbol, trade_date)
    win = make_summary_window(trade_date=trade_date, last_summary_calculated_at=last_summary_at, yahoo_latest_at=yahoo_latest)
    if not win.valid:
        logger.debug("[YAHOO RANKING FOLLOW] summary skip symbol=%s reason=%s", symbol, win.reason)
        return pd.DataFrame()

    hist_start = max(pd.Timestamp(win.start) - pd.Timedelta(minutes=HISTORY_MINUTES_FOR_INDICATORS), pd.Timestamp(trade_date).replace(hour=9, minute=0, second=0))
    hist = _read_yahoo_history(symbol, trade_date, hist_start, win.end)
    if hist.empty:
        return pd.DataFrame()
    full_calc = add_technicals(hist)
    delta = filter_calc_output_window(full_calc, start=win.start, end=win.end)
    if not delta.empty:
        delta["source"] = "summary_yahoo_ranking_follow_1m"
    return delta


def _resample_summary(summary_1m: pd.DataFrame, interval: int) -> pd.DataFrame:
    if summary_1m is None or summary_1m.empty:
        return pd.DataFrame()
    if interval == 1:
        return summary_1m.copy()

    df = normalize_ohlcv(summary_1m, source=f"summary_yahoo_ranking_follow_{interval}m")
    if df.empty:
        return df

    frames = []
    rule = f"{int(interval)}min"
    for symbol, g in df.groupby("symbol", sort=False):
        x = g.sort_values("datetime").set_index("datetime")
        agg = {
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "volume": "sum",
        }
        if "symbolname" in x.columns:
            agg["symbolname"] = "last"
        r = x.resample(rule, label="right", closed="right").agg(agg).dropna(subset=["close"]).reset_index()
        r["symbol"] = symbol
        r["open_price"] = r["open"]
        r["high_price"] = r["high"]
        r["low_price"] = r["low"]
        r["close_price"] = r["close"]
        r["source"] = f"summary_yahoo_ranking_follow_{interval}m"
        frames.append(r)
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return add_technicals(out) if not out.empty else out


def _publish_to_global_context(df: pd.DataFrame, *, interval: int) -> None:
    if df is None or df.empty:
        return
    try:
        from core.global_context import context as ctx
        for name in (
            "set_yahoo_merged_summary",
            "set_yahoo_summary",
            "set_summary_df",
            "set_merged_summary",
        ):
            fn = getattr(ctx, name, None)
            if callable(fn):
                try:
                    fn(interval, df)
                except TypeError:
                    try:
                        fn(df, interval=interval, source="yahoo")
                    except TypeError:
                        fn(df)
                logger.info("[YAHOO RANKING FOLLOW] published to global_context fn=%s interval=%s rows=%s", name, interval, len(df))
                return
    except Exception as e:
        logger.debug("[YAHOO RANKING FOLLOW] global_context publish skipped err=%s", e)


def run_yahoo_ranking_follow_once(
    *,
    now: Optional[dt.datetime] = None,
    trade_date: Optional[dt.date | str] = None,
    min_price: float = DEFAULT_MIN_PRICE,
    max_symbols_per_run: int = 80,
    do_download: bool = True,
    do_summary: bool = True,
    do_resample: bool = True,
) -> Dict[str, Any]:
    started = time.time()
    now = now or dt.datetime.now()
    trade_date = trade_date or now.date()

    synced = _sync_symbols_from_ranking(trade_date, min_price=min_price)
    states = _get_tracking_symbols(trade_date)
    if max_symbols_per_run and len(states) > max_symbols_per_run:
        states = states[:max_symbols_per_run]

    raw_rows = 0
    raw_saved = 0
    summary_rows = 0
    summary_saved = 0
    resample_rows = {3: 0, 5: 0}
    resample_saved = {3: 0, 5: 0}
    latest_summary_by_symbol: Dict[str, Any] = {}

    logger.info("[YAHOO RANKING FOLLOW] start trade_date=%s tracking=%s synced=%s", trade_date, len(states), synced)

    for st in states:
        symbol = str(st.get("symbol") or "").strip()
        if not symbol:
            continue

        last_dl = st.get("last_yahoo_downloaded_at") or _max_dt_in_yahoo_db(symbol, trade_date)
        if do_download:
            win = make_download_window(trade_date=trade_date, last_yahoo_downloaded_at=last_dl, now=now)
            if win.valid:
                raw = _download_yahoo_1m(symbol, win.start, win.end)
                raw = normalize_ohlcv(raw, source="yahoo_ranking_follow")
                if not raw.empty:
                    raw_rows += len(raw)
                    raw_saved += _upsert_yahoo_raw(raw, trade_date)
                    merge_raw_1m(raw)
                    max_dt = raw["datetime"].max()
                    _update_download_state(symbol, trade_date, max_dt)
            else:
                logger.debug("[YAHOO RANKING FOLLOW] download skip symbol=%s reason=%s", symbol, win.reason)

        if do_summary:
            last_sum = st.get("last_summary_calculated_at")
            delta_sum = _calculate_symbol_summary_delta(symbol, trade_date, last_sum)
            if not delta_sum.empty:
                summary_rows += len(delta_sum)
                summary_saved += _upsert_summary(delta_sum, interval=1)
                merge_summary(1, delta_sum)
                _publish_to_global_context(delta_sum, interval=1)
                max_dt = delta_sum["datetime"].max()
                latest_summary_by_symbol[symbol] = max_dt
                _update_summary_state(symbol, trade_date, max_dt, interval=1)

    # 3m/5mは今回更新された1m差分を元に軽く更新。既存の本格MTFがある場合は後段で上書き可。
    if do_resample and latest_summary_by_symbol:
        for interval in (3, 5):
            frames = []
            for symbol, max_dt in latest_summary_by_symbol.items():
                start = pd.Timestamp(max_dt) - pd.Timedelta(minutes=HISTORY_MINUTES_FOR_INDICATORS)
                hist = _read_yahoo_history(symbol, trade_date, start, max_dt)
                if not hist.empty:
                    frames.append(hist)
            if frames:
                one = add_technicals(pd.concat(frames, ignore_index=True))
                rs = _resample_summary(one, interval)
                if not rs.empty:
                    resample_rows[interval] += len(rs)
                    resample_saved[interval] += _upsert_summary(rs, interval=interval)
                    merge_summary(interval, rs)
                    _publish_to_global_context(rs, interval=interval)

    elapsed = time.time() - started
    result = {
        "synced": synced,
        "tracking": len(states),
        "raw_rows": raw_rows,
        "raw_saved": raw_saved,
        "summary_rows": summary_rows,
        "summary_saved": summary_saved,
        "resample_rows": resample_rows,
        "resample_saved": resample_saved,
        "elapsed_sec": round(elapsed, 3),
    }
    logger.info("[YAHOO RANKING FOLLOW] done %s", result)
    return result
