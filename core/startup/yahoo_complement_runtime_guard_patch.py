# ============================================================
# File   : core/startup/yahoo_complement_runtime_guard_patch.py
# Purpose:
#   Yahoo補完の1分tick詰まりを抑える runtime patch。
#
# 背景:
#   ログ上で以下が増えるケースを軽減する。
#     - [YAHOO TASK] skipped because previous worker still running
#     - [YAHOO DEBUG] long pending futures count=...
#
# 方針:
#   1. 補完runnerに入る直前で対象symbol/行数を上限化する
#   2. 1回の補完処理にソフト期限を持たせ、期限超過後のintervalをスキップする
#   3. ダウンローダ側が参照していれば効くよう、保守的なtimeout/worker環境変数も既定設定する
#
# 安全性:
#   - YAHOO_COMPLEMENT_RUNTIME_GUARD_ENABLED=0 で完全無効化
#   - 既存関数をwrapするだけで、元実装は残す
# ============================================================

from __future__ import annotations

import contextvars
import datetime as dt
import logging
import os
import time
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_DEADLINE_TS: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "YAHOO_COMPLEMENT_DEADLINE_TS", default=None
)


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "enable", "enabled"}


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        return int(default) if v is None or str(v).strip() == "" else int(float(v))
    except Exception:
        return int(default)


def _cap_yahoo_df(df: Any) -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    max_symbols = _env_int("YAHOO_COMPLEMENT_MAX_SYMBOLS_PER_TICK", 220)
    max_rows_per_symbol = _env_int("YAHOO_COMPLEMENT_MAX_1M_ROWS_PER_SYMBOL", 120)
    if max_symbols <= 0 and max_rows_per_symbol <= 0:
        return df
    if "symbol" not in df.columns:
        return df

    out = df.copy()
    before_rows = len(out)
    before_symbols = int(out["symbol"].nunique())

    if "datetime" in out.columns:
        out["_yc_dt"] = pd.to_datetime(out["datetime"], errors="coerce")
        sort_cols = ["symbol", "_yc_dt"]
        ascending = [True, False]
    else:
        out["_yc_dt"] = pd.NaT
        sort_cols = ["symbol"]
        ascending = [True]

    # 各symbolの直近行を残して、古い履歴でCPU/DBを膨らませない。
    if max_rows_per_symbol > 0:
        out = (
            out.sort_values(sort_cols, ascending=ascending, kind="stable")
            .groupby("symbol", sort=False, group_keys=False)
            .head(max_rows_per_symbol)
        )

    # symbol数が多すぎる場合は、直近データが新しい銘柄を優先する。
    if max_symbols > 0:
        latest = (
            out.groupby("symbol", sort=False)["_yc_dt"]
            .max()
            .sort_values(ascending=False, kind="stable")
        )
        keep = set(latest.head(max_symbols).index.astype(str))
        out = out[out["symbol"].astype(str).isin(keep)].copy()

    out = out.drop(columns=["_yc_dt"], errors="ignore").reset_index(drop=True)
    after_rows = len(out)
    after_symbols = int(out["symbol"].nunique()) if "symbol" in out.columns else 0

    if after_rows != before_rows or after_symbols != before_symbols:
        logger.warning(
            "[YAHOO COMPLEMENT GUARD] capped df rows=%s->%s symbols=%s->%s max_symbols=%s max_rows_per_symbol=%s",
            before_rows,
            after_rows,
            before_symbols,
            after_symbols,
            max_symbols,
            max_rows_per_symbol,
        )
    return out


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True
    if not _env_bool("YAHOO_COMPLEMENT_RUNTIME_GUARD_ENABLED", True):
        logger.warning("[YAHOO COMPLEMENT GUARD] disabled by env")
        return False

    # ダウンロード側がこの環境変数を読む実装なら即効く。未使用でも副作用はない。
    os.environ.setdefault("YAHOO_COMPLEMENT_MAX_WORKERS", "4")
    os.environ.setdefault("YAHOO_DOWNLOAD_MAX_WORKERS", "4")
    os.environ.setdefault("YAHOO_FUTURE_TIMEOUT_SEC", "12")
    os.environ.setdefault("YAHOO_DOWNLOAD_FUTURE_TIMEOUT_SEC", "12")
    os.environ.setdefault("YAHOO_COMPLEMENT_WORKER_COOLDOWN_SEC", "90")

    try:
        import trading.yahoo.pipeline.complement.runner as runner
    except Exception:
        logger.exception("[YAHOO COMPLEMENT GUARD] import runner failed")
        return False

    original_run_pipeline = getattr(runner, "run_yahoo_complement_pipeline", None)
    original_run_once = getattr(runner, "run_yahoo_complement_once", None)
    original_single = getattr(runner, "_run_single_interval_pipeline", None)
    if not callable(original_run_pipeline) or not callable(original_single):
        logger.warning("[YAHOO COMPLEMENT GUARD] target functions not callable")
        return False

    def guarded_single(df_yahoo_1min, *args, **kwargs):
        deadline = _DEADLINE_TS.get()
        interval = kwargs.get("interval", args[0] if args else None)
        if deadline is not None and time.monotonic() >= deadline:
            logger.warning(
                "[YAHOO COMPLEMENT GUARD] skip interval=%s because soft deadline exceeded",
                interval,
            )
            return pd.DataFrame()
        return original_single(df_yahoo_1min, *args, **kwargs)

    def guarded_pipeline(df_yahoo, *args, **kwargs):
        started = time.monotonic()
        budget_sec = _env_int("YAHOO_COMPLEMENT_SOFT_BUDGET_SEC", 45)
        token = None
        if budget_sec > 0:
            token = _DEADLINE_TS.set(started + float(budget_sec))
        try:
            capped = _cap_yahoo_df(df_yahoo)
            return original_run_pipeline(capped, *args, **kwargs)
        finally:
            if token is not None:
                _DEADLINE_TS.reset(token)
            elapsed = time.monotonic() - started
            if budget_sec > 0 and elapsed > budget_sec:
                logger.warning(
                    "[YAHOO COMPLEMENT GUARD] pipeline exceeded soft budget elapsed=%.3fs budget=%ss",
                    elapsed,
                    budget_sec,
                )

    def guarded_once(df_yahoo, *args, **kwargs):
        capped = _cap_yahoo_df(df_yahoo)
        if callable(original_run_once):
            return original_run_once(capped, *args, **kwargs)
        return guarded_pipeline(capped, *args, **kwargs)

    try:
        setattr(runner, "_run_single_interval_pipeline", guarded_single)
        setattr(runner, "run_yahoo_complement_pipeline", guarded_pipeline)
        setattr(runner, "run_yahoo_complement_once", guarded_once)
        _PATCHED = True
        logger.warning(
            "[YAHOO COMPLEMENT GUARD] installed max_symbols=%s max_rows_per_symbol=%s budget=%ss",
            _env_int("YAHOO_COMPLEMENT_MAX_SYMBOLS_PER_TICK", 220),
            _env_int("YAHOO_COMPLEMENT_MAX_1M_ROWS_PER_SYMBOL", 120),
            _env_int("YAHOO_COMPLEMENT_SOFT_BUDGET_SEC", 45),
        )
        return True
    except Exception:
        logger.exception("[YAHOO COMPLEMENT GUARD] install failed")
        return False


__all__ = ["install"]
