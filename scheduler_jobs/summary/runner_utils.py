# ============================================================
# File   : scheduler_jobs/summary/runner_utils.py
# Version: PRODUCTION-STABLE-REV2.3-SAFE-KWARG-FILTER-FULL-COMPAT
# ------------------------------------------------------------
# Purpose:
#   summary runner 呼び出し補助
#
# Fix:
#   - SummaryController.diff_update() got unexpected keyword argument 'display'
#     を防ぐ
#   - 呼び出し先 runner が受け取れない kwargs を自動除外
#   - safe_io.py / closed_market_display.py / summary_ai_entry_hook.py
#     が期待する互換関数を復元
#
# Compatibility:
#   - env_bool
#   - env_int
#   - env_float
#   - df_rows
#   - df_cols
#   - is_nonempty_df
#   - log_df_state(df, label=..., context=...)
#   - log_df_state("label", interval, df)
#   - call_runner_with_optional_now
#   - call_runner_safely
# ============================================================

from __future__ import annotations

import inspect
import logging
import os
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


# ============================================================
# Env helpers
# ============================================================

def env_bool(name: str, default: bool = False) -> bool:
    """
    環境変数を bool として読む。

    True:
      1, true, t, yes, y, on

    False:
      0, false, f, no, n, off
    """
    try:
        v = os.environ.get(str(name), None)
        if v is None:
            return bool(default)

        if isinstance(v, bool):
            return bool(v)

        s = str(v).strip().lower()

        if s in ("1", "true", "t", "yes", "y", "on"):
            return True

        if s in ("0", "false", "f", "no", "n", "off", ""):
            return False

        return bool(default)

    except Exception:
        return bool(default)


def env_int(name: str, default: int = 0) -> int:
    """
    環境変数を int として読む。
    """
    try:
        v = os.environ.get(str(name), None)
        if v is None or str(v).strip() == "":
            return int(default)

        return int(float(str(v).strip()))

    except Exception:
        return int(default)


def env_float(name: str, default: float = 0.0) -> float:
    """
    環境変数を float として読む。
    """
    try:
        v = os.environ.get(str(name), None)
        if v is None or str(v).strip() == "":
            return float(default)

        return float(str(v).strip())

    except Exception:
        return float(default)


# ============================================================
# DataFrame helpers
# ============================================================

def df_rows(df: Any) -> int:
    """
    DataFrame風オブジェクトの行数を返す。
    None / 非DataFrameでも落とさない。
    """
    try:
        if df is None:
            return 0

        if hasattr(df, "shape"):
            shape = getattr(df, "shape", None)
            if shape is not None and len(shape) >= 1:
                return int(shape[0])

        try:
            return int(len(df))
        except Exception:
            return 0

    except Exception:
        return 0


def df_cols(df: Any) -> int:
    """
    DataFrame風オブジェクトの列数を返す。
    """
    try:
        if df is None:
            return 0

        if hasattr(df, "shape"):
            shape = getattr(df, "shape", None)
            if shape is not None and len(shape) >= 2:
                return int(shape[1])

        cols = getattr(df, "columns", None)
        if cols is not None:
            return int(len(cols))

        return 0

    except Exception:
        return 0


def is_nonempty_df(df: Any) -> bool:
    """
    DataFrameが空でないかを安全に判定する。
    """
    try:
        if df is None:
            return False

        empty = getattr(df, "empty", None)
        if empty is not None:
            return not bool(empty)

        return df_rows(df) > 0

    except Exception:
        return False


def _safe_nunique(series: Any) -> int:
    try:
        return int(series.nunique(dropna=True))
    except Exception:
        return 0


def _safe_nonnull(series: Any) -> int:
    try:
        return int(series.notna().sum())
    except Exception:
        return 0


def _safe_min(series: Any) -> Any:
    try:
        return series.min()
    except Exception:
        return None


def _safe_max(series: Any) -> Any:
    try:
        return series.max()
    except Exception:
        return None


def _parse_log_df_state_args(
    *args: Any,
    label: str = "",
    interval: Optional[int] = None,
    df: Any = None,
    context: str = "",
) -> tuple[Any, str, Optional[int], str]:
    """
    log_df_state の旧形式/新形式を吸収する。

    対応形式:
      1. log_df_state(df, label="xxx", context="yyy")
      2. log_df_state("label", interval, df)
      3. log_df_state("label", df)
      4. log_df_state(df)
    """
    try:
        if len(args) == 0:
            return df, label, interval, context

        # 旧形式: log_df_state("closed-market persisted", interval, df)
        if len(args) >= 3 and isinstance(args[0], str):
            parsed_label = args[0]
            parsed_interval = args[1]
            parsed_df = args[2]

            try:
                parsed_interval = int(parsed_interval)
            except Exception:
                pass

            return parsed_df, parsed_label, parsed_interval, context

        # 旧形式: log_df_state("label", df)
        if len(args) == 2 and isinstance(args[0], str):
            parsed_label = args[0]
            parsed_df = args[1]
            return parsed_df, parsed_label, interval, context

        # 新形式: log_df_state(df, label=..., context=...)
        parsed_df = args[0]
        return parsed_df, label, interval, context

    except Exception:
        return df, label, interval, context


def log_df_state(
    *args: Any,
    label: str = "",
    interval: Optional[int] = None,
    df: Any = None,
    context: str = "",
    level: int = logging.INFO,
    extra_cols: Optional[list[str]] = None,
) -> None:
    """
    DataFrameの状態をログ出力する。

    互換対応:
      - log_df_state(df, label="xxx", context="yyy")
      - log_df_state("closed-market persisted", interval, df)

    closed_market_display.py の既存呼び出し:
      log_df_state("closed-market persisted", interval, df)
    を落とさない。
    """
    try:
        parsed_df, parsed_label, parsed_interval, parsed_context = _parse_log_df_state_args(
            *args,
            label=label,
            interval=interval,
            df=df,
            context=context,
        )

        rows = df_rows(parsed_df)
        cols = df_cols(parsed_df)

        interval_text = ""
        if parsed_interval is not None:
            interval_text = f" interval={parsed_interval}"

        if parsed_df is None:
            logger.log(
                level,
                "[summary.runner_utils] df_state label=%s%s context=%s df=None rows=0 cols=0",
                parsed_label,
                interval_text,
                parsed_context,
            )
            return

        col_names = []
        try:
            col_names = list(getattr(parsed_df, "columns", []))
        except Exception:
            col_names = []

        symbols = 0
        latest_dt = None

        try:
            if "symbol" in col_names:
                symbols = _safe_nunique(parsed_df["symbol"])
        except Exception:
            symbols = 0

        for dt_col in ("datetime", "dt", "timestamp", "snapshot_time"):
            try:
                if dt_col in col_names:
                    latest_dt = _safe_max(parsed_df[dt_col])
                    break
            except Exception:
                pass

        logger.log(
            level,
            "[summary.runner_utils] df_state label=%s%s context=%s rows=%d cols=%d symbols=%d latest_dt=%s",
            parsed_label,
            interval_text,
            parsed_context,
            rows,
            cols,
            symbols,
            latest_dt,
        )

        default_cols = [
            "symbol",
            "datetime",
            "close",
            "close_price",
            "price",
            "volume",
            "score",
            "score_buy",
            "score_sell",
            "buy_score",
            "sell_score",
            "score_total",
            "final_score",
            "display_score",
            "rsi",
            "macd",
            "signal",
            "slope",
            "slope_atr_scaled",
            "mtf",
            "score_mtf",
            "technical_ready",
            "display_ready",
            "source",
        ]

        if extra_cols:
            default_cols.extend(extra_cols)

        seen = set()
        for c in default_cols:
            if c in seen:
                continue
            seen.add(c)

            if c not in col_names:
                continue

            try:
                s = parsed_df[c]
                logger.log(
                    level,
                    "[summary.runner_utils] df_state label=%s%s col=%s non_null=%d nunique=%d min=%s max=%s",
                    parsed_label,
                    interval_text,
                    c,
                    _safe_nonnull(s),
                    _safe_nunique(s),
                    _safe_min(s),
                    _safe_max(s),
                )
            except Exception:
                logger.debug(
                    "[summary.runner_utils] failed to profile col=%s label=%s",
                    c,
                    parsed_label,
                    exc_info=True,
                )

    except Exception:
        logger.exception(
            "[summary.runner_utils] log_df_state failed label=%s context=%s",
            label,
            context,
        )


# ============================================================
# Callable helpers
# ============================================================

def _callable_name(fn: Any) -> str:
    try:
        mod = getattr(fn, "__module__", "")
        name = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", str(fn))
        if mod:
            return f"{mod}.{name}"
        return str(name)
    except Exception:
        return str(fn)


def _filter_kwargs_for_callable(
    fn: Callable[..., Any],
    kwargs: Dict[str, Any],
    *,
    context: str = "",
) -> Dict[str, Any]:
    """
    callable が受け取れる kwargs だけに絞る。

    - **kwargs がある場合は全て渡す
    - signature が取れない場合は安全側でそのまま返す
    - positional-only は kwargs で渡せないので除外
    """
    if not kwargs:
        return {}

    try:
        sig = inspect.signature(fn)
    except Exception:
        logger.debug(
            "[summary.runner_utils] signature unavailable runner=%s context=%s -> pass kwargs as-is",
            _callable_name(fn),
            context,
        )
        return dict(kwargs)

    params = sig.parameters

    has_var_keyword = any(
        p.kind == inspect.Parameter.VAR_KEYWORD
        for p in params.values()
    )

    if has_var_keyword:
        return dict(kwargs)

    allowed = {
        name
        for name, p in params.items()
        if p.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
    }

    filtered = {
        k: v
        for k, v in kwargs.items()
        if k in allowed
    }

    dropped = [
        k
        for k in kwargs.keys()
        if k not in allowed
    ]

    if dropped:
        logger.info(
            "[summary.runner_utils] dropped unsupported kwargs runner=%s context=%s dropped=%s allowed=%s",
            _callable_name(fn),
            context,
            dropped,
            sorted(allowed),
        )

    return filtered


# ============================================================
# Runner call APIs
# ============================================================

def call_runner_with_optional_now(
    runner: Callable[..., Any],
    *,
    interval: Optional[int] = None,
    now: Any = None,
    rebuild_now: Any = None,
    display: Optional[bool] = None,
    force: Optional[bool] = None,
    context: str = "",
    **kwargs: Any,
) -> Any:
    """
    summary runner を安全に呼ぶ。

    runner の種類によって受け取れる引数が違っても落ちないように、
    signature を見て未対応 kwargs を除外する。
    """
    if runner is None or not callable(runner):
        raise TypeError(f"runner is not callable: {runner!r}")

    call_kwargs: Dict[str, Any] = {}

    if interval is not None:
        call_kwargs["interval"] = interval

    effective_now = rebuild_now if rebuild_now is not None else now
    if effective_now is not None:
        call_kwargs["now"] = effective_now

    if rebuild_now is not None:
        call_kwargs["rebuild_now"] = rebuild_now

    if display is not None:
        call_kwargs["display"] = display

    if force is not None:
        call_kwargs["force"] = force

    if context:
        call_kwargs["context"] = context

    for k, v in kwargs.items():
        if v is not None:
            call_kwargs[k] = v

    safe_kwargs = _filter_kwargs_for_callable(
        runner,
        call_kwargs,
        context=context,
    )

    logger.info(
        "[summary.runner_utils] call runner=%s context=%s kwargs=%s",
        _callable_name(runner),
        context,
        sorted(safe_kwargs.keys()),
    )

    return runner(**safe_kwargs)


def call_runner_safely(
    runner: Callable[..., Any],
    **kwargs: Any,
) -> Any:
    """
    汎用安全呼び出し。
    """
    if runner is None or not callable(runner):
        raise TypeError(f"runner is not callable: {runner!r}")

    safe_kwargs = _filter_kwargs_for_callable(
        runner,
        kwargs,
        context=str(kwargs.get("context", "")),
    )

    logger.info(
        "[summary.runner_utils] call safely runner=%s kwargs=%s",
        _callable_name(runner),
        sorted(safe_kwargs.keys()),
    )

    return runner(**safe_kwargs)


__all__ = [
    "env_bool",
    "env_int",
    "env_float",
    "df_rows",
    "df_cols",
    "is_nonempty_df",
    "log_df_state",
    "call_runner_with_optional_now",
    "call_runner_safely",
]