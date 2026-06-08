# ============================================================
# File   : core/startup/ranking_entry_push_fallback_patch.py
# Version: V1-PUSH-SUMMARY-FALLBACK-WHEN-RANKING-EMPTY
# ------------------------------------------------------------
# 目的:
#   ranking20260608.db が空で ranking entry が
#     [RANKING ENTRY BUDGET] skip reason=no_ranking_df
#   になる場合でも、PUSH 1min merged summary からランキング風候補を作り、
#   entry_from_ranking の既存 pending 生成ルートへ流す。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIG_GET_RANKING_SOURCE_DF = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return int(default)
        return int(float(raw))
    except Exception:
        return int(default)


def _num(s: pd.Series | Any, default: float = 0.0):
    try:
        return pd.to_numeric(s, errors="coerce").fillna(default)
    except Exception:
        return default


def _latest_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out.sort_values(["symbol", "datetime"], kind="stable").groupby("symbol", as_index=False).tail(1)
    else:
        out = out.drop_duplicates(subset=["symbol"], keep="last")
    return out


def _get_push_summary_df() -> pd.DataFrame:
    candidates = []
    try:
        from global_state import global_data
        for fn_name in ("get_push_merged_summary", "get_merged_summary", "get_multi_summary", "get_summary"):
            try:
                fn = getattr(global_data, fn_name, None)
                if not callable(fn):
                    continue
                if fn_name in {"get_merged_summary", "get_multi_summary"}:
                    df = fn(1, source="push")
                else:
                    df = fn(1)
                if isinstance(df, pd.DataFrame) and not df.empty:
                    candidates.append((fn_name, df))
            except TypeError:
                try:
                    df = fn(1)
                    if isinstance(df, pd.DataFrame) and not df.empty:
                        candidates.append((fn_name, df))
                except Exception:
                    pass
            except Exception:
                pass
    except Exception:
        pass

    try:
        from core.global_context.context import global_context as GC
        for call in (
            lambda: GC.get_merged_summary(1, source="push"),
            lambda: GC.get_push_merged_summary(1),
            lambda: GC.summary.to_dataframe(1),
        ):
            try:
                df = call()
                if isinstance(df, pd.DataFrame) and not df.empty:
                    candidates.append(("GC", df))
            except Exception:
                pass
    except Exception:
        pass

    if not candidates:
        return pd.DataFrame()
    name, df = max(candidates, key=lambda x: len(x[1]))
    latest = _latest_per_symbol(df)
    logger.warning("[RANKING PUSH FALLBACK] source=%s rows=%s latest_rows=%s", name, len(df), len(latest))
    return latest


def _build_ranking_like_from_push(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "symbol" not in df.columns:
        return pd.DataFrame()

    out = df.copy()
    out["symbol"] = out["symbol"].astype(str).str.strip()
    out = out[out["symbol"] != ""]
    if out.empty:
        return pd.DataFrame()

    price = _num(out.get("price", out.get("close", out.get("current_price", 0.0))), 0.0)
    close = _num(out.get("close", price), 0.0)
    open_ = _num(out.get("open", out.get("open_price", close)), close)
    volume = _num(out.get("volume", 0.0), 0.0)
    turnover = _num(out.get("turnover", price * volume), 0.0)
    if isinstance(turnover, (int, float)):
        turnover = price * volume
    score_buy = _num(out.get("score_buy", out.get("buy_score", 0.0)), 0.0)
    score_sell = _num(out.get("score_sell", out.get("sell_score", 0.0)), 0.0)
    score_total = _num(out.get("score_total", out.get("final_score", out.get("score", 0.0))), 0.0)
    slope = _num(out.get("slope", 0.0), 0.0)
    mtf = _num(out.get("mtf", out.get("score_mtf", out.get("mtf_score", 0.0))), 0.0)

    day_change_pct = ((close - open_) / open_.replace(0, pd.NA) * 100.0).fillna(0.0)
    side = pd.Series("BUY", index=out.index)
    side = side.mask((score_sell > score_buy) | ((score_buy <= 0) & (slope < 0)), "SELL")

    strength = pd.concat([
        score_buy.abs(),
        score_sell.abs(),
        score_total.abs(),
        slope.abs() * 1000.0,
        mtf.abs(),
    ], axis=1).max(axis=1).fillna(0.0)
    out["_strength"] = strength
    out = out.sort_values("_strength", ascending=False, kind="stable")
    max_rows = max(10, _env_int("RANKING_PUSH_FALLBACK_MAX_ROWS", 80))
    out = out.head(max_rows).copy()

    out["price"] = price.loc[out.index].replace(0, close.loc[out.index])
    out["current_price"] = out["price"]
    out["volume"] = volume.loc[out.index]
    out["turnover"] = turnover.loc[out.index]
    out["trading_value"] = out["turnover"]
    out["day_change_pct"] = day_change_pct.loc[out.index]
    out["side"] = side.loc[out.index]
    out["rank_position"] = range(1, len(out) + 1)
    out["rank"] = out["rank_position"]
    out["rank_type"] = out["side"].map({"BUY": "売買代金急増", "SELL": "値下がり率"}).fillna("売買代金急増")
    out["ranking_source"] = "push_summary_fallback"
    out["ranking_name"] = out.get("symbolname", out["symbol"])

    logger.warning(
        "[RANKING PUSH FALLBACK] built ranking-like df rows=%s buy=%s sell=%s symbols=%s",
        len(out),
        int((out["side"] == "BUY").sum()),
        int((out["side"] == "SELL").sum()),
        out["symbol"].nunique(),
    )
    return out


def _patched_get_ranking_source_df(*args: Any, **kwargs: Any):
    df = None
    try:
        if callable(_ORIG_GET_RANKING_SOURCE_DF):
            df = _ORIG_GET_RANKING_SOURCE_DF(*args, **kwargs)
    except Exception:
        logger.exception("[RANKING PUSH FALLBACK] original _get_ranking_source_df failed")
        df = None

    if isinstance(df, pd.DataFrame) and not df.empty:
        return df

    if not _env_bool("RANKING_ENTRY_PUSH_SUMMARY_FALLBACK_ENABLED", True):
        return df

    push_df = _get_push_summary_df()
    fb = _build_ranking_like_from_push(push_df)
    if fb.empty:
        logger.warning("[RANKING PUSH FALLBACK] fallback empty; original_empty=%s", df is None or (isinstance(df, pd.DataFrame) and df.empty))
        return df
    return fb


def install() -> bool:
    global _PATCHED, _ORIG_GET_RANKING_SOURCE_DF
    if _PATCHED:
        return True
    try:
        import trading.ranking.entry_from_ranking as efr
        cur = getattr(efr, "_get_ranking_source_df", None)
        if not callable(cur):
            logger.warning("[RANKING PUSH FALLBACK] target missing")
            return False
        if getattr(cur, "_ranking_push_fallback_v1", False):
            _PATCHED = True
            return True
        _ORIG_GET_RANKING_SOURCE_DF = cur
        _patched_get_ranking_source_df._ranking_push_fallback_v1 = True  # type: ignore[attr-defined]
        _patched_get_ranking_source_df._original = cur  # type: ignore[attr-defined]
        efr._get_ranking_source_df = _patched_get_ranking_source_df
        _PATCHED = True
        logger.warning("[RANKING PUSH FALLBACK] installed enabled=%s max_rows=%s", _env_bool("RANKING_ENTRY_PUSH_SUMMARY_FALLBACK_ENABLED", True), _env_int("RANKING_PUSH_FALLBACK_MAX_ROWS", 80))
        return True
    except Exception:
        logger.exception("[RANKING PUSH FALLBACK] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING PUSH FALLBACK] auto install failed")

__all__ = ["install"]
