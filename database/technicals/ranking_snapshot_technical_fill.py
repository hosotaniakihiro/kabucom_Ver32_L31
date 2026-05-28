# ============================================================
# File   : database/technicals/ranking_snapshot_technical_fill.py
# Version: Ver1.0-RANKING-SNAPSHOT-TECHNICAL-FILL
# ------------------------------------------------------------
# 目的:
#   ranking_snapshot_1min に保存済みの価格・出来高履歴から、
#   1m/3m/5m それぞれの ma5/ma25/ma75 と主要テクニカル指標を計算してDBへ保存する。
#
# 保存する主な列:
#   ma5_1m / ma25_1m / ma75_1m
#   ma5_3m / ma25_3m / ma75_3m
#   ma5_5m / ma25_5m / ma75_5m
#   rsi_* / macd_* / signal_* / macd_hist_*
#   slope_* / slope_pct_* / atr_* / slope_atr_scaled_*
#   volume_sma5_* / volume_sma25_* / volume_ratio5_*
#   technical_ready_* / technical_updated_at / technical_source
#
# 注意:
#   - 1mは ranking_snapshot_1min の価格をそのまま使用。
#   - 3m/5mは ranking_snapshot_1min を symbol単位でresampleして計算し、
#     各1m行には「その時刻以前で最新の3m/5mテクニカル」を asof で付与する。
#   - OHLCが無い場合が多いのでATRは価格差ベースの簡易TRで計算する。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import sqlite3
import time
from typing import Any, Iterable

import pandas as pd

from database.paths.ranking_paths import resolve_ranking_db_path
from database.schema.ranking_snapshot_schema import SNAPSHOT_TABLE, patch_ranking_snapshot_schema
from database.sqlite import prepare_sqlite_connection, quote_ident

logger = logging.getLogger(__name__)

TECH_INTERVALS = ("1m", "3m", "5m")
TECH_COLUMNS: list[str] = []
for _tf in TECH_INTERVALS:
    TECH_COLUMNS.extend([
        f"ma5_{_tf}", f"ma25_{_tf}", f"ma75_{_tf}",
        f"ma5_slope_{_tf}", f"ma25_slope_{_tf}", f"ma75_slope_{_tf}",
        f"ma5_slope_pct_{_tf}", f"ma25_slope_pct_{_tf}", f"ma75_slope_pct_{_tf}",
        f"rsi_{_tf}", f"macd_{_tf}", f"signal_{_tf}", f"macd_hist_{_tf}",
        f"slope_{_tf}", f"slope_pct_{_tf}", f"atr_{_tf}", f"slope_atr_scaled_{_tf}",
        f"price_change_pct_{_tf}",
        f"volume_sma5_{_tf}", f"volume_sma25_{_tf}", f"volume_ratio5_{_tf}",
        f"technical_ready_{_tf}",
    ])
TECH_COLUMNS.extend(["technical_updated_at", "technical_source"])


def _to_symbol_list(symbols: Iterable[Any] | None) -> list[str]:
    if not symbols:
        return []
    out: list[str] = []
    for s in symbols:
        try:
            v = str(s or "").strip()
            if v.endswith(".0"):
                v = v[:-2]
            if v and v not in out:
                out.append(v)
        except Exception:
            continue
    return out


def _safe_float_series(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").astype("float64")


def _rsi(close: pd.Series, period: int = 14) -> pd.Series:
    close = _safe_float_series(close)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=max(3, period // 2)).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=max(3, period // 2)).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    out = 100 - (100 / (1 + rs))
    return pd.to_numeric(out, errors="coerce").fillna(50.0)


def _macd(close: pd.Series) -> tuple[pd.Series, pd.Series, pd.Series]:
    close = _safe_float_series(close)
    ema12 = close.ewm(span=12, adjust=False, min_periods=3).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=5).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False, min_periods=3).mean()
    hist = macd - signal
    return macd.fillna(0.0), signal.fillna(0.0), hist.fillna(0.0)


def _add_indicators(base: pd.DataFrame, tf: str) -> pd.DataFrame:
    if base is None or base.empty:
        return pd.DataFrame()
    x = base.copy().sort_values("datetime")
    x["close"] = _safe_float_series(x["close"])
    x["volume"] = _safe_float_series(x.get("volume", pd.Series(0.0, index=x.index))).fillna(0.0)
    x = x.dropna(subset=["datetime", "close"])
    if x.empty:
        return x

    close = x["close"]
    volume = x["volume"]
    for n in (5, 25, 75):
        ma = close.rolling(n, min_periods=2).mean()
        prev = ma.shift(1)
        slope = ma - prev
        x[f"ma{n}_{tf}"] = ma
        x[f"ma{n}_slope_{tf}"] = slope.fillna(0.0)
        x[f"ma{n}_slope_pct_{tf}"] = (slope / prev.replace(0, pd.NA) * 100.0).replace([float("inf"), -float("inf")], pd.NA).fillna(0.0)

    x[f"rsi_{tf}"] = _rsi(close)
    macd, signal, hist = _macd(close)
    x[f"macd_{tf}"] = macd
    x[f"signal_{tf}"] = signal
    x[f"macd_hist_{tf}"] = hist

    prev_close = close.shift(1)
    slope = close - prev_close
    x[f"slope_{tf}"] = slope.fillna(0.0)
    x[f"slope_pct_{tf}"] = (slope / prev_close.replace(0, pd.NA) * 100.0).replace([float("inf"), -float("inf")], pd.NA).fillna(0.0)
    tr = (close - prev_close).abs()
    atr = tr.rolling(14, min_periods=3).mean()
    x[f"atr_{tf}"] = atr.fillna(0.0)
    x[f"slope_atr_scaled_{tf}"] = (slope / atr.replace(0, pd.NA)).replace([float("inf"), -float("inf")], pd.NA).fillna(0.0)
    x[f"price_change_pct_{tf}"] = x[f"slope_pct_{tf}"]

    x[f"volume_sma5_{tf}"] = volume.rolling(5, min_periods=2).mean().fillna(0.0)
    x[f"volume_sma25_{tf}"] = volume.rolling(25, min_periods=2).mean().fillna(0.0)
    x[f"volume_ratio5_{tf}"] = (volume / x[f"volume_sma5_{tf}"].replace(0, pd.NA)).replace([float("inf"), -float("inf")], pd.NA).fillna(0.0)
    # ma75までは揃わなくても、短期指標が計算できたらready=1にする。
    x[f"technical_ready_{tf}"] = ((x[f"ma5_{tf}"].notna()) & (x[f"rsi_{tf}"].notna())).astype(int)
    return x


def _build_symbol_technical_df(symbol_df: pd.DataFrame) -> pd.DataFrame:
    if symbol_df is None or symbol_df.empty:
        return pd.DataFrame()
    x = symbol_df.copy()
    x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce").dt.floor("min")
    x["close"] = _safe_float_series(x["price"])
    x["volume"] = _safe_float_series(x.get("volume", pd.Series(0.0, index=x.index))).fillna(0.0)
    x = x.dropna(subset=["datetime", "close"]).sort_values("datetime")
    x = x.drop_duplicates(subset=["datetime"], keep="last")
    if x.empty:
        return pd.DataFrame()

    one = _add_indicators(x[["datetime", "close", "volume"]], "1m")
    out = x[["symbol", "datetime", "ranking_type", "market"]].copy()
    out = out.merge(one[["datetime"] + [c for c in one.columns if c.endswith("_1m")]], on="datetime", how="left")

    for rule, tf in (("3min", "3m"), ("5min", "5m")):
        rs = x.set_index("datetime").resample(rule).agg({"close": "last", "volume": "sum"}).dropna(subset=["close"]).reset_index()
        tech = _add_indicators(rs, tf)
        if tech.empty:
            continue
        cols = ["datetime"] + [c for c in tech.columns if c.endswith(f"_{tf}")]
        left = out.sort_values("datetime")
        right = tech[cols].sort_values("datetime")
        out = pd.merge_asof(left, right, on="datetime", direction="backward")

    for c in TECH_COLUMNS:
        if c not in out.columns and c not in {"technical_updated_at", "technical_source"}:
            out[c] = 0.0 if not c.startswith("technical_ready_") else 0
    out["technical_updated_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out["technical_source"] = "ranking_snapshot_price_history"
    return out


def _load_recent_rows(conn: sqlite3.Connection, *, symbols: list[str], lookback_rows: int) -> pd.DataFrame:
    table = quote_ident(SNAPSHOT_TABLE)
    limit = max(int(lookback_rows), 120)
    frames: list[pd.DataFrame] = []
    if symbols:
        for sym in symbols:
            try:
                df = pd.read_sql_query(
                    f"""
                    SELECT symbol, datetime, ranking_type, market, price, current_price, volume, trading_volume
                      FROM {table}
                     WHERE CAST(symbol AS TEXT)=?
                     ORDER BY datetime DESC
                     LIMIT ?
                    """,
                    conn,
                    params=(sym, limit),
                )
                if not df.empty:
                    frames.append(df)
            except Exception:
                logger.debug("[RANKING SNAPSHOT TECH] load symbol failed symbol=%s", sym, exc_info=True)
    else:
        try:
            df = pd.read_sql_query(
                f"""
                SELECT symbol, datetime, ranking_type, market, price, current_price, volume, trading_volume
                  FROM {table}
                 WHERE datetime >= datetime('now', '-2 hours')
                 ORDER BY symbol, datetime
                """,
                conn,
            )
            if not df.empty:
                frames.append(df)
        except Exception:
            logger.debug("[RANKING SNAPSHOT TECH] load recent all failed", exc_info=True)
    if not frames:
        return pd.DataFrame()
    x = pd.concat(frames, ignore_index=True)
    x["price"] = pd.to_numeric(x["price"], errors="coerce")
    cp = pd.to_numeric(x.get("current_price"), errors="coerce")
    x["price"] = x["price"].where(x["price"] > 0, cp)
    x["volume"] = pd.to_numeric(x.get("volume"), errors="coerce")
    tv = pd.to_numeric(x.get("trading_volume"), errors="coerce")
    x["volume"] = x["volume"].where(x["volume"].notna(), tv).fillna(0.0)
    x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce")
    x = x.dropna(subset=["symbol", "datetime", "price"])
    x = x[x["price"] > 0]
    return x.sort_values(["symbol", "datetime"])


def _update_rows(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    update_cols = [c for c in TECH_COLUMNS if c in df.columns]
    set_sql = ", ".join(f"{quote_ident(c)}=?" for c in update_cols)
    sql = f"""
        UPDATE {quote_ident(SNAPSHOT_TABLE)}
           SET {set_sql}
         WHERE symbol=?
           AND datetime=?
           AND ranking_type=?
           AND market=?
    """
    rows: list[tuple] = []
    for _, r in df.iterrows():
        vals = []
        for c in update_cols:
            v = r.get(c)
            if pd.isna(v):
                v = None
            if c.startswith("technical_ready_") and v is not None:
                v = int(v)
            vals.append(v)
        vals.extend([
            str(r.get("symbol")),
            pd.to_datetime(r.get("datetime")).strftime("%Y-%m-%d %H:%M:%S"),
            str(r.get("ranking_type") or "UNKNOWN"),
            str(r.get("market") or "ALL"),
        ])
        rows.append(tuple(vals))
    conn.executemany(sql, rows)
    return len(rows)


def fill_ranking_snapshot_technicals(
    *,
    db_path: str | None = None,
    symbols: Iterable[Any] | None = None,
    lookback_rows: int = 220,
) -> dict[str, Any]:
    if db_path is None:
        db_path = resolve_ranking_db_path()
    syms = _to_symbol_list(symbols)
    t0 = time.perf_counter()
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=10, check_same_thread=False, isolation_level=None)
        prepare_sqlite_connection(conn)
        conn.execute("BEGIN IMMEDIATE")
        patch_ranking_snapshot_schema(conn)
        raw = _load_recent_rows(conn, symbols=syms, lookback_rows=lookback_rows)
        if raw.empty:
            conn.execute("COMMIT")
            return {"ok": True, "db_path": str(db_path), "symbols": len(syms), "loaded_rows": 0, "updated_rows": 0}
        updates: list[pd.DataFrame] = []
        for _, g in raw.groupby("symbol"):
            tech = _build_symbol_technical_df(g)
            if not tech.empty:
                updates.append(tech)
        if not updates:
            conn.execute("COMMIT")
            return {"ok": True, "db_path": str(db_path), "symbols": raw["symbol"].nunique(), "loaded_rows": len(raw), "updated_rows": 0}
        upd = pd.concat(updates, ignore_index=True)
        updated = _update_rows(conn, upd)
        conn.execute("COMMIT")
        elapsed = time.perf_counter() - t0
        logger.info(
            "[RANKING SNAPSHOT TECH] filled db=%s symbols=%s loaded_rows=%s updated_rows=%s elapsed=%.3fs",
            db_path,
            raw["symbol"].nunique(),
            len(raw),
            updated,
            elapsed,
        )
        return {"ok": True, "db_path": str(db_path), "symbols": int(raw["symbol"].nunique()), "loaded_rows": int(len(raw)), "updated_rows": int(updated), "elapsed": elapsed}
    except Exception as exc:
        try:
            if conn is not None:
                conn.execute("ROLLBACK")
        except Exception:
            pass
        logger.warning("[RANKING SNAPSHOT TECH] fill failed db=%s symbols=%s err=%s", db_path, syms[:10], exc, exc_info=True)
        return {"ok": False, "db_path": str(db_path), "symbols": len(syms), "error": str(exc)}
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


__all__ = ["fill_ranking_snapshot_technicals"]
