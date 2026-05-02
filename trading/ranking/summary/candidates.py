# ============================================================
# File   : trading/ranking/summary/candidates.py
# Version: PRODUCTION-STABLE-REV1.0
# Purpose:
#   ranking_summary bootstrap 用 candidates 互換モジュール
#
# Features:
#   - ranking_snapshot_1min から候補銘柄をロード
#   - symbol / symbolname / datetime / price / volume を正規化
#   - 既存 bootstrap_loader.py からの import 互換を広めに保持
#   - 古い import 名でも落ちにくい fallback __getattr__ を提供
#
# Notes:
#   - このモジュールは発注しない
#   - PUSH由来summaryとは混ぜない
#   - ranking由来summaryの候補抽出専用
# ============================================================

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# constants
# ============================================================

# ============================================================
# constants
# ============================================================

DEFAULT_SNAPSHOT_TABLE = "ranking_snapshot_1min"

# ------------------------------------------------------------
# bootstrap_loader.py 互換用デフォルト定数
# ------------------------------------------------------------
# 買い候補として最低限ほしい buy score
DEFAULT_MIN_BUY_SCORE = 5.0

# 売りスコアがこれを超える銘柄は除外
DEFAULT_MAX_SELL_SCORE = 2.0

# 最低価格。低位すぎる銘柄除外用
DEFAULT_MIN_PRICE = 1.0

# 最低出来高。ranking由来では volume が無い/弱い場合もあるので緩め
DEFAULT_MIN_VOLUME = 0.0

# 候補数
DEFAULT_MAX_CANDIDATES = 100
DEFAULT_TOP_N = 100

# AI / summary 側互換
DEFAULT_MIN_CONFIDENCE = 0.65
DEFAULT_MIN_SCORE = 0.0

# ranking snapshot 読み込み件数
DEFAULT_READ_LIMIT = 50000

SYMBOL_COL_CANDIDATES = [
    "symbol",
    "code",
    "Code",
    "銘柄コード",
    "コード",
]

NAME_COL_CANDIDATES = [
    "symbolname",
    "name",
    "SymbolName",
    "銘柄名",
    "名称",
]

DATETIME_COL_CANDIDATES = [
    "datetime",
    "dt",
    "timestamp",
    "created_at",
    "time",
    "日時",
]

PRICE_COL_CANDIDATES = [
    "current_price",
    "price",
    "close",
    "close_price",
    "現在値",
    "株価",
]

VOLUME_COL_CANDIDATES = [
    "volume",
    "出来高",
    "売買高",
]

RANK_COL_CANDIDATES = [
    "rank",
    "ranking",
    "順位",
]

CATEGORY_COL_CANDIDATES = [
    "category",
    "ranking_type",
    "type",
    "market",
    "source_category",
    "カテゴリ",
    "ランキング種別",
]


# ============================================================
# small helpers
# ============================================================

def _as_path(path: str | Path | None) -> Optional[Path]:
    if path is None:
        return None
    try:
        return Path(path)
    except Exception:
        return None


def _pick_col(df: pd.DataFrame, candidates: Sequence[str]) -> Optional[str]:
    if df is None or df.empty:
        return None

    cols = list(df.columns)
    lower_map = {str(c).lower(): c for c in cols}

    for c in candidates:
        if c in df.columns:
            return c

    for c in candidates:
        hit = lower_map.get(str(c).lower())
        if hit is not None:
            return hit

    return None


def _safe_to_datetime(s: pd.Series) -> pd.Series:
    try:
        return pd.to_datetime(s, errors="coerce")
    except Exception:
        return pd.Series(pd.NaT, index=s.index)


def _safe_numeric(s: pd.Series, default: float = 0.0) -> pd.Series:
    try:
        return pd.to_numeric(s, errors="coerce").fillna(default)
    except Exception:
        return pd.Series(default, index=s.index)


def _normalize_symbol_value(v: Any) -> str:
    if v is None:
        return ""

    text = str(v).strip()
    if not text:
        return ""

    # 7203.0 のような値を 7203 に寄せる
    if text.endswith(".0"):
        text = text[:-2]

    # Yahoo形式 7203.T を 7203 に寄せる
    if "." in text:
        head = text.split(".", 1)[0]
        if head.isdigit():
            text = head

    # 数字だけなら4桁ゼロ埋め
    if text.isdigit():
        text = text.zfill(4)

    return text


def normalize_symbol_series(s: pd.Series) -> pd.Series:
    return s.map(_normalize_symbol_value)


def _table_exists(con: sqlite3.Connection, table: str) -> bool:
    try:
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _list_tables(con: sqlite3.Connection) -> list[str]:
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        return [str(r[0]) for r in rows]
    except Exception:
        return []


def find_ranking_snapshot_table(
    db_path: str | Path,
    preferred_table: str = DEFAULT_SNAPSHOT_TABLE,
) -> Optional[str]:
    """
    ranking DB 内の snapshot テーブル名を探す。

    優先:
      1. ranking_snapshot_1min
      2. snapshot を含むテーブル
      3. ranking を含み、raw/ma ではないテーブル
    """
    path = _as_path(db_path)
    if path is None or not path.exists():
        logger.warning("[RANKING CANDIDATES] db not found path=%s", db_path)
        return None

    try:
        with sqlite3.connect(str(path), timeout=10.0) as con:
            if _table_exists(con, preferred_table):
                return preferred_table

            tables = _list_tables(con)

            for t in tables:
                lt = t.lower()
                if "snapshot" in lt:
                    return t

            for t in tables:
                lt = t.lower()
                if "ranking" in lt and "raw" not in lt and "ma" not in lt:
                    return t

    except Exception:
        logger.exception("[RANKING CANDIDATES] find table failed path=%s", db_path)

    return None


def _read_table_recent(
    db_path: str | Path,
    table: str,
    *,
    limit: int = 50000,
) -> pd.DataFrame:
    """
    テーブルを安全に読む。
    datetime列がある場合は新しい順、ない場合は rowid 新しい順。
    """
    path = _as_path(db_path)
    if path is None or not path.exists():
        logger.warning("[RANKING CANDIDATES] db not found path=%s", db_path)
        return pd.DataFrame()

    try:
        with sqlite3.connect(str(path), timeout=10.0) as con:
            pragma = pd.read_sql_query(f"PRAGMA table_info({table})", con)
            cols = pragma["name"].astype(str).tolist() if not pragma.empty else []

            dt_col = None
            for c in DATETIME_COL_CANDIDATES:
                if c in cols:
                    dt_col = c
                    break

            if dt_col:
                sql = f"""
                    SELECT *
                    FROM {table}
                    ORDER BY datetime({dt_col}) DESC
                    LIMIT ?
                """
            else:
                sql = f"""
                    SELECT *
                    FROM {table}
                    ORDER BY rowid DESC
                    LIMIT ?
                """

            return pd.read_sql_query(sql, con, params=(int(limit),))

    except Exception:
        logger.exception(
            "[RANKING CANDIDATES] read table failed path=%s table=%s",
            db_path,
            table,
        )
        return pd.DataFrame()


# ============================================================
# normalize
# ============================================================

def normalize_ranking_candidates_df(
    df: pd.DataFrame,
    *,
    max_candidates: Optional[int] = None,
    latest_only: bool = False,
) -> pd.DataFrame:
    """
    ranking snapshot / raw df を候補DFとして正規化する。

    出力列:
      - symbol
      - symbolname
      - datetime
      - current_price
      - volume
      - rank
      - category
    """
    if df is None or df.empty:
        return pd.DataFrame(
            columns=[
                "symbol",
                "symbolname",
                "datetime",
                "current_price",
                "volume",
                "rank",
                "category",
            ]
        )

    work = df.copy()

    symbol_col = _pick_col(work, SYMBOL_COL_CANDIDATES)
    if symbol_col is None:
        logger.warning(
            "[RANKING CANDIDATES] symbol column not found columns=%s",
            list(work.columns),
        )
        return pd.DataFrame(
            columns=[
                "symbol",
                "symbolname",
                "datetime",
                "current_price",
                "volume",
                "rank",
                "category",
            ]
        )

    name_col = _pick_col(work, NAME_COL_CANDIDATES)
    dt_col = _pick_col(work, DATETIME_COL_CANDIDATES)
    price_col = _pick_col(work, PRICE_COL_CANDIDATES)
    volume_col = _pick_col(work, VOLUME_COL_CANDIDATES)
    rank_col = _pick_col(work, RANK_COL_CANDIDATES)
    category_col = _pick_col(work, CATEGORY_COL_CANDIDATES)

    out = pd.DataFrame(index=work.index)

    out["symbol"] = normalize_symbol_series(work[symbol_col])
    out = out[out["symbol"].astype(str).str.len() > 0].copy()

    if out.empty:
        return out

    work = work.loc[out.index].copy()

    if name_col:
        out["symbolname"] = work[name_col].astype(str).fillna("")
    else:
        out["symbolname"] = ""

    if dt_col:
        out["datetime"] = _safe_to_datetime(work[dt_col])
    else:
        out["datetime"] = pd.NaT

    if price_col:
        out["current_price"] = _safe_numeric(work[price_col], default=0.0)
    else:
        out["current_price"] = 0.0

    if volume_col:
        out["volume"] = _safe_numeric(work[volume_col], default=0.0)
    else:
        out["volume"] = 0.0

    if rank_col:
        out["rank"] = _safe_numeric(work[rank_col], default=999999)
    else:
        out["rank"] = 999999

    if category_col:
        out["category"] = work[category_col].astype(str).fillna("")
    else:
        out["category"] = ""

    # datetimeがある場合、最新のみ抽出も可能
    if latest_only and out["datetime"].notna().any():
        latest_dt = out["datetime"].max()
        out = out[out["datetime"] == latest_dt].copy()

    # 同一symbolは rank が良いもの、datetime が新しいものを優先
    sort_cols = []
    ascending = []

    if "datetime" in out.columns:
        sort_cols.append("datetime")
        ascending.append(False)

    if "rank" in out.columns:
        sort_cols.append("rank")
        ascending.append(True)

    if sort_cols:
        out = out.sort_values(sort_cols, ascending=ascending)

    out = out.drop_duplicates(subset=["symbol"], keep="first").copy()

    if max_candidates is not None and int(max_candidates) > 0:
        out = out.head(int(max_candidates)).copy()

    out = out.reset_index(drop=True)

    logger.info(
        "[RANKING CANDIDATES] normalized rows=%s symbols=%s latest=%s",
        len(out),
        out["symbol"].nunique() if "symbol" in out.columns else 0,
        out["datetime"].max() if "datetime" in out.columns and out["datetime"].notna().any() else None,
    )

    return out


# alias
normalize_candidates_df = normalize_ranking_candidates_df
normalize_candidates = normalize_ranking_candidates_df


# ============================================================
# public loaders
# ============================================================

def load_latest_ranking_snapshot_candidates(
    ranking_db_path: str | Path,
    *,
    table: str = DEFAULT_SNAPSHOT_TABLE,
    max_candidates: int = 100,
    limit: int = 50000,
    latest_only: bool = True,
) -> pd.DataFrame:
    """
    ranking_snapshot_1min から最新候補を読む。
    """
    path = _as_path(ranking_db_path)
    if path is None or not path.exists():
        logger.warning(
            "[RANKING CANDIDATES] ranking db not found path=%s",
            ranking_db_path,
        )
        return normalize_ranking_candidates_df(pd.DataFrame())

    actual_table = find_ranking_snapshot_table(path, preferred_table=table)
    if not actual_table:
        logger.warning(
            "[RANKING CANDIDATES] snapshot table not found path=%s preferred=%s",
            ranking_db_path,
            table,
        )
        return normalize_ranking_candidates_df(pd.DataFrame())

    raw = _read_table_recent(path, actual_table, limit=limit)

    out = normalize_ranking_candidates_df(
        raw,
        max_candidates=max_candidates,
        latest_only=latest_only,
    )

    logger.info(
        "[RANKING CANDIDATES] loaded path=%s table=%s raw_rows=%s out_rows=%s",
        ranking_db_path,
        actual_table,
        len(raw),
        len(out),
    )

    return out


def load_ranking_candidates(
    ranking_db_path: str | Path,
    *,
    max_candidates: int = 100,
    table: str = DEFAULT_SNAPSHOT_TABLE,
    latest_only: bool = True,
    **_: Any,
) -> pd.DataFrame:
    return load_latest_ranking_snapshot_candidates(
        ranking_db_path,
        table=table,
        max_candidates=max_candidates,
        latest_only=latest_only,
    )


def load_candidates(
    ranking_db_path: str | Path,
    *,
    max_candidates: int = 100,
    table: str = DEFAULT_SNAPSHOT_TABLE,
    latest_only: bool = True,
    **kwargs: Any,
) -> pd.DataFrame:
    return load_ranking_candidates(
        ranking_db_path,
        max_candidates=max_candidates,
        table=table,
        latest_only=latest_only,
        **kwargs,
    )


def get_ranking_candidates(
    ranking_db_path: str | Path,
    *,
    max_candidates: int = 100,
    table: str = DEFAULT_SNAPSHOT_TABLE,
    latest_only: bool = True,
    **kwargs: Any,
) -> pd.DataFrame:
    return load_ranking_candidates(
        ranking_db_path,
        max_candidates=max_candidates,
        table=table,
        latest_only=latest_only,
        **kwargs,
    )


def load_tonosama_candidates(
    ranking_db_path: str | Path,
    *,
    max_candidates: int = 100,
    table: str = DEFAULT_SNAPSHOT_TABLE,
    latest_only: bool = True,
    **kwargs: Any,
) -> pd.DataFrame:
    """
    殿様イナゴ用候補。
    現時点では ranking 最新候補と同じ。
    """
    return load_ranking_candidates(
        ranking_db_path,
        max_candidates=max_candidates,
        table=table,
        latest_only=latest_only,
        **kwargs,
    )


def build_ranking_candidates_from_snapshot(
    snapshot_df: pd.DataFrame,
    *,
    max_candidates: int = 100,
    latest_only: bool = False,
    **_: Any,
) -> pd.DataFrame:
    """
    既にロード済みの ranking snapshot df から候補を作る。
    """
    return normalize_ranking_candidates_df(
        snapshot_df,
        max_candidates=max_candidates,
        latest_only=latest_only,
    )


def build_candidates_from_snapshot(
    snapshot_df: pd.DataFrame,
    *,
    max_candidates: int = 100,
    latest_only: bool = False,
    **kwargs: Any,
) -> pd.DataFrame:
    return build_ranking_candidates_from_snapshot(
        snapshot_df,
        max_candidates=max_candidates,
        latest_only=latest_only,
        **kwargs,
    )


def build_candidates(
    snapshot_df: pd.DataFrame,
    *,
    max_candidates: int = 100,
    latest_only: bool = False,
    **kwargs: Any,
) -> pd.DataFrame:
    return build_ranking_candidates_from_snapshot(
        snapshot_df,
        max_candidates=max_candidates,
        latest_only=latest_only,
        **kwargs,
    )


def extract_candidate_symbols(
    candidates: pd.DataFrame | Iterable[Any],
    *,
    max_candidates: Optional[int] = None,
) -> list[str]:
    """
    候補DFまたはlistから symbol list を取り出す。
    """
    symbols: list[str] = []

    if candidates is None:
        return symbols

    if isinstance(candidates, pd.DataFrame):
        if candidates.empty:
            return symbols

        col = _pick_col(candidates, ["symbol", "code", "Code", "銘柄コード"])
        if col is None:
            return symbols

        values = candidates[col].tolist()
    else:
        values = list(candidates)

    for v in values:
        s = _normalize_symbol_value(v)
        if s and s not in symbols:
            symbols.append(s)

        if max_candidates is not None and len(symbols) >= int(max_candidates):
            break

    return symbols


def load_candidate_symbols(
    ranking_db_path: str | Path,
    *,
    max_candidates: int = 100,
    table: str = DEFAULT_SNAPSHOT_TABLE,
    latest_only: bool = True,
    **kwargs: Any,
) -> list[str]:
    df = load_ranking_candidates(
        ranking_db_path,
        max_candidates=max_candidates,
        table=table,
        latest_only=latest_only,
        **kwargs,
    )
    return extract_candidate_symbols(df, max_candidates=max_candidates)


def get_candidate_symbols(
    ranking_db_path: str | Path,
    *,
    max_candidates: int = 100,
    table: str = DEFAULT_SNAPSHOT_TABLE,
    latest_only: bool = True,
    **kwargs: Any,
) -> list[str]:
    return load_candidate_symbols(
        ranking_db_path,
        max_candidates=max_candidates,
        table=table,
        latest_only=latest_only,
        **kwargs,
    )


def select_candidate_symbols(
    candidates: pd.DataFrame | Iterable[Any],
    *,
    max_candidates: Optional[int] = None,
    **_: Any,
) -> list[str]:
    return extract_candidate_symbols(candidates, max_candidates=max_candidates)


def filter_summary_by_candidates(
    summary_df: pd.DataFrame,
    candidates: pd.DataFrame | Iterable[Any],
    *,
    max_candidates: Optional[int] = None,
    **_: Any,
) -> pd.DataFrame:
    """
    summary_df を candidates の symbol のみに絞る。
    """
    if summary_df is None or summary_df.empty:
        return pd.DataFrame() if summary_df is None else summary_df.copy()

    symbols = set(extract_candidate_symbols(candidates, max_candidates=max_candidates))
    if not symbols:
        return summary_df.iloc[0:0].copy()

    col = _pick_col(summary_df, ["symbol", "code", "Code", "銘柄コード"])
    if col is None:
        logger.warning(
            "[RANKING CANDIDATES] summary symbol column not found columns=%s",
            list(summary_df.columns),
        )
        return summary_df.iloc[0:0].copy()

    work = summary_df.copy()
    norm = normalize_symbol_series(work[col])
    out = work[norm.isin(symbols)].copy()

    logger.info(
        "[RANKING CANDIDATES] filter summary by candidates before=%s after=%s symbols=%s",
        len(summary_df),
        len(out),
        len(symbols),
    )

    return out


def get_latest_snapshot_datetime(
    ranking_db_path: str | Path,
    *,
    table: str = DEFAULT_SNAPSHOT_TABLE,
) -> Optional[pd.Timestamp]:
    path = _as_path(ranking_db_path)
    if path is None or not path.exists():
        return None

    actual_table = find_ranking_snapshot_table(path, preferred_table=table)
    if not actual_table:
        return None

    try:
        with sqlite3.connect(str(path), timeout=10.0) as con:
            pragma = pd.read_sql_query(f"PRAGMA table_info({actual_table})", con)
            cols = pragma["name"].astype(str).tolist() if not pragma.empty else []

            dt_col = None
            for c in DATETIME_COL_CANDIDATES:
                if c in cols:
                    dt_col = c
                    break

            if not dt_col:
                return None

            row = con.execute(
                f"SELECT MAX(datetime({dt_col})) FROM {actual_table}"
            ).fetchone()

            if not row or row[0] is None:
                return None

            ts = pd.to_datetime(row[0], errors="coerce")
            if pd.isna(ts):
                return None

            return ts

    except Exception:
        logger.exception(
            "[RANKING CANDIDATES] latest datetime failed path=%s table=%s",
            ranking_db_path,
            actual_table,
        )
        return None


# ============================================================
# compatibility fallback
# ============================================================

def _generic_candidate_loader(*args: Any, **kwargs: Any) -> pd.DataFrame:
    """
    bootstrap_loader.py 側に古い関数名 import が残っていても、
    起動停止させないための互換 fallback。

    第1引数または ranking_db_path/db_path/path からDBパスを探す。
    DataFrame が渡された場合は正規化して返す。
    """
    max_candidates = int(
        kwargs.get("max_candidates")
        or kwargs.get("top_n")
        or kwargs.get("limit")
        or 100
    )

    latest_only = bool(kwargs.get("latest_only", True))

    for a in args:
        if isinstance(a, pd.DataFrame):
            return normalize_ranking_candidates_df(
                a,
                max_candidates=max_candidates,
                latest_only=False,
            )

    path = (
        kwargs.get("ranking_db_path")
        or kwargs.get("db_path")
        or kwargs.get("path")
        or kwargs.get("ranking_path")
    )

    if path is None and args:
        first = args[0]
        if isinstance(first, (str, Path)):
            path = first

    if path is None:
        logger.warning(
            "[RANKING CANDIDATES] generic loader called without db path args=%s kwargs_keys=%s",
            len(args),
            list(kwargs.keys()),
        )
        return normalize_ranking_candidates_df(pd.DataFrame())

    return load_ranking_candidates(
        path,
        max_candidates=max_candidates,
        latest_only=latest_only,
    )


def _generic_symbol_loader(*args: Any, **kwargs: Any) -> list[str]:
    df = _generic_candidate_loader(*args, **kwargs)
    max_candidates = kwargs.get("max_candidates") or kwargs.get("top_n") or kwargs.get("limit")
    return extract_candidate_symbols(
        df,
        max_candidates=int(max_candidates) if max_candidates else None,
    )


def __getattr__(name: str) -> Any:
    """
    古い bootstrap_loader.py が想定している関数名が多少違っても、
    ModuleNotFoundError / ImportError で起動停止しないようにする。

    例:
      from .candidates import load_bootstrap_candidates
      from .candidates import get_candidate_symbols_from_ranking
    """
    lower = name.lower()

    if "symbol" in lower:
        return _generic_symbol_loader

    if (
        lower.startswith("load_")
        or lower.startswith("get_")
        or lower.startswith("build_")
        or lower.startswith("make_")
        or lower.startswith("collect_")
        or lower.startswith("select_")
        or lower.startswith("resolve_")
        or lower.startswith("ensure_")
    ):
        return _generic_candidate_loader

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "DEFAULT_SNAPSHOT_TABLE",
    "find_ranking_snapshot_table",
    "normalize_ranking_candidates_df",
    "normalize_candidates_df",
    "normalize_candidates",
    "load_latest_ranking_snapshot_candidates",
    "load_ranking_candidates",
    "load_candidates",
    "get_ranking_candidates",
    "load_tonosama_candidates",
    "build_ranking_candidates_from_snapshot",
    "build_candidates_from_snapshot",
    "build_candidates",
    "extract_candidate_symbols",
    "load_candidate_symbols",
    "get_candidate_symbols",
    "select_candidate_symbols",
    "filter_summary_by_candidates",
    "get_latest_snapshot_datetime",
    "DEFAULT_MIN_BUY_SCORE",
    "DEFAULT_MAX_SELL_SCORE",
    "DEFAULT_MIN_PRICE",
    "DEFAULT_MIN_VOLUME",
    "DEFAULT_MAX_CANDIDATES",
    "DEFAULT_TOP_N",
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_MIN_SCORE",
    "DEFAULT_READ_LIMIT",
]