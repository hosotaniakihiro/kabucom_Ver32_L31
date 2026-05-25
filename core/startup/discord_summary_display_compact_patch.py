# ============================================================
# File   : core/startup/discord_summary_display_compact_patch.py
# Version: V1.1-COMPACT-DISCORD-SUMMARY-DISPLAY-SEND-1MIN-DEFAULT
# ------------------------------------------------------------
# 目的:
#   Discordへ送信されるサマリー表示が横長・桁揃え・日本語銘柄名で崩れる問題を補正する。
#
# 方針:
#   - Discord専用表示は「縦リスト・短い2行」に統一する。
#   - 日本語銘柄名の桁揃えをやめる。
#   - PUSH由来サマリーは 1分 / 3分 / 5分 をDiscord送信する。
#   - ランキング由来サマリーの1分抑止は環境変数で制御する。
#   - ENTRY / EXIT 通知や重要アラートは対象外。
#   - コンソール表示は既存のまま変更しない。
#
# V1.1:
#   - SUMMARY_DISCORD_SEND_1MIN のデフォルトを True に変更
#   - これにより PUSH由来 SUMMARY TOP10 1min もDiscordへ送る
#   - 抑止したい場合だけ SUMMARY_DISCORD_SEND_1MIN=0 を設定する
# ============================================================

from __future__ import annotations

import logging
import os
import re
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)
_PATCHED = False

_TRUE = {"1", "true", "yes", "y", "on", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "disable", "disabled"}


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _send_summary_1min_enabled() -> bool:
    # PUSH由来は1分/3分/5分を見たいので、1分も既定で送る。
    return _env_bool("SUMMARY_DISCORD_SEND_1MIN", True)


def _send_ranking_summary_1min_enabled() -> bool:
    # ランキング由来は従来互換として既定では1分抑止。
    return _env_bool("RANKING_SUMMARY_DISCORD_SEND_1MIN", False)


def _is_1min_label(label: Any) -> bool:
    s = str(label or "").strip().lower().replace(" ", "")
    return s in {"1min", "1m", "1分", "1分足", "1"}


def _clean_text(v: Any, *, max_len: int = 28) -> str:
    try:
        s = str(v if v is not None else "").replace("\r", " ").replace("\n", " ").strip()
        s = re.sub(r"\s+", " ", s)
        if len(s) > max_len:
            return s[:max_len - 1] + "…"
        return s
    except Exception:
        return ""


def _first(disp: Any, row: Any, names: list[str], default: Any = "-") -> Any:
    try:
        return disp.first_existing(row, names, default)
    except Exception:
        return default


def _fmt_num(disp: Any, v: Any) -> str:
    try:
        if v == "-" or v is None:
            return "-"
        return disp.fmt_metric(v)
    except Exception:
        try:
            x = float(v)
            if not np.isfinite(x):
                return "-"
            return f"{x:.2f}"
        except Exception:
            return _clean_text(v, max_len=12) or "-"


def _fmt_price(disp: Any, v: Any) -> str:
    try:
        if v == "-" or v is None:
            return "-"
        return disp.fmt_price(v)
    except Exception:
        return _fmt_num(disp, v)


def _reason_for_discord(disp: Any, row: Any, side: str) -> str:
    try:
        raw = disp._reason_text_for_discord(row, side)
        return _clean_text(raw or "-", max_len=70) or "-"
    except Exception:
        return "-"


def _build_candidate_compact(disp: Any, i: int, row: Any, *, side: str) -> str:
    symbol = _clean_text(_first(disp, row, ["symbol"], ""), max_len=8)
    name = _clean_text(_first(disp, row, ["symbolname_view", "symbolname", "name"], ""), max_len=18)

    score = _fmt_num(disp, _first(disp, row, ["disp_score", "score", "display_score", "final_score"], np.nan))
    buy = _fmt_num(disp, _first(disp, row, ["disp_buy_score", "score_buy", "buy_score"], np.nan))
    sell = _fmt_num(disp, _first(disp, row, ["disp_sell_score", "score_sell", "sell_score"], np.nan))
    close = _fmt_price(disp, _first(disp, row, ["disp_close", "close", "close_price", "current_price", "price"], np.nan))
    slope = _fmt_num(disp, _first(disp, row, ["disp_slope", "slope", "score_slope", "slope_atr_scaled"], np.nan))
    mtf = _fmt_num(disp, _first(disp, row, ["disp_mtf", "mtf", "score_mtf", "mtf_score"], np.nan))
    rsi = _fmt_num(disp, _first(disp, row, ["disp_rsi", "rsi"], np.nan))
    macd = _fmt_num(disp, _first(disp, row, ["disp_macd", "macd"], np.nan))
    reason = _reason_for_discord(disp, row, side)

    mark = "🟦" if str(side).upper() == "BUY" else "🟥"
    return (
        f"{mark} {i}. {symbol} {name}\n"
        f"   株価={close} score={score} buy={buy} sell={sell} slope={slope} mtf={mtf} rsi={rsi} macd={macd}\n"
        f"   理由={reason}"
    )


def _build_ai_candidate_compact(disp: Any, i: int, row: Any, *, side: str) -> str:
    symbol = _clean_text(_first(disp, row, ["symbol"], ""), max_len=8)
    name = _clean_text(_first(disp, row, ["symbolname_view", "symbolname", "name"], ""), max_len=18)
    conf = _first(disp, row, ["confidence", "conf", "ai_confidence"], np.nan)
    try:
        conf_text = disp.fmt_confidence(conf)
    except Exception:
        conf_text = _fmt_num(disp, conf)
    lot = _fmt_num(disp, _first(disp, row, ["lot", "order_lot", "qty", "lot_multiplier"], np.nan))
    close = _fmt_price(disp, _first(disp, row, ["disp_close", "close", "close_price", "current_price", "price"], np.nan))
    score = _fmt_num(disp, _first(disp, row, ["disp_score", "score", "display_score", "final_score"], np.nan))
    buy = _fmt_num(disp, _first(disp, row, ["disp_buy_score", "score_buy", "buy_score"], np.nan))
    sell = _fmt_num(disp, _first(disp, row, ["disp_sell_score", "score_sell", "sell_score"], np.nan))
    reason = _clean_text(_first(disp, row, ["ai_reason", "reason", "gate_reason"], "") or _reason_for_discord(disp, row, side), max_len=80)
    mark = "🤖🟦" if str(side).upper() == "BUY" else "🤖🟥"
    return (
        f"{mark} {i}. {symbol} {name}\n"
        f"   conf={conf_text} lot={lot} 株価={close} score={score} buy={buy} sell={sell}\n"
        f"   理由={reason or '-'}"
    )


def _send_to_discord_compact(disp: Any, lines: list[str], title: str | None = None) -> None:
    try:
        if not callable(getattr(disp, "send_discord_message", None)):
            logger.info("[DISCORD SUMMARY COMPACT] sender not available")
            return
        cleaned = [_clean_text(x, max_len=900) for x in lines if x is not None and str(x).strip() != ""]
        if not cleaned:
            return
        header = _clean_text(title or "", max_len=120)
        text = (header + "\n" if header else "") + "\n".join(cleaned)
        text = text.strip()
        if not text:
            return

        limit = 1800
        chunks: list[str] = []
        while text:
            if len(text) <= limit:
                chunks.append(text)
                break
            cut = text.rfind("\n", 0, limit)
            if cut <= 0:
                cut = limit
            chunks.append(text[:cut].rstrip())
            text = text[cut:].lstrip()

        for idx, chunk in enumerate(chunks, start=1):
            ok = disp.send_discord_message(content=chunk)
            logger.info("[DISCORD SUMMARY COMPACT] sent chunk=%s/%s ok=%s chars=%s", idx, len(chunks), ok, len(chunk))
    except Exception:
        logger.exception("[DISCORD SUMMARY COMPACT] send failed")


def install() -> bool:
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import scheduler_jobs.summary.display as disp
    except Exception:
        logger.exception("[DISCORD SUMMARY COMPACT] display import failed")
        return False

    try:
        old_send = getattr(disp, "_send_to_discord", None)
        if callable(old_send) and not getattr(old_send, "_discord_compact_patch", False):
            def _send_to_discord_patched(lines: list[str], title: str | None = None) -> None:
                return _send_to_discord_compact(disp, lines, title)

            _send_to_discord_patched._discord_compact_patch = True  # type: ignore[attr-defined]
            _send_to_discord_patched._original = old_send  # type: ignore[attr-defined]
            disp._send_to_discord = _send_to_discord_patched

        def _candidate(i: int, row: Any, *, side: str) -> str:
            return _build_candidate_compact(disp, i, row, side=side)

        def _ai_candidate(i: int, row: Any, *, side: str) -> str:
            return _build_ai_candidate_compact(disp, i, row, side=side)

        disp._build_discord_candidate_2lines = _candidate
        disp._build_discord_ai_candidate_2lines = _ai_candidate

        old_print_summary = getattr(disp, "print_summary_top10", None)
        if callable(old_print_summary) and not getattr(old_print_summary, "_discord_1min_suppress_patch", False):
            def _print_summary_top10_patched(summary_df, interval_label="1min", *, notify_discord=True, **kwargs):
                if notify_discord and _is_1min_label(interval_label) and not _send_summary_1min_enabled():
                    logger.info("[DISCORD SUMMARY COMPACT] suppress 1min SUMMARY discord interval=%s", interval_label)
                    notify_discord = False
                return old_print_summary(summary_df, interval_label=interval_label, notify_discord=notify_discord, **kwargs)

            _print_summary_top10_patched._discord_1min_suppress_patch = True  # type: ignore[attr-defined]
            _print_summary_top10_patched._original = old_print_summary  # type: ignore[attr-defined]
            disp.print_summary_top10 = _print_summary_top10_patched

        old_print_ranking = getattr(disp, "print_ranking_summary_top10", None)
        if callable(old_print_ranking) and not getattr(old_print_ranking, "_discord_1min_suppress_patch", False):
            def _print_ranking_summary_top10_patched(summary_df, interval_label="1min", *, notify_discord=True, **kwargs):
                if notify_discord and _is_1min_label(interval_label) and not _send_ranking_summary_1min_enabled():
                    logger.info("[DISCORD SUMMARY COMPACT] suppress 1min RANKING SUMMARY discord interval=%s", interval_label)
                    notify_discord = False
                return old_print_ranking(summary_df, interval_label=interval_label, notify_discord=notify_discord, **kwargs)

            _print_ranking_summary_top10_patched._discord_1min_suppress_patch = True  # type: ignore[attr-defined]
            _print_ranking_summary_top10_patched._original = old_print_ranking  # type: ignore[attr-defined]
            disp.print_ranking_summary_top10 = _print_ranking_summary_top10_patched

        _PATCHED = True
        logger.warning(
            "[DISCORD SUMMARY COMPACT] installed compact_display=True suppress_summary_1min=%s suppress_ranking_1min=%s send_summary_1min=%s send_ranking_1min=%s",
            not _send_summary_1min_enabled(),
            not _send_ranking_summary_1min_enabled(),
            _send_summary_1min_enabled(),
            _send_ranking_summary_1min_enabled(),
        )
        return True
    except Exception:
        logger.exception("[DISCORD SUMMARY COMPACT] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[DISCORD SUMMARY COMPACT] auto install failed")


__all__ = ["install"]
