# ============================================================
# File   : scheduler_jobs/summary/announce_dispatcher.py
# Version: PRODUCTION-STABLE-REV1.0-SUMMARY-ANNOUNCE-DISPATCHER
# ------------------------------------------------------------
# 【概要】
#   定時サマリーjobの最後で Discord通知を統一的に実行する dispatcher
#
# 【主な機能】
#   - PUSH / RANKING / source別のタイトル切替
#   - add_entry_signals(df) の自動適用
#   - BUY / SELL / setup grouped / setup summary の通知
#   - 既存 sender 関数 or webhook URL の両対応
#   - 場中/時間外で通知本数や閾値を切替可能
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Callable, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

try:
    from trading.summary.entry_signals import add_entry_signals
except Exception:
    add_entry_signals = None

try:
    from trading.summary.announce import announce_summary_to_discord
except Exception:
    announce_summary_to_discord = None


# ------------------------------------------------------------
# 環境変数/設定の解決
# ------------------------------------------------------------
def _safe_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "y")


def _safe_int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _safe_float_env(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except Exception:
        return default


def _resolve_webhook_url(source: str = "") -> str:
    source = (source or "").strip().lower()

    source_specific = {
        "push": [
            "DISCORD_WEBHOOK_URL_PUSH",
            "SUMMARY_DISCORD_WEBHOOK_URL_PUSH",
        ],
        "ranking": [
            "DISCORD_WEBHOOK_URL_RANKING",
            "SUMMARY_DISCORD_WEBHOOK_URL_RANKING",
        ],
        "yahoo": [
            "DISCORD_WEBHOOK_URL_YAHOO",
            "SUMMARY_DISCORD_WEBHOOK_URL_YAHOO",
        ],
    }

    common_keys = [
        "DISCORD_WEBHOOK_URL",
        "SUMMARY_DISCORD_WEBHOOK_URL",
        "TRADING_DISCORD_WEBHOOK_URL",
    ]

    for key in source_specific.get(source, []):
        v = os.getenv(key, "").strip()
        if v:
            return v

    for key in common_keys:
        v = os.getenv(key, "").strip()
        if v:
            return v

    return ""


# ------------------------------------------------------------
# 表示ラベル
# ------------------------------------------------------------
def _source_label(source: str) -> str:
    s = (source or "").strip().lower()
    if s == "push":
        return "PUSH"
    if s == "ranking":
        return "RANKING"
    if s == "yahoo":
        return "YAHOO"
    return s.upper() if s else "SUMMARY"


def _build_title_prefix(source: str, market_label: str = "") -> str:
    src = _source_label(source)
    if market_label:
        return f"{src} {market_label}"
    return src


# ------------------------------------------------------------
# DataFrame 整形
# ------------------------------------------------------------
def _prepare_df(df: Optional[pd.DataFrame]) -> pd.DataFrame:
    if df is None:
        return pd.DataFrame()
    if not isinstance(df, pd.DataFrame):
        return pd.DataFrame()
    if df.empty:
        return df.copy()

    out = df.copy()

    if "symbolname" not in out.columns:
        if "symbolname_view" in out.columns:
            out["symbolname"] = out["symbolname_view"]
        elif "name" in out.columns:
            out["symbolname"] = out["name"]
        else:
            out["symbolname"] = ""

    if "datetime" in out.columns:
        try:
            out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        except Exception:
            pass

    return out


def _apply_entry_signals_if_needed(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    required = {"entry_setup_type", "setup_score", "entry_score_v4", "is_setup_entry"}
    if required.issubset(set(df.columns)):
        return df

    if add_entry_signals is None:
        logger.warning("[announce.dispatcher] add_entry_signals unavailable")
        return df

    try:
        return add_entry_signals(df)
    except Exception:
        logger.exception("[announce.dispatcher] add_entry_signals failed")
        return df


# ------------------------------------------------------------
# 閾値/本数の決定
# ------------------------------------------------------------
def _build_announce_config(
    *,
    in_session: bool,
    source: str,
    interval: int,
) -> Dict[str, object]:
    """
    場中/時間外で通知条件を変える。
    """
    source = (source or "").strip().lower()

    if in_session:
        cfg = {
            "include_buy": True,
            "include_sell": False,
            "include_grouped": True,
            "include_setup_summary": True,
            "top_n_buy": _safe_int_env("SUMMARY_TOP_N_BUY_IN_SESSION", 10),
            "top_n_sell": _safe_int_env("SUMMARY_TOP_N_SELL_IN_SESSION", 10),
            "top_n_per_setup": _safe_int_env("SUMMARY_TOP_N_PER_SETUP_IN_SESSION", 3),
            "min_entry_score": _safe_float_env("SUMMARY_MIN_ENTRY_SCORE_IN_SESSION", 55.0),
            "min_exit_score": _safe_float_env("SUMMARY_MIN_EXIT_SCORE_IN_SESSION", 35.0),
        }
    else:
        cfg = {
            "include_buy": True,
            "include_sell": False,
            "include_grouped": True,
            "include_setup_summary": True,
            "top_n_buy": _safe_int_env("SUMMARY_TOP_N_BUY_OFF_SESSION", 10),
            "top_n_sell": _safe_int_env("SUMMARY_TOP_N_SELL_OFF_SESSION", 10),
            "top_n_per_setup": _safe_int_env("SUMMARY_TOP_N_PER_SETUP_OFF_SESSION", 3),
            "min_entry_score": _safe_float_env("SUMMARY_MIN_ENTRY_SCORE_OFF_SESSION", 58.0),
            "min_exit_score": _safe_float_env("SUMMARY_MIN_EXIT_SCORE_OFF_SESSION", 38.0),
        }

    # ranking は本数をやや増やすなどの微調整が可能
    if source == "ranking":
        cfg["top_n_per_setup"] = int(cfg["top_n_per_setup"]) + 1

    # 5分足はやや厳しめにする例
    if int(interval) == 5:
        cfg["min_entry_score"] = float(cfg["min_entry_score"]) + 2.0

    return cfg


# ------------------------------------------------------------
# メイン
# ------------------------------------------------------------
def dispatch_summary_announce(
    df: Optional[pd.DataFrame],
    *,
    interval: int,
    source: str = "push",
    in_session: bool = True,
    sender: Optional[Callable[[str], object]] = None,
    webhook_url: Optional[str] = None,
    title_prefix: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> Dict[str, bool]:
    """
    定時jobの最後で呼ぶ統一入口。

    Parameters
    ----------
    df : pd.DataFrame
        通知対象 summary dataframe
    interval : int
        1 / 3 / 5
    source : str
        push / ranking / yahoo ...
    in_session : bool
        場中かどうか
    sender : callable, optional
        既存Discord sender
    webhook_url : str, optional
        Webhook URL
    title_prefix : str, optional
        タイトル接頭辞
    enabled : bool, optional
        Noneなら環境変数から解決
    """
    results = {
        "buy": False,
        "sell": False,
        "grouped": False,
        "setup_summary": False,
    }

    if announce_summary_to_discord is None:
        logger.warning("[announce.dispatcher] announce_summary_to_discord unavailable")
        return results

    if enabled is None:
        enabled = _safe_bool_env("SUMMARY_DISCORD_NOTIFY_ENABLED", True)

    if not enabled:
        logger.info(
            "[announce.dispatcher] skipped disabled source=%s interval=%s",
            source, interval
        )
        return results

    df = _prepare_df(df)
    if df is None or df.empty:
        logger.info(
            "[announce.dispatcher] skipped empty df source=%s interval=%s",
            source, interval
        )
        return results

    df = _apply_entry_signals_if_needed(df)

    cfg = _build_announce_config(
        in_session=in_session,
        source=source,
        interval=interval,
    )

    if not title_prefix:
        market_label = "IN-SESSION" if in_session else "OFF-SESSION"
        title_prefix = _build_title_prefix(source, market_label)

    resolved_webhook = webhook_url or _resolve_webhook_url(source)

    try:
        results = announce_summary_to_discord(
            df,
            interval=int(interval),
            include_buy=bool(cfg["include_buy"]),
            include_sell=bool(cfg["include_sell"]),
            include_grouped=bool(cfg["include_grouped"]),
            include_setup_summary=bool(cfg["include_setup_summary"]),
            top_n_buy=int(cfg["top_n_buy"]),
            top_n_sell=int(cfg["top_n_sell"]),
            top_n_per_setup=int(cfg["top_n_per_setup"]),
            min_entry_score=float(cfg["min_entry_score"]),
            min_exit_score=float(cfg["min_exit_score"]),
            sender=sender,
            webhook_url=resolved_webhook,
            title_prefix=title_prefix,
        )
        logger.info(
            "[announce.dispatcher] done source=%s interval=%s results=%s",
            source, interval, results
        )
        return results

    except Exception:
        logger.exception(
            "[announce.dispatcher] failed source=%s interval=%s",
            source, interval
        )
        return results