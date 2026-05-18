# ============================================================
# File   : core/startup/entry_qty_min_lot_runtime_patch.py
# Version: Ver03-ENTRY-QTY-MINLOT-SUMMARY-AI-THRESHOLDS-PIPE-DISPLAY
# ------------------------------------------------------------
# - entry_controller の数量0株を最低100株へ戻す最終防衛
# - SUMMARY AI hook の候補抽出閾値を環境変数で緩和
# - MERGED GET系表示を | 区切りにして score 列ずれを防ぐ
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIGINAL = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except Exception:
        return default


def _install_summary_ai_threshold_env() -> bool:
    try:
        defaults = {
            "SUMMARY_AI_MIN_BUY": "1.00",
            "SUMMARY_AI_MIN_SELL": "1.00",
            "SUMMARY_AI_MIN_CONF": "0.55",
            "MIN_ENTRY_SCORE_BUY_SUMMARY": "1.00",
            "MIN_ENTRY_SCORE_SELL_SUMMARY": "1.00",
            "MIN_SUMMARY_SCORE_BUY": "1.00",
            "MIN_SUMMARY_SCORE_SELL": "1.00",
            "MIN_COMPOSITE_SCORE_BUY": "0.50",
            "MIN_COMPOSITE_SCORE_SELL": "0.80",
            "SUMMARY_AI_TOP_N": "20",
        }
        for k, v in defaults.items():
            os.environ.setdefault(k, v)
        logger.warning("[SUMMARY AI THRESHOLD ENV PATCH] installed %s", defaults)
        return True
    except Exception:
        logger.exception("[SUMMARY AI THRESHOLD ENV PATCH] install failed")
        return False


def _get_budget() -> float:
    try:
        from trading.entry.entry_budget import get_max_entry_oneshot_yen
        v = float(get_max_entry_oneshot_yen())
        if v > 0:
            return v
    except Exception:
        pass
    return _safe_float(os.getenv("MAX_ENTRY_ONESHOT_YEN"), 700000.0)


def _get_lot() -> int:
    try:
        from trading.entry.entry_budget import get_order_lot_size
        v = int(get_order_lot_size())
        if v > 0:
            return v
    except Exception:
        pass
    return _safe_int(os.getenv("ORDER_LOT_SIZE"), 100)


def _price_range_ok(price: float) -> bool:
    try:
        from trading.entry.entry_budget import can_afford_min_lot
        ok, diag = can_afford_min_lot(price)
        if not ok:
            logger.warning("[ENTRY QTY MINLOT PATCH] affordability NG price=%s diag=%s", price, diag)
        return bool(ok)
    except Exception:
        min_price = _safe_float(os.getenv("ENTRY_MIN_PRICE"), 3000.0)
        max_price = _safe_float(os.getenv("ENTRY_MAX_PRICE"), 7000.0)
        return bool(price >= min_price and price <= max_price)


def _patched_calculate_entry_quantity(*, symbol: str, price: float, confidence: float, lot_multiplier: float, atr: Any = None) -> int:
    global _ORIGINAL
    qty = 0
    try:
        if callable(_ORIGINAL):
            qty = int(_ORIGINAL(symbol=symbol, price=price, confidence=confidence, lot_multiplier=lot_multiplier, atr=atr))
    except Exception:
        logger.exception("[ENTRY QTY MINLOT PATCH] original lot_sizer failed symbol=%s", symbol)
        qty = 0

    if qty > 0:
        return qty

    if not _env_bool("ENTRY_MIN_LOT_FALLBACK_WHEN_AFFORDABLE", True):
        return 0

    p = _safe_float(price, 0.0)
    if p <= 0:
        return 0

    budget = _get_budget()
    lot = _get_lot()
    if lot <= 0:
        lot = 100

    if not _price_range_ok(p):
        return 0

    max_qty = int((budget // p) // lot * lot)
    if max_qty < lot:
        logger.warning(
            "[ENTRY QTY MINLOT PATCH] cannot afford min lot symbol=%s price=%.1f budget=%.0f lot=%s max_qty=%s",
            symbol, p, budget, lot, max_qty,
        )
        return 0

    logger.warning(
        "[ENTRY QTY MINLOT PATCH] qty 0 -> min lot fallback symbol=%s price=%.1f qty=%s budget=%.0f lot=%s confidence=%s multiplier=%s atr=%s",
        symbol, p, lot, budget, lot, confidence, lot_multiplier, atr,
    )
    return int(lot)


def _install_pipe_summary_display_patch() -> bool:
    try:
        import pandas as pd
        import numpy as np
        import unicodedata

        symbolname_width = max(10, min(_safe_int(os.getenv("DISPLAY_SYMBOLNAME_WIDTH"), 18), 32))
        score_cols = {"score", "score_total", "final_score", "display_score", "score_buy", "score_sell"}
        slope_cols = {"slope", "slope_atr_scaled", "score_slope"}
        two_cols = {"mtf", "score_mtf", "mtf_score", "macd", "signal"}
        one_cols = {"open", "high", "low", "close", "rsi"}
        widths = {
            "symbol": 6, "symbolname": symbolname_width,
            "score": 7, "score_total": 7, "final_score": 7, "display_score": 7,
            "score_buy": 7, "score_sell": 7,
            "slope": 8, "slope_atr_scaled": 8, "score_slope": 8,
            "mtf": 6, "score_mtf": 6, "mtf_score": 6,
            "open": 7, "high": 7, "low": 7, "close": 7,
            "rsi": 5, "macd": 6, "signal": 6, "datetime": 19,
        }
        left_cols = {"symbol", "symbolname", "datetime"}

        def _w(s: str) -> int:
            total = 0
            for ch in str(s):
                total += 2 if unicodedata.east_asian_width(ch) in {"F", "W"} else 1
            return total

        def _pad(s: str, width: int, left: bool = False) -> str:
            s = str(s)
            pad = max(0, width - _w(s))
            return s + " " * pad if left else " " * pad + s

        def _clip(s: str, width: int) -> str:
            s = str(s)
            if _w(s) <= width:
                return s
            ell = "..."
            out = ""
            for ch in s:
                if _w(out + ch + ell) > width:
                    break
                out += ch
            return out + ell

        def _num(v, default=np.nan):
            try:
                if v is None or (isinstance(v, str) and v.strip() == ""):
                    return default
                return float(v)
            except Exception:
                return default

        def _fmt_num(v, width: int, digits: int) -> str:
            x = _num(v)
            if pd.isna(x):
                return _pad("-", width, left=False)
            return f"{x:{width}.{digits}f}"

        def _cell(c: str, v) -> str:
            if c in score_cols:
                return _fmt_num(v, widths.get(c, 7), 2)
            if c in slope_cols:
                return _fmt_num(v, widths.get(c, 8), 4)
            if c in two_cols:
                return _fmt_num(v, widths.get(c, 6), 2)
            if c in one_cols:
                return _fmt_num(v, widths.get(c, 7), 1)
            if c == "symbolname":
                return _pad(_clip(str(v), widths[c]), widths[c], left=True)
            if c == "symbol":
                return _pad(str(v), widths[c], left=True)
            if c == "datetime":
                return _pad(str(v), widths[c], left=True)
            return _pad(str(v), widths.get(c, max(8, len(c))), left=c in left_cols)

        def _table(df) -> str:
            try:
                if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                    return ""
                cols = [str(c) for c in df.columns]
                header = " | ".join(_pad(c, widths.get(c, max(8, len(c))), left=c in left_cols) for c in cols)
                sep = "-+-".join("-" * widths.get(c, max(8, len(c))) for c in cols)
                lines = [header, sep]
                for _, r in df.iterrows():
                    lines.append(" | ".join(_cell(c, r.get(c, "")) for c in cols))
                return "\n".join(lines)
            except Exception:
                try:
                    return df.to_string(index=False)
                except Exception:
                    return ""

        from core.global_context import context as ctx

        def _patched_log_df_profile(prefix, tf, source, df):
            try:
                prof = ctx._profile_df(df)
                ctx.logger.info(
                    "%s tf=%s source=%s rows=%s cols=%s unique_symbols=%s blank_symbolname=%s completed_summary=%s sample_cols=%s",
                    prefix, tf, source, prof.get("rows"), prof.get("cols"), prof.get("unique_symbols"),
                    prof.get("blank_symbolname"), prof.get("completed_summary"), prof.get("sample_cols"),
                )
                ctx.logger.info(
                    "%s tf=%s source=%s nonzero score=%s slope=%s slope_atr_scaled=%s score_slope=%s mtf=%s score_mtf=%s mtf_score=%s rsi=%s macd=%s signal=%s close_nonnull=%s datetime_nonnull=%s",
                    prefix, tf, source, prof.get("score_nonzero"), prof.get("slope_nonzero"),
                    prof.get("slope_atr_scaled_nonzero"), prof.get("score_slope_nonzero"), prof.get("mtf_nonzero"),
                    prof.get("score_mtf_nonzero"), prof.get("mtf_score_nonzero"), prof.get("rsi_nonzero"),
                    prof.get("macd_nonzero"), prof.get("signal_nonzero"), prof.get("close_nonnull"), prof.get("datetime_nonnull"),
                )
                show_cols = [c for c in [
                    "symbol", "symbolname", "score", "score_total", "final_score", "display_score",
                    "score_buy", "score_sell", "slope", "slope_atr_scaled", "score_slope",
                    "mtf", "score_mtf", "mtf_score", "open", "high", "low", "close", "rsi", "macd", "signal", "datetime"
                ] if c in df.columns]
                if show_cols and not df.empty:
                    ctx.logger.info("%s tf=%s source=%s\n%s", prefix, tf, source, _table(df[show_cols].head(20)))
            except Exception:
                ctx.logger.exception("[PIPE SUMMARY DISPLAY PATCH] _log_df_profile failed prefix=%s tf=%s source=%s", prefix, tf, source)

        _patched_log_df_profile._pipe_summary_display_patch_v1 = True
        ctx._log_df_profile = _patched_log_df_profile
        logger.warning("[PIPE SUMMARY DISPLAY PATCH] installed symbolname_width=%s", symbolname_width)
        return True
    except Exception:
        logger.exception("[PIPE SUMMARY DISPLAY PATCH] install failed")
        return False


def install() -> bool:
    global _INSTALLED, _ORIGINAL

    env_ok = _install_summary_ai_threshold_env()
    display_ok = _install_pipe_summary_display_patch()

    if _INSTALLED:
        return bool(env_ok or display_ok or True)
    try:
        import trading.handlers.entry_controller as ec
        old = getattr(ec, "calculate_entry_quantity", None)
        if callable(old) and getattr(old, "_entry_qty_minlot_patch_v1", False):
            _INSTALLED = True
            logger.warning("[ENTRY QTY MINLOT PATCH] already installed")
            return True
        _ORIGINAL = old
        _patched_calculate_entry_quantity._entry_qty_minlot_patch_v1 = True  # type: ignore[attr-defined]
        ec.calculate_entry_quantity = _patched_calculate_entry_quantity
        _INSTALLED = True
        logger.warning("[ENTRY QTY MINLOT PATCH] installed")
        return True
    except Exception:
        logger.exception("[ENTRY QTY MINLOT PATCH] install failed")
        return bool(env_ok or display_ok)


try:
    install()
except Exception:
    logger.exception("[ENTRY QTY MINLOT PATCH] auto install failed")

__all__ = ["install"]
