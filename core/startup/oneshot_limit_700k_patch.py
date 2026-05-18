# ============================================================
# File   : core/startup/oneshot_limit_700k_patch.py
# Version: Ver09-BUY-THRESHOLD-AND-FIXED-SYMBOLNAME-WIDTH
# ------------------------------------------------------------
# 起動時 runtime patches:
# - 70万円ワンショット制限
# - ENTRY数量0株の最低100株フォールバック
# - BUYエントリー閾値を後場スコアに合わせて緩和
# - SUMMARY AI daily risk / executed判定 / 売建不可候補除外
# - PUSH flush writer 自己復旧
# - SUMMARY表示ログの数値・列幅整形
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _install_entry_threshold_patch() -> bool:
    try:
        import trading.handlers.entry_controller as ec

        old_buy_score = getattr(ec, "MIN_SUMMARY_SCORE_BUY", None)
        old_buy_comp = getattr(ec, "MIN_COMPOSITE_SCORE_BUY", None)
        old_sell_score = getattr(ec, "MIN_SUMMARY_SCORE_SELL", None)
        old_sell_comp = getattr(ec, "MIN_COMPOSITE_SCORE_SELL", None)

        ec.MIN_SUMMARY_SCORE_BUY = _env_float("MIN_SUMMARY_SCORE_BUY", 1.0)
        ec.MIN_COMPOSITE_SCORE_BUY = _env_float("MIN_COMPOSITE_SCORE_BUY", 0.8)
        ec.MIN_SUMMARY_SCORE_SELL = _env_float("MIN_SUMMARY_SCORE_SELL", 1.0)
        ec.MIN_COMPOSITE_SCORE_SELL = _env_float("MIN_COMPOSITE_SCORE_SELL", 1.0)

        logger.warning(
            "[ENTRY THRESHOLD PATCH] installed BUY score %s->%s comp %s->%s SELL score %s->%s comp %s->%s",
            old_buy_score, ec.MIN_SUMMARY_SCORE_BUY,
            old_buy_comp, ec.MIN_COMPOSITE_SCORE_BUY,
            old_sell_score, ec.MIN_SUMMARY_SCORE_SELL,
            old_sell_comp, ec.MIN_COMPOSITE_SCORE_SELL,
        )
        return True
    except Exception:
        logger.exception("[ENTRY THRESHOLD PATCH] install failed")
        return False


def _install_entry_qty_minlot_patch() -> bool:
    try:
        os.environ.setdefault("ENTRY_MIN_LOT_FALLBACK_WHEN_AFFORDABLE", "1")
        from core.startup import entry_qty_min_lot_runtime_patch as p
        ok = p.install()
        logger.warning("[ONESHOT LIMIT PATCH] entry_qty_min_lot_runtime_patch installed=%s", ok)
        return bool(ok)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] entry_qty_min_lot_runtime_patch install failed")
        return False


def _install_aligned_summary_display_patch() -> bool:
    try:
        import unicodedata
        import pandas as pd
        import numpy as np

        score_cols = {
            "score", "score_total", "final_score", "display_score",
            "score_buy", "score_sell", "disp_score", "disp_buy_score",
            "disp_sell_score", "disp_total_score", "disp_final_score",
            "base", "trend", "mom", "vel", "pen",
        }
        slope_cols = {"slope", "slope_atr_scaled", "score_slope", "disp_slope", "disp_score_slope"}
        two_cols = {"mtf", "score_mtf", "mtf_score", "disp_mtf", "disp_score_mtf", "macd", "signal"}
        one_cols = {"open", "high", "low", "close", "open_price", "high_price", "low_price", "close_price", "price", "current_price", "rsi"}
        left_cols = {"symbol", "symbolname", "datetime"}
        symbolname_width = max(10, min(_env_int("DISPLAY_SYMBOLNAME_WIDTH", 24), 40))

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
                return "-".rjust(width)
            return f"{x:{width}.{digits}f}"

        def _cell(col: str, v) -> str:
            try:
                if col in score_cols:
                    return _fmt_num(v, 7, 2)
                if col in slope_cols:
                    return _fmt_num(v, 8, 4)
                if col in two_cols:
                    return _fmt_num(v, 7, 2)
                if col in one_cols:
                    return _fmt_num(v, 8, 1)
                if pd.isna(v):
                    return "-"
                return str(v)
            except Exception:
                return str(v)

        def _w(s: str) -> int:
            total = 0
            for ch in str(s):
                total += 2 if unicodedata.east_asian_width(ch) in {"F", "W", "A"} else 1
            return total

        def _pad(s: str, width: int, left: bool) -> str:
            s = str(s)
            pad = max(0, width - _w(s))
            return s + (" " * pad) if left else (" " * pad) + s

        def _clip(s: str, width: int) -> str:
            s = str(s)
            if _w(s) <= width:
                return s
            out = ""
            for ch in s:
                if _w(out + ch + "…") > width:
                    break
                out += ch
            return out + "…"

        def _table_to_string(df) -> str:
            try:
                if df is None or not isinstance(df, pd.DataFrame) or df.empty:
                    return ""
                rows = []
                cols = [str(c) for c in df.columns]
                for _, r in df.iterrows():
                    rows.append([_cell(c, r.get(c, "")) for c in cols])
                widths = []
                for i, c in enumerate(cols):
                    if c == "symbolname":
                        widths.append(symbolname_width)
                        continue
                    maxw = _w(c)
                    for row in rows:
                        maxw = max(maxw, _w(row[i]))
                    widths.append(maxw)

                header = " ".join(_pad(c, widths[i], True if c in left_cols else False) for i, c in enumerate(cols))
                lines = [header]
                for row in rows:
                    vals = []
                    for i, c in enumerate(cols):
                        s = row[i]
                        if c == "symbolname":
                            s = _clip(s, widths[i])
                        vals.append(_pad(s, widths[i], True if c in left_cols else False))
                    lines.append(" ".join(vals))
                return "\n".join(lines)
            except Exception:
                try:
                    return df.to_string(index=False)
                except Exception:
                    return ""

        try:
            from core.global_context import context as ctx
            old = getattr(ctx, "_log_df_profile", None)
            if callable(old) and not getattr(old, "_wide_aligned_display_patch_v3", False):
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
                            ctx.logger.info("%s tf=%s source=%s\n%s", prefix, tf, source, _table_to_string(df[show_cols].head(20)))
                    except Exception:
                        ctx.logger.exception("[GlobalContext] _log_df_profile patched failed prefix=%s tf=%s source=%s", prefix, tf, source)
                _patched_log_df_profile._wide_aligned_display_patch_v3 = True
                ctx._log_df_profile = _patched_log_df_profile
        except Exception:
            logger.debug("[ALIGNED DISPLAY PATCH] context patch skipped", exc_info=True)

        try:
            from scheduler_jobs.summary import display_base as db
            db.fmt_metric = lambda v: _fmt_num(v, 7, 2).strip()
            db.fmt_confidence = lambda v: _fmt_num(v, 5, 2).strip()
            db.fmt_price = lambda v: _fmt_num(v, 8, 1).strip()
        except Exception:
            logger.debug("[ALIGNED DISPLAY PATCH] display_base patch skipped", exc_info=True)

        logger.warning(
            "[ALIGNED DISPLAY PATCH] installed symbolname_width=%s score=2dec slope=4dec price/rsi=1dec",
            symbolname_width,
        )
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
    ok_threshold = _install_entry_threshold_patch()
    ok_qty_minlot = _install_entry_qty_minlot_patch()
    ok_push_flush = _install_push_flush_auto_recover_patch()

    ok_main = False
    try:
        from kabu_api import buy_sell_entry as bse
        old_value = getattr(bse, "MAX_ONESHOT", None)
        bse.MAX_ONESHOT = 700_000
        ok_main = True
        logger.warning("[ONESHOT LIMIT PATCH] MAX_ONESHOT changed old=%s new=%s", old_value, bse.MAX_ONESHOT)
    except Exception:
        logger.exception("[ONESHOT LIMIT PATCH] install failed")

    ok_symbol_risk = _install_summary_ai_symbol_risk_patch()
    ok_executor_result = _install_summary_ai_executor_result_patch()
    ok_sell_credit_prefilter = _install_summary_ai_sell_credit_prefilter_patch()

    _INSTALLED = bool(ok_display or ok_threshold or ok_qty_minlot or ok_push_flush or ok_main or ok_symbol_risk or ok_executor_result or ok_sell_credit_prefilter)
    return _INSTALLED
