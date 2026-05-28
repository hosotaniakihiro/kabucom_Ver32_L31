# ============================================================
# File   : trading/entry/tonosama/summary_loader.py
# Version: Ver1.3-TONOSAMA-DIRECT-GLOBAL-CONTEXT-FALLBACK
# ------------------------------------------------------------
# 目的:
#   殿様イナゴ用のサマリー読込。
#
# Ver1.3:
#   - Ver1.2 の get_summary_history fallback は global_data に互換メソッドが無い
#     環境では呼べない。
#   - 09:07〜09:10ログでは completed push merged summary が空で、
#     TONOSAMA が base 1m recent empty で停止していた。
#   - global_context.get_rejected_merged_summary() / get_summary_history() を
#     直接参照し、completed publish前の直近PUSH行を拾えるようにする。
#   - stale/recent 判定は volume_surge.py 側で継続する。
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from global_state import global_data
from .utils import normalize_symbol, first_existing_col

logger = logging.getLogger(__name__)


def _safe_df(df) -> pd.DataFrame:
    if isinstance(df, pd.DataFrame):
        return df
    return pd.DataFrame()


def _latest_dt(df: pd.DataFrame):
    try:
        if isinstance(df, pd.DataFrame) and not df.empty and "datetime" in df.columns:
            return pd.to_datetime(df["datetime"], errors="coerce").max()
    except Exception:
        pass
    return None


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
                    "[TONOSAMA ENTRY] loaded push merged summary interval=%s rows=%s via=get_push_merged_summary",
                    interval,
                    len(df),
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
                    "[TONOSAMA ENTRY] loaded push merged summary interval=%s rows=%s via=get_merged_summary_source_push",
                    interval,
                    len(df),
                )
                return df
    except Exception:
        logger.debug("[TONOSAMA ENTRY] get_merged_summary(source=push) failed interval=%s", interval, exc_info=True)

    # 3) rejected merged fallback。
    #    completed判定で採用されなかった直近PUSH行を使う。
    df = _call_global_context_method("get_rejected_merged_summary", interval, source="push")
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
    #    stale/recent判定は後段の volume_surge.py に任せる。
    df = _call_global_context_method("get_summary_history", interval, source="push")
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
                logger.warning(
                    "[TONOSAMA ENTRY] loaded push summary history fallback interval=%s rows=%s latest_dt=%s via=global_data.get_summary_history_push",
                    interval,
                    len(df),
                    _latest_dt(df),
                )
                return df
    except Exception:
        logger.debug("[TONOSAMA ENTRY] get_summary_history(source=push) failed interval=%s", interval, exc_info=True)

    # 6) 最後だけ旧API。ただし古いfallbackを掴む可能性があるためログに出す。
    try:
        fn = getattr(global_data, "get_merged_summary", None)
        if callable(fn):
            df = _safe_df(fn(interval))
            if not df.empty:
                logger.warning(
                    "[TONOSAMA ENTRY] loaded merged summary interval=%s rows=%s via=legacy_no_source fallback_may_be_stale",
                    interval,
                    len(df),
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
