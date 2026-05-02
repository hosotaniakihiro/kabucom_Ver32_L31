# ============================================================
# File   : scheduler_jobs/summary/push_summary.py
# Version: Ver31_L27-PUSH-SUMMARY-SOURCE-SEPARATED-V8
#          -PUSH-RUNNER-DIRECT
#          -NOW-PASSTHROUGH
#          -COMPAT-ALIASES
#          -DISPLAY-TRACE-ENHANCED
#          -LATEST-DF-PRIORITY
#          -INDICATOR-PROFILE
#          -RUNNER-NEWFILE-COMPAT
#          -PUBLISH-STATE-AWARE
# ------------------------------------------------------------
# ✔ PUSH由来サマリーjobの互換入口
# ✔ 1m / 3m / 5m job提供
# ✔ 実体は trading.summary.push.runner.run_push_summary_job を呼ぶ
# ✔ scheduler から渡される now をそのまま runner へ伝搬
# ✔ dict戻り値時は summary_latest_df を最優先で救出
# ✔ summary_df しか無い場合は symbol+datetime 最新1行へ絞って救出
# ✔ 指標列 non_null / non_zero ログを強化
# ✔ ranking 系への依存を除去
# ✔ published / published_df を保持
# ✔ 互換のため job_summary() / job_1m() / job_3m() / job_5m() は
#   DataFrame を返す
# ✔ 詳細結果が必要な箇所向けに job_summary_result() を追加
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Optional

import pandas as pd

from trading.summary.push.runner import run_push_summary_job

logger = logging.getLogger(__name__)

try:
    from core.global_context.context import global_data  # type: ignore
except Exception:
    try:
        from global_state import global_data  # type: ignore
    except Exception:
        global_data = None


_INDICATOR_COLS = [
    "rsi",
    "macd",
    "signal",
    "atr",
    "slope",
    "slope_atr_scaled",
    "score",
    "score_buy",
    "score_sell",
    "score_slope",
    "score_mtf",
    "score_total",
    "final_score",
    "display_score",
]


# ============================================================
# helpers
# ============================================================

def _as_interval(value: int | str) -> int:
    try:
        s = str(value).strip().lower().replace(" ", "")
        if s.endswith("min"):
            s = s[:-3]
        elif s.endswith("m"):
            s = s[:-1]
        return int(s)
    except Exception:
        logger.warning(
            "[summary.push_summary] invalid interval=%r -> fallback to 1",
            value,
        )
        return 1


def _ensure_df(x: Any) -> pd.DataFrame:
    try:
        if x is None:
            return pd.DataFrame()
        if isinstance(x, pd.DataFrame):
            return x.copy()
        if isinstance(x, pd.Series):
            return x.to_frame().T.reset_index(drop=True)
        return pd.DataFrame()
    except Exception:
        logger.exception("[summary.push_summary] _ensure_df failed")
        return pd.DataFrame()


def _safe_len(x: Any) -> int:
    try:
        return len(x)
    except Exception:
        return 0


def _safe_columns(df: Any) -> list[str]:
    try:
        if isinstance(df, pd.DataFrame):
            return list(df.columns)
    except Exception:
        pass
    return []


def _safe_latest_dt(df: Any) -> Any:
    try:
        if isinstance(df, pd.DataFrame) and not df.empty:
            for c in ("datetime", "dt", "timestamp", "end_time", "snapshot_time"):
                if c in df.columns:
                    s = pd.to_datetime(df[c], errors="coerce")
                    if s.notna().any():
                        x = s.max()
                        try:
                            x = x.tz_localize(None)
                        except Exception:
                            pass
                        return x
    except Exception:
        return None
    return None


def _safe_non_null(df: Any, col: str) -> int:
    try:
        if isinstance(df, pd.DataFrame) and col in df.columns:
            return int(pd.to_numeric(df[col], errors="coerce").notna().sum())
    except Exception:
        pass
    return 0


def _safe_non_zero(df: Any, col: str) -> int:
    try:
        if isinstance(df, pd.DataFrame) and col in df.columns:
            s = pd.to_numeric(df[col], errors="coerce")
            return int((s.fillna(0) != 0).sum())
    except Exception:
        pass
    return 0


def _safe_symbol_count(df: Any) -> int:
    try:
        if isinstance(df, pd.DataFrame) and "symbol" in df.columns:
            return int(df["symbol"].astype(str).str.strip().nunique())
    except Exception:
        pass
    return 0


def _log_indicator_profile(tag: str, df: Any) -> None:
    try:
        if not isinstance(df, pd.DataFrame):
            logger.warning("[summary.push_summary][%s] not a dataframe", tag)
            return
        if df.empty:
            logger.warning("[summary.push_summary][%s] empty dataframe", tag)
            return

        logger.info(
            "[summary.push_summary][%s] rows=%s cols=%s symbols=%s latest_dt=%s",
            tag,
            len(df),
            len(df.columns),
            _safe_symbol_count(df),
            _safe_latest_dt(df),
        )

        for c in _INDICATOR_COLS:
            if c not in df.columns:
                logger.warning(
                    "[summary.push_summary][%s] missing indicator col=%s",
                    tag,
                    c,
                )
            else:
                logger.info(
                    "[summary.push_summary][%s] %s non_null=%s non_zero=%s",
                    tag,
                    c,
                    _safe_non_null(df, c),
                    _safe_non_zero(df, c),
                )
    except Exception:
        logger.exception("[summary.push_summary] _log_indicator_profile failed tag=%s", tag)


def _log_df_preview(tag: str, df: pd.DataFrame, interval: int) -> None:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            logger.warning("[summary.push_summary] %s interval=%s empty", tag, interval)
            return

        show_cols = [
            c for c in [
                "symbol", "symbolname", "score", "score_buy", "score_sell",
                "final_score", "display_score", "slope", "score_slope",
                "mtf", "score_mtf", "mtf_score", "rsi", "macd", "signal",
                "close", "datetime"
            ] if c in df.columns
        ]
        logger.info(
            "[summary.push_summary] %s interval=%s rows=%s symbols=%s latest_dt=%s cols=%s",
            tag,
            interval,
            len(df),
            _safe_symbol_count(df),
            _safe_latest_dt(df),
            list(df.columns),
        )
        if show_cols:
            logger.info(
                "[summary.push_summary] %s preview interval=%s\n%s",
                tag,
                interval,
                df[show_cols].head(10).to_string(index=False),
            )
    except Exception:
        logger.exception("[summary.push_summary] _log_df_preview failed tag=%s interval=%s", tag, interval)


def _empty_df(reason: str, interval: int, now: Optional[dt.datetime] = None) -> pd.DataFrame:
    logger.warning(
        "[summary.push_summary] returning empty dataframe interval=%s now=%s reason=%s",
        interval,
        now,
        reason,
    )
    return pd.DataFrame()


def _empty_result(reason: str, interval: int, now: Optional[dt.datetime] = None) -> dict:
    logger.warning(
        "[summary.push_summary] returning empty result interval=%s now=%s reason=%s",
        interval,
        now,
        reason,
    )
    return {
        "interval": int(interval),
        "summary_df": pd.DataFrame(),
        "summary_latest_df": pd.DataFrame(),
        "published_df": pd.DataFrame(),
        "published": False,
    }


def _latest_only_from_df(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return pd.DataFrame()

    out = df.copy()

    dt_col = None
    for c in ("datetime", "dt", "timestamp", "end_time", "snapshot_time"):
        if c in out.columns:
            dt_col = c
            break

    if dt_col is None or "symbol" not in out.columns:
        return out.reset_index(drop=True)

    out["symbol"] = out["symbol"].astype(str).str.strip()
    out[dt_col] = pd.to_datetime(out[dt_col], errors="coerce")
    out = out.dropna(subset=["symbol", dt_col]).copy()
    out = out[out["symbol"] != ""].copy()

    if out.empty:
        return out

    try:
        out[dt_col] = out[dt_col].dt.tz_localize(None)
    except Exception:
        pass

    out = out.sort_values(["symbol", dt_col], kind="stable")
    out = out.groupby("symbol", as_index=False).tail(1)
    return out.reset_index(drop=True)


def _resolve_published_df(interval: int) -> pd.DataFrame:
    try:
        if global_data is None:
            return pd.DataFrame()

        getter = getattr(global_data, "get_push_merged_summary", None)
        if callable(getter):
            df = _ensure_df(getter(interval))
            if not df.empty:
                logger.info(
                    "[summary.push_summary] resolved published df via get_push_merged_summary interval=%s rows=%s",
                    interval,
                    len(df),
                )
                return df

        getter2 = getattr(global_data, "get_merged_summary", None)
        if callable(getter2):
            try:
                df = _ensure_df(getter2(interval, source="push"))
                if not df.empty:
                    logger.info(
                        "[summary.push_summary] resolved published df via get_merged_summary(interval, source=push) interval=%s rows=%s",
                        interval,
                        len(df),
                    )
                    return df
            except TypeError:
                pass

        getter3 = getattr(global_data, "get_push_summary", None)
        if callable(getter3):
            df = _ensure_df(getter3(interval))
            if not df.empty:
                logger.info(
                    "[summary.push_summary] resolved published df via get_push_summary interval=%s rows=%s",
                    interval,
                    len(df),
                )
                return df

    except Exception:
        logger.exception("[summary.push_summary] _resolve_published_df failed interval=%s", interval)

    return pd.DataFrame()


def _normalize_runner_result(
    result: Any,
    interval: int,
    now: Optional[dt.datetime],
) -> dict:
    """
    runner の戻り値を正規化して返す。
    戻り値:
      {
        "interval": int,
        "summary_df": pd.DataFrame,
        "summary_latest_df": pd.DataFrame,
        "published_df": pd.DataFrame,
        "published": bool,
      }
    """
    if result is None:
        logger.warning(
            "[summary.push_summary] runner returned None interval=%s now=%s",
            interval,
            now,
        )
        return _empty_result("runner_returned_none", interval=interval, now=now)

    if isinstance(result, pd.DataFrame):
        logger.info(
            "[summary.push_summary] dataframe result interval=%s rows=%s cols=%s latest_dt=%s now=%s",
            interval,
            len(result),
            len(result.columns),
            _safe_latest_dt(result),
            now,
        )
        latest_df = _latest_only_from_df(result)
        published_df = _resolve_published_df(interval)
        out = {
            "interval": interval,
            "summary_df": result.copy(),
            "summary_latest_df": latest_df,
            "published_df": published_df,
            "published": not published_df.empty,
        }
        _log_indicator_profile(f"result-df-{interval}m", latest_df)
        return out

    if isinstance(result, tuple):
        logger.warning(
            "[summary.push_summary] runner returned tuple interval=%s len=%s now=%s",
            interval,
            len(result),
            now,
        )
        try:
            if len(result) >= 1 and isinstance(result[0], pd.DataFrame):
                base_df = result[0].copy()
                latest_df = _latest_only_from_df(base_df)
                published_df = _resolve_published_df(interval)
                out = {
                    "interval": interval,
                    "summary_df": base_df,
                    "summary_latest_df": latest_df,
                    "published_df": published_df,
                    "published": not published_df.empty,
                }
                _log_indicator_profile(f"result-tuple0-{interval}m", latest_df)
                return out
        except Exception:
            logger.exception("[summary.push_summary] tuple result rescue failed interval=%s", interval)

        return _empty_result("runner_returned_tuple_without_dataframe", interval=interval, now=now)

    if isinstance(result, dict):
        keys = sorted(list(result.keys()))
        logger.info(
            "[summary.push_summary] runner returned dict interval=%s keys=%s now=%s",
            interval,
            keys,
            now,
        )

        summary_df = _ensure_df(result.get("summary_df"))
        summary_latest_df = _ensure_df(result.get("summary_latest_df"))
        published = bool(result.get("published", False))

        if summary_latest_df.empty and not summary_df.empty:
            summary_latest_df = _latest_only_from_df(summary_df)
            logger.warning(
                "[summary.push_summary] summary_latest_df rescued from summary_df interval=%s src_rows=%s out_rows=%s",
                interval,
                len(summary_df),
                len(summary_latest_df),
            )

        published_df = _ensure_df(result.get("published_df"))
        if published_df.empty:
            published_df = _resolve_published_df(interval)

        if not published and not published_df.empty:
            published = True
            logger.warning(
                "[summary.push_summary] publish flag corrected to True interval=%s because published_df exists rows=%s",
                interval,
                len(published_df),
            )

        _log_indicator_profile(f"dict-summary-{interval}m", summary_df)
        _log_indicator_profile(f"dict-summary-latest-{interval}m", summary_latest_df)
        _log_indicator_profile(f"dict-published-{interval}m", published_df)

        return {
            "interval": interval,
            "summary_df": summary_df,
            "summary_latest_df": summary_latest_df,
            "published_df": published_df,
            "published": published,
        }

    logger.warning(
        "[summary.push_summary] runner returned unexpected type interval=%s type=%s now=%s",
        interval,
        type(result).__name__,
        now,
    )
    return _empty_result(
        f"runner_returned_unexpected_type:{type(result).__name__}",
        interval=interval,
        now=now,
    )


# ============================================================
# public detailed entrypoint
# ============================================================

def job_summary_result(
    interval: int | str = 1,
    display: bool = True,
    now: Optional[dt.datetime] = None,
    **kwargs,
) -> dict:
    """
    詳細結果を dict で返す版。

    returns:
      {
        "interval": int,
        "summary_df": DataFrame,
        "summary_latest_df": DataFrame,
        "published_df": DataFrame,
        "published": bool,
      }
    """
    interval = _as_interval(interval)

    try:
        logger.info(
            "[summary.push_summary] start interval=%s display=%s now=%s extra_keys=%s",
            interval,
            display,
            now,
            sorted(list(kwargs.keys())),
        )

        conflict_keys = [k for k in ("interval", "display", "now") if k in kwargs]
        if conflict_keys:
            logger.warning(
                "[summary.push_summary] conflicting kwargs ignored interval=%s conflict_keys=%s",
                interval,
                conflict_keys,
            )

        result = run_push_summary_job(
            interval=interval,
            display=display,
            now=now,
            **kwargs,
        )

        out = _normalize_runner_result(result, interval=interval, now=now)

        _log_df_preview("summary_df", out["summary_df"], interval)
        _log_df_preview("summary_latest_df", out["summary_latest_df"], interval)
        _log_df_preview("published_df", out["published_df"], interval)

        if out["published"]:
            logger.info(
                "[summary.push_summary] publish ok interval=%s published_rows=%s latest_dt=%s",
                interval,
                len(out["published_df"]),
                _safe_latest_dt(out["published_df"]),
            )
        else:
            logger.warning(
                "[summary.push_summary] publish NG interval=%s summary_rows=%s latest_rows=%s published_rows=%s",
                interval,
                len(out["summary_df"]),
                len(out["summary_latest_df"]),
                len(out["published_df"]),
            )

        logger.info(
            "[summary.push_summary] finished interval=%s published=%s summary_rows=%s latest_rows=%s published_rows=%s now=%s",
            interval,
            out["published"],
            len(out["summary_df"]),
            len(out["summary_latest_df"]),
            len(out["published_df"]),
            now,
        )
        return out

    except Exception:
        logger.exception(
            "[summary.push_summary] job_summary_result failed interval=%s now=%s",
            interval,
            now,
        )
        return _empty_result("job_summary_result_exception", interval=interval, now=now)


# ============================================================
# public compat entrypoints
# ============================================================

def job_summary(
    interval: int | str = 1,
    display: bool = True,
    now: Optional[dt.datetime] = None,
    **kwargs,
) -> pd.DataFrame:
    """
    互換API:
    従来どおり DataFrame を返す。
    優先順位:
      1) published_df
      2) summary_latest_df
      3) summary_df の latest化
    """
    result = job_summary_result(
        interval=interval,
        display=display,
        now=now,
        **kwargs,
    )

    published_df = _ensure_df(result.get("published_df"))
    if not published_df.empty:
        logger.info(
            "[summary.push_summary] job_summary returning published_df interval=%s rows=%s",
            result.get("interval"),
            len(published_df),
        )
        return published_df

    latest_df = _ensure_df(result.get("summary_latest_df"))
    if not latest_df.empty:
        logger.warning(
            "[summary.push_summary] job_summary fallback returning summary_latest_df interval=%s rows=%s",
            result.get("interval"),
            len(latest_df),
        )
        return latest_df

    summary_df = _ensure_df(result.get("summary_df"))
    if not summary_df.empty:
        rescued = _latest_only_from_df(summary_df)
        logger.warning(
            "[summary.push_summary] job_summary fallback returning rescued latest from summary_df interval=%s src_rows=%s out_rows=%s",
            result.get("interval"),
            len(summary_df),
            len(rescued),
        )
        return rescued

    return _empty_df("job_summary_no_usable_dataframe", interval=_as_interval(interval), now=now)


def job_1m(
    display: bool = True,
    now: Optional[dt.datetime] = None,
    **kwargs,
) -> pd.DataFrame:
    logger.info(
        "[summary.push_summary] job_1m called display=%s now=%s extra_keys=%s",
        display,
        now,
        sorted(list(kwargs.keys())),
    )
    return job_summary(interval=1, display=display, now=now, **kwargs)


def job_3m(
    display: bool = True,
    now: Optional[dt.datetime] = None,
    **kwargs,
) -> pd.DataFrame:
    logger.info(
        "[summary.push_summary] job_3m called display=%s now=%s extra_keys=%s",
        display,
        now,
        sorted(list(kwargs.keys())),
    )
    return job_summary(interval=3, display=display, now=now, **kwargs)


def job_5m(
    display: bool = True,
    now: Optional[dt.datetime] = None,
    **kwargs,
) -> pd.DataFrame:
    logger.info(
        "[summary.push_summary] job_5m called display=%s now=%s extra_keys=%s",
        display,
        now,
        sorted(list(kwargs.keys())),
    )
    return job_summary(interval=5, display=display, now=now, **kwargs)


def run_push_summary_job_compat(
    interval: int | str = 1,
    display: bool = True,
    now: Optional[dt.datetime] = None,
    **kwargs,
) -> pd.DataFrame:
    logger.info(
        "[summary.push_summary] run_push_summary_job_compat called interval=%r display=%s now=%s",
        interval,
        display,
        now,
    )
    return job_summary(interval=interval, display=display, now=now, **kwargs)


__all__ = [
    "job_summary",
    "job_summary_result",
    "job_1m",
    "job_3m",
    "job_5m",
    "run_push_summary_job",
    "run_push_summary_job_compat",
]