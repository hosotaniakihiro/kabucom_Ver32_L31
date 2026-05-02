# ============================================================
# File   : trading/yahoo/monitor/intraday_monitor.py
# Version: Ver2.1-PRO-YAHOO-INTRADAY-MONITOR-NAS-FIX-L14
# ------------------------------------------------------------
# ✔ Yahoo intraday DB監視
# ✔ 正式DBパスAPI(get_intraday_db_path)使用
# ✔ 差分のみ抽出
# ✔ OHLCV完全取得
# ✔ 日付切替時のlast_seenリセット
# ✔ 再起動耐性
# ✔ DB未作成耐性
# ✔ WAL/ロック耐性
# ✔ global_data安全更新
# ✔ scheduler絶対停止しない
# ✔ NAS対応
# ✔ 重複完全排除
# ============================================================

from __future__ import annotations

import time
import logging
import datetime as dt

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

from global_state import global_data
from trading.yahoo.storage.yahoo_1min_bootstrap import get_intraday_db_path

logger = logging.getLogger(__name__)

POLL_INTERVAL = 1.0
EMPTY_SLEEP = 0.5


def _safe_set_global_attr(name: str, value) -> None:
    try:
        setattr(global_data, name, value)
    except Exception:
        logger.debug("[INTRADAY MONITOR] failed setattr name=%s", name, exc_info=True)


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if df is None or df.empty:
            return pd.DataFrame()

        if not isinstance(df, pd.DataFrame):
            df = pd.DataFrame(df)

        df = df.copy()
        df.columns = [str(c).strip().lower() for c in df.columns]

        try:
            df = df.loc[:, ~df.columns.duplicated()]
        except Exception:
            pass

        required = {"symbol", "datetime", "close"}
        if not required.issubset(df.columns):
            logger.warning(
                "[INTRADAY MONITOR] missing columns=%s",
                sorted(required - set(df.columns)),
            )
            return pd.DataFrame()

        df["symbol"] = df["symbol"].astype(str).str.strip()
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")

        for col in ["open", "high", "low", "close", "volume"]:
            if col not in df.columns:
                if col == "volume":
                    df[col] = 0.0
                else:
                    df[col] = pd.NA

            df[col] = pd.to_numeric(df[col], errors="coerce")

        # OHLC不足時はcloseで最低限補完
        for col in ["open", "high", "low"]:
            df[col] = df[col].fillna(df["close"])

        df["volume"] = df["volume"].fillna(0.0)

        df = df.dropna(subset=["symbol", "datetime", "close"])
        if df.empty:
            return pd.DataFrame()

        try:
            if getattr(df["datetime"].dt, "tz", None) is not None:
                df["datetime"] = df["datetime"].dt.tz_convert(None)
        except Exception:
            pass

        df = (
            df.drop_duplicates(subset=["symbol", "datetime"])
              .sort_values(["datetime", "symbol"])
              .reset_index(drop=True)
        )

        return df

    except Exception:
        logger.exception("[INTRADAY MONITOR] normalize failed")
        return pd.DataFrame()


def _table_exists(engine, table_name: str) -> bool:
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT name
                    FROM sqlite_master
                    WHERE type='table' AND name=:table_name
                    """
                ),
                {"table_name": table_name},
            ).fetchone()
            return bool(row)
    except SQLAlchemyError:
        return False
    except Exception:
        logger.exception("[INTRADAY MONITOR] table_exists failed table=%s", table_name)
        return False


def _read_delta(
    engine,
    last_seen: dt.datetime | None,
) -> tuple[pd.DataFrame, dt.datetime | None]:
    try:
        if last_seen is None:
            sql = """
                SELECT symbol, datetime, open, high, low, close, volume
                FROM yahoo_1min
            """
            df = pd.read_sql(sql, engine)
        else:
            sql = text(
                """
                SELECT symbol, datetime, open, high, low, close, volume
                FROM yahoo_1min
                WHERE datetime > :last_seen
                """
            )
            df = pd.read_sql(
                sql,
                engine,
                params={"last_seen": last_seen.strftime("%Y-%m-%d %H:%M:%S")},
            )

    except Exception:
        logger.exception("[INTRADAY MONITOR] read_sql failed")
        return pd.DataFrame(), last_seen

    df = _normalize_df(df)
    if df.empty:
        return pd.DataFrame(), last_seen

    latest_dt = df["datetime"].max()
    return df, latest_dt


def intraday_monitor_loop():
    """
    Yahoo intraday DB を監視し、
    新規datetimeのみ global_data.intraday_delta に格納する
    """
    logger.info("🟢 intraday_monitor_loop started")

    last_seen: dt.datetime | None = None
    current_date: dt.date | None = None

    while True:
        engine = None

        try:
            today = dt.date.today()

            # 日付切替時は監視状態をリセット
            if current_date != today:
                logger.info(
                    "[INTRADAY MONITOR] trading date changed %s -> %s, reset last_seen",
                    current_date,
                    today,
                )
                current_date = today
                last_seen = None

            db_path = get_intraday_db_path(today)

            engine = create_engine(
                f"sqlite:///{db_path}",
                connect_args={
                    "check_same_thread": False,
                    "timeout": 5,
                },
                pool_pre_ping=True,
            )

            if not _table_exists(engine, "yahoo_1min"):
                time.sleep(EMPTY_SLEEP)
                continue

            delta, latest_dt = _read_delta(engine, last_seen)

            if delta.empty or latest_dt is None:
                time.sleep(POLL_INTERVAL)
                continue

            _safe_set_global_attr("intraday_delta", delta)
            _safe_set_global_attr("last_yahoo_intraday_dt", latest_dt)
            _safe_set_global_attr("last_yahoo_intraday_rows", len(delta))
            _safe_set_global_attr("last_yahoo_intraday_symbols", int(delta["symbol"].nunique()))

            last_seen = latest_dt

            logger.info(
                "[INTRADAY MONITOR] delta rows=%d symbols=%d latest=%s",
                len(delta),
                delta["symbol"].nunique(),
                latest_dt,
            )

        except Exception:
            logger.exception("❌ intraday_monitor_loop unexpected error")

        finally:
            try:
                if engine is not None:
                    engine.dispose()
            except Exception:
                pass

        time.sleep(POLL_INTERVAL)