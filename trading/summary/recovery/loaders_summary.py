# ============================================================
# File   : trading/summary/recovery/loaders_summary.py
# Ver    : PRODUCTION-STABLE-REV1.2-LOADERS-SUMMARY
#          -BOOT-HISTORY-BARS-EXPANDED
#          -SYMBOL-CHUNKED-TAIL-LOAD
#          -TAIL-PER-SYMBOL-STRICT
#          -DATETIME-FALLBACK-DATE-TIMERANGE
#          -HISTORY-QUALITY-LOG
# ------------------------------------------------------------
# 【概要】
#   summary DB loaders
#
# 【主な機能】
#   ✔ summary DB loader 群
#   ✔ latest checkpoint 読み込み
#   ✔ datetime 範囲読み込み
#   ✔ symbol ごとの recent tail 読み込み
#   ✔ latest snapshot 読み込み
#   ✔ large-symbol restore safe
#   ✔ SQLite expression tree too large 回避
#
# 【REV1.2 修正内容】
#   ✔ 起動直後のテクニカル指標計算用に履歴本数を増加
#       1min: 180本
#       3min: 120本
#       5min: 90本
#
#   ✔ load_recent_summary_tail_per_symbol を本番強化
#       - 全体 LIMIT ではなく symbol ごとの最新N本
#       - ROW_NUMBER() OVER(PARTITION BY symbol ORDER BY datetime DESC)
#       - symbol chunk 対応
#       - date / time_range しかないテーブルでも datetime fallback
#       - table name を SQLite quote して安全化
#       - 読み込み後に Python 側でも symbol ごとの tail を再保証
#
#   ✔ 起動時診断ログ強化
#       - min / median / max
#       - required_hint 未満の不足銘柄数
#
# 【目的】
#   システム再起動直後に、global_data 側へ
#   RSI / MACD / MA75 / slope / MTF 計算に必要な履歴を
#   できるだけ多く復元する。
# ============================================================

from __future__ import annotations

import logging
from typing import Iterable, Optional, Sequence

import pandas as pd
from sqlalchemy import text

from .helpers import normalize_datetime_columns
from .loaders_common import (
    apply_max_allowed_dt_filter,
    apply_target_date_filter,
    coerce_date_set,
    log_df_date_breakdown,
    normalize_symbols,
    sanitize_checkpoint_dt,
    sanitize_query_dt,
)

logger = logging.getLogger(__name__)

DEFAULT_SYMBOL_CHUNK_SIZE = 300

# 起動時・通常復元時の標準履歴本数
# 1分足は MA75 / MACD / RSI / slope / MTF の安定化のため 180 本を標準にする。
DEFAULT_TAIL_BARS_BY_INTERVAL = {
    1: 180,
    3: 120,
    5: 90,
}

# 本数不足警告の目安
HISTORY_REQUIRED_HINT_BY_INTERVAL = {
    1: 75,
    3: 50,
    5: 40,
}


def _quote_identifier(name: str) -> str:
    """
    SQLite identifier quote.
    table name / column name 用。
    """
    s = str(name).replace('"', '""')
    return f'"{s}"'


def resolve_summary_table_name_from_model(model) -> str:
    try:
        table = getattr(model, "__table__", None)
        if table is not None:
            return str(table.name)
    except Exception:
        pass

    name = getattr(model, "__name__", "")
    mapping = {
        "StockSummary1Min": "stock_summary_1min",
        "StockSummary3Min": "stock_summary_3min",
        "StockSummary5Min": "stock_summary_5min",
    }
    if name in mapping:
        return mapping[name]

    raise RuntimeError(f"cannot resolve table name from model={model}")


def _get_summary_model(interval: int):
    from database.models import StockSummary1Min, StockSummary3Min, StockSummary5Min

    model_map = {
        1: StockSummary1Min,
        3: StockSummary3Min,
        5: StockSummary5Min,
    }
    return model_map[int(interval)]


def _get_summary_engine():
    from database.session import get_summary_engine

    return get_summary_engine()


def _chunk_list(values: list[str], chunk_size: int) -> list[list[str]]:
    try:
        chunk_size = max(int(chunk_size), 1)
    except Exception:
        chunk_size = DEFAULT_SYMBOL_CHUNK_SIZE

    if not values:
        return []

    return [values[i : i + chunk_size] for i in range(0, len(values), chunk_size)]


def _get_table_columns(conn, table_name: str) -> list[str]:
    try:
        rows = conn.execute(
            text(f"PRAGMA table_info({_quote_identifier(table_name)})")
        ).fetchall()
        return [str(r[1]) for r in rows]
    except Exception:
        logger.exception(
            "[summary.recovery.loaders_summary] PRAGMA table_info failed table=%s",
            table_name,
        )
        return []


def _build_datetime_expr(columns: Sequence[str]) -> str:
    """
    summary table の datetime 判定式を返す。

    基本は datetime。
    ただし旧テーブルや3分/5分で date/time_range のみの場合もあるため fallback する。
    """
    cols = set(str(c) for c in columns)

    if "datetime" in cols:
        return "datetime"

    if "date" in cols and "time" in cols:
        return "datetime(date || ' ' || time)"

    if "date" in cols and "start_time" in cols:
        return "datetime(date || ' ' || start_time)"

    if "date" in cols and "time_range" in cols:
        return "datetime(date || ' ' || substr(time_range, 1, 5))"

    return "rowid"


def _format_dt_param(value) -> Optional[str]:
    ts = sanitize_query_dt(value, label="loaders_summary.dt_param")
    if ts is None or pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _history_required_hint(interval: int, bars_per_symbol: int) -> int:
    try:
        interval = int(interval)
    except Exception:
        interval = 1

    hint = HISTORY_REQUIRED_HINT_BY_INTERVAL.get(interval, 50)
    return min(int(bars_per_symbol), int(hint))


def _log_tail_quality(
    df: pd.DataFrame,
    *,
    interval: int,
    bars_per_symbol: int,
    label: str,
) -> None:
    try:
        if df is None or df.empty or "symbol" not in df.columns:
            logger.warning(
                "[summary.recovery.loaders_summary] %s interval=%s history empty",
                label,
                interval,
            )
            return

        counts = df.groupby("symbol").size()
        if counts.empty:
            logger.warning(
                "[summary.recovery.loaders_summary] %s interval=%s history count empty",
                label,
                interval,
            )
            return

        required_hint = _history_required_hint(interval, bars_per_symbol)
        shortage = int((counts < required_hint).sum())

        logger.info(
            "[summary.recovery.loaders_summary] %s interval=%s history_quality "
            "rows=%d symbols=%d bars_per_symbol=%d min=%d median=%d max=%d "
            "shortage_lt_%d=%d",
            label,
            interval,
            len(df),
            int(counts.shape[0]),
            int(bars_per_symbol),
            int(counts.min()),
            int(counts.median()),
            int(counts.max()),
            int(required_hint),
            shortage,
        )

        if shortage > 0:
            logger.warning(
                "[summary.recovery.loaders_summary] %s interval=%s history shortage detected "
                "shortage_symbols=%d/%d required_hint=%d median=%d "
                "=> boot indicators may be unstable for shortage symbols",
                label,
                interval,
                shortage,
                int(counts.shape[0]),
                int(required_hint),
                int(counts.median()),
            )

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_summary] history quality log failed interval=%s label=%s",
            interval,
            label,
        )


def read_sqlalchemy_model_to_df(model) -> pd.DataFrame:
    try:
        engine = _get_summary_engine()
        table_name = resolve_summary_table_name_from_model(model)
        q_table = _quote_identifier(table_name)

        with engine.begin() as conn:
            df = pd.read_sql(text(f"SELECT * FROM {q_table}"), conn)

        out = normalize_datetime_columns(df)
        logger.info(
            "[summary.recovery.loaders_summary] read_sqlalchemy_model_to_df table=%s rows=%d",
            table_name,
            len(out),
        )
        return out

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_summary] failed read model=%s",
            getattr(model, "__name__", str(model)),
        )
        return pd.DataFrame()


def load_last_summary_datetime(
    interval: int,
    *,
    target_dates: Optional[Iterable] = None,
    anchor_day=None,
    max_allowed_dt: Optional[pd.Timestamp] = None,
) -> Optional[pd.Timestamp]:
    try:
        engine = _get_summary_engine()
        model = _get_summary_model(int(interval))
        table_name = resolve_summary_table_name_from_model(model)
        q_table = _quote_identifier(table_name)

        allowed_dates = coerce_date_set(target_dates)
        max_allowed_dt = sanitize_query_dt(
            max_allowed_dt,
            label=f"load_last_summary_datetime.max_allowed_dt[{interval}]",
        )

        wheres: list[str] = []
        params: dict[str, object] = {}

        with engine.begin() as conn:
            columns = _get_table_columns(conn, table_name)
            dt_expr = _build_datetime_expr(columns)

            if allowed_dates:
                date_conditions = []
                for idx, d in enumerate(sorted(allowed_dates)):
                    key = f"d{idx}"
                    if "date" in columns:
                        date_conditions.append(f"date = :{key}")
                    else:
                        date_conditions.append(f"date({dt_expr}) = :{key}")
                    params[key] = d
                wheres.append("(" + " OR ".join(date_conditions) + ")")

            if max_allowed_dt is not None and pd.notna(max_allowed_dt):
                wheres.append(f"{dt_expr} <= :max_allowed_dt")
                params["max_allowed_dt"] = max_allowed_dt.strftime("%Y-%m-%d %H:%M:%S")

            where_sql = ""
            if wheres:
                where_sql = " WHERE " + " AND ".join(wheres)

            sql = f"SELECT MAX({dt_expr}) AS last_dt FROM {q_table}{where_sql}"
            df = pd.read_sql(text(sql), conn, params=params)

        if df.empty or "last_dt" not in df.columns:
            logger.info(
                "[summary.recovery.loaders_summary] load_last_summary_datetime "
                "interval=%s last_dt=None empty table=%s target_dates=%s anchor_day=%s max_allowed_dt=%s",
                interval,
                table_name,
                sorted(allowed_dates),
                anchor_day,
                max_allowed_dt,
            )
            return None

        raw_last_dt = pd.to_datetime(df.loc[0, "last_dt"], errors="coerce")
        if pd.isna(raw_last_dt):
            logger.info(
                "[summary.recovery.loaders_summary] load_last_summary_datetime "
                "interval=%s last_dt=None parse_failed table=%s target_dates=%s anchor_day=%s max_allowed_dt=%s",
                interval,
                table_name,
                sorted(allowed_dates),
                anchor_day,
                max_allowed_dt,
            )
            return None

        last_dt = sanitize_checkpoint_dt(
            raw_last_dt,
            label="load_last_summary_datetime",
            interval=interval,
        )

        logger.info(
            "[summary.recovery.loaders_summary] load_last_summary_datetime "
            "interval=%s table=%s raw_last_dt=%s sanitized_last_dt=%s "
            "target_dates=%s anchor_day=%s max_allowed_dt=%s",
            interval,
            table_name,
            raw_last_dt,
            last_dt,
            sorted(allowed_dates),
            anchor_day,
            max_allowed_dt,
        )
        return last_dt

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_summary] load last summary datetime failed "
            "interval=%s target_dates=%s anchor_day=%s max_allowed_dt=%s",
            interval,
            list(target_dates) if target_dates is not None else None,
            anchor_day,
            max_allowed_dt,
        )
        return None


def load_summary_df_from_datetime(
    interval: int,
    start_dt: Optional[pd.Timestamp],
) -> pd.DataFrame:
    try:
        engine = _get_summary_engine()
        model = _get_summary_model(int(interval))
        table_name = resolve_summary_table_name_from_model(model)
        q_table = _quote_identifier(table_name)

        start_dt = sanitize_query_dt(
            start_dt,
            label=f"load_summary_df_from_datetime[{interval}]",
        )

        with engine.begin() as conn:
            columns = _get_table_columns(conn, table_name)
            dt_expr = _build_datetime_expr(columns)

            if start_dt is None or pd.isna(start_dt):
                sql = f"""
                    SELECT *
                    FROM {q_table}
                    ORDER BY {dt_expr}
                """
                params = {}
            else:
                sql = f"""
                    SELECT *
                    FROM {q_table}
                    WHERE {dt_expr} >= :start_dt
                    ORDER BY {dt_expr}
                """
                params = {"start_dt": start_dt.strftime("%Y-%m-%d %H:%M:%S")}

            df = pd.read_sql(text(sql), conn, params=params)

        out = normalize_datetime_columns(df, interval=interval)

        logger.info(
            "[summary.recovery.loaders_summary] load_summary_df_from_datetime "
            "interval=%s start_dt=%s rows=%d",
            interval,
            start_dt,
            len(out),
        )
        return out

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_summary] load summary from datetime failed "
            "interval=%s start_dt=%s",
            interval,
            start_dt,
        )
        return pd.DataFrame()


def load_summary_df_between(
    interval: int,
    start_dt: pd.Timestamp | None,
    end_dt: pd.Timestamp | None,
    *,
    target_dates: Optional[Iterable] = None,
    anchor_day=None,
    max_allowed_dt: Optional[pd.Timestamp] = None,
) -> pd.DataFrame:
    try:
        engine = _get_summary_engine()
        model = _get_summary_model(int(interval))
        table_name = resolve_summary_table_name_from_model(model)
        q_table = _quote_identifier(table_name)

        start_dt = sanitize_query_dt(
            start_dt,
            label=f"load_summary_df_between.start[{interval}]",
        )
        end_dt = sanitize_query_dt(
            end_dt,
            label=f"load_summary_df_between.end[{interval}]",
        )
        max_allowed_dt = sanitize_query_dt(
            max_allowed_dt,
            label=f"load_summary_df_between.max_allowed_dt[{interval}]",
        )

        if (
            end_dt is not None
            and pd.notna(end_dt)
            and max_allowed_dt is not None
            and pd.notna(max_allowed_dt)
        ):
            effective_end_dt = min(end_dt, max_allowed_dt)
        else:
            effective_end_dt = end_dt if end_dt is not None and pd.notna(end_dt) else max_allowed_dt

        allowed_dates = coerce_date_set(target_dates)

        with engine.begin() as conn:
            columns = _get_table_columns(conn, table_name)
            dt_expr = _build_datetime_expr(columns)

            wheres: list[str] = []
            params: dict[str, object] = {}

            if start_dt is not None and not pd.isna(start_dt):
                wheres.append(f"{dt_expr} >= :start_dt")
                params["start_dt"] = start_dt.strftime("%Y-%m-%d %H:%M:%S")

            if effective_end_dt is not None and not pd.isna(effective_end_dt):
                wheres.append(f"{dt_expr} <= :end_dt")
                params["end_dt"] = effective_end_dt.strftime("%Y-%m-%d %H:%M:%S")

            if allowed_dates:
                date_conditions = []
                for idx, d in enumerate(sorted(allowed_dates)):
                    key = f"d{idx}"
                    if "date" in columns:
                        date_conditions.append(f"date = :{key}")
                    else:
                        date_conditions.append(f"date({dt_expr}) = :{key}")
                    params[key] = d
                wheres.append("(" + " OR ".join(date_conditions) + ")")

            where_sql = ""
            if wheres:
                where_sql = " WHERE " + " AND ".join(wheres)

            sql = f"""
                SELECT *
                FROM {q_table}
                {where_sql}
                ORDER BY {dt_expr}
            """

            df = pd.read_sql(text(sql), conn, params=params)

        out = normalize_datetime_columns(df, interval=interval)
        out = apply_target_date_filter(
            out,
            datetime_col="datetime",
            target_dates=target_dates,
            label=f"load_summary_df_between[{interval}]",
        )
        out = apply_max_allowed_dt_filter(
            out,
            datetime_col="datetime",
            max_allowed_dt=max_allowed_dt,
            label=f"load_summary_df_between[{interval}]",
        )

        logger.info(
            "[summary.recovery.loaders_summary] load_summary_df_between "
            "interval=%s start_dt=%s end_dt=%s effective_end_dt=%s rows=%d "
            "target_dates=%s anchor_day=%s max_allowed_dt=%s",
            interval,
            start_dt,
            end_dt,
            effective_end_dt,
            len(out),
            sorted(allowed_dates),
            anchor_day,
            max_allowed_dt,
        )
        log_df_date_breakdown(out, label=f"load_summary_df_between[{interval}]")
        return out

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_summary] load_summary_df_between failed "
            "interval=%s start_dt=%s end_dt=%s target_dates=%s anchor_day=%s max_allowed_dt=%s",
            interval,
            start_dt,
            end_dt,
            list(target_dates) if target_dates is not None else None,
            anchor_day,
            max_allowed_dt,
        )
        return pd.DataFrame()


def load_recent_summary_tail_per_symbol(
    interval: int,
    *,
    bars_per_symbol: int,
    end_dt=None,
    start_dt=None,
    target_dates: Optional[Iterable] = None,
    anchor_day=None,
    max_allowed_dt: Optional[pd.Timestamp] = None,
    symbols: Optional[Iterable] = None,
    symbol_chunk_size: int = DEFAULT_SYMBOL_CHUNK_SIZE,
) -> pd.DataFrame:
    """
    summary DB から symbol ごとの最新 N 本を読む。

    重要:
      - 全体 LIMIT ではない
      - symbol ごとの ROW_NUMBER() で tail を取得
      - 起動直後の indicator 計算用履歴として使う
    """
    try:
        engine = _get_summary_engine()
        model = _get_summary_model(int(interval))
        table_name = resolve_summary_table_name_from_model(model)
        q_table = _quote_identifier(table_name)

        bars_per_symbol = max(_safe_int(bars_per_symbol, 1), 1)
        symbol_chunk_size = max(_safe_int(symbol_chunk_size, DEFAULT_SYMBOL_CHUNK_SIZE), 1)

        start_dt = sanitize_query_dt(
            start_dt,
            label=f"load_recent_summary_tail_per_symbol.start[{interval}]",
        )
        end_dt = sanitize_query_dt(
            end_dt,
            label=f"load_recent_summary_tail_per_symbol.end[{interval}]",
        )
        max_allowed_dt = sanitize_query_dt(
            max_allowed_dt,
            label=f"load_recent_summary_tail_per_symbol.max_allowed_dt[{interval}]",
        )

        if end_dt is not None and max_allowed_dt is not None:
            effective_end_dt = min(end_dt, max_allowed_dt)
        else:
            effective_end_dt = end_dt if end_dt is not None else max_allowed_dt

        allowed_dates = coerce_date_set(target_dates)
        symbol_list = normalize_symbols(symbols)

        frames: list[pd.DataFrame] = []
        executed_chunks = 0
        table_columns: list[str] = []

        with engine.begin() as conn:
            table_columns = _get_table_columns(conn, table_name)
            if not table_columns:
                logger.warning(
                    "[summary.recovery.loaders_summary] load_recent_summary_tail_per_symbol "
                    "table has no columns or missing table interval=%s table=%s",
                    interval,
                    table_name,
                )
                return pd.DataFrame()

            dt_expr = _build_datetime_expr(table_columns)

            base_where_parts = [
                "symbol IS NOT NULL",
                "TRIM(symbol) <> ''",
            ]
            base_params: dict[str, object] = {
                "bars_per_symbol": bars_per_symbol,
            }

            # datetime列がある場合は NULL を除外。
            # fallback式の場合も式が NULL にならないよう可能な範囲で条件を付ける。
            if "datetime" in table_columns:
                base_where_parts.append("datetime IS NOT NULL")
            elif "date" in table_columns:
                base_where_parts.append("date IS NOT NULL")

            if start_dt is not None and not pd.isna(start_dt):
                base_where_parts.append(f"{dt_expr} >= :start_dt")
                base_params["start_dt"] = start_dt.strftime("%Y-%m-%d %H:%M:%S")

            if effective_end_dt is not None and not pd.isna(effective_end_dt):
                base_where_parts.append(f"{dt_expr} <= :end_dt")
                base_params["end_dt"] = effective_end_dt.strftime("%Y-%m-%d %H:%M:%S")

            if allowed_dates:
                date_conditions = []
                for idx, d in enumerate(sorted(allowed_dates)):
                    key = f"d{idx}"
                    if "date" in table_columns:
                        date_conditions.append(f"date = :{key}")
                    else:
                        date_conditions.append(f"date({dt_expr}) = :{key}")
                    base_params[key] = d
                base_where_parts.append("(" + " OR ".join(date_conditions) + ")")

            def _run_one_chunk(chunk_symbols: Optional[list[str]]) -> pd.DataFrame:
                where_parts = list(base_where_parts)
                params = dict(base_params)

                if chunk_symbols:
                    placeholders = []
                    for idx, sym in enumerate(chunk_symbols):
                        key = f"s{idx}"
                        placeholders.append(f":{key}")
                        params[key] = sym
                    where_parts.append(f"symbol IN ({', '.join(placeholders)})")

                # rowid DESC を入れて、同一 symbol/datetime の重複時にも最新行を優先
                # SQLite の rowid は CTE をまたぐと参照できない場合があるため、
                # base CTE で __rid として明示的に持たせる。
                # ただし WITHOUT ROWID table や view 相当で rowid が無い場合もあるため、
                # rowid 依存SQLが失敗したら datetime のみで再実行する。

                sql = f"""
                WITH base AS (
                    SELECT
                        rowid AS __rid,
                        *
                    FROM {q_table}
                    WHERE {" AND ".join(where_parts)}
                ),
                ranked AS (
                    SELECT
                        *,
                        ROW_NUMBER() OVER (
                            PARTITION BY symbol
                            ORDER BY {dt_expr} DESC, __rid DESC
                        ) AS rn
                    FROM base
                )
                SELECT *
                FROM ranked
                WHERE rn <= :bars_per_symbol
                ORDER BY symbol ASC, {dt_expr} ASC
                """

                try:
                    return pd.read_sql(text(sql), conn, params=params)
                except Exception as e:
                    msg = str(e)
                    if "no such column: rowid" not in msg and "no such column: __rid" not in msg:
                        raise

                    logger.warning(
                        "[summary.recovery.loaders_summary] rowid unavailable -> retry without rowid "
                        "interval=%s table=%s",
                        interval,
                        table_name,
                    )

                    sql_no_rowid = f"""
                    WITH base AS (
                        SELECT *
                        FROM {q_table}
                        WHERE {" AND ".join(where_parts)}
                    ),
                    ranked AS (
                        SELECT
                            *,
                            ROW_NUMBER() OVER (
                                PARTITION BY symbol
                                ORDER BY {dt_expr} DESC
                            ) AS rn
                        FROM base
                    )
                    SELECT *
                    FROM ranked
                    WHERE rn <= :bars_per_symbol
                    ORDER BY symbol ASC, {dt_expr} ASC
                    """

                    return pd.read_sql(text(sql_no_rowid), conn, params=params)
            if symbol_list:
                symbol_chunks = _chunk_list(symbol_list, symbol_chunk_size)
                for chunk in symbol_chunks:
                    executed_chunks += 1
                    try:
                        one = _run_one_chunk(chunk)
                        if one is not None and not one.empty:
                            frames.append(one)
                    except Exception:
                        logger.exception(
                            "[summary.recovery.loaders_summary] tail chunk load failed "
                            "interval=%s chunk_no=%s chunk_size=%s bars_per_symbol=%s",
                            interval,
                            executed_chunks,
                            len(chunk),
                            bars_per_symbol,
                        )
            else:
                executed_chunks = 1
                df_all = _run_one_chunk(None)
                if df_all is not None and not df_all.empty:
                    frames.append(df_all)

        if not frames:
            logger.info(
                "[summary.recovery.loaders_summary] load_recent_summary_tail_per_symbol empty "
                "interval=%s bars_per_symbol=%s start_dt=%s end_dt=%s "
                "target_dates=%s anchor_day=%s max_allowed_dt=%s requested_symbols=%d "
                "executed_chunks=%d chunk_size=%d",
                interval,
                bars_per_symbol,
                start_dt,
                effective_end_dt,
                sorted(allowed_dates),
                anchor_day,
                max_allowed_dt,
                len(symbol_list),
                executed_chunks,
                symbol_chunk_size,
            )
            return pd.DataFrame()

        if len(frames) == 1:
            df = frames[0].copy()
        else:
            df = pd.concat(frames, ignore_index=True)

        df = df.drop(columns=["rn"], errors="ignore")

        out = normalize_datetime_columns(df, interval=interval)

        if not out.empty and "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip()
            out = out[out["symbol"].ne("")]

        if not out.empty and {"symbol", "datetime"}.issubset(out.columns):
            before = len(out)
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            out = out.dropna(subset=["datetime"])

            out = (
                out.sort_values(["symbol", "datetime"], kind="stable")
                .drop_duplicates(subset=["symbol", "datetime"], keep="last")
                .reset_index(drop=True)
            )

            # Python 側でも symbol ごとの tail を再保証
            out = (
                out.groupby("symbol", group_keys=False)
                .tail(int(bars_per_symbol))
                .sort_values(["symbol", "datetime"], kind="stable")
                .reset_index(drop=True)
            )

            dropped = before - len(out)
            if dropped > 0:
                logger.info(
                    "[summary.recovery.loaders_summary] load_recent_summary_tail_per_symbol "
                    "dedup/tail applied interval=%s dropped=%d",
                    interval,
                    dropped,
                )

        out = apply_target_date_filter(
            out,
            datetime_col="datetime",
            target_dates=target_dates,
            label=f"load_recent_summary_tail_per_symbol[{interval}]",
        )
        out = apply_max_allowed_dt_filter(
            out,
            datetime_col="datetime",
            max_allowed_dt=max_allowed_dt,
            label=f"load_recent_summary_tail_per_symbol[{interval}]",
        )

        try:
            hist_counts = (
                out.groupby("symbol")["datetime"].count()
                if "symbol" in out.columns and "datetime" in out.columns and not out.empty
                else pd.Series(dtype="int64")
            )
            logger.info(
                "[summary.recovery.loaders_summary] load_recent_summary_tail_per_symbol "
                "interval=%s rows=%d loaded_symbols=%d requested_symbols=%d "
                "bars_per_symbol=%d min=%d median=%d max=%d "
                "start_dt=%s end_dt=%s target_dates=%s anchor_day=%s max_allowed_dt=%s "
                "executed_chunks=%d chunk_size=%d",
                interval,
                len(out),
                int(hist_counts.shape[0]) if len(hist_counts) else 0,
                len(symbol_list),
                bars_per_symbol,
                int(hist_counts.min()) if len(hist_counts) else 0,
                int(hist_counts.median()) if len(hist_counts) else 0,
                int(hist_counts.max()) if len(hist_counts) else 0,
                start_dt,
                effective_end_dt,
                sorted(allowed_dates),
                anchor_day,
                max_allowed_dt,
                executed_chunks,
                symbol_chunk_size,
            )
        except Exception:
            logger.exception(
                "[summary.recovery.loaders_summary] tail-per-symbol stats log failed interval=%s",
                interval,
            )

        _log_tail_quality(
            out,
            interval=int(interval),
            bars_per_symbol=int(bars_per_symbol),
            label="load_recent_summary_tail_per_symbol",
        )

        log_df_date_breakdown(out, label=f"load_recent_summary_tail_per_symbol[{interval}]")
        return out.reset_index(drop=True)

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_summary] load_recent_summary_tail_per_symbol failed "
            "interval=%s bars_per_symbol=%s",
            interval,
            bars_per_symbol,
        )
        return pd.DataFrame()


def load_recent_summary_tail_default(
    interval: int,
    *,
    end_dt=None,
    start_dt=None,
    target_dates: Optional[Iterable] = None,
    anchor_day=None,
    max_allowed_dt: Optional[pd.Timestamp] = None,
    symbols: Optional[Iterable] = None,
    symbol_chunk_size: int = DEFAULT_SYMBOL_CHUNK_SIZE,
) -> pd.DataFrame:
    """
    起動時・通常復元用の標準 tail loader。

    REV1.2:
      1min=180, 3min=120, 5min=90 に増量。
    """
    try:
        interval = int(interval)
        bars = DEFAULT_TAIL_BARS_BY_INTERVAL.get(interval, 90)

        return load_recent_summary_tail_per_symbol(
            interval=interval,
            bars_per_symbol=bars,
            start_dt=start_dt,
            end_dt=end_dt,
            target_dates=target_dates,
            anchor_day=anchor_day,
            max_allowed_dt=max_allowed_dt,
            symbols=symbols,
            symbol_chunk_size=symbol_chunk_size,
        )
    except Exception:
        logger.exception(
            "[summary.recovery.loaders_summary] load_recent_summary_tail_default failed interval=%s",
            interval,
        )
        return pd.DataFrame()


def load_latest_summary_snapshot(interval: int) -> pd.DataFrame:
    try:
        engine = _get_summary_engine()
        model = _get_summary_model(int(interval))
        table_name = resolve_summary_table_name_from_model(model)
        q_table = _quote_identifier(table_name)

        with engine.begin() as conn:
            columns = _get_table_columns(conn, table_name)
            dt_expr = _build_datetime_expr(columns)

            sql = f"""
            WITH latest AS (
                SELECT symbol, MAX({dt_expr}) AS max_dt
                FROM {q_table}
                WHERE symbol IS NOT NULL
                  AND TRIM(symbol) <> ''
                GROUP BY symbol
            )
            SELECT t.*
            FROM {q_table} t
            INNER JOIN latest l
              ON t.symbol = l.symbol
             AND {dt_expr} = l.max_dt
            ORDER BY t.symbol
            """

            df = pd.read_sql(text(sql), conn)

        out = normalize_datetime_columns(df, interval=interval)

        if not out.empty and {"symbol", "datetime"}.issubset(out.columns):
            out = (
                out.sort_values(["symbol", "datetime"], kind="stable")
                .drop_duplicates(subset=["symbol"], keep="last")
                .reset_index(drop=True)
            )

        logger.info(
            "[summary.recovery.loaders_summary] load_latest_summary_snapshot "
            "interval=%s rows=%d symbols=%d",
            interval,
            len(out),
            int(out["symbol"].nunique()) if not out.empty and "symbol" in out.columns else 0,
        )
        log_df_date_breakdown(out, label=f"load_latest_summary_snapshot[{interval}]")
        return out

    except Exception:
        logger.exception(
            "[summary.recovery.loaders_summary] load_latest_summary_snapshot failed interval=%s",
            interval,
        )
        return pd.DataFrame()


__all__ = [
    "DEFAULT_SYMBOL_CHUNK_SIZE",
    "DEFAULT_TAIL_BARS_BY_INTERVAL",
    "HISTORY_REQUIRED_HINT_BY_INTERVAL",
    "resolve_summary_table_name_from_model",
    "read_sqlalchemy_model_to_df",
    "load_last_summary_datetime",
    "load_summary_df_from_datetime",
    "load_summary_df_between",
    "load_recent_summary_tail_per_symbol",
    "load_recent_summary_tail_default",
    "load_latest_summary_snapshot",
]