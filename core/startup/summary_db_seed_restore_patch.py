# ============================================================
# File   : core/startup/summary_db_seed_restore_patch.py
# Version: V1.0-MAIN-SUMMARY-DB-SEED-RESTORE
# ------------------------------------------------------------
# 目的:
#   main.py は split mode / entry_only のため summary DB へ正式保存しない。
#   ただし、エントリー判定・AI判定・Discord表示では
#   main_database.py が保存した stock_summary_1min/3min/5min の履歴が必要。
#
# 修正内容:
#   - system_startup の summary_engine rebind 後に summaryYYYYMMDD.db を読む
#   - stock_summary_1min/3min/5min を GlobalContext の history cache へ復元
#   - 表示用 merged summary へ source=push / source=ranking 別に最新行を復元
#   - global_data.summary_1m_df / 3m / 5m へも履歴DFを注入
#
# 効果:
#   - main.py 側の previous PUSH merged summary rows=0 を防ぐ
#   - MACD / signal / slope / MTF / ma75 / technical_ready など
#     履歴依存項目が 0/NULL になりにくくなる
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pandas as pd

logger = logging.getLogger(__name__)

_TABLES = {
    1: "stock_summary_1min",
    3: "stock_summary_3min",
    5: "stock_summary_5min",
}

_RESTORED = False


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _engine_database_path() -> Optional[str]:
    try:
        import database.session as ds

        engine = getattr(ds, "summary_engine", None) or getattr(ds, "_summary_engine", None)
        if engine is None:
            return None

        db = getattr(getattr(engine, "url", None), "database", None)
        if db:
            return str(db)

        url = str(getattr(engine, "url", "") or "")
        if url.startswith("sqlite:///"):
            return url.replace("sqlite:///", "", 1)
    except Exception:
        logger.exception("[SUMMARY DB SEED RESTORE] resolve summary_engine database failed")
    return None


def _resolve_db_path(db_path: Any = None) -> Optional[Path]:
    try:
        if db_path:
            p = Path(str(db_path))
            return p

        env_path = os.getenv("SUMMARY_DB_SEED_RESTORE_PATH") or os.getenv("SUMMARY_DB_PATH")
        if env_path:
            return Path(env_path)

        engine_path = _engine_database_path()
        if engine_path:
            return Path(engine_path)
    except Exception:
        logger.exception("[SUMMARY DB SEED RESTORE] db path resolve failed")
    return None


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        cur = conn.execute(f'PRAGMA table_info("{table}")')
        return [str(row[1]) for row in cur.fetchall()]
    except Exception:
        logger.exception("[SUMMARY DB SEED RESTORE] PRAGMA table_info failed table=%s", table)
        return []


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        cur = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        )
        return cur.fetchone() is not None
    except Exception:
        return False


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def _read_table(conn: sqlite3.Connection, table: str, max_rows: int) -> pd.DataFrame:
    if not _table_exists(conn, table):
        logger.warning("[SUMMARY DB SEED RESTORE] table missing table=%s", table)
        return pd.DataFrame()

    cols = _table_columns(conn, table)
    if not cols:
        return pd.DataFrame()

    q_table = _quote_ident(table)
    order_col = None
    for cand in ("datetime", "end_time", "start_time", "time"):
        if cand in cols:
            order_col = cand
            break

    if order_col:
        q_order = _quote_ident(order_col)
        sql = f"SELECT * FROM (SELECT * FROM {q_table} WHERE {q_order} IS NOT NULL ORDER BY {q_order} DESC LIMIT ?) ORDER BY {q_order} ASC"
    else:
        sql = f"SELECT * FROM {q_table} LIMIT ?"

    try:
        return pd.read_sql_query(sql, conn, params=(int(max_rows),))
    except Exception:
        logger.exception("[SUMMARY DB SEED RESTORE] read_sql failed table=%s", table)
        return pd.DataFrame()


def _normalize_summary_df(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    try:
        if df is None or df.empty:
            return pd.DataFrame()

        out = df.copy()
        out = out.loc[:, ~out.columns.duplicated()].copy()

        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        else:
            return pd.DataFrame()

        # OHLC alias 補完。DBによって open_price / close_price 側だけ入る場合がある。
        alias_pairs = (
            ("open", "open_price"),
            ("high", "high_price"),
            ("low", "low_price"),
            ("close", "close_price"),
        )
        for canonical, alias in alias_pairs:
            if canonical not in out.columns and alias in out.columns:
                out[canonical] = out[alias]
            if alias not in out.columns and canonical in out.columns:
                out[alias] = out[canonical]

        if "close" not in out.columns and "price" in out.columns:
            out["close"] = out["price"]
            if "close_price" not in out.columns:
                out["close_price"] = out["price"]

        if "datetime" not in out.columns:
            if "end_time" in out.columns:
                out["datetime"] = out["end_time"]
            elif "start_time" in out.columns:
                out["datetime"] = out["start_time"]

        if "source" not in out.columns:
            out["source"] = "push"
        else:
            out["source"] = out["source"].fillna("push").astype(str).str.strip().replace({"": "push"})

        out["interval"] = interval

        for col in (
            "score", "score_total", "final_score", "display_score", "score_buy", "score_sell",
            "slope", "slope_atr_scaled", "score_slope", "mtf", "score_mtf", "mtf_score",
            "open", "high", "low", "close", "open_price", "high_price", "low_price", "close_price",
            "volume", "trading_value", "turnover", "rsi", "macd", "signal", "ma5", "ma25", "ma75",
        ):
            if col in out.columns:
                out[col] = pd.to_numeric(out[col], errors="coerce")

        if "datetime" in out.columns:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
            out = out.sort_values(["symbol", "datetime"], kind="stable")
            out = out.drop_duplicates(subset=["symbol", "datetime", "source"], keep="last")
        else:
            out = out.drop_duplicates(subset=["symbol", "source"], keep="last")

        out = out[out["symbol"].astype(str).str.strip() != ""].reset_index(drop=True)
        return out
    except Exception:
        logger.exception("[SUMMARY DB SEED RESTORE] normalize failed interval=%s", interval)
        return pd.DataFrame()


def _publish_interval(interval: int, df: pd.DataFrame) -> dict[str, Any]:
    stats: dict[str, Any] = {"interval": interval, "rows": 0, "push_rows": 0, "ranking_rows": 0}
    if df is None or df.empty:
        return stats

    try:
        from global_state import global_data
        from core.global_context.context import global_context as GC

        # 計算用履歴。source別ではなく、履歴全体を保持する。
        try:
            GC.set_summary_history(interval, df, source="db_seed")
        except Exception:
            logger.exception("[SUMMARY DB SEED RESTORE] set_summary_history failed interval=%s", interval)

        # 旧 runtime kwargs 互換用にも入れる。
        try:
            setattr(global_data, f"summary_{interval}m_df", df.copy())
        except Exception:
            pass

        src = df["source"].fillna("push").astype(str).str.lower() if "source" in df.columns else pd.Series("push", index=df.index)
        ranking_mask = src.str.contains("ranking|rank", regex=True, na=False)
        push_mask = ~ranking_mask

        push_df = df.loc[push_mask].copy()
        ranking_df = df.loc[ranking_mask].copy()

        if not push_df.empty:
            try:
                global_data.set_push_merged_summary(interval, push_df)
            except Exception:
                logger.exception("[SUMMARY DB SEED RESTORE] set_push_merged_summary failed interval=%s", interval)

        if not ranking_df.empty:
            try:
                global_data.set_ranking_merged_summary(interval, ranking_df)
            except Exception:
                logger.exception("[SUMMARY DB SEED RESTORE] set_ranking_merged_summary failed interval=%s", interval)

        stats.update(
            rows=int(len(df)),
            push_rows=int(len(push_df)),
            ranking_rows=int(len(ranking_df)),
            symbols=int(df["symbol"].nunique()) if "symbol" in df.columns else 0,
            latest_dt=str(pd.to_datetime(df["datetime"], errors="coerce").max()) if "datetime" in df.columns else None,
            macd_nonzero=int((pd.to_numeric(df.get("macd"), errors="coerce").fillna(0) != 0).sum()) if "macd" in df.columns else -1,
            mtf_nonzero=int((pd.to_numeric(df.get("mtf"), errors="coerce").fillna(0) != 0).sum()) if "mtf" in df.columns else -1,
        )
        return stats
    except Exception:
        logger.exception("[SUMMARY DB SEED RESTORE] publish failed interval=%s", interval)
        return stats


def restore_summary_db_seed(db_path: Any = None, *, force: bool = False) -> dict[str, Any]:
    """
    main_database.py が保存した summary DB を main.py メモリへ復元する。
    """
    global _RESTORED

    if not force and _RESTORED:
        logger.info("[SUMMARY DB SEED RESTORE] already restored -> skip")
        return {"ok": True, "skipped": True, "reason": "already_restored"}

    if not _env_bool("SUMMARY_DB_SEED_RESTORE_ENABLED", True):
        logger.warning("[SUMMARY DB SEED RESTORE] disabled by env SUMMARY_DB_SEED_RESTORE_ENABLED")
        return {"ok": False, "skipped": True, "reason": "disabled"}

    path = _resolve_db_path(db_path)
    if path is None:
        logger.warning("[SUMMARY DB SEED RESTORE] db path unresolved")
        return {"ok": False, "reason": "db_path_unresolved"}

    if not path.exists():
        logger.warning("[SUMMARY DB SEED RESTORE] db missing path=%s", path)
        return {"ok": False, "reason": "db_missing", "path": str(path)}

    max_rows = max(1000, _env_int("SUMMARY_DB_SEED_RESTORE_MAX_ROWS_PER_TF", 200000))
    result: dict[str, Any] = {"ok": True, "path": str(path), "max_rows": max_rows, "intervals": {}}

    try:
        logger.warning("[SUMMARY DB SEED RESTORE] start path=%s max_rows_per_tf=%s", path, max_rows)
        with sqlite3.connect(str(path), timeout=30) as conn:
            try:
                conn.execute("PRAGMA busy_timeout=30000")
                conn.execute("PRAGMA query_only=ON")
            except Exception:
                pass

            for interval, table in _TABLES.items():
                raw = _read_table(conn, table, max_rows=max_rows)
                df = _normalize_summary_df(raw, interval=interval)
                stats = _publish_interval(interval, df)
                stats["table"] = table
                stats["raw_rows"] = int(len(raw)) if isinstance(raw, pd.DataFrame) else 0
                result["intervals"][str(interval)] = stats
                logger.warning(
                    "[SUMMARY DB SEED RESTORE] interval=%sm table=%s raw_rows=%s rows=%s symbols=%s push_rows=%s ranking_rows=%s latest_dt=%s macd_nonzero=%s mtf_nonzero=%s",
                    interval,
                    table,
                    stats.get("raw_rows"),
                    stats.get("rows"),
                    stats.get("symbols"),
                    stats.get("push_rows"),
                    stats.get("ranking_rows"),
                    stats.get("latest_dt"),
                    stats.get("macd_nonzero"),
                    stats.get("mtf_nonzero"),
                )

        _RESTORED = True
        logger.warning("[SUMMARY DB SEED RESTORE] done path=%s", path)
        return result
    except Exception as e:
        logger.exception("[SUMMARY DB SEED RESTORE] failed path=%s", path)
        return {"ok": False, "reason": "exception", "error": str(e), "path": str(path)}


__all__ = ["restore_summary_db_seed"]
