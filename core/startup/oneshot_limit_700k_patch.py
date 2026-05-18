# ============================================================
# File   : core/startup/oneshot_limit_700k_patch.py
# Version: Ver06-ALIGNED-SUMMARY-DISPLAY-AND-ENTRY-PATCHES
# ------------------------------------------------------------
# kabu_api.buy_sell_entry.MAX_ONESHOT を起動時に 700,000 円へ変更する。
# SUMMARY AI の daily risk 事前除外を銘柄単位に限定する。
# SUMMARY AI executor の executed 誤判定を補正する。
# SUMMARY AI のSELL候補から売建不可銘柄を候補前除外する。
# PUSH受信中にflush writerが止まった場合、monitor側で自己復旧する。
# SUMMARY表示ログの数値を項目ごとに桁揃えする。
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_INSTALLED = False


def _install_aligned_summary_display_patch() -> bool:
    try:
        import pandas as pd
        import numpy as np

        score_cols = {
            "score", "score_total", "final_score", "display_score",
            "score_buy", "score_sell", "disp_score", "disp_buy_score",
            "disp_sell_score", "disp_total_score", "disp_final_score",
            "base", "trend", "mom", "vel", "pen",
        }
        slope_cols = {
            "slope", "slope_atr_scaled", "score_slope", "disp_slope", "disp_score_slope",
        }
        two_cols = {"mtf", "score_mtf", "mtf_score", "disp_mtf", "disp_score_mtf", "macd", "signal"}
        one_cols = {"open", "high", "low", "close", "open_price", "high_price", "low_price", "close_price", "price", "current_price", "rsi"}

        def _num(v, default=np.nan):
            try:
                if v is None or (isinstance(v, str) and v.strip() == ""):
                    return default
                return float(v)
            except Exception:
                return default

        def _fmt(v, width: int, digits: int) -> str:
            x = _num(v)
            if pd.isna(x):
                return "-".rjust(width)
            return f"{x:{width}.{digits}f}"

        def _format_log_df(df):
            try:
                if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                    return df
                out = df.copy()
                for col in list(out.columns):
                    if col in score_cols:
                        out[col] = out[col].map(lambda v: _fmt(v, 7, 2))
                    elif col in slope_cols:
                        out[col] = out[col].map(lambda v: _fmt(v, 8, 4))
                    elif col in two_cols:
                        out[col] = out[col].map(lambda v: _fmt(v, 7, 2))
                    elif col in one_cols:
                        out[col] = out[col].map(lambda v: _fmt(v, 8, 1))
                return out
            except Exception:
                return df

        try:
            from core.global_context import context as ctx
            old = getattr(ctx, "_log_df_profile", None)
            if callable(old) and not getattr(old, "_aligned_display_patch_v1", False):
                def _patched_log_df_profile(prefix, tf, source, df):
                    try:
                        prof = ctx._profile_df(df)
                        ctx.logger.info(
                            "%s tf=%s source=%s rows=%s cols=%s unique_symbols=%s blank_symbolname=%s completed_summary=%s sample_cols=%s",
                            prefix, tf, source, prof.get("rows"), prof.get("cols"),
                            prof.get("unique_symbols"), prof.get("blank_symbolname"),
                            prof.get("completed_summary"), prof.get("sample_cols"),
                        )
                        ctx.logger.info(
                            "%s tf=%s source=%s nonzero score=%s slope=%s slope_atr_scaled=%s score_slope=%s mtf=%s score_mtf=%s mtf_score=%s rsi=%s macd=%s signal=%s close_nonnull=%s datetime_nonnull=%s",
                            prefix, tf, source, prof.get("score_nonzero"), prof.get("slope_nonzero"),
                            prof.get("slope_atr_scaled_nonzero"), prof.get("score_slope_nonzero"),
                            prof.get("mtf_nonzero"), prof.get("score_mtf_nonzero"),
                            prof.get("mtf_score_nonzero"), prof.get("rsi_nonzero"),
                            prof.get("macd_nonzero"), prof.get("signal_nonzero"),
                            prof.get("close_nonnull"), prof.get("datetime_nonnull"),
                        )
                        show_cols = [
                            c for c in [
                                "symbol", "symbolname", "score", "score_total", "final_score", "display_score",
                                "score_buy", "score_sell", "slope", "slope_atr_scaled", "score_slope",
                                "mtf", "score_mtf", "mtf_score",
                                "open", "high", "low", "close", "rsi", "macd", "signal", "datetime"
                            ] if c in df.columns
                        ]
                        if show_cols and not df.empty:
                            log_df = _format_log_df(df[show_cols].head(20))
                            ctx.logger.info("%s tf=%s source=%s\n%s", prefix, tf, source, log_df.to_string(index=False))
                    except Exception:
                        ctx.logger.exception("[GlobalContext] _log_df_profile patched failed prefix=%s tf=%s source=%s", prefix, tf, source)
                _patched_log_df_profile._aligned_display_patch_v1 = True
                ctx._log_df_profile = _patched_log_df_profile
        except Exception:
            logger.debug("[ALIGNED DISPLAY PATCH] context patch skipped", exc_info=True)

        try:
            from scheduler_jobs.summary import display_base as db
            db.fmt_metric = lambda v: _fmt(v, 7, 2).strip()
            db.fmt_confidence = lambda v: _fmt(v, 5, 2).strip()
            db.fmt_price = lambda v: _fmt(v, 8, 1).strip()
        except Exception:
            logger.debug("[ALIGNED DISPLAY PATCH] display_base patch skipped", exc_info=True)

        logger.warning("[ALIGNED DISPLAY PATCH] installed score=2dec slope=4dec price/rsi=1dec aligned")
        return True
    except Exception:
        logger.exception("[ALIGNED DISPLAY PATCH] install failed")
        return False


def _install_push_flush_auto_recover_patch() -> bool:
    try:
        os.environ.setdefault("PUSH_STREAM_AUTO_RECOVER_FLUSH", "1")
        from core.startup import push_flush_auto_recover_patch as p

        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] push_flush_auto_recover_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] push_flush_auto_recover_patch install failed")
        return False


def _install_summary_ai_symbol_risk_patch() -> bool:
    try:
        os.environ.setdefault("SUMMARY_AI_PRE_FILTER_DAILY_RISK", "1")
        os.environ.setdefault("SUMMARY_AI_PRE_FILTER_DAILY_RISK_SCOPE", "symbol_only")
        os.environ.setdefault("ENTRY_GLOBAL_MAX_DAILY_LOSS_YEN", "-50000")
        os.environ.setdefault("ENTRY_GLOBAL_MAX_CONSECUTIVE_LOSSES", "20")

        from core.startup import summary_ai_daily_risk_symbol_only_patch as p

        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] summary_ai_symbol_risk_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] summary_ai_symbol_risk_patch install failed")
        return False


def _install_summary_ai_executor_result_patch() -> bool:
    try:
        from core.startup import summary_ai_executor_result_patch as p

        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] summary_ai_executor_result_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] summary_ai_executor_result_patch install failed")
        return False


def _install_summary_ai_sell_credit_prefilter_patch() -> bool:
    try:
        from core.startup import summary_ai_sell_credit_prefilter_patch as p

        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] summary_ai_sell_credit_prefilter_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] summary_ai_sell_credit_prefilter_patch install failed")
        return False


def install() -> bool:
    global _INSTALLED

    if _INSTALLED:
        return True

    ok_display = _install_aligned_summary_display_patch()
    ok_push_flush = _install_push_flush_auto_recover_patch()

    ok_main = False
    try:
        from kabu_api import buy_sell_entry as bse

        old_value = getattr(bse, "MAX_ONESHOT", None)
        bse.MAX_ONESHOT = 700_000
        ok_main = True

        logger.warning(
            "[ONESHOT LIMIT PATCH] MAX_ONESHOT changed old=%s new=%s",
            old_value,
            bse.MAX_ONESHOT,
        )
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] install failed")

    ok_symbol_risk = _install_summary_ai_symbol_risk_patch()
    ok_executor_result = _install_summary_ai_executor_result_patch()
    ok_sell_credit_prefilter = _install_summary_ai_sell_credit_prefilter_patch()

    _INSTALLED = bool(ok_display or ok_push_flush or ok_main or ok_symbol_risk or ok_executor_result or ok_sell_credit_prefilter)
    return _INSTALLED
