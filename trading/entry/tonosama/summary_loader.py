# ============================================================
# File   : trading/entry/tonosama/summary_loader.py
# Version: Ver1.2-TONOSAMA-PUSH-HISTORY-FALLBACK
# ------------------------------------------------------------
# 目的:
#   殿様イナゴ用のサマリー読込。
#
# Ver1.1:
#   - global_data.get_merged_summary(interval) を source未指定で呼ぶと、
#     3m/5mで completed-only fallback に落ち、前日/古いYahoo行を掴むことがある。
#   - 殿様イナゴはPUSH由来のリアルタイム短期エントリーなので、
#     get_push_merged_summary -> get_merged_summary(source='push') -> legacy の順で読む。
#
# Ver1.2:
#   - 09:09ログで、1m completed summary が一時的に空になり、
#       [TONOSAMA ENTRY] merged summary empty interval=1
#       [TONOSAMA SURGE] base 1m recent empty -> skip TONOSAMA
#     となった。
#   - ただし GlobalContext には計算用 history cache があり、
#     get_summary_history(tf, source='push') で取得できる。
#   - completed summary がまだpublishされていない瞬間でも、
#     PUSH履歴キャッシュから最新行を拾えるように fallback を追加する。
#   - recent/stale判定は volume_surge.py 側で引き続き行う。
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

    # 3) PUSH履歴キャッシュ fallback。
    # completed summary がpublishされていない瞬間でも、history cache には
    # 最新PUSHサマリーが保持されていることがある。
    # stale/recent判定は後段の volume_surge.py に任せる。
    try:
        fn = getattr(global_data, "get_summary_history", None)
        if callable(fn):
            try:
                df = _safe_df(fn(interval, source="push"))
            except TypeError:
                df = _safe_df(fn(interval))
            if not df.empty:
                latest_dt = None
                if "datetime" in df.columns:
                    latest_dt = pd.to_datetime(df["datetime"], errors="coerce").max()
                logger.warning(
                    "[TONOSAMA ENTRY] loaded push summary history fallback interval=%s rows=%s latest_dt=%s via=get_summary_history_push",
                    interval,
                    len(df),
                    latest_dt,
                )
                return df
    except Exception:
        logger.debug("[TONOSAMA ENTRY] get_summary_history(source=push) failed interval=%s", interval, exc_info=True)

    # 4) 最後だけ旧API。ただしこの場合は古いfallbackを掴む可能性があるためログに出す。
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
