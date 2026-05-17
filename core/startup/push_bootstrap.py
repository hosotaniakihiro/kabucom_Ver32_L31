# ============================================================
# File   : core/startup/push_bootstrap.py
# Ver    : PRODUCTION-STABLE-REV7-PREMARKET-RESTORE-0800
# ------------------------------------------------------------
# ✔ pushDB 復元（当日分）
# ✔ 起動時は全日分を読まず、最大行だけ高速復元
# ✔ time列が HH:MM:SS / HHMMSS / 時刻のみ でも当日datetimeへ補完
# ✔ time列には日付付きcutoff検索を使わず、空振りwarningを防止
# ✔ datetime/received_at 等の日付付き列がある場合だけSQLite側で直近検索
# ✔ pandas dateutil warning を抑制
# ✔ datetime列を必ず補完
# ✔ 寄前PUSH/板気配データを復元対象に含める（既定 08:00〜15:30）
# ✔ PUSH_BOOTSTRAP_SESSION_OPEN / PUSH_BOOTSTRAP_SESSION_CLOSE で復元時間を変更可能
# ✔ 列名小文字正規化
# ✔ 例外完全吸収
# ✔ push_df 安全初期化
# ✔ WebSocket memory-only df とマージ可能な形式へ整える
# ✔ normalize empty 時に、列名・時刻parse・価格列・symbol列・市場時間filterの診断を出す
# ============================================================

from __future__ import annotations

import os
import re
import sqlite3
import logging
import datetime as dt
from typing import Optional, Any

import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# 設定
# ------------------------------------------------------------

MAX_RESTORE_ROWS = int(os.environ.get("PUSH_BOOTSTRAP_MAX_RESTORE_ROWS", "8000"))
RESTORE_LOOKBACK_MINUTES = int(os.environ.get("PUSH_BOOTSTRAP_LOOKBACK_MINUTES", "120"))

_SQLITE_DATETIME_COLUMN_CANDIDATES = (
    "datetime",
    "timestamp",
    "current_price_time",
    "received_at",
    "inserted_at",
)

_TIME_COLUMN_CANDIDATES = (
    "time",
    "datetime",
    "timestamp",
    "current_price_time",
    "received_at",
    "inserted_at",
    "CurrentPriceTime",
    "CurrentPriceTimeNano",
)


def _parse_hhmm_time(value: str, default: dt.time) -> dt.time:
    try:
        s = str(value or "").strip()
        if not s:
            return default
        m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$", s)
        if not m:
            return default
        hh = int(m.group(1))
        mm = int(m.group(2))
        ss = int(m.group(3) or 0)
        if 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59:
            return dt.time(hh, mm, ss)
    except Exception:
        pass
    return default


# 寄前の板気配/CalcPrice を残すため、既定を 08:00 にする。
# 必要なら set PUSH_BOOTSTRAP_SESSION_OPEN=08:30 などで調整可能。
SESSION_OPEN_TIME = _parse_hhmm_time(
    os.environ.get("PUSH_BOOTSTRAP_SESSION_OPEN", "08:00"),
    dt.time(8, 0),
)
SESSION_CLOSE_TIME = _parse_hhmm_time(
    os.environ.get("PUSH_BOOTSTRAP_SESSION_CLOSE", "15:30"),
    dt.time(15, 30),
)

# 旧名を参照している外部コードへの互換用。
MARKET_OPEN_TIME = SESSION_OPEN_TIME
MARKET_CLOSE_TIME = SESSION_CLOSE_TIME


def _today_date() -> dt.date:
    try:
        return dt.datetime.now().date()
    except Exception:
        return dt.date.today()


def _sample_values(df: pd.DataFrame, col: str, n: int = 5) -> list[str]:
    try:
        if df is None or df.empty or col not in df.columns:
            return []
        return [str(x) for x in df[col].dropna().head(n).tolist()]
    except Exception:
        return []


def _parse_time_only_to_today(value: Any, trade_date: dt.date | None = None) -> Optional[dt.datetime]:
    if value is None:
        return None

    trade_date = trade_date or _today_date()

    try:
        s = str(value).strip()
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return None

        if re.search(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}", s):
            return None

        m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?$", s)
        if m:
            hh = int(m.group(1))
            mm = int(m.group(2))
            ss = int(m.group(3) or 0)
            micros = int((m.group(4) or "0").ljust(6, "0")[:6])
            if 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59:
                return dt.datetime.combine(trade_date, dt.time(hh, mm, ss, micros))
            return None

        s2 = s.split(".", 1)[0]
        if s2.isdigit() and 3 <= len(s2) <= 6:
            s2 = s2.zfill(6)
            hh = int(s2[0:2])
            mm = int(s2[2:4])
            ss = int(s2[4:6])
            if 0 <= hh <= 23 and 0 <= mm <= 59 and 0 <= ss <= 59:
                return dt.datetime.combine(trade_date, dt.time(hh, mm, ss))
            return None

        return None

    except Exception:
        return None


def _safe_parse_datetime_value(value: Any, trade_date: dt.date | None = None) -> Optional[dt.datetime]:
    if value is None:
        return None

    t_only = _parse_time_only_to_today(value, trade_date=trade_date)
    if t_only is not None:
        return t_only

    try:
        s = str(value).strip()
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return None

        ts = pd.to_datetime(s, errors="coerce")
        if pd.isna(ts):
            return None

        try:
            if getattr(ts, "tzinfo", None) is not None:
                ts = ts.tz_convert("Asia/Tokyo").tz_localize(None)
        except Exception:
            try:
                ts = ts.tz_localize(None)
            except Exception:
                pass

        py = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
        if isinstance(py, dt.datetime) and py.year < 2000:
            return None
        return py

    except Exception:
        return None


def _parse_datetime_series(series: pd.Series, trade_date: dt.date | None = None) -> pd.Series:
    if series is None:
        return pd.Series(dtype="datetime64[ns]")

    trade_date = trade_date or _today_date()

    try:
        out = series.apply(lambda x: _safe_parse_datetime_value(x, trade_date=trade_date))
        return pd.to_datetime(out, errors="coerce")
    except Exception:
        try:
            return pd.to_datetime(series, errors="coerce")
        except Exception:
            return pd.Series(pd.NaT, index=getattr(series, "index", None), dtype="datetime64[ns]")


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(r[1]).strip().lower() for r in rows if len(r) >= 2}
    except Exception:
        return set()


def _resolve_sqlite_datetime_column(cols: set[str]) -> str | None:
    lower = {str(c).strip().lower() for c in cols}
    for c in _SQLITE_DATETIME_COLUMN_CANDIDATES:
        if c.lower() in lower:
            return c.lower()
    return None


def _load_push_db(db_path: str) -> pd.DataFrame:
    started = dt.datetime.now()
    cutoff_dt = started - dt.timedelta(minutes=max(RESTORE_LOOKBACK_MINUTES, 1))
    cutoff_text = cutoff_dt.strftime("%Y-%m-%d %H:%M:%S")

    try:
        with sqlite3.connect(db_path, timeout=5.0) as conn:
            try:
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA query_only=ON")
            except Exception:
                pass

            cols = _table_columns(conn, "stream_data")
            dt_col = _resolve_sqlite_datetime_column(cols)
            logger.info("[PUSH BOOTSTRAP DIAG] table=stream_data cols=%s dt_col=%s", sorted(cols), dt_col)

            if dt_col:
                try:
                    sql = f"""
                    SELECT *
                    FROM stream_data
                    WHERE {dt_col} >= ?
                    ORDER BY rowid DESC
                    LIMIT ?
                    """
                    df = pd.read_sql(sql, conn, params=(cutoff_text, int(MAX_RESTORE_ROWS)))

                    if isinstance(df, pd.DataFrame) and not df.empty:
                        logger.info(
                            "📡 pushDB recent restore query rows=%d dt_col=%s cutoff=%s limit=%d elapsed=%.3fs",
                            len(df),
                            dt_col,
                            cutoff_text,
                            MAX_RESTORE_ROWS,
                            (dt.datetime.now() - started).total_seconds(),
                        )
                        return df

                    logger.warning(
                        "⚠ pushDB recent restore returned empty dt_col=%s cutoff=%s -> fallback latest rows",
                        dt_col,
                        cutoff_text,
                    )
                except Exception:
                    logger.warning("⚠ pushDB recent restore query failed dt_col=%s -> fallback latest rows", dt_col, exc_info=True)

            df = pd.read_sql(
                """
                SELECT *
                FROM stream_data
                ORDER BY rowid DESC
                LIMIT ?
                """,
                conn,
                params=(int(MAX_RESTORE_ROWS),),
            )

            logger.info(
                "📡 pushDB fallback restore query rows=%d limit=%d elapsed=%.3fs",
                len(df),
                MAX_RESTORE_ROWS,
                (dt.datetime.now() - started).total_seconds(),
            )
            return df

    except Exception:
        logger.exception("❌ pushDB restore failed")
        return pd.DataFrame()


def _pick_time_source_column(out: pd.DataFrame) -> str | None:
    cols = {str(c).strip().lower() for c in out.columns}
    for c in _TIME_COLUMN_CANDIDATES:
        if c.lower() in cols:
            return c.lower()
    return None


def _normalize_push_df_for_summary(df: pd.DataFrame) -> pd.DataFrame:
    norm_started = dt.datetime.now()
    trade_date = _today_date()

    if df is None or df.empty:
        return pd.DataFrame()

    raw_cols = [str(c) for c in df.columns]
    out = df.copy()
    out.columns = [str(c).strip().lower() for c in out.columns]

    logger.info(
        "[PUSH BOOTSTRAP DIAG] normalize start raw_rows=%d raw_cols=%s normalized_cols=%s session_open=%s session_close=%s head=%s",
        len(out),
        raw_cols,
        list(out.columns),
        SESSION_OPEN_TIME,
        SESSION_CLOSE_TIME,
        out.head(3).to_dict("records") if len(out) else [],
    )

    if "symbol" not in out.columns:
        for c in ("code", "symbol_code", "銘柄コード"):
            if c in out.columns:
                out["symbol"] = out[c]
                logger.info("[PUSH BOOTSTRAP DIAG] symbol mapped from col=%s", c)
                break

    if "symbol" in out.columns:
        try:
            out["symbol"] = (
                out["symbol"]
                .astype(str)
                .str.strip()
                .str.upper()
                .str.replace(r"\.T$", "", regex=True)
                .str.replace(r"\.0$", "", regex=True)
            )
        except Exception:
            pass
    else:
        logger.warning("[PUSH BOOTSTRAP DIAG] symbol column missing normalized_cols=%s", list(out.columns))

    time_src = _pick_time_source_column(out)
    if time_src is None:
        logger.warning("⚠ pushDB missing time-like column cols=%s", list(out.columns))
        return pd.DataFrame()

    logger.info("[PUSH BOOTSTRAP DIAG] time_src=%s samples=%s", time_src, _sample_values(out, time_src, 10))
    parsed_time = _parse_datetime_series(out[time_src], trade_date=trade_date)
    ok_time = int(parsed_time.notna().sum())
    logger.info(
        "[PUSH BOOTSTRAP DIAG] time parse result time_src=%s ok=%d ng=%d min=%s max=%s",
        time_src,
        ok_time,
        int(len(parsed_time) - ok_time),
        parsed_time.min() if ok_time else None,
        parsed_time.max() if ok_time else None,
    )
    out["time"] = parsed_time

    before_time_drop = len(out)
    out = out.dropna(subset=["time"])
    if out.empty:
        logger.warning("⚠ pushDB all time parse failed time_src=%s before=%d samples=%s", time_src, before_time_drop, _sample_values(df, time_src, 10) if time_src in df.columns else [])
        return pd.DataFrame()

    if "datetime" in out.columns and time_src != "datetime":
        parsed_dt = _parse_datetime_series(out["datetime"], trade_date=trade_date)
        out["datetime"] = parsed_dt.fillna(out["time"])
    else:
        out["datetime"] = out["time"]

    before_session = len(out)
    try:
        out = out[out["time"].dt.time.between(SESSION_OPEN_TIME, SESSION_CLOSE_TIME)]
    except Exception:
        logger.warning("⚠ session time filter failed (skipped)")

    if out.empty:
        logger.warning(
            "⚠ pushDB all rows removed by session time filter before=%d open=%s close=%s time_min=%s time_max=%s",
            before_session,
            SESSION_OPEN_TIME,
            SESSION_CLOSE_TIME,
            parsed_time.min() if ok_time else None,
            parsed_time.max() if ok_time else None,
        )
        return pd.DataFrame()

    if "price" not in out.columns:
        for c in ("current_price", "calcprice", "calc_price", "close", "close_price", "last_price"):
            if c in out.columns:
                out["price"] = out[c]
                logger.info("[PUSH BOOTSTRAP DIAG] price mapped from col=%s", c)
                break

    if "current_price" not in out.columns and "price" in out.columns:
        out["current_price"] = out["price"]

    if "price" not in out.columns and "current_price" not in out.columns:
        logger.warning("[PUSH BOOTSTRAP DIAG] price/current_price column missing cols=%s", list(out.columns))

    if "symbol" in out.columns:
        symbol_count = int(out["symbol"].nunique(dropna=True))
    else:
        symbol_count = 0
    logger.info(
        "[PUSH BOOTSTRAP DIAG] post-filter rows=%d symbols=%d cols=%s price_samples=%s current_price_samples=%s",
        len(out),
        symbol_count,
        list(out.columns),
        _sample_values(out, "price", 5),
        _sample_values(out, "current_price", 5),
    )

    if {"symbol", "datetime"}.issubset(out.columns):
        out = (
            out.sort_values("datetime")
            .drop_duplicates(["symbol", "datetime"], keep="last")
            .reset_index(drop=True)
        )

    if len(out) > MAX_RESTORE_ROWS:
        out = out.tail(MAX_RESTORE_ROWS).reset_index(drop=True)

    latest = out["datetime"].max() if "datetime" in out.columns else None
    logger.info(
        "📡 pushDB normalize done rows=%d time_src=%s latest=%s session=%s-%s elapsed=%.3fs",
        len(out),
        time_src,
        latest,
        SESSION_OPEN_TIME,
        SESSION_CLOSE_TIME,
        (dt.datetime.now() - norm_started).total_seconds(),
    )

    return out.reset_index(drop=True)


def bootstrap_push(push_dir: str):
    boot_started = dt.datetime.now()
    logger.info("📡 push bootstrap start session_open=%s session_close=%s", SESSION_OPEN_TIME, SESSION_CLOSE_TIME)

    today_str = dt.datetime.now().strftime("%Y%m%d")
    db_path = os.path.join(push_dir, f"push{today_str}.db")

    if not os.path.exists(db_path):
        empty = pd.DataFrame()
        global_data.push_df = empty
        try:
            global_data.set_push_df(empty)
        except Exception:
            pass
        logger.warning("⚠ pushDB not found → push_df empty path=%s", db_path)
        return

    raw = _load_push_db(db_path)

    if raw.empty:
        empty = pd.DataFrame()
        global_data.push_df = empty
        try:
            global_data.set_push_df(empty)
        except Exception:
            pass
        logger.warning("⚠ pushDB empty or failed path=%s", db_path)
        return

    df = _normalize_push_df_for_summary(raw)

    if df.empty:
        empty = pd.DataFrame()
        global_data.push_df = empty
        try:
            global_data.set_push_df(empty)
        except Exception:
            pass
        logger.warning("⚠ pushDB normalize resulted empty raw_rows=%d raw_cols=%s path=%s", len(raw), list(raw.columns), db_path)
        return

    global_data.push_df = df
    try:
        global_data.set_push_df(df)
    except Exception:
        pass

    latest = df["datetime"].max() if "datetime" in df.columns else None

    try:
        global_data.push_bootstrap_db_path = db_path
        global_data.push_bootstrap_rows = int(len(df))
        global_data.push_bootstrap_raw_rows = int(len(raw))
        global_data.push_bootstrap_latest_datetime = latest
        global_data.push_bootstrap_lookback_minutes = int(RESTORE_LOOKBACK_MINUTES)
        global_data.push_bootstrap_max_restore_rows = int(MAX_RESTORE_ROWS)
        global_data.push_bootstrap_session_open = str(SESSION_OPEN_TIME)
        global_data.push_bootstrap_session_close = str(SESSION_CLOSE_TIME)
    except Exception:
        pass

    logger.info(
        "📡 push bootstrap complete rows=%d raw_rows=%d latest=%s lookback_min=%d limited=%d session=%s-%s elapsed=%.3fs path=%s",
        len(df),
        len(raw),
        latest,
        RESTORE_LOOKBACK_MINUTES,
        MAX_RESTORE_ROWS,
        SESSION_OPEN_TIME,
        SESSION_CLOSE_TIME,
        (dt.datetime.now() - boot_started).total_seconds(),
        db_path,
    )
