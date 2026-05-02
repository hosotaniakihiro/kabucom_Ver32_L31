# ============================================================
# File   : trading/summary/announce.py
# Version: PRODUCTION-STABLE-REV1.0-DISCORD-SETUP-AWARE
# ------------------------------------------------------------
# 【概要】
#   summary DataFrame から setup-aware な候補を抽出し、
#   Discordへ通知するためのモジュール
#
# 【主な機能】
#   - BUY / SELL 候補のDiscord通知
#   - setup別 grouped 通知
#   - setup summary 通知
#   - Webhook直送 / 既存sender関数の両対応
#
# 【前提】
#   - trading.summary.entry_signals.add_entry_signals
#   - trading.summary.top_candidates
#
# 【使い方】
#   announce_summary_to_discord(df, interval=1, webhook_url=...)
#   announce_summary_to_discord(df, interval=5, sender=my_sender)
# ============================================================

from __future__ import annotations

import json
import logging
import os
from typing import Callable, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

try:
    from trading.summary.entry_signals import add_entry_signals
except Exception:  # pragma: no cover
    add_entry_signals = None

try:
    from trading.summary.top_candidates import (
        build_discord_buy_message,
        build_discord_sell_message,
        build_setup_grouped_text,
        build_setup_summary_text,
        get_top_buy_candidates,
        get_top_sell_candidates,
        make_top_candidates_package,
    )
except Exception as e:  # pragma: no cover
    build_discord_buy_message = None
    build_discord_sell_message = None
    build_setup_grouped_text = None
    build_setup_summary_text = None
    get_top_buy_candidates = None
    get_top_sell_candidates = None
    make_top_candidates_package = None
    logger.exception("[summary.announce] import failed: %s", e)


# ------------------------------------------------------------
# utility
# ------------------------------------------------------------
def _safe_str(v, default="") -> str:
    try:
        if pd.isna(v):
            return default
        return str(v)
    except Exception:
        return default


def _chunk_text(text: str, max_len: int = 1800) -> list[str]:
    text = _safe_str(text, "")
    if not text:
        return []

    lines = text.splitlines()
    chunks: list[str] = []
    buf: list[str] = []
    size = 0

    for line in lines:
        add_len = len(line) + 1
        if buf and size + add_len > max_len:
            chunks.append("\n".join(buf))
            buf = [line]
            size = add_len
        else:
            buf.append(line)
            size += add_len

    if buf:
        chunks.append("\n".join(buf))

    return chunks


def _resolve_webhook_url(webhook_url: Optional[str] = None) -> str:
    if webhook_url:
        return webhook_url

    for env_key in [
        "DISCORD_WEBHOOK_URL",
        "SUMMARY_DISCORD_WEBHOOK_URL",
        "TRADING_DISCORD_WEBHOOK_URL",
    ]:
        v = os.getenv(env_key, "").strip()
        if v:
            return v

    return ""


def _post_discord_webhook(webhook_url: str, content: str, timeout: int = 10) -> bool:
    if not webhook_url:
        logger.warning("[summary.announce] webhook_url empty")
        return False

    if requests is None:
        logger.error("[summary.announce] requests not available")
        return False

    try:
        resp = requests.post(
            webhook_url,
            json={"content": content},
            timeout=timeout,
        )
        if 200 <= resp.status_code < 300:
            return True

        logger.warning(
            "[summary.announce] discord webhook failed status=%s body=%s",
            resp.status_code,
            resp.text[:300],
        )
        return False
    except Exception:
        logger.exception("[summary.announce] discord webhook exception")
        return False


def _send_text(
    text: str,
    *,
    sender: Optional[Callable[[str], object]] = None,
    webhook_url: Optional[str] = None,
    max_len: int = 1800,
) -> bool:
    if not text:
        return False

    chunks = _chunk_text(text, max_len=max_len)
    if not chunks:
        return False

    ok_any = False

    if sender is not None:
        for chunk in chunks:
            try:
                sender(chunk)
                ok_any = True
            except Exception:
                logger.exception("[summary.announce] sender failed")
        return ok_any

    wh = _resolve_webhook_url(webhook_url)
    if not wh:
        logger.warning("[summary.announce] no sender and no webhook url")
        return False

    for chunk in chunks:
        ok = _post_discord_webhook(wh, chunk)
        ok_any = ok_any or ok

    return ok_any


def _apply_entry_signals_if_needed(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df

    required_cols = {"entry_setup_type", "setup_score", "entry_score_v4", "is_setup_entry"}
    if required_cols.issubset(set(df.columns)):
        return df

    if add_entry_signals is None:
        logger.warning("[summary.announce] add_entry_signals unavailable")
        return df

    try:
        return add_entry_signals(df)
    except Exception:
        logger.exception("[summary.announce] add_entry_signals failed")
        return df


def _header(interval: int, mode: str, label: str = "") -> str:
    interval_label = f"{interval}m"
    suffix = f" {label}" if label else ""
    return f"【SUMMARY {mode.upper()} {interval_label}{suffix}】"


# ------------------------------------------------------------
# single message builders
# ------------------------------------------------------------
def build_buy_announce_text(
    df: pd.DataFrame,
    *,
    interval: int,
    top_n: int = 10,
    min_entry_score: Optional[float] = None,
    title_prefix: str = "",
) -> str:
    if build_discord_buy_message is None:
        return ""

    title = _header(interval, "BUY", title_prefix)
    return build_discord_buy_message(
        df,
        top_n=top_n,
        latest_per_symbol=True,
        only_setup_entry=True,
        min_entry_score=min_entry_score,
        header=title,
    )


def build_sell_announce_text(
    df: pd.DataFrame,
    *,
    interval: int,
    top_n: int = 10,
    min_exit_score: Optional[float] = None,
    title_prefix: str = "",
) -> str:
    if build_discord_sell_message is None:
        return ""

    title = _header(interval, "SELL", title_prefix)
    return build_discord_sell_message(
        df,
        top_n=top_n,
        latest_per_symbol=True,
        min_exit_score=min_exit_score,
        header=title,
    )


def build_grouped_announce_text(
    df: pd.DataFrame,
    *,
    interval: int,
    top_n_per_setup: int = 3,
    min_entry_score: Optional[float] = None,
    title_prefix: str = "",
) -> str:
    if build_setup_grouped_text is None:
        return ""

    title = _header(interval, "SETUP", title_prefix)
    return build_setup_grouped_text(
        df,
        title=title,
        latest_per_symbol=True,
        only_setup_entry=True,
        min_entry_score=min_entry_score,
        top_n_per_setup=top_n_per_setup,
    )


def build_setup_summary_announce_text(
    df: pd.DataFrame,
    *,
    interval: int,
    min_entry_score: Optional[float] = None,
    title_prefix: str = "",
) -> str:
    if build_setup_summary_text is None:
        return ""

    title = _header(interval, "SETUP-SUMMARY", title_prefix)
    return build_setup_summary_text(
        df,
        title=title,
        latest_per_symbol=True,
        only_setup_entry=True,
        min_entry_score=min_entry_score,
    )


# ------------------------------------------------------------
# public notify functions
# ------------------------------------------------------------
def announce_buy_candidates_to_discord(
    df: pd.DataFrame,
    *,
    interval: int,
    top_n: int = 10,
    min_entry_score: Optional[float] = None,
    sender: Optional[Callable[[str], object]] = None,
    webhook_url: Optional[str] = None,
    title_prefix: str = "",
) -> bool:
    if df is None or df.empty:
        logger.info("[summary.announce] buy announce skipped: empty df")
        return False

    df = _apply_entry_signals_if_needed(df)
    text = build_buy_announce_text(
        df,
        interval=interval,
        top_n=top_n,
        min_entry_score=min_entry_score,
        title_prefix=title_prefix,
    )
    return _send_text(text, sender=sender, webhook_url=webhook_url)


def announce_sell_candidates_to_discord(
    df: pd.DataFrame,
    *,
    interval: int,
    top_n: int = 10,
    min_exit_score: Optional[float] = None,
    sender: Optional[Callable[[str], object]] = None,
    webhook_url: Optional[str] = None,
    title_prefix: str = "",
) -> bool:
    if df is None or df.empty:
        logger.info("[summary.announce] sell announce skipped: empty df")
        return False

    df = _apply_entry_signals_if_needed(df)
    text = build_sell_announce_text(
        df,
        interval=interval,
        top_n=top_n,
        min_exit_score=min_exit_score,
        title_prefix=title_prefix,
    )
    return _send_text(text, sender=sender, webhook_url=webhook_url)


def announce_grouped_candidates_to_discord(
    df: pd.DataFrame,
    *,
    interval: int,
    top_n_per_setup: int = 3,
    min_entry_score: Optional[float] = None,
    sender: Optional[Callable[[str], object]] = None,
    webhook_url: Optional[str] = None,
    title_prefix: str = "",
) -> bool:
    if df is None or df.empty:
        logger.info("[summary.announce] grouped announce skipped: empty df")
        return False

    df = _apply_entry_signals_if_needed(df)
    text = build_grouped_announce_text(
        df,
        interval=interval,
        top_n_per_setup=top_n_per_setup,
        min_entry_score=min_entry_score,
        title_prefix=title_prefix,
    )
    return _send_text(text, sender=sender, webhook_url=webhook_url)


def announce_setup_summary_to_discord(
    df: pd.DataFrame,
    *,
    interval: int,
    min_entry_score: Optional[float] = None,
    sender: Optional[Callable[[str], object]] = None,
    webhook_url: Optional[str] = None,
    title_prefix: str = "",
) -> bool:
    if df is None or df.empty:
        logger.info("[summary.announce] setup summary announce skipped: empty df")
        return False

    df = _apply_entry_signals_if_needed(df)
    text = build_setup_summary_announce_text(
        df,
        interval=interval,
        min_entry_score=min_entry_score,
        title_prefix=title_prefix,
    )
    return _send_text(text, sender=sender, webhook_url=webhook_url)


def announce_summary_to_discord(
    df: pd.DataFrame,
    *,
    interval: int,
    include_buy: bool = True,
    include_sell: bool = True,
    include_grouped: bool = True,
    include_setup_summary: bool = True,
    top_n_buy: int = 10,
    top_n_sell: int = 10,
    top_n_per_setup: int = 3,
    min_entry_score: Optional[float] = None,
    min_exit_score: Optional[float] = None,
    sender: Optional[Callable[[str], object]] = None,
    webhook_url: Optional[str] = None,
    title_prefix: str = "",
) -> Dict[str, bool]:
    """
    summary候補をまとめてDiscord通知する。
    """
    results = {
        "buy": False,
        "sell": False,
        "grouped": False,
        "setup_summary": False,
    }

    if df is None or df.empty:
        logger.info("[summary.announce] summary announce skipped: empty df")
        return results

    df = _apply_entry_signals_if_needed(df)

    if include_buy:
        results["buy"] = announce_buy_candidates_to_discord(
            df,
            interval=interval,
            top_n=top_n_buy,
            min_entry_score=min_entry_score,
            sender=sender,
            webhook_url=webhook_url,
            title_prefix=title_prefix,
        )

    if include_sell:
        results["sell"] = announce_sell_candidates_to_discord(
            df,
            interval=interval,
            top_n=top_n_sell,
            min_exit_score=min_exit_score,
            sender=sender,
            webhook_url=webhook_url,
            title_prefix=title_prefix,
        )

    if include_grouped:
        results["grouped"] = announce_grouped_candidates_to_discord(
            df,
            interval=interval,
            top_n_per_setup=top_n_per_setup,
            min_entry_score=min_entry_score,
            sender=sender,
            webhook_url=webhook_url,
            title_prefix=title_prefix,
        )

    if include_setup_summary:
        results["setup_summary"] = announce_setup_summary_to_discord(
            df,
            interval=interval,
            min_entry_score=min_entry_score,
            sender=sender,
            webhook_url=webhook_url,
            title_prefix=title_prefix,
        )

    return results


# ------------------------------------------------------------
# convenience wrappers
# ------------------------------------------------------------
def announce_buy_only(
    df: pd.DataFrame,
    interval: int,
    *,
    sender: Optional[Callable[[str], object]] = None,
    webhook_url: Optional[str] = None,
    top_n: int = 10,
    min_entry_score: Optional[float] = None,
) -> bool:
    return announce_buy_candidates_to_discord(
        df,
        interval=interval,
        sender=sender,
        webhook_url=webhook_url,
        top_n=top_n,
        min_entry_score=min_entry_score,
    )


def announce_sell_only(
    df: pd.DataFrame,
    interval: int,
    *,
    sender: Optional[Callable[[str], object]] = None,
    webhook_url: Optional[str] = None,
    top_n: int = 10,
    min_exit_score: Optional[float] = None,
) -> bool:
    return announce_sell_candidates_to_discord(
        df,
        interval=interval,
        sender=sender,
        webhook_url=webhook_url,
        top_n=top_n,
        min_exit_score=min_exit_score,
    )