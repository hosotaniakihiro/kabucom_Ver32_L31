# ============================================================
# File   : trading/summary/mtf/daily_mtf_loader.py
# Version: PRODUCTION-STABLE-DAILY-MTF-LOADER-REV1.0
# ------------------------------------------------------------
# Purpose:
#   - stock_analysis.db の stock_analysis_latest から
#     日足 MA-MTF 用データを読み込む
#
# Source DB:
#   \\192.168.0.22\kabu\stock_data\daily_db\stock_analysis.db
#
# Source table:
#   stock_analysis_latest
#
# Output columns:
#   symbol
#   symbolname
#   market
#   daily_date
#   daily_close
#   daily_ma5
#   daily_ma25
#   daily_ma75
#   daily_ma5_slope
#   daily_ma25_slope
#   daily_ma75_slope
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any, Dict

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


DEFAULT_DAILY_MTF_DB_PATH = (
    r"\\192.168.0.22\kabu\stock_data\daily_db\stock_analysis.db"
)

DEFAULT_DAILY_MTF_TABLE = "stock_analysis_latest"


_REQUIRED_COLUMNS = [
    "stock_code",
    "stock_name",
    "market",
    "date",
    "close",
    "MA_5",
    "MA_25",
    "MA_75",
    "MA_5_Slope",
    "MA_25_Slope",
    "MA_75_Slope",
]


def _normalize_symbol_series(s: pd.Series) -> pd.Series:
    return (
        s.astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
        .str.replace(r"\.T$", "", regex=True)
    )


def _safe_num(s: pd.Series, default: float = 0.0) -> pd.Series:
    return (
        pd.to_numeric(s, errors="coerce")
        .replace([np.inf, -np.inf], np.nan)
        .fillna(default)
    )


def _read_table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    cur = conn.cursor()
    cur.execute(f'PRAGMA table_info("{table_name}")')
    return {str(row[1]) for row in cur.fetchall()}


# ------------------------------------------------------------
# SQLite 接続まわり
#
# stock_analysis.db は NAS 上にあり、夜間バッチ
# (scripts/night_yahoo_daily_*) が PRAGMA journal_mode=WAL で書き込む。
# ここは純粋な読み取りなので、
#   - read-only (mode=ro) で開く      : 書き込みロックを取らない / hot journal 復旧を試みない
#   - 明示的に close する             : `with sqlite3.connect(...)` は close しないため接続が残る
#   - busy_timeout + 短い retry       : 一時的な locked は待って読み直す
# という方針にする。trading/daily/daily_signal_loader.py と同じ read-only 方式。
# ------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _is_locked_error(exc: BaseException) -> bool:
    msg = str(exc or "").lower()
    return ("database is locked" in msg) or ("database table is locked" in msg) or ("database is busy" in msg)


def _connect_readonly(db_path: str) -> sqlite3.Connection:
    """読み取り専用で開く。呼び出し側が必ず close すること。"""
    timeout_sec = max(0.5, _env_float("DAILY_MTF_SQLITE_TIMEOUT_SEC", 5.0))
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=timeout_sec)
    try:
        busy_ms = max(0, _env_int("DAILY_MTF_SQLITE_BUSY_TIMEOUT_MS", int(timeout_sec * 1000)))
        conn.execute(f"PRAGMA busy_timeout={busy_ms}")
    except Exception:
        logger.debug("[DAILY MTF LOADER] busy_timeout set failed", exc_info=True)
    return conn


# 連続失敗時に NAS へ読みに行き続けないためのクールダウン。
# 呼び出し元 (daily_mtf_summary / daily_runtime) は
# 空DFをキャッシュしない作りなので、これが無いと保存のたびに再接続してしまう。
_LAST_FAILURE_TS: float = 0.0
_LAST_FAILURE_REASON: str = ""


def _failure_cooldown_active() -> bool:
    # 呼び出し元 (daily_mtf_summary) の成功時キャッシュTTLと同じ 300 秒。
    # ロックが続く間、保存のたびに NAS へ行って数十秒止まるのを防ぐ。
    cooldown = _env_float("DAILY_MTF_LOAD_FAILURE_COOLDOWN_SEC", 300.0)
    if cooldown <= 0 or _LAST_FAILURE_TS <= 0:
        return False
    remain = cooldown - (time.monotonic() - _LAST_FAILURE_TS)
    if remain <= 0:
        return False
    logger.warning(
        "[DAILY MTF LOADER] skip read during failure cooldown remain=%.1fs last_reason=%s "
        "(DAILY_MTF_LOAD_FAILURE_COOLDOWN_SEC=0 で無効化できます)",
        remain,
        _LAST_FAILURE_REASON,
    )
    return True


def _mark_failure(reason: str) -> None:
    global _LAST_FAILURE_TS, _LAST_FAILURE_REASON
    _LAST_FAILURE_TS = time.monotonic()
    _LAST_FAILURE_REASON = str(reason)[:200]


def _clear_failure() -> None:
    global _LAST_FAILURE_TS, _LAST_FAILURE_REASON
    _LAST_FAILURE_TS = 0.0
    _LAST_FAILURE_REASON = ""


def _read_latest_raw(db_path: str, table_name: str) -> pd.DataFrame:
    """read-only 接続で latest テーブルを読む。locked のときだけ短く retry する。

    retry を使い切っても読めなければ例外をそのまま送出する (握り潰さない)。
    """
    # NAS(SMB)上でロックされている場合、busy_timeout に関係なく
    # 1試行あたり実測で 20 秒前後ブロックする。retry を増やすと
    # summary 保存側を長時間止めてしまうため、retry は最小限にし、
    # 継続的なロックは下のクールダウンで受け止める。
    retries = max(0, _env_int("DAILY_MTF_SQLITE_LOCK_RETRIES", 1))
    sleep_base = max(0.05, _env_float("DAILY_MTF_SQLITE_LOCK_SLEEP_BASE_SEC", 0.2))

    attempt = 0
    while True:
        conn = None
        try:
            conn = _connect_readonly(db_path)
            existing_cols = _read_table_columns(conn, table_name)
            missing = [c for c in _REQUIRED_COLUMNS if c not in existing_cols]
            if missing:
                logger.warning(
                    "[DAILY MTF LOADER] required columns missing table=%s missing=%s existing=%s",
                    table_name,
                    missing,
                    sorted(existing_cols),
                )
                return pd.DataFrame()

            sql = f"""
                SELECT
                    stock_code,
                    stock_name,
                    market,
                    date,
                    close,
                    MA_5,
                    MA_25,
                    MA_75,
                    MA_5_Slope,
                    MA_25_Slope,
                    MA_75_Slope
                FROM {table_name}
            """
            return pd.read_sql_query(sql, conn)

        except Exception as e:
            if not _is_locked_error(e) or attempt >= retries:
                raise
            attempt += 1
            wait = sleep_base * (2 ** (attempt - 1))
            logger.warning(
                "[DAILY MTF LOADER] locked -> retry %s/%s in %.2fs db=%s table=%s err=%s",
                attempt, retries, wait, db_path, table_name, e,
            )
            time.sleep(wait)

        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    logger.debug("[DAILY MTF LOADER] close failed", exc_info=True)


def load_daily_mtf_latest_df(
    *,
    db_path: str = DEFAULT_DAILY_MTF_DB_PATH,
    table_name: str = DEFAULT_DAILY_MTF_TABLE,
    min_close: float = 1.0,
) -> pd.DataFrame:
    """
    日足MA-MTF用の latest データを読み込む。

    Returns
    -------
    pd.DataFrame
        columns:
          symbol
          symbolname
          market
          daily_date
          daily_close
          daily_ma5
          daily_ma25
          daily_ma75
          daily_ma5_slope
          daily_ma25_slope
          daily_ma75_slope
    """
    if not db_path:
        logger.warning("[DAILY MTF LOADER] db_path empty")
        return pd.DataFrame()

    if not os.path.exists(db_path):
        logger.warning("[DAILY MTF LOADER] db not found: %s", db_path)
        return pd.DataFrame()

    if _failure_cooldown_active():
        return pd.DataFrame()

    try:
        df = _read_latest_raw(db_path, table_name)
    except Exception as e:
        _mark_failure(f"{type(e).__name__}: {e}")
        logger.exception(
            "[DAILY MTF LOADER] failed to read db=%s table=%s",
            db_path,
            table_name,
        )
        return pd.DataFrame()

    _clear_failure()

    if df.empty:
        logger.warning("[DAILY MTF LOADER] empty db=%s table=%s", db_path, table_name)
        return pd.DataFrame()

    out = pd.DataFrame()
    out["symbol"] = _normalize_symbol_series(df["stock_code"])
    out["symbolname"] = df["stock_name"].astype(str).fillna("")
    out["market"] = df["market"].astype(str).fillna("")
    out["daily_date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y-%m-%d")

    out["daily_close"] = _safe_num(df["close"])
    out["daily_ma5"] = _safe_num(df["MA_5"])
    out["daily_ma25"] = _safe_num(df["MA_25"])
    out["daily_ma75"] = _safe_num(df["MA_75"])

    out["daily_ma5_slope"] = _safe_num(df["MA_5_Slope"])
    out["daily_ma25_slope"] = _safe_num(df["MA_25_Slope"])
    out["daily_ma75_slope"] = _safe_num(df["MA_75_Slope"])

    out = out[out["symbol"].ne("")]
    out = out[out["daily_close"] >= float(min_close)]

    out = (
        out.sort_values(["symbol", "daily_date"])
        .drop_duplicates(subset=["symbol"], keep="last")
        .reset_index(drop=True)
    )

    latest_date = out["daily_date"].max() if "daily_date" in out.columns and not out.empty else None

    logger.info(
        "[DAILY MTF LOADER] loaded rows=%s symbols=%s latest_date=%s db=%s table=%s",
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns else 0,
        latest_date,
        db_path,
        table_name,
    )

    _warn_if_stale(latest_date, db_path)

    return out


def _stale_days_threshold() -> int:
    try:
        return max(1, int(float(os.getenv("DAILY_MTF_STALE_WARN_DAYS", "4"))))
    except Exception:
        return 4


def _warn_if_stale(latest_date: Any, db_path: str) -> None:
    """日足が古いまま判定に使われていないか監視する。

    2026-08-18 に、日足DB が 2026-07-17 を最後に更新されておらず
    (夜間バッチが TLS で失敗し、かつタスク登録も無かった)、
    日足MTF が 1 か月前の値で判定していたことが分かった。
    それでもログは INFO の "latest_date=2026-07-16" だけで、
    古いことが分かる出力が無かったため気付けなかった。

    休日を挟むと数日空くのは正常なので、既定は 4 日超で WARNING。
    DAILY_MTF_STALE_WARN_DAYS で調整できる。
    """
    try:
        if latest_date is None:
            logger.warning("[DAILY MTF LOADER] latest_date unknown db=%s", db_path)
            return

        d = pd.to_datetime(latest_date, errors="coerce")
        if pd.isna(d):
            logger.warning("[DAILY MTF LOADER] latest_date unparsable value=%r db=%s", latest_date, db_path)
            return

        age_days = (pd.Timestamp.now().normalize() - d.normalize()).days
        limit = _stale_days_threshold()
        if age_days > limit:
            logger.warning(
                "[DAILY MTF STALE] 日足が %s 日前のまま判定に使われています "
                "latest_date=%s age_days=%s limit=%s db=%s "
                "hint=夜間バッチ (NightYahooDailyFullSummary) が動いているか確認してください",
                age_days, d.date(), age_days, limit, db_path,
            )
    except Exception:
        logger.debug("[DAILY MTF LOADER] stale check failed", exc_info=True)


def load_daily_mtf_latest_map(
    *,
    db_path: str = DEFAULT_DAILY_MTF_DB_PATH,
    table_name: str = DEFAULT_DAILY_MTF_TABLE,
) -> Dict[str, dict]:
    """
    symbol -> daily data dict の形で返す補助関数。
    """
    df = load_daily_mtf_latest_df(db_path=db_path, table_name=table_name)
    if df.empty:
        return {}
    return df.set_index("symbol").to_dict(orient="index")
