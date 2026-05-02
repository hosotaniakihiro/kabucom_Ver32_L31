# ============================================================
# File   : scheduler_jobs/summary/summary_ai_entry_hook.py
# Version: PRODUCTION-STABLE-SUMMARY-AI-ENTRY-HOOK-V3.0-UNIFIED-4ROUTE
# ------------------------------------------------------------
# 【概要】
#   定時サマリー計算後に、BUY TOP10をAI gateへ確認し、
#   AI_OK銘柄だけ既存entry_pipelineへ渡すためのhook。
#
# 【REV3.0 修正点】
#   - runner.py を最優先で解決
#   - RANKING_SUMMARY / PUSH_SUMMARY / YAHOO_SUMMARY / TONOSAMA の source を保持
#   - RANKING source の場合は runner 側で slope/tonosama を強制しない
#   - unified_router.py が導入済みなら任意で統合候補化できる
#   - 既存呼び出し run_summary_ai_entry_safe(interval, now, df, source=...) は互換維持
#
# 【重要】
#   - 発注は runner/executor 側に集約
#   - このhook自体は直接entry_pipelineを呼ばない
#
# 【ENV】
#   SUMMARY_AI_ENTRY_ENABLED=1
#   SUMMARY_AI_ENTRY_DRY_RUN=0
#   SUMMARY_AI_ENTRY_REQUIRE_MARKET_OPEN=1
#   SUMMARY_AI_ENTRY_TOP_N=10
#   SUMMARY_AI_ENTRY_MAX_ENTRIES=3
#
#   SUMMARY_AI_ENTRY_USE_TONOSAMA_FILTER=1
#   SUMMARY_AI_ENTRY_TONOSAMA_FAIL_OPEN=1
#   SUMMARY_AI_ENTRY_TONOSAMA_MAX_CANDIDATES=10
#
#   SUMMARY_AI_ENTRY_USE_UNIFIED_ROUTER=0
#     1にすると unified_router.py を使って候補DataFrameを正規化してからrunnerへ渡す
#
#   SUMMARY_AI_MIN_TOP10_SLOPE=0.03
#   ENTRY_MIN_BUY_SLOPE=0.03
# ============================================================

from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import inspect
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from .runner_utils import env_bool, env_float, env_int, is_nonempty_df

logger = logging.getLogger(__name__)

_SUMMARY_AI_ENTRY_RUNNER: Optional[Callable[..., object]] = None
_SUMMARY_AI_ENTRY_IMPORT_FAILED = False
_SUMMARY_AI_ENTRY_IMPORT_FAILED_COUNT = 0


# ============================================================
# env config
# ============================================================

def summary_ai_entry_enabled() -> bool:
    return env_bool("SUMMARY_AI_ENTRY_ENABLED", True)


def summary_ai_entry_dry_run() -> bool:
    return env_bool("SUMMARY_AI_ENTRY_DRY_RUN", False)


def summary_ai_entry_require_market_open() -> bool:
    return env_bool("SUMMARY_AI_ENTRY_REQUIRE_MARKET_OPEN", True)


def summary_ai_entry_top_n() -> int:
    return env_int("SUMMARY_AI_ENTRY_TOP_N", 10)


def summary_ai_entry_max_entries() -> int:
    return env_int("SUMMARY_AI_ENTRY_MAX_ENTRIES", 3)


def summary_ai_entry_min_confidence() -> float:
    return env_float("SUMMARY_AI_ENTRY_MIN_CONFIDENCE", 0.65)


def summary_ai_entry_min_buy_score() -> float:
    return env_float("SUMMARY_AI_ENTRY_MIN_BUY_SCORE", 5.0)


def summary_ai_entry_max_sell_score() -> float:
    return env_float("SUMMARY_AI_ENTRY_MAX_SELL_SCORE", 2.0)


def summary_ai_entry_min_volume() -> float:
    return env_float("SUMMARY_AI_ENTRY_MIN_VOLUME", 1.0)


def summary_ai_entry_min_price() -> float:
    return env_float("SUMMARY_AI_ENTRY_MIN_PRICE", 1.0)


def summary_ai_entry_use_tonosama_filter() -> bool:
    return env_bool("SUMMARY_AI_ENTRY_USE_TONOSAMA_FILTER", True)


def summary_ai_entry_tonosama_fail_open() -> bool:
    return env_bool("SUMMARY_AI_ENTRY_TONOSAMA_FAIL_OPEN", True)


def summary_ai_entry_tonosama_max_candidates() -> int:
    return env_int("SUMMARY_AI_ENTRY_TONOSAMA_MAX_CANDIDATES", 10)


def summary_ai_entry_use_unified_router() -> bool:
    return env_bool("SUMMARY_AI_ENTRY_USE_UNIFIED_ROUTER", False)


def summary_ai_entry_min_top10_slope() -> float:
    v = os.environ.get("SUMMARY_AI_MIN_TOP10_SLOPE")
    if v is not None and str(v).strip() != "":
        return env_float("SUMMARY_AI_MIN_TOP10_SLOPE", 0.03)
    return env_float("ENTRY_MIN_BUY_SLOPE", 0.03)


def summary_ai_entry_min_ranking_score() -> float:
    return env_float("RANKING_AI_MIN_SCORE", 0.0)


def summary_ai_entry_min_ranking_momentum() -> float:
    return env_float("RANKING_AI_MIN_MOMENTUM", 0.0)


def summary_ai_entry_tonosama_ranking_db_path() -> Optional[str]:
    v = os.environ.get("SUMMARY_AI_ENTRY_TONOSAMA_RANKING_DB_PATH", "")
    v = str(v or "").strip()
    return v or None


# ============================================================
# source helpers
# ============================================================

def _normalize_source(source: Any) -> str:
    s = str(source or "SUMMARY").strip().upper()
    return s or "SUMMARY"


def _is_ranking_source(source: Any) -> bool:
    return "RANKING" in _normalize_source(source)


def _is_tonosama_source(source: Any) -> bool:
    return "TONOSAMA" in _normalize_source(source)


def _is_yahoo_source(source: Any) -> bool:
    return "YAHOO" in _normalize_source(source)


def _is_push_source(source: Any) -> bool:
    s = _normalize_source(source)
    return "PUSH" in s or s in {"SUMMARY", "STOCK_SUMMARY", "PUSH_SUMMARY"}


def _effective_use_tonosama_filter(source: str) -> bool:
    """
    RANKING_SUMMARY 自体に殿様フィルタを重ねると候補が消えやすい。
    そのため ranking source ではOFF。
    """
    if _is_ranking_source(source):
        return False
    return summary_ai_entry_use_tonosama_filter()


def _effective_use_pre_slope_filter(source: str) -> bool:
    """
    RANKING/Tonosama単独は本物ATR/slope前提ではないためOFF。
    PUSH/Yahoo/通常SUMMARYではON。
    """
    if _is_ranking_source(source):
        return False
    if _is_tonosama_source(source):
        return False
    return True


# ============================================================
# resolver diagnostics
# ============================================================

def _callable_attrs_sample(mod: Any) -> list[str]:
    try:
        names: list[str] = []
        for name in dir(mod):
            low = str(name).lower()
            if (
                "run" in low
                or "entry" in low
                or "gate" in low
                or "summary" in low
                or "start" in low
            ):
                names.append(str(name))
        return sorted(names)[:80]
    except Exception:
        return []


def _project_root_candidates() -> list[Path]:
    roots: list[Path] = []

    try:
        roots.append(Path.cwd())
    except Exception:
        pass

    try:
        p = Path(__file__).resolve()
        for parent in p.parents:
            roots.append(parent)
    except Exception:
        pass

    try:
        env_root = os.environ.get("PROJECT_ROOT") or os.environ.get("KABU_PROJECT_ROOT")
        if env_root:
            roots.append(Path(env_root))
    except Exception:
        pass

    uniq: list[Path] = []
    seen: set[str] = set()

    for r in roots:
        try:
            rr = r.resolve()
            key = str(rr).lower()
            if key not in seen:
                uniq.append(rr)
                seen.add(key)
        except Exception:
            continue

    return uniq


def _find_file(rel: Path) -> Optional[Path]:
    for root in _project_root_candidates():
        try:
            path = root / rel
            if path.exists() and path.is_file():
                return path
        except Exception:
            continue
    return None


def _find_summary_ai_runner_file() -> Optional[Path]:
    return _find_file(Path("trading") / "entry" / "summary_ai" / "runner.py")


def _find_ai_gate_runner_file() -> Optional[Path]:
    return _find_file(Path("trading") / "entry" / "summary_ai" / "ai_gate_runner.py")


# ============================================================
# resolver
# ============================================================

def _try_resolve_callable(module_name: str, func_name: str):
    try:
        mod = importlib.import_module(module_name)
        fn = getattr(mod, func_name, None)

        if callable(fn):
            logger.info(
                "[summary.runners] summary AI entry runner resolved %s.%s module_file=%s",
                module_name,
                func_name,
                getattr(mod, "__file__", None),
            )
            return fn

        logger.warning(
            "[summary.runners] summary AI entry callable not found module=%s func=%s "
            "module_file=%s attrs_sample=%s",
            module_name,
            func_name,
            getattr(mod, "__file__", None),
            _callable_attrs_sample(mod),
        )

    except Exception as e:
        logger.warning(
            "[summary.runners] summary AI entry resolve failed module=%s func=%s "
            "error=%s: %s",
            module_name,
            func_name,
            type(e).__name__,
            e,
        )
        logger.debug(
            "[summary.runners] summary AI entry resolve traceback module=%s func=%s\n%s",
            module_name,
            func_name,
            traceback.format_exc(),
        )

    return None


def _try_load_runner_from_file(path: Optional[Path], module_key: str, func_name: str):
    if path is None:
        logger.warning(
            "[summary.runners] direct runner file not found module_key=%s roots=%s",
            module_key,
            [str(x) for x in _project_root_candidates()[:8]],
        )
        return None

    try:
        spec = importlib.util.spec_from_file_location(module_key, str(path))
        if spec is None or spec.loader is None:
            logger.warning(
                "[summary.runners] direct runner spec failed path=%s func=%s",
                path,
                func_name,
            )
            return None

        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_key] = mod
        spec.loader.exec_module(mod)

        fn = getattr(mod, func_name, None)
        if callable(fn):
            logger.info(
                "[summary.runners] summary AI entry runner resolved direct-file path=%s func=%s",
                path,
                func_name,
            )
            return fn

        logger.warning(
            "[summary.runners] direct runner callable not found path=%s func=%s attrs_sample=%s",
            path,
            func_name,
            _callable_attrs_sample(mod),
        )

    except Exception as e:
        logger.warning(
            "[summary.runners] direct runner load failed path=%s func=%s error=%s: %s",
            path,
            func_name,
            type(e).__name__,
            e,
        )
        logger.debug(
            "[summary.runners] direct runner traceback path=%s func=%s\n%s",
            path,
            func_name,
            traceback.format_exc(),
        )

    return None


def resolve_summary_ai_entry_runner(*, force_refresh: bool = False):
    global _SUMMARY_AI_ENTRY_RUNNER
    global _SUMMARY_AI_ENTRY_IMPORT_FAILED
    global _SUMMARY_AI_ENTRY_IMPORT_FAILED_COUNT

    if force_refresh:
        _SUMMARY_AI_ENTRY_RUNNER = None
        _SUMMARY_AI_ENTRY_IMPORT_FAILED = False

    if _SUMMARY_AI_ENTRY_RUNNER is not None:
        return _SUMMARY_AI_ENTRY_RUNNER

    _SUMMARY_AI_ENTRY_IMPORT_FAILED = False

    candidates = [
        # 最新 runner.py
        ("trading.entry.summary_ai.runner", "run_push_summary_ai_entry"),
        ("trading.entry.summary_ai.runner", "run_summary_ai_entry_from_df"),
        ("trading.entry.summary_ai.runner", "run_yahoo_summary_ai_entry"),
        ("trading.entry.summary_ai.runner", "run_ranking_summary_ai_entry"),
        ("trading.entry.summary_ai.runner", "run_tonosama_summary_ai_entry"),
        ("trading.entry.summary_ai.runner", "run_summary_ai_gate"),
        ("trading.entry.summary_ai.runner", "run_ai_gate_once"),

        # package公開API
        ("trading.entry.summary_ai", "run_push_summary_ai_entry"),
        ("trading.entry.summary_ai", "run_summary_ai_entry_from_df"),
        ("trading.entry.summary_ai", "run_yahoo_summary_ai_entry"),
        ("trading.entry.summary_ai", "run_ranking_summary_ai_entry"),
        ("trading.entry.summary_ai", "run_summary_ai_gate"),
        ("trading.entry.summary_ai", "run_ai_gate_once"),

        # 旧/別実体
        ("trading.entry.summary_ai.ai_gate_runner", "run_push_summary_ai_entry"),
        ("trading.entry.summary_ai.ai_gate_runner", "run_summary_ai_entry_from_df"),
        ("trading.entry.summary_ai.ai_gate_runner", "run_summary_ai_gate"),
        ("trading.entry.summary_ai.ai_gate_runner", "run_ai_gate_once"),
        ("trading.entry.summary_ai.ai_gate_runner", "start"),
        ("trading.entry.summary_ai.ai_gate_runner", "run"),

        # 旧 shim
        ("trading.entry.summary_ai_entry_runner", "run_push_summary_ai_entry"),
        ("trading.entry.summary_ai_entry_runner", "run_summary_ai_entry_from_df"),
        ("trading.entry.summary_ai_entry_runner", "run_summary_ai_gate"),
        ("trading.entry.summary_ai_entry_runner", "run_ai_gate_once"),
    ]

    for module_name, func_name in candidates:
        fn = _try_resolve_callable(module_name, func_name)
        if callable(fn):
            _SUMMARY_AI_ENTRY_RUNNER = fn
            _SUMMARY_AI_ENTRY_IMPORT_FAILED = False
            _SUMMARY_AI_ENTRY_IMPORT_FAILED_COUNT = 0
            return _SUMMARY_AI_ENTRY_RUNNER

    direct_func_candidates = [
        "run_push_summary_ai_entry",
        "run_summary_ai_entry_from_df",
        "run_yahoo_summary_ai_entry",
        "run_ranking_summary_ai_entry",
        "run_tonosama_summary_ai_entry",
        "run_summary_ai_gate",
        "run_ai_gate_once",
        "start",
        "run",
    ]

    runner_path = _find_summary_ai_runner_file()
    for func_name in direct_func_candidates:
        fn = _try_load_runner_from_file(
            runner_path,
            "_direct_trading_entry_summary_ai_runner",
            func_name,
        )
        if callable(fn):
            _SUMMARY_AI_ENTRY_RUNNER = fn
            _SUMMARY_AI_ENTRY_IMPORT_FAILED = False
            _SUMMARY_AI_ENTRY_IMPORT_FAILED_COUNT = 0
            return _SUMMARY_AI_ENTRY_RUNNER

    ai_gate_path = _find_ai_gate_runner_file()
    for func_name in direct_func_candidates:
        fn = _try_load_runner_from_file(
            ai_gate_path,
            "_direct_trading_entry_summary_ai_ai_gate_runner",
            func_name,
        )
        if callable(fn):
            _SUMMARY_AI_ENTRY_RUNNER = fn
            _SUMMARY_AI_ENTRY_IMPORT_FAILED = False
            _SUMMARY_AI_ENTRY_IMPORT_FAILED_COUNT = 0
            return _SUMMARY_AI_ENTRY_RUNNER

    _SUMMARY_AI_ENTRY_IMPORT_FAILED = True
    _SUMMARY_AI_ENTRY_IMPORT_FAILED_COUNT += 1

    logger.warning(
        "[summary.runners] summary AI entry runner import failed count=%s "
        "module_candidates=%s direct_candidates=%s",
        _SUMMARY_AI_ENTRY_IMPORT_FAILED_COUNT,
        len(candidates),
        len(direct_func_candidates),
    )

    return None


def resolve_summary_ai_entry_runner_for_source(
    source: str,
    *,
    force_refresh: bool = False,
):
    """
    source別に最適なrunner関数を優先解決する。
    """
    source_s = _normalize_source(source)

    if _is_ranking_source(source_s):
        source_candidates = [
            ("trading.entry.summary_ai.runner", "run_ranking_summary_ai_entry"),
            ("trading.entry.summary_ai", "run_ranking_summary_ai_entry"),
            ("trading.entry.summary_ai.runner", "run_summary_ai_entry_from_df"),
        ]
    elif _is_yahoo_source(source_s):
        source_candidates = [
            ("trading.entry.summary_ai.runner", "run_yahoo_summary_ai_entry"),
            ("trading.entry.summary_ai", "run_yahoo_summary_ai_entry"),
            ("trading.entry.summary_ai.runner", "run_summary_ai_entry_from_df"),
        ]
    elif _is_tonosama_source(source_s):
        source_candidates = [
            ("trading.entry.summary_ai.runner", "run_tonosama_summary_ai_entry"),
            ("trading.entry.summary_ai.runner", "run_summary_ai_entry_from_df"),
        ]
    else:
        source_candidates = [
            ("trading.entry.summary_ai.runner", "run_push_summary_ai_entry"),
            ("trading.entry.summary_ai.runner", "run_summary_ai_entry_from_df"),
            ("trading.entry.summary_ai", "run_push_summary_ai_entry"),
        ]

    for module_name, func_name in source_candidates:
        fn = _try_resolve_callable(module_name, func_name)
        if callable(fn):
            return fn

    return resolve_summary_ai_entry_runner(force_refresh=force_refresh)


# ============================================================
# optional unified router
# ============================================================

def _try_build_unified_df(
    *,
    df: pd.DataFrame,
    source: str,
    interval: int,
) -> pd.DataFrame:
    """
    unified_router.py が存在し、ENVでONの場合だけ使う。
    単一df呼び出しでも source に応じて適切な入力枠へ入れる。
    """
    if not summary_ai_entry_use_unified_router():
        return df

    try:
        from trading.entry.summary_ai.unified_router import build_unified_ai_candidates
    except Exception:
        logger.warning(
            "[summary.runners] unified_router unavailable; use original df source=%s interval=%s",
            source,
            interval,
            exc_info=True,
        )
        return df

    kwargs: dict[str, Any] = {
        "ranking_summary_df": None,
        "push_summary_df": None,
        "yahoo_summary_df": None,
        "tonosama_df": None,
        "max_candidates": summary_ai_entry_top_n(),
        "require_real_technical_for_entry": False,
        "min_slope_atr_scaled": summary_ai_entry_min_top10_slope(),
        "min_buy_score": summary_ai_entry_min_buy_score(),
        "max_sell_score": summary_ai_entry_max_sell_score(),
    }

    source_s = _normalize_source(source)

    if _is_ranking_source(source_s):
        kwargs["ranking_summary_df"] = df
    elif _is_yahoo_source(source_s):
        kwargs["yahoo_summary_df"] = df
    elif _is_tonosama_source(source_s):
        kwargs["tonosama_df"] = df
    else:
        kwargs["push_summary_df"] = df

    try:
        unified_df = build_unified_ai_candidates(**kwargs)

        if isinstance(unified_df, pd.DataFrame) and not unified_df.empty:
            logger.info(
                "[summary.runners] unified_router built source=%s interval=%s before=%s after=%s symbols=%s",
                source_s,
                interval,
                len(df),
                len(unified_df),
                unified_df["symbol"].nunique() if "symbol" in unified_df.columns else 0,
            )
            return unified_df

        logger.warning(
            "[summary.runners] unified_router returned empty; fallback original source=%s interval=%s rows=%s",
            source_s,
            interval,
            len(df),
        )
        return df

    except Exception:
        logger.warning(
            "[summary.runners] unified_router failed; fallback original source=%s interval=%s",
            source_s,
            interval,
            exc_info=True,
        )
        return df


# ============================================================
# runner invocation compatibility
# ============================================================

def _filter_kwargs_for_callable(fn: Callable[..., Any], kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        sig = inspect.signature(fn)
        params = sig.parameters

        has_var_kw = any(
            p.kind == inspect.Parameter.VAR_KEYWORD
            for p in params.values()
        )
        if has_var_kw:
            return dict(kwargs)

        allowed = set(params.keys())
        return {k: v for k, v in kwargs.items() if k in allowed}

    except Exception:
        logger.debug("[summary.runners] inspect signature failed; pass all kwargs", exc_info=True)
        return dict(kwargs)


def _invoke_runner_compat(
    fn: Callable[..., Any],
    *,
    df: pd.DataFrame,
    interval: int,
    source: str,
    now: dt.datetime,
    dry_run: bool,
    require_market_open: bool,
) -> object:
    source_s = _normalize_source(source)

    common_kwargs: dict[str, Any] = {
        "summary_df": df,
        "df": df,

        "interval": interval,
        "interval_label": f"{int(interval)}min",
        "source": source_s,
        "now": now,

        "top_n": summary_ai_entry_top_n(),
        "max_entries": summary_ai_entry_max_entries(),

        "min_ai_confidence": summary_ai_entry_min_confidence(),
        "min_confidence": summary_ai_entry_min_confidence(),
        "min_conf": summary_ai_entry_min_confidence(),

        "min_buy_score": summary_ai_entry_min_buy_score(),
        "max_sell_score": summary_ai_entry_max_sell_score(),
        "min_volume": summary_ai_entry_min_volume(),
        "min_price": summary_ai_entry_min_price(),

        "require_buy_target": False,
        "exclude_etf_fund": True,
        "require_market_open": require_market_open,
        "dry_run": dry_run,
        "default_dominant_ratio": 1.0,

        "use_tonosama_filter": _effective_use_tonosama_filter(source_s),
        "tonosama_ranking_db_path": summary_ai_entry_tonosama_ranking_db_path(),
        "tonosama_max_candidates": summary_ai_entry_tonosama_max_candidates(),
        "fail_open_tonosama": summary_ai_entry_tonosama_fail_open(),

        "use_pre_slope_filter": _effective_use_pre_slope_filter(source_s),
        "min_top10_slope": summary_ai_entry_min_top10_slope(),
        "min_ranking_score": summary_ai_entry_min_ranking_score(),
        "min_ranking_momentum": summary_ai_entry_min_ranking_momentum(),
        "use_entry_dedupe_guard": True,
    }

    kwargs = _filter_kwargs_for_callable(fn, common_kwargs)

    logger.info(
        "[summary.runners] invoking summary AI entry runner fn=%s kwargs=%s "
        "source=%s tonosama=%s pre_slope=%s unified=%s",
        getattr(fn, "__name__", repr(fn)),
        sorted(kwargs.keys()),
        source_s,
        _effective_use_tonosama_filter(source_s),
        _effective_use_pre_slope_filter(source_s),
        summary_ai_entry_use_unified_router(),
    )

    try:
        return fn(**kwargs)

    except TypeError as e:
        logger.warning(
            "[summary.runners] summary AI entry runner kwargs call failed "
            "fn=%s typeerror=%s -> retry compact",
            getattr(fn, "__name__", repr(fn)),
            e,
        )

        compact_kwargs = {
            "summary_df": df,
            "df": df,
            "interval": interval,
            "source": source_s,
            "top_n": summary_ai_entry_top_n(),
            "max_entries": summary_ai_entry_max_entries(),
            "min_ai_confidence": summary_ai_entry_min_confidence(),
            "min_confidence": summary_ai_entry_min_confidence(),
            "min_conf": summary_ai_entry_min_confidence(),
            "dry_run": dry_run,
            "require_market_open": require_market_open,
            "use_tonosama_filter": _effective_use_tonosama_filter(source_s),
            "tonosama_ranking_db_path": summary_ai_entry_tonosama_ranking_db_path(),
            "tonosama_max_candidates": summary_ai_entry_tonosama_max_candidates(),
            "fail_open_tonosama": summary_ai_entry_tonosama_fail_open(),
            "use_pre_slope_filter": _effective_use_pre_slope_filter(source_s),
            "min_top10_slope": summary_ai_entry_min_top10_slope(),
        }

        compact_kwargs = _filter_kwargs_for_callable(fn, compact_kwargs)

        try:
            return fn(**compact_kwargs)
        except TypeError:
            logger.warning(
                "[summary.runners] summary AI entry compact kwargs failed fn=%s -> retry positional",
                getattr(fn, "__name__", repr(fn)),
                exc_info=True,
            )
            return fn(df)


# ============================================================
# result helper
# ============================================================

def _len_maybe(v: Any) -> int:
    try:
        if v is None:
            return 0
        return len(v)
    except Exception:
        return 0


def _result_to_dict(result: object) -> dict[str, Any]:
    if isinstance(result, dict):
        return result

    if isinstance(result, pd.DataFrame):
        return {
            "candidates": result,
            "ai_results": result,
            "ai_ok": result,
            "approved_rows": result,
            "execution": {
                "executed": False,
                "skip_reason": "runner_returned_dataframe",
            },
        }

    if isinstance(result, list):
        return {
            "candidates": result,
            "ai_results": result,
            "ai_ok": result,
            "approved_rows": result,
            "execution": {
                "executed": False,
                "skip_reason": "runner_returned_list",
            },
        }

    return {
        "candidates": [],
        "ai_results": [],
        "ai_ok": [],
        "approved_rows": [],
        "execution": {
            "executed": False,
            "skip_reason": f"runner_returned_{type(result).__name__}",
        },
    }


def _object_to_records(obj: Any) -> list[Any]:
    if obj is None:
        return []
    if isinstance(obj, pd.DataFrame):
        if obj.empty:
            return []
        return obj.to_dict("records")
    if isinstance(obj, list):
        return obj
    try:
        return list(obj)
    except Exception:
        return []


# ============================================================
# public hooks
# ============================================================

def run_summary_ai_entry_safe(
    interval: int,
    now: dt.datetime,
    df: Optional[pd.DataFrame] = None,
    *,
    source: str = "SUMMARY",
) -> bool:
    try:
        interval = int(interval)
        now = (now or dt.datetime.now()).replace(microsecond=0)
        source_s = _normalize_source(source)

        if not summary_ai_entry_enabled():
            logger.info(
                "[summary.runners] summary AI entry skipped interval=%s now=%s source=%s reason=disabled_env",
                interval,
                now,
                source_s,
            )
            return False

        if df is not None and not is_nonempty_df(df):
            logger.warning(
                "[summary.runners] summary AI entry skipped interval=%s now=%s source=%s reason=empty_summary_df",
                interval,
                now,
                source_s,
            )
            return False

        if df is None or not isinstance(df, pd.DataFrame):
            logger.warning(
                "[summary.runners] summary AI entry skipped interval=%s now=%s source=%s "
                "reason=df_unavailable type=%s",
                interval,
                now,
                source_s,
                type(df).__name__,
            )
            return False

        df_for_runner = _try_build_unified_df(
            df=df,
            source=source_s,
            interval=interval,
        )

        fn = resolve_summary_ai_entry_runner_for_source(source_s)

        if not callable(fn):
            fn = resolve_summary_ai_entry_runner_for_source(source_s, force_refresh=True)

        if not callable(fn):
            logger.warning(
                "[summary.runners] summary AI entry runner unavailable interval=%s now=%s source=%s",
                interval,
                now,
                source_s,
            )
            return False

        dry_run = summary_ai_entry_dry_run()
        require_market_open = summary_ai_entry_require_market_open()

        logger.info(
            "[summary.runners] summary AI entry start interval=%s now=%s source=%s rows=%d "
            "runner=%s dry_run=%s require_market_open=%s top_n=%s max_entries=%s min_conf=%.2f "
            "min_buy=%.2f max_sell=%.2f min_volume=%.1f min_price=%.1f "
            "tonosama=%s tonosama_max=%s tonosama_fail_open=%s pre_slope=%s min_slope=%.4f unified=%s",
            interval,
            now,
            source_s,
            len(df_for_runner),
            getattr(fn, "__name__", repr(fn)),
            dry_run,
            require_market_open,
            summary_ai_entry_top_n(),
            summary_ai_entry_max_entries(),
            summary_ai_entry_min_confidence(),
            summary_ai_entry_min_buy_score(),
            summary_ai_entry_max_sell_score(),
            summary_ai_entry_min_volume(),
            summary_ai_entry_min_price(),
            _effective_use_tonosama_filter(source_s),
            summary_ai_entry_tonosama_max_candidates(),
            summary_ai_entry_tonosama_fail_open(),
            _effective_use_pre_slope_filter(source_s),
            summary_ai_entry_min_top10_slope(),
            summary_ai_entry_use_unified_router(),
        )

        result = _invoke_runner_compat(
            fn,
            df=df_for_runner,
            interval=interval,
            source=source_s,
            now=now,
            dry_run=dry_run,
            require_market_open=require_market_open,
        )

        result_dict = _result_to_dict(result)

        candidates = _object_to_records(result_dict.get("candidates"))
        ai_results = _object_to_records(result_dict.get("ai_results"))
        ai_ok = _object_to_records(result_dict.get("ai_ok"))
        approved_rows = _object_to_records(result_dict.get("approved_rows"))

        execution = result_dict.get("execution") or {}
        executed = bool(execution.get("executed")) if isinstance(execution, dict) else False
        skip_reason = execution.get("skip_reason") if isinstance(execution, dict) else None

        logger.info(
            "[summary.runners] summary AI entry done interval=%s now=%s source=%s "
            "candidates=%s ai_results=%s ai_ok=%s approved=%s executed=%s dry_run=%s skip=%s",
            interval,
            now,
            source_s,
            _len_maybe(candidates),
            _len_maybe(ai_results),
            _len_maybe(ai_ok),
            _len_maybe(approved_rows),
            executed,
            dry_run,
            skip_reason,
        )

        return True

    except Exception:
        logger.exception(
            "[summary.runners] summary AI entry failed interval=%s now=%s source=%s",
            interval,
            now,
            source,
        )
        return False


# ============================================================
# multi-route optional helper
# ============================================================

def run_summary_ai_entry_unified_safe(
    *,
    interval: int,
    now: dt.datetime,
    ranking_df: Optional[pd.DataFrame] = None,
    push_df: Optional[pd.DataFrame] = None,
    yahoo_df: Optional[pd.DataFrame] = None,
    tonosama_df: Optional[pd.DataFrame] = None,
    source: str = "UNIFIED",
) -> bool:
    """
    4ルートDataFrameを同時に受けて unified_router で統合してからAI runnerへ渡す。
    既存スケジューラが単一dfしか渡せない場合は run_summary_ai_entry_safe を使う。
    """
    try:
        try:
            from trading.entry.summary_ai.unified_router import build_unified_ai_candidates
        except Exception:
            logger.warning(
                "[summary.runners] unified safe skipped because unified_router import failed",
                exc_info=True,
            )
            return False

        interval = int(interval)
        now = (now or dt.datetime.now()).replace(microsecond=0)

        unified_df = build_unified_ai_candidates(
            ranking_summary_df=ranking_df,
            push_summary_df=push_df,
            yahoo_summary_df=yahoo_df,
            tonosama_df=tonosama_df,
            max_candidates=summary_ai_entry_top_n(),
            require_real_technical_for_entry=False,
            min_slope_atr_scaled=summary_ai_entry_min_top10_slope(),
            min_buy_score=summary_ai_entry_min_buy_score(),
            max_sell_score=summary_ai_entry_max_sell_score(),
        )

        if unified_df is None or unified_df.empty:
            logger.warning(
                "[summary.runners] unified summary AI entry skipped interval=%s now=%s reason=empty_unified_df",
                interval,
                now,
            )
            return False

        return run_summary_ai_entry_safe(
            interval=interval,
            now=now,
            df=unified_df,
            source=source,
        )

    except Exception:
        logger.exception(
            "[summary.runners] unified summary AI entry failed interval=%s now=%s source=%s",
            interval,
            now,
            source,
        )
        return False


# ============================================================
# compatibility aliases
# ============================================================

def run_summary_ai_entry(
    interval: int,
    now: dt.datetime,
    df: Optional[pd.DataFrame] = None,
    *,
    source: str = "SUMMARY",
) -> bool:
    return run_summary_ai_entry_safe(
        interval=interval,
        now=now,
        df=df,
        source=source,
    )


def run_summary_ai_entry_hook(
    interval: int,
    now: dt.datetime,
    df: Optional[pd.DataFrame] = None,
    *,
    source: str = "SUMMARY",
) -> bool:
    return run_summary_ai_entry_safe(
        interval=interval,
        now=now,
        df=df,
        source=source,
    )


__all__ = [
    "summary_ai_entry_enabled",
    "summary_ai_entry_dry_run",
    "summary_ai_entry_require_market_open",
    "summary_ai_entry_top_n",
    "summary_ai_entry_max_entries",
    "summary_ai_entry_min_confidence",
    "summary_ai_entry_min_buy_score",
    "summary_ai_entry_max_sell_score",
    "summary_ai_entry_min_volume",
    "summary_ai_entry_min_price",
    "summary_ai_entry_use_tonosama_filter",
    "summary_ai_entry_tonosama_fail_open",
    "summary_ai_entry_tonosama_max_candidates",
    "summary_ai_entry_tonosama_ranking_db_path",
    "summary_ai_entry_use_unified_router",
    "resolve_summary_ai_entry_runner",
    "resolve_summary_ai_entry_runner_for_source",
    "run_summary_ai_entry_safe",
    "run_summary_ai_entry_unified_safe",
    "run_summary_ai_entry",
    "run_summary_ai_entry_hook",
]