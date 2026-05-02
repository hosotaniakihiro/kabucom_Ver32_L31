# ============================================================
# File   : core/startup/startup_summary_restore.py
# Version: PRODUCTION-STABLE-REV1.0-STARTUP-SUMMARY-RESTORE-MINIMAL-TAIL
# ------------------------------------------------------------
# 【概要】
#   起動時に summary DB / PUSH DB から必要最小限のデータだけを読み込み、
#   作成済み3分足・5分足は再作成せず、未作成分だけ補完して表示する。
#
# 【目的】
#   - 起動直後に latest summary を global_data へ復元
#   - 1分足の大量ロードを避ける
#   - 3分足/5分足の作成済みデータを再作成しない
#   - 5分足75MA等の指標継続は既存5分足 tail を使う
#   - PUSH DB の差分があれば未作成MTFだけ作成
#
# 【重要仕様】
#   - 1分足は 450分読み込まない
#   - 1分足は min(latest_3min, latest_5min) - 10〜15分程度のみ
#   - 3分足/5分足は DB の latest_dt より後だけ作成・保存
#   - 5分足75MAは既存5分足 tail + 新規5分足で計算
#   - 保存対象は new_3min / new_5min のみ
#   - global_data には tail + new を格納
#
# 【呼び出し例】
#   from core.startup.startup_summary_restore import restore_startup_summary_minimal_tail
#
#   restore_startup_summary_minimal_tail(
#       intervals=(1, 3, 5),
#       display=True,
#       save_missing=True,
#       tail_rows=100,
#       one_min_lookback_minutes=15,
#   )
# ============================================================

from __future__ import annotations

import datetime as dt
import importlib
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "PRODUCTION-STABLE-REV1.0-STARTUP-SUMMARY-RESTORE-MINIMAL-TAIL"


# ============================================================
# Constants
# ============================================================

DEFAULT_BASE_DIR = r"\\192.168.0.22\AutoStockBuyAndSell"
DEFAULT_SUMMARY_DIR = rf"{DEFAULT_BASE_DIR}\raw_data\kabu_station\summary"
DEFAULT_PUSH_DIR = rf"{DEFAULT_BASE_DIR}\raw_data\kabu_station\push"

SUMMARY_TABLE_BY_INTERVAL = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
}

DEFAULT_TAIL_ROWS = 100
DEFAULT_1MIN_LOOKBACK_MINUTES = 15

PRICE_COLUMNS = ["open", "high", "low", "close", "current_price", "price"]
VOLUME_COLUMNS = ["volume", "trading_volume", "turnover", "trading_value"]

SCORE_COLUMNS = [
    "score",
    "score_total",
    "final_score",
    "display_score",
    "score_buy",
    "score_sell",
    "slope",
    "slope_atr_scaled",
    "score_slope",
    "mtf",
    "score_mtf",
    "mtf_score",
    "rsi",
    "macd",
    "signal",
]

DISPLAY_COLUMNS = [
    "symbol",
    "symbolname",
    "datetime",
    "close",
    "score",
    "final_score",
    "display_score",
    "score_buy",
    "score_sell",
    "slope",
    "score_slope",
    "mtf",
    "score_mtf",
    "rsi",
    "macd",
    "signal",
]


# ============================================================
# Dataclass
# ============================================================

@dataclass
class RestoreResult:
    ok: bool
    summary_db: Optional[str] = None
    push_db: Optional[str] = None
    latest_1min_dt: Optional[pd.Timestamp] = None
    latest_3min_dt: Optional[pd.Timestamp] = None
    latest_5min_dt: Optional[pd.Timestamp] = None
    one_min_load_from: Optional[pd.Timestamp] = None
    loaded_1min_rows: int = 0
    loaded_push_rows: int = 0
    existing_3min_rows: int = 0
    existing_5min_rows: int = 0
    new_3min_rows: int = 0
    new_5min_rows: int = 0
    saved_3min_rows: int = 0
    saved_5min_rows: int = 0
    message: str = ""


# ============================================================
# Public API
# ============================================================

def restore_startup_summary_minimal_tail(
    *,
    intervals: Iterable[int] = (1, 3, 5),
    display: bool = True,
    save_missing: bool = True,
    tail_rows: int = DEFAULT_TAIL_ROWS,
    one_min_lookback_minutes: int = DEFAULT_1MIN_LOOKBACK_MINUTES,
    summary_db_path: Optional[str] = None,
    push_db_path: Optional[str] = None,
    trade_date: Optional[dt.date | str] = None,
    max_allowed_dt: Optional[pd.Timestamp | dt.datetime | str] = None,
) -> RestoreResult:
    """
    起動時 summary 復元の本体。

    方針:
      - 既存3分/5分足 tail をロード
      - 1分足は MTF latest 近辺だけロード
      - PUSH DB も同じ範囲だけロード
      - 3分/5分足は未作成分だけ生成
      - 既存 tail + 新規足で indicator / scoring を再計算
      - 保存は新規足だけ
      - global_data には tail + 新規をセット
    """

    logger.info("📊 [STARTUP SUMMARY RESTORE] minimal-tail mode start version=%s", VERSION)

    result = RestoreResult(ok=False)

    try:
        intervals = tuple(int(x) for x in intervals)
        trade_day = _normalize_trade_date(trade_date)
        cutoff_dt = _resolve_max_allowed_dt(max_allowed_dt=max_allowed_dt, trade_date=trade_day)

        summary_db = summary_db_path or _resolve_latest_db(
            db_dir=DEFAULT_SUMMARY_DIR,
            prefix="summary",
            trade_date=trade_day,
            required_table="stock_summary_1min",
        )
        push_db = push_db_path or _resolve_latest_db(
            db_dir=DEFAULT_PUSH_DIR,
            prefix="push",
            trade_date=trade_day,
            required_table=None,
        )

        result.summary_db = summary_db
        result.push_db = push_db

        if not summary_db or not Path(summary_db).exists():
            msg = f"summary DB not found: {summary_db}"
            logger.warning("[STARTUP SUMMARY RESTORE] %s", msg)
            result.message = msg
            return result

        if not push_db or not Path(push_db).exists():
            logger.warning("[STARTUP SUMMARY RESTORE] push DB not found: %s", push_db)

        logger.info("📂 [STARTUP SUMMARY RESTORE] summary_db=%s", summary_db)
        logger.info("📡 [STARTUP SUMMARY RESTORE] push_db=%s", push_db)

        # ----------------------------------------------------
        # 1. 既存3分/5分足 tail を読む
        # ----------------------------------------------------
        existing_3 = pd.DataFrame()
        existing_5 = pd.DataFrame()

        if 3 in intervals:
            existing_3 = load_existing_summary_tail(
                summary_db,
                interval=3,
                tail_rows=tail_rows,
                max_allowed_dt=cutoff_dt,
            )
            result.existing_3min_rows = len(existing_3)
            result.latest_3min_dt = _latest_dt(existing_3)

        if 5 in intervals:
            existing_5 = load_existing_summary_tail(
                summary_db,
                interval=5,
                tail_rows=tail_rows,
                max_allowed_dt=cutoff_dt,
            )
            result.existing_5min_rows = len(existing_5)
            result.latest_5min_dt = _latest_dt(existing_5)

        logger.info(
            "📂 [STARTUP SUMMARY RESTORE] existing 3min tail rows=%s latest_dt=%s",
            len(existing_3),
            result.latest_3min_dt,
        )
        logger.info(
            "📂 [STARTUP SUMMARY RESTORE] existing 5min tail rows=%s latest_dt=%s",
            len(existing_5),
            result.latest_5min_dt,
        )

        # ----------------------------------------------------
        # 2. 1分足ロード開始時刻を決める
        #    450分は読まない。
        # ----------------------------------------------------
        one_min_load_from = resolve_one_min_load_from(
            latest_3min_dt=result.latest_3min_dt,
            latest_5min_dt=result.latest_5min_dt,
            one_min_lookback_minutes=one_min_lookback_minutes,
            trade_date=trade_day,
        )
        result.one_min_load_from = one_min_load_from

        logger.info(
            "📌 [STARTUP SUMMARY RESTORE] 1min load_from=%s reason=missing_mtf_only lookback=%smin",
            one_min_load_from,
            one_min_lookback_minutes,
        )

        # ----------------------------------------------------
        # 3. 1分足 summary tail を必要最小限だけ読む
        # ----------------------------------------------------
        one_min_df = pd.DataFrame()
        if 1 in intervals or 3 in intervals or 5 in intervals:
            one_min_df = load_1min_tail_from_summary_db(
                summary_db,
                load_from=one_min_load_from,
                max_allowed_dt=cutoff_dt,
            )
            result.loaded_1min_rows = len(one_min_df)
            result.latest_1min_dt = _latest_dt(one_min_df)

        logger.info(
            "📂 [STARTUP SUMMARY RESTORE] 1min summary tail loaded rows=%s latest_dt=%s",
            len(one_min_df),
            result.latest_1min_dt,
        )

        # ----------------------------------------------------
        # 4. PUSH DB も同じ範囲だけ読む
        # ----------------------------------------------------
        push_df = pd.DataFrame()
        if push_db and Path(push_db).exists():
            push_df = load_push_tail_from_push_db(
                push_db,
                load_from=one_min_load_from,
                max_allowed_dt=cutoff_dt,
            )
            result.loaded_push_rows = len(push_df)

        logger.info(
            "📡 [STARTUP SUMMARY RESTORE] push tail loaded rows=%s latest_dt=%s",
            len(push_df),
            _latest_dt(push_df),
        )

        # ----------------------------------------------------
        # 5. 1分足 summary + PUSH を統合
        # ----------------------------------------------------
        merged_1min = merge_1min_and_push_tail(one_min_df, push_df)
        merged_1min = _apply_symbolname_map(merged_1min)
        merged_1min = _sort_dedup_by_symbol_datetime(merged_1min)

        logger.info(
            "🧩 [STARTUP SUMMARY RESTORE] merged 1min rows=%s symbols=%s latest_dt=%s",
            len(merged_1min),
            _nunique_symbol(merged_1min),
            _latest_dt(merged_1min),
        )

        # ----------------------------------------------------
        # 6. 未作成3分/5分足だけ作る
        # ----------------------------------------------------
        new_3 = pd.DataFrame()
        new_5 = pd.DataFrame()

        if 3 in intervals:
            new_3 = build_missing_mtf_only(
                merged_1min,
                interval=3,
                latest_existing_dt=result.latest_3min_dt,
                max_allowed_dt=cutoff_dt,
            )
            result.new_3min_rows = len(new_3)

        if 5 in intervals:
            new_5 = build_missing_mtf_only(
                merged_1min,
                interval=5,
                latest_existing_dt=result.latest_5min_dt,
                max_allowed_dt=cutoff_dt,
            )
            result.new_5min_rows = len(new_5)

        logger.info(
            "🧮 [STARTUP SUMMARY RESTORE] missing 3min built rows=%s from>%s",
            len(new_3),
            result.latest_3min_dt,
        )
        logger.info(
            "🧮 [STARTUP SUMMARY RESTORE] missing 5min built rows=%s from>%s",
            len(new_5),
            result.latest_5min_dt,
        )

        # ----------------------------------------------------
        # 7. existing tail + new で指標・スコア再計算
        #    保存対象は new のみ。
        # ----------------------------------------------------
        combined_3 = pd.DataFrame()
        combined_5 = pd.DataFrame()
        save_3 = pd.DataFrame()
        save_5 = pd.DataFrame()

        if 3 in intervals:
            combined_3 = _combine_existing_and_new(existing_3, new_3)
            combined_3 = apply_indicators_and_scoring_safe(combined_3, interval=3)
            combined_3 = _sort_dedup_by_symbol_datetime(combined_3)
            save_3 = _filter_new_rows_only(combined_3, result.latest_3min_dt)

        if 5 in intervals:
            combined_5 = _combine_existing_and_new(existing_5, new_5)
            combined_5 = apply_indicators_and_scoring_safe(combined_5, interval=5)
            combined_5 = _sort_dedup_by_symbol_datetime(combined_5)
            save_5 = _filter_new_rows_only(combined_5, result.latest_5min_dt)

        logger.info(
            "📈 [STARTUP SUMMARY RESTORE] recalc 3min combined rows=%s save_new_rows=%s",
            len(combined_3),
            len(save_3),
        )
        logger.info(
            "📈 [STARTUP SUMMARY RESTORE] recalc 5min combined rows=%s save_new_rows=%s",
            len(combined_5),
            len(save_5),
        )

        # ----------------------------------------------------
        # 8. 未作成分だけ保存
        # ----------------------------------------------------
        if save_missing:
            if not save_3.empty:
                result.saved_3min_rows = save_missing_summary_only(
                    save_3,
                    interval=3,
                    summary_db_path=summary_db,
                )

            if not save_5.empty:
                result.saved_5min_rows = save_missing_summary_only(
                    save_5,
                    interval=5,
                    summary_db_path=summary_db,
                )

        logger.info(
            "💾 [STARTUP SUMMARY RESTORE] save 3min missing only rows=%s",
            result.saved_3min_rows,
        )
        logger.info(
            "💾 [STARTUP SUMMARY RESTORE] save 5min missing only rows=%s",
            result.saved_5min_rows,
        )

        # ----------------------------------------------------
        # 9. global_data へ反映
        # ----------------------------------------------------
        if 1 in intervals:
            cache_1 = apply_indicators_and_scoring_safe(merged_1min, interval=1)
            cache_1 = _sort_dedup_by_symbol_datetime(cache_1)
            set_global_summary_cache(cache_1, interval=1, source="push")
            logger.info(
                "✅ [STARTUP SUMMARY RESTORE] cache set tf=1 rows=%s latest_dt=%s",
                len(cache_1),
                _latest_dt(cache_1),
            )

        if 3 in intervals:
            set_global_summary_cache(combined_3, interval=3, source="push")
            logger.info(
                "✅ [STARTUP SUMMARY RESTORE] cache set tf=3 rows=%s latest_dt=%s",
                len(combined_3),
                _latest_dt(combined_3),
            )

        if 5 in intervals:
            set_global_summary_cache(combined_5, interval=5, source="push")
            logger.info(
                "✅ [STARTUP SUMMARY RESTORE] cache set tf=5 rows=%s latest_dt=%s",
                len(combined_5),
                _latest_dt(combined_5),
            )

        # ----------------------------------------------------
        # 10. 起動直後表示
        # ----------------------------------------------------
        if display:
            if 1 in intervals:
                display_summary_top10(cache_1 if "cache_1" in locals() else merged_1min, interval=1)
            if 3 in intervals:
                display_summary_top10(combined_3, interval=3)
            if 5 in intervals:
                display_summary_top10(combined_5, interval=5)

        result.ok = True
        result.message = "startup summary restore minimal-tail completed"

        logger.info(
            "✅ [STARTUP SUMMARY RESTORE] minimal-tail mode done "
            "1min_rows=%s push_rows=%s new3=%s new5=%s saved3=%s saved5=%s",
            result.loaded_1min_rows,
            result.loaded_push_rows,
            result.new_3min_rows,
            result.new_5min_rows,
            result.saved_3min_rows,
            result.saved_5min_rows,
        )

        return result

    except Exception as e:
        logger.exception("[STARTUP SUMMARY RESTORE] failed")
        result.ok = False
        result.message = str(e)
        return result


# ============================================================
# DB resolve
# ============================================================

def _normalize_trade_date(trade_date: Optional[dt.date | str]) -> dt.date:
    if trade_date is None:
        return dt.date.today()
    if isinstance(trade_date, dt.date):
        return trade_date
    return pd.to_datetime(str(trade_date)).date()


def _resolve_max_allowed_dt(
    *,
    max_allowed_dt: Optional[pd.Timestamp | dt.datetime | str],
    trade_date: dt.date,
) -> Optional[pd.Timestamp]:
    if max_allowed_dt is not None:
        ts = pd.to_datetime(max_allowed_dt, errors="coerce")
        if pd.notna(ts):
            return _to_naive_ts(ts)

    # 通常の日本株現物の終値足は 15:30 を上限にする。
    # 未来時刻混入対策。
    return pd.Timestamp.combine(trade_date, dt.time(15, 30))


def _resolve_latest_db(
    *,
    db_dir: str,
    prefix: str,
    trade_date: dt.date,
    required_table: Optional[str] = None,
) -> Optional[str]:
    """
    今日DBを優先し、なければ同prefixの最新DBを返す。
    """

    direct = Path(db_dir) / f"{prefix}{trade_date:%Y%m%d}.db"
    if direct.exists():
        if required_table is None or _sqlite_table_exists(str(direct), required_table):
            return str(direct)

    base = Path(db_dir)
    if not base.exists():
        return None

    pattern = re.compile(rf"^{re.escape(prefix)}(\d{{8}})\.db$")
    candidates: list[tuple[str, Path]] = []

    for p in base.glob(f"{prefix}*.db"):
        m = pattern.match(p.name)
        if not m:
            continue
        if required_table is not None and not _sqlite_table_exists(str(p), required_table):
            continue
        candidates.append((m.group(1), p))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return str(candidates[0][1])


def _sqlite_table_exists(db_path: str, table: str) -> bool:
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            cur = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            )
            return cur.fetchone() is not None
    except Exception:
        return False


def _get_table_columns(db_path: str, table: str) -> list[str]:
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
        return [r[1] for r in rows]
    except Exception:
        return []


# ============================================================
# Load summary
# ============================================================

def load_existing_summary_tail(
    summary_db_path: str,
    *,
    interval: int,
    tail_rows: int = DEFAULT_TAIL_ROWS,
    max_allowed_dt: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """
    既存3分/5分足の末尾だけ読む。
    ここでは大量ロードしない。
    """

    table = SUMMARY_TABLE_BY_INTERVAL[int(interval)]
    if not _sqlite_table_exists(summary_db_path, table):
        return pd.DataFrame()

    cols = _get_table_columns(summary_db_path, table)
    dt_expr = _datetime_expr_for_table(cols)

    where = []
    params: list[Any] = []

    if max_allowed_dt is not None:
        where.append(f"{dt_expr} <= ?")
        params.append(_fmt_dt(max_allowed_dt))

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    # symbolごとのtailを厳密に取るとSQLが重くなるため、
    # 起動高速化優先で全体latestから tail_rows * 500 程度を読む。
    # その後 pandas 側で symbolごとtailに整形する。
    limit = max(int(tail_rows) * 500, int(tail_rows))

    sql = f"""
        SELECT *
        FROM "{table}"
        {where_sql}
        ORDER BY {dt_expr} DESC
        LIMIT {limit}
    """

    df = _read_sql(summary_db_path, sql, params=params)
    df = normalize_summary_df(df, interval=interval)

    if df.empty:
        return df

    df = df.sort_values(["symbol", "datetime"])
    df = df.groupby("symbol", group_keys=False).tail(int(tail_rows))
    df = _sort_dedup_by_symbol_datetime(df)

    return df


def load_1min_tail_from_summary_db(
    summary_db_path: str,
    *,
    load_from: Optional[pd.Timestamp],
    max_allowed_dt: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """
    stock_summary_1min から load_from 以降だけ読む。
    450分ロードしない。
    """

    table = SUMMARY_TABLE_BY_INTERVAL[1]
    if not _sqlite_table_exists(summary_db_path, table):
        return pd.DataFrame()

    cols = _get_table_columns(summary_db_path, table)
    dt_expr = _datetime_expr_for_table(cols)

    where = []
    params: list[Any] = []

    if load_from is not None:
        where.append(f"{dt_expr} >= ?")
        params.append(_fmt_dt(load_from))

    if max_allowed_dt is not None:
        where.append(f"{dt_expr} <= ?")
        params.append(_fmt_dt(max_allowed_dt))

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    sql = f"""
        SELECT *
        FROM "{table}"
        {where_sql}
        ORDER BY {dt_expr} ASC
    """

    df = _read_sql(summary_db_path, sql, params=params)
    df = normalize_summary_df(df, interval=1)
    return _sort_dedup_by_symbol_datetime(df)


def normalize_summary_df(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    df = _drop_duplicate_columns(df)

    if "symbol" in df.columns:
        df["symbol"] = df["symbol"].astype(str).str.strip()

    if "datetime" not in df.columns:
        if "date" in df.columns and "time" in df.columns:
            df["datetime"] = pd.to_datetime(
                df["date"].astype(str) + " " + df["time"].astype(str),
                errors="coerce",
            )
        elif "date" in df.columns and "time_range" in df.columns:
            df["datetime"] = pd.to_datetime(
                df["date"].astype(str) + " " + df["time_range"].astype(str).str[:5],
                errors="coerce",
            )
        elif "inserted_at" in df.columns:
            df["datetime"] = pd.to_datetime(df["inserted_at"], errors="coerce")
        elif "updated_at" in df.columns:
            df["datetime"] = pd.to_datetime(df["updated_at"], errors="coerce")

    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce").map(_to_naive_ts)
        df = df[df["datetime"].notna()].copy()

    for col in PRICE_COLUMNS + VOLUME_COLUMNS + SCORE_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if "interval" not in df.columns:
        df["interval"] = int(interval)

    return df


# ============================================================
# Load PUSH
# ============================================================

def load_push_tail_from_push_db(
    push_db_path: str,
    *,
    load_from: Optional[pd.Timestamp],
    max_allowed_dt: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """
    PUSH DB から load_from 以降だけ読む。
    テーブル名は環境差を吸収する。
    """

    table = _resolve_push_table(push_db_path)
    if not table:
        logger.warning("[STARTUP SUMMARY RESTORE] push table not found db=%s", push_db_path)
        return pd.DataFrame()

    cols = _get_table_columns(push_db_path, table)
    dt_col = _resolve_push_datetime_column(cols)

    if not dt_col:
        logger.warning(
            "[STARTUP SUMMARY RESTORE] push datetime column not found table=%s cols=%s",
            table,
            cols,
        )
        return pd.DataFrame()

    where = []
    params: list[Any] = []

    if load_from is not None:
        where.append(f'"{dt_col}" >= ?')
        params.append(_fmt_dt(load_from))

    if max_allowed_dt is not None:
        where.append(f'"{dt_col}" <= ?')
        params.append(_fmt_dt(max_allowed_dt))

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""

    sql = f"""
        SELECT *
        FROM "{table}"
        {where_sql}
        ORDER BY "{dt_col}" ASC
    """

    df = _read_sql(push_db_path, sql, params=params)
    return normalize_push_df(df, dt_col=dt_col)


def _resolve_push_table(push_db_path: str) -> Optional[str]:
    preferred = [
        "stream_data",
        "push_stream",
        "push_ticks",
        "push_data",
        "ticks",
    ]

    try:
        with sqlite3.connect(push_db_path, timeout=10) as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        tables = [r[0] for r in rows]
    except Exception:
        return None

    for t in preferred:
        if t in tables:
            return t

    for t in tables:
        low = t.lower()
        if "push" in low or "stream" in low or "tick" in low:
            return t

    return tables[0] if tables else None


def _resolve_push_datetime_column(cols: list[str]) -> Optional[str]:
    candidates = [
        "datetime",
        "timestamp",
        "time",
        "received_at",
        "inserted_at",
        "created_at",
        "CurrentPriceTime",
        "current_price_time",
    ]

    for c in candidates:
        if c in cols:
            return c

    for c in cols:
        low = c.lower()
        if "time" in low or "date" in low:
            return c

    return None


def normalize_push_df(df: pd.DataFrame, *, dt_col: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    df = _drop_duplicate_columns(df)

    rename_map = {
        "Symbol": "symbol",
        "symbol_code": "symbol",
        "CurrentPrice": "close",
        "current_price": "close",
        "Price": "close",
        "price": "close",
        "TradingVolume": "volume",
        "trading_volume": "volume",
        "Volume": "volume",
        "volume": "volume",
        "CurrentPriceTime": "datetime",
        "current_price_time": "datetime",
        dt_col: "datetime",
    }

    for src, dst in rename_map.items():
        if src in df.columns and src != dst:
            df = df.rename(columns={src: dst})

    if "symbol" not in df.columns:
        return pd.DataFrame()

    if "datetime" not in df.columns and dt_col in df.columns:
        df["datetime"] = df[dt_col]

    if "datetime" not in df.columns:
        return pd.DataFrame()

    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce").map(_to_naive_ts)
    df = df[df["symbol"].ne("") & df["datetime"].notna()].copy()

    if "close" not in df.columns:
        for c in ["current_price", "price", "CurrentPrice", "Price"]:
            if c in df.columns:
                df["close"] = df[c]
                break

    if "close" in df.columns:
        df["close"] = pd.to_numeric(df["close"], errors="coerce")

    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce")

    # PUSHはtickなので1分OHLCへ丸める
    df["datetime"] = df["datetime"].dt.floor("min")

    agg = {}
    if "close" in df.columns:
        agg["close"] = "last"
        agg["open"] = ("close", "first")
        agg["high"] = ("close", "max")
        agg["low"] = ("close", "min")
    if "volume" in df.columns:
        agg["volume"] = ("volume", "last")

    if "close" not in df.columns:
        return pd.DataFrame()

    grouped = (
        df.sort_values(["symbol", "datetime"])
        .groupby(["symbol", "datetime"], as_index=False)
        .agg(
            open=("close", "first"),
            high=("close", "max"),
            low=("close", "min"),
            close=("close", "last"),
            volume=("volume", "last") if "volume" in df.columns else ("close", "size"),
        )
    )

    return _sort_dedup_by_symbol_datetime(grouped)


# ============================================================
# Merge / build MTF
# ============================================================

def resolve_one_min_load_from(
    *,
    latest_3min_dt: Optional[pd.Timestamp],
    latest_5min_dt: Optional[pd.Timestamp],
    one_min_lookback_minutes: int,
    trade_date: dt.date,
) -> pd.Timestamp:
    candidates = [x for x in [latest_3min_dt, latest_5min_dt] if x is not None and pd.notna(x)]

    if candidates:
        base = min(candidates)
        return _to_naive_ts(base) - pd.Timedelta(minutes=int(one_min_lookback_minutes))

    # 3分/5分がまだ空の場合のみ、当日寄り付き付近から。
    return pd.Timestamp.combine(trade_date, dt.time(9, 0))


def merge_1min_and_push_tail(one_min_df: pd.DataFrame, push_df: pd.DataFrame) -> pd.DataFrame:
    """
    summary 1min と PUSH由来1minを統合。
    同じ symbol/datetime は PUSH 側を優先する。
    """

    frames = []

    if one_min_df is not None and not one_min_df.empty:
        a = one_min_df.copy()
        a["_priority"] = 1
        frames.append(a)

    if push_df is not None and not push_df.empty:
        b = push_df.copy()
        b["_priority"] = 2
        frames.append(b)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True, sort=False)
    df = normalize_summary_df(df, interval=1)

    if df.empty:
        return df

    df = df.sort_values(["symbol", "datetime", "_priority"])
    df = df.drop_duplicates(["symbol", "datetime"], keep="last")
    df = df.drop(columns=["_priority"], errors="ignore")

    # OHLC欠損をcloseで補完
    if "close" in df.columns:
        for c in ["open", "high", "low"]:
            if c not in df.columns:
                df[c] = df["close"]
            else:
                df[c] = df[c].fillna(df["close"])

    return _sort_dedup_by_symbol_datetime(df)


def build_missing_mtf_only(
    one_min_df: pd.DataFrame,
    *,
    interval: int,
    latest_existing_dt: Optional[pd.Timestamp],
    max_allowed_dt: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    """
    1分足から未作成の3分足/5分足だけ作成する。
    latest_existing_dt 以下は返さない。
    """

    if one_min_df is None or one_min_df.empty:
        return pd.DataFrame()

    interval = int(interval)
    if interval not in (3, 5):
        raise ValueError(f"unsupported interval: {interval}")

    df = one_min_df.copy()
    df = normalize_summary_df(df, interval=1)
    if df.empty:
        return df

    required = {"symbol", "datetime", "open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        logger.warning("[STARTUP SUMMARY RESTORE] cannot resample missing columns=%s", sorted(missing))
        return pd.DataFrame()

    if max_allowed_dt is not None:
        df = df[df["datetime"] <= _to_naive_ts(max_allowed_dt)].copy()

    if latest_existing_dt is not None and pd.notna(latest_existing_dt):
        # 直前数分はロードしているが、作成対象は latest より後だけ。
        pass

    rule = f"{interval}min"

    out_frames = []
    for symbol, g in df.groupby("symbol"):
        g = g.sort_values("datetime").set_index("datetime")

        # label/right にせず、00分起点の自然な 3min/5min 足を作る。
        # 例: 15:26-15:30 の5分足は 15:30 ラベルにしたい場合は
        # closed='right', label='right' が必要。
        # ここでは既存summaryとの整合を優先し、右ラベルにする。
        res = g.resample(rule, label="right", closed="right").agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "last") if "volume" in g.columns else ("close", "count"),
        )

        res = res.dropna(subset=["open", "high", "low", "close"], how="any")
        if res.empty:
            continue

        res = res.reset_index()
        res["symbol"] = symbol

        if "symbolname" in g.columns:
            names = g["symbolname"].dropna()
            if not names.empty:
                res["symbolname"] = str(names.iloc[-1])

        out_frames.append(res)

    if not out_frames:
        return pd.DataFrame()

    out = pd.concat(out_frames, ignore_index=True, sort=False)
    out["interval"] = interval

    if latest_existing_dt is not None and pd.notna(latest_existing_dt):
        latest_existing_dt = _to_naive_ts(latest_existing_dt)
        out = out[out["datetime"] > latest_existing_dt].copy()

    if max_allowed_dt is not None:
        out = out[out["datetime"] <= _to_naive_ts(max_allowed_dt)].copy()

    out = _apply_symbolname_map(out)
    out = _sort_dedup_by_symbol_datetime(out)

    return out


def _combine_existing_and_new(existing: pd.DataFrame, new: pd.DataFrame) -> pd.DataFrame:
    frames = []
    if existing is not None and not existing.empty:
        frames.append(existing)
    if new is not None and not new.empty:
        frames.append(new)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True, sort=False)
    df = _apply_symbolname_map(df)
    return _sort_dedup_by_symbol_datetime(df)


def _filter_new_rows_only(df: pd.DataFrame, latest_existing_dt: Optional[pd.Timestamp]) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    if latest_existing_dt is None or pd.isna(latest_existing_dt):
        return df.copy()

    latest_existing_dt = _to_naive_ts(latest_existing_dt)
    return df[df["datetime"] > latest_existing_dt].copy()


# ============================================================
# Indicators / scoring
# ============================================================

def apply_indicators_and_scoring_safe(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    """
    既存プロジェクトの indicator / scoring を可能な範囲で呼ぶ。
    失敗しても起動を止めない。
    """

    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    out = normalize_summary_df(out, interval=interval)
    out = _sort_dedup_by_symbol_datetime(out)

    interval_text = f"{int(interval)}min"

    # 1. indicator
    indicator_candidates: list[tuple[str, str]] = [
        ("trading.summary.indicators.indicator_processor", "apply_indicator"),
        ("trading.summary.indicators.indicator_processor", "apply_indicator_strict"),
        ("trading.summary.indicators.indicator_calculator", "add_all_indicators"),
    ]

    for module_name, func_name in indicator_candidates:
        fn = _resolve_callable(module_name, func_name)
        if not fn:
            continue

        try:
            try:
                out2 = fn(out, interval=interval_text)
            except TypeError:
                out2 = fn(out)
            if isinstance(out2, pd.DataFrame) and not out2.empty:
                out = out2
                logger.info(
                    "[STARTUP SUMMARY RESTORE] indicator applied %s.%s interval=%s rows=%s",
                    module_name,
                    func_name,
                    interval_text,
                    len(out),
                )
                break
        except Exception:
            logger.exception(
                "[STARTUP SUMMARY RESTORE] indicator failed %s.%s interval=%s",
                module_name,
                func_name,
                interval_text,
            )

    # 2. scoring
    scoring_candidates: list[tuple[str, str]] = [
        ("trading.scoring.core.scoring_pipeline", "run_scoring_pipeline"),
        ("trading.scoring.core.scoring_pipeline", "scoring_pipeline"),
        ("trading.scoring.core.score_calculator", "calculate_score"),
        ("trading.scoring.core.scoring_core", "calculate_scores"),
    ]

    for module_name, func_name in scoring_candidates:
        fn = _resolve_callable(module_name, func_name)
        if not fn:
            continue

        try:
            try:
                out2 = fn(out, interval=interval_text)
            except TypeError:
                try:
                    out2 = fn(out, interval=int(interval))
                except TypeError:
                    out2 = fn(out)

            if isinstance(out2, pd.DataFrame) and not out2.empty:
                out = out2
                logger.info(
                    "[STARTUP SUMMARY RESTORE] scoring applied %s.%s interval=%s rows=%s",
                    module_name,
                    func_name,
                    interval_text,
                    len(out),
                )
                break
        except Exception:
            logger.exception(
                "[STARTUP SUMMARY RESTORE] scoring failed %s.%s interval=%s",
                module_name,
                func_name,
                interval_text,
            )

    out = _ensure_score_columns(out)
    out = _sort_dedup_by_symbol_datetime(out)

    return out


def _ensure_score_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    for c in SCORE_COLUMNS:
        if c not in df.columns:
            df[c] = pd.NA

    if "score" in df.columns:
        if "score_total" in df.columns:
            df["score_total"] = df["score_total"].fillna(df["score"])
        if "final_score" in df.columns:
            df["final_score"] = df["final_score"].fillna(df["score"])
        if "display_score" in df.columns:
            df["display_score"] = df["display_score"].fillna(df["final_score"]).fillna(df["score"])

    return df


# ============================================================
# Save
# ============================================================

def save_missing_summary_only(
    df: pd.DataFrame,
    *,
    interval: int,
    summary_db_path: str,
) -> int:
    """
    未作成分だけ保存。
    既存の save_summary_bulk / execute_upsert が使えればそれを優先。
    ダメなら sqlite の INSERT OR REPLACE fallback。
    """

    if df is None or df.empty:
        return 0

    df = df.copy()
    df = normalize_summary_df(df, interval=interval)
    df = _sort_dedup_by_symbol_datetime(df)

    if df.empty:
        return 0

    table = SUMMARY_TABLE_BY_INTERVAL[int(interval)]

    # 既存 saver 優先
    saver_candidates: list[tuple[str, str]] = [
        ("trading.summary.persistence.summary_saver_bulk", "save_summary_bulk"),
        ("trading.summary.persistence.core.upsert_engine", "execute_upsert"),
    ]

    for module_name, func_name in saver_candidates:
        fn = _resolve_callable(module_name, func_name)
        if not fn:
            continue

        try:
            logger.info(
                "[STARTUP SUMMARY RESTORE] save via %s.%s interval=%s rows=%s",
                module_name,
                func_name,
                interval,
                len(df),
            )

            # 複数シグネチャに対応
            try:
                ret = fn(df, interval=interval)
            except TypeError:
                try:
                    ret = fn(df=df, interval=interval)
                except TypeError:
                    try:
                        ret = fn(table_name=table, df=df, interval=interval)
                    except TypeError:
                        ret = fn(table, df)

            if isinstance(ret, int):
                return ret
            return len(df)

        except Exception:
            logger.exception(
                "[STARTUP SUMMARY RESTORE] save via %s.%s failed -> fallback",
                module_name,
                func_name,
            )

    return _sqlite_insert_or_replace_summary(
        df,
        summary_db_path=summary_db_path,
        table=table,
        interval=interval,
    )


def _sqlite_insert_or_replace_summary(
    df: pd.DataFrame,
    *,
    summary_db_path: str,
    table: str,
    interval: int,
) -> int:
    if df is None or df.empty:
        return 0

    cols = _get_table_columns(summary_db_path, table)
    if not cols:
        logger.warning("[STARTUP SUMMARY RESTORE] table columns not found table=%s", table)
        return 0

    out = df.copy()

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce").dt.strftime("%Y-%m-%d %H:%M:%S")
        if "date" in cols and "date" not in out.columns:
            out["date"] = out["datetime"].str[:10]
        if "time" in cols and "time" not in out.columns:
            out["time"] = out["datetime"].str[11:19]
        if "time_range" in cols and "time_range" not in out.columns:
            out["time_range"] = out["datetime"].str[11:16]

    if "interval" in cols and "interval" not in out.columns:
        out["interval"] = int(interval)

    use_cols = [c for c in cols if c in out.columns]
    if not use_cols:
        return 0

    out = out[use_cols].copy()
    out = out.where(pd.notna(out), None)

    placeholders = ", ".join(["?"] * len(use_cols))
    col_sql = ", ".join([f'"{c}"' for c in use_cols])
    sql = f'INSERT OR REPLACE INTO "{table}" ({col_sql}) VALUES ({placeholders})'

    rows = [tuple(x) for x in out.itertuples(index=False, name=None)]

    try:
        with sqlite3.connect(summary_db_path, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.executemany(sql, rows)
            conn.commit()
        return len(rows)
    except Exception:
        logger.exception("[STARTUP SUMMARY RESTORE] sqlite fallback save failed table=%s", table)
        return 0


# ============================================================
# global_data
# ============================================================

def set_global_summary_cache(df: pd.DataFrame, *, interval: int, source: str = "push") -> bool:
    """
    global_data の実装差を吸収して summary cache をセットする。
    """

    gd = _resolve_global_data()
    if gd is None:
        logger.warning("[STARTUP SUMMARY RESTORE] global_data not found")
        return False

    if df is None:
        df = pd.DataFrame()

    df = df.copy()
    df = _sort_dedup_by_symbol_datetime(df)

    tf = int(interval)

    method_candidates = [
        "set_merged_summary",
        "set_summary_history",
        "set_summary_df",
        "update_merged_summary",
        "update_summary_history",
        "set_push_summary",
    ]

    for name in method_candidates:
        fn = getattr(gd, name, None)
        if not callable(fn):
            continue

        try:
            try:
                fn(tf, df, source=source)
            except TypeError:
                try:
                    fn(interval=tf, df=df, source=source)
                except TypeError:
                    try:
                        fn(df, tf=tf, source=source)
                    except TypeError:
                        fn(tf, df)
            return True
        except Exception:
            logger.exception("[STARTUP SUMMARY RESTORE] global_data.%s failed", name)

    # 最後の手段: 属性に直接入れる
    try:
        for attr in [
            f"summary_{tf}min",
            f"merged_summary_{tf}min",
            f"push_summary_{tf}min",
            f"summary_df_{tf}",
        ]:
            setattr(gd, attr, df)

        if not hasattr(gd, "summary_cache") or getattr(gd, "summary_cache") is None:
            setattr(gd, "summary_cache", {})

        cache = getattr(gd, "summary_cache")
        if isinstance(cache, dict):
            cache[(source, tf)] = df
            cache[tf] = df

        return True
    except Exception:
        logger.exception("[STARTUP SUMMARY RESTORE] global_data direct set failed")
        return False


def _resolve_global_data() -> Any:
    candidates = [
        ("global_state", "global_data"),
        ("core.global_context.context", "global_data"),
    ]

    for module_name, attr in candidates:
        try:
            mod = importlib.import_module(module_name)
            gd = getattr(mod, attr, None)
            if gd is not None:
                return gd
        except Exception:
            continue

    return None


def _apply_symbolname_map(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    if "symbol" not in df.columns:
        return df

    out = df.copy()

    try:
        gd = _resolve_global_data()
        mp = None

        if gd is not None:
            for attr in ["symbol_name_map", "symbolname_map", "symbol_map"]:
                v = getattr(gd, attr, None)
                if isinstance(v, dict) and v:
                    mp = v
                    break

        if mp:
            if "symbolname" not in out.columns:
                out["symbolname"] = out["symbol"].astype(str).map(mp)
            else:
                mapped = out["symbol"].astype(str).map(mp)
                out["symbolname"] = out["symbolname"].fillna(mapped)
                out.loc[out["symbolname"].astype(str).str.strip().eq(""), "symbolname"] = mapped

    except Exception:
        logger.debug("[STARTUP SUMMARY RESTORE] symbolname map failed", exc_info=True)

    return out


# ============================================================
# Display
# ============================================================

def display_summary_top10(df: pd.DataFrame, *, interval: int) -> None:
    """
    起動直後の簡易TOP10表示。
    既存display関数があれば優先し、なければfallback表示。
    """

    if df is None or df.empty:
        logger.warning("========== 📊 SUMMARY TOP10 (%smin) EMPTY ==========", interval)
        return

    display_candidates: list[tuple[str, str]] = [
        ("scheduler_jobs.summary.display", "display_summary_top10"),
        ("trading.summary.display", "display_summary_top10"),
        ("trading.summary.display.summary_display", "display_summary_top10"),
        ("trading.summary.display.summary_display", "print_summary_top10"),
    ]

    for module_name, func_name in display_candidates:
        fn = _resolve_callable(module_name, func_name)
        if not fn:
            continue

        try:
            try:
                fn(df, interval=interval)
            except TypeError:
                try:
                    fn(df=df, interval=interval)
                except TypeError:
                    fn(df)
            return
        except Exception:
            logger.exception(
                "[STARTUP SUMMARY RESTORE] display via %s.%s failed -> fallback",
                module_name,
                func_name,
            )

    _fallback_display_summary_top10(df, interval=interval)


def _fallback_display_summary_top10(df: pd.DataFrame, *, interval: int) -> None:
    out = df.copy()
    out = normalize_summary_df(out, interval=interval)
    out = _apply_symbolname_map(out)

    if out.empty:
        logger.warning("========== 📊 SUMMARY TOP10 (%smin) EMPTY ==========", interval)
        return

    score_col = _resolve_score_col(out)

    latest = _latest_dt(out)
    if latest is not None:
        latest_df = out[out["datetime"] == latest].copy()
    else:
        latest_df = out.copy()

    if score_col not in latest_df.columns:
        latest_df[score_col] = 0.0

    buy = latest_df.sort_values(score_col, ascending=False).head(10)
    sell_score_col = "score_sell" if "score_sell" in latest_df.columns else score_col
    sell = latest_df.sort_values(sell_score_col, ascending=False).head(10)

    logger.info("")
    logger.info("========== 📊 SUMMARY TOP10 (%smin) latest=%s ==========", interval, latest)
    logger.info("---------- BUY TOP10 ----------")
    logger.info("\n%s", _format_display_df(buy))
    logger.info("---------- SELL TOP10 ----------")
    logger.info("\n%s", _format_display_df(sell))


def _resolve_score_col(df: pd.DataFrame) -> str:
    for c in ["display_score", "final_score", "score_total", "score"]:
        if c in df.columns:
            return c
    return "score"


def _format_display_df(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "(empty)"

    cols = [c for c in DISPLAY_COLUMNS if c in df.columns]
    out = df[cols].copy()

    for c in out.columns:
        if c in ["open", "high", "low", "close", "current_price", "price"]:
            out[c] = pd.to_numeric(out[c], errors="coerce").round(1)
        elif c not in ["symbol", "symbolname", "datetime"]:
            if pd.api.types.is_numeric_dtype(out[c]):
                out[c] = pd.to_numeric(out[c], errors="coerce").round(2)

    return out.to_string(index=False)


# ============================================================
# SQL helper
# ============================================================

def _read_sql(db_path: str, sql: str, *, params: Optional[list[Any]] = None) -> pd.DataFrame:
    try:
        with sqlite3.connect(db_path, timeout=30) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            return pd.read_sql_query(sql, conn, params=params or [])
    except Exception:
        logger.exception("[STARTUP SUMMARY RESTORE] read_sql failed db=%s sql=%s", db_path, sql)
        return pd.DataFrame()


def _datetime_expr_for_table(cols: list[str]) -> str:
    if "datetime" in cols:
        return '"datetime"'
    if "date" in cols and "time" in cols:
        return '("date" || " " || "time")'
    if "date" in cols and "time_range" in cols:
        return '("date" || " " || substr("time_range", 1, 5) || ":00")'
    if "inserted_at" in cols:
        return '"inserted_at"'
    if "updated_at" in cols:
        return '"updated_at"'
    return '"datetime"'


# ============================================================
# Generic helper
# ============================================================

def _resolve_callable(module_name: str, func_name: str) -> Optional[Callable]:
    try:
        mod = importlib.import_module(module_name)
        fn = getattr(mod, func_name, None)
        return fn if callable(fn) else None
    except Exception:
        return None


def _drop_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df
    return df.loc[:, ~df.columns.duplicated()].copy()


def _to_naive_ts(x: Any) -> pd.Timestamp:
    ts = pd.to_datetime(x, errors="coerce")
    if pd.isna(ts):
        return pd.NaT
    if getattr(ts, "tzinfo", None) is not None:
        try:
            ts = ts.tz_convert(None)
        except Exception:
            ts = ts.tz_localize(None)
    return pd.Timestamp(ts).tz_localize(None) if getattr(pd.Timestamp(ts), "tzinfo", None) else pd.Timestamp(ts)


def _fmt_dt(x: Any) -> str:
    ts = _to_naive_ts(x)
    if pd.isna(ts):
        return ""
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _latest_dt(df: pd.DataFrame) -> Optional[pd.Timestamp]:
    if df is None or df.empty or "datetime" not in df.columns:
        return None
    s = pd.to_datetime(df["datetime"], errors="coerce").dropna()
    if s.empty:
        return None
    return _to_naive_ts(s.max())


def _nunique_symbol(df: pd.DataFrame) -> int:
    if df is None or df.empty or "symbol" not in df.columns:
        return 0
    return int(df["symbol"].astype(str).nunique())


def _sort_dedup_by_symbol_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    out = _drop_duplicate_columns(out)

    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str).str.strip()

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce").map(_to_naive_ts)
        out = out[out["datetime"].notna()].copy()

    if "symbol" in out.columns and "datetime" in out.columns:
        out = out.sort_values(["symbol", "datetime"])
        out = out.drop_duplicates(["symbol", "datetime"], keep="last")
    elif "datetime" in out.columns:
        out = out.sort_values("datetime")

    return out.reset_index(drop=True)