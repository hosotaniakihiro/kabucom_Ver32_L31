from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
_PATCHED = False


def _install_order_5s_optional() -> bool:
    try:
        import core.startup.entry_order_5s_breakout_optional_patch as p
        fn = getattr(p, "install", None)
        ok = bool(fn()) if callable(fn) else False
        logger.warning("[TONOSAMA 5SEC ADVISORY PATCH] order 5s optional installed=%s", ok)
        return ok
    except Exception:
        logger.exception("[TONOSAMA 5SEC ADVISORY PATCH] order 5s optional install failed")
        return False


def install() -> bool:
    global _PATCHED
    ok_order_5s = _install_order_5s_optional()
    if _PATCHED:
        return True and ok_order_5s
    try:
        import pandas as pd
        import trading.entry.tonosama.runner as r

        old_apply = getattr(r, "_apply_climax_guards", None)
        if callable(old_apply) and getattr(old_apply, "_tonosama_climax_safe_v3", False):
            _PATCHED = True
            logger.warning(
                "[TONOSAMA 5SEC ADVISORY PATCH] installed v3 existing_climax_safe=True order_5s_optional=%s",
                ok_order_5s,
            )
            return True and ok_order_5s

        def _num_series(df: pd.DataFrame, col: str, default: float = 0.0) -> pd.Series:
            try:
                fn = getattr(r, "_num_series", None)
                if callable(fn):
                    return fn(df, col, default)
            except Exception:
                pass
            if df is None or df.empty or col not in df.columns:
                return pd.Series(default, index=df.index if df is not None else None, dtype="float64")
            return pd.to_numeric(df[col], errors="coerce").fillna(default)

        def _log_filter_step(stage: str, before: pd.DataFrame, after: pd.DataFrame, reason: str, threshold: dict, sample_cols: list[str]) -> None:
            try:
                fn = getattr(r, "_log_filter_step", None)
                if callable(fn):
                    fn(stage=stage, before=before, after=after, reason=reason, threshold=threshold, sample_cols=sample_cols)
                    return
            except Exception:
                logger.debug("[TONOSAMA 5SEC ADVISORY PATCH] delegated filter log failed", exc_info=True)
            try:
                logger.info(
                    "[TONOSAMA FILTER PASS] stage=%s reason=%s before=%s after=%s threshold=%s",
                    stage,
                    reason,
                    0 if before is None else len(before),
                    0 if after is None else len(after),
                    threshold,
                )
            except Exception:
                pass

        def _safe_apply_climax_guards(x: pd.DataFrame, *, stage: str, sample_cols: list[str]) -> pd.DataFrame:
            if x is None or x.empty:
                return pd.DataFrame()

            # BUY side: all conditions are vectorized with & and |. Never use Python and/or on Series.
            surge = _num_series(x, "_max_volume_surge_ratio")
            price_chg = _num_series(x, "_max_price_change_pct")
            signed_body = _num_series(x, "_signed_body_change_pct")
            close_pos = _num_series(x, "_close_position_pct", 50.0)
            upper_wick = _num_series(x, "_upper_wick_pct")
            slope = _num_series(x, "_slope")

            buy_like = (price_chg > 0) | (signed_body > 0) | (slope > 0)
            buy_too_late = buy_like & (price_chg >= getattr(r, "MAX_BUY_PRICE_CHANGE_PCT", 999.0))
            buy_high_zone = buy_like & (close_pos >= getattr(r, "MAX_BUY_CLOSE_POSITION_PCT", 85.0)) & (
                price_chg >= getattr(r, "BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT", 0.5)
            )
            buy_upper_wick_reversal = buy_like & (upper_wick >= getattr(r, "MAX_BUY_UPPER_WICK_PCT", 80.0)) & (
                close_pos <= getattr(r, "BUY_REJECTED_CLOSE_POSITION_PCT", 35.0)
            )
            buying_climax = buy_like & (surge >= getattr(r, "BUYING_CLIMAX_MIN_SURGE_RATIO", 3.0)) & (
                ((price_chg >= getattr(r, "BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT", 0.5)) & (close_pos >= getattr(r, "MAX_BUY_CLOSE_POSITION_PCT", 85.0)))
                | ((upper_wick >= getattr(r, "MAX_BUY_UPPER_WICK_PCT", 80.0)) & (close_pos <= getattr(r, "BUY_REJECTED_CLOSE_POSITION_PCT", 35.0)))
            )

            before = x.copy()
            x = x[~(buy_too_late | buy_high_zone | buy_upper_wick_reversal | buying_climax)]
            _log_filter_step(
                stage,
                before,
                x,
                "buying_climax_or_upper_wick_reversal_guard_safe_v3",
                {
                    "MAX_BUY_PRICE_CHANGE_PCT": getattr(r, "MAX_BUY_PRICE_CHANGE_PCT", None),
                    "MAX_BUY_CLOSE_POSITION_PCT": getattr(r, "MAX_BUY_CLOSE_POSITION_PCT", None),
                    "MAX_BUY_UPPER_WICK_PCT": getattr(r, "MAX_BUY_UPPER_WICK_PCT", None),
                    "BUY_REJECTED_CLOSE_POSITION_PCT": getattr(r, "BUY_REJECTED_CLOSE_POSITION_PCT", None),
                    "BUYING_CLIMAX_MIN_SURGE_RATIO": getattr(r, "BUYING_CLIMAX_MIN_SURGE_RATIO", None),
                    "BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT": getattr(r, "BUYING_CLIMAX_MIN_PRICE_CHANGE_PCT", None),
                },
                sample_cols,
            )

            if x.empty:
                return x

            # SELL side: also fully vectorized.
            surge = _num_series(x, "_max_volume_surge_ratio")
            price_chg = _num_series(x, "_max_price_change_pct")
            signed_body = _num_series(x, "_signed_body_change_pct")
            close_pos = _num_series(x, "_close_position_pct", 50.0)
            lower_wick = _num_series(x, "_lower_wick_pct")
            slope = _num_series(x, "_slope")
            drop_abs = price_chg.abs()

            sell_like = (price_chg < 0) | (signed_body < 0) | (slope < 0)
            sell_too_late = sell_like & (drop_abs >= getattr(r, "MAX_SELL_PRICE_DROP_PCT", 999.0))
            sell_low_zone = sell_like & (close_pos <= getattr(r, "MIN_SELL_CLOSE_POSITION_PCT", 15.0)) & (
                drop_abs >= getattr(r, "SELLING_CLIMAX_MIN_PRICE_DROP_PCT", 0.5)
            )
            sell_lower_wick_reversal = sell_like & (lower_wick >= getattr(r, "MAX_SELL_LOWER_WICK_PCT", 80.0)) & (
                close_pos >= getattr(r, "SELL_REJECTED_CLOSE_POSITION_PCT", 65.0)
            )
            selling_climax = sell_like & (surge >= getattr(r, "SELLING_CLIMAX_MIN_SURGE_RATIO", 3.0)) & (
                ((drop_abs >= getattr(r, "SELLING_CLIMAX_MIN_PRICE_DROP_PCT", 0.5)) & (close_pos <= getattr(r, "MIN_SELL_CLOSE_POSITION_PCT", 15.0)))
                | ((lower_wick >= getattr(r, "MAX_SELL_LOWER_WICK_PCT", 80.0)) & (close_pos >= getattr(r, "SELL_REJECTED_CLOSE_POSITION_PCT", 65.0)))
            )

            before = x.copy()
            x = x[~(sell_too_late | sell_low_zone | sell_lower_wick_reversal | selling_climax)]
            _log_filter_step(
                stage,
                before,
                x,
                "selling_climax_or_lower_wick_reversal_guard_safe_v3",
                {
                    "MAX_SELL_PRICE_DROP_PCT": getattr(r, "MAX_SELL_PRICE_DROP_PCT", None),
                    "MIN_SELL_CLOSE_POSITION_PCT": getattr(r, "MIN_SELL_CLOSE_POSITION_PCT", None),
                    "MAX_SELL_LOWER_WICK_PCT": getattr(r, "MAX_SELL_LOWER_WICK_PCT", None),
                    "SELL_REJECTED_CLOSE_POSITION_PCT": getattr(r, "SELL_REJECTED_CLOSE_POSITION_PCT", None),
                    "SELLING_CLIMAX_MIN_SURGE_RATIO": getattr(r, "SELLING_CLIMAX_MIN_SURGE_RATIO", None),
                    "SELLING_CLIMAX_MIN_PRICE_DROP_PCT": getattr(r, "SELLING_CLIMAX_MIN_PRICE_DROP_PCT", None),
                },
                sample_cols,
            )
            return x

        def _patched_apply_climax_guards(x: pd.DataFrame, *, stage: str, sample_cols: list[str]) -> pd.DataFrame:
            if callable(old_apply):
                try:
                    return old_apply(x, stage=stage, sample_cols=sample_cols)
                except ValueError as e:
                    if "truth value of a Series is ambiguous" not in str(e):
                        logger.exception("[TONOSAMA 5SEC ADVISORY PATCH] original climax guard failed unexpected ValueError")
                        return x if x is not None else pd.DataFrame()
                    logger.warning(
                        "[TONOSAMA 5SEC ADVISORY PATCH] original climax guard Series ambiguity -> safe vectorized fallback stage=%s rows=%s",
                        stage,
                        0 if x is None else len(x),
                    )
                    return _safe_apply_climax_guards(x, stage=stage, sample_cols=sample_cols)
                except Exception:
                    logger.exception("[TONOSAMA 5SEC ADVISORY PATCH] original climax guard failed")
                    return x if x is not None else pd.DataFrame()
            return _safe_apply_climax_guards(x, stage=stage, sample_cols=sample_cols)

        _patched_apply_climax_guards._tonosama_climax_safe_v3 = True  # type: ignore[attr-defined]
        _patched_apply_climax_guards._original = old_apply  # type: ignore[attr-defined]
        r._apply_climax_guards = _patched_apply_climax_guards
        _PATCHED = True
        logger.warning(
            "[TONOSAMA 5SEC ADVISORY PATCH] installed v3 vectorized_fallback=True order_5s_optional=%s",
            ok_order_5s,
        )
        return True and ok_order_5s
    except Exception:
        logger.exception("[TONOSAMA 5SEC ADVISORY PATCH] install failed")
        return bool(ok_order_5s)


__all__ = ["install"]