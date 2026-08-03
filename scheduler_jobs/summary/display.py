# ============================================================
# File   : scheduler_jobs/summary/display.py
# Function:
#   - public printer / compatibility 入口
#   - console / Discord SUMMARY TOP10 表示
#   - 1銘柄3行固定表示
#   - AI sections
#   - Discord 通知
# ------------------------------------------------------------
# Version: Ver11-INLINE-DISCORD-DISPLAY-COMPACT-KWARG-SAFETY-LABEL-GUARD
# ------------------------------------------------------------
# 目的:
#   Discord / console の SUMMARY TOP10 が横長1行にならないよう、
#   BUY/SELL候補を 1銘柄3行固定にする。
#   symbolname が空、または銘柄コードと同じ場合は global_data.symbol_name_map から補完する。
#
# 表示形式 (console):
#   🟦 1. 9632 スバル興業 Price=3695.0 Score=10.55 Buy=10.55 Sell=0.00
#      Slope=0.0300 MTF=0.00 RSI=50.00 MACD=0.00
#      理由=買いスコア優勢 buy=10.55 / 上向き傾き slope=0.0300 / スコア条件で抽出
#
# Ver11:
#   main.py / fast_startup_runtime_patch.py から起動していた3つの runtime patch
#   (discord_summary_display_compact_patch V1.6 / discord_summary_kwarg_safety_patch V2.7 /
#   summary_display_label_guard_patch V1.2) を本文へインライン化し、パッチファイル自体・
#   main.py / fast_startup_runtime_patch.py 側の install() 呼び出しを削除した。
#   3パッチが積み重なって実際に発火していた最終的な挙動をそのまま本文化している:
#     - print_summary_top10 / print_ranking_summary_top10 等は kwargs (interval= 等) を
#       受け取り、interval_label が無い/壊れている場合は kwargs から復元する
#       (旧: kwargs は **kwargs で握りつぶされ interval_label が常に既定値"1min"になるバグがあった)。
#     - 呼び出し引数が (interval_label, summary_df) の順で来た場合に summary_df を
#       失わないよう入れ替える (label_guard V1.2)。
#     - 定時表示で summary_df が空/Noneの場合、global_context から直近の完成済み
#       summaryを取り直す (kwarg_safety V2.7)。
#     - 結果時刻が古すぎるSUMMARYはDiscordへ送らない stale guard (kwarg_safety V2.7)。
#     - Discord BUY/SELL TOP10見出しに PUSH/ RANKING 由来とサマリー結果時刻を付記する
#       (label_guard V1.1)。
#     - Discord候補行 (_build_discord_candidate_2lines) は Slope を小数4桁で表示する
#       (kwarg_safety が最後に上書きしていたため; console側の3行表示は従来通り2桁)。
#     - Discord AI候補行 (_build_discord_ai_candidate_2lines) は
#       "conf=... lot=... 株価=..." の日本語ラベル版レイアウトを使う
#       (discord_summary_display_compact_patch が最後に上書きしていたため)。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import re

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


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


# ============================================================
# Discord sender
# ============================================================

def _discord_available() -> bool:
    return callable(send_discord_message)


def _extract_interval_min_from_text(text: str) -> int:
    try:
        m = re.search(r"(?:TOP10|SUMMARY TOP10)\s*\(?\s*(\d+)\s*(?:min|m|分)?", text, flags=re.I)
        if m:
            return max(1, int(m.group(1)))
        m = re.search(r"\((\d+)\s*(?:min|m|分)?\)", text, flags=re.I)
        if m:
            return max(1, int(m.group(1)))
    except Exception:
        pass
    return 1


def _extract_result_dt_from_text(text: str) -> dt.datetime | None:
    try:
        m = re.search(r"結果時刻\s*=\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})", text)
        if not m:
            return None
        return dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _stale_limit_min(interval_min: int) -> float:
    override = os.getenv("SUMMARY_DISCORD_STALE_LIMIT_MIN")
    if override is not None and str(override).strip() != "":
        return _env_float("SUMMARY_DISCORD_STALE_LIMIT_MIN", 8.0)
    if interval_min <= 1:
        return _env_float("SUMMARY_DISCORD_STALE_LIMIT_1MIN", 4.0)
    if interval_min <= 3:
        return _env_float("SUMMARY_DISCORD_STALE_LIMIT_3MIN", 8.0)
    if interval_min <= 5:
        return _env_float("SUMMARY_DISCORD_STALE_LIMIT_5MIN", 12.0)
    return _env_float("SUMMARY_DISCORD_STALE_LIMIT_OTHER", 15.0)


def _should_skip_stale_discord(lines: list[str], title: str | None) -> tuple[bool, str]:
    try:
        if not _env_bool("SUMMARY_DISCORD_STALE_GUARD", True):
            return False, "disabled"
        text = (str(title or "") + "\n" + "\n".join(str(x) for x in (lines or [])))[:6000]
        if "SUMMARY TOP10" not in text and "PUSH SUMMARY TOP10" not in text and "RANKING SUMMARY TOP10" not in text:
            return False, "not_summary"
        result_dt = _extract_result_dt_from_text(text)
        if result_dt is None:
            return False, "no_result_time"
        interval_min = _extract_interval_min_from_text(text)
        now = dt.datetime.now().replace(tzinfo=None, microsecond=0)
        age_min = (now - result_dt).total_seconds() / 60.0
        limit = _stale_limit_min(interval_min)
        if age_min > limit:
            return True, f"result_dt={result_dt} now={now} age_min={age_min:.1f} limit_min={limit:.1f} interval={interval_min}"
        return False, f"fresh age_min={age_min:.1f} limit_min={limit:.1f} interval={interval_min}"
    except Exception as e:
        return False, f"guard_error={e}"


def _send_to_discord(lines: list[str], title: str | None = None) -> None:
    try:
        if not _discord_available():
            logger.info("[SUMMARY DISPLAY] discord sender not available")
            return

        skip, reason = _should_skip_stale_discord(lines or [], title)
        if skip:
            logger.warning("[SUMMARY DISPLAY] skip stale summary discord %s", reason)
            return
        logger.info("[SUMMARY DISPLAY] allow summary discord %s", reason)

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


def _fmt_metric_digits(v, digits: int = 2) -> str:
    try:
        x = float(v)
        if not np.isfinite(x):
            return "-"
        return f"{x:.{digits}f}"
    except Exception:
        return "-"


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


def _discord_reason_ja(row: pd.Series, side: str) -> str:
    """Discord候補行専用の日本語理由 (旧 discord_summary_kwarg_safety_patch._reason_ja)。"""
    try:
        side_u = str(side or "").upper()
        parts: list[str] = []
        buy = _num(first_existing(row, ["disp_buy_score", "score_buy", "buy_score"], 0.0))
        sell = _num(first_existing(row, ["disp_sell_score", "score_sell", "sell_score"], 0.0))
        slope = _num(first_existing(row, ["disp_slope", "slope", "score_slope", "slope_atr_scaled"], 0.0))
        mtf = _num(first_existing(row, ["disp_mtf", "mtf", "score_mtf", "mtf_score"], 0.0))
        rsi = _num(first_existing(row, ["disp_rsi", "rsi"], 50.0), 50.0)
        macd = _num(first_existing(row, ["disp_macd", "macd"], 0.0))
        if side_u == "BUY":
            if buy > 0:
                parts.append(f"買いスコア優勢 buy={buy:.2f}")
            parts.append(f"上向き傾き slope={slope:.4f}" if slope > 0 else f"傾きは弱い slope={slope:.4f}")
        else:
            if sell > 0:
                parts.append(f"売りスコア優勢 sell={sell:.2f}")
            parts.append(f"下向き傾き slope={slope:.4f}" if slope < 0 else f"下落傾きは弱い slope={slope:.4f}")
        if mtf:
            parts.append(f"複数時間足={mtf:.2f}")
        if rsi != 50.0:
            parts.append(f"RSI={rsi:.1f}")
        if macd:
            parts.append(f"MACD={macd:.3f}")
        code_reason = _clean(first_existing(row, ["reason", "entry_reason", "flag_reason", "signal_reason"], ""), max_len=40)
        if code_reason and code_reason not in {"-", "flag_score"}:
            parts.append(f"元理由={code_reason}")
        elif code_reason == "flag_score":
            parts.append("スコア条件で抽出")
        return " / ".join(parts) if parts else "理由データ不足: スコア・傾き・補助指標から判定"
    except Exception:
        logger.debug("[SUMMARY DISPLAY] _discord_reason_ja failed", exc_info=True)
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
    return _discord_reason_ja(row, side)


def _build_discord_candidate_2lines(i: int, row: pd.Series, *, side: str) -> str:
    symbol = _clean(first_existing(row, ["symbol"], ""), max_len=8)
    name = _clean(_resolve_symbol_name(row, first_existing(row, ["symbol"], "")), max_len=18)
    score = first_existing(row, ["disp_score", "score", "display_score", "final_score"], np.nan)
    buy = first_existing(row, ["disp_buy_score", "score_buy", "buy_score"], np.nan)
    sell = first_existing(row, ["disp_sell_score", "score_sell", "sell_score"], np.nan)
    close = first_existing(row, ["disp_close", "close", "close_price", "current_price", "price"], np.nan)
    slope = first_existing(row, ["disp_slope", "slope", "score_slope", "slope_atr_scaled"], np.nan)
    mtf = first_existing(row, ["disp_mtf", "mtf", "score_mtf", "mtf_score"], np.nan)
    rsi = first_existing(row, ["disp_rsi", "rsi"], np.nan)
    macd = first_existing(row, ["disp_macd", "macd"], np.nan)
    reason = _discord_reason_ja(row, side)
    mark = "🟦" if str(side).upper() == "BUY" else "🟥"
    return (
        f"{mark} {i}. {symbol} {name} Price={fmt_price(close)} Score={fmt_metric(score)} Buy={fmt_metric(buy)} Sell={fmt_metric(sell)}\n"
        f"   Slope={_fmt_metric_digits(slope, 4)} MTF={fmt_metric(mtf)} RSI={fmt_metric(rsi)} MACD={fmt_metric(macd)}\n"
        f"   理由={reason}"
    )


def _summary_source_label_for_discord(ranking: bool) -> str:
    return "ランキング由来サマリー" if bool(ranking) else "PUSH由来サマリー"


def _latest_summary_time_for_discord(df: pd.DataFrame) -> str:
    """Discord表示用に、DataFrame内の最新サマリー時刻を抽出する。"""
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return "-"
        candidates = [
            "summary_datetime", "summary_dt", "bar_datetime", "datetime", "dt",
            "timestamp", "time", "saved_at", "updated_at", "created_at",
        ]
        for col in candidates:
            if col not in df.columns:
                continue
            try:
                s = pd.to_datetime(df[col], errors="coerce")
                if s.notna().any():
                    mx = s.max()
                    if pd.notna(mx):
                        return mx.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
    except Exception:
        logger.debug("[SUMMARY DISPLAY] latest summary time detect failed", exc_info=True)
    return "-"


def _collect_discord_top10_sections(
    df: pd.DataFrame,
    interval_label: str,
    *,
    ranking: bool = False,
) -> list[str]:
    lines: list[str] = []
    try:
        source_label = _summary_source_label_for_discord(ranking)
        summary_time = _latest_summary_time_for_discord(df)
        title_prefix = "RANKING SUMMARY" if ranking else "PUSH SUMMARY"
        meta = f"{source_label} / 結果時刻={summary_time}"

        lines.append(f"========== 📊 {title_prefix} TOP10 ({interval_label}) / {meta} ==========")

        lines.append(f"🔵 BUY TOP10【{meta}】")
        buy_df = prepare_buy_df(df)
        if buy_df.empty:
            lines.append(" (no buy candidates)")
        else:
            for i, (_, row) in enumerate(buy_df.head(10).iterrows(), start=1):
                lines.append(_build_discord_candidate_2lines(i, row, side="BUY"))

        lines.append(f"🔴 SELL TOP10【{meta}】")
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
    symbol = _clean(first_existing(row, ["symbol"], ""), max_len=8)
    name = _clean(_resolve_symbol_name(row, first_existing(row, ["symbol"], "")), max_len=18)
    conf = first_existing(row, ["confidence", "conf", "ai_confidence"], np.nan)
    conf_text = fmt_confidence(conf)
    lot = fmt_metric(first_existing(row, ["lot", "order_lot", "qty", "lot_multiplier"], np.nan))
    close = fmt_price(first_existing(row, ["disp_close", "close", "close_price", "current_price", "price"], np.nan))
    score = fmt_metric(first_existing(row, ["disp_score", "score", "display_score", "final_score"], np.nan))
    buy = fmt_metric(first_existing(row, ["disp_buy_score", "score_buy", "buy_score"], np.nan))
    sell = fmt_metric(first_existing(row, ["disp_sell_score", "score_sell", "sell_score"], np.nan))
    slope = fmt_metric(first_existing(row, ["disp_slope", "slope", "score_slope", "slope_atr_scaled"], np.nan))
    reason = _clean(
        first_existing(row, ["ai_reason", "reason", "gate_reason"], "") or _discord_reason_ja(row, side),
        max_len=120,
    )
    mark = "🤖🟦" if str(side).upper() == "BUY" else "🤖🟥"
    return (
        f"{mark} {i}. {symbol} {name}\n"
        f"   conf={conf_text} lot={lot} 株価={close} score={score} buy={buy} sell={sell} slope={slope}\n"
        f"   理由={reason or '-'}"
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
# call-argument normalization (旧 summary_display_label_guard_patch +
# discord_summary_kwarg_safety_patch)
# ============================================================

def _is_df_like(v) -> bool:
    try:
        return isinstance(v, pd.DataFrame)
    except Exception:
        return False


def _looks_bad_label(v) -> bool:
    try:
        if v is None:
            return False
        if isinstance(v, (pd.DataFrame, pd.Series, dict, list, tuple, set)):
            return True
        s = str(v)
        if " rows x " in s and "columns" in s:
            return True
        if "   symbol" in s and "datetime" in s:
            return True
        if len(s) > 80:
            return True
        return False
    except Exception:
        return True


def _label_from_value(v, default: str | None = None) -> str | None:
    if v is None or _looks_bad_label(v):
        return default
    try:
        return f"{int(float(v))}min"
    except Exception:
        s = str(v).strip()
        if not s or len(s) > 40 or "\n" in s:
            return default
        return s


def _label_from_kwargs(kwargs: dict, default: str = "1min") -> str:
    for k in ("interval_label", "interval", "interval_min", "minutes", "tf"):
        if k in kwargs:
            label = _label_from_value(kwargs.get(k))
            if label:
                return label
    return default


def _normalize_display_call(summary_df, interval_label, kwargs: dict) -> tuple:
    """呼び出し引数を正規化する。

    - (interval_label, summary_df) の順で positional 呼び出しされた場合、
      summary_df を失わずに入れ替える。
    - interval_label が DataFrame 等の壊れた値、または未指定の場合は
      kwargs (interval= 等) から復元する。
    - interval/interval_min/minutes/tf は summary_df 側の関数シグネチャに
      存在しないため kwargs から取り除く。
    """
    kw = dict(kwargs or {})

    if _is_df_like(interval_label) and not _is_df_like(summary_df):
        label = _label_from_value(summary_df, _label_from_kwargs(kw, default="1min"))
        summary_df, interval_label = interval_label, (label or "1min")
        logger.warning(
            "[SUMMARY DISPLAY] swapped positional summary_df/interval_label fixed_label=%s rows=%s",
            interval_label,
            len(summary_df) if hasattr(summary_df, "__len__") else "-",
        )
    else:
        # kwargs (interval= 等) が渡っていれば、既定値の interval_label より優先する。
        # 呼び出し元の多くは interval_label ではなく interval=... で渡すため。
        old_label = interval_label
        label = _label_from_kwargs(kw, default=None)
        interval_label = label or _label_from_value(interval_label, "1min") or "1min"
        if interval_label != old_label:
            logger.warning("[SUMMARY DISPLAY] resolved interval_label old=%r new=%s", old_label, interval_label)

    for k in ("interval", "interval_min", "minutes", "tf", "interval_label"):
        kw.pop(k, None)

    return summary_df, interval_label, kw


def _df_empty(v) -> bool:
    try:
        return (v is None) or (_is_df_like(v) and bool(v.empty))
    except Exception:
        return True


def _fallback_summary_df(summary_df, interval_label: str, fn_name: str, kwargs: dict):
    """定時表示でNone/空DFが来た時に、直近の完成済みsummaryを取り直す。"""
    if not _df_empty(summary_df):
        return summary_df
    if not _env_bool("SUMMARY_DISPLAY_FALLBACK_ENABLED", True):
        return summary_df

    try:
        interval_min = int(re.search(r"(\d+)", str(interval_label or "1")).group(1))
    except Exception:
        interval_min = 1
    source_hint = str((kwargs or {}).get("source") or "").strip().lower()
    if not source_hint:
        source_hint = "ranking" if "ranking" in str(fn_name).lower() else "push"

    try:
        from core.global_context import context as gc
    except Exception:
        logger.debug("[SUMMARY DISPLAY] global_context import failed", exc_info=True)
        return summary_df

    candidates: list[tuple[str, object]] = []
    if source_hint == "ranking":
        candidates.extend([
            ("gc.get_ranking_summary", getattr(gc, "get_ranking_summary", None)),
            ("gc.get_merged_summary", getattr(gc, "get_merged_summary", None)),
        ])
    else:
        candidates.extend([
            ("gc.get_push_summary", getattr(gc, "get_push_summary", None)),
            ("gc.get_merged_summary", getattr(gc, "get_merged_summary", None)),
            ("gc.get_summary", getattr(gc, "get_summary", None)),
        ])

    for label, fn in candidates:
        if not callable(fn):
            continue
        call_patterns = [
            ((interval_min,), {"source": source_hint}),
            ((), {"tf": interval_min, "source": source_hint}),
            ((), {"interval": interval_min, "source": source_hint}),
            ((), {"interval_min": interval_min, "source": source_hint}),
            ((interval_min,), {}),
            ((), {"tf": interval_min}),
            ((), {"interval": interval_min}),
            ((), {"interval_min": interval_min}),
        ]
        for args, kw in call_patterns:
            try:
                df = fn(*args, **kw)
            except TypeError:
                continue
            except Exception:
                logger.debug("[SUMMARY DISPLAY] fallback candidate call failed fn=%s", label, exc_info=True)
                continue
            if not _df_empty(df):
                logger.warning(
                    "[SUMMARY DISPLAY] recovered summary fn=%s source=%s interval=%s rows=%s",
                    label, source_hint, interval_min, len(df) if hasattr(df, "__len__") else "?",
                )
                return df

    logger.warning(
        "[SUMMARY DISPLAY] no completed summary available fn=print_summary_top10 source=%s interval=%s",
        source_hint, interval_min,
    )
    return summary_df


# ============================================================
# public printers
# ============================================================

def print_summary_top10(
    summary_df: pd.DataFrame,
    interval_label: str = "1min",
    *,
    notify_discord: bool = True,
    **kwargs,
) -> None:
    try:
        summary_df, interval_label, kwargs = _normalize_display_call(summary_df, interval_label, kwargs)
        summary_df = _fallback_summary_df(summary_df, interval_label, "print_summary_top10", kwargs)

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
    **kwargs,
) -> None:
    try:
        summary_df, interval_label, kwargs = _normalize_display_call(summary_df, interval_label, kwargs)
        summary_df = _fallback_summary_df(summary_df, interval_label, "print_ranking_summary_top10", kwargs)

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
    summary_df, interval_label, kwargs = _normalize_display_call(summary_df, interval_label, kwargs)
    if summary_df is None:
        return
    print_summary_top10(summary_df=summary_df, interval_label=interval_label, notify_discord=notify_discord, **kwargs)


def print_ranking_summary(
    summary_df: pd.DataFrame | None = None,
    interval_label: str = "1min",
    *,
    notify_discord: bool = True,
    **kwargs,
) -> None:
    summary_df, interval_label, kwargs = _normalize_display_call(summary_df, interval_label, kwargs)
    if summary_df is None:
        return
    print_ranking_summary_top10(summary_df=summary_df, interval_label=interval_label, notify_discord=notify_discord, **kwargs)


def display_ai_passed_summary(
    summary_df: pd.DataFrame | None = None,
    interval_label: str = "1min",
    *,
    notify_discord: bool = True,
    **kwargs,
) -> None:
    summary_df, interval_label, kwargs = _normalize_display_call(summary_df, interval_label, kwargs)
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
    summary_df, interval_label, kwargs = _normalize_display_call(summary_df, interval_label, kwargs)
    if summary_df is None:
        return
    print_summary_top10(summary_df=summary_df, interval_label=interval_label, notify_discord=notify_discord, **kwargs)


def display_push_summary(
    summary_df: pd.DataFrame | None = None,
    interval_label: str = "1min",
    *,
    notify_discord: bool = True,
    **kwargs,
) -> None:
    summary_df, interval_label, kwargs = _normalize_display_call(summary_df, interval_label, kwargs)
    if summary_df is None:
        return
    print_summary_top10(summary_df=summary_df, interval_label=interval_label, notify_discord=notify_discord, **kwargs)


def display_ranking_summary(
    summary_df: pd.DataFrame | None = None,
    interval_label: str = "1min",
    *,
    notify_discord: bool = True,
    **kwargs,
) -> None:
    summary_df, interval_label, kwargs = _normalize_display_call(summary_df, interval_label, kwargs)
    if summary_df is None:
        return
    print_ranking_summary_top10(summary_df=summary_df, interval_label=interval_label, notify_discord=notify_discord, **kwargs)
