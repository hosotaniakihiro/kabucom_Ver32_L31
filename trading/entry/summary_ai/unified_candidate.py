# ============================================================
# File   : trading/entry/summary_ai/unified_candidate.py
# Version: PRODUCTION-STABLE-REV1.0-UNIFIED-CANDIDATE
# Purpose:
#   複数ルートのENTRY候補を1つの共通形式に統合する
#
# Routes:
#   - RANKING_SUMMARY
#   - PUSH_SUMMARY
#   - YAHOO_SUMMARY
#   - TONOSAMA
#
# Important:
#   このファイルは発注しない
# ============================================================

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
import math


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        x = float(v)
        if math.isnan(x) or math.isinf(x):
            return default
        return x
    except Exception:
        return default


def _to_str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    return str(v)


@dataclass
class UnifiedEntryCandidate:
    symbol: str
    symbolname: str = ""
    source: str = "UNKNOWN"

    close: float = 0.0
    score_buy: float = 0.0
    score_sell: float = 0.0
    score_total: float = 0.0
    final_score: float = 0.0

    slope: float = 0.0
    slope_atr_scaled: float = 0.0
    atr: float = 0.0
    rsi: float = 0.0
    macd: float = 0.0

    ranking_score: float = 0.0
    ranking_momentum: float = 0.0
    price_delta_pct: float = 0.0
    rank_improve: float = 0.0
    volume_delta: float = 0.0

    tonosama_score: float = 0.0
    tonosama_hit: bool = False

    priority: float = 0.0
    reasons: list[str] = field(default_factory=list)

    raw: Dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return str(self.symbol)

    def has_real_technical(self) -> bool:
        """
        PUSH/Yahoo由来の本物OHLCテクニカルがあるか。
        """
        return (
            self.slope_atr_scaled > 0
            or self.atr > 0
            or self.rsi > 0
            or self.macd != 0
        )

    def is_ranking_like(self) -> bool:
        s = self.source.upper()
        return "RANKING" in s

    def is_push_like(self) -> bool:
        s = self.source.upper()
        return "PUSH" in s or "SUMMARY" == s

    def is_yahoo_like(self) -> bool:
        s = self.source.upper()
        return "YAHOO" in s

    def is_tonosama_like(self) -> bool:
        s = self.source.upper()
        return "TONOSAMA" in s or self.tonosama_hit


def candidate_from_row(row: Any, *, source: str) -> UnifiedEntryCandidate:
    """
    pandas.Series / dict から共通候補へ変換する。
    """
    get = row.get if hasattr(row, "get") else lambda k, d=None: d

    symbol = _to_str(get("symbol", ""))
    symbolname = _to_str(
        get("symbolname", get("symbolname_view", get("name", "")))
    )

    close = _to_float(
        get("close", get("close_price", get("current_price", 0.0)))
    )

    score_buy = _to_float(get("score_buy", get("buy_score", 0.0)))
    score_sell = _to_float(get("score_sell", get("sell_score", 0.0)))
    score_total = _to_float(get("score_total", get("total_score", 0.0)))
    final_score = _to_float(
        get("final_score", get("display_score", get("score", score_total)))
    )

    slope = _to_float(get("slope", 0.0))
    slope_atr_scaled = _to_float(
        get("slope_atr_scaled", get("score_slope", slope))
    )
    atr = _to_float(get("atr", get("atr_1m", 0.0)))
    rsi = _to_float(get("rsi", 0.0))
    macd = _to_float(get("macd", 0.0))

    ranking_score = _to_float(get("ranking_score", 0.0))
    ranking_momentum = _to_float(get("ranking_momentum", 0.0))
    price_delta_pct = _to_float(get("price_delta_pct", 0.0))
    rank_improve = _to_float(get("rank_improve", 0.0))
    volume_delta = _to_float(get("volume_delta", 0.0))

    tonosama_score = _to_float(get("tonosama_score", 0.0))
    tonosama_hit = bool(get("tonosama_hit", False)) or source.upper() == "TONOSAMA"

    c = UnifiedEntryCandidate(
        symbol=symbol,
        symbolname=symbolname,
        source=source,
        close=close,
        score_buy=score_buy,
        score_sell=score_sell,
        score_total=score_total,
        final_score=final_score,
        slope=slope,
        slope_atr_scaled=slope_atr_scaled,
        atr=atr,
        rsi=rsi,
        macd=macd,
        ranking_score=ranking_score,
        ranking_momentum=ranking_momentum,
        price_delta_pct=price_delta_pct,
        rank_improve=rank_improve,
        volume_delta=volume_delta,
        tonosama_score=tonosama_score,
        tonosama_hit=tonosama_hit,
        raw=dict(row) if hasattr(row, "items") else {},
    )

    c.priority = calc_candidate_priority(c)
    return c


def calc_candidate_priority(c: UnifiedEntryCandidate) -> float:
    """
    候補統合時の優先度。
    発注判断ではなく、重複時にどの情報を優先するか。
    """
    p = 0.0

    # 本物テクニカルを最優先
    p += c.slope_atr_scaled * 10.0
    p += c.final_score * 1.0
    p += c.score_buy * 0.8
    p -= c.score_sell * 0.8

    # ランキング由来の勢い
    p += c.ranking_score * 0.5
    p += c.ranking_momentum * 0.5
    p += c.rank_improve * 0.2

    # 殿様イナゴ
    if c.tonosama_hit:
        p += 5.0 + c.tonosama_score

    # source bonus
    s = c.source.upper()
    if "PUSH" in s:
        p += 10.0
    elif "YAHOO" in s:
        p += 7.0
    elif "TONOSAMA" in s:
        p += 5.0
    elif "RANKING" in s:
        p += 2.0

    return p