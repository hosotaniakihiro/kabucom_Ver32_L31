from __future__ import annotations

import logging
import os
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIGINAL_RESOLVE = None
_ORIGINAL_STORE = None
_ORIGINAL_PUBLIC: dict[str, Callable] = {}

# 履歴から復元したい列。PUSH再接続直後やfallback summaryでは、最新行だけが残り
# macd/signal/slope が 0 に戻ることがあるため、同一銘柄の過去の非ゼロ値で補完する。
_TECH_FILL_COLS = (
    "ma5", "ma25", "ma75", "rsi", "macd", "signal", "hist", "atr",
    "slope", "slope_atr_scaled", "score_slope", "mtf", "score_mtf", "mtf_score",
    "technical_ready", "symbol_hist_len",
)

_NONZERO_PREFERRED_COLS = {
    "macd", "signal", "hist", "rsi", "slope", "slope_atr_scaled", "score_slope",
    "mtf", "score_mtf", "mtf_score", "technical_ready", "symbol_hist_len",
}


def _env_bool(name: str, default: bool = True) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _safe_df(value: Any) -> pd.DataFrame:
    if isinstance(value, pd.DataFrame):
        return value.copy()
    try:
        return pd.DataFrame(value).copy()
    except Exception:
        return pd.DataFrame()


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    out = _safe_df(df)
    if out.empty or "symbol" not in out.columns:
        return pd.DataFrame()
    out = out.loc[:, ~out.columns.duplicated()].copy()
    out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    out = out[out["symbol"] != ""].copy()
    if "datetime" not in out.columns:
        for c in ("end_time", "start_time", "time"):
            if c in out.columns:
                out["datetime"] = out[c]
                break
    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        try:
            out["datetime"] = out["datetime"].dt.tz_localize(None)
        except Exception:
            pass
    return out.reset_index(drop=True)


def _latest_dt(df: pd.DataFrame):
    try:
        if df.empty or "datetime" not in df.columns:
            return None
        s = pd.to_datetime(df["datetime"], errors="coerce")
        return s.max() if s.notna().any() else None
    except Exception:
        return None


def _nonzero(df: pd.DataFrame, col: str) -> int:
    try:
        if df.empty or col not in df.columns:
            return -1
        return int((pd.to_numeric(df[col], errors="coerce").fillna(0) != 0).sum())
    except Exception:
        return -1


def _is_blank(v: Any) -> bool:
    try:
        return bool(pd.isna(v))
    except Exception:
        return str(v).strip() in {"", "None", "nan", "NaN"}


def _is_zero_like(v: Any) -> bool:
    if _is_blank(v):
        return True
    try:
        return float(v) == 0.0
    except Exception:
        return str(v).strip() in {"", "0", "0.0", "False", "false", "None"}


def _is_useful_value(v: Any, *, prefer_nonzero: bool) -> bool:
    if _is_blank(v):
        return False
    if prefer_nonzero:
        return not _is_zero_like(v)
    return True


def _gc():
    try:
        from core.global_context.context import global_context as GC
        return GC
    except Exception:
        return None


def _get_history(interval: int) -> pd.DataFrame:
    GC = _gc()
    if GC is None:
        return pd.DataFrame()
    try:
        hist = GC.get_summary_history(interval, source="push")
    except TypeError:
        try:
            hist = GC.get_summary_history(interval)
        except Exception:
            hist = pd.DataFrame()
    except Exception:
        logger.exception("[PUSH HISTORY PATCH] get history failed interval=%s", interval)
        hist = pd.DataFrame()
    hist = _normalize(hist)
    if not hist.empty:
        logger.warning(
            "[PUSH HISTORY PATCH] history interval=%s rows=%s symbols=%s latest_dt=%s macd=%s signal=%s mtf=%s",
            interval, len(hist), hist["symbol"].nunique(), _latest_dt(hist),
            _nonzero(hist, "macd"), _nonzero(hist, "signal"), _nonzero(hist, "mtf"),
        )
    return hist


def _useful(hist: pd.DataFrame) -> bool:
    if hist.empty or "symbol" not in hist.columns:
        return False
    try:
        rows = len(hist)
        syms = int(hist["symbol"].nunique())
        if rows > max(10, syms * 2):
            return True
        if _nonzero(hist, "macd") > 0 or _nonzero(hist, "signal") > 0:
            return True
        if "symbol_hist_len" in hist.columns:
            return pd.to_numeric(hist["symbol_hist_len"], errors="coerce").max() >= 3
    except Exception:
        return False
    return False


def _latest_by_symbol(df: pd.DataFrame) -> pd.DataFrame:
    out = _normalize(df)
    if out.empty:
        return out
    if "datetime" in out.columns:
        out = out.sort_values(["symbol", "datetime"], kind="stable")
    return out.drop_duplicates(subset=["symbol"], keep="last").reset_index(drop=True)


def _build_best_history_by_symbol(hist: pd.DataFrame) -> dict[str, dict[str, Any]]:
    """
    最新行が 0 の場合でも、同一銘柄の過去履歴から非ゼロのテクニカル値を拾う。
    WebSocket reconnect 後の fallback DF で macd/signal が 0 へ退行する対策。
    """
    df = _normalize(hist)
    if df.empty or "symbol" not in df.columns:
        return {}
    if "datetime" in df.columns:
        df = df.sort_values(["symbol", "datetime"], kind="stable")

    result: dict[str, dict[str, Any]] = {}
    for sym, g in df.groupby("symbol", sort=False):
        sym_s = str(sym).strip()
        if not sym_s:
            continue
        best: dict[str, Any] = {}
        latest_row = g.iloc[-1]

        # 非テクニカルの基礎値は最新行を採用
        for col in ("symbol", "symbolname", "datetime", "close", "close_price", "price", "volume"):
            if col in g.columns:
                best[col] = latest_row.get(col)

        for col in _TECH_FILL_COLS:
            if col not in g.columns:
                continue
            prefer_nonzero = col in _NONZERO_PREFERRED_COLS
            val = None
            # 最新から過去へ見て、非ゼロ優先列は非ゼロ値、その他は非NULL値を採用
            for v in reversed(list(g[col].values)):
                if _is_useful_value(v, prefer_nonzero=prefer_nonzero):
                    val = v
                    break
            # 非ゼロ値が無い場合でも、最新がNULLでなければ最後の値を保持
            if val is None:
                lv = latest_row.get(col)
                if not _is_blank(lv):
                    val = lv
            if val is not None:
                best[col] = val

        result[sym_s] = best
    return result


def _tf_signal(row: pd.Series) -> float:
    score = 0.0

    def _num(col: str):
        try:
            return float(row.get(col))
        except Exception:
            return None

    slope = _num("slope")
    if slope is None or slope == 0.0:
        slope = _num("slope_atr_scaled")
    if slope is not None:
        if slope > 0:
            score += 1.0
        elif slope < 0:
            score -= 1.0

    macd = _num("macd")
    signal = _num("signal")
    if macd is not None and signal is not None:
        diff = macd - signal
        if diff > 0:
            score += 1.0
        elif diff < 0:
            score -= 1.0
    elif macd is not None:
        if macd > 0:
            score += 0.5
        elif macd < 0:
            score -= 0.5

    close = _num("close")
    if close is None:
        close = _num("close_price")
    ma5 = _num("ma5")
    ma25 = _num("ma25")
    if close is not None and ma5 is not None:
        if close > ma5:
            score += 0.5
        elif close < ma5:
            score -= 0.5
    if ma5 is not None and ma25 is not None:
        if ma5 > ma25:
            score += 0.5
        elif ma5 < ma25:
            score -= 0.5

    return float(max(-3.0, min(3.0, score)))


def _build_mtf_map() -> dict[str, dict[str, float]]:
    histories = {tf: _latest_by_symbol(_get_history(tf)) for tf in (1, 3, 5)}
    latest_by_tf: dict[int, dict[str, pd.Series]] = {}
    for tf, df in histories.items():
        m: dict[str, pd.Series] = {}
        if isinstance(df, pd.DataFrame) and not df.empty and "symbol" in df.columns:
            for _, r in df.iterrows():
                m[str(r.get("symbol", "")).strip()] = r
        latest_by_tf[tf] = m

    symbols = set()
    for m in latest_by_tf.values():
        symbols.update(m.keys())

    weights = {1: 1.0, 3: 1.25, 5: 1.5}
    result: dict[str, dict[str, float]] = {}
    for sym in symbols:
        total = 0.0
        wsum = 0.0
        tf_count = 0
        raw_parts = {}
        for tf in (1, 3, 5):
            row = latest_by_tf.get(tf, {}).get(sym)
            if row is None:
                continue
            val = _tf_signal(row)
            raw_parts[f"tf{tf}"] = val
            if val != 0:
                tf_count += 1
            w = weights[tf]
            total += val * w
            wsum += w
        if wsum <= 0:
            continue
        mtf = float(total / wsum)
        if abs(mtf) < 1e-12:
            continue
        result[sym] = {
            "mtf": mtf,
            "score_mtf": mtf,
            "mtf_score": mtf,
            "mtf_tf_count": float(tf_count),
            **raw_parts,
        }
    return result


def _need_fill_mtf_value(v: Any) -> bool:
    return _is_zero_like(v)


def _apply_mtf_rebuild(df: pd.DataFrame, interval: int, *, context: str) -> pd.DataFrame:
    out = _normalize(df)
    if out.empty or "symbol" not in out.columns:
        return out
    if not _env_bool("PUSH_HISTORY_PATCH_REBUILD_MTF", True):
        return out

    mtf_map = _build_mtf_map()
    if not mtf_map:
        logger.warning("[PUSH HISTORY PATCH][MTF] no mtf map context=%s interval=%s", context, interval)
        return out

    fill_count = 0
    hit_count = 0
    score_scale = _env_float("PUSH_HISTORY_PATCH_SCORE_MTF_SCALE", 1.0)
    for idx, row in out.iterrows():
        sym = str(row.get("symbol", "")).strip()
        vals = mtf_map.get(sym)
        if not vals:
            continue
        hit_count += 1
        mtf_val = float(vals.get("mtf", 0.0))
        score_val = float(vals.get("score_mtf", mtf_val)) * score_scale
        for col, val in (("mtf", mtf_val), ("score_mtf", score_val), ("mtf_score", score_val)):
            if col not in out.columns:
                out[col] = pd.NA
            if _need_fill_mtf_value(out.at[idx, col]):
                out.at[idx, col] = val
                fill_count += 1
        if "mtf_tf_count" not in out.columns:
            out["mtf_tf_count"] = pd.NA
        if _need_fill_mtf_value(out.at[idx, "mtf_tf_count"]):
            out.at[idx, "mtf_tf_count"] = vals.get("mtf_tf_count", 0.0)

    logger.warning(
        "[PUSH HISTORY PATCH][MTF] context=%s interval=%s rows=%s hits=%s fill_count=%s mtf=%s score_mtf=%s mtf_score=%s",
        context, interval, len(out), hit_count, fill_count,
        _nonzero(out, "mtf"), _nonzero(out, "score_mtf"), _nonzero(out, "mtf_score"),
    )
    return out


def _patched_resolve(interval: int) -> pd.DataFrame:
    interval = int(interval)
    hist = _get_history(interval)
    if _useful(hist):
        hist = _apply_mtf_rebuild(hist, interval, context="seed")
        logger.warning("[PUSH HISTORY PATCH] use history as pipeline seed interval=%s rows=%s", interval, len(hist))
        return hist
    if callable(_ORIGINAL_RESOLVE):
        return _ORIGINAL_RESOLVE(interval)
    return pd.DataFrame()


def _fill_from_history(df: pd.DataFrame, hist: pd.DataFrame, interval: int, *, context: str = "store") -> pd.DataFrame:
    out = _normalize(df)
    if out.empty:
        return out

    best_map = _build_best_history_by_symbol(hist)
    fill_count = 0
    hit_count = 0
    macd_fill = 0
    signal_fill = 0
    slope_fill = 0

    for idx, row in out.iterrows():
        sym = str(row.get("symbol", "")).strip()
        if not sym or sym not in best_map:
            continue
        hit_count += 1
        best = best_map[sym]
        for col in _TECH_FILL_COLS:
            if col not in best or _is_blank(best.get(col)):
                continue
            if col not in out.columns:
                out[col] = pd.NA
            cur = out.at[idx, col]
            prefer_nonzero = col in _NONZERO_PREFERRED_COLS
            need = _is_blank(cur) or (prefer_nonzero and _is_zero_like(cur) and not _is_zero_like(best.get(col)))
            if need:
                out.at[idx, col] = best.get(col)
                fill_count += 1
                if col == "macd":
                    macd_fill += 1
                elif col == "signal":
                    signal_fill += 1
                elif col in {"slope", "slope_atr_scaled", "score_slope"}:
                    slope_fill += 1

    # macd/signal/slope が戻った行は technical_ready も立て直す。
    try:
        if "technical_ready" not in out.columns:
            out["technical_ready"] = False
        macd_nz = pd.to_numeric(out.get("macd"), errors="coerce").fillna(0) != 0 if "macd" in out.columns else False
        sig_nz = pd.to_numeric(out.get("signal"), errors="coerce").fillna(0) != 0 if "signal" in out.columns else False
        rsi_ok = pd.to_numeric(out.get("rsi"), errors="coerce").fillna(0) != 0 if "rsi" in out.columns else False
        ready = macd_nz | sig_nz | rsi_ok
        out.loc[ready, "technical_ready"] = True
        if "symbol_hist_len" not in out.columns:
            out["symbol_hist_len"] = pd.NA
        out.loc[ready & out["symbol_hist_len"].isna(), "symbol_hist_len"] = 3
    except Exception:
        logger.exception("[PUSH HISTORY PATCH] technical_ready rebuild failed interval=%s context=%s", interval, context)

    out = _apply_mtf_rebuild(out, interval, context=context)
    logger.warning(
        "[PUSH HISTORY PATCH] filled context=%s interval=%s rows=%s hits=%s fill_count=%s macd_fill=%s signal_fill=%s slope_fill=%s macd=%s signal=%s mtf=%s score_mtf=%s ready=%s",
        context, interval, len(out), hit_count, fill_count, macd_fill, signal_fill, slope_fill,
        _nonzero(out, "macd"), _nonzero(out, "signal"), _nonzero(out, "mtf"), _nonzero(out, "score_mtf"), _nonzero(out, "technical_ready"),
    )
    return out


def _merge_history(hist: pd.DataFrame, latest: pd.DataFrame, interval: int) -> pd.DataFrame:
    hist = _normalize(hist)
    latest = _normalize(latest)
    if hist.empty:
        return latest
    if latest.empty:
        return hist

    # latest は既に _fill_from_history 済みだが、同一 symbol/datetime の重複で
    # 非ゼロテクニカルが 0 に戻らないように latest を後勝ちにしつつ補完済み値を保持する。
    merged = pd.concat([hist, latest], ignore_index=True, sort=False)
    if "source" not in merged.columns:
        merged["source"] = "push"
    if "interval" not in merged.columns:
        merged["interval"] = int(interval)
    subset = ["symbol"]
    if "datetime" in merged.columns:
        subset.append("datetime")
    return merged.sort_values(subset, kind="stable").drop_duplicates(subset=subset, keep="last").reset_index(drop=True)


def _set_history(interval: int, df: pd.DataFrame) -> None:
    GC = _gc()
    if GC is None or df.empty:
        return
    try:
        GC.set_summary_history(interval, df.copy(), source="push_history_patch")
    except Exception:
        logger.exception("[PUSH HISTORY PATCH] set history failed interval=%s", interval)


def _fix_df(interval: int, df: pd.DataFrame, *, context: str) -> pd.DataFrame:
    interval = int(interval)
    hist = _get_history(interval)
    fixed = _fill_from_history(df, hist, interval, context=context) if _useful(hist) else _apply_mtf_rebuild(_safe_df(df), interval, context=context)
    merged = _merge_history(hist, fixed, interval)
    if not merged.empty:
        _set_history(interval, merged)
    return fixed


def _patched_store(interval: int, df: pd.DataFrame) -> None:
    fixed = _fix_df(int(interval), df, context="store")
    if callable(_ORIGINAL_STORE):
        return _ORIGINAL_STORE(int(interval), fixed)
    return None


def _wrap_public(fn_name: str, fn: Callable) -> Callable:
    def _wrapped(*args, **kwargs):
        interval = kwargs.get("interval", None)
        if interval is None and args:
            try:
                interval = int(args[0])
            except Exception:
                interval = 1
        if interval is None:
            interval = 1
        ret = fn(*args, **kwargs)
        if isinstance(ret, pd.DataFrame):
            return _fix_df(int(interval), ret, context=f"return:{fn_name}")
        return ret
    _wrapped._push_history_patch = True  # type: ignore[attr-defined]
    return _wrapped


def install() -> bool:
    global _PATCHED, _ORIGINAL_RESOLVE, _ORIGINAL_STORE
    if _PATCHED:
        return True
    if not _env_bool("PUSH_SUMMARY_HISTORY_PATCH_ENABLED", True):
        return False
    try:
        import trading.summary.engine.push_summary_engine as pse

        old_resolve = getattr(pse, "_resolve_summary_source_df", None)
        if callable(old_resolve) and not getattr(old_resolve, "_push_history_patch", False):
            _ORIGINAL_RESOLVE = old_resolve
            _patched_resolve._push_history_patch = True  # type: ignore[attr-defined]
            pse._resolve_summary_source_df = _patched_resolve
            logger.warning("[PUSH HISTORY PATCH] patched resolve")

        old_store = getattr(pse, "_store_push_merged_summary", None)
        if callable(old_store) and not getattr(old_store, "_push_history_patch", False):
            _ORIGINAL_STORE = old_store
            _patched_store._push_history_patch = True  # type: ignore[attr-defined]
            pse._store_push_merged_summary = _patched_store
            logger.warning("[PUSH HISTORY PATCH] patched store")

        for name in ("build_summary", "build_push_summary", "push_summary_engine", "run_push_summary_engine", "run_summary_engine", "run"):
            fn = getattr(pse, name, None)
            if callable(fn) and not getattr(fn, "_push_history_patch", False):
                _ORIGINAL_PUBLIC[name] = fn
                setattr(pse, name, _wrap_public(name, fn))
                logger.warning("[PUSH HISTORY PATCH] patched public %s", name)

        _PATCHED = True
        logger.warning("[PUSH HISTORY PATCH] installed V4 return-filled mtf-rebuild macd-signal-preserve")
        return True
    except Exception:
        logger.exception("[PUSH HISTORY PATCH] install failed")
        return False


__all__ = ["install"]
