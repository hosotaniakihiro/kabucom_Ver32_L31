# ============================================================
# File   : trading/summary/ranking/cache_writer.py
# Ver    : PRODUCTION-STABLE-RANKING-CACHE-WRITER-V1.0
#          -RANKING-ONLY
#          -NO-PUSH-SHARE
# ------------------------------------------------------------
# ✔ RANKING由来サマリー専用の保存処理
# ✔ PUSH系は一切参照しない
# ✔ global_data の RANKING系キーだけ保存
# ✔ ranking DB / summary table の RANKING系テーブルだけ保存
# ✔ save_ranking_summary(df, interval) を公開
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

try:
    from global_state import global_data
except Exception:
    try:
        from core.global_context.context import global_data  # type: ignore
    except Exception:
        global_data = None


# ============================================================
# basic helpers
# ============================================================

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
        for key in (
            "result_df",
            "merged_df",
            "df",
            "summary_df",
            "output_df",
            "display_df",
            "latest_df",
            "latest_summary_df",
        ):
            v = value.get(key)
            if isinstance(v, pd.DataFrame):
                return v.copy()

    try:
        return pd.DataFrame(value).copy()
    except Exception:
        return pd.DataFrame()


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


def _ensure_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()

    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
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


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_copy_df(df)
    if out.empty:
        return out

    out = _ensure_symbol(out)
    out = _ensure_datetime(out)

    if "source" not in out.columns:
        out["source"] = "ranking"
    else:
        out["source"] = out["source"].fillna("ranking").astype(str)

    out = out.dropna(subset=["symbol"], how="any")
    if "datetime" in out.columns:
        out = out.dropna(subset=["datetime"], how="all")

    return out.reset_index(drop=True)


def _safe_symbol_count(df: pd.DataFrame) -> int:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return 0
    try:
        return int(df["symbol"].astype(str).nunique())
    except Exception:
        return 0


def _safe_latest_dt(df: pd.DataFrame):
    if not isinstance(df, pd.DataFrame) or df.empty or "datetime" not in df.columns:
        return None
    try:
        s = pd.to_datetime(df["datetime"], errors="coerce")
        return s.max() if s.notna().any() else None
    except Exception:
        return None


def _log_df_state(label: str, interval: int, df: pd.DataFrame) -> None:
    logger.info(
        "[ranking.cache_writer] %s interval=%s rows=%s symbols=%s latest_dt=%s cols=%s",
        label,
        interval,
        len(df) if isinstance(df, pd.DataFrame) else 0,
        _safe_symbol_count(df),
        _safe_latest_dt(df),
        list(df.columns)[:20] if isinstance(df, pd.DataFrame) else [],
    )


# ============================================================
# global_data store
# ============================================================

def _store_to_global_data(interval: int, df: pd.DataFrame) -> None:
    if global_data is None:
        logger.info("[ranking.cache_writer] global_data unavailable interval=%s", interval)
        return

    payload = _safe_copy_df(df)

    key_candidates = [
        f"ranking_summary_{interval}min",
        f"ranking_summary_{interval}",
        f"latest_ranking_summary_{interval}min",
        f"latest_ranking_summary_{interval}",
    ]

    for key in key_candidates:
        try:
            setattr(global_data, key, payload.copy())
            logger.info(
                "[ranking.cache_writer] stored global_data key=%s interval=%s rows=%s",
                key,
                interval,
                len(payload),
            )
        except Exception:
            logger.exception("[ranking.cache_writer] setattr failed key=%s interval=%s", key, interval)

    setter = _safe_attr(global_data, "set_ranking_summary", None)
    if callable(setter):
        try:
            setter(interval, payload.copy())
            logger.info(
                "[ranking.cache_writer] set_ranking_summary success interval=%s rows=%s",
                interval,
                len(payload),
            )
        except Exception:
            logger.exception("[ranking.cache_writer] set_ranking_summary failed interval=%s", interval)

    latest_setter = _safe_attr(global_data, "set_latest_ranking_summary", None)
    if callable(latest_setter):
        try:
            latest_setter(interval, payload.copy())
            logger.info(
                "[ranking.cache_writer] set_latest_ranking_summary success interval=%s rows=%s",
                interval,
                len(payload),
            )
        except Exception:
            logger.exception("[ranking.cache_writer] set_latest_ranking_summary failed interval=%s", interval)


# ============================================================
# DB helpers
# ============================================================

def _candidate_ranking_db_paths(now=None) -> list[Path]:
    paths: list[Path] = []

    try:
        p = _safe_attr(global_data, "ranking_db_path", None)
        if p:
            paths.append(Path(str(p)))
    except Exception:
        pass

    try:
        base = Path(r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\ranking")
        ts = pd.Timestamp(now) if now is not None else pd.Timestamp.now()
        ymd = ts.strftime("%Y%m%d")
        paths.append(base / f"ranking{ymd}.db")
    except Exception:
        pass

    uniq: list[Path] = []
    seen = set()
    for p in paths:
        s = str(p)
        if s not in seen:
            seen.add(s)
            uniq.append(p)
    return uniq


def _ensure_parent_dir(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        logger.exception("[ranking.cache_writer] ensure parent dir failed path=%s", path)


def _table_name(interval: int) -> str:
    return f"ranking_summary_{int(interval)}min"


def _prepare_db_df(df: pd.DataFrame) -> pd.DataFrame:
    out = _normalize_df(df)
    if out.empty:
        return out

    db_df = out.copy()

    if "datetime" in db_df.columns:
        try:
            s = pd.to_datetime(db_df["datetime"], errors="coerce")
            db_df["datetime"] = s.dt.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    for col in db_df.columns:
        try:
            if str(db_df[col].dtype) == "bool":
                db_df[col] = db_df[col].astype("int64")
        except Exception:
            pass

    return db_df.reset_index(drop=True)


def _write_db(df: pd.DataFrame, interval: int, now=None) -> None:
    db_df = _prepare_db_df(df)
    if db_df.empty:
        logger.info("[ranking.cache_writer] db write skipped interval=%s reason=empty", interval)
        return

    table = _table_name(interval)

    for db_path in _candidate_ranking_db_paths(now=now):
        try:
            _ensure_parent_dir(db_path)

            con = sqlite3.connect(str(db_path))
            try:
                cur = con.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                con.commit()

                db_df.to_sql(table, con, if_exists="replace", index=False)

                try:
                    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_symbol ON {table}(symbol)")
                except Exception:
                    pass
                try:
                    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_datetime ON {table}(datetime)")
                except Exception:
                    pass
                try:
                    cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_symbol_datetime ON {table}(symbol, datetime)")
                except Exception:
                    pass

                con.commit()

                logger.info(
                    "[ranking.cache_writer] db write success path=%s table=%s interval=%s rows=%s",
                    db_path,
                    table,
                    interval,
                    len(db_df),
                )
                return

            finally:
                con.close()

        except Exception:
            logger.exception(
                "[ranking.cache_writer] db write failed path=%s table=%s interval=%s",
                db_path,
                table,
                interval,
            )

    logger.warning(
        "[ranking.cache_writer] all db write attempts failed interval=%s table=%s",
        interval,
        table,
    )


# ============================================================
# public
# ============================================================

def save_ranking_summary(df: pd.DataFrame, interval: int, now=None, persist_db: bool = True) -> pd.DataFrame:
    """
    RANKINGサマリー専用保存。
    - global_data の RANKING系キーへ保存
    - 必要なら ranking DB の ranking_summary_{interval}min へ保存
    - PUSH系キー / PUSH系DB は一切触らない
    """
    interval = int(interval)
    out = _normalize_df(df)

    _log_df_state("save_ranking_summary input", interval, out)

    if out.empty:
        logger.warning("[ranking.cache_writer] save_ranking_summary skipped interval=%s reason=empty", interval)
        return out

    _store_to_global_data(interval, out)

    if persist_db:
        _write_db(out, interval=interval, now=now)
    else:
        logger.info("[ranking.cache_writer] persist_db=False interval=%s", interval)

    logger.info(
        "[ranking.cache_writer] save_ranking_summary done interval=%s rows=%s symbols=%s latest_dt=%s",
        interval,
        len(out),
        _safe_symbol_count(out),
        _safe_latest_dt(out),
    )
    return out


__all__ = [
    "save_ranking_summary",
]