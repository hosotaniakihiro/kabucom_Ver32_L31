# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_controller_latest_enrich_patch.py
# Version: V4-INTRADAY-MTF-BACKFILL-1M
# ------------------------------------------------------------
# Purpose:
#   summary_controller の df_latest が保存/cache/entryへ流れる直前に
#   controller_enrich.enrich_summary_latest() を通す。
#
# V4:
#   - main.py で 3m/5m をDB保存せずメモリ生成する構成でも、
#     3m/5m の最新スコアから 1m summary の mtf/score_mtf/mtf_score を
#     symbol単位で厳格補完する。
#   - 3m/5m が欠損・古い・方向不一致の場合は補完しない。
#   - 3m/5m publish 後に既存1m cache/historyを再補完して、
#     SUMMARY HISTORY GET tf=1 mtf=0 へ戻る状態を防ぐ。
# ============================================================
from __future__ import annotations

import logging
import os
from functools import wraps
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
VERSION = "V4-INTRADAY-MTF-BACKFILL-1M"
_INSTALLED = False
_ORIG_GLOBAL_SETTERS: dict[str, Any] = {}
_REFRESHING_1M = False


def _nonzero(df: Any, col: str) -> int:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or col not in df.columns:
            return 0
        return int(pd.to_numeric(df[col], errors="coerce").fillna(0).ne(0).sum())
    except Exception:
        return 0


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _num_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    try:
        if col not in df.columns:
            return pd.Series(default, index=df.index, dtype="float64")
        return pd.to_numeric(df[col], errors="coerce").replace([float("inf"), float("-inf")], pd.NA).fillna(default).astype("float64")
    except Exception:
        return pd.Series(default, index=getattr(df, "index", None), dtype="float64")


def _ensure_numeric_col(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        df[col] = 0.0
    return _num_series(df, col, 0.0)


def _first_nonzero(df: pd.DataFrame, cols: tuple[str, ...]) -> pd.Series:
    out = pd.Series(0.0, index=df.index, dtype="float64")
    for c in cols:
        if c not in df.columns:
            continue
        s = _num_series(df, c, 0.0)
        out = out.where(out.ne(0), s)
    return out.fillna(0.0)


def _symbolize(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if out.empty:
        return out
    if "symbol" not in out.columns:
        for c in ("Symbol", "code", "Code", "stock_code", "銘柄コード"):
            if c in out.columns:
                out["symbol"] = out[c]
                break
    if "symbol" in out.columns:
        out["symbol"] = out["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True).str.replace(r"\.T$", "", regex=True)
        out = out[out["symbol"].astype(str).str.len() > 0].copy()
    return out


def _normalize_dt(df: pd.DataFrame) -> pd.DataFrame:
    out = _symbolize(df)
    if out.empty:
        return out
    if "datetime" not in out.columns:
        for c in ("time", "timestamp", "dt", "last_tick_at", "first_tick_at", "DateTime"):
            if c in out.columns:
                out["datetime"] = out[c]
                break
    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out.dropna(subset=["datetime"])
    return out.reset_index(drop=True)


def _latest_by_symbol(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return pd.DataFrame()
        work = _normalize_dt(df)
        if work.empty:
            return pd.DataFrame()
        return work.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False).tail(1).reset_index(drop=True)
    except Exception:
        return pd.DataFrame()


def _repair_zero_scores_from_ranking_mtf(df: pd.DataFrame, *, interval: int | None, context: str) -> pd.DataFrame:
    try:
        if not isinstance(df, pd.DataFrame) or df.empty:
            return df
        out = df.copy()
        score_before = _nonzero(out, "score")
        buy_before = _nonzero(out, "score_buy")
        sell_before = _nonzero(out, "score_sell")
        if score_before or buy_before or sell_before:
            return out

        rank = _first_nonzero(out, ("ranking_score", "ranking_score_total"))
        mtf = _first_nonzero(out, ("score_mtf", "mtf_score", "mtf", "mtf_alignment", "score_mtf_daily"))
        daily_buy = _first_nonzero(out, ("daily_mtf_buy", "daily_mtf_buy_daily_src"))
        daily_sell = _first_nonzero(out, ("daily_mtf_sell", "daily_mtf_sell_daily_src"))

        rank_boost = rank.clip(lower=0.0, upper=1.5) * 2.0
        buy_score = (daily_buy.clip(lower=0.0) + rank_boost).where(daily_buy.ne(0), 0.0)
        sell_score = (daily_sell.clip(lower=0.0) + rank_boost).where(daily_sell.ne(0), 0.0)

        no_side = buy_score.eq(0) & sell_score.eq(0) & mtf.ne(0)
        if bool(no_side.any()):
            neutral = (mtf.clip(lower=0.0, upper=6.5) * 0.5 + rank_boost).fillna(0.0)
            buy_score = buy_score.where(~no_side, neutral)
            sell_score = sell_score.where(~no_side, neutral)

        has_any = buy_score.ne(0) | sell_score.ne(0)
        if not bool(has_any.any()):
            return out

        signed = buy_score - sell_score
        display = pd.concat([buy_score.abs(), sell_score.abs(), mtf.abs(), rank_boost.abs()], axis=1).max(axis=1).fillna(0.0)

        for col in ("score_buy", "buy_score"):
            out[col] = _ensure_numeric_col(out, col).where(_ensure_numeric_col(out, col).ne(0), buy_score)
        for col in ("score_sell", "sell_score"):
            out[col] = _ensure_numeric_col(out, col).where(_ensure_numeric_col(out, col).ne(0), sell_score)
        for col in ("score", "score_total", "final_score"):
            out[col] = _ensure_numeric_col(out, col).where(_ensure_numeric_col(out, col).ne(0), signed)
        out["display_score"] = _ensure_numeric_col(out, "display_score").where(_ensure_numeric_col(out, "display_score").ne(0), display)
        out["summary_score_repaired_from_ranking_mtf"] = has_any

        logger.warning(
            "[SUMMARY CONTROLLER SCORE REPAIR] context=%s interval=%s rows=%s repaired=%s score %s->%s buy %s->%s sell %s->%s",
            context, interval, len(out), int(has_any.sum()), score_before, _nonzero(out, "score"), buy_before, _nonzero(out, "score_buy"), sell_before, _nonzero(out, "score_sell"),
        )
        return out
    except Exception:
        logger.debug("[SUMMARY CONTROLLER SCORE REPAIR] failed context=%s interval=%s", context, interval, exc_info=True)
        return df


def _repair_score_aliases(df: Any, *, interval: int | None = None, context: str = "runtime") -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    try:
        out = df.copy()
        changed: dict[str, tuple[int, int]] = {}
        out = _repair_zero_scores_from_ranking_mtf(out, interval=interval, context=context)

        for dst, src in (("buy_score", "score_buy"), ("sell_score", "score_sell")):
            if src in out.columns:
                before = _nonzero(out, dst)
                src_s = _num_series(out, src, 0.0)
                dst_s = _ensure_numeric_col(out, dst)
                out[dst] = src_s if before == 0 and int(src_s.ne(0).sum()) > 0 else dst_s.where(dst_s.ne(0), src_s)
                after = _nonzero(out, dst)
                if after != before:
                    changed[dst] = (before, after)

        if "display_score" in out.columns:
            before = _nonzero(out, "display_score")
            disp = _ensure_numeric_col(out, "display_score")
            if before == 0:
                for src in ("final_score", "score_total", "score"):
                    if src in out.columns:
                        src_s = _num_series(out, src, 0.0)
                        if int(src_s.ne(0).sum()) > 0:
                            out["display_score"] = src_s.abs()
                            break
            else:
                for src in ("final_score", "score_total", "score"):
                    if src in out.columns:
                        out["display_score"] = disp.where(disp.ne(0), _num_series(out, src, 0.0).abs())
                        break
            after = _nonzero(out, "display_score")
            if after != before:
                changed["display_score"] = (before, after)

        if changed:
            logger.warning("[SUMMARY CONTROLLER SCORE ALIAS REPAIR] context=%s interval=%s rows=%s changed=%s", context, interval, len(out), changed)
        return out
    except Exception:
        logger.debug("[SUMMARY CONTROLLER SCORE ALIAS REPAIR] failed context=%s interval=%s", context, interval, exc_info=True)
        return df


def _read_global_summary(interval: int) -> pd.DataFrame:
    try:
        from global_state import global_data
        candidates: list[Any] = []
        for method in ("get_summary_history", "get_push_merged_summary", "get_latest_summary", "get_push_summary", "get_merged_summary"):
            try:
                fn = getattr(global_data, method, None)
                if callable(fn):
                    candidates.append(fn(int(interval)))
            except Exception:
                pass
        for name in (
            f"summary_{int(interval)}m_df", f"push_summary_{int(interval)}m_df", f"latest_summary_{int(interval)}m_df",
            f"summary_{int(interval)}m_latest_df", f"push_merged_summary_{int(interval)}m_df",
        ):
            try:
                candidates.append(getattr(global_data, name, None))
            except Exception:
                pass
        frames = []
        for x in candidates:
            if isinstance(x, pd.DataFrame) and not x.empty:
                d = _normalize_dt(x)
                if not d.empty:
                    frames.append(d)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True, sort=False)
        out = out.loc[:, ~pd.Index(out.columns).duplicated()].copy()
        out = _normalize_dt(out)
        if out.empty:
            return pd.DataFrame()
        return out.drop_duplicates(subset=["symbol", "datetime"], keep="last").reset_index(drop=True)
    except Exception:
        logger.debug("[SUMMARY CONTROLLER INTRADAY MTF] read global summary failed interval=%s", interval, exc_info=True)
        return pd.DataFrame()


def _signed_mtf_score(df: pd.DataFrame) -> pd.Series:
    score = _first_nonzero(df, ("score_total", "final_score", "score", "mtf", "score_mtf", "mtf_score", "slope_atr_scaled", "slope"))
    try:
        buy = _first_nonzero(df, ("score_buy", "buy_score"))
        sell = _first_nonzero(df, ("score_sell", "sell_score"))
        side_score = buy.where(buy.ne(0), 0.0) - sell.where(sell.ne(0), 0.0)
        score = score.where(score.ne(0), side_score)
    except Exception:
        pass
    return score.fillna(0.0)


def _fresh_latest_mtf(interval: int) -> pd.DataFrame:
    df = _latest_by_symbol(_read_global_summary(interval))
    if df.empty:
        return pd.DataFrame()
    try:
        max_age = _env_float("SUMMARY_INTRADAY_MTF_BACKFILL_MAX_AGE_SEC", 240.0)
        latest_dt = pd.to_datetime(df["datetime"], errors="coerce")
        now = pd.Timestamp.now()
        age = (now - latest_dt).dt.total_seconds()
        # future labels from right-closed resample are accepted as fresh; old labels are rejected.
        df = df[(age <= max_age) | (age < 0)].copy()
    except Exception:
        pass
    if df.empty:
        return pd.DataFrame()
    df[f"mtf_{interval}m"] = _signed_mtf_score(df)
    return df[["symbol", f"mtf_{interval}m"]].drop_duplicates("symbol", keep="last")


def _build_intraday_mtf_map() -> pd.DataFrame:
    try:
        m3 = _fresh_latest_mtf(3)
        m5 = _fresh_latest_mtf(5)
        if m3.empty and m5.empty:
            return pd.DataFrame()
        out = m3 if not m3.empty else pd.DataFrame(columns=["symbol"])
        if not m5.empty:
            out = out.merge(m5, on="symbol", how="outer") if "symbol" in out.columns and not out.empty else m5
        out = _symbolize(out)
        s3 = pd.to_numeric(out.get("mtf_3m", pd.Series(index=out.index)), errors="coerce").fillna(0.0)
        s5 = pd.to_numeric(out.get("mtf_5m", pd.Series(index=out.index)), errors="coerce").fillna(0.0)
        both = s3.ne(0) & s5.ne(0)
        same_dir = both & ((s3 > 0) == (s5 > 0))
        one_side = s3.where(s3.ne(0), s5)
        combined = one_side.where(~same_dir, (s3 + s5) / 2.0)
        # 3m/5mが両方あり方向不一致なら補完しない。片側だけなら補完可。
        combined = combined.where(~(both & ~same_dir), 0.0)
        out["_intraday_mtf_backfill"] = combined.fillna(0.0)
        return out[["symbol", "_intraday_mtf_backfill"]][out["_intraday_mtf_backfill"].ne(0)].drop_duplicates("symbol", keep="last")
    except Exception:
        logger.debug("[SUMMARY CONTROLLER INTRADAY MTF] build map failed", exc_info=True)
        return pd.DataFrame()


def _apply_intraday_mtf_to_1m(df: Any, *, context: str) -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return df
    try:
        out = _symbolize(df)
        if out.empty:
            return df
        before = _nonzero(out, "score_mtf") + _nonzero(out, "mtf") + _nonzero(out, "mtf_score")
        mtf_map = _build_intraday_mtf_map()
        if mtf_map.empty:
            return out
        merged = out.merge(mtf_map, on="symbol", how="left")
        src = pd.to_numeric(merged.get("_intraday_mtf_backfill", pd.Series(index=merged.index)), errors="coerce").fillna(0.0)
        for col in ("mtf", "score_mtf", "mtf_score", "mtf_alignment"):
            base = _ensure_numeric_col(merged, col)
            merged[col] = base.where(base.ne(0), src)
        merged = merged.drop(columns=["_intraday_mtf_backfill"], errors="ignore")
        after = _nonzero(merged, "score_mtf") + _nonzero(merged, "mtf") + _nonzero(merged, "mtf_score")
        if after != before:
            logger.warning(
                "[SUMMARY CONTROLLER INTRADAY MTF] context=%s interval=1 rows=%s mtf_nonzero %s->%s map_rows=%s version=%s",
                context, len(merged), before, after, len(mtf_map), VERSION,
            )
        return merged
    except Exception:
        logger.debug("[SUMMARY CONTROLLER INTRADAY MTF] apply failed context=%s", context, exc_info=True)
        return df


def _enrich(df: Any, *, interval: int | None = None, context: str = "runtime") -> Any:
    if not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
        return df
    try:
        from trading.summary.controller_enrich import enrich_summary_latest
        interval_i = int(interval or 1)
        before_rank = _nonzero(df, "ranking_score")
        out = enrich_summary_latest(df, interval=interval_i, context=context)
        if interval_i == 1:
            out = _apply_intraday_mtf_to_1m(out, context=context)
        out = _repair_score_aliases(out, interval=interval_i, context=context)
        after_rank = _nonzero(out, "ranking_score")
        if after_rank != before_rank or "ranking_score" not in df.columns:
            logger.warning(
                "[SUMMARY CONTROLLER LATEST ENRICH] context=%s interval=%s rows=%s ranking_score_nonzero %s->%s",
                context, interval_i, len(out) if isinstance(out, pd.DataFrame) else len(df), before_rank, after_rank,
            )
        return out if isinstance(out, pd.DataFrame) else df
    except Exception:
        logger.debug("[SUMMARY CONTROLLER LATEST ENRICH] enrich failed context=%s interval=%s", context, interval, exc_info=True)
        fallback = _apply_intraday_mtf_to_1m(df, context=context) if int(interval or 1) == 1 else df
        return _repair_score_aliases(fallback, interval=interval, context=context)


def _extract_interval(args: tuple[Any, ...], kwargs: dict[str, Any]) -> int | None:
    try:
        for key in ("tf", "interval"):
            if key in kwargs:
                return int(kwargs[key])
        for x in args:
            if isinstance(x, int):
                return int(x)
            if isinstance(x, str) and x.strip().isdigit():
                return int(x)
    except Exception:
        return None
    return None


def _extract_df(args: tuple[Any, ...], kwargs: dict[str, Any]) -> pd.DataFrame | None:
    if isinstance(kwargs.get("df"), pd.DataFrame):
        return kwargs["df"]
    for x in args:
        if isinstance(x, pd.DataFrame):
            return x
    return None


def _replace_df(args: tuple[Any, ...], kwargs: dict[str, Any], df: pd.DataFrame) -> tuple[tuple[Any, ...], dict[str, Any]]:
    if "df" in kwargs and isinstance(kwargs.get("df"), pd.DataFrame):
        new_kwargs = dict(kwargs)
        new_kwargs["df"] = df
        return args, new_kwargs
    new_args = list(args)
    for i, x in enumerate(new_args):
        if isinstance(x, pd.DataFrame):
            new_args[i] = df
            return tuple(new_args), kwargs
    return args, kwargs


def _call_original_setter(name: str, interval: int, df: pd.DataFrame) -> None:
    try:
        from global_state import global_data
        fn = _ORIG_GLOBAL_SETTERS.get(name) or getattr(global_data, name, None)
        if not callable(fn):
            return
        for call in (
            lambda: fn(tf=interval, df=df.copy(), source="push"),
            lambda: fn(interval, df.copy()),
            lambda: fn(interval, df.copy(), "push"),
        ):
            try:
                call()
                return
            except TypeError:
                continue
    except Exception:
        logger.debug("[SUMMARY CONTROLLER INTRADAY MTF] original setter failed name=%s interval=%s", name, interval, exc_info=True)


def _refresh_1m_mtf_from_global(reason: str) -> None:
    global _REFRESHING_1M
    if _REFRESHING_1M:
        return
    try:
        _REFRESHING_1M = True
        one = _read_global_summary(1)
        if one.empty:
            return
        before = _nonzero(one, "score_mtf") + _nonzero(one, "mtf") + _nonzero(one, "mtf_score")
        enriched = _apply_intraday_mtf_to_1m(one, context=f"refresh-{reason}")
        after = _nonzero(enriched, "score_mtf") + _nonzero(enriched, "mtf") + _nonzero(enriched, "mtf_score")
        if after <= before:
            return
        latest = _latest_by_symbol(enriched)
        for method_name, df_arg in (
            ("set_summary_history", enriched),
            ("set_push_merged_summary", latest if not latest.empty else enriched),
            ("set_merged_summary", latest if not latest.empty else enriched),
            ("set_latest_summary", latest if not latest.empty else enriched),
            ("set_push_summary", latest if not latest.empty else enriched),
        ):
            _call_original_setter(method_name, 1, df_arg)
        logger.warning("[SUMMARY CONTROLLER INTRADAY MTF] refreshed 1m from global reason=%s rows=%s mtf_nonzero %s->%s version=%s", reason, len(enriched), before, after, VERSION)
    except Exception:
        logger.debug("[SUMMARY CONTROLLER INTRADAY MTF] refresh failed reason=%s", reason, exc_info=True)
    finally:
        _REFRESHING_1M = False


def _patch_global_data_setters() -> bool:
    try:
        from global_state import global_data
        patched = 0
        for name in ("set_summary_history", "set_push_merged_summary", "set_merged_summary", "set_latest_summary", "set_push_summary"):
            old = getattr(global_data, name, None)
            if not callable(old) or getattr(old, "_latest_enrich_intraday_mtf_v4", False):
                continue
            _ORIG_GLOBAL_SETTERS[name] = old

            @wraps(old)
            def _wrapped(*args, __old=old, __name=name, **kwargs):
                interval = _extract_interval(args, kwargs)
                df = _extract_df(args, kwargs)
                if interval == 1 and isinstance(df, pd.DataFrame) and not df.empty and not _REFRESHING_1M:
                    new_df = _enrich(df, interval=1, context=f"global-{__name}")
                    args, kwargs = _replace_df(args, kwargs, new_df)
                    return __old(*args, **kwargs)
                ret = __old(*args, **kwargs)
                if interval in (3, 5) and isinstance(df, pd.DataFrame) and not df.empty:
                    _refresh_1m_mtf_from_global(f"global-{__name}-{interval}m")
                return ret

            _wrapped._latest_enrich_intraday_mtf_v4 = True  # type: ignore[attr-defined]
            _wrapped._original = old  # type: ignore[attr-defined]
            setattr(global_data, name, _wrapped)
            patched += 1
        logger.warning("[SUMMARY CONTROLLER INTRADAY MTF] global setters patched=%s version=%s", patched, VERSION)
        return patched > 0
    except Exception:
        logger.debug("[SUMMARY CONTROLLER INTRADAY MTF] global setter patch failed", exc_info=True)
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.summary.summary_controller as sc

        old_save = getattr(sc, "save_summary", None)
        if callable(old_save) and not getattr(old_save, "_latest_enrich_v4", False):
            @wraps(old_save)
            def _save_summary_enriched(df, interval, *args, **kwargs):
                return old_save(_enrich(df, interval=int(interval), context="before-save"), interval, *args, **kwargs)
            _save_summary_enriched._latest_enrich_v4 = True  # type: ignore[attr-defined]
            _save_summary_enriched._latest_enrich_v3 = True  # type: ignore[attr-defined]
            _save_summary_enriched._original = old_save  # type: ignore[attr-defined]
            sc.save_summary = _save_summary_enriched

        old_run_ranking = getattr(sc, "run_ranking_pipeline", None)
        if callable(old_run_ranking) and not getattr(old_run_ranking, "_latest_enrich_v4", False):
            @wraps(old_run_ranking)
            def _run_ranking_enriched(df_latest, interval, *args, **kwargs):
                return old_run_ranking(_enrich(df_latest, interval=int(interval), context="before-ranking-pipeline"), interval, *args, **kwargs)
            _run_ranking_enriched._latest_enrich_v4 = True  # type: ignore[attr-defined]
            _run_ranking_enriched._latest_enrich_v3 = True  # type: ignore[attr-defined]
            _run_ranking_enriched._original = old_run_ranking  # type: ignore[attr-defined]
            sc.run_ranking_pipeline = _run_ranking_enriched

        old_log_probe = getattr(sc, "log_scoring_probe", None)
        if callable(old_log_probe) and not getattr(old_log_probe, "_latest_enrich_v4", False):
            @wraps(old_log_probe)
            def _log_probe_enriched(label, interval, df, *args, **kwargs):
                return old_log_probe(label, interval, _enrich(df, interval=int(interval), context=f"log-{label}"), *args, **kwargs)
            _log_probe_enriched._latest_enrich_v4 = True  # type: ignore[attr-defined]
            _log_probe_enriched._latest_enrich_v3 = True  # type: ignore[attr-defined]
            _log_probe_enriched._original = old_log_probe  # type: ignore[attr-defined]
            sc.log_scoring_probe = _log_probe_enriched

        old_set_latest = getattr(sc, "safe_global_set_latest", None)
        if callable(old_set_latest) and not getattr(old_set_latest, "_latest_enrich_v4", False):
            @wraps(old_set_latest)
            def _set_latest_enriched(interval, df, *args, **kwargs):
                ret = old_set_latest(interval, _enrich(df, interval=int(interval), context="cache-latest"), *args, **kwargs)
                if int(interval) in (3, 5):
                    _refresh_1m_mtf_from_global(f"summary-controller-cache-latest-{int(interval)}m")
                return ret
            _set_latest_enriched._latest_enrich_v4 = True  # type: ignore[attr-defined]
            _set_latest_enriched._latest_enrich_v3 = True  # type: ignore[attr-defined]
            _set_latest_enriched._original = old_set_latest  # type: ignore[attr-defined]
            sc.safe_global_set_latest = _set_latest_enriched

        global_setters_ok = _patch_global_data_setters()
        _INSTALLED = True
        logger.warning("[SUMMARY CONTROLLER LATEST ENRICH] installed version=%s intraday_mtf_global_setters=%s", VERSION, global_setters_ok)
        return True
    except Exception:
        logger.exception("[SUMMARY CONTROLLER LATEST ENRICH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY CONTROLLER LATEST ENRICH] auto install failed")

__all__ = ["VERSION", "install"]
