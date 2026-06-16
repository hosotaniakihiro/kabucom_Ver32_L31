# ============================================================
# File   : trading/audit_logging/entry_audit.py
# Version: Ver03-ENTRY-AUDIT-FULL-LINKS
# ------------------------------------------------------------
# Helper functions for writing entry pipeline audit records.
# These wrappers are intentionally safe: audit failure must not stop trading.
# ============================================================

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from .recorder import (
    record_candidate_event,
    record_filter_event,
    record_order_event,
)


_TECH_KEYS = (
    # identity / source
    'symbol', 'symbolname', 'datetime', 'dt', 'source', 'entry_source', 'trigger_type',
    'interval', 'interval_min', 'tf', 'timeframe', 'direction', 'side',
    # ranking identity
    'rank_type', 'ranking_type', 'rank', 'rank_position', 'ranking_rank', 'ranking_snapshot_time',
    'snapshot_time', 'rank_type_id', 'market', 'category', 'categoryname',
    # prices / candles
    'open', 'high', 'low', 'close', 'price', 'current_price', 'close_price',
    'volume', 'turnover', 'turnover_yen', 'trading_value', 'vwap',
    'change_rate', 'change_percentage', 'price_delta_1m', 'volume_delta_1m', 'volume_speed',
    # scoring
    'score', 'score_buy', 'score_sell', 'score_total', 'final_score', 'display_score',
    'score_slope', 'score_mtf', 'score_momentum', 'score_volume', 'score_liquidity',
    'ai_score', 'ai_confidence', 'confidence', 'lot_multiplier',
    # MTF / trend
    'mtf', 'mtf_score', 'mtf_buy', 'mtf_sell', 'mtf_direction', 'mtf_aligned',
    'trend', 'trend_1m', 'trend_3m', 'trend_5m',
    # base indicators
    'ma5', 'ma25', 'ma75', 'ema12', 'ema26', 'rsi', 'macd', 'signal', 'hist', 'atr',
    'slope', 'slope_atr_scaled', 'bb_upper', 'bb_middle', 'bb_lower',
    # 1m indicators
    'ma5_1m', 'ma25_1m', 'ma75_1m', 'ema12_1m', 'ema26_1m', 'rsi_1m', 'macd_1m',
    'signal_1m', 'hist_1m', 'atr_1m', 'slope_1m', 'slope_atr_scaled_1m',
    'vwap_1m', 'volume_1m', 'turnover_1m', 'range_pct_1m',
    # 3m indicators
    'ma5_3m', 'ma25_3m', 'ma75_3m', 'ema12_3m', 'ema26_3m', 'rsi_3m', 'macd_3m',
    'signal_3m', 'hist_3m', 'atr_3m', 'slope_3m', 'slope_atr_scaled_3m',
    'vwap_3m', 'volume_3m', 'turnover_3m', 'range_pct_3m',
    # 5m indicators
    'ma5_5m', 'ma25_5m', 'ma75_5m', 'ema12_5m', 'ema26_5m', 'rsi_5m', 'macd_5m',
    'signal_5m', 'hist_5m', 'atr_5m', 'slope_5m', 'slope_atr_scaled_5m',
    'vwap_5m', 'volume_5m', 'turnover_5m', 'range_pct_5m',
    # guard / explanation fields often attached by pipelines
    'guard_reason', 'skip_reason', 'reason', 'entry_reason', 'ai_reason', 'reason_code',
    'liquidity_ok', 'volume_sum', 'avg_volume', 'latest_volume', 'latest_dt',
)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == '':
            return default
        x = float(v)
        if x != x:
            return default
        return x
    except Exception:
        return default


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == '':
            return default
        return int(float(v))
    except Exception:
        return default


def _safe_jsonable(v: Any) -> Any:
    try:
        if v is None:
            return None
        if isinstance(v, (str, int, bool)):
            return v
        if isinstance(v, float):
            return None if v != v else v
        if hasattr(v, 'item'):
            return _safe_jsonable(v.item())
        if isinstance(v, dict):
            return {str(k): _safe_jsonable(x) for k, x in v.items()}
        if isinstance(v, (list, tuple, set)):
            return [_safe_jsonable(x) for x in v]
        return str(v)
    except Exception:
        try:
            return str(v)
        except Exception:
            return None


def _safe_text(v: Any, max_len: int = 20000) -> str:
    try:
        if isinstance(v, str):
            s = v
        else:
            s = json.dumps(_safe_jsonable(v), ensure_ascii=False, default=str, separators=(',', ':'))
    except Exception:
        s = str(v)
    return s[:max_len]


def _row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        if isinstance(row, dict):
            return row.get(key, default)
        if hasattr(row, 'get'):
            return row.get(key, default)
        return getattr(row, key, default)
    except Exception:
        return default


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or '').strip()
        if s.endswith('.0'):
            s = s[:-2]
        return s
    except Exception:
        return ''


def _source_from_row(entry_row: Any) -> str:
    return str(_row_get(entry_row, 'source') or _row_get(entry_row, 'entry_source') or '').upper()


def _ranking_type(entry_row: Any) -> str:
    return str(_row_get(entry_row, 'ranking_type') or _row_get(entry_row, 'rank_type') or _row_get(entry_row, 'categoryname') or '')


def _rank_position(entry_row: Any) -> int:
    return _safe_int(_row_get(entry_row, 'rank_position') or _row_get(entry_row, 'rank') or _row_get(entry_row, 'ranking_rank'), 0)


def _snapshot_time(entry_row: Any) -> str:
    return str(_row_get(entry_row, 'ranking_snapshot_time') or _row_get(entry_row, 'snapshot_time') or _row_get(entry_row, 'datetime') or _row_get(entry_row, 'dt') or '')


def _build_entry_id(symbol: str, side: str, entry_row: Any) -> str:
    source = _source_from_row(entry_row) or 'ENTRY'
    when = str(_row_get(entry_row, 'datetime') or _row_get(entry_row, 'dt') or _snapshot_time(entry_row) or datetime.now().isoformat(timespec='seconds'))
    keep = ''.join(ch for ch in when if ch.isdigit())[:14] or datetime.now().strftime('%Y%m%d%H%M%S')
    return f'{source}-{keep}-{_norm_symbol(symbol)}-{str(side or "").upper() or "NA"}'


def _build_technical_snapshot(entry_row: Any, ai: Any, ai_msg: str, side: str, entry_id: str = '') -> str:
    """Return compact JSON snapshot of summary/ranking entry indicators."""
    try:
        row = dict(entry_row or {}) if isinstance(entry_row, dict) else {}
        if not row and hasattr(entry_row, 'to_dict'):
            try:
                row = dict(entry_row.to_dict())
            except Exception:
                row = {}

        flat = {}
        for key in _TECH_KEYS:
            value = row.get(key, _row_get(entry_row, key))
            if value is not None and value != '':
                flat[key] = _safe_jsonable(value)

        for key, value in row.items():
            sk = str(key)
            if sk in flat:
                continue
            if sk.startswith('display_') or sk.endswith('_score') or sk.startswith('score_'):
                flat[sk] = _safe_jsonable(value)

        by_tf = {}
        for tf in ('1m', '3m', '5m'):
            suffix = '_' + tf
            bucket = {}
            for key, value in row.items():
                sk = str(key)
                if sk.endswith(suffix):
                    bucket[sk[:-len(suffix)]] = _safe_jsonable(value)
            if bucket:
                by_tf[tf] = bucket

        payload = {
            'schema': 'entry_technical_snapshot_v2',
            'entry_id': entry_id,
            'captured_at': datetime.now().isoformat(timespec='seconds'),
            'side': str(side or '').upper(),
            'source': str(flat.get('source') or flat.get('entry_source') or ''),
            'ranking': {
                'ranking_type': _ranking_type(entry_row),
                'rank_position': _rank_position(entry_row),
                'ranking_snapshot_time': _snapshot_time(entry_row),
            },
            'interval_min': _safe_int(flat.get('interval') or flat.get('interval_min'), 0),
            'technical': flat,
            'by_timeframe': by_tf,
            'ai': _safe_jsonable(ai if isinstance(ai, dict) else {}),
            'ai_msg': str(ai_msg or '')[:2000],
        }
        return _safe_text(payload, max_len=20000)
    except Exception:
        return _safe_text({'schema': 'entry_technical_snapshot_v2', 'entry_id': entry_id, 'error': 'build_failed'}, max_len=20000)


def audit_entry_skip(symbol: str, reason: str, detail: dict | None = None) -> None:
    try:
        detail = detail or {}
        side = str(detail.get('side') or '').upper()
        source = str(detail.get('source') or detail.get('entry_source') or '').upper()
        entry_id = detail.get('entry_id') or ''
        record_filter_event(
            datetime=datetime.now().isoformat(timespec='seconds'),
            symbol=str(symbol),
            filter_name=str(reason),
            passed=False,
            detail=_safe_text(detail or {}),
            entry_id=entry_id,
            source=source,
            side=side,
        )
    except Exception:
        return


def audit_candidate_ok(symbol: str, side: str, entry_row: dict, ai: dict, ai_msg: str = '') -> None:
    try:
        entry_id = _build_entry_id(symbol, side, entry_row)
        score = _safe_float(_row_get(entry_row, 'score'), 0.0)
        score_buy = _safe_float(_row_get(entry_row, 'score_buy'), score)
        score_sell = _safe_float(_row_get(entry_row, 'score_sell'), 0.0)
        final_score = _safe_float(
            _row_get(entry_row, 'final_score')
            or _row_get(entry_row, 'score_final')
            or _row_get(entry_row, 'score_total')
            or score,
            score,
        )
        reason_code = str(_row_get(entry_row, 'reason_code') or _row_get(entry_row, 'guard_reason') or '')
        record_candidate_event(
            datetime=str(
                _row_get(entry_row, 'datetime')
                or _row_get(entry_row, 'dt')
                or datetime.now().isoformat(timespec='seconds')
            ),
            symbol=str(symbol),
            side=str(side).upper(),
            source=str(_row_get(entry_row, 'source') or _row_get(entry_row, 'entry_source') or ''),
            interval_min=_safe_int(_row_get(entry_row, 'interval') or _row_get(entry_row, 'interval_min'), 0),
            score_buy=score_buy,
            score_sell=score_sell,
            score_total=_safe_float(_row_get(entry_row, 'score_total'), score),
            final_score=final_score,
            ai_result='AI_OK',
            reason=_safe_text({
                'entry_id': entry_id,
                'ai_reason': ai.get('reason') if isinstance(ai, dict) else '',
                'ai_msg': ai_msg,
                'confidence': ai.get('confidence') if isinstance(ai, dict) else None,
                'entry_reason': _row_get(entry_row, 'reason') or _row_get(entry_row, 'entry_reason') or '',
                'reason_code': reason_code,
                'ranking_type': _ranking_type(entry_row),
                'rank_position': _rank_position(entry_row),
            }),
            technical_snapshot=_build_technical_snapshot(entry_row, ai, ai_msg, side, entry_id=entry_id),
            entry_id=entry_id,
            reason_code=reason_code,
            ranking_type=_ranking_type(entry_row),
            rank_position=_rank_position(entry_row),
            ranking_snapshot_time=_snapshot_time(entry_row),
        )
    except Exception:
        return


def audit_order(
    symbol: str,
    side: str,
    qty: int,
    order_type: str,
    price: Any,
    status: str,
    order_id: str | None = None,
    reason: str | None = None,
    entry_row: dict | None = None,
    ai: dict | None = None,
    ai_msg: str = '',
    entry_id: str | None = None,
) -> None:
    try:
        entry_row = entry_row or {}
        entry_id = entry_id or _build_entry_id(symbol, side, entry_row)
        record_order_event(
            datetime=datetime.now().isoformat(timespec='seconds'),
            symbol=str(symbol),
            side=str(side).upper(),
            qty=_safe_int(qty, 0),
            order_type=str(order_type or ''),
            order_id=str(order_id or ''),
            status=str(status or ''),
            price=_safe_float(price, 0.0),
            filled_price=None,
            cancel_reason=reason,
            reason=reason,
            entry_id=entry_id,
            entry_source=_source_from_row(entry_row),
            entry_mode=str(_row_get(entry_row, 'entry_mode') or _row_get(entry_row, 'trigger_type') or ''),
            technical_snapshot=_build_technical_snapshot(entry_row, ai or {}, ai_msg, side, entry_id=entry_id) if entry_row else '',
        )
    except Exception:
        return
