# ============================================================
# File   : trading/entry/tonosama/summary_loader.py
# Version: Ver1.4-TONOSAMA-REJECT-PREV-DAY-HISTORY-FALLBACK
# ------------------------------------------------------------
# 目的:
#   殿様イナゴ用のサマリー読込。
#
# Ver1.3:
#   - completed publish前の直近PUSH行を拾うため、global_context の
#     get_rejected_merged_summary / get_summary_history を fallback に使う。
#
# Ver1.4:
#   - 2026-06-02 09:00ログで、当日サマリー未完成のため
#     get_summary_history fallback が前日 2026-06-01 15:30 の履歴を拾い、
#     TONOSAMA fresh summary wait が latest=前日 / age=63020秒 と判定していた。
#   - TONOSAMAの場中判定では、前日push summary履歴は使わない。
#   - fallback df の latest datetime が今日でない場合は空扱いにする。
#   - 明示的に許可したい場合だけ TONOSAMA_ALLOW_PREV_DAY_SUMMARY_HISTORY=1。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os

import pandas as pd

from global_state import global_data
from .utils import normalize_symbol, first_existing_col

logger = logging.getLogger(__name__)


def _safe_df(df) -> pd.DataFrame:
    if isinstance(df, pd.DataFrame):
        return df
    return pd.DataFrame()


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _latest_dt(df: pd.DataFrame):
    try:
        if isinstance(df, pd.DataFrame) and not df.empty and "datetime" in df.columns:
            return pd.to_datetime(df["datetime"], errors="coerce").max()
    except Exception:
        pass
    return None


def _today_naive() -> dt.date:
    try:
        return dt.datetime.now().date()
    except Exception:
        return dt.date.today()


def _reject_prev_day_history(df: pd.DataFrame, *, interval: int, via: str) -> pd.DataFrame:
    """TONOSAMAでは前日summary history fallbackを使わない。"""
    if df is None or df.empty:
        return pd.DataFrame()
    if _env_bool("TONOSAMA_ALLOW_PREV_DAY_SUMMARY_HISTORY", False):
        return df
    latest = _latest_dt(df)
    if latest is None or pd.isna(latest):
        return df
    try:
        latest_date = pd.Timestamp(latest).to_pydatetime().date()
    except Exception:
        return df
    today = _today_naive()
    if latest_date != today:
        logger.warning(
            "[TONOSAMA ENTRY] reject stale summary fallback interval=%s rows=%s latest_dt=%s latest_date=%s today=%s via=%s allow_prev_day=%s",
            interval,
            len(df),
            latest,
            latest_date,
            today,
            via,
            _env_bool("TONOSAMA_ALLOW_PREV_DAY_SUMMARY_HISTORY", False),
        )
        return pd.DataFrame()
    return df


def _call_global_context_method(name: str, interval: int, *, source: str = "push") -> pd.DataFrame:
    try:
        from core.global_context.context import global_context as GC

        fn = getattr(GC, name, None)
        if not callable(fn):
            return pd.DataFrame()
        try:
            return _safe_df(fn(int(interval), source=source))
        except TypeError:
            return _safe_df(fn(int(interval)))
    except Exception:
        logger.debug("[TONOSAMA ENTRY] global_context.%s failed interval=%s", name, interval, exc_info=True)
        return pd.DataFrame()


def _call_summary_getter(interval: int) -> pd.DataFrame | None:
    """PUSH優先でmerged summaryを取得する。"""
    interval = int(interval)

    # 1) 明示PUSH APIを優先
    try:
        fn = getattr(global_data, "get_push_merged_summary", None)
        if callable(fn):
            df = _safe_df(fn(interval))
            if not df.empty:
                logger.info(
                    "[TONOSAMA ENTRY] loaded push merged summary interval=%s rows=%s latest_dt=%s via=get_push_merged_summary",
                    interval,
                    len(df),
                    _latest_dt(df),
                )
                return df
    except Exception:
        logger.debug("[TONOSAMA ENTRY] get_push_merged_summary failed interval=%s", interval, exc_info=True)

    # 2) source='push' 対応API
    try:
        fn = getattr(global_data, "get_merged_summary", None)
        if callable(fn):
            try:
                df = _safe_df(fn(interval, source="push"))
            except TypeError:
                df = pd.DataFrame()
            if not df.empty:
                logger.info(
                    "[TONOSAMA ENTRY] loaded push merged summary interval=%s rows=%s latest_dt=%s via=get_merged_summary_source_push",
                    interval,
                    len(df),
                    _latest_dt(df),
                )
                return df
    except Exception:
        logger.debug("[TONOSAMA ENTRY] get_merged_summary(source=push) failed interval=%s", interval, exc_info=True)

    # 3) rejected merged fallback。
    #    completed判定で採用されなかった直近PUSH行を使う。
    df = _call_global_context_method("get_rejected_merged_summary", interval, source="push")
    if not df.empty:
        df = _reject_prev_day_history(df, interval=interval, via="global_context.rejected")
        if not df.empty:
            logger.warning(
                "[TONOSAMA ENTRY] loaded rejected push summary interval=%s rows=%s latest_dt=%s via=global_context.rejected fallback_recent_filter_required",
                interval,
                len(df),
                _latest_dt(df),
            )
            return df

    # 4) PUSH履歴キャッシュ fallback。
    #    completed summary がpublishされていない瞬間でも、history cache には
    #    最新PUSHサマリーが保持されていることがある。
    #    ただし前日履歴はTONOSAMAでは採用しない。
    df = _call_global_context_method("get_summary_history", interval, source="push")
    if not df.empty:
        df = _reject_prev_day_history(df, interval=interval, via="global_context.get_summary_history_push")
        if not df.empty:
            logger.warning(
                "[TONOSAMA ENTRY] loaded push summary history fallback interval=%s rows=%s latest_dt=%s via=global_context.get_summary_history_push",
                interval,
                len(df),
                _latest_dt(df),
            )
            return df

    # 5) global_data に互換 get_summary_history がある場合。
    try:
        fn = getattr(global_data, "get_summary_history", None)
        if callable(fn):
            try:
                df = _safe_df(fn(interval, source="push"))
            except TypeError:
                df = _safe_df(fn(interval))
            if not df.empty:
                df = _reject_prev_day_history(df, interval=interval, via="global_data.get_summary_history_push")
                if not df.empty:
                    logger.warning(
                        "[TONOSAMA ENTRY] loaded push summary history fallback interval=%s rows=%s latest_dt=%s via=global_data.get_summary_history_push",
                        interval,
                        len(df),
                        _latest_dt(df),
                    )
                    return df
    except Exception:
        logger.debug("[TONOSAMA ENTRY] get_summary_history(source=push) failed interval=%s", interval, exc_info=True)

    # 6) 最後だけ旧API。ただし古いfallbackを掴む可能性があるため、前日なら拒否する。
    try:
        fn = getattr(global_data, "get_merged_summary", None)
        if callable(fn):
            df = _safe_df(fn(interval))
            if not df.empty:
                df = _reject_prev_day_history(df, interval=interval, via="legacy_no_source")
                if not df.empty:
                    logger.warning(
                        "[TONOSAMA ENTRY] loaded merged summary interval=%s rows=%s latest_dt=%s via=legacy_no_source fallback_may_be_stale",
                        interval,
                        len(df),
                        _latest_dt(df),
                    )
                    return df
    except Exception:
        logger.debug("[TONOSAMA ENTRY] get_merged_summary legacy failed interval=%s", interval, exc_info=True)

    return None


def load_merged_summary(interval: int) -> pd.DataFrame:
    try:
        df = _call_summary_getter(int(interval))
        if df is None or getattr(df, "empty", True):
            logger.info("[TONOSAMA ENTRY] merged summary empty interval=%s", interval)
            return pd.DataFrame()
        if not isinstance(df, pd.DataFrame):
            logger.warning("[TONOSAMA ENTRY] merged summary is not DataFrame interval=%s type=%s", interval, type(df).__name__)
            return pd.DataFrame()
        out = df.copy()
        out["_interval"] = int(interval)
        return out
    except Exception:
        logger.exception("[TONOSAMA ENTRY] load merged summary failed interval=%s", interval)
        return pd.DataFrame()


def normalize_summary_base(df: pd.DataFrame, *, interval: int) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    x = df.copy()
    x["_interval"] = int(interval)
    if "symbol" not in x.columns:
        return pd.DataFrame()
    x["symbol"] = x["symbol"].map(normalize_symbol)
    x = x[x["symbol"] != ""]
    if x.empty:
        return pd.DataFrame()
    if "symbolname" not in x.columns:
        name_col = first_existing_col(x, ["name", "symbol_name", "SymbolName"])
        x["symbolname"] = x[name_col].astype(str) if name_col else ""
    x["datetime"] = pd.to_datetime(x["datetime"], errors="coerce") if "datetime" in x.columns else pd.NaT
    close_col = first_existing_col(x, ["close", "close_price", "current_price", "price", "last_price"])
    if close_col is None:
        return pd.DataFrame()
    volume_col = first_existing_col(x, ["volume", "trading_volume", "Volume"])
    x["volume"] = pd.to_numeric(x[volume_col], errors="coerce").fillna(0.0) if volume_col else 0.0
    x["close"] = pd.to_numeric(x[close_col], errors="coerce")
    x = x.dropna(subset=["close"])
    x = x[x["close"] > 0]
    numeric_cols = [
        "score", "score_buy", "score_sell", "score_total", "final_score", "display_score", "disp_score",
        "ranking_score", "rsi", "macd", "signal", "slope", "slope_atr_scaled", "mtf", "score_mtf",
        "mtf_score", "ma5", "ma25", "ma75", "ma25_conf", "ma75_conf", "ai_prob", "ranking_momentum",
        "rank_improve", "volume_delta", "change_percentage",
    ]
    for c in numeric_cols:
        if c in x.columns:
            x[c] = pd.to_numeric(x[c], errors="coerce")
    return x.sort_values(["symbol", "datetime"])


__all__ = ["load_merged_summary", "normalize_summary_base"]
