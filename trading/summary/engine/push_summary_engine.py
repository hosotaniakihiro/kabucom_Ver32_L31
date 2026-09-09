# ============================================================
# File   : trading/summary/engine/push_summary_engine.py
# Version: Ver1.2.1-PUSH-EMPTY-NO-PREVIOUS-REUSE
#          -PUSH-ONLY
#          -PIPELINE-FIRST
#          -IMPORT-COMPAT
#          -NO-PREVIOUS-DAY-REUSE
#          -DIRECT-OHLC-FALLBACK-WHEN-PIPELINE-EMPTY
# ------------------------------------------------------------
# ✔ PUSHベースのサマリー計算専用
# ✔ ranking 系ロジックは一切持たない
# ✔ push_rows=0 時に前回/前日 PUSH merged summary を再投入しない
# ✔ 前日分や巨大キャッシュを parent tick に渡してタイムアウトさせない
# ✔ PUSH が来た時だけ当日データに絞って pipeline / direct OHLC fallback を実行
# ============================================================
from __future__ import annotations

import datetime as dt
import importlib
import logging
import os
import re
import sqlite3
import time as _pse_time
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np
import pandas as pd
from sqlite_cm import committing_connect
from nas_root import nas_path, nas_root

logger = logging.getLogger(__name__)

_PUSH_SUMMARY_DB_SOURCE_TRUE = {"1", "true", "yes", "y", "on", "enable", "enabled"}
_PUSH_SUMMARY_DB_SOURCE_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}


def _push_summary_db_source_env_true(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in _PUSH_SUMMARY_DB_SOURCE_TRUE:
            return True
        if raw in _PUSH_SUMMARY_DB_SOURCE_FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _push_summary_db_source_env_int(name: str, default: int) -> int:
    try:
        raw = str(os.getenv(name, str(default))).strip()
        return int(float(raw))
    except Exception:
        return int(default)


def _push_summary_db_source_safe_latest_dt(df: pd.DataFrame) -> pd.Timestamp | None:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    for col in ("datetime", "received_at", "last_tick_at", "first_tick_at"):
        if col not in df.columns:
            continue
        try:
            s = pd.to_datetime(df[col], errors="coerce")
            if s.notna().any():
                latest = s.max()
                try:
                    latest = latest.tz_localize(None)
                except Exception:
                    pass
                return latest
        except Exception:
            pass
    return None


def _push_summary_db_source_safe_symbol_count(df: pd.DataFrame) -> int:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return 0
    try:
        return int(df["symbol"].astype(str).nunique())
    except Exception:
        return 0


def _push_db_dir_candidates() -> list[Path]:
    """push DB を探す順番。ローカル (writer の書き先) が先、NAS の複製が後。

    2026-09-05 に writer はローカルへ書くようになったが、この読み手だけ
    NAS 固定のまま取り残されていた。NAS へは既定で 30 分に 1 回しか複製
    されないので、1 分足がずっと最大 30 分古いまま固定され、鮮度ガードが
    毎分すべて落として entry が出なくなる (2026-09-09 の実測で確認)。

    過去日のリプレイでは古い DB がローカルに無いことがあるので、
    NAS も候補として残す。
    """
    candidates: list[Path] = []
    try:
        from trading.push import push_db_location

        candidates.append(Path(push_db_location.push_db_dir()))
    except Exception:
        logger.exception("[PUSH SUMMARY DB SOURCE] push_db_location を使えない")

    try:
        from config.paths import get_path

        candidates.append(Path(get_path("raw_push")))
    except Exception:
        logger.exception("[PUSH SUMMARY DB SOURCE] get_path(raw_push) failed")
        env_dir = os.getenv("PUSH_DB_DIR")
        candidates.append(Path(env_dir) if env_dir else Path(nas_path(r"raw_data\kabu_station\push")))

    return candidates


def _push_summary_db_source_resolve_db_path() -> Path | None:
    candidates = _push_db_dir_candidates()
    if not candidates:
        return None

    today = dt.datetime.now().strftime("%Y%m%d")
    for base_dir in candidates:
        db_path = base_dir / f"push{today}.db"
        if db_path.exists():
            return db_path

    logger.warning(
        "[PUSH SUMMARY DB SOURCE] db not found tried=%s",
        [str(d / f"push{today}.db") for d in candidates],
    )
    return None


def _push_summary_db_source_table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table_name,),
        ).fetchone()
        return row is not None
    except Exception:
        return False



def _push_db_effective_lookback_min(default_min: int, *, warm_env: str, cold_min: int) -> int:
    """1分足キャッシュが温まっていれば、PUSH DB を読む窓を短くする。

    2026-08-19 の実測:
      差分キャッシュ (bar_cache) を on にしたことで足の組み立て入力は
      ticks_in 20,000〜30,000 -> ticks_used 764〜1,072 (97%削減) になったが、
      1 tick は 62〜85 秒のままでほとんど改善しなかった。
      原因は「PUSH DB から読む行数自体は 20,000〜30,000 行のまま」で、
      削減できていたのは読んだ後の足組み立てだけだったため。

    キャッシュが足の履歴を保持している以上、毎 tick 20 分ぶん読む必要はない。
    キャッシュが空 (起動直後) のときだけ従来どおり長く読み、
    温まってからは直近ぶんだけ読む。

    warm_env で明示指定があればそれを優先する。
    """
    try:
        raw = os.getenv(warm_env)
        if raw is not None and str(raw).strip() != "":
            return max(1, int(float(raw)))
    except Exception:
        pass

    try:
        from trading.summary.engine.incremental import bar_cache

        if bar_cache.get_mode() == "on" and bar_cache.cached_rows(1) > 0:
            return max(1, int(cold_min))
    except Exception:
        logger.debug("[PUSH SUMMARY DB SOURCE] warm lookback check failed", exc_info=True)

    return int(default_min)

def _push_summary_should_stop(label: str) -> bool:
    """協調キャンセルの降り場所。

    scheduler_jobs.summary.stage_tracker.should_stop() は
    「cancel 要求済み」または「時間予算を使い切った」ときに True を返す。
    worker context の外 (単体呼び出し) では常に False なので、
    通常経路の挙動は変わらない。
    """
    try:
        from scheduler_jobs.summary.stage_tracker import should_stop

        return bool(should_stop(label))
    except Exception:
        return False


def _push_summary_db_source_read_recent_rows() -> pd.DataFrame:
    """summary_database_runner.py 専用: メモリ上のPUSH DFが空/古い場合に
    pushYYYYMMDD.dbの直近コミット済み行を読み直す (旧 push_summary_db_source_patch)。

    summary_database は push_receiver とは別プロセスのため、global_data の
    push_df/stream_data がメモリ上で空/古いままになりうる。
    PUSH_SUMMARY_DB_SOURCE_FALLBACK=1 が設定されているプロセス
    (summary_database_runner.py) でのみ有効。
    """
    # cancel 済み / 予算切れの worker は、そもそも重い read に入らない。
    # ここを通り過ぎると pandas.read_sql_query -> cursor.execute の
    # キャンセル不能区間に入ってしまう。
    if _push_summary_should_stop("push_summary_db_read.before"):
        return pd.DataFrame()

    db_path = _push_summary_db_source_resolve_db_path()
    if db_path is None:
        return pd.DataFrame()

    lookback_min = max(3, _push_summary_db_source_env_int("PUSH_SUMMARY_DB_LOOKBACK_MIN", 20))
    lookback_min = _push_db_effective_lookback_min(
        lookback_min, warm_env="PUSH_SUMMARY_DB_WARM_LOOKBACK_MIN", cold_min=3
    )
    max_rows = max(1000, _push_summary_db_source_env_int("PUSH_SUMMARY_DB_MAX_ROWS", 30000))
    busy_timeout_ms = max(1000, _push_summary_db_source_env_int("PUSH_SUMMARY_DB_BUSY_TIMEOUT_MS", 5000))

    now = dt.datetime.now()
    cutoff = (now - dt.timedelta(minutes=lookback_min)).strftime("%Y-%m-%d %H:%M:%S")

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=max(1.0, busy_timeout_ms / 1000.0))
        conn.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        conn.execute("PRAGMA query_only=ON")

        table_name = "stream_data_raw" if _push_summary_db_source_table_exists(conn, "stream_data_raw") else "stream_data"
        if not _push_summary_db_source_table_exists(conn, table_name):
            logger.warning("[PUSH SUMMARY DB SOURCE] no stream table db=%s", db_path)
            return pd.DataFrame()

        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table_name})").fetchall()]
        colset = set(cols)

        # ------------------------------------------------------------
        # raw_json は SELECT しない (2026-09-03)
        # ------------------------------------------------------------
        # raw_json は PUSH メッセージ全文で、このテーブルで最も大きい列。
        # この関数より下流で一度も参照されていない (grep で
        # push_summary_engine.py 内の出現はこの列リスト 1 箇所だけ)。
        # 30,000 行分を NAS(SMB) 越しに引いてそのまま捨てていた。
        #
        # 実測 (2026-09-03 13:37 のゾンビ worker のスタック):
        #     push_summary_engine.py:241  pd.read_sql_query(...)
        #     pandas/io/sql.py:2768       result = cur.fetchall()   <- ここで固着
        # 詰まっていたのは execute() ではなく fetchall()、つまり
        # クエリプランではなく **転送量** が原因だった
        # (プラン自体は idx_stream_data_raw_datetime で解決済み)。
        #
        # cursor.fetchall() の内側は Python からキャンセルできないので、
        # cancel SLA を守るには転送量そのものを削るしかない。
        #
        # PUSH_SUMMARY_DB_SOURCE_SELECT_RAW_JSON=1 で従来動作に戻せる。
        _want_raw_json = _push_summary_db_source_env_true(
            "PUSH_SUMMARY_DB_SOURCE_SELECT_RAW_JSON", False
        )
        select_cols: list[str] = []
        for c in (
            "symbol", "symbolname", "datetime", "date", "time", "price", "volume",
            "trading_value", "vwap", "previousclose", "opening_price", "high_price", "low_price",
            "raw_json", "received_at",
        ):
            if c == "raw_json" and not _want_raw_json:
                continue
            if c in colset:
                select_cols.append(c)

        if "symbol" not in select_cols or "datetime" not in select_cols:
            logger.warning(
                "[PUSH SUMMARY DB SOURCE] required cols missing table=%s cols=%s",
                table_name,
                cols,
            )
            return pd.DataFrame()

        where_parts = ["datetime >= ?"]
        params: list[Any] = [cutoff]
        if "received_at" in colset:
            where_parts.append("received_at >= ?")
            params.append(cutoff)

        sql = (
            f"SELECT {', '.join(select_cols)} FROM {table_name} "
            f"WHERE ({' OR '.join(where_parts)}) "
            f"ORDER BY datetime DESC LIMIT ?"
        )
        params.append(max_rows)

        _read_t0 = _pse_time.perf_counter()
        df = pd.read_sql_query(sql, conn, params=params)
        _read_dt = _pse_time.perf_counter() - _read_t0
        if _read_dt >= 3.0:
            logger.warning(
                "[PUSH SUMMARY DB SOURCE] slow read %.3fs table=%s cutoff=%s rows=%s db=%s "
                "(datetime index が無いとフルスキャンになる)",
                _read_dt, table_name, cutoff, len(df), db_path,
            )

        # read 中に cancel された場合、結果は捨てる。
        # 古い generation の結果を publish/cache させない。
        if _push_summary_should_stop("push_summary_db_read.after"):
            logger.warning(
                "[PUSH SUMMARY DB SOURCE] cancelled during read -> 結果を破棄する "
                "read=%.3fs rows=%s table=%s",
                _read_dt, len(df), table_name,
            )
            return pd.DataFrame()

        if df.empty:
            logger.warning(
                "[PUSH SUMMARY DB SOURCE] recent rows empty table=%s cutoff=%s lookback_min=%s db=%s",
                table_name,
                cutoff,
                lookback_min,
                db_path,
            )
            return df

        df = df.sort_values(["symbol", "datetime"], kind="stable").reset_index(drop=True)

        if "price" in df.columns:
            df["current_price"] = pd.to_numeric(df["price"], errors="coerce")
            df["close"] = pd.to_numeric(df["price"], errors="coerce")
            df["close_price"] = pd.to_numeric(df["price"], errors="coerce")
        if "opening_price" in df.columns:
            df["open"] = pd.to_numeric(df["opening_price"], errors="coerce")
            df["open_price"] = pd.to_numeric(df["opening_price"], errors="coerce")
        if "high_price" in df.columns:
            df["high"] = pd.to_numeric(df["high_price"], errors="coerce")
        if "low_price" in df.columns:
            df["low"] = pd.to_numeric(df["low_price"], errors="coerce")
        if "volume" in df.columns:
            df["trading_volume"] = pd.to_numeric(df["volume"], errors="coerce")

        df["source"] = "push_db_recent"

        latest = _push_summary_db_source_safe_latest_dt(df)
        logger.warning(
            "[PUSH SUMMARY DB SOURCE] loaded table=%s cols=%s rows=%s symbols=%s latest_dt=%s cutoff=%s db=%s",
            table_name,
            len(select_cols),
            len(df),
            _push_summary_db_source_safe_symbol_count(df),
            latest,
            cutoff,
            db_path,
        )
        return df

    except Exception:
        logger.exception("[PUSH SUMMARY DB SOURCE] read failed db=%s", db_path)
        return pd.DataFrame()
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _push_summary_db_source_augment(mem_df: pd.DataFrame) -> pd.DataFrame:
    if not _push_summary_db_source_env_true("PUSH_SUMMARY_DB_SOURCE_FALLBACK", False):
        return mem_df

    db_df = _push_summary_db_source_read_recent_rows()
    if not isinstance(db_df, pd.DataFrame) or db_df.empty:
        return mem_df if isinstance(mem_df, pd.DataFrame) else pd.DataFrame()

    if not isinstance(mem_df, pd.DataFrame) or mem_df.empty:
        logger.warning(
            "[PUSH SUMMARY DB SOURCE] use db because memory push_df empty rows=%s symbols=%s latest_dt=%s",
            len(db_df),
            _push_summary_db_source_safe_symbol_count(db_df),
            _push_summary_db_source_safe_latest_dt(db_df),
        )
        return db_df

    mem_latest = _push_summary_db_source_safe_latest_dt(mem_df)
    db_latest = _push_summary_db_source_safe_latest_dt(db_df)
    max_lag_sec = max(5, _push_summary_db_source_env_int("PUSH_SUMMARY_DB_SOURCE_MAX_MEM_LAG_SEC", 30))

    try:
        if mem_latest is None or (db_latest is not None and (db_latest - mem_latest).total_seconds() > max_lag_sec):
            logger.warning(
                "[PUSH SUMMARY DB SOURCE] use db because memory stale mem_rows=%s mem_latest=%s db_rows=%s db_latest=%s lag_limit=%ss",
                len(mem_df),
                mem_latest,
                len(db_df),
                db_latest,
                max_lag_sec,
            )
            return db_df
    except Exception:
        logger.exception("[PUSH SUMMARY DB SOURCE] freshness compare failed; keep memory")

    logger.info(
        "[PUSH SUMMARY DB SOURCE] keep memory rows=%s latest=%s db_rows=%s db_latest=%s",
        len(mem_df),
        mem_latest,
        len(db_df),
        db_latest,
    )
    return mem_df

try:
    from global_state import global_data
except Exception:
    try:
        from core.global_context.context import global_data  # type: ignore
    except Exception:
        global_data = None

try:
    from trading.summary.engine.data_quality_engine import run_data_quality_checks
except Exception:
    run_data_quality_checks = None


def _resolve_callable(candidates: list[tuple[str, str]]) -> Optional[Callable]:
    for module_name, func_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
            if callable(fn):
                logger.info("[PUSH SUMMARY ENGINE] resolved %s -> %s.%s", func_name, module_name, func_name)
                return fn
            logger.warning("[PUSH SUMMARY ENGINE] candidate attribute missing: %s.%s", module_name, func_name)
        except Exception as e:
            logger.warning("[PUSH SUMMARY ENGINE] candidate import failed: %s.%s: %s: %s", module_name, func_name, type(e).__name__, e)
    return None


def _resolve_build_incremental_summary() -> Optional[Callable]:
    return _resolve_callable([
        ("trading.summary.pipeline.summary_pipeline", "run_summary_pipeline"),
        ("trading.summary.engine.summary_incremental_engine", "build_incremental_summary"),
        ("trading.summary.engine.incremental_summary_engine", "build_incremental_summary"),
        ("trading.summary.engine.summary_recovery_engine", "build_incremental_summary"),
    ])


def _safe_attr(obj: Any, name: str, default=None):
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def _safe_copy_df(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    if isinstance(value, tuple) and len(value) >= 1 and isinstance(value[0], pd.DataFrame):
        return value[0].copy()
    if isinstance(value, dict):
        for key in ("result_df", "merged_df", "df", "summary_df", "output_df", "display_df", "latest_df", "latest_summary_df"):
            v = value.get(key)
            if isinstance(v, pd.DataFrame):
                return v.copy()
    try:
        return pd.DataFrame(value).copy()
    except Exception:
        return pd.DataFrame()


def _safe_symbol_count(df: pd.DataFrame) -> int:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return 0
    try:
        return int(df["symbol"].astype(str).nunique())
    except Exception:
        return 0


def _safe_latest_dt(df: pd.DataFrame):
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    for col in ("datetime", "end_time", "start_time", "snapshot_time", "received_at", "CurrentPriceTime", "current_price_time"):
        if col not in df.columns:
            continue
        try:
            s = pd.to_datetime(df[col], errors="coerce")
            if s.notna().any():
                ts = s.max()
                try:
                    ts = ts.tz_localize(None)
                except Exception:
                    pass
                return ts
        except Exception:
            pass
    return None


def _today_date():
    try:
        return pd.Timestamp.now().date()
    except Exception:
        return None


def _latest_date(df: pd.DataFrame):
    latest = _safe_latest_dt(df)
    try:
        return pd.Timestamp(latest).date() if latest is not None else None
    except Exception:
        return None


def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    out = df.copy()
    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    elif "date" in out.columns and "start_time" in out.columns:
        out["datetime"] = pd.to_datetime(out["date"].astype(str).str.strip() + " " + out["start_time"].astype(str).str.strip(), errors="coerce")
    elif "date" in out.columns and "time" in out.columns:
        out["datetime"] = pd.to_datetime(out["date"].astype(str).str.strip() + " " + out["time"].astype(str).str.strip(), errors="coerce")
    elif "end_time" in out.columns:
        out["datetime"] = pd.to_datetime(out["end_time"], errors="coerce")
    elif "snapshot_time" in out.columns:
        out["datetime"] = pd.to_datetime(out["snapshot_time"], errors="coerce")
    elif "CurrentPriceTime" in out.columns:
        out["datetime"] = pd.to_datetime(out["CurrentPriceTime"], errors="coerce")
    elif "current_price_time" in out.columns:
        out["datetime"] = pd.to_datetime(out["current_price_time"], errors="coerce")
    elif "received_at" in out.columns:
        out["datetime"] = pd.to_datetime(out["received_at"], errors="coerce")
    else:
        out["datetime"] = pd.NaT
    try:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce").dt.tz_localize(None)
    except Exception:
        pass
    return out


def _ensure_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    out = df.copy()
    if "symbol" not in out.columns:
        for c in ("Symbol", "symbol_code", "Code", "code"):
            if c in out.columns:
                out["symbol"] = out[c]
                break
    if "symbol" not in out.columns:
        out["symbol"] = ""
    out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    out = out[out["symbol"] != ""].copy()
    return out


def _coalesce_numeric_columns(df: pd.DataFrame, target: str, candidates: list[str]) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    out = df.copy()
    merged = None
    for c in candidates:
        if c not in out.columns:
            continue
        try:
            s = pd.to_numeric(out[c], errors="coerce")
            merged = s if merged is None else merged.combine_first(s)
        except Exception:
            pass
    if merged is not None:
        out[target] = merged
    return out


def _normalize_push_source_df_core(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_copy_df(df)
    if out.empty:
        return out
    out = _ensure_symbol(out)
    out = _ensure_datetime(out)
    out = _coalesce_numeric_columns(out, "close", ["close", "close_price", "price", "Price", "current_price", "CurrentPrice", "last_price", "LastPrice", "Close", "ClosePrice"])
    out = _coalesce_numeric_columns(out, "open", ["open", "open_price", "Open", "OpenPrice"])
    out = _coalesce_numeric_columns(out, "high", ["high", "high_price", "High", "HighPrice"])
    out = _coalesce_numeric_columns(out, "low", ["low", "low_price", "Low", "LowPrice"])
    if "close" in out.columns:
        for c in ("open", "high", "low"):
            if c not in out.columns:
                out[c] = out["close"]
            else:
                try:
                    out[c] = pd.to_numeric(out[c], errors="coerce").combine_first(pd.to_numeric(out["close"], errors="coerce"))
                except Exception:
                    pass
        if "close_price" not in out.columns:
            out["close_price"] = out["close"]
        if "price" not in out.columns:
            out["price"] = out["close"]
        if "current_price" not in out.columns:
            out["current_price"] = out["close"]
    if "open" in out.columns and "open_price" not in out.columns:
        out["open_price"] = out["open"]
    if "high" in out.columns and "high_price" not in out.columns:
        out["high_price"] = out["high"]
    if "low" in out.columns and "low_price" not in out.columns:
        out["low_price"] = out["low"]
    out = _coalesce_numeric_columns(out, "volume", ["volume", "Volume", "TradingVolume", "trading_volume", "CumVolume", "cum_volume", "last_cum_volume"])
    if "symbolname" not in out.columns:
        for c in ("SymbolName", "symbol_name", "name", "Name"):
            if c in out.columns:
                out["symbolname"] = out[c]
                break
    if "symbolname" not in out.columns and "symbol" in out.columns:
        out["symbolname"] = out["symbol"]
    if "source" not in out.columns:
        out["source"] = "push_stream"
    out = out.dropna(subset=["symbol"], how="any")
    if "datetime" in out.columns:
        out = out.dropna(subset=["datetime"], how="all")
    logger.info("[PUSH SUMMARY ENGINE] normalized push source rows=%s cols=%s symbols=%s latest_dt=%s", len(out), len(out.columns), _safe_symbol_count(out), _safe_latest_dt(out))
    return out.reset_index(drop=True)


def _normalize_summary_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_copy_df(df)
    if out.empty:
        return out
    out = _ensure_symbol(_ensure_datetime(out))
    return out.reset_index(drop=True)


def _filter_same_day(df: pd.DataFrame, *, ref_df: pd.DataFrame | None = None, today_only: bool = True) -> pd.DataFrame:
    out = _normalize_summary_df(df)
    if out.empty or "datetime" not in out.columns:
        return out
    ref_date = _latest_date(ref_df) if isinstance(ref_df, pd.DataFrame) and not ref_df.empty else None
    if ref_date is None and today_only:
        ref_date = _today_date()
    if ref_date is None:
        return out
    try:
        s = pd.to_datetime(out["datetime"], errors="coerce")
        before = len(out)
        out = out[s.dt.date == ref_date].copy()
        if before and len(out) != before:
            logger.warning("[PUSH SUMMARY ENGINE] filtered stale summary rows before=%s after=%s date=%s", before, len(out), ref_date)
    except Exception:
        logger.exception("[PUSH SUMMARY ENGINE] same-day filter failed")
    return out.reset_index(drop=True)


def _trim_recent_per_symbol(df: pd.DataFrame, bars: int = 120) -> pd.DataFrame:
    out = _normalize_summary_df(df)
    if out.empty or "symbol" not in out.columns or "datetime" not in out.columns:
        return out
    try:
        before = len(out)
        out = out.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", group_keys=False).tail(int(bars)).reset_index(drop=True)
        if before != len(out):
            logger.warning("[PUSH SUMMARY ENGINE] trimmed summary rows before=%s after=%s bars=%s", before, len(out), bars)
    except Exception:
        logger.exception("[PUSH SUMMARY ENGINE] trim recent failed")
    return out


def _log_df_stats(label: str, df: pd.DataFrame) -> None:
    rows = len(df) if isinstance(df, pd.DataFrame) else 0
    cols = len(df.columns) if isinstance(df, pd.DataFrame) else 0
    symbols = _safe_symbol_count(df)
    latest_dt = _safe_latest_dt(df)
    logger.info("[PUSH SUMMARY STATS] %s | rows=%d cols=%d symbols=%d latest_dt=%s", label, rows, cols, symbols, latest_dt)


def _get_from_global_data(candidates: list[str]) -> Any:
    if global_data is None:
        return None
    for name in candidates:
        try:
            if hasattr(global_data, name):
                value = getattr(global_data, name)
                if value is not None:
                    logger.info("[PUSH SUMMARY ENGINE] global_data hit key=%s type=%s", name, type(value).__name__)
                    return value
        except Exception:
            pass
    logger.info("[PUSH SUMMARY ENGINE] global_data miss keys=%s", candidates)
    return None


def _push_memory_stream_df() -> pd.DataFrame:
    """PUSH WebSocket のメモリバッファを返す (NAS アクセスなし)。

    memory_latest_1m._load_push_memory_df_core() と同じソース。
    main.py は memory-only 運用なので、ここが唯一の新鮮な PUSH 経路になる。
    """
    try:
        from trading.push.push_stream import get_push_dataframe

        x = get_push_dataframe()
        if isinstance(x, pd.DataFrame) and not x.empty:
            return x
    except Exception:
        logger.debug("[PUSH SUMMARY ENGINE] push_stream memory df unavailable", exc_info=True)
    return pd.DataFrame()


def _push_source_max_age_sec() -> float:
    """global_data 側を stale とみなす秒数。

    既存の direct_push_1m と同じ既定値 (90秒) に揃える。
    """
    try:
        raw = os.getenv("PUSH_SUMMARY_MEMORY_PREFER_MAX_AGE_SEC")
        if raw is None or str(raw).strip() == "":
            raw = os.getenv("SUMMARY_FORCE_DIRECT_MEMORY_LOOKBACK_SEC", "90")
        return max(30.0, float(raw))
    except Exception:
        return 90.0


def _prefer_fresh_push_memory(base: pd.DataFrame) -> pd.DataFrame:
    """global_data 側が stale なら、より新しいメモリ PUSH を優先する。

    起動時に core/startup/push/bootstrap が NAS の push DB から seed した
    global_data.push_df は、push_receiver が落ちていた時間帯の古い
    スナップショットのままになりうる。
    それをそのまま使うと 1 tick 丸々を無駄に使った上で
    後段の STALE DROP で全件捨てられる (= tick timeout)。
    """
    try:
        mem = _push_memory_stream_df()
        if not isinstance(mem, pd.DataFrame) or mem.empty:
            return base

        base_dt = _safe_latest_dt(base) if isinstance(base, pd.DataFrame) and not base.empty else None
        mem_dt = _safe_latest_dt(mem)
        if mem_dt is None:
            return base

        now = pd.Timestamp(dt.datetime.now())
        base_age = float((now - pd.Timestamp(base_dt)).total_seconds()) if base_dt is not None else float("inf")
        mem_age = float((now - pd.Timestamp(mem_dt)).total_seconds())
        max_age = _push_source_max_age_sec()

        # base が新鮮なら何もしない (従来動作)。
        if base_age <= max_age:
            return base
        # memory も base と同等以上に古いなら差し替えても意味が無い。
        if base_dt is not None and mem_dt <= base_dt:
            return base

        logger.warning(
            "[PUSH SUMMARY ENGINE] prefer fresh push memory over stale global_data "
            "base_latest=%s base_age=%.1fs mem_latest=%s mem_age=%.1fs max_age=%.0fs base_rows=%s mem_rows=%s",
            base_dt, base_age, mem_dt, mem_age, max_age,
            len(base) if isinstance(base, pd.DataFrame) else 0, len(mem),
        )
        return mem
    except Exception:
        logger.exception("[PUSH SUMMARY ENGINE] prefer fresh push memory failed")
        return base


def _resolve_push_source_df_core() -> pd.DataFrame:
    df = _get_from_global_data(["stream_data", "push_df", "push_data", "latest_push_df", "push_snapshot_df"])
    if (not isinstance(df, pd.DataFrame)) or df.empty:
        getter = _safe_attr(global_data, "get_push_df", None)
        if callable(getter):
            try:
                df = getter()
                logger.info("[PUSH SUMMARY ENGINE] get_push_df() used")
            except Exception:
                logger.exception("[PUSH SUMMARY ENGINE] get_push_df() failed")

    # global_data 側が stale だったらメモリ PUSH を優先する (NAS は読まない)。
    df = _prefer_fresh_push_memory(df if isinstance(df, pd.DataFrame) else pd.DataFrame())
    out = _normalize_push_source_df(df)
    out = _filter_same_day(out, today_only=True)
    _log_df_stats("resolved push source", out)
    out = _push_summary_db_source_augment(out)
    return out


# --- push_summary_db_latest_source_patch (native化, 元 core/startup/push_summary_db_latest_source_patch.py) ---
# data_collectors_runner / summary_database 等、push_receiver とは別プロセスでは
# process-local の global_data.push_df が古いまま残り、PUSH DB (push{YYYYMMDD}.db) には
# 最新行が保存されているのに1分PUSH summaryが古いデータを再利用してしまう問題への対処。
#
# 注意: この節が読む push{YYYYMMDD}.db の stream_data_raw/stream_data テーブルは、
# 上の _push_summary_db_source_augment() (別の未記録コミットで本文化済みの
# 旧 push_summary_db_source_patch、既定OFFの PUSH_SUMMARY_DB_SOURCE_FALLBACK gate)
# が読むテーブルと同じだが、env変数名・列選択・既定値が異なる独立した実装であり、
# 意図的に重複させたまま移植した (旧パッチ自身が「original呼び出し後にさらに
# 自前のDB読み込みで比較・置換する」設計だったため、native化でもその二重構造を
# そのまま保持する)。
_PUSH_SUMMARY_DB_LATEST_SOURCE_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _push_summary_db_latest_source_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in _PUSH_SUMMARY_DB_LATEST_SOURCE_TRUE
    except Exception:
        return bool(default)


def _push_summary_db_latest_source_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _push_summary_db_latest_source_env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _push_summary_db_latest_source_argv_context() -> str:
    """core/startup/runtime_env/installer.py の _argv_context() と同一の判定を再現する。"""
    try:
        text = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if "main_database.py" in text:
            return "main_database"
        if text.endswith("main.py") or "/main.py" in text:
            return "main"
        if "db_prepare_runner.py" in text:
            return "db_prepare"
        if "push_receiver_runner.py" in text:
            return "push_receiver"
        if "summary_database_runner.py" in text:
            return "summary_database"
        if "ranking_collector_runner.py" in text:
            return "ranking_collector"
        if "yahoo_complement_runner.py" in text:
            return "yahoo_complement"
        if "data_collectors_runner.py" in text:
            return "main_database"
        if text:
            return "helper"
    except Exception:
        pass
    return "unknown"


def _push_summary_db_latest_source_scope_enabled() -> bool:
    """旧パッチの root importer だった DB_CONTEXTS|GENERIC_HELPER_CONTEXTS
    ({"main_database","db_prepare","push_receiver","summary_database",
      "ranking_collector","yahoo_complement","helper"}、実質 "main.py以外") を再現する。
    """
    try:
        if str(os.environ.get("DISABLE_PUSH_SUMMARY_DB_SOURCE_PATCH", "")).strip() == "1":
            return False
        if not _push_summary_db_latest_source_env_bool("PUSH_SUMMARY_DB_SOURCE_ENABLED", True):
            return False
        raw = os.getenv("PUSH_SUMMARY_DB_LATEST_SOURCE_SCOPE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return _push_summary_db_latest_source_env_bool("PUSH_SUMMARY_DB_LATEST_SOURCE_SCOPE_ENABLED", False)
        return _push_summary_db_latest_source_argv_context() not in {"main", "unknown"}
    except Exception:
        return False


def _push_summary_db_latest_source_push_db_path() -> Path:
    ymd = os.getenv("KABU_TODAY") or os.getenv("TARGET_DATE") or dt.datetime.now().strftime("%Y%m%d")
    # 置き場所の判断は _push_db_dir_candidates に集約した (ローカル優先)。
    candidates = _push_db_dir_candidates()
    for root in candidates:
        path = root / f"push{ymd}.db"
        if path.exists():
            return path
    fallback = candidates[0] if candidates else Path(
        os.getenv("PUSH_DB_DIR", nas_path(r"raw_data\kabu_station\push"))
    )
    return fallback / f"push{ymd}.db"


def _push_summary_db_latest_source_latest_dt(df: Any):
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None
        for c in ("datetime", "current_price_time", "CurrentPriceTime", "received_at", "time"):
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce")
                if s.notna().any():
                    ts = s.max()
                    try:
                        return ts.tz_localize(None)
                    except Exception:
                        return ts
    except Exception:
        pass
    return None


def _push_summary_db_latest_source_age_sec(ts: Any) -> float:
    try:
        if ts is None or pd.isna(ts):
            return 999999.0
        t = pd.Timestamp(ts)
        try:
            t = t.tz_localize(None)
        except Exception:
            pass
        return max(0.0, (dt.datetime.now() - t.to_pydatetime()).total_seconds())
    except Exception:
        return 999999.0


def _push_summary_db_latest_source_read_recent() -> pd.DataFrame:
    db = _push_summary_db_latest_source_push_db_path()
    if not db.exists():
        return pd.DataFrame()
    lookback_min = max(1, _push_summary_db_latest_source_env_int("PUSH_SUMMARY_DB_SOURCE_LOOKBACK_MIN", 20))
    lookback_min = _push_db_effective_lookback_min(
        lookback_min, warm_env="PUSH_SUMMARY_DB_SOURCE_WARM_LOOKBACK_MIN", cold_min=3
    )
    limit = max(100, _push_summary_db_latest_source_env_int("PUSH_SUMMARY_DB_SOURCE_LIMIT", 20000))
    busy_ms = max(100, _push_summary_db_latest_source_env_int("PUSH_SUMMARY_DB_SOURCE_BUSY_TIMEOUT_MS", 1000))
    cutoff = (dt.datetime.now() - dt.timedelta(minutes=lookback_min)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        with committing_connect(str(db), timeout=max(0.5, busy_ms / 1000.0)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute(f"PRAGMA busy_timeout={busy_ms}")
            table = "stream_data_raw"
            try:
                conn.execute("SELECT 1 FROM stream_data_raw LIMIT 1").fetchone()
            except Exception:
                table = "stream_data"
            sql = f"""
                SELECT *
                  FROM {table}
                 WHERE datetime >= ?
                 ORDER BY datetime DESC
                 LIMIT ?
            """
            df = pd.read_sql_query(sql, conn, params=(cutoff, int(limit)))
    except Exception:
        logger.exception("[PUSH SUMMARY DB SOURCE] read db failed db=%s", db)
        return pd.DataFrame()

    if df.empty:
        logger.warning("[PUSH SUMMARY DB SOURCE] db recent empty db=%s cutoff=%s table=stream_data/raw", db, cutoff)
        return df
    try:
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime", "symbol"]).copy()
        df["symbol"] = df["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        if "price" in df.columns:
            df["current_price"] = pd.to_numeric(df.get("current_price", df["price"]), errors="coerce").combine_first(pd.to_numeric(df["price"], errors="coerce"))
            df["close"] = pd.to_numeric(df["price"], errors="coerce").combine_first(pd.to_numeric(df["current_price"], errors="coerce"))
        if "volume" in df.columns:
            df["trading_volume"] = pd.to_numeric(df.get("trading_volume", df["volume"]), errors="coerce").combine_first(pd.to_numeric(df["volume"], errors="coerce"))
        df["source"] = "push_stream_raw_db_latest_patch"
    except Exception:
        logger.exception("[PUSH SUMMARY DB SOURCE] normalize failed")
    logger.warning(
        "[PUSH SUMMARY DB SOURCE] loaded db rows=%s symbols=%s latest_dt=%s db=%s",
        len(df), df["symbol"].nunique() if "symbol" in df.columns else 0, _push_summary_db_latest_source_latest_dt(df), db,
    )
    return df.reset_index(drop=True)


def _push_summary_db_latest_source_better_df(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    """Return fresher/non-empty source. b is DB source."""
    if not isinstance(b, pd.DataFrame) or b.empty:
        return a
    if not isinstance(a, pd.DataFrame) or a.empty:
        return b
    a_dt = _push_summary_db_latest_source_latest_dt(a)
    b_dt = _push_summary_db_latest_source_latest_dt(b)
    a_age = _push_summary_db_latest_source_age_sec(a_dt)
    b_age = _push_summary_db_latest_source_age_sec(b_dt)
    stale_sec = max(30.0, _push_summary_db_latest_source_env_float("PUSH_SUMMARY_DB_SOURCE_REPLACE_IF_OLDER_SEC", 180.0))
    if b_dt is not None and (a_dt is None or b_dt > a_dt or a_age > stale_sec):
        logger.warning(
            "[PUSH SUMMARY DB SOURCE] replace push source old_latest=%s old_age=%.1fs db_latest=%s db_age=%.1fs old_rows=%s db_rows=%s",
            a_dt, a_age, b_dt, b_age, len(a), len(b),
        )
        return b
    return a


def _resolve_push_source_df() -> pd.DataFrame:
    base = pd.DataFrame()
    try:
        base = _resolve_push_source_df_core()
    except Exception:
        logger.exception("[PUSH SUMMARY DB SOURCE] original resolve failed")
    if not _push_summary_db_latest_source_scope_enabled():
        return base
    try:
        db_df = _push_summary_db_latest_source_read_recent()
        return _push_summary_db_latest_source_better_df(base, db_df)
    except Exception:
        logger.exception("[PUSH SUMMARY DB SOURCE] patched resolve failed")
        return base


def _resolve_summary_source_df(interval: int, *, push_df: pd.DataFrame) -> pd.DataFrame:
    tf = int(interval)
    df = _get_from_global_data([f"push_summary_{tf}min", f"push_summary_{tf}", f"summary_{tf}min_df", f"summary_df_{tf}min", f"latest_summary_{tf}min", f"push_merged_summary_{tf}min", f"push_merged_summary_{tf}"])
    if (not isinstance(df, pd.DataFrame)) or df.empty:
        getter = _safe_attr(global_data, "get_push_summary", None)
        if callable(getter):
            try:
                df = getter(tf)
                logger.info("[PUSH SUMMARY ENGINE] get_push_summary(tf=%s) used", tf)
            except Exception:
                logger.exception("[PUSH SUMMARY ENGINE] get_push_summary(tf=%s) failed", tf)
    if (not isinstance(df, pd.DataFrame)) or df.empty:
        getter = _safe_attr(global_data, "get_merged_summary", None)
        if callable(getter):
            try:
                df = getter(tf, source="push")
                logger.info("[PUSH SUMMARY ENGINE] get_merged_summary(tf=%s, source=push) used", tf)
            except TypeError:
                try:
                    df = getter(tf)
                    logger.info("[PUSH SUMMARY ENGINE] get_merged_summary(tf=%s) used as push fallback", tf)
                except Exception:
                    pass
            except Exception:
                pass
    out = _normalize_summary_df(df)
    out = _filter_same_day(out, ref_df=push_df, today_only=True)
    out = _trim_recent_per_symbol(out, bars=120)
    _log_df_stats(f"resolved push summary source interval={tf}", out)
    return out


def _is_latest_only_frame(df: pd.DataFrame) -> tuple[bool, float, int, int]:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return False, 0.0, 0, 0
    try:
        out = _ensure_symbol(_ensure_datetime(df))
        total = len(out)
        if total == 0 or "datetime" not in out.columns:
            return False, 0.0, 0, total
        s = pd.to_datetime(out["datetime"], errors="coerce")
        if s.notna().sum() == 0:
            return False, 0.0, 0, total
        latest = s.max()
        one_bar = int((s == latest).sum())
        ratio = float(one_bar / total) if total > 0 else 0.0
        return ratio >= 0.98, ratio, one_bar, total
    except Exception:
        logger.exception("[PUSH SUMMARY ENGINE] latest-only probe failed")
        return False, 0.0, 0, 0


def _maturity_profile(df: pd.DataFrame) -> dict:
    prof = {"rows": 0, "symbols": 0, "technical_ready_rows": 0, "hist_ge3": 0, "hist_max": 0.0, "score_nonzero": 0, "score_buy_nonzero": 0, "score_sell_nonzero": 0}
    if not isinstance(df, pd.DataFrame) or df.empty:
        return prof
    try:
        prof["rows"] = len(df)
        prof["symbols"] = _safe_symbol_count(df)
        if "technical_ready" in df.columns:
            prof["technical_ready_rows"] = int(pd.Series(df["technical_ready"]).fillna(False).astype(bool).sum())
        if "symbol_hist_len" in df.columns:
            hist = pd.to_numeric(df["symbol_hist_len"], errors="coerce")
            prof["hist_ge3"] = int((hist >= 3).sum())
            prof["hist_max"] = float(hist.max()) if hist.notna().any() else 0.0
        for col, key in (("score", "score_nonzero"), ("score_buy", "score_buy_nonzero"), ("score_sell", "score_sell_nonzero")):
            if col in df.columns:
                s = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
                prof[key] = int((s != 0).sum())
    except Exception:
        logger.exception("[PUSH SUMMARY ENGINE] maturity profile failed")
    return prof


def _looks_immature(df: pd.DataFrame) -> tuple[bool, dict]:
    prof = _maturity_profile(df)
    immature = prof["rows"] > 0 and ((prof["technical_ready_rows"] == 0 and prof["hist_ge3"] == 0 and prof["hist_max"] <= 2.0) or (prof["score_nonzero"] == 0 and prof["score_buy_nonzero"] == 0 and prof["score_sell_nonzero"] == 0))
    return immature, prof


def _calc_rsi_direct(close: pd.Series, period: int = 14) -> pd.Series:
    try:
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
        rs = avg_gain / avg_loss.replace(0, pd.NA)
        return pd.to_numeric(100 - (100 / (1 + rs)), errors="coerce").fillna(50.0)
    except Exception:
        return pd.Series([50.0] * len(close), index=close.index)


def _build_direct_ohlc_from_push_source_core(push_df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    ticks = _normalize_push_source_df(push_df)
    if ticks.empty:
        logger.warning("[PUSH SUMMARY ENGINE][DIRECT OHLC] normalized ticks empty interval=%s", interval)
        return pd.DataFrame()
    required = {"datetime", "symbol", "close"}
    if not required.issubset(set(ticks.columns)):
        logger.warning("[PUSH SUMMARY ENGINE][DIRECT OHLC] required cols missing interval=%s cols=%s", interval, list(ticks.columns))
        return pd.DataFrame()
    ticks = ticks.dropna(subset=["datetime", "symbol", "close"]).copy()
    if ticks.empty:
        return pd.DataFrame()
    ticks["datetime"] = pd.to_datetime(ticks["datetime"], errors="coerce")
    try:
        ticks["datetime"] = ticks["datetime"].dt.tz_localize(None)
    except Exception:
        pass
    freq = f"{int(interval)}min"
    ticks["_slot"] = ticks["datetime"].dt.floor(freq)
    ticks = ticks.sort_values(["symbol", "datetime"], kind="stable")

    def _last_nonempty(s: pd.Series):
        try:
            x = s.dropna()
            return x.iloc[-1] if not x.empty else ""
        except Exception:
            return ""

    bars = ticks.groupby(["symbol", "_slot"], as_index=False).agg(
        symbolname=("symbolname", _last_nonempty),
        open=("close", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "max"),
        tick_count=("close", "count"),
        first_tick_at=("datetime", "min"),
        last_tick_at=("datetime", "max"),
    ).rename(columns={"_slot": "datetime"})
    for c in ("open", "high", "low", "close", "volume"):
        if c in bars.columns:
            bars[c] = pd.to_numeric(bars[c], errors="coerce")
    bars = bars.dropna(subset=["symbol", "datetime", "close"]).copy()
    if bars.empty:
        return pd.DataFrame()
    parts = []
    for _sym, one in bars.groupby("symbol", sort=False):
        one = one.copy().sort_values("datetime", kind="stable")
        close = pd.to_numeric(one["close"], errors="coerce")
        prev_close = close.shift(1)
        pct = (close - prev_close) / prev_close.replace(0, pd.NA)
        intrabar = (close - pd.to_numeric(one["open"], errors="coerce")) / pd.to_numeric(one["open"], errors="coerce").replace(0, pd.NA)
        rng = (pd.to_numeric(one["high"], errors="coerce") - pd.to_numeric(one["low"], errors="coerce")) / close.replace(0, pd.NA)
        one["rsi"] = _calc_rsi_direct(close)
        ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
        ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
        one["macd"] = (ema12 - ema26).fillna(0.0)
        one["signal"] = one["macd"].ewm(span=9, adjust=False, min_periods=1).mean().fillna(0.0)
        one["hist"] = (one["macd"] - one["signal"]).fillna(0.0)
        one["slope"] = pct.combine_first(intrabar).fillna(0.0)
        one["slope_atr_scaled"] = one["slope"]
        one["mtf"] = 0.0
        one["score_slope"] = pd.to_numeric(one["slope"], errors="coerce").fillna(0.0) * 100.0
        one["score_mtf"] = 0.0
        base = pct.combine_first(intrabar).combine_first(rng).fillna(0.0) * 100.0
        tick_bonus = pd.to_numeric(one.get("tick_count", 1), errors="coerce") if "tick_count" in one.columns else pd.Series([1] * len(one), index=one.index)
        tick_bonus = pd.Series(tick_bonus, index=one.index).fillna(1).clip(lower=1) * 0.0001
        base = base.where(base.abs() > 0, tick_bonus)
        one["score"] = base.fillna(0.0001)
        one["score_total"] = one["score"] + one["score_slope"].fillna(0.0) + one["score_mtf"].fillna(0.0)
        one["final_score"] = one["score_total"]
        one["display_score"] = one["score_total"]
        one["score_buy"] = one["score_total"].clip(lower=0)
        one["score_sell"] = (-one["score_total"]).clip(lower=0)
        one["technical_ready"] = True
        one["symbol_hist_len"] = range(1, len(one) + 1)
        one["source"] = "push_stream_direct_ohlc_fallback"
        one["interval"] = int(interval)
        parts.append(one)
    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    logger.warning("[PUSH SUMMARY ENGINE][DIRECT OHLC] built fallback interval=%s rows=%s symbols=%s latest_dt=%s", interval, len(out), _safe_symbol_count(out), _safe_latest_dt(out))
    return out.reset_index(drop=True)


def _call_summary_fn(fn: Callable, *, interval: int, label: str) -> pd.DataFrame:
    for call in (lambda: fn(interval=interval), lambda: fn(interval), lambda: fn()):
        try:
            return _safe_copy_df(call())
        except TypeError:
            continue
        except Exception:
            logger.exception("[PUSH SUMMARY ENGINE] %s failed", label)
            return pd.DataFrame()
    return pd.DataFrame()


def _run_push_summary(interval: int, *, summary_df: pd.DataFrame, push_df: pd.DataFrame) -> pd.DataFrame:
    fn = _resolve_build_incremental_summary()
    if fn is None:
        logger.warning("[PUSH SUMMARY ENGINE] build_incremental_summary unavailable")
        return pd.DataFrame()
    module_name = getattr(fn, "__module__", "")
    func_name = getattr(fn, "__name__", "")
    if module_name == "trading.summary.pipeline.summary_pipeline" and func_name == "run_summary_pipeline":
        try:
            out = fn(summary_df=summary_df, push_df=push_df, interval=interval, evaluate_signals=True, latest_only=False, recent_bars_per_symbol=120)
            out = _safe_copy_df(out)
            latest_only, ratio, one_bar, total = _is_latest_only_frame(out)
            immature, prof = _looks_immature(out)
            logger.info("[PUSH SUMMARY ENGINE] pipeline interval=%s summary_rows=%s push_rows=%s out_rows=%s latest_only=%s ratio=%.4f one_bar=%s total=%s immature=%s hist_max=%s ready_rows=%s score_nonzero=%s", interval, len(summary_df), len(push_df), len(out), latest_only, ratio, one_bar, total, immature, prof.get("hist_max", 0.0), prof.get("technical_ready_rows", 0), prof.get("score_nonzero", 0))
            if out.empty and not push_df.empty:
                logger.warning("[PUSH SUMMARY ENGINE] pipeline returned empty interval=%s push_rows=%s -> direct OHLC fallback", interval, len(push_df))
                out = _build_direct_ohlc_from_push_source(push_df, interval=interval)
            logger.info("[PUSH SUMMARY TRACE] interval=%s runner_result_rows=%s latest_dt=%s", interval, len(out), _safe_latest_dt(out))
            return out
        except Exception:
            logger.exception("[PUSH SUMMARY ENGINE] run_summary_pipeline failed interval=%s", interval)
            return pd.DataFrame()
    df = _call_summary_fn(fn, interval=interval, label="push summary")
    logger.info("[PUSH SUMMARY TRACE] interval=%s runner_result_rows=%s latest_dt=%s", interval, len(df), _safe_latest_dt(df))
    return df


def _normalize_for_source(df: pd.DataFrame, source_name: str) -> pd.DataFrame:
    out = _normalize_summary_df(df)
    if out.empty:
        return pd.DataFrame()
    if "source" not in out.columns:
        out["source"] = source_name
    else:
        out["source"] = out["source"].fillna(source_name).astype(str)
    if "datetime" not in out.columns:
        out["datetime"] = pd.NaT
    out = out[out["symbol"] != ""].copy()
    return out.reset_index(drop=True)


def _run_quality_engine(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    if run_data_quality_checks is None:
        logger.info("[PUSH SUMMARY ENGINE] data quality skipped (engine unavailable)")
        return df
    try:
        out = run_data_quality_checks(df, interval=interval)
        return out if isinstance(out, pd.DataFrame) else df
    except TypeError:
        try:
            out = run_data_quality_checks(df)
            return out if isinstance(out, pd.DataFrame) else df
        except Exception:
            logger.exception("[PUSH SUMMARY ENGINE] data quality failed (legacy)")
            return df
    except Exception:
        logger.exception("[PUSH SUMMARY ENGINE] data quality failed")
        return df


def _should_store_push_summary(df: pd.DataFrame, interval: int) -> bool:
    if not isinstance(df, pd.DataFrame) or df.empty:
        logger.warning("[PUSH SUMMARY ENGINE] store skipped: empty interval=%s", interval)
        return False
    latest_only, ratio, one_bar, total = _is_latest_only_frame(df)
    immature, prof = _looks_immature(df)
    logger.info("[PUSH SUMMARY ENGINE] store probe interval=%s latest_only=%s ratio=%.4f one_bar=%s total=%s immature=%s rows=%s symbols=%s ready_rows=%s hist_ge3=%s hist_max=%s score_nonzero=%s", interval, latest_only, ratio, one_bar, total, immature, prof.get("rows", 0), prof.get("symbols", 0), prof.get("technical_ready_rows", 0), prof.get("hist_ge3", 0), prof.get("hist_max", 0.0), prof.get("score_nonzero", 0))
    if latest_only and total <= max(3, prof.get("symbols", 0)):
        return False
    if immature and prof.get("rows", 0) <= max(5, prof.get("symbols", 0) + 2):
        return False
    return True


def _store_push_merged_summary(interval: int, df: pd.DataFrame) -> None:
    if global_data is None or not isinstance(df, pd.DataFrame):
        return
    setter = _safe_attr(global_data, "set_merged_summary", None)
    if callable(setter):
        try:
            try:
                setter(interval, df.copy(), source="push")
            except TypeError:
                setter(interval, df.copy())
        except Exception:
            logger.exception("[PUSH SUMMARY ENGINE] set_merged_summary failed interval=%s", interval)
    for name in (f"push_merged_summary_{interval}min", f"push_merged_summary_{interval}", f"merged_summary_{interval}min", f"merged_summary_{interval}", "merged_summary"):
        try:
            setattr(global_data, name, df.copy())
        except Exception:
            logger.exception("[PUSH SUMMARY ENGINE] store failed key=%s interval=%s", name, interval)


def _store_push_frame(interval: int, push_df: pd.DataFrame) -> None:
    if global_data is None:
        return
    for name in (f"push_summary_{interval}min", f"push_summary_{interval}"):
        try:
            setattr(global_data, name, push_df.copy())
        except Exception:
            logger.exception("[PUSH SUMMARY ENGINE] store failed key=%s interval=%s", name, interval)
    setter = _safe_attr(global_data, "set_push_summary", None)
    if callable(setter):
        try:
            setter(interval, push_df.copy())
        except Exception:
            logger.exception("[PUSH SUMMARY ENGINE] set_push_summary failed interval=%s", interval)


def build_summary(interval: int = 1) -> pd.DataFrame:
    """
    PUSH専用 summary engine。
    ranking へは絶対にフォールバックしない。

    Ver1.2.1:
    push_rows=0 のとき、前回/前日の push_merged_summary を再保存しない。
    これにより、前日巨大キャッシュを毎分 pipeline に渡して timeout する問題を防ぐ。
    """
    logger.info("🚀 push_summary_engine START interval=%s", interval)
    push_source_df = _resolve_push_source_df()
    if push_source_df.empty:
        empty = pd.DataFrame()
        _store_push_frame(interval=interval, push_df=empty)
        logger.warning("[PUSH SUMMARY ENGINE] push source empty -> no previous merged reuse interval=%s", interval)
        logger.info("✅ push_summary_engine END interval=%s push_rows=0 stored_new_push=False latest_dt=None", interval)
        return empty
    summary_df = _resolve_summary_source_df(interval, push_df=push_source_df)
    push_df = _run_push_summary(interval=interval, summary_df=summary_df, push_df=push_source_df)
    push_df = _normalize_for_source(push_df, "push")
    push_df = _filter_same_day(push_df, ref_df=push_source_df, today_only=True)
    push_df = _run_quality_engine(push_df, interval=interval)
    _log_df_stats("push summary", push_df)
    _store_push_frame(interval=interval, push_df=push_df)
    push_rows = len(push_df) if isinstance(push_df, pd.DataFrame) else 0
    stored_new_push = False
    if push_rows > 0 and _should_store_push_summary(push_df, interval=interval):
        _store_push_merged_summary(interval=interval, df=push_df)
        stored_new_push = True
    elif push_rows > 0:
        logger.warning("[PUSH SUMMARY ENGINE] overwrite skipped by maturity/latest-only guard interval=%s push_rows=%s", interval, push_rows)
    else:
        logger.warning("[PUSH SUMMARY ENGINE] skip PUSH merged_summary overwrite because push_rows=0 interval=%s", interval)
    logger.info("✅ push_summary_engine END interval=%s push_rows=%d stored_new_push=%s latest_dt=%s", interval, push_rows, stored_new_push, _safe_latest_dt(push_df))
    return push_df if isinstance(push_df, pd.DataFrame) else pd.DataFrame()


def build_push_summary(interval: int = 1) -> pd.DataFrame:
    return build_summary(interval=interval)


def push_summary_engine(interval: int = 1) -> pd.DataFrame:
    return build_summary(interval=interval)


def run_push_summary_engine(interval: int = 1) -> pd.DataFrame:
    return build_summary(interval=interval)


def run_summary_engine(interval: int = 1) -> pd.DataFrame:
    return build_summary(interval=interval)


def run(interval: int = 1) -> pd.DataFrame:
    return build_summary(interval=interval)


# ============================================================
# PUSH source の堅牢な列正規化 + direct OHLC フォールバック
# ------------------------------------------------------------
# 旧 core/startup/push_summary_direct_ohlc_runtime_patch.py を本文化。
# 旧パッチは _normalize_push_source_df / _build_direct_ohlc_from_push_source を
# **完全置換**しており (元関数を一度も呼ばない)、元実装は
# *_core として残してある (スコープ外プロセス用のフォールバック)。
#
# 適用スコープは旧 install 導線 (fast_startup_runtime_patch <- main.py) を
# _pdo_scope_enabled() で再現する。
#
# 【注意】この2関数を差し替える実行時パッチは他に無いことを確認済み
# (静的収集 + 変数名 setattr の目視の2段階チェック)。
# ============================================================



def _pdo_safe_copy_df(value) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    try:
        return pd.DataFrame(value).copy()
    except Exception:
        return pd.DataFrame()


def _pdo_key(s: str) -> str:
    return re.sub(r"[^0-9a-zA-Z一-龥ぁ-んァ-ヶー_]", "", str(s)).lower()


def _pdo_find_col(df: pd.DataFrame, names: Iterable[str]) -> Optional[str]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None
    direct = {str(c): c for c in df.columns}
    lowered = {str(c).lower(): c for c in df.columns}
    keyed = {_pdo_key(str(c)): c for c in df.columns}
    for name in names:
        s = str(name)
        if s in direct:
            return direct[s]
        if s.lower() in lowered:
            return lowered[s.lower()]
        k = _pdo_key(s)
        if k in keyed:
            return keyed[k]
    return None


def _pdo_num(df: pd.DataFrame, names: Iterable[str]) -> pd.Series:
    # object dtype の全 NA Series へ combine_first すると
    #   FutureWarning: The behavior of array concatenation with empty entries is deprecated
    # になる。最後に pd.to_numeric するのだから最初から float64 で作る。
    out = pd.Series(np.nan, index=df.index, dtype="float64")
    for name in names:
        c = _pdo_find_col(df, [name])
        if c is None:
            continue
        try:
            s = pd.to_numeric(df[c], errors="coerce")
            out = out.combine_first(s)
        except Exception:
            continue
    return pd.to_numeric(out, errors="coerce")


def _pdo_str_series(df: pd.DataFrame, names: Iterable[str], default: str = "") -> pd.Series:
    c = _pdo_find_col(df, names)
    if c is None:
        return pd.Series([default] * len(df), index=df.index, dtype="object")
    try:
        return df[c].astype(str)
    except Exception:
        return pd.Series([default] * len(df), index=df.index, dtype="object")


def _pdo_dt_series(df: pd.DataFrame, names: Iterable[str]) -> pd.Series:
    out = pd.Series([pd.NaT] * len(df), index=df.index, dtype="datetime64[ns]")
    for name in names:
        c = _pdo_find_col(df, [name])
        if c is None:
            continue
        try:
            s = pd.to_datetime(df[c], errors="coerce")
            try:
                s = s.dt.tz_localize(None)
            except Exception:
                pass
            out = out.combine_first(s)
        except Exception:
            continue
    return pd.to_datetime(out, errors="coerce")


def _pdo_normalize_push_source_df(df: pd.DataFrame) -> pd.DataFrame:
    raw = _pdo_safe_copy_df(df)
    if raw.empty:
        return raw

    out = raw.copy()

    # symbol
    if "symbol" not in out.columns:
        out["symbol"] = _pdo_str_series(
            out,
            [
                "symbol", "Symbol", "SYMBOL", "code", "Code", "銘柄コード",
                "symbol_code", "SymbolCode", "IssueCode", "issue_code",
            ],
        )
    out["symbol"] = (
        out["symbol"].astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )
    out = out[out["symbol"].ne("") & out["symbol"].ne("nan")].copy()
    if out.empty:
        logger.warning("[PUSH DIRECT OHLC PATCH] normalize empty after symbol filter raw_cols=%s", list(raw.columns))
        return out

    # datetime
    out["datetime"] = _pdo_dt_series(
        out,
        [
            "datetime", "Datetime", "date_time", "timestamp", "Timestamp",
            "CurrentPriceTime", "current_price_time", "currentpricetime",
            "PriceTime", "price_time", "ReceivedAt", "received_at",
            "inserted_at", "created_at", "time", "Time", "時刻", "現在値時刻",
        ],
    )
    if out["datetime"].isna().all():
        # 最終防衛: PUSHが来ているなら現在時刻でバー化する。
        out["datetime"] = pd.Timestamp.now().floor("s")
        logger.warning("[PUSH DIRECT OHLC PATCH] datetime all NaT -> filled now rows=%d", len(out))

    # price candidates
    close = _pdo_num(
        out,
        [
            "close", "close_price", "Close", "ClosePrice", "price", "Price",
            "current_price", "CurrentPrice", "currentprice", "現在値", "現値",
            "last_price", "LastPrice", "lastprice", "約定値", "ExecutionPrice",
            "price_current", "current", "last",
        ],
    )
    bid = _pdo_num(
        out,
        [
            "bid", "bid_price", "BidPrice", "BestBid", "best_bid", "Buy1Price",
            "buy1_price", "買気配", "買気配値", "最良買気配", "Bid1Price",
        ],
    )
    ask = _pdo_num(
        out,
        [
            "ask", "ask_price", "AskPrice", "BestAsk", "best_ask", "Sell1Price",
            "sell1_price", "売気配", "売気配値", "最良売気配", "Ask1Price",
        ],
    )
    mid = None
    try:
        mid = (bid + ask) / 2.0
        mid = mid.where((bid > 0) & (ask > 0))
    except Exception:
        mid = pd.Series([pd.NA] * len(out), index=out.index)

    close = close.combine_first(mid).combine_first(bid).combine_first(ask)

    open_ = _pdo_num(out, ["open", "open_price", "Open", "OpenPrice", "始値"])
    high = _pdo_num(out, ["high", "high_price", "High", "HighPrice", "高値"])
    low = _pdo_num(out, ["low", "low_price", "Low", "LowPrice", "安値"])

    close = close.combine_first(open_).combine_first(high).combine_first(low)
    open_ = open_.combine_first(close)
    high = high.combine_first(close)
    low = low.combine_first(close)

    out["close"] = pd.to_numeric(close, errors="coerce")
    out["open"] = pd.to_numeric(open_, errors="coerce")
    out["high"] = pd.to_numeric(high, errors="coerce")
    out["low"] = pd.to_numeric(low, errors="coerce")

    out["close_price"] = out["close"]
    out["open_price"] = out["open"]
    out["high_price"] = out["high"]
    out["low_price"] = out["low"]
    out["price"] = out["close"]
    out["current_price"] = out["close"]

    vol = _pdo_num(
        out,
        [
            "volume", "Volume", "trading_volume", "TradingVolume", "出来高",
            "CumVolume", "cum_volume", "last_cum_volume", "CurrentVolume",
            "current_volume", "売買数量", "TradingVolumeToday",
        ],
    )
    out["volume"] = pd.to_numeric(vol, errors="coerce").fillna(0.0)
    out["trading_volume"] = out["volume"]

    if "symbolname" not in out.columns:
        out["symbolname"] = _pdo_str_series(
            out,
            ["symbolname", "SymbolName", "symbol_name", "name", "Name", "銘柄名", "IssueName"],
            default="",
        )
    out["symbolname"] = out["symbolname"].replace({"": pd.NA, "nan": pd.NA}).fillna(out["symbol"])

    out["source"] = out.get("source", "push_stream")

    before = len(out)
    out = out.dropna(subset=["datetime", "symbol", "close"]).copy()
    out = out[pd.to_numeric(out["close"], errors="coerce") > 0].copy()

    latest = out["datetime"].max() if not out.empty else None
    logger.warning(
        "[PUSH DIRECT OHLC PATCH] normalized raw_rows=%d out_rows=%d dropped=%d cols=%d symbols=%d latest=%s price_nonnull=%d sample_cols=%s",
        len(raw),
        len(out),
        before - len(out),
        len(out.columns),
        int(out["symbol"].nunique()) if not out.empty and "symbol" in out.columns else 0,
        latest,
        int(out["close"].notna().sum()) if "close" in out.columns else 0,
        list(raw.columns)[:20],
    )
    return out.reset_index(drop=True)


def _pdo_calc_rsi_direct(close: pd.Series, period: int = 14) -> pd.Series:
    try:
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
        avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=1).mean()
        rs = avg_gain / avg_loss.replace(0, pd.NA)
        return pd.to_numeric(100 - (100 / (1 + rs)), errors="coerce").fillna(50.0)
    except Exception:
        return pd.Series([50.0] * len(close), index=close.index)


def _pdo_build_direct_ohlc_from_push_source(push_df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    ticks = _pdo_normalize_push_source_df(push_df)
    if ticks.empty:
        logger.warning("[PUSH DIRECT OHLC PATCH] direct OHLC skipped normalized empty interval=%s", interval)
        return pd.DataFrame()

    try:
        ticks["datetime"] = pd.to_datetime(ticks["datetime"], errors="coerce")
        ticks["_slot"] = ticks["datetime"].dt.floor(f"{int(interval)}min")
        ticks = ticks.sort_values(["symbol", "datetime"], kind="stable")
    except Exception:
        logger.exception("[PUSH DIRECT OHLC PATCH] slot build failed interval=%s", interval)
        return pd.DataFrame()

    def _last_nonempty(s: pd.Series):
        x = s.dropna()
        return x.iloc[-1] if not x.empty else ""

    try:
        bars = (
            ticks.groupby(["symbol", "_slot"], as_index=False)
            .agg(
                symbolname=("symbolname", _last_nonempty),
                open=("close", "first"),
                high=("close", "max"),
                low=("close", "min"),
                close=("close", "last"),
                volume=("volume", "max"),
                tick_count=("close", "count"),
                first_tick_at=("datetime", "min"),
                last_tick_at=("datetime", "max"),
            )
            .rename(columns={"_slot": "datetime"})
        )
    except Exception:
        logger.exception("[PUSH DIRECT OHLC PATCH] groupby OHLC failed interval=%s", interval)
        return pd.DataFrame()

    if bars.empty:
        return pd.DataFrame()

    for c in ("open", "high", "low", "close", "volume"):
        bars[c] = pd.to_numeric(bars[c], errors="coerce")
    bars = bars.dropna(subset=["symbol", "datetime", "close"]).copy()
    bars = bars[bars["close"] > 0].copy()
    if bars.empty:
        return pd.DataFrame()

    parts: list[pd.DataFrame] = []
    for _sym, one in bars.groupby("symbol", sort=False):
        one = one.copy().sort_values("datetime", kind="stable")
        close = pd.to_numeric(one["close"], errors="coerce")
        prev_close = close.shift(1)
        pct = (close - prev_close) / prev_close.replace(0, pd.NA)
        intrabar = (close - pd.to_numeric(one["open"], errors="coerce")) / pd.to_numeric(one["open"], errors="coerce").replace(0, pd.NA)

        one["rsi"] = _pdo_calc_rsi_direct(close)
        ema12 = close.ewm(span=12, adjust=False, min_periods=1).mean()
        ema26 = close.ewm(span=26, adjust=False, min_periods=1).mean()
        one["macd"] = (ema12 - ema26).fillna(0.0)
        one["signal"] = one["macd"].ewm(span=9, adjust=False, min_periods=1).mean().fillna(0.0)
        one["hist"] = (one["macd"] - one["signal"]).fillna(0.0)
        one["slope"] = pct.combine_first(intrabar).fillna(0.0)
        one["slope_atr_scaled"] = one["slope"]
        one["mtf"] = 0.0
        one["score_slope"] = one["slope"] * 100.0
        one["score_mtf"] = 0.0
        tick_bonus = pd.to_numeric(one.get("tick_count", 1), errors="coerce").fillna(1).clip(lower=1) * 0.0001
        base = (one["score_slope"].abs() + tick_bonus).fillna(0.0001)
        signed = base.where(one["score_slope"] >= 0, -base)
        one["score"] = signed
        one["score_total"] = signed
        one["final_score"] = signed
        one["display_score"] = signed
        one["score_buy"] = signed.clip(lower=0)
        one["score_sell"] = (-signed).clip(lower=0)
        one["technical_ready"] = True
        one["display_ready"] = True
        one["symbol_hist_len"] = range(1, len(one) + 1)
        one["source"] = "push_stream_direct_ohlc_runtime_patch"
        one["interval"] = int(interval)
        one["open_price"] = one["open"]
        one["high_price"] = one["high"]
        one["low_price"] = one["low"]
        one["close_price"] = one["close"]
        one["current_price"] = one["close"]
        one["price"] = one["close"]
        parts.append(one)

    out = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()
    logger.warning(
        "[PUSH DIRECT OHLC PATCH] built interval=%s rows=%d symbols=%d latest=%s",
        interval,
        len(out),
        int(out["symbol"].nunique()) if not out.empty and "symbol" in out.columns else 0,
        out["datetime"].max() if not out.empty and "datetime" in out.columns else None,
    )
    return out.reset_index(drop=True)


_PDO_SCOPE = None


def _pdo_scope_enabled() -> bool:
    global _PDO_SCOPE
    if _PDO_SCOPE is not None:
        return _PDO_SCOPE
    enabled = False
    try:
        import sys as _sys

        argv_text = " ".join(str(x) for x in _sys.argv).replace("\\", "/").lower()
        db_names = (
            "main_database.py", "data_collectors_runner.py", "db_prepare_runner.py",
            "push_receiver_runner.py", "yahoo_complement_runner.py",
            "summary_database_runner.py", "ranking_collector_runner.py",
        )
        if not any(n in argv_text for n in db_names):
            enabled = "main.py" in argv_text
    except Exception:
        enabled = False
    _PDO_SCOPE = enabled
    return enabled


def _normalize_push_source_df(df: pd.DataFrame) -> pd.DataFrame:
    if _pdo_scope_enabled():
        return _pdo_normalize_push_source_df(df)
    return _normalize_push_source_df_core(df)


def _build_direct_ohlc_from_push_source(push_df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    if _pdo_scope_enabled():
        return _pdo_build_direct_ohlc_from_push_source(push_df, interval=interval)
    return _build_direct_ohlc_from_push_source_core(push_df, interval=interval)
