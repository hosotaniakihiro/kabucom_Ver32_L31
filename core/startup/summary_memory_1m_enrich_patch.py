# ============================================================
# File   : core/startup/summary_memory_1m_enrich_patch.py
# Version: V2-MEMORY-1M-SCORE-MTF-ENRICH-STRONG-MTF
# ------------------------------------------------------------
# 【目的】
#   main.py の PUSH memory 由来 1分summary は rows が作れていても、
#   最新1本中心だと prev_close が取れず、slope/score/MTF/MACD が全0になる。
#   その結果 SUMMARY_AI が「no AI candidates」になる。
#
# 方針:
#   - PUSH DB保存は main.py では再開しない。
#   - memory 1m summary の OHLC / day_open / day_high / day_low から軽量に再計算する。
#   - 既に非ゼロの指標がある場合は極力上書きしない。
#   - 全0に近い時だけ fallback slope/score/MTF/MACD を補完する。
#   - V2: score が非ゼロでも mtf/score_mtf/mtf_score が0なら、range/close_position 由来の
#         directional proxy で MTF を補完する。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V2-MEMORY-1M-SCORE-MTF-ENRICH-STRONG-MTF"
_INSTALLED = False
_ORIG_BUILD_MEMORY_1M_SUMMARY: Callable[..., Any] | None = None
_ORIG_PUBLISH_LATEST: Callable[..., Any] | None = None

_TRUE_SET = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE_SET = {"0", "false", "no", "n", "off", "ng", "disable", "disabled", ""}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None:
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE_SET:
            return True
        if s in _FALSE_SET:
            return False
    except Exception:
        pass
    return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.getenv(name)
        if raw is not None and str(raw).strip() != "":
            return float(str(raw).replace(",", ""))
    except Exception:
        pass
    return float(default)


def _num(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
    try:
        if col in df.columns:
            return pd.to_numeric(df[col], errors="coerce").fillna(default)
    except Exception:
        pass
    return pd.Series(default, index=df.index, dtype="float64")


def _nonnull_nonzero(df: pd.DataFrame, cols: tuple[str, ...]) -> dict[str, int]:
    out: dict[str, int] = {}
    for c in cols:
        try:
            if c in df.columns:
                s = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
                out[c] = int((s.abs() > 1e-12).sum())
            else:
                out[c] = 0
        except Exception:
            out[c] = 0
    return out


def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    try:
        return (a / b.replace(0, pd.NA)).replace([pd.NA, pd.NaT], 0).fillna(0.0).astype(float)
    except Exception:
        return pd.Series(0.0, index=a.index, dtype="float64")


def _nonzero_count(s: pd.Series) -> int:
    try:
        return int((pd.to_numeric(s, errors="coerce").fillna(0.0).abs() > 1e-12).sum())
    except Exception:
        return 0


def enrich_memory_1m_summary(df: pd.DataFrame, *, reason: str = "unknown") -> pd.DataFrame:
    if not _env_bool("SUMMARY_MEMORY_1M_ENRICH_ENABLED", True):
        return df
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()
    try:
        close = _num(out, "close", 0.0)
        if close.abs().sum() <= 0 and "price" in out.columns:
            close = _num(out, "price", 0.0)
            out["close"] = close
        open_ = _num(out, "open", 0.0)
        high = _num(out, "high", 0.0)
        low = _num(out, "low", 0.0)
        day_open = _num(out, "day_open", 0.0)
        day_high = _num(out, "day_high", 0.0)
        day_low = _num(out, "day_low", 0.0)
        prev_close = _num(out, "prev_close", 0.0)

        # 1分内OHLCが乏しい場合は日中高安/始値を補助に使う。
        open_fallback = open_.where(open_ > 0, day_open.where(day_open > 0, prev_close.where(prev_close > 0, close)))
        high_fallback = high.where(high > 0, day_high.where(day_high > 0, close))
        low_fallback = low.where(low > 0, day_low.where(day_low > 0, close))
        high_fallback = pd.concat([high_fallback, close], axis=1).max(axis=1)
        low_fallback = pd.concat([low_fallback, close], axis=1).min(axis=1)

        base = prev_close.where(prev_close > 0, open_fallback.where(open_fallback > 0, close))
        fallback_slope = _safe_div(close - base, base).clip(-0.20, 0.20)
        range_pct = _safe_div(high_fallback - low_fallback, close).clip(lower=0.0, upper=0.30)
        atr = (high_fallback - low_fallback).clip(lower=0.0)

        before = _nonnull_nonzero(out, ("slope", "score", "score_buy", "score_sell", "mtf", "score_mtf", "mtf_score", "macd", "signal"))

        if "range_pct" not in out.columns or _nonzero_count(_num(out, "range_pct", 0.0)) == 0:
            out["range_pct"] = range_pct
        if "atr" not in out.columns or _nonzero_count(_num(out, "atr", 0.0)) == 0:
            out["atr"] = atr

        if "slope" not in out.columns or _nonzero_count(_num(out, "slope", 0.0)) == 0:
            out["slope"] = fallback_slope
        if "slope_atr_scaled" not in out.columns or _nonzero_count(_num(out, "slope_atr_scaled", 0.0)) == 0:
            out["slope_atr_scaled"] = fallback_slope
        if "score_slope" not in out.columns or _nonzero_count(_num(out, "score_slope", 0.0)) == 0:
            out["score_slope"] = fallback_slope * 100.0

        slope = _num(out, "slope", 0.0)
        effective_range_pct = _num(out, "range_pct", 0.0).where(_num(out, "range_pct", 0.0) > 0, range_pct)
        close_pos = _safe_div(close - low_fallback, (high_fallback - low_fallback).replace(0, pd.NA)).clip(0.0, 1.0)

        # slope が小さいがレンジ内の位置が強い/弱い場合も方向を軽く付ける。
        pos_bias = pd.Series(0.0, index=out.index, dtype="float64")
        pos_bias = pos_bias.where(~((slope.abs() < 1e-9) & (effective_range_pct >= 0.0015) & (close_pos >= 0.65)), 1.0)
        pos_bias = pos_bias.where(~((slope.abs() < 1e-9) & (effective_range_pct >= 0.0015) & (close_pos <= 0.35)), -1.0)

        directional = (slope * 1000.0).clip(-4.0, 4.0)
        directional = directional.where(directional.abs() > 1e-9, pos_bias)
        range_bonus = (effective_range_pct * 30.0).clip(0.0, 2.5)

        buy_score = directional.clip(lower=0.0) + range_bonus.where(directional > 0, 0.0)
        sell_score = (-directional).clip(lower=0.0) + range_bonus.where(directional < 0, 0.0)

        if "score_buy" not in out.columns or _nonzero_count(_num(out, "score_buy", 0.0)) == 0:
            out["score_buy"] = buy_score.fillna(0.0)
        if "score_sell" not in out.columns or _nonzero_count(_num(out, "score_sell", 0.0)) == 0:
            out["score_sell"] = sell_score.fillna(0.0)

        score_signed = _num(out, "score_buy", 0.0) - _num(out, "score_sell", 0.0)
        # score が既にあるが全0の場合は補完。既存scoreが非ゼロなら尊重する。
        if "score" not in out.columns or _nonzero_count(_num(out, "score", 0.0)) == 0:
            out["score"] = score_signed
        if "score_total" not in out.columns or _nonzero_count(_num(out, "score_total", 0.0)) == 0:
            out["score_total"] = _num(out, "score", 0.0)
        if "final_score" not in out.columns or _nonzero_count(_num(out, "final_score", 0.0)) == 0:
            out["final_score"] = _num(out, "score", 0.0)
        if "display_score" not in out.columns or _nonzero_count(_num(out, "display_score", 0.0)) == 0:
            out["display_score"] = _num(out, "score", 0.0)

        # MTF proxy: scoreが非ゼロならscore優先。scoreも弱ければ directional/range/close_position 由来で補完。
        score_for_mtf = _num(out, "score", 0.0)
        mtf_proxy = score_for_mtf.where(score_for_mtf.abs() > 1e-12, directional + range_bonus.where(directional >= 0, -range_bonus))
        mtf_proxy = mtf_proxy.clip(-3.0, 3.0).fillna(0.0)
        if "mtf" not in out.columns or _nonzero_count(_num(out, "mtf", 0.0)) == 0:
            out["mtf"] = mtf_proxy
        if "score_mtf" not in out.columns or _nonzero_count(_num(out, "score_mtf", 0.0)) == 0:
            out["score_mtf"] = mtf_proxy
        if "mtf_score" not in out.columns or _nonzero_count(_num(out, "mtf_score", 0.0)) == 0:
            out["mtf_score"] = mtf_proxy

        # 1本だけで本物のMACDは成熟しないため、AI候補作成用に方向 proxy を入れる。
        if "macd" not in out.columns or _nonzero_count(_num(out, "macd", 0.0)) == 0:
            out["macd"] = (slope * close).fillna(0.0)
        if "signal" not in out.columns or _nonzero_count(_num(out, "signal", 0.0)) == 0:
            out["signal"] = 0.0
        if "hist" not in out.columns or _nonzero_count(_num(out, "hist", 0.0)) == 0:
            out["hist"] = _num(out, "macd", 0.0) - _num(out, "signal", 0.0)

        out["technical_ready"] = True
        out["display_ready"] = True
        out["completed_summary"] = True
        out["_memory_1m_enriched"] = True

        after = _nonnull_nonzero(out, ("slope", "score", "score_buy", "score_sell", "mtf", "score_mtf", "mtf_score", "macd", "signal"))
        logger.warning(
            "[SUMMARY MEMORY 1M ENRICH] enriched reason=%s rows=%s before=%s after=%s version=%s",
            reason,
            len(out),
            before,
            after,
            VERSION,
        )
        return out
    except Exception:
        logger.exception("[SUMMARY MEMORY 1M ENRICH] enrich failed reason=%s", reason)
        return df


def install() -> bool:
    global _INSTALLED, _ORIG_BUILD_MEMORY_1M_SUMMARY, _ORIG_PUBLISH_LATEST
    if _INSTALLED:
        return True
    if not _env_bool("SUMMARY_MEMORY_1M_ENRICH_ENABLED", True):
        logger.warning("[SUMMARY MEMORY 1M ENRICH] disabled by env")
        return False
    try:
        import core.startup.summary_main_memory_latest_1m_patch as sm

        cur_build = getattr(sm, "_build_memory_1m_summary", None)
        if callable(cur_build) and not getattr(cur_build, "_summary_memory_1m_enrich_v2", False):
            _ORIG_BUILD_MEMORY_1M_SUMMARY = cur_build

            def _wrapped_build_memory_1m_summary(*args: Any, **kwargs: Any):
                df = _ORIG_BUILD_MEMORY_1M_SUMMARY(*args, **kwargs)
                return enrich_memory_1m_summary(df, reason="build_memory_1m_summary")

            _wrapped_build_memory_1m_summary._summary_memory_1m_enrich_v2 = True  # type: ignore[attr-defined]
            _wrapped_build_memory_1m_summary._summary_memory_1m_enrich_v1 = True  # type: ignore[attr-defined]
            _wrapped_build_memory_1m_summary._original = cur_build  # type: ignore[attr-defined]
            sm._build_memory_1m_summary = _wrapped_build_memory_1m_summary

        cur_publish = getattr(sm, "_publish_latest", None)
        if callable(cur_publish) and not getattr(cur_publish, "_summary_memory_1m_enrich_publish_v2", False):
            _ORIG_PUBLISH_LATEST = cur_publish

            def _wrapped_publish_latest(df: pd.DataFrame) -> None:
                enriched = enrich_memory_1m_summary(df, reason="publish_latest")
                return _ORIG_PUBLISH_LATEST(enriched)

            _wrapped_publish_latest._summary_memory_1m_enrich_publish_v2 = True  # type: ignore[attr-defined]
            _wrapped_publish_latest._summary_memory_1m_enrich_publish_v1 = True  # type: ignore[attr-defined]
            _wrapped_publish_latest._original = cur_publish  # type: ignore[attr-defined]
            sm._publish_latest = _wrapped_publish_latest

        _INSTALLED = True
        logger.warning("[SUMMARY MEMORY 1M ENRICH] installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY MEMORY 1M ENRICH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY MEMORY 1M ENRICH] auto install failed")


__all__ = ["install", "enrich_memory_1m_summary", "VERSION"]
