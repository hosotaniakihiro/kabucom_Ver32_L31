# ============================================================
# File   : trading/summary/persistence/preprocess_pipeline.py
# Version: Ver1.0-PRODUCTION-PREPROCESS-PIPELINE
# ------------------------------------------------------------
# 機能:
# - summary保存前の前処理統合
# - datetime正規化
# - identity列補完
# - numeric正規化
# - symbolname補完
# - required columns補完
# - duplicate row除去
# ------------------------------------------------------------
# 主な責務:
# - 保存直前の前処理を一元化
# - 各前処理モジュールを順序付きで実行
# ============================================================

from __future__ import annotations

import logging

import pandas as pd

from trading.summary.persistence.dataframe_normalizer import normalize_dataframe
from trading.summary.persistence.dataframe_utils import ensure_dataframe
from trading.summary.persistence.identity_builder import (
    ensure_required_identity_columns,
    normalize_datetime_columns,
)
from trading.summary.persistence.preprocess.datetime_handler import ensure_required_columns
from trading.summary.persistence.preprocess.duplicate_handler import drop_duplicate_rows
from trading.summary.persistence.preprocess.numeric_handler import normalize_numeric
from trading.summary.persistence.preprocess.symbol_handler import ensure_symbolname

logger = logging.getLogger(__name__)


def preprocess_summary_dataframe(df: pd.DataFrame, interval: int) -> pd.DataFrame:
    df = ensure_dataframe(df)
    if df.empty:
        return df

    df = normalize_datetime_columns(df)
    df = ensure_required_identity_columns(df, interval)

    df = normalize_dataframe(df)
    df = normalize_numeric(df)
    df = ensure_symbolname(df)
    df = ensure_required_columns(df, interval)
    df = drop_duplicate_rows(df)

    df = normalize_datetime_columns(df)
    df = ensure_required_identity_columns(df, interval)

    return df