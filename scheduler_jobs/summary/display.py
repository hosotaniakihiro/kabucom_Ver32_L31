# ============================================================
# File   : scheduler_jobs/summary/display.py
# Function:
#   - public printer / compatibility 入口
#   - line builder
#   - AI sections
#   - Discord 通知
#   - Discord用 1銘柄2行メッセージ生成
# ------------------------------------------------------------
# Version: Ver10.3-PRODUCTION-DISPLAY-SPLIT-DISCORD-2LINES
# ============================================================

from __future__ import annotations

import logging
import numpy as np
import pandas as pd

from utils.alerts_util import send_discord_message

from .display_base import safe_df, first_existing, fmt_metric, fmt_price, fmt_confidence, print_line
from .display_reasons import (
    build_buy_reason_line,
    build_sell_reason_line,
    build_exit_reason_line,
)
from .display_ranking import build_ranking_line
from .display_ai import (
    build_ai_buy_line,
    build_ai_sell_line,
    build_ai_exit_line,
)
from .display_normalizer import repair_mtf_consistency, ensure_display_columns
from .display_sorting import (
    build_header_context,
    latest_header_text,
    prepare_buy_df,
    prepare_sell_df,
    prepare_ai_buy_df,
    prepare_ai_sell_df,
    prepare_ai_exit_df,
)

logger = logging.getLogger(__name__)


# ============================================================
# Discord sender
# ============================================================

def _discord_available() -> bool:
    return callable(send_discord_message)


def _send_to_discord(lines: list[str], title: str | None = None) -> None:
    """
    表示本文を Discord へ送る。
    1メッセージが長すぎる場合は分割する。
    """
    try:
        if not _discord_available():
            logger.info("[SUMMARY DISPLAY] discord sender not available")
            return

        cleaned = [str(x) for x in lines if x is not None and str(x).strip() != ""]
        if not cleaned:
            return

        body = "\n".join(cleaned).strip()
        if not body:
            return

        prefix = f"{title}\n" if title else ""
        text = f"{prefix}{body}".strip()

        limit = 1900
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
            try:
                send_discord_message(content=chunk)
                logger.info(
                    "[SUMMARY DISPLAY] discord sent chunk=%s/%s chars=%s",
                    idx,
                    len(chunks),
                    len(chunk),
                )
            except Exception:
                logger.exception(
                    "[SUMMARY DISPLAY] discord send failed chunk=%s/%s",
                    idx,
                    len(chunks),
                )

    except Exception:
        logger.exception("[SUMMARY DISPLAY] _send_to_discord failed")


# ============================================================
# line builders for console / logger
# ============================================================

def _build_breakdown_line(row: pd.Series) -> str:
    base = first_existing(row, ["disp_base", "score_base", "breakdown_base", "base_score", "base"], np.nan)
    trend = first_existing(row, ["disp_trend", "score_trend", "breakdown_trend", "trend_score", "trend"], np.nan)
    mom = first_existing(
        row,
        ["disp_mom", "score_momentum", "breakdown_mom", "score_mom", "momentum_score", "mom", "momentum"],
        np.nan,
    )
    vel = first_existing(
        row,
        ["disp_vel", "score_velocity", "breakdown_vel", "score_vel", "velocity_score", "vel", "velocity"],
        np.nan,
    )
    pen = first_existing(
        row,
        ["disp_pen", "score_penalty", "breakdown_pen", "score_pen", "penalty_score", "penalty", "pen"],
        np.nan,
    )

    return (
        f"    base={fmt_metric(base):>6} "
        f"trend={fmt_metric(trend):>6} "
        f"mom={fmt_metric(mom):>6} "
        f"vel={fmt_metric(vel):>6} "
        f"pen={fmt_metric(pen):>6}"
    )


def _build_buy_line(i: int, row: pd.Series) -> str:
    symbol = first_existing(row, ["symbol"], "")
    symbolname = first_existing(row, ["symbolname_view", "symbolname", "name"], "")

    score = first_existing(row, ["disp_score", "score", "display_score", "final_score"], np.nan)
    score_buy = first_existing(row, ["disp_buy_score", "score_buy"], np.nan)
    score_sell = first_existing(row, ["disp_sell_score", "score_sell"], np.nan)
    slope = first_existing(row, ["disp_slope", "slope", "score_slope"], np.nan)
    mtf = first_existing(row, ["disp_mtf", "mtf", "score_mtf", "mtf_score"], np.nan)
    total = first_existing(row, ["disp_total_score", "score_total", "total_score"], np.nan)
    final_score = first_existing(row, ["disp_final_score", "final_score", "display_score", "score"], np.nan)
    rsi = first_existing(row, ["disp_rsi", "rsi"], np.nan)
    macd = first_existing(row, ["disp_macd", "macd"], np.nan)
    close = first_existing(row, ["disp_close", "close", "close_price", "current_price"], np.nan)

    line1 = (
        f"{i:2d}. {str(symbol):<6} {str(symbolname):<28} "
        f"score={fmt_metric(score):>6} "
        f"buy={fmt_metric(score_buy):>6} "
        f"sell={fmt_metric(score_sell):>6} "
        f"slope={fmt_metric(slope):>6} "
        f"mtf={fmt_metric(mtf):>6} "
        f"total={fmt_metric(total):>6} "
        f"final={fmt_metric(final_score):>6} "
        f"rsi={fmt_metric(rsi):>6} "
        f"macd={fmt_metric(macd):>6} "
        f"close={fmt_price(close):>7}"
    )

    line2 = _build_breakdown_line(row)
    line3 = build_buy_reason_line(row)
    line_rank = build_ranking_line(row)

    parts = [line1, line2]
    if line_rank:
        parts.append(line_rank)
    parts.append(line3)

    return "\n".join(parts)


def _build_sell_line(i: int, row: pd.Series) -> str:
    symbol = first_existing(row, ["symbol"], "")
    symbolname = first_existing(row, ["symbolname_view", "symbolname", "name"], "")

    score = first_existing(row, ["disp_score", "score", "display_score", "final_score"], np.nan)
    score_buy = first_existing(row, ["disp_buy_score", "score_buy"], np.nan)
    score_sell = first_existing(row, ["disp_sell_score", "score_sell"], np.nan)
    slope = first_existing(row, ["disp_slope", "slope", "score_slope"], np.nan)
    mtf = first_existing(row, ["disp_mtf", "mtf", "score_mtf", "mtf_score"], np.nan)
    total = first_existing(row, ["disp_total_score", "score_total", "total_score"], np.nan)
    final_score = first_existing(row, ["disp_final_score", "final_score", "display_score", "score"], np.nan)
    rsi = first_existing(row, ["disp_rsi", "rsi"], np.nan)
    macd = first_existing(row, ["disp_macd", "macd"], np.nan)
    close = first_existing(row, ["disp_close", "close", "close_price", "current_price"], np.nan)

    line1 = (
        f"{i:2d}. {str(symbol):<6} {str(symbolname):<28} "
        f"score={fmt_metric(score):>6} "
        f"buy={fmt_metric(score_buy):>6} "
        f"sell={fmt_metric(score_sell):>6} "
        f"slope={fmt_metric(slope):>6} "
        f"mtf={fmt_metric(mtf):>6} "
        f"total={fmt_metric(total):>6} "
        f"final={fmt_metric(final_score):>6} "
        f"rsi={fmt_metric(rsi):>6} "
        f"macd={fmt_metric(macd):>6} "
        f"close={fmt_price(close):>7}"
    )

    line2 = _build_breakdown_line(row)
    line3 = build_sell_reason_line(row)
    line_rank = build_ranking_line(row)

    parts = [line1, line2]
    if line_rank:
        parts.append(line_rank)
    parts.append(line3)

    return "\n".join(parts)


# ============================================================
# Discord 2-line builders
# ============================================================

def _reason_text_for_discord(row: pd.Series, side: str) -> str:
    """
    build_buy_reason_line / build_sell_reason_line の先頭ラベルを削って、
    Discord用に短くする。

    例:
      '    理由(SELL)=売りスコア優勢 / 下向き'
      -> '売りスコア優勢 / 下向き'
    """
    try:
        if side.upper() == "SELL":
            raw = build_sell_reason_line(row)
        else:
            raw = build_buy_reason_line(row)

        raw = str(raw or "").strip()
        if not raw:
            return "-"

        if "=" in raw:
            return raw.split("=", 1)[1].strip()

        return raw
    except Exception:
        logger.debug("[SUMMARY DISPLAY] _reason_text_for_discord failed", exc_info=True)
        return "-"


def _build_discord_candidate_2lines(i: int, row: pd.Series, *, side: str) -> str:
    """
    Discord用。
    1銘柄を必ず2行にする。

    情報量を増やした版:
      1行目:
        symbol / name / score / buy / sell / total / final / close / rsi / macd
      2行目:
        slope / mtf / base / trend / mom / vel / pen / rank / chg / turn / tick / 理由

    コンソール表示用の _build_buy_line / _build_sell_line は変更しない。
    """

    symbol = first_existing(row, ["symbol"], "")
    symbolname = first_existing(row, ["symbolname_view", "symbolname", "name"], "")

    score = first_existing(row, ["disp_score", "score", "display_score", "final_score"], np.nan)
    score_buy = first_existing(row, ["disp_buy_score", "score_buy"], np.nan)
    score_sell = first_existing(row, ["disp_sell_score", "score_sell"], np.nan)
    total = first_existing(row, ["disp_total_score", "score_total", "total_score"], np.nan)
    final_score = first_existing(row, ["disp_final_score", "final_score", "display_score", "score"], np.nan)

    close = first_existing(row, ["disp_close", "close", "close_price", "current_price"], np.nan)
    rsi = first_existing(row, ["disp_rsi", "rsi"], np.nan)
    macd = first_existing(row, ["disp_macd", "macd"], np.nan)

    slope = first_existing(row, ["disp_slope", "slope", "score_slope", "slope_atr_scaled"], np.nan)
    mtf = first_existing(row, ["disp_mtf", "mtf", "score_mtf", "mtf_score"], np.nan)

    base = first_existing(row, ["disp_base", "score_base", "breakdown_base", "base_score", "base"], np.nan)
    trend = first_existing(row, ["disp_trend", "score_trend", "breakdown_trend", "trend_score", "trend"], np.nan)
    mom = first_existing(
        row,
        ["disp_mom", "score_momentum", "breakdown_mom", "score_mom", "momentum_score", "mom", "momentum"],
        np.nan,
    )
    vel = first_existing(
        row,
        ["disp_vel", "score_velocity", "breakdown_vel", "score_vel", "velocity_score", "vel", "velocity"],
        np.nan,
    )
    pen = first_existing(
        row,
        ["disp_pen", "score_penalty", "breakdown_pen", "score_pen", "penalty_score", "penalty", "pen"],
        np.nan,
    )

    rank = first_existing(row, ["rank", "ranking_rank", "disp_rank"], "-")
    chg = first_existing(row, ["change_rate", "chg", "ranking_change_rate", "disp_chg"], "-")
    turn = first_existing(row, ["turnover", "turn", "ranking_turnover", "disp_turn"], "-")
    tick = first_existing(row, ["tick", "tick_count", "ticks", "disp_tick"], "-")

    reason = _reason_text_for_discord(row, side)

    # Discordは等幅フォントで見やすくするため、銘柄名は少し長めに確保
    line1 = (
        f"{i:2d}. {str(symbol):<6} {str(symbolname):<22} "
        f"score={fmt_metric(score)} "
        f"buy={fmt_metric(score_buy)} "
        f"sell={fmt_metric(score_sell)} "
        f"total={fmt_metric(total)} "
        f"final={fmt_metric(final_score)} "
        f"close={fmt_price(close)} "
        f"rsi={fmt_metric(rsi)} "
        f"macd={fmt_metric(macd)}"
    )

    rank_text = str(rank)
    chg_text = fmt_metric(chg) if chg != "-" else "-"
    turn_text = fmt_metric(turn) if turn != "-" else "-"
    tick_text = fmt_metric(tick) if tick != "-" else "-"

    line2 = (
        f"    slope={fmt_metric(slope)} "
        f"mtf={fmt_metric(mtf)} "
        f"base={fmt_metric(base)} "
        f"trend={fmt_metric(trend)} "
        f"mom={fmt_metric(mom)} "
        f"vel={fmt_metric(vel)} "
        f"pen={fmt_metric(pen)} "
        f"rank={rank_text} "
        f"chg={chg_text} "
        f"turn={turn_text} "
        f"tick={tick_text} "
        f"理由={reason}"
    )

    return line1 + "\n" + line2

def _collect_discord_top10_sections(
    df: pd.DataFrame,
    interval_label: str,
    *,
    ranking: bool = False,
) -> list[str]:
    """
    Discord通知専用。
    ログ表示用 lines とは別に、1銘柄2行だけで作る。
    """

    lines: list[str] = []

    try:
        title_prefix = "RANKING SUMMARY" if ranking else "SUMMARY"

        lines.append(f"========== 📊 {title_prefix} TOP10 ({interval_label}) ==========")

        lines.append("🔵 BUY TOP10")
        buy_df = prepare_buy_df(df)
        if buy_df.empty:
            lines.append(" (no buy candidates)")
        else:
            for i, (_, row) in enumerate(buy_df.head(10).iterrows(), start=1):
                lines.append(_build_discord_candidate_2lines(i, row, side="BUY"))

        lines.append("🔴 SELL TOP10")
        sell_df = prepare_sell_df(df)
        if sell_df.empty:
            lines.append(" (no sell candidates)")
        else:
            for i, (_, row) in enumerate(sell_df.head(10).iterrows(), start=1):
                lines.append(_build_discord_candidate_2lines(i, row, side="SELL"))

    except Exception:
        logger.exception("[SUMMARY DISPLAY] _collect_discord_top10_sections failed")

    return lines


def _build_discord_ai_candidate_2lines(i: int, row: pd.Series, *, side: str) -> str:
    """
    AI通過銘柄用のDiscord 2行表示。
    既存のAI表示関数はコンソール用として残す。
    """

    symbol = first_existing(row, ["symbol"], "")
    symbolname = first_existing(row, ["symbolname_view", "symbolname", "name"], "")

    confidence = first_existing(row, ["confidence", "conf", "ai_confidence"], np.nan)
    lot = first_existing(row, ["lot", "order_lot", "qty"], np.nan)
    model = first_existing(row, ["model", "ai_model"], "-")

    score = first_existing(row, ["disp_score", "score", "display_score", "final_score"], np.nan)
    score_buy = first_existing(row, ["disp_buy_score", "score_buy"], np.nan)
    score_sell = first_existing(row, ["disp_sell_score", "score_sell"], np.nan)
    total = first_existing(row, ["disp_total_score", "score_total", "total_score"], np.nan)
    close = first_existing(row, ["disp_close", "close", "close_price", "current_price"], np.nan)

    reason = first_existing(row, ["ai_reason", "reason", "gate_reason"], "")
    if not reason:
        reason = _reason_text_for_discord(row, side)

    line1 = (
        f"{i:2d}. {str(symbol):<6} {str(symbolname):<18} "
        f"conf={fmt_confidence(confidence)} "
        f"lot={fmt_metric(lot)} "
        f"model={model} "
        f"close={fmt_price(close)}"
    )

    line2 = (
        f"    score={fmt_metric(score)} "
        f"buy={fmt_metric(score_buy)} "
        f"sell={fmt_metric(score_sell)} "
        f"total={fmt_metric(total)} "
        f"理由={reason}"
    )

    return line1 + "\n" + line2


def _collect_discord_ai_sections(df: pd.DataFrame, interval_label: str) -> list[str]:
    """
    Discord通知専用 AI セクション。
    """

    lines: list[str] = []

    try:
        lines.append("")
        lines.append(f"========== 🤖 AI PASSED BUY CANDIDATES ({interval_label}) ==========")
        buy_df = prepare_ai_buy_df(df)
        if buy_df.empty:
            lines.append(" (no ai-passed buy candidates)")
        else:
            for i, (_, row) in enumerate(buy_df.head(10).iterrows(), start=1):
                lines.append(_build_discord_ai_candidate_2lines(i, row, side="BUY"))

        lines.append(f"========== 🤖 AI PASSED SELL CANDIDATES ({interval_label}) ==========")
        sell_df = prepare_ai_sell_df(df)
        if sell_df.empty:
            lines.append(" (no ai-passed sell candidates)")
        else:
            for i, (_, row) in enumerate(sell_df.head(10).iterrows(), start=1):
                lines.append(_build_discord_ai_candidate_2lines(i, row, side="SELL"))

        lines.append(f"========== 🤖 AI PASSED EXIT CANDIDATES ({interval_label}) ==========")
        exit_df = prepare_ai_exit_df(df)
        if exit_df.empty:
            lines.append(" (no ai-passed exit candidates)")
        else:
            for i, (_, row) in enumerate(exit_df.head(10).iterrows(), start=1):
                lines.append(build_ai_exit_line(i, row))

    except Exception:
        logger.exception("[SUMMARY DISPLAY] _collect_discord_ai_sections failed")

    return lines


# ============================================================
# collect helpers for console / logger
# ============================================================

def _collect_ai_sections(df: pd.DataFrame, interval_label: str) -> list[str]:
    lines: list[str] = []

    try:
        lines.append("")
        lines.append(f"========== 🤖 AI PASSED BUY CANDIDATES ({interval_label}) ==========")
        buy_df = prepare_ai_buy_df(df)
        if buy_df.empty:
            lines.append(" (no ai-passed buy candidates)")
        else:
            for i, (_, row) in enumerate(buy_df.head(10).iterrows(), start=1):
                lines.append(build_ai_buy_line(i, row))

        lines.append(f"========== 🤖 AI PASSED SELL CANDIDATES ({interval_label}) ==========")
        sell_df = prepare_ai_sell_df(df)
        if sell_df.empty:
            lines.append(" (no ai-passed sell candidates)")
        else:
            for i, (_, row) in enumerate(sell_df.head(10).iterrows(), start=1):
                lines.append(build_ai_sell_line(i, row))

        lines.append(f"========== 🤖 AI PASSED EXIT CANDIDATES ({interval_label}) ==========")
        exit_df = prepare_ai_exit_df(df)
        if exit_df.empty:
            lines.append(" (no ai-passed exit candidates)")
        else:
            for i, (_, row) in enumerate(exit_df.head(10).iterrows(), start=1):
                lines.append(build_ai_exit_line(i, row))

    except Exception:
        logger.exception("[SUMMARY DISPLAY] _collect_ai_sections failed")

    return lines


def _emit_lines(lines: list[str]) -> None:
    for line in lines:
        print_line(line)


# ============================================================
# public printers
# ============================================================

def print_summary_top10(
    summary_df: pd.DataFrame,
    interval_label: str = "1min",
    *,
    notify_discord: bool = True,
) -> None:
    try:
        df = repair_mtf_consistency(safe_df(summary_df))
        df = ensure_display_columns(df)

        lines: list[str] = []

        header = build_header_context(df, interval_label)
        if header:
            lines.append("")
            lines.append(header)

        lines.append("")
        lines.append(f"========== 📊 SUMMARY TOP10 ({interval_label}) ==========")
        lines.append("🔵 BUY TOP10（score_buy 優先）")

        buy_df = prepare_buy_df(df)
        if buy_df.empty:
            lines.append(" (no buy candidates)")
        else:
            for i, (_, row) in enumerate(buy_df.head(10).iterrows(), start=1):
                lines.append(_build_buy_line(i, row))

        lines.append("🔴 SELL TOP10（score_sell 優先）")

        sell_df = prepare_sell_df(df)
        if sell_df.empty:
            lines.append(" (no sell candidates)")
        else:
            for i, (_, row) in enumerate(sell_df.head(10).iterrows(), start=1):
                lines.append(_build_sell_line(i, row))

        lines.extend(_collect_ai_sections(df, interval_label))

        # コンソール / logger は従来通り詳細表示
        _emit_lines(lines)

        # Discord は1銘柄2行の専用表示
        if notify_discord:
            discord_lines = _collect_discord_top10_sections(
                df,
                interval_label,
                ranking=False,
            )
            discord_lines.extend(_collect_discord_ai_sections(df, interval_label))
            _send_to_discord(discord_lines, title=f"📊 SUMMARY TOP10 {interval_label}")

    except Exception:
        logger.exception("[SUMMARY DISPLAY] print_summary_top10 failed")


def print_ranking_summary_top10(
    summary_df: pd.DataFrame,
    interval_label: str = "1min",
    *,
    notify_discord: bool = True,
) -> None:
    try:
        df = repair_mtf_consistency(safe_df(summary_df))
        df = ensure_display_columns(df)

        lines: list[str] = []

        header = latest_header_text(df, f"{interval_label} ランキングサマリー")
        if header:
            lines.append("")
            lines.append(header)

        lines.append("")
        lines.append(f"========== 📊 RANKING SUMMARY TOP10 ({interval_label}) ==========")
        lines.append("🔵 BUY TOP10（score_buy 優先）")

        buy_df = prepare_buy_df(df)
        if buy_df.empty:
            lines.append(" (no buy candidates)")
        else:
            for i, (_, row) in enumerate(buy_df.head(10).iterrows(), start=1):
                lines.append(_build_buy_line(i, row))

        lines.append("🔴 SELL TOP10（score_sell 優先）")

        sell_df = prepare_sell_df(df)
        if sell_df.empty:
            lines.append(" (no sell candidates)")
        else:
            for i, (_, row) in enumerate(sell_df.head(10).iterrows(), start=1):
                lines.append(_build_sell_line(i, row))

        lines.extend(_collect_ai_sections(df, interval_label))

        # コンソール / logger は従来通り詳細表示
        _emit_lines(lines)

        # Discord は1銘柄2行の専用表示
        if notify_discord:
            discord_lines = _collect_discord_top10_sections(
                df,
                interval_label,
                ranking=True,
            )
            discord_lines.extend(_collect_discord_ai_sections(df, interval_label))
            _send_to_discord(discord_lines, title=f"📊 RANKING SUMMARY TOP10 {interval_label}")

    except Exception:
        logger.exception("[SUMMARY DISPLAY] print_ranking_summary_top10 failed")


def print_push_summary(
    summary_df: pd.DataFrame | None = None,
    interval_label: str = "1min",
    *,
    notify_discord: bool = True,
    **kwargs,
) -> None:
    if summary_df is None:
        return
    print_summary_top10(
        summary_df=summary_df,
        interval_label=interval_label,
        notify_discord=notify_discord,
    )


def print_ranking_summary(
    summary_df: pd.DataFrame | None = None,
    interval_label: str = "1min",
    *,
    notify_discord: bool = True,
    **kwargs,
) -> None:
    if summary_df is None:
        return
    print_ranking_summary_top10(
        summary_df=summary_df,
        interval_label=interval_label,
        notify_discord=notify_discord,
    )


def display_ai_passed_summary(
    summary_df: pd.DataFrame | None = None,
    interval_label: str = "1min",
    *,
    notify_discord: bool = True,
    **kwargs,
) -> None:
    if summary_df is None:
        return

    try:
        df = repair_mtf_consistency(safe_df(summary_df))
        df = ensure_display_columns(df)

        # コンソール / logger は従来通り
        lines = _collect_ai_sections(df, interval_label)
        _emit_lines(lines)

        # Discord は2行版
        if notify_discord:
            discord_lines = _collect_discord_ai_sections(df, interval_label)
            _send_to_discord(discord_lines, title=f"🤖 AI PASSED {interval_label}")

    except Exception:
        logger.exception("[SUMMARY DISPLAY] display_ai_passed_summary failed")


# ============================================================
# compatibility wrappers
# ============================================================

def display_summary(
    summary_df: pd.DataFrame | None = None,
    interval_label: str = "1min",
    *,
    notify_discord: bool = True,
    **kwargs,
) -> None:
    if summary_df is None:
        return
    print_summary_top10(
        summary_df=summary_df,
        interval_label=interval_label,
        notify_discord=notify_discord,
    )


def display_push_summary(
    summary_df: pd.DataFrame | None = None,
    interval_label: str = "1min",
    *,
    notify_discord: bool = True,
    **kwargs,
) -> None:
    if summary_df is None:
        return
    print_summary_top10(
        summary_df=summary_df,
        interval_label=interval_label,
        notify_discord=notify_discord,
    )


def display_ranking_summary(
    summary_df: pd.DataFrame | None = None,
    interval_label: str = "1min",
    *,
    notify_discord: bool = True,
    **kwargs,
) -> None:
    if summary_df is None:
        return
    print_ranking_summary_top10(
        summary_df=summary_df,
        interval_label=interval_label,
        notify_discord=notify_discord,
    )