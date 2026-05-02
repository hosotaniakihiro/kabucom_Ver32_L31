# ============================================================
# File   : trading/entry/summary_ai/utils.py
# Version: PRODUCTION-STABLE-REV1.0-SUMMARY-AI-UTILS
# ------------------------------------------------------------
# 【概要】
#   summary_ai パッケージ共通の安全化ユーティリティ。
#
# 【主な機能】
#   - DataFrame 安全化
#   - 数値 / 文字列 / symbol 正規化
#   - optional callable resolver
#   - market open 判定
#   - DataFrame → records 変換
# ============================================================

from __future__ import annotations

import datetime as dt
import importlib
import logging
import math
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


VALID_MARKET_TYPES = {"プライム", "スタンダード", "グロース"}


def safe_df(df: Any) -> pd.DataFrame:
    try:
        if df is None:
            return pd.DataFrame()

        if isinstance(df, pd.DataFrame):
            out = df.copy()
        elif isinstance(df, pd.Series):
            out = pd.DataFrame([df.to_dict()])
        elif isinstance(df, dict):
            out = pd.DataFrame([df])
        else:
            out = pd.DataFrame(df).copy()

        if out.empty:
            return out

        try:
            if isinstance(out.columns, pd.MultiIndex):
                out.columns = [
                    "_".join([str(x) for x in col if x not in ("", None)])
                    for col in out.columns.to_flat_index()
                ]
        except Exception:
            logger.debug("[SUMMARY AI UTILS] MultiIndex flatten failed", exc_info=True)

        out.columns = [str(c) for c in out.columns]
        out = out.replace([np.inf, -np.inf], np.nan)
        return out.reset_index(drop=True)

    except Exception:
        logger.exception("[SUMMARY AI UTILS] safe_df failed")
        return pd.DataFrame()


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return int(x)
    except Exception:
        return default


def safe_str(v: Any, default: str = "") -> str:
    try:
        if v is None:
            return default
        s = str(v).strip()
        if s.lower() in {"nan", "none", "null"}:
            return default
        return s
    except Exception:
        return default


def normalize_symbol(v: Any) -> str:
    s = safe_str(v, "")
    if s.endswith(".0"):
        s2 = s[:-2]
        if s2.isdigit():
            return s2
    return s


def first_value(row: pd.Series | Dict[str, Any], names: Sequence[str], default: Any = None) -> Any:
    for name in names:
        try:
            if isinstance(row, dict):
                if name not in row:
                    continue
                v = row.get(name)
            else:
                if name not in row.index:
                    continue
                v = row[name]

            if v is None:
                continue

            try:
                if pd.isna(v):
                    continue
            except Exception:
                pass

            if isinstance(v, str) and v.strip() == "":
                continue

            return v
        except Exception:
            continue

    return default


def to_records(df: pd.DataFrame) -> List[Dict[str, Any]]:
    try:
        return df.to_dict(orient="records")
    except Exception:
        rows: List[Dict[str, Any]] = []
        try:
            for _, row in df.iterrows():
                rows.append(dict(row))
        except Exception:
            pass
        return rows


def now_jst_naive() -> dt.datetime:
    return dt.datetime.now()


def is_market_open(now: Optional[dt.datetime] = None) -> bool:
    now = now or now_jst_naive()
    t = now.time()
    return (
        dt.time(9, 0) <= t <= dt.time(11, 30)
        or dt.time(12, 30) <= t <= dt.time(15, 30)
    )


def is_truthy(v: Any, default: bool = False) -> bool:
    if v is None:
        return default

    if isinstance(v, bool):
        return v

    s = str(v).strip().lower()

    if s in {"1", "true", "yes", "y", "ok", "buy", "対象"}:
        return True

    if s in {"0", "false", "no", "n", "ng", "none", ""}:
        return False

    try:
        return bool(float(s))
    except Exception:
        return default


def resolve_callable(candidates: Sequence[Tuple[str, str]]) -> Optional[Callable[..., Any]]:
    for module_name, func_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
            if callable(fn):
                logger.info(
                    "[SUMMARY AI UTILS] resolved callable %s.%s",
                    module_name,
                    func_name,
                )
                return fn
        except Exception:
            continue

    return None


def get_ai_final_entry_check() -> Optional[Callable[[Dict[str, Any]], Dict[str, Any]]]:
    return resolve_callable(
        [
            ("AI.entry_gate", "ai_final_entry_check"),
            ("trading.ai.entry_gate", "ai_final_entry_check"),
            ("trading.entry.entry_gate", "ai_final_entry_check"),
        ]
    )


def get_bulk_entry_pipeline() -> Optional[Callable[..., Any]]:
    """
    既存の entry pipeline を探索する。

    優先:
      trading.summary.pipeline.entry_pipeline.run_entry_pipeline

    想定:
      run_entry_pipeline(approved_rows, df_summary, interval)
    """
    return resolve_callable(
        [
            ("trading.summary.pipeline.entry_pipeline", "run_entry_pipeline"),
            ("trading.entry.entry_pipeline", "run_entry_pipeline"),
            ("trading.entry.entry_pipeline", "run_entry"),
            ("core.entry_exit_tasks", "run_entry_pipeline"),
        ]
    )


def pick_num_series(df: pd.DataFrame, candidates: Sequence[str], default: float = 0.0) -> pd.Series:
    for c in candidates:
        if c in df.columns:
            try:
                return (
                    pd.to_numeric(df[c], errors="coerce")
                    .replace([np.inf, -np.inf], np.nan)
                    .fillna(default)
                )
            except Exception:
                pass

    return pd.Series(default, index=df.index, dtype="float64")


def pick_text_series(df: pd.DataFrame, candidates: Sequence[str], default: str = "") -> pd.Series:
    for c in candidates:
        if c in df.columns:
            try:
                return df[c].fillna(default).astype(str)
            except Exception:
                pass

    return pd.Series(default, index=df.index, dtype="object")