# ============================================================
# File   : core/startup/summary_bootstrap.py
# Version: PRODUCTION-STABLE-SUMMARY-BOOTSTRAP-ENTRYPOINT-V4.1
#          -PREVIOUS-SUMMARY-PRESERVE
#          -IMMATURE-JUDGEMENT-RELAXED
# ------------------------------------------------------------
# 【概要】
#   startup.py が期待する bootstrap_summary を公開する入口
#
# 【主な機能】
#   - core.startup.__init__ が期待する
#     run_bootstrap_incremental_rebuild_if_available を公開
#   - self import / circular import を排除
#   - lazy import で依存解決
#   - 複数候補実装を安全に探索
#   - merged summary cache へ反映を試行
#   - 場中/休場どちらでも落ちにくい
#   - 戻り値は dict / DataFrame / None を吸収
#   - 例外時も startup 全体を壊しにくい
#   - 起動前の summary cache を退避
#   - bootstrap結果が未成熟/空なら前回 summary を保持
#   - bootstrap結果が十分育ったときのみ source="push" で上書き
#   - source 対応前の旧 global_state にも後方互換
#
# 【今回の修正】
#   - immature 判定を緩和
#   - score_nonzero / score_buy_nonzero / score_sell_nonzero がある場合、
#     technical 不足だけでは immature 扱いしない
#   - no_technical_like 単独では reject しない
#   - 起動直後の score あり / rsi・macd 未成熟サマリーを保持しやすくする
#
# 【背景】
#   起動直後は PUSH履歴が少なく、
#     - rsi
#     - macd
#     - slope
#     - mtf
#   がまだ 0 / NaN になりやすい。
#
#   しかし score が入っている場合は、表示・entry候補として
#   最低限使えるため、technical不足だけで previous summary へ戻すと
#   最新サマリーが global_data に反映されにくくなる。
# ============================================================

from __future__ import annotations

import importlib
import inspect
import logging
from functools import lru_cache
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)

try:
    from global_state import global_data  # type: ignore
except Exception:
    try:
        from core.global_context import global_data  # type: ignore
    except Exception:
        class _FallbackGlobalData:
            pass

        global_data = _FallbackGlobalData()


_IMPL_CANDIDATES: list[tuple[str, str]] = [
    ("trading.summary.engine.summary_recovery_engine", "bootstrap_incremental_rebuild_from_push"),
    ("trading.summary.engine.summary_recovery_engine", "bootstrap_summary"),
    ("trading.summary.engine.summary_recovery_engine", "run_bootstrap_summary"),
    ("core.startup.summary_bootstrap_impl", "bootstrap_summary"),
    ("core.startup.summary_bootstrap_impl", "run"),
]


# ============================================================
# generic helpers
# ============================================================

def _safe_signature_text(fn: Callable) -> str:
    try:
        return str(inspect.signature(fn))
    except Exception:
        return "(signature unavailable)"


def _ensure_df(obj: Any) -> pd.DataFrame:
    if obj is None:
        return pd.DataFrame()

    if isinstance(obj, pd.DataFrame):
        out = obj.copy()
    elif isinstance(obj, pd.Series):
        out = pd.DataFrame([obj.to_dict()])
    elif isinstance(obj, dict):
        try:
            out = pd.DataFrame([obj])
        except Exception:
            return pd.DataFrame()
    else:
        try:
            out = pd.DataFrame(obj).copy()
        except Exception:
            return pd.DataFrame()

    try:
        out = out.reset_index(drop=True)
    except Exception:
        pass

    return out


def _symbols(df: Any) -> int:
    try:
        if isinstance(df, pd.DataFrame) and not df.empty and "symbol" in df.columns:
            return int(
                df["symbol"]
                .fillna("")
                .astype(str)
                .str.strip()
                .replace("", pd.NA)
                .dropna()
                .nunique()
            )
    except Exception:
        pass
    return 0


def _latest_dt(df: Any) -> str | None:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return None

        for c in ("datetime", "end_time", "time", "start_time"):
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce").dropna()
                if not s.empty:
                    return str(s.max())
    except Exception:
        pass
    return None


def _ensure_symbol_datetime(df: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()

    if "symbol" not in out.columns:
        for c in ("Symbol", "code", "Code", "symbol_code"):
            if c in out.columns:
                out["symbol"] = out[c]
                break

    if "symbol" not in out.columns:
        out["symbol"] = ""

    out["symbol"] = (
        out["symbol"]
        .astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
    )

    if "datetime" not in out.columns:
        if "date" in out.columns and "start_time" in out.columns:
            out["datetime"] = pd.to_datetime(
                out["date"].astype(str).str.strip()
                + " "
                + out["start_time"].astype(str).str.strip(),
                errors="coerce",
            )
        elif "date" in out.columns and "time" in out.columns:
            out["datetime"] = pd.to_datetime(
                out["date"].astype(str).str.strip()
                + " "
                + out["time"].astype(str).str.strip(),
                errors="coerce",
            )
        elif "end_time" in out.columns:
            out["datetime"] = pd.to_datetime(out["end_time"], errors="coerce")
        else:
            out["datetime"] = pd.NaT
    else:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")

    try:
        out["datetime"] = out["datetime"].dt.tz_localize(None)
    except Exception:
        pass

    out = out[out["symbol"] != ""].copy()
    return out.reset_index(drop=True)


# ============================================================
# maturity / score profile
# ============================================================

def _numeric_nonzero(df: pd.DataFrame, col: str) -> int:
    try:
        if col not in df.columns:
            return 0
        s = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
        return int((s != 0).sum())
    except Exception:
        return 0


def _numeric_nonnull(df: pd.DataFrame, col: str) -> int:
    try:
        if col not in df.columns:
            return 0
        s = pd.to_numeric(df[col], errors="coerce")
        return int(s.notna().sum())
    except Exception:
        return 0


def _score_profile(df: pd.DataFrame) -> dict[str, Any]:
    prof = {
        "rows": 0,
        "symbols": 0,
        "latest_dt": None,
        "score_nonzero": 0,
        "score_buy_nonzero": 0,
        "score_sell_nonzero": 0,
        "final_score_nonzero": 0,
        "display_score_nonzero": 0,
        "rsi_nonnull": 0,
        "macd_nonnull": 0,
        "slope_nonzero": 0,
        "score_slope_nonzero": 0,
        "mtf_nonzero": 0,
        "score_mtf_nonzero": 0,
        "hist_max": 0.0,
        "latest_only_ratio": 0.0,
        "one_bar_rows": 0,
        "technical_ready_rows": 0,
    }

    if not isinstance(df, pd.DataFrame) or df.empty:
        return prof

    out = _ensure_symbol_datetime(df)
    prof["rows"] = len(out)
    prof["symbols"] = _symbols(out)
    prof["latest_dt"] = _latest_dt(out)

    prof["score_nonzero"] = _numeric_nonzero(out, "score")
    prof["score_buy_nonzero"] = _numeric_nonzero(out, "score_buy")
    prof["score_sell_nonzero"] = _numeric_nonzero(out, "score_sell")
    prof["final_score_nonzero"] = _numeric_nonzero(out, "final_score")
    prof["display_score_nonzero"] = _numeric_nonzero(out, "display_score")

    prof["rsi_nonnull"] = _numeric_nonnull(out, "rsi")
    prof["macd_nonnull"] = _numeric_nonnull(out, "macd")

    prof["slope_nonzero"] = _numeric_nonzero(out, "slope")
    prof["score_slope_nonzero"] = _numeric_nonzero(out, "score_slope")
    prof["mtf_nonzero"] = _numeric_nonzero(out, "mtf")
    prof["score_mtf_nonzero"] = _numeric_nonzero(out, "score_mtf")

    if "symbol_hist_len" in out.columns:
        try:
            hist = pd.to_numeric(out["symbol_hist_len"], errors="coerce")
            prof["hist_max"] = float(hist.max()) if hist.notna().any() else 0.0
        except Exception:
            pass

    if "technical_ready" in out.columns:
        try:
            prof["technical_ready_rows"] = int(
                pd.Series(out["technical_ready"]).fillna(False).astype(bool).sum()
            )
        except Exception:
            pass

    if "datetime" in out.columns and out["datetime"].notna().any():
        try:
            latest = out["datetime"].max()
            one_bar = int((out["datetime"] == latest).sum())
            prof["one_bar_rows"] = one_bar
            prof["latest_only_ratio"] = float(one_bar / len(out)) if len(out) > 0 else 0.0
        except Exception:
            pass

    return prof


def _looks_immature(df: pd.DataFrame) -> tuple[bool, dict[str, Any]]:
    """
    bootstrap 結果が global_data に反映してよい成熟度かを判定する。

    Ver4.1 の緩和:
      - score 系が1つでも非ゼロなら、technical不足だけでは immature にしない。
      - rsi/macd/slope/mtf が未成熟でも、score が存在すれば表示・候補生成には使える。
      - zero_score_like は引き続き immature とする。
      - latest_only_like は引き続き immature とする。
    """
    prof = _score_profile(df)

    rows = int(prof.get("rows", 0) or 0)
    symbols = int(prof.get("symbols", 0) or 0)
    latest_only_ratio = float(prof.get("latest_only_ratio", 0.0) or 0.0)
    hist_max = float(prof.get("hist_max", 0.0) or 0.0)
    tech_ready = int(prof.get("technical_ready_rows", 0) or 0)

    score_nonzero = int(prof.get("score_nonzero", 0) or 0)
    score_buy_nonzero = int(prof.get("score_buy_nonzero", 0) or 0)
    score_sell_nonzero = int(prof.get("score_sell_nonzero", 0) or 0)
    final_score_nonzero = int(prof.get("final_score_nonzero", 0) or 0)
    display_score_nonzero = int(prof.get("display_score_nonzero", 0) or 0)

    rsi_nonnull = int(prof.get("rsi_nonnull", 0) or 0)
    macd_nonnull = int(prof.get("macd_nonnull", 0) or 0)
    slope_nonzero = int(prof.get("slope_nonzero", 0) or 0)
    score_slope_nonzero = int(prof.get("score_slope_nonzero", 0) or 0)
    mtf_nonzero = int(prof.get("mtf_nonzero", 0) or 0)
    score_mtf_nonzero = int(prof.get("score_mtf_nonzero", 0) or 0)

    score_any_nonzero = (
        score_nonzero
        + score_buy_nonzero
        + score_sell_nonzero
        + final_score_nonzero
        + display_score_nonzero
    ) > 0

    technical_any_ready = (
        tech_ready > 0
        or hist_max > 2.0
        or rsi_nonnull > 0
        or macd_nonnull > 0
        or slope_nonzero > 0
        or score_slope_nonzero > 0
        or mtf_nonzero > 0
        or score_mtf_nonzero > 0
    )

    latest_only_like = latest_only_ratio >= 0.98 and rows <= max(3, symbols + 2)

    zero_score_like = (
        rows > 0
        and not score_any_nonzero
    )

    no_technical_like = not technical_any_ready

    # Ver4.1:
    # technical が無くても score があるなら immature にしない。
    immature = latest_only_like or zero_score_like

    logger.info(
        "[summary_bootstrap] maturity rows=%s symbols=%s latest_only_ratio=%.4f hist_max=%s score_any_nonzero=%s technical_any_ready=%s latest_only_like=%s zero_score_like=%s no_technical_like=%s immature=%s",
        rows,
        symbols,
        latest_only_ratio,
        hist_max,
        score_any_nonzero,
        technical_any_ready,
        latest_only_like,
        zero_score_like,
        no_technical_like,
        immature,
    )

    return immature, prof


# ============================================================
# merged summary set
# ============================================================

def _set_merged_summary_safe(tf: int, df: pd.DataFrame, *, source: str = "push") -> None:
    try:
        if df is None or df.empty:
            return

        if hasattr(global_data, "set_merged_summary"):
            try:
                global_data.set_merged_summary(int(tf), df, source=source)
            except TypeError:
                global_data.set_merged_summary(int(tf), df)

            logger.info(
                "[summary_bootstrap] merged summary set tf=%s source=%s rows=%s symbols=%s latest_dt=%s",
                tf,
                source,
                len(df),
                _symbols(df),
                _latest_dt(df),
            )
            return

    except Exception:
        logger.exception("[summary_bootstrap] set_merged_summary failed tf=%s source=%s", tf, source)

    try:
        if hasattr(global_data, "set_push_merged_summary"):
            global_data.set_push_merged_summary(int(tf), df)
            logger.info(
                "[summary_bootstrap] push merged summary fallback set tf=%s rows=%s symbols=%s latest_dt=%s",
                tf,
                len(df),
                _symbols(df),
                _latest_dt(df),
            )
            return
    except Exception:
        logger.exception("[summary_bootstrap] set_push_merged_summary fallback failed tf=%s", tf)

    try:
        setattr(global_data, f"merged_summary_{int(tf)}min", df.copy())
        setattr(global_data, f"merged_summary_{int(tf)}", df.copy())
    except Exception:
        logger.exception("[summary_bootstrap] setattr merged_summary failed tf=%s", tf)


def _read_global_df(*names: str) -> pd.DataFrame:
    for name in names:
        try:
            if hasattr(global_data, name):
                v = getattr(global_data, name)
                df = _ensure_df(v)
                if not df.empty:
                    return df
        except Exception:
            pass
    return pd.DataFrame()


def _capture_previous_summary_cache() -> dict[int, pd.DataFrame]:
    captured: dict[int, pd.DataFrame] = {}

    key_map = {
        1: (
            "merged_summary_1min",
            "merged_summary_1",
            "summary_1min_df",
            "push_summary_1min",
            "summary_df_1min",
            "merged_summary",
        ),
        3: (
            "merged_summary_3min",
            "merged_summary_3",
            "summary_3min_df",
            "push_summary_3min",
            "summary_df_3min",
        ),
        5: (
            "merged_summary_5min",
            "merged_summary_5",
            "summary_5min_df",
            "push_summary_5min",
            "summary_df_5min",
        ),
    }

    for tf, names in key_map.items():
        df = _read_global_df(*names)
        df = _ensure_symbol_datetime(df)
        if not df.empty:
            captured[tf] = df
            logger.info(
                "[summary_bootstrap] captured previous summary tf=%s rows=%s symbols=%s latest_dt=%s",
                tf,
                len(df),
                _symbols(df),
                _latest_dt(df),
            )

    return captured


# ============================================================
# result apply
# ============================================================

def _apply_one_tf_result(
    *,
    tf: int,
    df: pd.DataFrame,
    prev_df: pd.DataFrame,
) -> None:
    df = _ensure_symbol_datetime(_ensure_df(df))
    prev_df = _ensure_symbol_datetime(_ensure_df(prev_df))

    if df.empty:
        if not prev_df.empty:
            logger.warning(
                "[summary_bootstrap] bootstrap result empty tf=%s -> preserve previous rows=%s symbols=%s latest_dt=%s",
                tf,
                len(prev_df),
                _symbols(prev_df),
                _latest_dt(prev_df),
            )
            _set_merged_summary_safe(tf, prev_df, source="previous_summary")
        else:
            logger.warning("[summary_bootstrap] bootstrap result empty tf=%s and no previous cache", tf)
        return

    immature, prof = _looks_immature(df)

    if immature and not prev_df.empty:
        logger.warning(
            "[summary_bootstrap] bootstrap result immature tf=%s -> preserve previous rows=%s new_rows=%s latest_only_ratio=%.4f hist_max=%s score_nonzero=%s score_buy_nonzero=%s score_sell_nonzero=%s final_score_nonzero=%s display_score_nonzero=%s",
            tf,
            len(prev_df),
            prof.get("rows", 0),
            prof.get("latest_only_ratio", 0.0),
            prof.get("hist_max", 0.0),
            prof.get("score_nonzero", 0),
            prof.get("score_buy_nonzero", 0),
            prof.get("score_sell_nonzero", 0),
            prof.get("final_score_nonzero", 0),
            prof.get("display_score_nonzero", 0),
        )
        _set_merged_summary_safe(tf, prev_df, source="previous_summary")
        return

    logger.info(
        "[summary_bootstrap] bootstrap result accepted tf=%s rows=%s symbols=%s latest_dt=%s score_nonzero=%s score_buy_nonzero=%s score_sell_nonzero=%s final_score_nonzero=%s display_score_nonzero=%s",
        tf,
        len(df),
        _symbols(df),
        _latest_dt(df),
        prof.get("score_nonzero", 0),
        prof.get("score_buy_nonzero", 0),
        prof.get("score_sell_nonzero", 0),
        prof.get("final_score_nonzero", 0),
        prof.get("display_score_nonzero", 0),
    )
    _set_merged_summary_safe(tf, df, source="push")


def _apply_result_to_cache(result: Any, previous_cache: dict[int, pd.DataFrame] | None = None) -> None:
    previous_cache = previous_cache or {}

    if result is None:
        logger.warning("[summary_bootstrap] result is None")
        for tf, prev_df in previous_cache.items():
            _set_merged_summary_safe(tf, prev_df, source="previous_summary")
        return

    if isinstance(result, pd.DataFrame):
        _apply_one_tf_result(
            tf=1,
            df=_ensure_df(result),
            prev_df=previous_cache.get(1, pd.DataFrame()),
        )
        return

    if not isinstance(result, dict):
        logger.info("[summary_bootstrap] result type=%s (cache apply skipped)", type(result).__name__)
        for tf, prev_df in previous_cache.items():
            _set_merged_summary_safe(tf, prev_df, source="previous_summary")
        return

    key_map = {
        1: (
            "summary_1min",
            "1m",
            "tf1",
            "df1",
            "result_1m",
            "output_1m",
            "merged_1m",
            "latest_1m",
        ),
        3: (
            "summary_3min",
            "3m",
            "tf3",
            "df3",
            "result_3m",
            "output_3m",
            "merged_3m",
            "latest_3m",
        ),
        5: (
            "summary_5min",
            "5m",
            "tf5",
            "df5",
            "result_5m",
            "output_5m",
            "merged_5m",
            "latest_5m",
        ),
    }

    for tf, keys in key_map.items():
        chosen = None
        chosen_key = None

        for k in keys:
            if k in result:
                chosen = result.get(k)
                chosen_key = k
                break

        logger.info("[summary_bootstrap] result choose tf=%s key=%s", tf, chosen_key)

        _apply_one_tf_result(
            tf=tf,
            df=_ensure_df(chosen),
            prev_df=previous_cache.get(tf, pd.DataFrame()),
        )

    try:
        logger.info("[summary_bootstrap] result keys=%s", sorted(list(result.keys())))
    except Exception:
        pass


# ============================================================
# implementation resolver
# ============================================================

@lru_cache(maxsize=1)
def _resolve_impl() -> Callable:
    errors: list[str] = []

    for module_name, attr_name in _IMPL_CANDIDATES:
        try:
            mod = importlib.import_module(module_name)
        except Exception as e:
            msg = f"{module_name}.{attr_name}: import failed: {type(e).__name__}: {e}"
            errors.append(msg)
            logger.debug("[summary_bootstrap] candidate import failed: %s", msg, exc_info=True)
            continue

        try:
            fn = getattr(mod, attr_name, None)
        except Exception as e:
            msg = f"{module_name}.{attr_name}: getattr failed: {type(e).__name__}: {e}"
            errors.append(msg)
            logger.debug("[summary_bootstrap] candidate getattr failed: %s", msg, exc_info=True)
            continue

        if not callable(fn):
            msg = f"{module_name}.{attr_name}: attribute missing or not callable"
            errors.append(msg)
            logger.debug("[summary_bootstrap] candidate skipped: %s", msg)
            continue

        logger.info(
            "[summary_bootstrap] resolved implementation -> %s.%s %s",
            module_name,
            attr_name,
            _safe_signature_text(fn),
        )
        return fn

    raise RuntimeError(
        "summary bootstrap implementation unresolved: "
        + " | ".join(errors if errors else ["no candidates"])
    )


def clear_cached_impl() -> None:
    try:
        _resolve_impl.cache_clear()
        logger.info("[summary_bootstrap] implementation cache cleared")
    except Exception:
        logger.exception("[summary_bootstrap] failed to clear implementation cache")


def _call_with_best_effort(fn: Callable, *args: Any, **kwargs: Any) -> Any:
    attempts: list[tuple[str, Callable[[], Any]]] = [
        ("fn(*args, **kwargs)", lambda: fn(*args, **kwargs)),
        ("fn()", lambda: fn()),
    ]

    errors: list[str] = []

    for label, caller in attempts:
        try:
            return caller()
        except TypeError as e:
            errors.append(f"{label}: TypeError: {e}")
            continue
        except Exception:
            raise

    raise TypeError(
        "summary bootstrap callable invocation failed; signature="
        f"{_safe_signature_text(fn)}; attempts={' | '.join(errors)}"
    )


# ============================================================
# public API
# ============================================================

def bootstrap_summary(*args: Any, **kwargs: Any) -> Any:
    logger.info("[summary_bootstrap] bootstrap_summary start")

    previous_cache = _capture_previous_summary_cache()

    try:
        fn = _resolve_impl()
    except Exception:
        logger.exception("[summary_bootstrap] resolve implementation failed")
        for tf, prev_df in previous_cache.items():
            _set_merged_summary_safe(tf, prev_df, source="previous_summary")
        return {}

    try:
        result = _call_with_best_effort(fn, *args, **kwargs)
    except Exception:
        logger.exception("[summary_bootstrap] implementation call failed")
        for tf, prev_df in previous_cache.items():
            _set_merged_summary_safe(tf, prev_df, source="previous_summary")
        return {}

    try:
        _apply_result_to_cache(result, previous_cache=previous_cache)
    except Exception:
        logger.exception("[summary_bootstrap] cache apply failed")
        for tf, prev_df in previous_cache.items():
            _set_merged_summary_safe(tf, prev_df, source="previous_summary")

    try:
        if isinstance(result, dict):
            logger.info("[summary_bootstrap] done result_keys=%s", sorted(list(result.keys())))
        elif isinstance(result, pd.DataFrame):
            logger.info(
                "[summary_bootstrap] done dataframe rows=%s symbols=%s latest_dt=%s",
                len(result),
                _symbols(result),
                _latest_dt(result),
            )
        else:
            logger.info("[summary_bootstrap] done result_type=%s", type(result).__name__)
    except Exception:
        pass

    return result


def run_bootstrap_incremental_rebuild_if_available(*args: Any, **kwargs: Any) -> Any:
    logger.info("[summary_bootstrap] run_bootstrap_incremental_rebuild_if_available start")
    return bootstrap_summary(*args, **kwargs)


def run() -> Any:
    return bootstrap_summary()


__all__ = [
    "bootstrap_summary",
    "run_bootstrap_incremental_rebuild_if_available",
    "run",
    "clear_cached_impl",
]