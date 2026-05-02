# ============================================================
# File   : trading/summary/recovery/persistence_pkg/db_normalizer.py
# Ver    : PRODUCTION-STABLE-REV9.1-DB-NORMALIZER-KEY-GUARD
# ------------------------------------------------------------
# 【概要】
#   summary DB 保存前の列正規化
#
# 【主な機能】
#   - DB保存前DataFrame安全化
#   - duplicate column coalesce
#   - datetime復元
#   - numeric正規化
#   - OHLCV alias正規化
#   - score alias正規化
#   - symbol / symbolname正規化
#   - DB保存対象列だけへ絞り込み
#
# 【REV9.1 修正】
#   - datetime_utils REV9.1 の復元処理を前提に key guard を追加
#   - symbol / datetime が無いDataFrameをUPSERTへ渡さない
#   - datetime NaT行を保存前に明示drop
#   - keep_cols絞り込み前後で key列を検証
#   - rows dropped after table-column filter の原因を事前ログ化
#
# 【重要】
#   - UPSERT key は symbol + datetime
#   - ここで key が壊れた行を通すと upsert_executor 側で
#       rows dropped after column filter
#       no rows after table-column filter
#     が出る
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from .constants import NUMERIC_COLS, SUMMARY_DB_COLUMNS, DB_DROP_COLS
from .column_utils import (
    safe_df,
    normalize_symbol_value,
    coalesce_duplicate_columns,
    coalesce_into_column,
)
from .datetime_utils import (
    normalize_datetime_like,
    normalize_date_columns_for_db,
    parse_datetime_series_safely,
)
from .symbol_utils import normalize_text_aliases_for_db, resolve_symbolname_series
from .score_utils import ensure_score_columns, repair_mtf_consistency

logger = logging.getLogger(__name__)


# ============================================================
# numeric
# ============================================================

def normalize_numeric_like(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    for c in NUMERIC_COLS:
        if c in out.columns:
            try:
                out[c] = pd.to_numeric(out[c], errors="coerce")
            except Exception:
                pass

    return out


# ============================================================
# aliases
# ============================================================

def normalize_ohlcv_aliases_for_db(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    out = coalesce_into_column(out, dst="open", sources=["open", "open_price"], numeric=True)
    out = coalesce_into_column(out, dst="high", sources=["high", "high_price"], numeric=True)
    out = coalesce_into_column(out, dst="low", sources=["low", "low_price"], numeric=True)

    out = coalesce_into_column(
        out,
        dst="close",
        sources=[
            "close",
            "close_price",
            "price",
            "current_price",
            "currentprice",
            "last_price",
            "lastprice",
            "CurrentPrice",
            "LastPrice",
        ],
        numeric=True,
    )

    out = coalesce_into_column(out, dst="open_price", sources=["open_price", "open"], numeric=True)
    out = coalesce_into_column(out, dst="high_price", sources=["high_price", "high"], numeric=True)
    out = coalesce_into_column(out, dst="low_price", sources=["low_price", "low"], numeric=True)
    out = coalesce_into_column(out, dst="close_price", sources=["close_price", "close"], numeric=True)

    out = coalesce_into_column(
        out,
        dst="volume",
        sources=[
            "volume",
            "trading_volume",
            "tradingvolume",
            "TradingVolume",
            "last_cum_volume",
        ],
        numeric=True,
    )

    return out


def normalize_score_aliases_for_db(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()

    out = coalesce_into_column(out, dst="base", sources=["base", "score_base"], numeric=True)
    out = coalesce_into_column(out, dst="trend", sources=["trend", "score_trend"], numeric=True)
    out = coalesce_into_column(out, dst="mom", sources=["mom", "score_momentum", "momentum_score"], numeric=True)
    out = coalesce_into_column(out, dst="vel", sources=["vel", "score_velocity"], numeric=True)
    out = coalesce_into_column(out, dst="pen", sources=["pen", "direction_penalty"], numeric=True)

    out = coalesce_into_column(
        out,
        dst="score",
        sources=["score", "score_total", "display_score", "final_score", "combined_score"],
        numeric=True,
    )
    out = coalesce_into_column(
        out,
        dst="score_total",
        sources=["score_total", "score", "display_score", "final_score", "combined_score"],
        numeric=True,
    )
    out = coalesce_into_column(
        out,
        dst="display_score",
        sources=["display_score", "final_score", "score_total", "score"],
        numeric=True,
    )
    out = coalesce_into_column(
        out,
        dst="final_score",
        sources=["final_score", "display_score", "score_total", "score"],
        numeric=True,
    )

    out = coalesce_into_column(out, dst="score_buy", sources=["score_buy", "buy_score", "buy"], numeric=True)
    out = coalesce_into_column(out, dst="buy_score", sources=["buy_score", "score_buy", "buy"], numeric=True)

    out = coalesce_into_column(out, dst="score_sell", sources=["score_sell", "sell_score", "sell"], numeric=True)
    out = coalesce_into_column(out, dst="sell_score", sources=["sell_score", "score_sell", "sell"], numeric=True)

    out = ensure_score_columns(out)

    out = coalesce_into_column(out, dst="mtf", sources=["mtf", "mtf_alignment"], numeric=True)
    out = coalesce_into_column(out, dst="score_mtf", sources=["score_mtf", "mtf_score"], numeric=True)
    out = coalesce_into_column(out, dst="score_slope", sources=["score_slope", "slope", "slope_atr_scaled"], numeric=True)

    return out


# ============================================================
# key guard
# ============================================================

def _ensure_key_columns_before_filter(df: pd.DataFrame, interval: int, *, label: str) -> pd.DataFrame:
    """
    DB列filter前の key guard。

    symbol + datetime がUPSERTキー。
    ここで欠けている行を落とさないと、upsert_executor側で
    chunk丸ごと dropped になる。
    """
    out = safe_df(df)
    if out.empty:
        return out

    try:
        if "symbol" not in out.columns:
            logger.error(
                "[summary.recovery.persistence] cannot upsert: symbol column missing interval=%s label=%s cols=%s",
                interval,
                label,
                list(out.columns),
            )
            return pd.DataFrame()

        if "datetime" not in out.columns:
            logger.warning(
                "[summary.recovery.persistence] datetime column missing before key guard interval=%s label=%s; trying normalize_datetime_like cols=%s",
                interval,
                label,
                list(out.columns),
            )
            out = normalize_datetime_like(out)

        if "datetime" not in out.columns:
            logger.error(
                "[summary.recovery.persistence] cannot upsert: datetime column missing interval=%s label=%s cols=%s",
                interval,
                label,
                list(out.columns),
            )
            return pd.DataFrame()

        before = len(out)

        out["symbol"] = out["symbol"].map(normalize_symbol_value)
        out = out[out["symbol"] != ""].copy()

        out["datetime"] = parse_datetime_series_safely(
            out["datetime"],
            base_df=out,
            col_name="datetime",
            allow_time_only=True,
        )

        out = out[out["datetime"].notna()].copy()

        if not out.empty:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce").dt.floor("min")
            out = out[out["datetime"].notna()].copy()

        dropped = before - len(out)
        if dropped > 0:
            logger.warning(
                "[summary.recovery.persistence] key guard dropped invalid rows interval=%s label=%s before=%s after=%s dropped=%s",
                interval,
                label,
                before,
                len(out),
                dropped,
            )

        if out.empty:
            logger.warning(
                "[summary.recovery.persistence] no valid key rows before upsert interval=%s label=%s",
                interval,
                label,
            )

        return out

    except Exception:
        logger.exception(
            "[summary.recovery.persistence] key guard failed interval=%s label=%s",
            interval,
            label,
        )
        return pd.DataFrame()


def _ensure_key_columns_after_filter(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    """
    DB列filter後の key guard。
    """
    out = safe_df(df)
    if out.empty:
        return out

    try:
        required = ["symbol", "datetime"]
        missing = [c for c in required if c not in out.columns]

        if missing:
            logger.error(
                "[summary.recovery.persistence] key columns missing after DB column filter interval=%s missing=%s cols=%s",
                interval,
                missing,
                list(out.columns),
            )
            return pd.DataFrame()

        return _ensure_key_columns_before_filter(out, interval, label="after_db_column_filter")

    except Exception:
        logger.exception(
            "[summary.recovery.persistence] key guard after filter failed interval=%s",
            interval,
        )
        return pd.DataFrame()


# ============================================================
# main DB normalizer
# ============================================================

def normalize_summary_columns_for_db(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    out = safe_df(df)
    if out.empty:
        return out

    try:
        out = coalesce_duplicate_columns(out)

        # datetime復元を最初に実行
        out = normalize_datetime_like(out)

        out = normalize_numeric_like(out)

        # symbol/datetime key guard
        out = _ensure_key_columns_before_filter(out, int(interval), label="after_datetime_numeric")
        if out.empty:
            return out

        if "interval" not in out.columns:
            out["interval"] = int(interval)
        else:
            try:
                out["interval"] = pd.to_numeric(out["interval"], errors="coerce").fillna(int(interval)).astype(int)
            except Exception:
                out["interval"] = int(interval)

        out = normalize_text_aliases_for_db(out)
        out = normalize_ohlcv_aliases_for_db(out)
        out = normalize_score_aliases_for_db(out)
        out = repair_mtf_consistency(out)

        # date/time/start/endをdatetimeから再整形
        out = normalize_date_columns_for_db(out)

        # date_columns処理後も再度key guard
        out = _ensure_key_columns_before_filter(out, int(interval), label="after_date_columns")
        if out.empty:
            return out

        if "last_update" not in out.columns:
            out["last_update"] = pd.Timestamp.now()

        drop_cols = [c for c in DB_DROP_COLS if c in out.columns]
        if drop_cols:
            out = out.drop(columns=drop_cols, errors="ignore")
            logger.info(
                "[summary.recovery.persistence] db column normalize dropped interval=%s cols=%s",
                interval,
                drop_cols,
            )

        if "id" in out.columns:
            out = out.drop(columns=["id"], errors="ignore")

        allowed = [c for c in SUMMARY_DB_COLUMNS if c != "id"]

        # keep前にsymbol/datetimeがallowedに存在することを確認
        if "symbol" not in allowed or "datetime" not in allowed:
            logger.error(
                "[summary.recovery.persistence] SUMMARY_DB_COLUMNS does not include key columns interval=%s allowed=%s",
                interval,
                allowed,
            )
            return pd.DataFrame()

        keep_cols = [c for c in allowed if c in out.columns]
        unknown_cols = [c for c in out.columns if c not in allowed]

        if unknown_cols:
            logger.info(
                "[summary.recovery.persistence] db column normalize removed unknown interval=%s cols=%s",
                interval,
                unknown_cols,
            )

        if "symbol" not in keep_cols or "datetime" not in keep_cols:
            logger.error(
                "[summary.recovery.persistence] keep_cols missing key interval=%s keep_cols=%s out_cols=%s",
                interval,
                keep_cols,
                list(out.columns),
            )
            return pd.DataFrame()

        out = out[keep_cols].copy()

        # keep後にkey再検証
        out = _ensure_key_columns_after_filter(out, int(interval))
        if out.empty:
            return out

        out = normalize_numeric_like(out)
        out = ensure_score_columns(out)

        # ensure_score_columns 後に列が増えるため再度 allowed へ寄せる
        keep_cols = [c for c in allowed if c in out.columns]
        out = out[keep_cols].copy()

        out = _ensure_key_columns_after_filter(out, int(interval))
        if out.empty:
            return out

        from .column_utils import pick_numeric_series_nan

        logger.info(
            "[summary.recovery.persistence] db column normalize done interval=%s rows=%s cols=%s score_nonnull=%s buy_nonnull=%s sell_nonnull=%s dt_min=%s dt_max=%s",
            interval,
            len(out),
            list(out.columns),
            int(pick_numeric_series_nan(out, ["score", "score_total", "display_score", "final_score"]).notna().sum()),
            int(pick_numeric_series_nan(out, ["score_buy", "buy_score", "buy"]).notna().sum()),
            int(pick_numeric_series_nan(out, ["score_sell", "sell_score", "sell"]).notna().sum()),
            out["datetime"].min() if "datetime" in out.columns and not out.empty else None,
            out["datetime"].max() if "datetime" in out.columns and not out.empty else None,
        )

        return out

    except Exception:
        logger.exception(
            "[summary.recovery.persistence] normalize_summary_columns_for_db failed interval=%s",
            interval,
        )
        return df.copy()


def finalize_for_upsert(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    out = safe_df(df)

    if out.empty:
        logger.info(
            "[summary.recovery.persistence] finalize_for_upsert done interval=%s rows=0 cols=[]",
            interval,
        )
        return out

    try:
        out = coalesce_duplicate_columns(out)

        # datetime復元を最初に実行
        out = normalize_datetime_like(out)

        out = normalize_numeric_like(out)
        out = ensure_score_columns(out)

        if "symbol" in out.columns:
            out["symbol"] = out["symbol"].map(normalize_symbol_value)

        if "interval" not in out.columns:
            out["interval"] = int(interval)

        if "symbolname" not in out.columns:
            out["symbolname"] = resolve_symbolname_series(out)
        else:
            out["symbolname"] = resolve_symbolname_series(out)

        # ここでDB保存列へ正規化 + key guard
        out = normalize_summary_columns_for_db(out, interval=int(interval))

        logger.info(
            "[summary.recovery.persistence] finalize_for_upsert done interval=%s rows=%s cols=%s",
            interval,
            len(out),
            list(out.columns),
        )

        return out

    except Exception:
        logger.exception("[summary.recovery.persistence] finalize_for_upsert failed interval=%s", interval)
        return out


__all__ = [
    "normalize_numeric_like",
    "normalize_ohlcv_aliases_for_db",
    "normalize_score_aliases_for_db",
    "normalize_summary_columns_for_db",
    "finalize_for_upsert",
]