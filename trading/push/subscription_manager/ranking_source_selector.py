# ============================================================
# File   : trading/push/subscription_manager/ranking_source_selector.py
# Function:
#   - ranking DB から購読候補100銘柄を優先枠で選定する
#   - 値上がり率 / 値下がり率 / 売買代金 / TICK を組み合わせる
# ------------------------------------------------------------
# Version: PRODUCTION-REV1.0-RANKING-SOURCE-SELECTOR
# ============================================================

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from .ranking_source_paths import REGISTER_MAX_SYMBOLS
from .ranking_source_retention import append_unique, normalize_symbols
from .symbols import normalize_symbol

logger = logging.getLogger(__name__)

GAINERS_TOP_N = 50
LOSERS_TOP_N = 50

SLOT_GAINERS_BY_TURNOVER = 40
SLOT_LOSERS_BY_TURNOVER = 20
SLOT_TURNOVER_BY_GAINERS = 20
SLOT_TURNOVER_BY_LOSERS = 20

TURNOVER_POOL_N = 200
TICK_POOL_N = 300

PRIMARY_TABLE_CANDIDATES = (
    "ranking_snapshot_1min",
    "ranking_raw_1min",
    "ranking",
)

LEGACY_TABLE_KEYWORDS = (
    "値上がり",
    "値下がり",
    "売買代金",
    "TICK",
    "Tick",
    "tick",
    "ティック",
)


def _quote_ident(name: str) -> str:
    s = str(name).replace('"', '""')
    return f'"{s}"'


def _safe_to_numeric(s: pd.Series, default: float = np.nan) -> pd.Series:
    try:
        if s is None:
            return pd.Series(dtype="float64")

        if pd.api.types.is_numeric_dtype(s):
            return pd.to_numeric(s, errors="coerce")

        def clean_one(v: Any) -> Any:
            if v is None:
                return np.nan

            text = str(v).strip()
            if not text:
                return np.nan

            text = (
                text.replace(",", "")
                .replace("％", "")
                .replace("%", "")
                .replace("円", "")
                .replace("株", "")
                .replace("回", "")
                .replace("+", "")
            )

            mul = 1.0
            if "兆" in text:
                mul *= 1_000_000_000_000.0
                text = text.replace("兆", "")
            if "億" in text:
                mul *= 100_000_000.0
                text = text.replace("億", "")
            if "万" in text:
                mul *= 10_000.0
                text = text.replace("万", "")

            text = re.sub(r"[^0-9.\-]", "", text)
            if text in ("", "-", ".", "-."):
                return np.nan

            try:
                return float(text) * mul
            except Exception:
                return np.nan

        out = s.map(clean_one)
        out = pd.to_numeric(out, errors="coerce")
        return out.fillna(default) if not pd.isna(default) else out

    except Exception:
        logger.debug("[SUB MANAGER] numeric normalize failed", exc_info=True)
        try:
            return pd.to_numeric(s, errors="coerce")
        except Exception:
            return pd.Series(default, index=getattr(s, "index", None), dtype="float64")


def _first_col(df: pd.DataFrame, candidates: Sequence[str]) -> Optional[str]:
    if df is None or df.empty:
        return None

    cols = {str(c).lower(): str(c) for c in df.columns}

    for c in candidates:
        key = str(c).lower()
        if key in cols:
            return cols[key]

    lower_cols = [(str(c).lower(), str(c)) for c in df.columns]
    for c in candidates:
        key = str(c).lower()
        for lc, original in lower_cols:
            if key and key in lc:
                return original

    return None


def _string_series(df: pd.DataFrame, candidates: Sequence[str], default: str = "") -> pd.Series:
    col = _first_col(df, candidates)
    if col and col in df.columns:
        try:
            return df[col].fillna(default).astype(str)
        except Exception:
            pass

    return pd.Series(default, index=df.index, dtype="object")


def _number_series(df: pd.DataFrame, candidates: Sequence[str], default: float = np.nan) -> pd.Series:
    col = _first_col(df, candidates)
    if col and col in df.columns:
        return _safe_to_numeric(df[col], default=default)

    return pd.Series(default, index=df.index, dtype="float64")


def _datetime_series(df: pd.DataFrame, candidates: Sequence[str]) -> pd.Series:
    col = _first_col(df, candidates)
    if not col or col not in df.columns:
        return pd.Series(pd.NaT, index=df.index)

    try:
        return pd.to_datetime(df[col], errors="coerce")
    except Exception:
        return pd.Series(pd.NaT, index=df.index)


def _list_tables(conn: sqlite3.Connection) -> List[str]:
    try:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return [str(r[0]) for r in rows]
    except Exception:
        logger.exception("[SUB MANAGER] ranking table list failed")
        return []


def detect_existing_table(conn: sqlite3.Connection, candidates: Sequence[str]) -> Optional[str]:
    """
    旧互換用。
    """
    try:
        names = set(_list_tables(conn))
        for c in candidates:
            if c in names:
                return c
    except Exception:
        logger.exception("[SUB MANAGER] ranking table detect failed")

    return None


def _detect_read_tables(conn: sqlite3.Connection) -> List[str]:
    tables = _list_tables(conn)
    if not tables:
        return []

    primary = [t for t in PRIMARY_TABLE_CANDIDATES if t in tables]
    if primary:
        return [primary[0]]

    legacy: List[str] = []
    for t in tables:
        if any(k in str(t) for k in LEGACY_TABLE_KEYWORDS):
            legacy.append(t)

    return legacy


def _read_one_table(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    try:
        q = f"SELECT * FROM {_quote_ident(table)}"
        df = pd.read_sql_query(q, conn)
        if df is None or df.empty:
            return pd.DataFrame()

        df["_source_table"] = table
        return df

    except Exception:
        logger.exception("[SUB MANAGER] ranking table read failed table=%s", table)
        return pd.DataFrame()


def read_ranking_df_from_db(path: str) -> Tuple[pd.DataFrame, List[str]]:
    read_tables: List[str] = []

    try:
        with sqlite3.connect(path, timeout=30) as conn:
            tables = _detect_read_tables(conn)
            if not tables:
                return pd.DataFrame(), []

            frames: List[pd.DataFrame] = []
            for table in tables:
                one = _read_one_table(conn, table)
                if one.empty:
                    continue

                frames.append(one)
                read_tables.append(table)

            if not frames:
                return pd.DataFrame(), read_tables

            df = pd.concat(frames, ignore_index=True, sort=False)
            return df, read_tables

    except Exception:
        logger.exception("[SUB MANAGER] ranking read failed path=%s", path)
        return pd.DataFrame(), read_tables


def _category_text_from_type_value(v: Any) -> str:
    if v is None:
        return ""

    text = str(v).strip()
    if not text:
        return ""

    if any(k in text for k in ("値上がり", "値下がり", "売買代金", "TICK", "Tick", "ティック")):
        return text

    try:
        n = int(float(text))
        if n == 1:
            return "値上がり率"
        if n == 2:
            return "値下がり率"
    except Exception:
        pass

    return text


def _normalize_ranking_df(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    symbol_s = _string_series(
        out,
        ["symbol", "code", "銘柄コード", "コード", "Symbol", "CODE"],
        default="",
    )

    out["symbol_norm"] = symbol_s.map(lambda x: normalize_symbol(x) if str(x).strip() else "")

    out = out[out["symbol_norm"].astype(str).str.len() > 0].copy()
    if out.empty:
        return out

    category_s = _string_series(
        out,
        [
            "category",
            "ranking_category",
            "ranking_type_name",
            "ranking_name",
            "type_name",
            "kind",
            "ランキング種別",
            "種別",
        ],
        default="",
    )

    type_s = _string_series(
        out,
        ["type", "ranking_type", "rankingType", "ranking_id", "type_id", "ランキングタイプ"],
        default="",
    )

    source_table_s = _string_series(out, ["_source_table"], default="")

    category_text = category_s.astype(str).str.strip()
    type_text = type_s.map(_category_text_from_type_value)

    category_text = category_text.mask(category_text.eq(""), type_text)
    category_text = category_text.mask(category_text.eq(""), source_table_s.astype(str))

    out["category_text"] = category_text.fillna("").astype(str)

    out["rank_num"] = _number_series(
        out,
        ["rank", "ranking_rank", "順位", "rank_no", "no", "No"],
        default=np.nan,
    )

    out["change_rate_num"] = _number_series(
        out,
        [
            "change_rate",
            "chg",
            "changePercent",
            "change_percent",
            "rate",
            "騰落率",
            "前日比率",
            "値上がり率",
            "値下がり率",
        ],
        default=np.nan,
    )

    out["turnover_num"] = _number_series(
        out,
        [
            "turnover",
            "turnover_value",
            "trading_value",
            "tradingValue",
            "amount",
            "value",
            "売買代金",
        ],
        default=np.nan,
    )

    out["volume_num"] = _number_series(
        out,
        ["volume", "出来高", "出来高数"],
        default=np.nan,
    )

    out["turnover_num"] = out["turnover_num"].where(
        out["turnover_num"].notna(),
        out["volume_num"],
    )

    out["tick_num"] = _number_series(
        out,
        ["tick", "ticks", "tick_count", "tickCount", "TICK回数", "ティック回数", "約定回数"],
        default=np.nan,
    )

    out["ts"] = _datetime_series(
        out,
        ["snapshot_time", "created_at", "datetime", "updated_at", "time", "日時"],
    )

    try:
        out["_ts_sort"] = out["ts"].fillna(pd.Timestamp.min)
        out["_rank_sort"] = out["rank_num"].fillna(999999)

        out = out.sort_values(
            ["_ts_sort", "_rank_sort"],
            ascending=[False, True],
            kind="mergesort",
        ).reset_index(drop=True)

    except Exception:
        out = out.reset_index(drop=True)

    return out


def _latest_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    try:
        work = df.copy()
        work["_ts_sort"] = work["ts"].fillna(pd.Timestamp.min)
        work = work.sort_values("_ts_sort", ascending=False, kind="mergesort")

        rows: List[Dict[str, Any]] = []

        for symbol, g in work.groupby("symbol_norm", sort=False):
            if not symbol:
                continue

            first = g.iloc[0].to_dict()

            for col in ("turnover_num", "tick_num"):
                try:
                    first[col] = pd.to_numeric(g[col], errors="coerce").max()
                except Exception:
                    pass

            cr = pd.to_numeric(g["change_rate_num"], errors="coerce")
            first["max_gain_rate"] = cr.max()
            first["max_loss_rate"] = cr.min()

            try:
                first["best_rank_num"] = pd.to_numeric(g["rank_num"], errors="coerce").min()
            except Exception:
                first["best_rank_num"] = np.nan

            try:
                cats = [
                    str(x)
                    for x in g["category_text"].dropna().astype(str).unique().tolist()
                    if str(x).strip()
                ]
                first["category_joined"] = " / ".join(cats)
            except Exception:
                first["category_joined"] = str(first.get("category_text", ""))

            rows.append(first)

        return pd.DataFrame(rows)

    except Exception:
        logger.debug("[SUB MANAGER] latest per symbol failed", exc_info=True)
        try:
            return df.drop_duplicates(subset=["symbol_norm"], keep="first").copy()
        except Exception:
            return df


def _contains_category(df: pd.DataFrame, keywords: Sequence[str]) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(False, index=getattr(df, "index", None), dtype="bool")

    try:
        text = (
            df.get("category_text", "")
            .fillna("")
            .astype(str)
            + " "
            + df.get("category_joined", "")
            .fillna("")
            .astype(str)
            + " "
            + df.get("_source_table", "")
            .fillna("")
            .astype(str)
        )
    except Exception:
        text = pd.Series("", index=df.index, dtype="object")

    mask = pd.Series(False, index=df.index, dtype="bool")
    for k in keywords:
        try:
            mask = mask | text.str.contains(str(k), case=False, regex=False, na=False)
        except Exception:
            pass

    return mask


def _gainer_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    cat_mask = _contains_category(df, ["値上がり"])
    cr = pd.to_numeric(df.get("change_rate_num", np.nan), errors="coerce")

    out = df[cat_mask | (cr > 0)].copy()
    if out.empty:
        return out

    out["_rank_sort"] = pd.to_numeric(out.get("rank_num", np.nan), errors="coerce").fillna(999999)
    out["_gain_sort"] = pd.to_numeric(out.get("change_rate_num", np.nan), errors="coerce").fillna(-999999)
    out["_turnover_sort"] = pd.to_numeric(out.get("turnover_num", np.nan), errors="coerce").fillna(-1)

    out = out.sort_values(
        ["_rank_sort", "_gain_sort", "_turnover_sort"],
        ascending=[True, False, False],
        kind="mergesort",
    )

    return out.drop_duplicates(subset=["symbol_norm"], keep="first").reset_index(drop=True)


def _loser_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    cat_mask = _contains_category(df, ["値下がり"])
    cr = pd.to_numeric(df.get("change_rate_num", np.nan), errors="coerce")

    out = df[cat_mask | (cr < 0)].copy()
    if out.empty:
        return out

    out["_rank_sort"] = pd.to_numeric(out.get("rank_num", np.nan), errors="coerce").fillna(999999)
    out["_loss_sort"] = pd.to_numeric(out.get("change_rate_num", np.nan), errors="coerce").fillna(999999)
    out["_turnover_sort"] = pd.to_numeric(out.get("turnover_num", np.nan), errors="coerce").fillna(-1)

    out = out.sort_values(
        ["_rank_sort", "_loss_sort", "_turnover_sort"],
        ascending=[True, True, False],
        kind="mergesort",
    )

    return out.drop_duplicates(subset=["symbol_norm"], keep="first").reset_index(drop=True)


def _turnover_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    out["_turnover_sort"] = pd.to_numeric(out.get("turnover_num", np.nan), errors="coerce").fillna(-1)

    try:
        out["_ts_sort"] = pd.to_datetime(out.get("ts", pd.NaT), errors="coerce").fillna(pd.Timestamp.min)
    except Exception:
        out["_ts_sort"] = pd.Timestamp.min

    out = out.sort_values(
        ["_turnover_sort", "_ts_sort"],
        ascending=[False, False],
        kind="mergesort",
    )

    return out.drop_duplicates(subset=["symbol_norm"], keep="first").reset_index(drop=True)


def _tick_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()
    out["_tick_sort"] = pd.to_numeric(out.get("tick_num", np.nan), errors="coerce").fillna(-1)
    out["_turnover_sort"] = pd.to_numeric(out.get("turnover_num", np.nan), errors="coerce").fillna(-1)

    out = out.sort_values(
        ["_tick_sort", "_turnover_sort"],
        ascending=[False, False],
        kind="mergesort",
    )

    return out.drop_duplicates(subset=["symbol_norm"], keep="first").reset_index(drop=True)


def _newest_rows(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    out = df.copy()

    try:
        out["_ts_sort"] = pd.to_datetime(out.get("ts", pd.NaT), errors="coerce").fillna(pd.Timestamp.min)
    except Exception:
        out["_ts_sort"] = pd.Timestamp.min

    out["_turnover_sort"] = pd.to_numeric(out.get("turnover_num", np.nan), errors="coerce").fillna(-1)
    out["_tick_sort"] = pd.to_numeric(out.get("tick_num", np.nan), errors="coerce").fillna(-1)

    out = out.sort_values(
        ["_ts_sort", "_turnover_sort", "_tick_sort"],
        ascending=[False, False, False],
        kind="mergesort",
    )

    return out.drop_duplicates(subset=["symbol_norm"], keep="first").reset_index(drop=True)


def select_subscription_symbols_from_ranking_df(
    df: pd.DataFrame,
    *,
    limit: int = REGISTER_MAX_SYMBOLS,
    priority_symbols: Optional[Sequence[Any]] = None,
) -> List[str]:
    """
    ranking DataFrame から PUSH 登録候補を選定する。

    優先順位:
      0. priority_symbols
      1. 値上がり率上位50の中で売買代金上位40
      2. 値下がり率上位50の中で売買代金上位20
      3. 売買代金上位の中で値上がり率上位20
      4. 売買代金上位の中で値下がり率上位20
      5. TICK回数上位で補充
      6. 値上がり率上位で補充
      7. 全体新着順で補充
    """
    if limit <= 0:
        return []

    result: List[str] = []

    priority = normalize_symbols(priority_symbols)
    append_unique(result, priority, limit=limit)

    if len(result) >= limit:
        return result[:limit]

    ndf = _normalize_ranking_df(df)
    if ndf.empty:
        return result[:limit]

    latest = _latest_per_symbol(ndf)
    if latest.empty:
        latest = ndf.copy()

    gainers = _gainer_rows(ndf)
    losers = _loser_rows(ndf)
    turnover = _turnover_rows(latest)
    ticks = _tick_rows(latest)
    newest = _newest_rows(latest)

    try:
        g_top50 = gainers.head(GAINERS_TOP_N).copy()
        if not g_top50.empty:
            g_top50["_turnover_sort"] = pd.to_numeric(
                g_top50.get("turnover_num", np.nan),
                errors="coerce",
            ).fillna(-1)
            g_top50["_tick_sort"] = pd.to_numeric(
                g_top50.get("tick_num", np.nan),
                errors="coerce",
            ).fillna(-1)

            g_pick = g_top50.sort_values(
                ["_turnover_sort", "_tick_sort"],
                ascending=[False, False],
                kind="mergesort",
            ).head(SLOT_GAINERS_BY_TURNOVER)

            append_unique(result, g_pick["symbol_norm"].tolist(), limit=limit)
    except Exception:
        logger.debug("[SUB MANAGER] select gainers-by-turnover failed", exc_info=True)

    try:
        l_top50 = losers.head(LOSERS_TOP_N).copy()
        if not l_top50.empty:
            l_top50["_turnover_sort"] = pd.to_numeric(
                l_top50.get("turnover_num", np.nan),
                errors="coerce",
            ).fillna(-1)
            l_top50["_tick_sort"] = pd.to_numeric(
                l_top50.get("tick_num", np.nan),
                errors="coerce",
            ).fillna(-1)

            l_pick = l_top50.sort_values(
                ["_turnover_sort", "_tick_sort"],
                ascending=[False, False],
                kind="mergesort",
            ).head(SLOT_LOSERS_BY_TURNOVER)

            append_unique(result, l_pick["symbol_norm"].tolist(), limit=limit)
    except Exception:
        logger.debug("[SUB MANAGER] select losers-by-turnover failed", exc_info=True)

    try:
        t_pool = turnover.head(TURNOVER_POOL_N).copy()
        if not t_pool.empty:
            gain_col = t_pool.get("max_gain_rate", t_pool.get("change_rate_num", np.nan))
            t_gain = t_pool[pd.to_numeric(gain_col, errors="coerce") > 0].copy()

            if not t_gain.empty:
                t_gain["_gain_sort"] = pd.to_numeric(
                    t_gain.get("max_gain_rate", t_gain.get("change_rate_num", np.nan)),
                    errors="coerce",
                ).fillna(-999999)
                t_gain["_turnover_sort"] = pd.to_numeric(
                    t_gain.get("turnover_num", np.nan),
                    errors="coerce",
                ).fillna(-1)
                t_gain["_tick_sort"] = pd.to_numeric(
                    t_gain.get("tick_num", np.nan),
                    errors="coerce",
                ).fillna(-1)

                t_gain = t_gain.sort_values(
                    ["_gain_sort", "_turnover_sort", "_tick_sort"],
                    ascending=[False, False, False],
                    kind="mergesort",
                ).head(SLOT_TURNOVER_BY_GAINERS)

                append_unique(result, t_gain["symbol_norm"].tolist(), limit=limit)
    except Exception:
        logger.debug("[SUB MANAGER] select turnover-by-gainers failed", exc_info=True)

    try:
        t_pool = turnover.head(TURNOVER_POOL_N).copy()
        if not t_pool.empty:
            loss_col = t_pool.get("max_loss_rate", t_pool.get("change_rate_num", np.nan))
            t_loss = t_pool[pd.to_numeric(loss_col, errors="coerce") < 0].copy()

            if not t_loss.empty:
                t_loss["_loss_sort"] = pd.to_numeric(
                    t_loss.get("max_loss_rate", t_loss.get("change_rate_num", np.nan)),
                    errors="coerce",
                ).fillna(999999)
                t_loss["_turnover_sort"] = pd.to_numeric(
                    t_loss.get("turnover_num", np.nan),
                    errors="coerce",
                ).fillna(-1)
                t_loss["_tick_sort"] = pd.to_numeric(
                    t_loss.get("tick_num", np.nan),
                    errors="coerce",
                ).fillna(-1)

                t_loss = t_loss.sort_values(
                    ["_loss_sort", "_turnover_sort", "_tick_sort"],
                    ascending=[True, False, False],
                    kind="mergesort",
                ).head(SLOT_TURNOVER_BY_LOSERS)

                append_unique(result, t_loss["symbol_norm"].tolist(), limit=limit)
    except Exception:
        logger.debug("[SUB MANAGER] select turnover-by-losers failed", exc_info=True)

    try:
        if len(result) < limit and not ticks.empty:
            append_unique(result, ticks.head(TICK_POOL_N)["symbol_norm"].tolist(), limit=limit)
    except Exception:
        logger.debug("[SUB MANAGER] select tick fill failed", exc_info=True)

    try:
        if len(result) < limit and not gainers.empty:
            append_unique(result, gainers["symbol_norm"].tolist(), limit=limit)
    except Exception:
        logger.debug("[SUB MANAGER] select gainer fill failed", exc_info=True)

    try:
        if len(result) < limit and not newest.empty:
            append_unique(result, newest["symbol_norm"].tolist(), limit=limit)
    except Exception:
        logger.debug("[SUB MANAGER] select newest fill failed", exc_info=True)

    logger.info(
        "[SUB MANAGER] ranking priority selection result=%d priority=%d gainers=%d losers=%d turnover=%d ticks=%d",
        len(result),
        len(priority),
        0 if gainers is None else len(gainers),
        0 if losers is None else len(losers),
        0 if turnover is None else len(turnover),
        0 if ticks is None else len(ticks),
    )

    return result[:limit]