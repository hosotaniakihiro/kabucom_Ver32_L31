# ============================================================
# File   : scheduler_jobs/summary/display.py
# Function:
#   - public printer / compatibility 入口
#   - console / Discord SUMMARY TOP10 表示
#   - 1銘柄3行固定表示
#   - AI sections
#   - Discord 通知
# ------------------------------------------------------------
# Version: Ver10.5-PRODUCTION-DISPLAY-3LINES-SYMBOLNAME-FALLBACK
# ------------------------------------------------------------
# 目的:
#   Discord / console の SUMMARY TOP10 が横長1行にならないよう、
#   BUY/SELL候補を 1銘柄3行固定にする。
#   symbolname が空、または銘柄コードと同じ場合は global_data.symbol_name_map から補完する。
#
# 表示形式:
#   🟦 1. 9632 スバル興業 Price=3695.0 Score=10.55 Buy=10.55 Sell=0.00
#      Slope=0.0300 MTF=0.00 RSI=50.00 MACD=0.00
#      理由=買いスコア優勢 buy=10.55 / 上向き傾き slope=0.0300 / スコア条件で抽出
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
                logger.exception("[SUMMARY DISPLAY] discord send failed chunk=%s/%s", idx, len(chunks))

    except Exception:
        logger.exception("[SUMMARY DISPLAY] _send_to_discord failed")


# ============================================================
# helpers
# ============================================================

def _clean(v, max_len: int = 24) -> str:
    try:
        s = str(v if v is not None else "").replace("\r", " ").replace("\n", " ").strip()
        if len(s) > max_len:
            return s[: max_len - 1] + "…"
        return s
    except Exception:
        return ""


def _symbol_key(v) -> str:
    try:
        s = str(v if v is not None else "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _invalid_symbol_name(symbol, name) -> bool:
    try:
        sym = _symbol_key(symbol)
        nm = str(name if name is not None else "").strip()
        if not nm or nm.lower() in {"nan", "none", "null", "-"}:
            return True
        return _symbol_key(nm) == sym
    except Exception:
        return True


def _lookup_symbol_name(symbol) -> str:
    sym = _symbol_key(symbol)
    if not sym:
        return ""
    try:
        from global_state import global_data
        mp = getattr(global_data, "symbol_name_map", None)
        if isinstance(mp, dict) and mp:
            for key in (sym, str(sym), f"{sym}.0"):
                v = mp.get(key)
                if v is not None and str(v).strip() and str(v).strip().lower() not in {"nan", "none"}:
                    return str(v).strip()
    except Exception:
        logger.debug("[SUMMARY DISPLAY] symbol_name_map lookup failed symbol=%s", sym, exc_info=True)
    return ""


def _resolve_symbol_name(row: pd.Series, symbol) -> str:
    raw = first_existing(row, ["symbolname_view", "symbolname", "name", "company_name", "銘柄名", "銘柄名称"], "")
    if not _invalid_symbol_name(symbol, raw):
        return str(raw).strip()
    mapped = _lookup_symbol_name(symbol)
    if mapped:
        return mapped
    return ""


def _num(v, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() in {"", "-", "nan", "None"}:
            return default
        x = float(v)
        return x if np.isfinite(x) else default
    except Exception:
        return default


def _strip_reason_prefix(raw: str) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    if "=" in s:
        s = s.split("=", 1)[1].strip()
    return s


def _score_reason_ja(row: pd.Series, side: str) -> str:
    """SUMMARY表示用の日本語理由。長すぎないよう主要項目に絞る。"""
    try:
        side_u = str(side or "").upper()
        buy = _num(first_existing(row, ["disp_buy_score", "score_buy", "buy_score"], 0.0))
        sell = _num(first_existing(row, ["disp_sell_score", "score_sell", "sell_score"], 0.0))
        slope = _num(first_existing(row, ["disp_slope", "slope", "score_slope", "slope_atr_scaled"], 0.0))
        mtf = _num(first_existing(row, ["disp_mtf", "mtf", "score_mtf", "mtf_score"], 0.0))
        rsi = _num(first_existing(row, ["disp_rsi", "rsi"], 50.0), 50.0)
        macd = _num(first_existing(row, ["disp_macd", "macd"], 0.0))

        parts: list[str] = []
        if side_u == "SELL":
            if sell > 0:
                parts.append(f"売りスコア優勢 sell={sell:.2f}")
            if slope < 0:
                parts.append(f"下向き傾き slope={slope:.4f}")
            else:
                parts.append(f"下落傾きは弱い slope={slope:.4f}")
        else:
            if buy > 0:
                parts.append(f"買いスコア優勢 buy={buy:.2f}")
            if slope > 0:
                parts.append(f"上向き傾き slope={slope:.4f}")
            else:
                parts.append(f"傾きは弱い slope={slope:.4f}")

        if mtf:
            parts.append(f"複数時間足={mtf:.2f}")
        if rsi != 50.0:
            parts.append(f"RSI={rsi:.1f}")
        if macd:
            parts.append(f"MACD={macd:.3f}")

        raw = build_sell_reason_line(row) if side_u == "SELL" else build_buy_reason_line(row)
        raw_reason = _strip_reason_prefix(raw)
        if raw_reason and raw_reason not in {"-", "flag_score"}:
            if raw_reason not in " / ".join(parts):
                parts.append(raw_reason)
        elif raw_reason == "flag_score":
            parts.append("スコア条件で抽出")

        return " / ".join(parts) if parts else "理由データ不足: スコア・傾き・補助指標から判定"
    except Exception:
        logger.debug("[SUMMARY DISPLAY] _score_reason_ja failed", exc_info=True)
        return "理由生成失敗"


# ============================================================
# line builders for console / logger / Discord
# ============================================================

def _build_summary_candidate_3lines(i: int, row: pd.Series, *, side: str) -> str:
    symbol = first_existing(row, ["symbol"], "")
    symbolname = _resolve_symbol_name(row, symbol)

    score = first_existing(row, ["disp_score", "score", "display_score", "final_score"], np.nan)
    score_buy = first_existing(row, ["disp_buy_score", "score_buy", "buy_score"], np.nan)
    score_sell = first_existing(row, ["disp_sell_score", "score_sell", "sell_score"], np.nan)
    close = first_existing(row, ["disp_close", "close", "close_price", "current_price", "price"], np.nan)
    slope = first_existing(row, ["disp_slope", "slope", "score_slope", "slope_atr_scaled"], np.nan)
    mtf = first_existing(row, ["disp_mtf", "mtf", "score_mtf", "mtf_score"], np.nan)
    rsi = first_existing(row, ["disp_rsi", "rsi"], np.nan)
    macd = first_existing(row, ["disp_macd", "macd"], np.nan)

    mark = "🟦" if str(side).upper() == "BUY" else "🟥"
    reason = _score_reason_ja(row, side)

    return (
        f"{mark} {i}. {_clean(symbol, 8)} {_clean(symbolname, 28)} "
        f"Price={fmt_price(close)} Score={fmt_metric(score)} Buy={fmt_metric(score_buy)} Sell={fmt_metric(score_sell)}\n"
        f"   Slope={fmt_metric(slope)} MTF={fmt_metric(mtf)} RSI={fmt_metric(rsi)} MACD={fmt_metric(macd)}\n"
        f"   理由={reason}"
    )


def _build_buy_line(i: int, row: pd.Series) -> str:
    return _build_summary_candidate_3lines(i, row, side="BUY")


def _build_sell_line(i: int, row: pd.Series) -> str:
    return _build_summary_candidate_3lines(i, row, side="SELL")


def _reason_text_for_discord(row: pd.Series, side: str) -> str:
    return _score_reason_ja(row, side)


def _build_discord_candidate_2lines(i: int, row: pd.Series, *, side: str) -> str:
    return _build_summary_candidate_3lines(i, row, side=side)


def _collect_discord_top10_sections(
    df: pd.DataFrame,
    interval_label: str,
    *,
    ranking: bool = False,
) -> list[str]:
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
    symbol = first_existing(row, ["symbol"], "")
    symbolname = _resolve_symbol_name(row, symbol)
    confidence = first_existing(row, ["confidence", "conf", "ai_confidence"], np.nan)
    lot = first_existing(row, ["lot", "order_lot", "qty"], np.nan)
    score = first_existing(row, ["disp_score", "score", "display_score", "final_score"], np.nan)
    score_buy = first_existing(row, ["disp_buy_score", "score_buy", "buy_score"], np.nan)
    score_sell = first_existing(row, ["disp_sell_score", "score_sell", "sell_score"], np.nan)
    close = first_existing(row, ["disp_close", "close", "close_price", "current_price", "price"], np.nan)
    slope = first_existing(row, ["disp_slope", "slope", "score_slope", "slope_atr_scaled"], np.nan)

    reason = first_existing(row, ["ai_reason", "reason", "gate_reason"], "")
    if not reason:
        reason = _reason_text_for_discord(row, side)

    mark = "🤖🟦" if str(side).upper() == "BUY" else "🤖🟥"
    return (
        f"{mark} {i}. {_clean(symbol, 8)} {_clean(symbolname, 24)} "
        f"Price={fmt_price(close)} Score={fmt_metric(score)} Buy={fmt_metric(score_buy)} Sell={fmt_metric(score_sell)}\n"
        f"   Conf={fmt_confidence(confidence)} Lot={fmt_metric(lot)} Slope={fmt_metric(slope)}\n"
        f"   理由={reason}"
    )


def _collect_discord_ai_sections(df: pd.DataFrame, interval_label: str) -> list[str]:
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
        _emit_lines(lines)

        if notify_discord:
            discord_lines = _collect_discord_top10_sections(df, interval_label, ranking=False)
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
        _emit_lines(lines)

        if notify_discord:
            discord_lines = _collect_discord_top10_sections(df, interval_label, ranking=True)
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
    print_summary_top10(summary_df=summary_df, interval_label=interval_label, notify_discord=notify_discord)


def print_ranking_summary(
    summary_df: pd.DataFrame | None = None,
    interval_label: str = "1min",
    *,
    notify_discord: bool = True,
    **kwargs,
) -> None:
    if summary_df is None:
        return
    print_ranking_summary_top10(summary_df=summary_df, interval_label=interval_label, notify_discord=notify_discord)


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
        lines = _collect_ai_sections(df, interval_label)
        _emit_lines(lines)
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
    print_summary_top10(summary_df=summary_df, interval_label=interval_label, notify_discord=notify_discord)


def display_push_summary(
    summary_df: pd.DataFrame | None = None,
    interval_label: str = "1min",
    *,
    notify_discord: bool = True,
    **kwargs,
) -> None:
    if summary_df is None:
        return
    print_summary_top10(summary_df=summary_df, interval_label=interval_label, notify_discord=notify_discord)


def display_ranking_summary(
    summary_df: pd.DataFrame | None = None,
    interval_label: str = "1min",
    *,
    notify_discord: bool = True,
    **kwargs,
) -> None:
    if summary_df is None:
        return
    print_ranking_summary_top10(summary_df=summary_df, interval_label=interval_label, notify_discord=notify_discord)
