# ============================================================
# File   : trading/ranking/summary/announce.py
# Version: Ver1.4-PRODUCTION-RANKING-SUMMARY-ANNOUNCE-DISCORD-FIX
#          -PUSH-LIKE-BUY-SELL-TOP10-DISPLAY
# ------------------------------------------------------------
# ranking summary 用 announce / console text / announced state 管理
# ranking_summary_engine.py から安全に切り出すためのモジュール
#
# Ver1.4 変更:
#   - _try_send_discord_message の二重定義を解消
#   - 消えていた announce_ranking_summary 本体を復元
#   - Discord 通知をデフォルト ON に変更
#   - alerts_util.py / utils.alerts_util / discord_notifier 系に対応
#   - Discord 2000文字制限対策として 1800文字単位で分割送信
#   - Discord送信結果ログを強化
#
# Ver1.3 変更:
#   - PUSH由来サマリーと同様に BUY TOP10 / SELL TOP10 を表示
#   - 価格 close は小数点第1位まで
#   - score / slope / mtf / rsi / macd は小数点第2位まで
#   - 0.0 の slope / mtf / macd は "-" 表示
#   - score_sell がある場合は SELL TOP10 を score_sell 降順で表示
#   - score_sell がない場合は暫定で score_buy / ranking_score の低い順を SELL 表示
#   - 既存の announce_ranking_summary(interval, topn, use_discord) API は維持
#
# 既存機能:
#   - 実質空データの announce 抑止
#   - timestamp 未解決時の announce 抑止
#   - close / score / type が全部空の placeholder TOP10 を抑止
#   - 可能なら ETF/ETN/REIT/FUND 系を表示から除外
#   - technicals.py 側 indicator mode と連携
#   - slope 表示を slope / slope_atr_scaled の両対応
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Optional

import pandas as pd

from trading.ranking.summary.cache_store import (
    _ensure_global_slots,
    get_latest_ranking_summary,
)

logger = logging.getLogger(__name__)


# ============================================================
# global_data 互換解決
# ============================================================

try:
    from global_state import global_data  # type: ignore
except Exception:
    try:
        from core.global_context import global_data  # type: ignore
    except Exception:
        class _FallbackGlobalData:
            pass

        global_data = _FallbackGlobalData()


# ============================================================
# indicator mode state
# technicals.py 側と連携
# ============================================================

_LAST_INDICATOR_MODE = "unresolved"  # external / fallback / unresolved


def set_indicator_mode(mode: str) -> None:
    global _LAST_INDICATOR_MODE

    try:
        m = str(mode).strip().lower()
        if not m:
            return
        _LAST_INDICATOR_MODE = m
    except Exception:
        logger.exception("[RANKING SUMMARY] set indicator mode failed")

    try:
        from trading.ranking.summary.technicals import set_indicator_mode as _set_mode_technical

        if callable(_set_mode_technical):
            _set_mode_technical(mode)
    except Exception:
        logger.debug(
            "[RANKING SUMMARY] propagate indicator mode to technicals failed",
            exc_info=True,
        )


def get_indicator_mode() -> str:
    try:
        from trading.ranking.summary.technicals import get_indicator_mode as _get_mode_technical

        if callable(_get_mode_technical):
            m = str(_get_mode_technical()).strip().lower()
            if m:
                return m
    except Exception:
        logger.debug(
            "[RANKING SUMMARY] read indicator mode from technicals failed",
            exc_info=True,
        )

    try:
        return str(_LAST_INDICATOR_MODE)
    except Exception:
        return "unresolved"


# ============================================================
# basic helpers
# ============================================================

def _safe_str(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.lower() in {"nan", "none", "nat"}:
            return ""
        return s
    except Exception:
        return ""


def _normalize_symbol(v: Any) -> str:
    s = _safe_str(v)
    if not s:
        return ""
    if "." in s:
        s = s.split(".", 1)[0].strip()
    return s


def _normalize_symbolname(v: Any) -> str:
    try:
        if v is None:
            return ""
        if pd.isna(v):
            return ""
        s = str(v).strip()
        if not s:
            return ""
        if s.lower() in {"nan", "none", "nat"}:
            return ""
        if s == "0":
            return ""
        return s
    except Exception:
        return ""


def _display_symbolname(row: pd.Series) -> str:
    name = _normalize_symbolname(row.get("symbolname"))
    if name:
        return name
    return _normalize_symbol(row.get("symbol"))


def _coerce_numeric(series: pd.Series | Any, default: float = 0.0) -> pd.Series:
    try:
        if isinstance(series, pd.Series):
            return pd.to_numeric(series, errors="coerce").fillna(default)
    except Exception:
        pass

    try:
        return pd.Series(dtype="float64")
    except Exception:
        return pd.Series([])


def _safe_to_numeric_value(v: Any) -> Any:
    try:
        return pd.to_numeric(v, errors="coerce")
    except Exception:
        return pd.NA


def _has_non_null_numeric(row: pd.Series, col: str) -> bool:
    if col not in row.index:
        return False

    try:
        v = pd.to_numeric(row.get(col), errors="coerce")
        return not pd.isna(v)
    except Exception:
        return False


# ============================================================
# best rank repair
# ============================================================

def _repair_best_rank_for_display(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame() if df is None else df.copy()

    x = df.copy()

    if "best_rank_position" not in x.columns:
        x["best_rank_position"] = pd.NA

    try:
        x["best_rank_position"] = pd.to_numeric(
            x["best_rank_position"],
            errors="coerce",
        )
    except Exception:
        x["best_rank_position"] = pd.NA

    for fallback_col in ("last_rank_position", "avg_rank_position", "rank_position"):
        if fallback_col in x.columns:
            try:
                alt = pd.to_numeric(x[fallback_col], errors="coerce")
                x["best_rank_position"] = x["best_rank_position"].combine_first(alt)
            except Exception:
                logger.exception(
                    "[RANKING SUMMARY] best_rank fallback failed col=%s",
                    fallback_col,
                )

    try:
        x.loc[x["best_rank_position"] <= 0, "best_rank_position"] = pd.NA
    except Exception:
        pass

    return x


# ============================================================
# timestamp
# ============================================================

def _resolve_summary_timestamp(df: pd.DataFrame) -> Optional[pd.Timestamp]:
    if df is None or df.empty:
        return None

    for col in ("end_time", "datetime", "snapshot_time", "timestamp", "time"):
        if col not in df.columns:
            continue

        try:
            s = pd.to_datetime(df[col], errors="coerce")
            s = s.dropna()
            if not s.empty:
                return pd.Timestamp(s.max())
        except Exception:
            continue

    return None


# ============================================================
# ETF / FUND filter
# ============================================================

def _looks_like_fund_or_etf(row: pd.Series) -> bool:
    text = " ".join(
        [
            _safe_str(row.get("symbolname")),
            _safe_str(row.get("name")),
            _safe_str(row.get("security_type")),
            _safe_str(row.get("asset_type")),
            _safe_str(row.get("market_type")),
        ]
    ).upper()

    if not text:
        return False

    keywords = [
        "ETF",
        "ETN",
        "REIT",
        "FUND",
        "インバース",
        "レバレッジ",
        "連動型",
        "上場投信",
        "投資信託",
        "投信",
        "ファンド",
    ]

    return any(k in text for k in keywords)


# ============================================================
# display frame preparation
# ============================================================

def _prepare_display_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()
    x = x.loc[:, ~pd.Index(x.columns).duplicated()].copy()
    x = _repair_best_rank_for_display(x)

    if "symbol" in x.columns:
        try:
            x["symbol"] = x["symbol"].astype(str).str.strip()
        except Exception:
            pass
    else:
        x["symbol"] = ""

    if "symbolname" in x.columns:
        try:
            x["symbolname"] = x["symbolname"].astype(str).fillna("").str.strip()
        except Exception:
            pass
    else:
        x["symbolname"] = ""

    # BUY score alias
    if "score_buy" not in x.columns:
        if "ranking_score" in x.columns:
            x["score_buy"] = pd.to_numeric(x["ranking_score"], errors="coerce")
        elif "score" in x.columns:
            x["score_buy"] = pd.to_numeric(x["score"], errors="coerce")
        elif "display_score" in x.columns:
            x["score_buy"] = pd.to_numeric(x["display_score"], errors="coerce")
        elif "final_score" in x.columns:
            x["score_buy"] = pd.to_numeric(x["final_score"], errors="coerce")
        else:
            x["score_buy"] = 0.0

    # SELL score alias
    if "score_sell" not in x.columns:
        for c in ("sell_score", "ranking_sell_score", "display_score_sell"):
            if c in x.columns:
                x["score_sell"] = pd.to_numeric(x[c], errors="coerce")
                break

    # price alias
    if "close" not in x.columns:
        if "last_price" in x.columns:
            x["close"] = x["last_price"]
        elif "current_price" in x.columns:
            x["close"] = x["current_price"]
        elif "price" in x.columns:
            x["close"] = x["price"]
        else:
            x["close"] = 0.0

    # slope alias fallback
    if "slope" not in x.columns and "slope_atr_scaled" in x.columns:
        try:
            x["slope"] = pd.to_numeric(x["slope_atr_scaled"], errors="coerce")
        except Exception:
            x["slope"] = pd.NA
    elif "slope" in x.columns and "slope_atr_scaled" in x.columns:
        try:
            x["slope"] = pd.to_numeric(x["slope"], errors="coerce").combine_first(
                pd.to_numeric(x["slope_atr_scaled"], errors="coerce")
            )
        except Exception:
            pass

    # mtf alias
    if "mtf" not in x.columns:
        if "score_mtf" in x.columns:
            x["mtf"] = x["score_mtf"]
        elif "mtf_score" in x.columns:
            x["mtf"] = x["mtf_score"]
        else:
            x["mtf"] = pd.NA

    # type alias
    if "dominant_rank_type" not in x.columns:
        for c in ("ranking_type", "rank_type", "type", "source_type"):
            if c in x.columns:
                x["dominant_rank_type"] = x[c]
                break
        else:
            x["dominant_rank_type"] = ""

    # hist alias
    if "hist_len" not in x.columns:
        for c in ("hist", "history_count", "appear_count", "ranking_history_count"):
            if c in x.columns:
                x["hist_len"] = x[c]
                break
        else:
            x["hist_len"] = pd.NA

    # numeric coercion
    for c in (
        "score_buy",
        "score_sell",
        "close",
        "slope",
        "slope_atr_scaled",
        "mtf",
        "score_mtf",
        "rsi",
        "macd",
        "best_rank_position",
        "hist_len",
    ):
        if c in x.columns:
            try:
                x[c] = pd.to_numeric(x[c], errors="coerce")
            except Exception:
                pass

    try:
        x["dominant_rank_type"] = (
            x["dominant_rank_type"].astype(str).fillna("").str.strip()
        )
    except Exception:
        x["dominant_rank_type"] = ""

    try:
        valid_mask = (
            x["close"].fillna(0).gt(0)
            | x["score_buy"].fillna(0).ne(0)
            | x["dominant_rank_type"].astype(str).str.strip().ne("")
        )
    except Exception:
        valid_mask = pd.Series(True, index=x.index)

    x["_is_meaningful_row"] = valid_mask

    try:
        etf_mask = x.apply(_looks_like_fund_or_etf, axis=1)
        if etf_mask.any():
            before = len(x)
            x = x.loc[~etf_mask].copy()
            logger.info(
                "[RANKING SUMMARY] display filter dropped fund/etf rows=%s->%s",
                before,
                len(x),
            )
    except Exception:
        logger.exception("[RANKING SUMMARY] etf/fund display filter failed")

    return x.reset_index(drop=True)


def _is_meaningful_ranking_summary(df: pd.DataFrame) -> bool:
    x = _prepare_display_frame(df)
    if x.empty:
        return False

    try:
        valid = x.loc[x["_is_meaningful_row"].fillna(False)].copy()
        return not valid.empty
    except Exception:
        return False


# ============================================================
# announced state
# ============================================================

def _should_announce(interval: int, latest_dt: Optional[pd.Timestamp]) -> bool:
    _ensure_global_slots()

    if latest_dt is None or pd.isna(latest_dt):
        return False

    try:
        m = dict(getattr(global_data, "ranking_summary_last_announced_dt", {}) or {})
        prev = m.get(interval)
        return prev is None or pd.Timestamp(prev) != pd.Timestamp(latest_dt)
    except Exception:
        return True


def _mark_announced(interval: int, latest_dt: Optional[pd.Timestamp]) -> None:
    _ensure_global_slots()

    if latest_dt is None or pd.isna(latest_dt):
        return

    try:
        m = dict(getattr(global_data, "ranking_summary_last_announced_dt", {}) or {})
        m[int(interval)] = pd.Timestamp(latest_dt)
        global_data.ranking_summary_last_announced_dt = m
    except Exception:
        logger.exception(
            "[RANKING SUMMARY] mark announced failed interval=%s",
            interval,
        )


# ============================================================
# display format helpers
# ============================================================

def _fmt_num(
    v: Any,
    *,
    width: int = 8,
    prec: int = 2,
    zero_as_dash: bool = False,
) -> str:
    try:
        vv = pd.to_numeric(v, errors="coerce")
        if pd.isna(vv):
            return f"{'-':>{width}}"

        fv = float(vv)

        if zero_as_dash and abs(fv) < 1e-12:
            return f"{'-':>{width}}"

        return f"{fv:>{width}.{prec}f}"
    except Exception:
        return f"{'-':>{width}}"


def _fmt_price(v: Any, *, width: int = 8) -> str:
    return _fmt_num(v, width=width, prec=1, zero_as_dash=False)


def _fmt_rank(v: Any) -> str:
    try:
        vv = pd.to_numeric(v, errors="coerce")
        if pd.isna(vv):
            return "  -"
        return f"{int(round(float(vv))):>3}"
    except Exception:
        return "  -"


def _resolve_slope(row: pd.Series) -> Any:
    slope_val = row.get("slope", pd.NA)

    try:
        tmp = pd.to_numeric(pd.Series([slope_val]), errors="coerce").iloc[0]
        if not pd.isna(tmp):
            return tmp
    except Exception:
        pass

    return row.get("slope_atr_scaled", pd.NA)


def _resolve_mtf(row: pd.Series) -> Any:
    for c in ("mtf", "score_mtf", "mtf_score"):
        if c in row.index:
            v = row.get(c, pd.NA)
            try:
                vv = pd.to_numeric(v, errors="coerce")
                if not pd.isna(vv):
                    return vv
            except Exception:
                pass
    return pd.NA


def _resolve_close(row: pd.Series) -> Any:
    for c in ("close", "last_price", "current_price", "price"):
        if c in row.index:
            v = row.get(c, pd.NA)
            try:
                vv = pd.to_numeric(v, errors="coerce")
                if not pd.isna(vv):
                    return vv
            except Exception:
                pass
    return pd.NA


def _resolve_type(row: pd.Series) -> str:
    for col in ("dominant_rank_type", "ranking_type", "type", "rank_type", "source_type"):
        v = _safe_str(row.get(col, ""))
        if v:
            return v
    return "-"


def _resolve_score_for_buy(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()

    if "score_buy" not in x.columns:
        if "ranking_score" in x.columns:
            x["score_buy"] = pd.to_numeric(x["ranking_score"], errors="coerce")
        elif "score" in x.columns:
            x["score_buy"] = pd.to_numeric(x["score"], errors="coerce")
        elif "display_score" in x.columns:
            x["score_buy"] = pd.to_numeric(x["display_score"], errors="coerce")
        elif "final_score" in x.columns:
            x["score_buy"] = pd.to_numeric(x["final_score"], errors="coerce")
        else:
            x["score_buy"] = pd.NA

    try:
        x["score_buy"] = pd.to_numeric(x["score_buy"], errors="coerce")
    except Exception:
        pass

    return x


def _resolve_score_for_sell(df: pd.DataFrame) -> tuple[pd.DataFrame, str, bool]:
    x = df.copy()

    for c in ("score_sell", "sell_score", "ranking_sell_score", "display_score_sell"):
        if c in x.columns:
            try:
                x[c] = pd.to_numeric(x[c], errors="coerce")
            except Exception:
                pass
            return x, c, True

    if "score_buy" in x.columns:
        x["_tmp_sell_score"] = pd.to_numeric(x["score_buy"], errors="coerce")
    elif "ranking_score" in x.columns:
        x["_tmp_sell_score"] = pd.to_numeric(x["ranking_score"], errors="coerce")
    elif "score" in x.columns:
        x["_tmp_sell_score"] = pd.to_numeric(x["score"], errors="coerce")
    elif "display_score" in x.columns:
        x["_tmp_sell_score"] = pd.to_numeric(x["display_score"], errors="coerce")
    elif "final_score" in x.columns:
        x["_tmp_sell_score"] = pd.to_numeric(x["final_score"], errors="coerce")
    else:
        x["_tmp_sell_score"] = pd.NA

    return x, "_tmp_sell_score", False


def _format_display_row(
    i: int,
    row: pd.Series,
    *,
    side: str,
    score_col: str,
) -> str:
    symbol = _safe_str(row.get("symbol", ""))
    symbolname = _display_symbolname(row)[:24]

    close = _fmt_price(_resolve_close(row), width=8)
    score = _fmt_num(row.get(score_col, pd.NA), width=8, prec=2)

    slope = _fmt_num(
        _resolve_slope(row),
        width=8,
        prec=2,
        zero_as_dash=True,
    )

    mtf = _fmt_num(
        _resolve_mtf(row),
        width=8,
        prec=2,
        zero_as_dash=True,
    )

    rsi = _fmt_num(
        row.get("rsi", pd.NA),
        width=7,
        prec=2,
        zero_as_dash=False,
    )

    macd = _fmt_num(
        row.get("macd", pd.NA),
        width=8,
        prec=2,
        zero_as_dash=True,
    )

    best_rank = _fmt_rank(row.get("best_rank_position", pd.NA))
    hist_len = _fmt_rank(row.get("hist_len", row.get("hist", pd.NA)))
    rtype = _resolve_type(row)

    mark = "🟦" if side.upper() == "BUY" else "🟥"

    return (
        f"{i:>2}. {mark} {symbol:<6} {symbolname:<24} "
        f"close={close} "
        f"score={score} "
        f"slope={slope} "
        f"mtf={mtf} "
        f"rsi={rsi} "
        f"macd={macd} "
        f"best={best_rank} "
        f"hist={hist_len} "
        f"type={rtype}"
    )


# ============================================================
# text formatter
# ============================================================

def _format_top_text(df_latest: pd.DataFrame, interval: int, topn: int = 10) -> str:
    x = _prepare_display_frame(df_latest)

    if x.empty:
        return (
            f"========== 📊 RANKING SUMMARY TOP{topn} ({interval}min) ==========\n"
            "該当なし"
        )

    try:
        x = x.loc[x["_is_meaningful_row"].fillna(False)].copy()
    except Exception:
        pass

    if x.empty:
        return (
            f"========== 📊 RANKING SUMMARY TOP{topn} ({interval}min) ==========\n"
            "該当なし"
        )

    latest_dt = _resolve_summary_timestamp(x)
    title_dt = (
        latest_dt.strftime("%Y-%m-%d %H:%M:%S")
        if latest_dt is not None and pd.notna(latest_dt)
        else "-"
    )

    if "best_rank_position" not in x.columns:
        x["best_rank_position"] = pd.NA

    # BUY TOP10
    buy = _resolve_score_for_buy(x)

    try:
        buy = buy.dropna(subset=["score_buy"])
    except Exception:
        pass

    buy_sort_cols: list[str] = []
    buy_ascending: list[bool] = []

    if "score_buy" in buy.columns:
        buy_sort_cols.append("score_buy")
        buy_ascending.append(False)

    if "best_rank_position" in buy.columns:
        buy_sort_cols.append("best_rank_position")
        buy_ascending.append(True)

    if buy_sort_cols:
        try:
            buy = buy.sort_values(
                buy_sort_cols,
                ascending=buy_ascending,
                kind="stable",
            )
        except Exception:
            logger.exception(
                "[RANKING SUMMARY] buy sort failed interval=%s",
                interval,
            )

    buy = buy.head(topn)

    # SELL TOP10
    sell, sell_score_col, has_real_sell_score = _resolve_score_for_sell(x)

    try:
        sell = sell.dropna(subset=[sell_score_col])
    except Exception:
        pass

    sell_sort_cols: list[str] = [sell_score_col]
    sell_ascending: list[bool] = [not has_real_sell_score]

    if "best_rank_position" in sell.columns:
        sell_sort_cols.append("best_rank_position")
        sell_ascending.append(True)

    if not sell.empty:
        try:
            sell = sell.sort_values(
                sell_sort_cols,
                ascending=sell_ascending,
                kind="stable",
            )
        except Exception:
            logger.exception(
                "[RANKING SUMMARY] sell sort failed interval=%s",
                interval,
            )

    sell = sell.head(topn)

    lines: list[str] = []

    lines.append(f"=== ⏱ 最新 {interval}min ランキングサマリー｜{title_dt} ===")
    lines.append("")
    lines.append(f"========== 📊 RANKING SUMMARY TOP{topn} ({interval}min) ==========")

    lines.append("🔵 BUY TOP10（score / slope / mtf / rsi / macd / best_rank / hist）")
    if buy.empty:
        lines.append("  - 該当なし")
    else:
        for i, (_, row) in enumerate(buy.iterrows(), start=1):
            lines.append(
                _format_display_row(
                    i,
                    row,
                    side="BUY",
                    score_col="score_buy",
                )
            )

    lines.append("")
    lines.append("🔴 SELL TOP10（score / slope / mtf / rsi / macd / best_rank / hist）")
    if not has_real_sell_score:
        lines.append("    ※ score_sell 未生成のため、score_buy / ranking_score の低い順で暫定表示")

    if sell.empty:
        lines.append("  - 該当なし")
    else:
        for i, (_, row) in enumerate(sell.iterrows(), start=1):
            lines.append(
                _format_display_row(
                    i,
                    row,
                    side="SELL",
                    score_col=sell_score_col,
                )
            )

    lines.append(f"mode={get_indicator_mode()}")
    lines.append("============================================================")

    return "\n".join(lines)


# ============================================================
# Discord hook
# ============================================================

def _split_discord_text(text: str, *, max_len: int = 1800) -> list[str]:
    """
    Discord content 2000文字制限対策。
    1800文字程度で安全に分割する。
    """
    if not text:
        return []

    s = str(text)
    chunks: list[str] = []

    while s:
        chunks.append(s[:max_len])
        s = s[max_len:]

    return chunks


def _call_discord_sender(fn: Any, fn_name: str, chunk: str) -> Any:
    """
    通知関数ごとの引数差異を吸収する。
    """
    if fn_name in {"send_discord_message", "send_message"}:
        try:
            return fn(content=chunk)
        except TypeError:
            return fn(chunk)

    return fn(chunk)


def _try_send_discord_message(text: str) -> bool:
    """
    Discordへランキングサマリー本文を送信する。

    対応候補:
      - alerts_util.send_discord_message
      - alerts_util.send_discord_text
      - alerts_util.send_discord_notify
      - utils.alerts_util.*
      - utils.discord_notifier.*
      - trading.notifications.discord_notifier.*

    重要:
      - alerts_util.py が settings.ini を読めていない場合は送信できない
      - Discordは content 2000文字制限があるため分割送信する
    """
    if not text:
        logger.warning("[RANKING SUMMARY] discord skipped reason=empty_text")
        return False

    chunks = _split_discord_text(text, max_len=1800)
    if not chunks:
        logger.warning("[RANKING SUMMARY] discord skipped reason=no_chunks")
        return False

    candidates = [
        ("alerts_util", "send_discord_message"),
        ("alerts_util", "send_discord_text"),
        ("alerts_util", "send_discord_notify"),

        ("utils.alerts_util", "send_discord_message"),
        ("utils.alerts_util", "send_discord_text"),
        ("utils.alerts_util", "send_discord_notify"),

        ("utils.discord_notifier", "send_message"),
        ("utils.discord_notifier", "send_discord_message"),
        ("utils.discord_notifier", "send_discord_text"),
        ("utils.discord_notifier", "send_discord_notify"),

        ("trading.notifications.discord_notifier", "send_message"),
        ("trading.notifications.discord_notifier", "send_discord_message"),
        ("trading.notifications.discord_notifier", "send_discord_text"),
        ("trading.notifications.discord_notifier", "send_discord_notify"),
    ]

    for mod_name, fn_name in candidates:
        try:
            mod = __import__(mod_name, fromlist=[fn_name])
            fn = getattr(mod, fn_name, None)

            if not callable(fn):
                logger.debug(
                    "[RANKING SUMMARY] discord candidate not callable %s.%s",
                    mod_name,
                    fn_name,
                )
                continue

            sent_count = 0
            failed_count = 0

            for idx, chunk in enumerate(chunks, start=1):
                if len(chunks) > 1:
                    chunk = f"{chunk}\n\n({idx}/{len(chunks)})"

                try:
                    result = _call_discord_sender(fn, fn_name, chunk)

                    # 送信関数が False を返す場合は失敗扱い。
                    # None は旧実装互換として成功扱いにする。
                    if result is False:
                        failed_count += 1
                        logger.warning(
                            "[RANKING SUMMARY] discord chunk returned False via %s.%s chunk=%s/%s",
                            mod_name,
                            fn_name,
                            idx,
                            len(chunks),
                        )
                    else:
                        sent_count += 1

                except Exception:
                    failed_count += 1
                    logger.exception(
                        "[RANKING SUMMARY] discord chunk send failed via %s.%s chunk=%s/%s",
                        mod_name,
                        fn_name,
                        idx,
                        len(chunks),
                    )

            if sent_count > 0 and failed_count == 0:
                logger.info(
                    "[RANKING SUMMARY] discord notified via %s.%s chunks=%s",
                    mod_name,
                    fn_name,
                    sent_count,
                )
                return True

            if sent_count > 0:
                logger.warning(
                    "[RANKING SUMMARY] discord partially notified via %s.%s sent=%s failed=%s",
                    mod_name,
                    fn_name,
                    sent_count,
                    failed_count,
                )
                return True

            logger.warning(
                "[RANKING SUMMARY] discord candidate failed all chunks via %s.%s",
                mod_name,
                fn_name,
            )

        except ModuleNotFoundError:
            logger.debug(
                "[RANKING SUMMARY] discord candidate module not found %s.%s",
                mod_name,
                fn_name,
            )
            continue
        except Exception:
            logger.exception(
                "[RANKING SUMMARY] discord notify candidate failed via %s.%s",
                mod_name,
                fn_name,
            )
            continue

    logger.warning("[RANKING SUMMARY] discord notifier not found or all candidates failed")
    return False


# ============================================================
# public api
# ============================================================

def announce_ranking_summary(
    interval: int,
    topn: int = 10,
    use_discord: bool = True,
) -> bool:
    """
    最新ランキングサマリーを取得し、ログ表示および必要に応じてDiscord通知する。

    Parameters
    ----------
    interval:
        1 / 3 / 5 などの分足
    topn:
        表示件数
    use_discord:
        Trueなら表示本文をDiscordへ送信する。
        Ver1.4 ではデフォルト True。
    """
    _ensure_global_slots()

    try:
        interval = int(interval)
    except Exception:
        interval = 1

    try:
        df_latest = get_latest_ranking_summary(interval)

        if df_latest is None or df_latest.empty:
            logger.info(
                "[RANKING SUMMARY] announce skipped interval=%s reason=empty",
                interval,
            )
            return False

        latest_dt = _resolve_summary_timestamp(df_latest)
        if latest_dt is None or pd.isna(latest_dt):
            logger.warning(
                "[RANKING SUMMARY] announce skipped interval=%s reason=timestamp_unresolved",
                interval,
            )
            return False

        if not _is_meaningful_ranking_summary(df_latest):
            logger.warning(
                "[RANKING SUMMARY] announce skipped interval=%s reason=no_meaningful_rows",
                interval,
            )
            return False

        if not _should_announce(interval, latest_dt):
            logger.info(
                "[RANKING SUMMARY] announce skipped interval=%s latest_dt=%s reason=already_announced",
                interval,
                latest_dt,
            )
            return False

        text = _format_top_text(
            df_latest,
            interval=interval,
            topn=topn,
        )

        logger.info("\n%s", text)

        discord_ok = False
        if use_discord:
            try:
                discord_ok = _try_send_discord_message(text)
                logger.info(
                    "[RANKING SUMMARY] discord result interval=%s ok=%s",
                    interval,
                    discord_ok,
                )
            except Exception:
                logger.exception(
                    "[RANKING SUMMARY] discord notify failed interval=%s",
                    interval,
                )
                discord_ok = False
        else:
            logger.info(
                "[RANKING SUMMARY] discord skipped interval=%s reason=use_discord_false",
                interval,
            )

        _mark_announced(interval, latest_dt)

        return True

    except Exception:
        logger.exception(
            "[RANKING SUMMARY] announce failed interval=%s",
            interval,
        )
        return False


# ============================================================
# optional public formatter
# runner/debug から直接文字列だけ作りたい場合用
# ============================================================

def format_ranking_summary_text(
    df_latest: pd.DataFrame,
    interval: int,
    topn: int = 10,
) -> str:
    try:
        interval = int(interval)
    except Exception:
        interval = 1

    return _format_top_text(
        df_latest,
        interval=interval,
        topn=topn,
    )


__all__ = [
    "set_indicator_mode",
    "get_indicator_mode",
    "announce_ranking_summary",
    "format_ranking_summary_text",
]