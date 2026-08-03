# ============================================================
# File   : core/startup/summary_ai_entry_execution_fix_patch.py
# Version: V1-SUMMARY-AI-BLOWOFF-HISTORY-FIX
# ------------------------------------------------------------
# 【目的】
#   SUMMARY_AI で AI_OK / approved まで進んだ後、
#   entry_pipeline の blowoff_top 一律除外により全候補が実注文前に落ちる問題を修正する。
#
#   また main.py memory 由来の 1分 summary は merged_summary_1 には入るが、
#   summary_history_cache[1] が空のままになり、履歴参照側が rows=0 になる問題を補強する。
#
# 方針:
#   - blowoff_top を無効化しない。
#   - BUY の本当に危険な吹き上げ買いだけ止める。
#   - SELL は blowoff_top では止めない。SELL は別の下落/売り専用ガードに任せる。
#   - BUY でも高値から少し押している場合は押し目候補として通す。
#   - 1分 summary publish 時に set_summary_history(1, source="push") も呼ぶ。
#   - get_summary_history(1, source="push") が空なら merged_summary_1 を fallback で返す。
# ============================================================
from __future__ import annotations

import logging
import os
from typing import Any, Callable

import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V1-SUMMARY-AI-BLOWOFF-HISTORY-FIX"
_INSTALLED = False
_ORIG_FILTER_BLOWOFF: Callable[..., Any] | None = None
_ORIG_PUBLISH_LATEST: Callable[..., Any] | None = None
_ORIG_GET_SUMMARY_HISTORY: Callable[..., Any] | None = None

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


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".T"):
            s = s[:-2]
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ""


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if pd.isna(x):
            return float(default)
        return x
    except Exception:
        return float(default)


def _row_to_dict(row: Any) -> dict[str, Any]:
    try:
        if isinstance(row, dict):
            return dict(row)
        if isinstance(row, pd.Series):
            return row.to_dict()
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return dict(d)
    except Exception:
        pass
    return {}


def _first(d: dict[str, Any], names: tuple[str, ...], default: Any = None) -> Any:
    for name in names:
        try:
            v = d.get(name)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


def _side_from_row(d: dict[str, Any]) -> str:
    side = str(_first(d, ("side", "entry_decision", "ai_side", "decision"), "") or "").strip().upper()
    if side in {"BUY", "SELL"}:
        return side
    score = _safe_float(_first(d, ("score", "score_total", "final_score", "display_score"), 0.0), 0.0)
    buy = _safe_float(_first(d, ("score_buy", "buy_score"), 0.0), 0.0)
    sell = _safe_float(_first(d, ("score_sell", "sell_score"), 0.0), 0.0)
    if sell > buy and sell > 0:
        return "SELL"
    if buy > sell and buy > 0:
        return "BUY"
    if score < 0:
        return "SELL"
    if score > 0:
        return "BUY"
    return ""


def _symbol_from_row(row: Any) -> str:
    d = _row_to_dict(row)
    return _norm_symbol(_first(d, ("symbol", "Symbol", "code", "銘柄コード"), ""))


def _should_skip_blowoff_top(row: Any, *, symbol: str) -> tuple[bool, dict[str, Any]]:
    """
    True のときだけ blowoff_top で除外する。

    旧処理は detect_blowoff_top に出た銘柄を BUY/SELL/押し目問わず除外していた。
    ここではユーザー方針に合わせ、上昇出来高増の買いクライマックスだけ止める。
    """
    d = _row_to_dict(row)
    side = _side_from_row(d)
    if side != "BUY":
        return False, {"side": side, "reason": "not_buy"}

    close = _safe_float(_first(d, ("close", "close_price", "price", "current_price"), 0.0), 0.0)
    high = _safe_float(_first(d, ("high", "high_price", "day_high", "HighPrice"), 0.0), 0.0)
    rsi = _safe_float(_first(d, ("rsi", "rsi_1m", "display_rsi"), 50.0), 50.0)
    slope = _safe_float(_first(d, ("slope", "slope_atr_scaled", "score_slope", "recent_slope"), 0.0), 0.0)
    score_buy = _safe_float(_first(d, ("score_buy", "buy_score", "score", "score_total"), 0.0), 0.0)

    if close <= 0 or high <= 0:
        # 判定材料が足りない場合は、過剰除外を避ける。
        return False, {"side": side, "reason": "missing_price", "close": close, "high": high}

    max_gap_pct = max(0.0, _env_float("SUMMARY_AI_BLOWOFF_TOP_MAX_HIGH_GAP_PCT", 0.15))
    min_slope = _env_float("SUMMARY_AI_BLOWOFF_TOP_MIN_SLOPE", 0.006)
    min_score_buy = _env_float("SUMMARY_AI_BLOWOFF_TOP_MIN_SCORE_BUY", 3.0)
    min_rsi = _env_float("SUMMARY_AI_BLOWOFF_TOP_MIN_RSI", 90.0)

    high_gap_pct = ((high - close) / high * 100.0) if high > 0 else 999.0
    near_high = high_gap_pct <= max_gap_pct
    strong_up = slope >= min_slope or score_buy >= min_score_buy
    hot_rsi = rsi >= min_rsi

    skip = bool(near_high and (strong_up or hot_rsi))
    return skip, {
        "side": side,
        "close": close,
        "high": high,
        "high_gap_pct": round(high_gap_pct, 4),
        "near_high": near_high,
        "slope": slope,
        "score_buy": score_buy,
        "rsi": rsi,
        "strong_up": strong_up,
        "hot_rsi": hot_rsi,
        "thresholds": {
            "max_high_gap_pct": max_gap_pct,
            "min_slope": min_slope,
            "min_score_buy": min_score_buy,
            "min_rsi": min_rsi,
        },
    }


def _install_selective_blowoff_filter() -> bool:
    global _ORIG_FILTER_BLOWOFF
    if not _env_bool("SUMMARY_AI_SELECTIVE_BLOWOFF_FILTER_ENABLED", True):
        logger.warning("[SUMMARY AI EXEC FIX] selective blowoff disabled by env")
        return False
    try:
        import trading.summary.pipeline.entry_pipeline as ep

        cur = getattr(ep, "_filter_blowoff", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI EXEC FIX] entry_pipeline._filter_blowoff not callable")
            return False
        if getattr(cur, "_summary_ai_selective_blowoff_v1", False):
            return True
        _ORIG_FILTER_BLOWOFF = cur

        def _patched_filter_blowoff(rows, df_summary):
            try:
                if df_summary is None or not isinstance(df_summary, pd.DataFrame) or df_summary.empty:
                    return rows
                try:
                    tops = ep.detect_blowoff_top(df_summary)
                except Exception:
                    logger.exception("[SUMMARY AI EXEC FIX] detect_blowoff_top failed -> fail open")
                    return rows
                if tops is None or not isinstance(tops, pd.DataFrame) or tops.empty or "symbol" not in tops.columns:
                    return rows

                top_symbols = {_norm_symbol(x) for x in tops["symbol"].astype(str).tolist() if _norm_symbol(x)}
                if not top_symbols:
                    return rows

                filtered = []
                skipped = []
                allowed = []
                for r in rows or []:
                    symbol = _symbol_from_row(r)
                    if symbol not in top_symbols:
                        filtered.append(r)
                        continue
                    skip, detail = _should_skip_blowoff_top(r, symbol=symbol)
                    if skip:
                        skipped.append({"symbol": symbol, **detail})
                        logger.info("[entry_pipeline] skip blowoff top symbol=%s selective=True detail=%s", symbol, detail)
                        continue
                    allowed.append({"symbol": symbol, **detail})
                    logger.warning("[entry_pipeline] blowoff top relaxed allow symbol=%s detail=%s", symbol, detail)
                    filtered.append(r)

                if skipped or allowed:
                    logger.warning(
                        "[SUMMARY AI EXEC FIX] selective blowoff before=%s after=%s skipped=%s allowed=%s detected=%s",
                        len(rows or []),
                        len(filtered),
                        skipped[:20],
                        allowed[:20],
                        len(top_symbols),
                    )
                return filtered
            except Exception:
                logger.exception("[SUMMARY AI EXEC FIX] selective blowoff failed -> original fallback")
                try:
                    return _ORIG_FILTER_BLOWOFF(rows, df_summary) if callable(_ORIG_FILTER_BLOWOFF) else rows
                except Exception:
                    return rows

        _patched_filter_blowoff._summary_ai_selective_blowoff_v1 = True  # type: ignore[attr-defined]
        _patched_filter_blowoff._original = cur  # type: ignore[attr-defined]
        ep._filter_blowoff = _patched_filter_blowoff
        logger.warning("[SUMMARY AI EXEC FIX] selective blowoff filter installed version=%s", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY AI EXEC FIX] selective blowoff install failed")
        return False


def _publish_summary_history_1m(df: pd.DataFrame, *, reason: str) -> bool:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return False
    try:
        from core.global_context.context import global_context as GC

        fn = getattr(GC, "set_summary_history", None)
        if callable(fn):
            try:
                fn(1, df, source="push")
            except TypeError:
                try:
                    fn(tf=1, df=df, source="push")
                except TypeError:
                    fn(1, df)
            logger.warning(
                "[SUMMARY AI EXEC FIX] tf=1 summary history published reason=%s rows=%s symbols=%s latest=%s",
                reason,
                len(df),
                int(df["symbol"].nunique()) if "symbol" in df.columns else 0,
                pd.to_datetime(df["datetime"], errors="coerce").max() if "datetime" in df.columns else None,
            )
            return True
    except Exception:
        logger.exception("[SUMMARY AI EXEC FIX] publish summary history 1m failed reason=%s", reason)
    return False


def _install_memory_1m_history_publish() -> bool:
    global _ORIG_PUBLISH_LATEST
    try:
        import core.startup.summary_main_memory_latest_1m_patch as sm

        cur = getattr(sm, "_publish_latest", None)
        if not callable(cur):
            logger.warning("[SUMMARY AI EXEC FIX] summary_main_memory _publish_latest not callable")
            return False
        if getattr(cur, "_summary_ai_history_publish_v1", False):
            return True
        _ORIG_PUBLISH_LATEST = cur

        def _wrapped_publish_latest(df: pd.DataFrame) -> None:
            if callable(_ORIG_PUBLISH_LATEST):
                _ORIG_PUBLISH_LATEST(df)
            _publish_summary_history_1m(df, reason="memory_publish_latest")

        _wrapped_publish_latest._summary_ai_history_publish_v1 = True  # type: ignore[attr-defined]
        _wrapped_publish_latest._original = cur  # type: ignore[attr-defined]
        sm._publish_latest = _wrapped_publish_latest
        logger.warning("[SUMMARY AI EXEC FIX] memory 1m summary history publish wrapper installed")
        return True
    except Exception:
        logger.exception("[SUMMARY AI EXEC FIX] memory history publish install failed")
        return False


def _install_history_get_fallback() -> bool:
    global _ORIG_GET_SUMMARY_HISTORY
    try:
        from core.global_context.context import global_context as GC

        cur = getattr(GC, "get_summary_history", None)
        if not callable(cur):
            return False
        if getattr(cur, "_summary_ai_history_fallback_v1", False):
            return True
        _ORIG_GET_SUMMARY_HISTORY = cur

        def _patched_get_summary_history(tf: Any, source: str = "push"):
            try:
                df = _ORIG_GET_SUMMARY_HISTORY(tf, source=source) if callable(_ORIG_GET_SUMMARY_HISTORY) else pd.DataFrame()
            except TypeError:
                df = _ORIG_GET_SUMMARY_HISTORY(tf) if callable(_ORIG_GET_SUMMARY_HISTORY) else pd.DataFrame()
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df
            try:
                tf_i = int(float(str(tf).replace("min", "").replace("m", "")))
            except Exception:
                tf_i = tf
            if tf_i == 1 and str(source or "push").lower() == "push":
                try:
                    merged = GC.get_merged_summary(1, source="push")
                except TypeError:
                    merged = GC.get_merged_summary(1)
                if isinstance(merged, pd.DataFrame) and not merged.empty:
                    logger.warning(
                        "[SUMMARY AI EXEC FIX] get_summary_history fallback tf=1 source=push rows=%s",
                        len(merged),
                    )
                    _publish_summary_history_1m(merged, reason="get_history_fallback")
                    return merged.copy()
            return df if isinstance(df, pd.DataFrame) else pd.DataFrame()

        _patched_get_summary_history._summary_ai_history_fallback_v1 = True  # type: ignore[attr-defined]
        _patched_get_summary_history._original = cur  # type: ignore[attr-defined]
        GC.get_summary_history = _patched_get_summary_history
        logger.warning("[SUMMARY AI EXEC FIX] summary history tf=1 fallback installed")
        return True
    except Exception:
        logger.exception("[SUMMARY AI EXEC FIX] history get fallback install failed")
        return False


def install() -> bool:
    global _INSTALLED
    try:
        ok1 = _install_selective_blowoff_filter()
        ok2 = _install_memory_1m_history_publish()
        ok3 = _install_history_get_fallback()
        _INSTALLED = bool(ok1 or ok2 or ok3)
        logger.warning(
            "[SUMMARY AI EXEC FIX] installed=%s selective_blowoff=%s history_publish=%s history_fallback=%s version=%s",
            _INSTALLED,
            ok1,
            ok2,
            ok3,
            VERSION,
        )
        return _INSTALLED
    except Exception:
        logger.exception("[SUMMARY AI EXEC FIX] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI EXEC FIX] auto install failed")


__all__ = ["install", "VERSION"]
