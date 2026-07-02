# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_candidate_refill_patch.py
# Version: V6-SUMMARY-AI-OHLC-SCORE-BOOTSTRAP
# ------------------------------------------------------------
# Purpose:
#   SUMMARY_AI candidate refill + safety guards.
#
# V6:
#   - scored completed summary が無い場合でも、fresh PUSH 1m の OHLC/volume から
#     最低限の buy/sell score を作る。
#   - score/slope/macd が全0の raw PUSH summary で candidates=0 になる症状を防ぐ。
#   - 低出来高・低変動・板・発注直前の厳密ガードは後段に残す。
#   - raw DB fallback は鮮度確認には使うが、AI入力では scored summary 優先。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
from functools import wraps
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V6-SUMMARY-AI-OHLC-SCORE-BOOTSTRAP"
_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _as_len(v: Any) -> int:
    try:
        if v is None:
            return 0
        return len(v)
    except Exception:
        return 0


def _is_empty_df_like(v: Any) -> bool:
    try:
        return bool(v is None or getattr(v, "empty", True))
    except Exception:
        return True


def _to_datetime(v: Any) -> dt.datetime | None:
    if v is None:
        return None
    if isinstance(v, dt.datetime):
        return v.replace(tzinfo=None)
    try:
        import pandas as pd
        ts = pd.to_datetime(v, errors="coerce")
        if pd.isna(ts):
            return None
        try:
            if getattr(ts, "tzinfo", None) is not None:
                ts = ts.tz_convert(None)
        except Exception:
            pass
        if hasattr(ts, "to_pydatetime"):
            return ts.to_pydatetime().replace(tzinfo=None)
    except Exception:
        pass
    try:
        return dt.datetime.fromisoformat(str(v).replace("Z", "+00:00").replace("+00:00", "")).replace(tzinfo=None)
    except Exception:
        return None


def _df_latest_age_sec(df: Any) -> tuple[bool, float | None, dt.datetime | None, int]:
    try:
        if df is None or getattr(df, "empty", True):
            return False, None, None, 0
        cols = getattr(df, "columns", [])
        dt_col = None
        for c in ("datetime", "end_time", "last_update", "updated_at", "inserted_at"):
            if c in cols:
                dt_col = c
                break
        if not dt_col:
            return False, None, None, len(df)
        latest = _to_datetime(df[dt_col].max())
        if latest is None:
            return False, None, None, len(df)
        age = (dt.datetime.now().replace(tzinfo=None) - latest).total_seconds()
        return True, age, latest, len(df)
    except Exception:
        return False, None, None, 0


def _num_series(df: Any, names: tuple[str, ...], default: float = 0.0):
    import pandas as pd
    try:
        idx = df.index
    except Exception:
        idx = None
    for name in names:
        try:
            if name in getattr(df, "columns", []):
                return pd.to_numeric(df[name], errors="coerce").fillna(default).astype(float)
        except Exception:
            continue
    return pd.Series(default, index=idx, dtype="float64")


def _max_series(*series: Any):
    import pandas as pd
    frames = []
    for s in series:
        try:
            frames.append(pd.to_numeric(s, errors="coerce").fillna(0.0).astype(float))
        except Exception:
            pass
    if not frames:
        return None
    return pd.concat(frames, axis=1).max(axis=1).fillna(0.0)


def _score_profile(df: Any) -> dict[str, Any]:
    try:
        import pandas as pd
        if df is None or getattr(df, "empty", True):
            return {"rows": 0, "score_nonzero": 0, "buy_pos": 0, "sell_pos": 0, "total_pos": 0, "total_neg": 0, "max_buy": 0.0, "max_sell": 0.0}
        buy = _num_series(df, ("ai_disp_buy_score", "config_buy_score", "score_buy", "buy_score", "buy", "buy_signal_score"), 0.0)
        sell = _num_series(df, ("ai_disp_sell_score", "config_sell_score", "score_sell", "sell_score", "sell", "sell_signal_score"), 0.0).abs()
        total = _num_series(df, ("ai_disp_total_score", "score_total", "total_score", "combined_score", "final_score", "display_score", "score"), 0.0)
        score_nonzero = int(((buy.abs() > 0) | (sell.abs() > 0) | (total.abs() > 0)).sum())
        return {
            "rows": len(df),
            "score_nonzero": score_nonzero,
            "buy_pos": int((buy > 0).sum()),
            "sell_pos": int((sell > 0).sum()),
            "total_pos": int((total > 0).sum()),
            "total_neg": int((total < 0).sum()),
            "max_buy": float(pd.to_numeric(buy, errors="coerce").fillna(0.0).max()) if len(buy) else 0.0,
            "max_sell": float(pd.to_numeric(sell, errors="coerce").fillna(0.0).max()) if len(sell) else 0.0,
        }
    except Exception:
        return {"rows": _as_len(df), "score_nonzero": 0, "buy_pos": 0, "sell_pos": 0, "total_pos": 0, "total_neg": 0, "max_buy": 0.0, "max_sell": 0.0}


def _has_score(df: Any) -> bool:
    try:
        return int(_score_profile(df).get("score_nonzero", 0)) > 0
    except Exception:
        return False


def _get_scored_completed_push_summary() -> Any:
    if not _env_bool("SUMMARY_AI_INPUT_PREFER_SCORED_SUMMARY", True):
        return None
    try:
        import core.global_context.context as ctx
        calls = (
            ("get_push_merged_summary", {"tf": 1}),
            ("get_push_merged_summary", {"interval": 1}),
            ("get_merged_summary", {"tf": 1, "source": "push"}),
            ("get_merged_summary", {"interval": 1, "source": "push"}),
            ("get_push_summary", {"tf": 1}),
            ("get_push_summary", {"interval": 1}),
        )
        best = None
        best_score_n = -1
        best_label = ""
        for name, kwargs in calls:
            fn = getattr(ctx, name, None)
            if not callable(fn):
                continue
            try:
                df = fn(**kwargs)
            except TypeError:
                continue
            except Exception:
                continue
            if _is_empty_df_like(df):
                continue
            prof = _score_profile(df)
            score_n = int(prof.get("score_nonzero", 0) or 0)
            if score_n <= 0:
                continue
            ok, age, latest, rows = _df_latest_age_sec(df)
            max_age = _env_float("SUMMARY_AI_SCORED_SUMMARY_MAX_AGE_SEC", _env_float("SUMMARY_AI_MAX_PUSH_1M_AGE_SEC", 120.0))
            if ok and age is not None and age > max_age:
                logger.warning("[SUMMARY AI INPUT PRIORITY] skip stale scored summary source=global_context.%s rows=%s latest=%s age=%.1f max=%.1f score_nonzero=%s", name, rows, latest, age, max_age, score_n)
                continue
            if score_n > best_score_n:
                best = df
                best_score_n = score_n
                best_label = f"global_context.{name}"
        if best is not None:
            prof = _score_profile(best)
            ok, age, latest, rows = _df_latest_age_sec(best)
            logger.warning(
                "[SUMMARY AI INPUT PRIORITY] selected scored completed summary source=%s rows=%s latest=%s age=%s score_nonzero=%s buy_pos=%s sell_pos=%s total_pos=%s total_neg=%s",
                best_label, rows, latest, f"{age:.1f}" if age is not None else None,
                prof.get("score_nonzero"), prof.get("buy_pos"), prof.get("sell_pos"), prof.get("total_pos"), prof.get("total_neg"),
            )
            return best
    except Exception:
        logger.debug("[SUMMARY AI INPUT PRIORITY] scored summary lookup failed", exc_info=True)
    return None


def _get_push_1m_context() -> Any:
    first_empty = None
    scored = _get_scored_completed_push_summary()
    if not _is_empty_df_like(scored):
        return scored
    try:
        import core.global_context.context as ctx
        calls = (
            ("get_push_merged_summary", {"tf": 1}),
            ("get_push_merged_summary", {"interval": 1}),
            ("get_merged_summary", {"tf": 1, "source": "push"}),
            ("get_merged_summary", {"interval": 1, "source": "push"}),
            ("get_summary_history", {"tf": 1, "source": "push"}),
            ("get_summary_history", {"interval": 1, "source": "push"}),
        )
        for name, kwargs in calls:
            fn = getattr(ctx, name, None)
            if not callable(fn):
                continue
            try:
                df = fn(**kwargs)
            except TypeError:
                continue
            except Exception:
                continue
            if not _is_empty_df_like(df):
                logger.info("[SUMMARY AI SAFETY GUARD] fresh-check source=global_context.%s rows=%s", name, _as_len(df))
                return df
            if first_empty is None:
                first_empty = df
    except Exception:
        pass

    if _env_bool("SUMMARY_AI_FRESH_CHECK_ALLOW_RAW_DB_FALLBACK", True):
        try:
            import scheduler_jobs.summary.fallback_loader as fl
            fn = getattr(fl, "fallback_push_summary_df", None)
            if callable(fn):
                now_i = getattr(fl, "now_naive", lambda: dt.datetime.now())()
                df = fn(1, now=now_i)
                if not _is_empty_df_like(df):
                    logger.warning(
                        "[SUMMARY AI SAFETY GUARD] fresh-check source=push_raw_db_fallback rows=%s latest=%s score_nonzero=%s note=raw_used_for_freshness_only",
                        _as_len(df),
                        getattr(df, "datetime", None).max() if hasattr(df, "datetime") else None,
                        _score_profile(df).get("score_nonzero"),
                    )
                    return df
        except Exception:
            logger.debug("[SUMMARY AI SAFETY GUARD] raw db fallback fresh-check failed", exc_info=True)
    return first_empty


def _summary_ai_entry_unsafe_reason() -> str | None:
    if _env_bool("SUMMARY_AI_REQUIRE_FRESH_PUSH_1M", True):
        df = _get_push_1m_context()
        ok, age, latest, rows = _df_latest_age_sec(df)
        if not ok:
            return f"no_fresh_push_1m rows={rows}"
        max_age = _env_float("SUMMARY_AI_MAX_PUSH_1M_AGE_SEC", 120.0)
        if age is None or age > max_age:
            return f"stale_push_1m rows={rows} latest={latest} age={age} max={max_age}"
    return None


def _result_needs_refill(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    try:
        execution = result.get("execution") if isinstance(result.get("execution"), dict) else {}
        if bool(execution.get("executed")):
            return False
        if _as_len(result.get("approved_rows")) > 0:
            return False
        candidates_n = _as_len(result.get("candidates"))
        ai_results_n = _as_len(result.get("ai_results"))
        skip = str(execution.get("skip_reason") or result.get("skip_reason") or "").lower()
        if candidates_n == 0 or ai_results_n == 0:
            return True
        if any(x in skip for x in ("no_candidates", "no_ai_ok", "no approved", "approved")):
            return True
    except Exception:
        return False
    return False


def _blocked_result(reason: str) -> dict[str, Any]:
    return {
        "candidates": [],
        "ai_results": [],
        "ai_ok": [],
        "approved_rows": [],
        "execution": {"executed": False, "skip_reason": f"blocked_by_summary_ai_safety_guard:{reason}"},
        "skip_reason": f"blocked_by_summary_ai_safety_guard:{reason}",
        "safety_blocked": True,
    }


def _bootstrap_scores_from_ohlc(df: Any) -> Any:
    """Create conservative SUMMARY_AI scores from fresh 1m OHLC when all score columns are zero."""
    if not _env_bool("SUMMARY_AI_BOOTSTRAP_SCORE_FROM_OHLC", True):
        return df
    try:
        import pandas as pd
        if not isinstance(df, pd.DataFrame) or df.empty or _has_score(df):
            return df
        out = df.copy()
        close = _num_series(out, ("close_price", "close", "price", "current_price"), 0.0)
        open_ = _num_series(out, ("open_price", "open", "day_open"), 0.0)
        high = _num_series(out, ("high_price", "high", "day_high"), 0.0)
        low = _num_series(out, ("low_price", "low", "day_low"), 0.0)
        volume = _num_series(out, ("volume", "trading_volume", "Volume"), 0.0)
        turnover = _num_series(out, ("turnover", "trading_value", "TradingValue"), 0.0)
        turnover = turnover.where(turnover > 0, close * volume)

        base = open_.where(open_ > 0, close)
        signed_body_pct = ((close - base) / base.where(base > 0, 1.0) * 100.0).fillna(0.0)
        range_pct = ((high - low) / close.where(close > 0, 1.0)).fillna(0.0)
        pos_pct = ((close - low) / (high - low).where((high - low) > 0, 1.0) * 100.0).fillna(50.0)

        min_turnover = _env_float("SUMMARY_AI_BOOTSTRAP_MIN_TURNOVER", _env_float("ENTRY_MIN_TURNOVER", 10000000.0))
        min_volume = _env_float("SUMMARY_AI_BOOTSTRAP_MIN_VOLUME", _env_float("ENTRY_MIN_VOLUME", 30000.0))
        min_range = _env_float("SUMMARY_AI_BOOTSTRAP_MIN_RANGE_PCT", 0.0015)
        strength = (signed_body_pct.abs() * 2.0 + (range_pct * 100.0) * 0.8).clip(lower=0.0, upper=4.0)
        liquid_ok = (volume >= min_volume) | (turnover >= min_turnover)
        range_ok = range_pct >= min_range

        buy = strength.where((signed_body_pct > 0) & (pos_pct >= 55.0) & liquid_ok & range_ok, 0.0)
        sell = strength.where((signed_body_pct < 0) & (pos_pct <= 45.0) & liquid_ok & range_ok, 0.0)
        # If the candle closes at the edge but body is almost flat, still expose a tiny directional score.
        edge_strength = ((range_pct * 100.0) * 0.6).clip(lower=0.0, upper=2.0)
        buy = buy.where(buy > 0, edge_strength.where((pos_pct >= 85.0) & liquid_ok & range_ok, 0.0))
        sell = sell.where(sell > 0, edge_strength.where((pos_pct <= 15.0) & liquid_ok & range_ok, 0.0))

        total = buy - sell
        out["score_buy"] = buy
        out["buy_score"] = buy
        out["score_sell"] = sell
        out["sell_score"] = sell
        out["score_total"] = total
        out["total_score"] = total
        out["final_score"] = total
        out["display_score"] = total
        out["ai_disp_buy_score"] = buy
        out["ai_disp_sell_score"] = sell
        out["ai_disp_total_score"] = total
        out["ai_disp_final_score"] = total
        if "source" not in out.columns:
            out["source"] = "SUMMARY"
        out["summary_ai_score_bootstrap"] = True
        out["summary_ai_bootstrap_body_pct"] = signed_body_pct
        out["summary_ai_bootstrap_range_pct"] = range_pct
        out["summary_ai_bootstrap_pos_pct"] = pos_pct

        logger.warning(
            "[SUMMARY AI SCORE BOOTSTRAP] applied rows=%s buy_pos=%s sell_pos=%s max_buy=%.3f max_sell=%.3f min_vol=%.0f min_turnover=%.0f min_range=%.4f version=%s",
            len(out), int((buy > 0).sum()), int((sell > 0).sum()), float(buy.max()) if len(buy) else 0.0, float(sell.max()) if len(sell) else 0.0,
            min_volume, min_turnover, min_range, VERSION,
        )
        return out
    except Exception:
        logger.exception("[SUMMARY AI SCORE BOOTSTRAP] failed; use original df")
        return df


def _bridge_summary_ai_score_columns(df: Any) -> Any:
    if not _env_bool("SUMMARY_AI_SCORE_BRIDGE_ENABLED", True):
        return df
    try:
        import pandas as pd
        if not isinstance(df, pd.DataFrame) or df.empty:
            return df
        out = _bootstrap_scores_from_ohlc(df)
        if _has_score(out):
            df = out
        out = df.copy()
        idx = out.index
        old_ai_buy = _num_series(out, ("ai_disp_buy_score", "disp_buy_score"), 0.0)
        old_ai_sell = _num_series(out, ("ai_disp_sell_score", "disp_sell_score"), 0.0).abs()
        cfg_buy = _num_series(out, ("config_buy_score", "config_buy_entry_score", "config_buy_bonus_score"), 0.0)
        cfg_sell = _num_series(out, ("config_sell_score", "config_sell_entry_score", "config_sell_bonus_score"), 0.0).abs()
        score_buy = _num_series(out, ("score_buy", "buy_score", "buy", "buy_signal_score"), 0.0)
        score_sell = _num_series(out, ("score_sell", "sell_score", "sell", "sell_signal_score"), 0.0).abs()
        score_total = _num_series(out, ("score_total", "total_score", "combined_score", "final_score", "display_score", "score"), 0.0)
        buy_from_total = score_total.clip(lower=0.0)
        sell_from_total = (-score_total).clip(lower=0.0)
        new_buy = _max_series(old_ai_buy, cfg_buy, score_buy, buy_from_total)
        new_sell = _max_series(old_ai_sell, cfg_sell, score_sell, sell_from_total)
        if new_buy is None or new_sell is None:
            return df
        new_buy = pd.Series(new_buy, index=idx).fillna(0.0).astype(float)
        new_sell = pd.Series(new_sell, index=idx).fillna(0.0).abs().astype(float)
        new_total = new_buy - new_sell
        out["ai_disp_buy_score"] = new_buy
        out["ai_disp_sell_score"] = new_sell
        out["config_buy_score"] = _max_series(cfg_buy, score_buy, buy_from_total).fillna(0.0)
        out["config_sell_score"] = _max_series(cfg_sell, score_sell, sell_from_total).fillna(0.0)
        out["score_buy"] = new_buy
        out["buy_score"] = new_buy
        out["score_sell"] = new_sell
        out["sell_score"] = new_sell
        out["score_total"] = new_total
        out["total_score"] = new_total
        out["final_score"] = new_total
        out["display_score"] = new_total
        out["ai_disp_total_score"] = new_total
        out["ai_disp_final_score"] = new_total
        logger.warning(
            "[SUMMARY AI SCORE BRIDGE] applied rows=%s buy_positive=%s sell_positive=%s buy_max=%.3f sell_max=%.3f cols=%s version=%s",
            len(out), int((new_buy > 0).sum()), int((new_sell > 0).sum()), float(new_buy.max()) if len(new_buy) else 0.0, float(new_sell.max()) if len(new_sell) else 0.0,
            [c for c in ("score_buy", "buy_score", "score_sell", "sell_score", "score_total", "final_score", "display_score", "score") if c in out.columns], VERSION,
        )
        return out
    except Exception:
        logger.exception("[SUMMARY AI SCORE BRIDGE] failed; use original df")
        return df


def _prefer_scored_summary_for_ai_input(df: Any, *, source: str | None = None, interval: Any = None) -> Any:
    try:
        current_prof = _score_profile(df)
        if int(current_prof.get("score_nonzero", 0) or 0) > 0:
            return df
        scored = _get_scored_completed_push_summary()
        if not _is_empty_df_like(scored) and _has_score(scored):
            scored_prof = _score_profile(scored)
            logger.warning(
                "[SUMMARY AI INPUT PRIORITY] replaced no-score AI input with scored completed summary current_rows=%s current_score_nonzero=%s replacement_rows=%s replacement_score_nonzero=%s buy_pos=%s sell_pos=%s source=%s interval=%s",
                current_prof.get("rows"), current_prof.get("score_nonzero"), scored_prof.get("rows"), scored_prof.get("score_nonzero"), scored_prof.get("buy_pos"), scored_prof.get("sell_pos"), source, interval,
            )
            return scored
        boot = _bootstrap_scores_from_ohlc(df)
        boot_prof = _score_profile(boot)
        if int(boot_prof.get("score_nonzero", 0) or 0) > 0:
            logger.warning(
                "[SUMMARY AI INPUT PRIORITY] bootstrapped no-score AI input current_rows=%s bootstrap_score_nonzero=%s buy_pos=%s sell_pos=%s source=%s interval=%s",
                current_prof.get("rows"), boot_prof.get("score_nonzero"), boot_prof.get("buy_pos"), boot_prof.get("sell_pos"), source, interval,
            )
            return boot
        logger.warning(
            "[SUMMARY AI INPUT PRIORITY] no scored summary replacement current_rows=%s current_score_nonzero=%s source=%s interval=%s",
            current_prof.get("rows"), current_prof.get("score_nonzero"), source, interval,
        )
        return df
    except Exception:
        logger.exception("[SUMMARY AI INPUT PRIORITY] replacement failed; use original df")
        return df


def _bridge_runner_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> tuple[tuple[Any, ...], dict[str, Any]]:
    try:
        new_args = list(args)
        new_kwargs = dict(kwargs)
        source = str(new_kwargs.get("source") or "SUMMARY")
        interval = new_kwargs.get("interval")
        if new_args:
            selected = _prefer_scored_summary_for_ai_input(new_args[0], source=source, interval=interval)
            new_args[0] = _bridge_summary_ai_score_columns(selected)
        else:
            for key in ("summary_df", "df", "source_df", "base_df"):
                if key in new_kwargs:
                    selected = _prefer_scored_summary_for_ai_input(new_kwargs[key], source=source, interval=interval)
                    new_kwargs[key] = _bridge_summary_ai_score_columns(selected)
                    break
        return tuple(new_args), new_kwargs
    except Exception:
        logger.exception("[SUMMARY AI SCORE BRIDGE] arg bridge failed")
        return args, kwargs


def _apply_wide_kwargs(kwargs: dict[str, Any], *, refill: bool = False) -> dict[str, Any]:
    out = dict(kwargs)
    top_n = max(1, _env_int("SUMMARY_AI_REFILL_TOP_N", 60))
    tonosama_n = max(1, _env_int("SUMMARY_AI_REFILL_TONOSAMA_MAX_CANDIDATES", top_n))
    if refill:
        top_n = max(top_n, _env_int("SUMMARY_AI_REFILL_RETRY_TOP_N", 80))
        tonosama_n = max(tonosama_n, _env_int("SUMMARY_AI_REFILL_RETRY_TONOSAMA_MAX_CANDIDATES", top_n))
    for key in ("top_n", "max_candidates", "candidate_limit"):
        try:
            cur = int(float(out.get(key))) if key in out and out.get(key) is not None else 0
        except Exception:
            cur = 0
        if cur <= 0 or cur < top_n:
            out[key] = top_n
    try:
        cur_t = int(float(out.get("tonosama_max_candidates"))) if out.get("tonosama_max_candidates") is not None else 0
    except Exception:
        cur_t = 0
    if cur_t <= 0 or cur_t < tonosama_n:
        out["tonosama_max_candidates"] = tonosama_n
    return out


def _summarize_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        return {"type": type(result).__name__}
    execution = result.get("execution") if isinstance(result.get("execution"), dict) else {}
    return {
        "candidates": _as_len(result.get("candidates")),
        "ai_results": _as_len(result.get("ai_results")),
        "ai_ok": _as_len(result.get("ai_ok")),
        "approved": _as_len(result.get("approved_rows")),
        "executed": bool(execution.get("executed")),
        "skip": execution.get("skip_reason") or result.get("skip_reason"),
    }


def _install_summary_ai_guard() -> bool:
    try:
        from trading.entry.summary_ai import runner as r
        cur = getattr(r, "run_summary_ai_entry_from_df", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI SAFETY GUARD] target missing")
            return False
        if getattr(cur, "_summary_ai_candidate_refill_v6", False):
            return True
        orig = getattr(cur, "_original", cur)

        @wraps(cur)
        def patched_run_summary_ai_entry_from_df(*args: Any, **kwargs: Any):
            source = str(kwargs.get("source") or "SUMMARY").upper()
            if "SUMMARY" in source and "RANKING" not in source:
                reason = _summary_ai_entry_unsafe_reason()
                if reason:
                    logger.warning("[SUMMARY AI SAFETY GUARD] blocked source=%s interval=%s reason=%s", source, kwargs.get("interval"), reason)
                    return _blocked_result(reason)
            wide_kwargs = _apply_wide_kwargs(kwargs, refill=False)
            bridged_args, bridged_kwargs = _bridge_runner_args(args, wide_kwargs)
            first = cur(*bridged_args, **bridged_kwargs)
            if not _env_bool("SUMMARY_AI_REFILL_RETRY_WITHOUT_TONOSAMA", True):
                return first
            if not _result_needs_refill(first):
                return first
            retry_kwargs = _apply_wide_kwargs(kwargs, refill=True)
            retry_kwargs["use_tonosama_filter"] = False
            retry_kwargs["fail_open_tonosama"] = False
            retry_args, retry_kwargs = _bridge_runner_args(args, retry_kwargs)
            logger.warning("[SUMMARY AI CANDIDATE REFILL] first pass empty -> retry without TONOSAMA first=%s retry_top_n=%s source=%s interval=%s", _summarize_result(first), retry_kwargs.get("top_n"), retry_kwargs.get("source"), retry_kwargs.get("interval"))
            second = cur(*retry_args, **retry_kwargs)
            if isinstance(second, dict):
                second = dict(second)
                second["candidate_refill_used"] = True
                second["candidate_refill_first"] = _summarize_result(first)
                second["candidate_refill_retry"] = _summarize_result(second)
            return second

        patched_run_summary_ai_entry_from_df._summary_ai_candidate_refill_v6 = True  # type: ignore[attr-defined]
        patched_run_summary_ai_entry_from_df._summary_ai_candidate_refill_v5 = True  # type: ignore[attr-defined]
        patched_run_summary_ai_entry_from_df._summary_ai_candidate_refill_v4 = True  # type: ignore[attr-defined]
        patched_run_summary_ai_entry_from_df._summary_ai_candidate_refill_v3 = True  # type: ignore[attr-defined]
        patched_run_summary_ai_entry_from_df._summary_ai_candidate_refill_v2 = True  # type: ignore[attr-defined]
        patched_run_summary_ai_entry_from_df._original = orig  # type: ignore[attr-defined]
        r.run_summary_ai_entry_from_df = patched_run_summary_ai_entry_from_df
        try:
            r.DEFAULT_AI_ENTRY_TOP_N = max(int(getattr(r, "DEFAULT_AI_ENTRY_TOP_N", 20)), _env_int("SUMMARY_AI_REFILL_TOP_N", 60))
            r.DEFAULT_TONOSAMA_AI_CANDIDATES = max(int(getattr(r, "DEFAULT_TONOSAMA_AI_CANDIDATES", 20)), _env_int("SUMMARY_AI_REFILL_TONOSAMA_MAX_CANDIDATES", 60))
        except Exception:
            pass
        logger.warning(
            "[SUMMARY AI SAFETY GUARD] installed version=%s top_n=%s tonosama_max=%s writer_required=%s fresh_required=%s raw_fallback=%s score_bridge=%s ohlc_bootstrap=%s prefer_scored_input=%s",
            VERSION,
            _env_int("SUMMARY_AI_REFILL_TOP_N", 60),
            _env_int("SUMMARY_AI_REFILL_TONOSAMA_MAX_CANDIDATES", 60),
            _env_bool("SUMMARY_AI_REQUIRE_PUSH_WRITER_READY", True),
            _env_bool("SUMMARY_AI_REQUIRE_FRESH_PUSH_1M", True),
            _env_bool("SUMMARY_AI_FRESH_CHECK_ALLOW_RAW_DB_FALLBACK", True),
            _env_bool("SUMMARY_AI_SCORE_BRIDGE_ENABLED", True),
            _env_bool("SUMMARY_AI_BOOTSTRAP_SCORE_FROM_OHLC", True),
            _env_bool("SUMMARY_AI_INPUT_PREFER_SCORED_SUMMARY", True),
        )
        return True
    except Exception:
        logger.exception("[SUMMARY AI SAFETY GUARD] install failed")
        return False


def _install_stale_fallback_guard() -> bool:
    try:
        import pandas as pd
        import scheduler_jobs.summary.fallback_loader as fl
        cur = getattr(fl, "select_best_candidate", None)
        if not callable(cur):
            return False
        if getattr(cur, "_summary_stale_fallback_guard_v1", False):
            return True

        @wraps(cur)
        def patched_select_best_candidate(*args: Any, **kwargs: Any):
            out = cur(*args, **kwargs)
            try:
                if out is None or getattr(out, "empty", True):
                    return out
                if bool(kwargs.get("for_ranking", False)):
                    return out
                ok, age, latest, rows = _df_latest_age_sec(out)
                max_age = _env_float("SUMMARY_STALE_DB_FALLBACK_MAX_AGE_SEC", 420.0)
                if ok and age is not None and age > max_age:
                    logger.warning("[SUMMARY FALLBACK STALE GUARD] dropped stale fallback rows=%s latest=%s age=%.1f max=%.1f", rows, latest, age, max_age)
                    return pd.DataFrame()
            except Exception:
                logger.exception("[SUMMARY FALLBACK STALE GUARD] check failed")
            return out

        patched_select_best_candidate._summary_stale_fallback_guard_v1 = True  # type: ignore[attr-defined]
        patched_select_best_candidate._original = cur  # type: ignore[attr-defined]
        fl.select_best_candidate = patched_select_best_candidate
        logger.warning("[SUMMARY FALLBACK STALE GUARD] installed max_age=%s", _env_float("SUMMARY_STALE_DB_FALLBACK_MAX_AGE_SEC", 420.0))
        return True
    except Exception:
        logger.exception("[SUMMARY FALLBACK STALE GUARD] install failed")
        return False


def _install_tonosama_failopen_guard() -> bool:
    # Keep the previous public install contract, but do not add extra TONOSAMA filtering here.
    return True


def install() -> bool:
    global _INSTALLED
    if not _env_bool("SUMMARY_AI_CANDIDATE_REFILL_ENABLED", True):
        logger.warning("[SUMMARY AI SAFETY GUARD] disabled by env")
        return False
    os.environ.setdefault("SUMMARY_AI_REQUIRE_PUSH_WRITER_READY", "1")
    os.environ.setdefault("SUMMARY_AI_REQUIRE_FRESH_PUSH_1M", "1")
    os.environ.setdefault("SUMMARY_AI_MAX_PUSH_1M_AGE_SEC", "120")
    os.environ.setdefault("SUMMARY_AI_SCORED_SUMMARY_MAX_AGE_SEC", os.environ.get("SUMMARY_AI_MAX_PUSH_1M_AGE_SEC", "120"))
    os.environ.setdefault("SUMMARY_AI_INPUT_PREFER_SCORED_SUMMARY", "1")
    os.environ.setdefault("SUMMARY_AI_FRESH_CHECK_ALLOW_RAW_DB_FALLBACK", "1")
    os.environ.setdefault("SUMMARY_AI_WRITER_CHECK_ALLOW_FRESH_RAW_DB", "1")
    os.environ.setdefault("SUMMARY_AI_SCORE_BRIDGE_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_BOOTSTRAP_SCORE_FROM_OHLC", "1")
    os.environ.setdefault("SUMMARY_AI_BOOTSTRAP_MIN_RANGE_PCT", "0.0015")
    os.environ.setdefault("SUMMARY_STALE_DB_FALLBACK_MAX_AGE_SEC", "420")
    ok_ai = _install_summary_ai_guard()
    ok_fb = _install_stale_fallback_guard()
    ok_tono = _install_tonosama_failopen_guard()
    _INSTALLED = bool(ok_ai or ok_fb or ok_tono)
    logger.warning("[SUMMARY AI SAFETY GUARD] install done ai=%s fallback=%s tonosama=%s version=%s", ok_ai, ok_fb, ok_tono, VERSION)
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI SAFETY GUARD] auto install failed")


__all__ = ["VERSION", "install"]
