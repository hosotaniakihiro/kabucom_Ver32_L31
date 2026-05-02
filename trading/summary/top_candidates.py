# ============================================================
# File   : trading/summary/top_candidates.py
# Version: Ver3.1-PRODUCTION-SUMMARY-TOP-CANDIDATES-FACADE
# ------------------------------------------------------------
# Function:
#   - top_candidates の後方互換 facade
#   - 旧 import パスを維持する
#   - 実装本体は trading.summary.top_candidates_pkg.* に分割
#   - import 時の循環参照 / 部分初期化に強い lazy resolve
#   - 実装配置ゆれに対して複数候補モジュールを探索
#   - DataFrame戻り値へ score_reasons 要約列を自動付与
# ------------------------------------------------------------
# Existing compatible APIs:
#   ✔ prepare_buy_sell_top_df()
#   ✔ prepare_buy_top_df()
#   ✔ prepare_sell_top_df()
# ------------------------------------------------------------
# AI Entry APIs:
#   ✔ collect_push_summary_candidates()
#   ✔ collect_ranking_summary_candidates()
#   ✔ merge_ai_entry_candidates()
#   ✔ collect_ai_entry_candidates()
#   ✔ collect_top_candidates_for_ai()
#   ✔ log_ai_entry_candidates()
# ------------------------------------------------------------
# Added helpers:
#   ✔ format_score_reasons()
#   ✔ get_top_score_reasons()
#   ✔ attach_score_reason_columns()
# ============================================================

from __future__ import annotations

import ast
import importlib
import logging
import traceback
from types import ModuleType
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# 公開 API
# ------------------------------------------------------------
_EXPORTS = (
    "prepare_buy_sell_top_df",
    "prepare_buy_top_df",
    "prepare_sell_top_df",
    "collect_push_summary_candidates",
    "collect_ranking_summary_candidates",
    "merge_ai_entry_candidates",
    "collect_ai_entry_candidates",
    "collect_top_candidates_for_ai",
    "log_ai_entry_candidates",
    "format_score_reasons",
    "get_top_score_reasons",
    "attach_score_reason_columns",
    "preload_top_candidates_exports",
    "get_facade_resolution_cache",
    "get_facade_resolution_errors",
)

__all__ = list(_EXPORTS)

# ------------------------------------------------------------
# lazy resolve cache
# ------------------------------------------------------------
_RESOLVED: Dict[str, Callable[..., Any]] = {}
_RESOLVE_ERRORS: Dict[str, str] = {}

# ------------------------------------------------------------
# 実装探索候補
# ------------------------------------------------------------
_MODULE_CANDIDATES = (
    "trading.summary.top_candidates_pkg",
    "trading.summary.top_candidates_pkg.api",
    "trading.summary.top_candidates_pkg.core",
    "trading.summary.top_candidates_pkg.prepare",
    "trading.summary.top_candidates_pkg.buy",
    "trading.summary.top_candidates_pkg.sell",
    "trading.summary.top_candidates_pkg.collectors",
    "trading.summary.top_candidates_pkg.merge",
    "trading.summary.top_candidates_pkg.ai_entry",
    "trading.summary.top_candidates_pkg.logging_utils",
    "trading.summary.top_candidates_pkg.loggers",
    "trading.summary.top_candidates_impl",
    "trading.summary.top_candidates_legacy",
)

# ------------------------------------------------------------
# DataFrame post-process 対象 API
# ------------------------------------------------------------
_DF_RETURNING_APIS = {
    "prepare_buy_sell_top_df",
    "prepare_buy_top_df",
    "prepare_sell_top_df",
    "collect_push_summary_candidates",
    "collect_ranking_summary_candidates",
    "merge_ai_entry_candidates",
    "collect_ai_entry_candidates",
    "collect_top_candidates_for_ai",
}

# ============================================================
# util
# ============================================================


def _short_exc() -> str:
    try:
        return traceback.format_exc(limit=2)
    except Exception:
        return "traceback unavailable"


def _iter_candidate_modules() -> Iterable[str]:
    for mod_name in _MODULE_CANDIDATES:
        yield mod_name


def _import_module_safely(module_name: str) -> Optional[ModuleType]:
    try:
        return importlib.import_module(module_name)
    except Exception:
        logger.debug(
            "[summary.top_candidates.facade] import miss module=%s\n%s",
            module_name,
            _short_exc(),
        )
        return None


def _resolve_attr_from_module(module: ModuleType, attr: str) -> Optional[Callable[..., Any]]:
    try:
        fn = getattr(module, attr, None)
        if callable(fn):
            return fn
    except Exception:
        logger.debug(
            "[summary.top_candidates.facade] getattr failed module=%s attr=%s\n%s",
            getattr(module, "__name__", "<unknown>"),
            attr,
            _short_exc(),
        )
    return None


def _resolve_export(name: str) -> Callable[..., Any]:
    """
    公開関数 name を lazy resolve する。
    import 時の循環参照を避けるため、呼び出し時に解決する。
    """
    if name in _RESOLVED:
        return _RESOLVED[name]

    for module_name in _iter_candidate_modules():
        module = _import_module_safely(module_name)
        if module is None:
            continue

        fn = _resolve_attr_from_module(module, name)
        if fn is not None:
            _RESOLVED[name] = fn
            logger.debug(
                "[summary.top_candidates.facade] resolved name=%s module=%s",
                name,
                module_name,
            )
            return fn

    last_error = (
        f"top_candidates facade failed to resolve export '{name}'. "
        f"Tried modules={list(_iter_candidate_modules())}"
    )
    _RESOLVE_ERRORS[name] = last_error
    logger.error("[summary.top_candidates.facade] %s", last_error)
    raise ImportError(last_error)


# ============================================================
# score_reasons helper
# ============================================================


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        if isinstance(v, bool):
            return int(v)
        return int(float(v))
    except Exception:
        return default


def _normalize_score_reasons(value: Any) -> Dict[str, int]:
    """
    score_reasons を dict[str, int] へ正規化する。
    対応:
      - dict
      - None / NaN
      - "{'a': 1, 'b': 2}" 形式文字列
      - list[tuple] / list[list]
    """
    if value is None:
        return {}

    try:
        if pd.isna(value):
            return {}
    except Exception:
        pass

    if isinstance(value, dict):
        out: Dict[str, int] = {}
        for k, v in value.items():
            key = str(k).strip()
            if not key:
                continue
            out[key] = out.get(key, 0) + _safe_int(v, 0)
        return out

    if isinstance(value, (list, tuple)):
        out: Dict[str, int] = {}
        for item in value:
            try:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    key = str(item[0]).strip()
                    if not key:
                        continue
                    out[key] = out.get(key, 0) + _safe_int(item[1], 0)
            except Exception:
                continue
        return out

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return {}
        try:
            parsed = ast.literal_eval(text)
            return _normalize_score_reasons(parsed)
        except Exception:
            # "a:1,b:2" 形式の簡易対応
            out: Dict[str, int] = {}
            for chunk in text.split(","):
                if ":" not in chunk:
                    continue
                k, v = chunk.split(":", 1)
                key = k.strip()
                if not key:
                    continue
                out[key] = out.get(key, 0) + _safe_int(v.strip(), 0)
            return out

    return {}


def get_top_score_reasons(
    value: Any,
    top_n: int = 3,
    *,
    include_negative: bool = True,
    sort_by_abs: bool = True,
) -> List[Tuple[str, int]]:
    """
    score_reasons から上位理由を返す。

    Parameters
    ----------
    value : Any
        score_reasons
    top_n : int
        上位件数
    include_negative : bool
        負スコアも含めるか
    sort_by_abs : bool
        True の場合は絶対値順、False の場合は値降順
    """
    reasons = _normalize_score_reasons(value)
    if not reasons:
        return []

    items = list(reasons.items())

    if not include_negative:
        items = [(k, v) for k, v in items if v > 0]

    if sort_by_abs:
        items.sort(key=lambda x: (abs(_safe_int(x[1])), _safe_int(x[1])), reverse=True)
    else:
        items.sort(key=lambda x: _safe_int(x[1]), reverse=True)

    return items[: max(0, int(top_n))]


def format_score_reasons(
    value: Any,
    top_n: int = 3,
    *,
    include_negative: bool = True,
    sort_by_abs: bool = True,
    sep: str = " / ",
    with_score: bool = True,
) -> str:
    """
    score_reasons を表示用文字列へ整形する。
    """
    items = get_top_score_reasons(
        value,
        top_n=top_n,
        include_negative=include_negative,
        sort_by_abs=sort_by_abs,
    )
    if not items:
        return ""

    parts: List[str] = []
    for key, score in items:
        if with_score:
            sign = f"{score:+d}"
            parts.append(f"{key}({sign})")
        else:
            parts.append(str(key))

    return sep.join(parts)


def attach_score_reason_columns(
    df: Optional[pd.DataFrame],
    *,
    source_col: str = "score_reasons",
    top3_col: str = "score_reason_top3",
    top5_col: str = "score_reason_top5",
    summary_col: str = "score_reason_summary",
    include_negative: bool = True,
    sort_by_abs: bool = True,
) -> Optional[pd.DataFrame]:
    """
    DataFrame に score_reasons 要約列を付与する。

    追加列:
      - score_reason_top3
      - score_reason_top5
      - score_reason_summary (= top3)
    """
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return df

    if source_col not in df.columns:
        # 列が無くても空列を保証
        out = df.copy()
        for c in (top3_col, top5_col, summary_col):
            if c not in out.columns:
                out[c] = ""
        return out

    out = df.copy()

    def _fmt(v: Any, n: int) -> str:
        return format_score_reasons(
            v,
            top_n=n,
            include_negative=include_negative,
            sort_by_abs=sort_by_abs,
        )

    try:
        out[top3_col] = out[source_col].apply(lambda v: _fmt(v, 3))
        out[top5_col] = out[source_col].apply(lambda v: _fmt(v, 5))
        out[summary_col] = out[top3_col]
    except Exception:
        logger.exception("[summary.top_candidates.facade] attach_score_reason_columns failed")
        for c in (top3_col, top5_col, summary_col):
            if c not in out.columns:
                out[c] = ""

    return out


def _postprocess_result(name: str, result: Any) -> Any:
    """
    DataFrame 戻り値へ score_reasons 要約列を付与する。
    """
    if name not in _DF_RETURNING_APIS:
        return result

    if isinstance(result, pd.DataFrame):
        return attach_score_reason_columns(result)

    # tuple の先頭が DataFrame の場合にも対応
    if isinstance(result, tuple) and len(result) > 0 and isinstance(result[0], pd.DataFrame):
        try:
            first = attach_score_reason_columns(result[0])
            return (first, *result[1:])
        except Exception:
            logger.exception("[summary.top_candidates.facade] tuple postprocess failed")
            return result

    return result


# ============================================================
# call wrapper
# ============================================================


def _call(name: str, *args: Any, **kwargs: Any) -> Any:
    fn = _resolve_export(name)
    result = fn(*args, **kwargs)
    return _postprocess_result(name, result)


def _callable_wrapper(name: str) -> Callable[..., Any]:
    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        return _call(name, *args, **kwargs)

    _wrapped.__name__ = name
    _wrapped.__qualname__ = name
    _wrapped.__doc__ = (
        f"Compatibility facade for `{name}`. "
        "Implementation is lazily resolved from trading.summary.top_candidates_pkg.*"
    )
    return _wrapped


# ============================================================
# 互換公開 API
# ============================================================
prepare_buy_sell_top_df = _callable_wrapper("prepare_buy_sell_top_df")
prepare_buy_top_df = _callable_wrapper("prepare_buy_top_df")
prepare_sell_top_df = _callable_wrapper("prepare_sell_top_df")

collect_push_summary_candidates = _callable_wrapper("collect_push_summary_candidates")
collect_ranking_summary_candidates = _callable_wrapper("collect_ranking_summary_candidates")
merge_ai_entry_candidates = _callable_wrapper("merge_ai_entry_candidates")
collect_ai_entry_candidates = _callable_wrapper("collect_ai_entry_candidates")
collect_top_candidates_for_ai = _callable_wrapper("collect_top_candidates_for_ai")
log_ai_entry_candidates = _callable_wrapper("log_ai_entry_candidates")


# ============================================================
# module-level getattr
# ============================================================
def __getattr__(name: str) -> Any:
    if name in {
        "prepare_buy_sell_top_df",
        "prepare_buy_top_df",
        "prepare_sell_top_df",
        "collect_push_summary_candidates",
        "collect_ranking_summary_candidates",
        "merge_ai_entry_candidates",
        "collect_ai_entry_candidates",
        "collect_top_candidates_for_ai",
        "log_ai_entry_candidates",
    }:
        return _resolve_export(name)

    if name in {
        "format_score_reasons",
        "get_top_score_reasons",
        "attach_score_reason_columns",
        "preload_top_candidates_exports",
        "get_facade_resolution_cache",
        "get_facade_resolution_errors",
    }:
        return globals()[name]

    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def __dir__() -> List[str]:
    return sorted(set(list(globals().keys()) + list(_EXPORTS)))


# ============================================================
# health / debug helpers
# ============================================================
def get_facade_resolution_cache() -> Dict[str, str]:
    """
    解決済み関数の参照先モジュール名を返す。
    """
    out: Dict[str, str] = {}
    for key, fn in _RESOLVED.items():
        try:
            out[key] = getattr(fn, "__module__", "<unknown>")
        except Exception:
            out[key] = "<unknown>"
    return out


def get_facade_resolution_errors() -> Dict[str, str]:
    """
    解決失敗した export のエラー内容を返す。
    """
    return dict(_RESOLVE_ERRORS)


def preload_top_candidates_exports() -> bool:
    """
    事前解決を行う。
    起動時に互換 API の解決可否を確認したい場合に使う。
    """
    ok = True
    for name in (
        "prepare_buy_sell_top_df",
        "prepare_buy_top_df",
        "prepare_sell_top_df",
        "collect_push_summary_candidates",
        "collect_ranking_summary_candidates",
        "merge_ai_entry_candidates",
        "collect_ai_entry_candidates",
        "collect_top_candidates_for_ai",
        "log_ai_entry_candidates",
    ):
        try:
            _resolve_export(name)
        except Exception:
            ok = False
    return ok