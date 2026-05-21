# ============================================================
# AI/entry_row_builder.py
# Version: Ver1.2-SIDE-NONE-FALLBACK-FIX
# ------------------------------------------------------------
# ✔ AI/entry_gate に渡す row を正規化生成
# ✔ summary / ranking / pending 共通
# ✔ 欠損・型ブレ完全耐性
# ✔ 判断ロジック一切なし
# ✔ pending / ranking の score_total を絶対に破壊しない
# ✔ pandas NaN を score_total=未設定として正しく扱う
# ✔ scoreをint丸めせず float のまま保持
# ✔ pending の score から score_buy / score_sell を復元
# ✔ entry_controller の BUY_SCORE_LOW:0 / SELL_SCORE_LOW:0 を防止
#
# Ver1.2:
# ✔ entry_decision='NONE' / '' / nan を有効sideとして扱わない
# ✔ entry_decision が NONE でも side='BUY'/'SELL' があれば正しく採用
# ✔ RANKING pending が SIDE_INVALID side=NONE で落ちる問題を修正
# ============================================================

from __future__ import annotations

import logging
import math
from typing import Any, Dict

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# util
# ============================================================

def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None:
            return default
        if isinstance(v, float) and math.isnan(v):
            return default
        return int(float(v))
    except Exception:
        return default


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        if isinstance(v, float) and math.isnan(v):
            return default
        if isinstance(v, str) and v.strip() == "":
            return default
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _is_blank_like(v: Any) -> bool:
    try:
        if v is None:
            return True
        if isinstance(v, float) and math.isnan(v):
            return True
        s = str(v).strip()
        if s == "":
            return True
        if s.upper() in {"NONE", "NULL", "NAN", "NA", "N/A", "-"}:
            return True
        return False
    except Exception:
        return True


def _first(row: Dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        try:
            if key in row:
                v = row.get(key)
                if not _is_blank_like(v):
                    return v
        except Exception:
            pass
    return default


def _norm_side(v: Any) -> str:
    try:
        if _is_blank_like(v):
            return ""
        s = str(v).strip().upper()
        return s if s in ("BUY", "SELL") else ""
    except Exception:
        return ""


def _resolve_side(row: Dict[str, Any]) -> str:
    """
    side解決専用。

    entry_decision='NONE' が入っていると Python の or では真として扱われ、
    後続の side='BUY'/'SELL' まで到達しない。
    そのため、NONE系を明示的に無効扱いしてから順番に見る。
    """
    for key in ("entry_decision", "side", "ai_side", "decision", "Side"):
        side = _norm_side(row.get(key))
        if side in ("BUY", "SELL"):
            return side
    return ""


# ============================================================
def build_entry_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    AI/entry_gate に渡す row を生成する唯一の関数。

    重要:
      pending_manager から来る SUMMARY_AI / RANKING は score だけを持ち、
      score_buy / score_sell を持たないことがある。
      ここで score と side から buy/sell score を復元しないと、
      entry_controller 側の最終 _passes_ai_gate で
      BUY_SCORE_LOW:0.000 / SELL_SCORE_LOW:0.000 になり、
      AI_OK 済み候補が発注直前に落ちる。
    """

    if row is None:
        return {}

    # pandas Series → dict
    if hasattr(row, "to_dict"):
        row = row.to_dict()

    if not isinstance(row, dict):
        return {}

    # ========================================================
    # 基本情報
    # ========================================================
    symbol = row.get("symbol")
    if not symbol:
        return {}

    source = row.get("source", "UNKNOWN")
    side = _resolve_side(row)

    entry_row: Dict[str, Any] = {
        "symbol": symbol,
        "symbolname": row.get("symbolname", ""),
        "source": source,
        "side": side,
        "entry_decision": side if side in ("BUY", "SELL") else "",
        "entry_type": row.get("entry_type") or row.get("entryType"),
        "ai_side": side if side in ("BUY", "SELL") else row.get("ai_side"),
    }

    # ========================================================
    # 時間・価格・出来高
    # ========================================================
    entry_row["datetime"] = row.get("datetime")
    entry_row["interval"] = _safe_int(row.get("interval", 1), 1)

    close_price = _safe_float(
        _first(row, ("close_price", "current_price", "price", "close"), 0.0),
        0.0,
    )
    entry_row["close_price"] = close_price
    entry_row["price"] = close_price
    entry_row["close"] = close_price
    entry_row["current_price"] = close_price

    volume = _safe_int(
        _first(row, ("volume", "trading_volume", "出来高"), 0),
        0,
    )
    entry_row["volume"] = volume

    # ========================================================
    # turnover（売買代金）完全吸収
    # ========================================================
    turnover = _first(
        row,
        (
            "turnover",
            "trading_value",
            "TradingValue",
            "value",
            "Value",
            "売買代金",
        ),
        None,
    )

    if turnover is None:
        turnover = close_price * volume

    entry_row["turnover"] = _safe_float(turnover, 0.0)

    # ========================================================
    # score 正規化（float保持・破壊禁止）
    # ========================================================
    raw_score = _safe_float(
        _first(row, ("score", "score_total", "final_score", "display_score"), 0.0),
        0.0,
    )

    score_total = _safe_float(
        _first(row, ("score_total", "total_score"), raw_score),
        raw_score,
    )

    buy_score = _safe_float(
        _first(row, ("score_buy", "buy_score"), 0.0),
        0.0,
    )
    sell_score = _safe_float(
        _first(row, ("score_sell", "sell_score"), 0.0),
        0.0,
    )

    # pending SUMMARY_AI / RANKING は score だけを持つことがあるため、side から復元する
    if side == "BUY" and buy_score <= 0 and raw_score > 0:
        buy_score = raw_score
    elif side == "SELL" and sell_score <= 0:
        if raw_score < 0:
            sell_score = abs(raw_score)
        elif raw_score > 0:
            sell_score = raw_score

    if score_total == 0.0:
        if side == "SELL" and sell_score > 0:
            score_total = -abs(sell_score)
        elif side == "BUY" and buy_score > 0:
            score_total = buy_score
        else:
            score_total = raw_score

    if raw_score == 0.0:
        raw_score = score_total

    entry_row["score"] = raw_score
    entry_row["score_total"] = score_total
    entry_row["total_score"] = score_total
    entry_row["final_score"] = score_total
    entry_row["display_score"] = score_total

    entry_row["score_buy"] = buy_score
    entry_row["buy_score"] = buy_score
    entry_row["score_sell"] = sell_score
    entry_row["sell_score"] = sell_score

    entry_row["dominant_side"] = row.get("dominant_side") or side or "NONE"
    entry_row["dominant_ratio"] = _safe_float(row.get("dominant_ratio"), 0.0)

    # ========================================================
    # ranking / optional
    # ========================================================
    entry_row["volume_speed"] = _safe_float(row.get("volume_speed"), 0.0)
    entry_row["rank_type"] = row.get("rank_type")
    entry_row["rank_position"] = row.get("rank_position")
    entry_row["market"] = row.get("market")

    # 可能な限り indicator も通す
    for key in (
        "open",
        "high",
        "low",
        "ma5",
        "ma25",
        "ma75",
        "rsi",
        "macd",
        "signal",
        "slope",
        "slope_atr_scaled",
        "atr",
        "atr_1m",
        "atr_5m",
        "mtf",
        "score_mtf",
        "mtf_score",
        "score_3m",
        "score_5m",
        "strategy",
        "entry_strategy",
        "reason",
        "ai_reason",
        "confidence",
    ):
        if key in row:
            entry_row[key] = row.get(key)

    # ========================================================
    # debug（判断には使わない）
    # ========================================================
    entry_row["_raw"] = {
        "score_raw": row.get("score"),
        "score_total_raw": row.get("score_total"),
        "score_buy_raw": row.get("score_buy"),
        "score_sell_raw": row.get("score_sell"),
        "buy_score_raw": row.get("buy_score"),
        "sell_score_raw": row.get("sell_score"),
        "pending_score": row.get("score"),
        "rank_strength": row.get("rank_strength"),
        "turnover_raw": row.get("turnover"),
        "side_raw": row.get("side"),
        "entry_decision_raw": row.get("entry_decision"),
        "ai_side_raw": row.get("ai_side"),
        "resolved_side": side,
    }

    if not side and (row.get("side") or row.get("entry_decision") or row.get("ai_side")):
        logger.warning(
            "[ENTRY ROW BUILDER] side unresolved symbol=%s raw_side=%s raw_entry_decision=%s raw_ai_side=%s keys=%s",
            symbol,
            row.get("side"),
            row.get("entry_decision"),
            row.get("ai_side"),
            sorted(list(row.keys()))[:40],
        )

    return entry_row


# ============================================================
def build_entry_rows(df: pd.DataFrame):
    """
    DataFrame → list[entry_row]

    - None / empty 完全耐性
    - 不正 row は自動スキップ
    """

    if df is None or df.empty:
        return []

    rows = []

    for _, r in df.iterrows():
        try:
            er = build_entry_row(r)
            if er:
                rows.append(er)
        except Exception:
            logger.exception("❌ entry_row build failed")

    return rows
