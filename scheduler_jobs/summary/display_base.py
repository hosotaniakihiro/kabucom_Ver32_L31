# ============================================================
# File   : scheduler_jobs/summary/display_base.py
# Function:
#   - display 系共通の基本 helper
#   - DataFrame 安全化
#   - 列揺れ吸収
#   - symbol / 数値 / テキスト整形
#   - Discord用 1銘柄2行フォーマット生成
#   - score_config.ini の買い/売りサインを日本語で表示
# ------------------------------------------------------------
# Version: Ver1.2-SCORING-SIGNAL-JAPANESE-DISPLAY
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Iterable, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# DataFrame安全化
# ============================================================

def safe_df(df: Any) -> pd.DataFrame:
    try:
        if df is None:
            return pd.DataFrame()

        if isinstance(df, pd.DataFrame):
            out = df.copy()
        elif isinstance(df, pd.Series):
            out = pd.DataFrame([df.to_dict()])
        elif isinstance(df, dict):
            out = pd.DataFrame([df])
        else:
            out = pd.DataFrame(df).copy()

        if out.empty:
            return out

        try:
            if isinstance(out.columns, pd.MultiIndex):
                out.columns = [
                    "_".join([str(x) for x in col if x not in ("", None)])
                    for col in out.columns.to_flat_index()
                ]
        except Exception:
            logger.debug("[SUMMARY DISPLAY] multiindex flatten failed", exc_info=True)

        try:
            out.columns = [str(c) for c in out.columns]
        except Exception:
            logger.debug("[SUMMARY DISPLAY] stringify columns failed", exc_info=True)

        out = out.reset_index(drop=True)

        try:
            out.replace([np.inf, -np.inf], np.nan, inplace=True)
        except Exception:
            logger.debug("[SUMMARY DISPLAY] inf replace failed", exc_info=True)

        return out

    except Exception:
        logger.exception("[SUMMARY DISPLAY] safe_df failed")
        return pd.DataFrame()


def coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = safe_df(df)
    if df.empty:
        return df

    try:
        cols = list(df.columns)
        if len(cols) == len(set(cols)):
            return df
    except Exception:
        return df

    try:
        unique_cols = []
        seen = set()
        for c in df.columns:
            if c not in seen:
                unique_cols.append(c)
                seen.add(c)

        out = {}
        for c in unique_cols:
            idxs = [i for i, name in enumerate(df.columns) if name == c]
            if len(idxs) == 1:
                out[c] = df.iloc[:, idxs[0]]
                continue

            s = df.iloc[:, idxs[0]]
            for j in idxs[1:]:
                try:
                    s = s.combine_first(df.iloc[:, j])
                except Exception:
                    try:
                        s = s.where(s.notna(), df.iloc[:, j])
                    except Exception:
                        pass
            out[c] = s

        return pd.DataFrame(out).reset_index(drop=True)

    except Exception:
        logger.debug("[SUMMARY DISPLAY] coalesce duplicate columns failed", exc_info=True)
        try:
            return df.loc[:, ~df.columns.duplicated(keep="last")].copy().reset_index(drop=True)
        except Exception:
            return df


# ============================================================
# 値取得 helper
# ============================================================

def first_existing(row: pd.Series, names: Iterable[str], default=None):
    for name in names:
        try:
            if name in row.index:
                val = row[name]
                if pd.isna(val):
                    continue
                if isinstance(val, str) and val.strip() == "":
                    continue
                return val
        except Exception:
            continue
    return default


def to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or (isinstance(v, str) and str(v).strip() == ""):
            return default
        if pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def fmt_price(v: Any) -> str:
    x = to_float(v, default=np.nan)
    if pd.isna(x):
        return "-"
    return f"{x:.1f}"


def fmt_metric(v: Any) -> str:
    x = to_float(v, default=np.nan)
    if pd.isna(x):
        return "-"
    return f"{x:.2f}"


def fmt_confidence(v: Any) -> str:
    x = to_float(v, default=np.nan)
    if pd.isna(x):
        return "-"
    return f"{x:.2f}"


def normalize_symbol_value(v: Any) -> str:
    try:
        s = str(v).strip()
    except Exception:
        return ""
    if not s:
        return ""
    if s.endswith(".0"):
        s2 = s[:-2]
        if s2.isdigit():
            return s2
    return s


# ============================================================
# Series選択 helper
# ============================================================

def pick_series(df: pd.DataFrame, candidates, default=0.0):
    for col in candidates:
        if col in df.columns:
            try:
                s = pd.to_numeric(df[col], errors="coerce")
                s = s.replace([np.inf, -np.inf], default)
                return s.fillna(default)
            except Exception:
                continue
    return pd.Series(default, index=df.index, dtype="float64")


def pick_series_nan(df: pd.DataFrame, candidates):
    for col in candidates:
        if col in df.columns:
            try:
                s = pd.to_numeric(df[col], errors="coerce")
                s = s.replace([np.inf, -np.inf], np.nan)
                return s
            except Exception:
                continue
    return pd.Series(np.nan, index=df.index, dtype="float64")


def pick_text_series(df: pd.DataFrame, candidates, default=""):
    for col in candidates:
        if col in df.columns:
            try:
                return df[col].fillna(default).astype(str)
            except Exception:
                continue
    return pd.Series(default, index=df.index, dtype="object")


def normalize_bool(v: Any) -> bool:
    try:
        if isinstance(v, bool):
            return v
        if v is None:
            return False
        if pd.isna(v):
            return False

        s = str(v).strip().lower()
        if s in {"1", "true", "t", "yes", "y", "ok", "pass", "passed"}:
            return True
        if s in {"0", "false", "f", "no", "n", "ng", "none", ""}:
            return False

        return float(v) != 0.0
    except Exception:
        return False


def pick_bool_series(df: pd.DataFrame, candidates, default=False):
    for col in candidates:
        if col in df.columns:
            try:
                s = df[col].map(normalize_bool)
                return s.fillna(default).astype(bool)
            except Exception:
                continue
    return pd.Series(default, index=df.index, dtype="bool")


# ============================================================
# symbolname 解決
# ============================================================

def resolve_symbolname_series(df: pd.DataFrame) -> pd.Series:
    symbol_s = pick_text_series(df, ["symbol"], default="").astype(str).str.strip()
    symbolname_s = pick_text_series(df, ["symbolname"], default="").astype(str).str.strip()
    name_s = pick_text_series(df, ["name"], default="").astype(str).str.strip()

    out = symbolname_s.copy()
    out = out.mask(out.eq(""), name_s)

    try:
        from global_state import global_data
        mp = getattr(global_data, "symbol_name_map", {})
        if isinstance(mp, dict) and mp:
            mapped = symbol_s.map(lambda x: str(mp.get(str(x).strip(), "")).strip())
            out = out.mask(out.eq(""), mapped)
    except Exception:
        logger.debug("[SUMMARY DISPLAY] symbol_name_map補完失敗", exc_info=True)

    out = out.mask(out.eq(""), symbol_s)
    out = out.fillna("").astype(str).str.strip()
    out = out.mask(out.eq(""), symbol_s)
    return out


# ============================================================
# logger 出力
# ============================================================

def print_line(s: str) -> None:
    logger.info("%s", s)


# ============================================================
# scoring.ini / score_config.ini サイン表示 helper
# ============================================================

def _active_scoring_signal_text(row: pd.Series, side: str) -> str:
    try:
        from scheduler_jobs.summary.scoring_signal_japanese import format_active_scoring_signals
        max_items = int(float(os.getenv("SUMMARY_DISPLAY_SIGNAL_MAX_ITEMS", "999")))
        return format_active_scoring_signals(row, side=side, max_items=max_items)
    except Exception:
        logger.debug("[SUMMARY DISPLAY] active scoring signal text failed", exc_info=True)
        return ""


def _score_config_catalog_text(side: str) -> str:
    try:
        from scheduler_jobs.summary.scoring_signal_japanese import format_score_config_catalog
        max_items = int(float(os.getenv("SUMMARY_DISPLAY_SIGNAL_CATALOG_MAX_ITEMS", "999")))
        return format_score_config_catalog(side=side, max_items=max_items)
    except Exception:
        logger.debug("[SUMMARY DISPLAY] score config catalog text failed", exc_info=True)
        return ""


# ============================================================
# Discord用 helper
# ============================================================

def _discord_reason(row: pd.Series, side: str = "BUY") -> str:
    """
    Discord表示用の理由を取得または簡易生成する。

    優先順:
      BUY  : reason_buy / buy_reason / reason
      SELL : reason_sell / sell_reason / reason

    さらに score_config.ini / scoring.ini 由来のONフラグがあれば
    日本語の「買いサイン=...」「売りサイン=...」を追加する。
    """

    side_u = str(side or "BUY").upper()

    if side_u == "SELL":
        reason = first_existing(row, ["reason_sell", "sell_reason", "reason", "理由_SELL", "理由"], "")
    else:
        reason = first_existing(row, ["reason_buy", "buy_reason", "reason", "理由_BUY", "理由"], "")

    if reason not in ("", "-", None):
        base_reason = str(reason)
    else:
        reasons: List[str] = []
        buy = to_float(first_existing(row, ["score_buy", "disp_buy_score", "buy"], 0.0), 0.0)
        sell = to_float(first_existing(row, ["score_sell", "disp_sell_score", "sell"], 0.0), 0.0)
        slope = to_float(first_existing(row, ["slope", "disp_slope", "score_slope", "slope_atr_scaled"], 0.0), 0.0)
        macd = to_float(first_existing(row, ["macd"], 0.0), 0.0)
        rsi = to_float(first_existing(row, ["rsi"], 0.0), 0.0)

        if side_u == "SELL":
            if sell >= buy:
                reasons.append("売りスコア優勢")
            if slope <= 0:
                reasons.append("下向き")
            if macd <= 0:
                reasons.append("MACD弱化")
            if rsi <= 45:
                reasons.append("RSI弱い")
        else:
            if buy >= sell:
                reasons.append("買いスコア優勢")
            if slope >= 0:
                reasons.append("上向き")
            if macd >= 0:
                reasons.append("MACD強化")
            if rsi >= 50:
                reasons.append("RSI良好")

        base_reason = " / ".join(reasons) if reasons else "-"

    signal_text = _active_scoring_signal_text(row, side_u)
    if signal_text:
        if base_reason and base_reason != "-":
            return base_reason + " / " + signal_text
        return signal_text
    return base_reason


def build_discord_candidate_2lines(i: int, row: pd.Series, *, side: str = "BUY") -> str:
    """
    Discord送信用。
    1銘柄を2〜3行に整形する。

    3行目に score_config.ini/scoring.ini の日本語サインを出す。
    """

    try:
        if not isinstance(row, pd.Series):
            row = pd.Series(row)
    except Exception:
        row = pd.Series({})

    symbol = normalize_symbol_value(first_existing(row, ["symbol", "code", "銘柄コード"], "-"))
    name = str(first_existing(row, ["symbolname_view", "symbolname", "name", "銘柄名"], "")).strip()

    score = first_existing(row, ["disp_score", "score", "display_score", "final_score", "score_total"], 0.0)
    buy = first_existing(row, ["disp_buy_score", "score_buy", "buy"], 0.0)
    sell = first_existing(row, ["disp_sell_score", "score_sell", "sell"], 0.0)
    total = first_existing(row, ["score_total", "total", "display_score", "final_score", "score"], score)
    final = first_existing(row, ["final_score", "display_score", "score_total", "score"], score)
    slope = first_existing(row, ["disp_slope", "slope", "score_slope", "slope_atr_scaled"], 0.0)
    mtf = first_existing(row, ["disp_mtf", "mtf", "score_mtf", "mtf_score"], 0.0)
    rsi = first_existing(row, ["rsi"], 0.0)
    macd = first_existing(row, ["macd"], 0.0)
    close = first_existing(row, ["close", "close_price"], 0.0)
    tick = first_existing(row, ["tick", "tick_count", "ticks"], "-")

    reason = _discord_reason(row, side=side)

    line1 = (
        f"{i:>2}. {symbol} {name} "
        f"score={fmt_metric(score)} "
        f"buy={fmt_metric(buy)} "
        f"sell={fmt_metric(sell)} "
        f"total={fmt_metric(total)} "
        f"final={fmt_metric(final)} "
        f"close={fmt_price(close)}"
    )

    tick_text = "-" if tick == "-" else fmt_metric(tick)
    line2 = (
        f"    slope={fmt_metric(slope)} "
        f"mtf={fmt_metric(mtf)} "
        f"rsi={fmt_metric(rsi)} "
        f"macd={fmt_metric(macd)} "
        f"tick={tick_text}"
    )
    line3 = f"    理由={reason}"

    return line1 + "\n" + line2 + "\n" + line3


def build_discord_top10_message_2lines(
    df: pd.DataFrame,
    *,
    title: str,
    side: str = "BUY",
    max_rows: int = 10,
    code_block: bool = True,
) -> str:
    """
    Discord送信用 TOP10 メッセージを作る。

    - 1銘柄2〜3行
    - score_config.ini/scoring.ini のONサインを日本語で表示
    """

    out_df = safe_df(df)
    out_df = coalesce_duplicate_columns(out_df)

    if out_df.empty:
        msg = f"{title}\n対象なし"
        if code_block:
            return f"```\n{msg}\n```"
        return msg

    rows = out_df.head(max_rows).copy()
    lines: List[str] = [str(title)]

    # 必要ならサイン定義カタログを先頭に全部出す。
    show_catalog = str(os.getenv("SUMMARY_DISPLAY_SHOW_SIGNAL_CATALOG", "0")).lower() in {"1", "true", "yes", "y", "on"}
    if show_catalog:
        catalog = _score_config_catalog_text(side)
        if catalog:
            lines.append(catalog)

    for i, (_, row) in enumerate(rows.iterrows(), start=1):
        lines.append(build_discord_candidate_2lines(i, row, side=side))

    msg = "\n".join(lines)
    if code_block:
        return f"```\n{msg}\n```"
    return msg


def build_discord_buy_top10_message_2lines(
    df: pd.DataFrame,
    *,
    interval: Any = "-",
    source: str = "SUMMARY",
    max_rows: int = 10,
    code_block: bool = True,
) -> str:
    title = f"[SUMMARY BUY TOP{max_rows}] interval={interval} source={source}"
    return build_discord_top10_message_2lines(df, title=title, side="BUY", max_rows=max_rows, code_block=code_block)


def build_discord_sell_top10_message_2lines(
    df: pd.DataFrame,
    *,
    interval: Any = "-",
    source: str = "SUMMARY",
    max_rows: int = 10,
    code_block: bool = True,
) -> str:
    title = f"[SUMMARY SELL TOP{max_rows}] interval={interval} source={source}"
    return build_discord_top10_message_2lines(df, title=title, side="SELL", max_rows=max_rows, code_block=code_block)


def build_discord_ai_ok_message_2lines(
    df: pd.DataFrame,
    *,
    title: str,
    side: str = "BUY",
    max_rows: int = 10,
    code_block: bool = True,
) -> str:
    return build_discord_top10_message_2lines(df, title=title, side=side, max_rows=max_rows, code_block=code_block)


# ============================================================
# 互換用 alias
# ============================================================

build_discord_candidate_line_2lines = build_discord_candidate_2lines
build_discord_summary_message_2lines = build_discord_top10_message_2lines
