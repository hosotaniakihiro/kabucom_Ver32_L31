# ============================================================
# File   : core/startup/symbol_performance_guard_patch.py
# Version: Ver01-SYMBOL-PERFORMANCE-GUARD-PATCH
# ------------------------------------------------------------
# entry_controller の AI_OK 後候補に銘柄別勝率ガードを差し込む。
#
# 既存の entry_controller.py を大きく壊さず、
# _build_scored_candidates() の戻り値をフィルタする runtime patch。
#
# AI_OK後でも、過去成績が悪い銘柄はENTRY候補から除外する。
# ============================================================

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_BUILD_SCORED_CANDIDATES = None


def _safe_symbol(item: dict, fallback: str = '') -> str:
    try:
        return str(
            item.get('symbol')
            or (item.get('entry_row') or {}).get('symbol')
            or fallback
            or ''
        ).strip().upper()
    except Exception:
        return str(fallback or '').strip().upper()


def _save_audit_skip(symbol: str, item: dict, detail: dict) -> None:
    try:
        from trading.runtime_persistence.trade_audit_trail import save_entry_decision

        entry_row = item.get('entry_row') if isinstance(item, dict) else {}
        ai = item.get('ai') if isinstance(item, dict) else {}
        side = str(item.get('side') or (entry_row or {}).get('side') or '').upper()

        save_entry_decision(
            symbol=symbol,
            side=side,
            decision='SKIP',
            row=entry_row if isinstance(entry_row, dict) else {},
            ai=ai if isinstance(ai, dict) else {},
            skip_reason='SYMBOL_PERFORMANCE_GUARD_NG',
            payload={'symbol_performance_guard': detail},
        )
    except Exception:
        logger.debug('[SYMBOL PERF PATCH] audit skip save failed symbol=%s', symbol, exc_info=True)


def _filter_candidates(symbol: str, candidates: list[dict]) -> list[dict]:
    if not candidates:
        return candidates

    try:
        from trading.entry.symbol_performance_guard import is_symbol_performance_ok
    except Exception:
        logger.exception('[SYMBOL PERF PATCH] import guard failed; keep candidates')
        return candidates

    kept: list[dict] = []
    blocked: list[dict] = []

    for item in candidates:
        try:
            sym = _safe_symbol(item, fallback=symbol)
            ok, detail = is_symbol_performance_ok(sym)
            if ok:
                kept.append(item)
            else:
                blocked.append({'symbol': sym, 'detail': detail})
                _save_audit_skip(sym, item, detail)
                logger.warning(
                    '[SYMBOL PERF PATCH] ENTRY candidate blocked symbol=%s detail=%s',
                    sym,
                    detail,
                )
        except Exception:
            logger.exception('[SYMBOL PERF PATCH] filter failed item=%s; keep item', item)
            kept.append(item)

    if blocked:
        logger.warning(
            '[SYMBOL PERF PATCH] filtered candidates original=%d kept=%d blocked=%d blocked_detail=%s',
            len(candidates),
            len(kept),
            len(blocked),
            blocked,
        )

    return kept


def install() -> bool:
    global _INSTALLED, _ORIGINAL_BUILD_SCORED_CANDIDATES
    if _INSTALLED:
        return True

    try:
        import trading.handlers.entry_controller as ec

        original = getattr(ec, '_build_scored_candidates', None)
        if not callable(original):
            logger.warning('[SYMBOL PERF PATCH] entry_controller._build_scored_candidates not callable')
            return False

        if getattr(original, '_symbol_performance_guard_patched', False):
            _INSTALLED = True
            return True

        _ORIGINAL_BUILD_SCORED_CANDIDATES = original

        def _wrapped_build_scored_candidates(symbol: str, entries: list[dict], open_position_symbols: set[str], boost_active: bool, pipeline_source=None, interval=None):
            candidates = original(
                symbol,
                entries,
                open_position_symbols,
                boost_active,
                pipeline_source=pipeline_source,
                interval=interval,
            )
            try:
                return _filter_candidates(symbol, candidates)
            except Exception:
                logger.exception('[SYMBOL PERF PATCH] wrapper filter failed symbol=%s; keep original candidates', symbol)
                return candidates

        _wrapped_build_scored_candidates._symbol_performance_guard_patched = True
        ec._build_scored_candidates = _wrapped_build_scored_candidates
        _INSTALLED = True
        logger.warning('[SYMBOL PERF PATCH] installed entry_controller._build_scored_candidates wrapper')
        return True

    except Exception:
        logger.exception('[SYMBOL PERF PATCH] install failed')
        return False
