# ============================================================
# File   : trading/entry/entry_quality_score.py
# Version: Ver01-ENTRY-QUALITY-SCORE
# ------------------------------------------------------------
# エントリー候補の品質を点数化する。
# 2500〜7000円・70万円運用では、spread / 流動性 / 5秒足の悪さが
# 損益に直撃するため、AI_OK後でも質が低い候補を落とす安全弁。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

from trading.entry.spread_guard import is_spread_acceptable

logger = logging.getLogger(__name__)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == '':
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    s = str(v).strip().lower()
    if s in {'1', 'true', 'yes', 'y', 'on', 'ok', 'enable', 'enabled'}:
        return True
    if s in {'0', 'false', 'no', 'n', 'off', 'ng', 'disable', 'disabled', ''}:
        return False
    return bool(default)


ENABLE_ENTRY_QUALITY_SCORE = _env_bool('ENABLE_ENTRY_QUALITY_SCORE', True)
ENTRY_QUALITY_MIN_SCORE = _env_float('ENTRY_QUALITY_MIN_SCORE', 70.0)
ENTRY_QUALITY_MIN_VOLUME = _env_float('ENTRY_QUALITY_MIN_VOLUME', 30_000.0)
ENTRY_QUALITY_MIN_TURNOVER = _env_float('ENTRY_QUALITY_MIN_TURNOVER', 10_000_000.0)
ENTRY_QUALITY_MIN_PRICE = _env_float('ENTRY_QUALITY_MIN_PRICE', 2_500.0)
ENTRY_QUALITY_MAX_PRICE = _env_float('ENTRY_QUALITY_MAX_PRICE', 7_000.0)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == '':
            return default
        return float(v)
    except Exception:
        return default


def _get(row: dict | None, *keys: str, default=None):
    if not isinstance(row, dict):
        return default
    for k in keys:
        if k in row and row.get(k) not in (None, ''):
            return row.get(k)
    return default


def calc_entry_quality_score(symbol: str, *, side: str = '', row: dict | None = None, ai: dict | None = None, quotes: dict | None = None) -> tuple[float, dict]:
    """
    0〜100点でエントリー品質を返す。

    減点項目:
      - spread NG / spread 過大
      - 出来高不足
      - 売買代金不足
      - 価格帯レンジ外
      - AI confidence 低い
      - 5秒足 momentum が逆方向
    """
    score = 100.0
    reasons: list[str] = []

    row = row or {}
    ai = ai or {}

    price = _safe_float(
        _get(row, 'close_price', 'close', 'price', 'current_price', default=0.0),
        0.0,
    )
    volume = _safe_float(
        _get(row, 'volume', 'trading_volume', '出来高', default=0.0),
        0.0,
    )
    turnover = _safe_float(
        _get(row, 'turnover', 'trading_value', '売買代金', default=0.0),
        0.0,
    )
    if turnover <= 0 and price > 0 and volume > 0:
        turnover = price * volume

    ai_conf = _safe_float(
        _get(ai, 'confidence', 'conf', 'ai_confidence', default=0.0),
        0.0,
    )

    # --------------------------------------------------------
    # price band
    # --------------------------------------------------------
    if price > 0:
        if price < ENTRY_QUALITY_MIN_PRICE:
            score -= 40
            reasons.append(f'PRICE_TOO_LOW:{price:.1f}<{ENTRY_QUALITY_MIN_PRICE:.1f}')
        if ENTRY_QUALITY_MAX_PRICE > 0 and price > ENTRY_QUALITY_MAX_PRICE:
            score -= 40
            reasons.append(f'PRICE_TOO_HIGH:{price:.1f}>{ENTRY_QUALITY_MAX_PRICE:.1f}')
    else:
        score -= 20
        reasons.append('PRICE_UNKNOWN')

    # --------------------------------------------------------
    # liquidity
    # --------------------------------------------------------
    if volume < ENTRY_QUALITY_MIN_VOLUME:
        score -= 25
        reasons.append(f'LOW_VOLUME:{volume:.0f}<{ENTRY_QUALITY_MIN_VOLUME:.0f}')

    if turnover < ENTRY_QUALITY_MIN_TURNOVER:
        score -= 25
        reasons.append(f'LOW_TURNOVER:{turnover:.0f}<{ENTRY_QUALITY_MIN_TURNOVER:.0f}')

    # --------------------------------------------------------
    # spread
    # --------------------------------------------------------
    spread_ok, spread_detail = is_spread_acceptable(symbol, quotes=quotes)
    if not spread_ok:
        score -= 35
        reasons.append(f'SPREAD_NG:{spread_detail.get("reason")}')
    else:
        sp = _safe_float(spread_detail.get('spread_pct'), 0.0)
        if sp > 0.15:
            score -= 10
            reasons.append(f'SPREAD_WARN:{sp:.4f}')

    # --------------------------------------------------------
    # AI confidence
    # --------------------------------------------------------
    if ai_conf > 0:
        if ai_conf < 0.55:
            score -= 20
            reasons.append(f'AI_CONF_LOW:{ai_conf:.3f}')
        elif ai_conf < 0.65:
            score -= 10
            reasons.append(f'AI_CONF_MID:{ai_conf:.3f}')

    # --------------------------------------------------------
    # simple 5s / slope / momentum hints if present
    # --------------------------------------------------------
    side_u = str(side or _get(row, 'side', default='')).upper()
    slope = _safe_float(_get(row, 'slope', 'slope_atr_scaled', default=0.0), 0.0)
    if side_u == 'BUY' and slope < 0:
        score -= 15
        reasons.append(f'BUY_NEGATIVE_SLOPE:{slope:.4f}')
    elif side_u == 'SELL' and slope > 0:
        score -= 15
        reasons.append(f'SELL_POSITIVE_SLOPE:{slope:.4f}')

    score = max(0.0, min(100.0, score))
    detail = {
        'symbol': str(symbol),
        'side': side_u,
        'score': score,
        'min_score': ENTRY_QUALITY_MIN_SCORE,
        'price': price,
        'volume': volume,
        'turnover': turnover,
        'ai_confidence': ai_conf,
        'spread': spread_detail,
        'reasons': reasons,
        'ok': score >= ENTRY_QUALITY_MIN_SCORE,
    }
    return score, detail


def is_entry_quality_ok(symbol: str, *, side: str = '', row: dict | None = None, ai: dict | None = None, quotes: dict | None = None) -> tuple[bool, dict]:
    if not ENABLE_ENTRY_QUALITY_SCORE:
        return True, {'reason': 'ENTRY_QUALITY_SCORE_DISABLED'}

    score, detail = calc_entry_quality_score(symbol, side=side, row=row, ai=ai, quotes=quotes)
    ok = score >= ENTRY_QUALITY_MIN_SCORE
    detail['ok'] = ok

    if ok:
        logger.info('[ENTRY QUALITY] OK symbol=%s score=%.1f detail=%s', symbol, score, detail)
    else:
        logger.warning('[ENTRY QUALITY] NG symbol=%s score=%.1f detail=%s', symbol, score, detail)

    return ok, detail
