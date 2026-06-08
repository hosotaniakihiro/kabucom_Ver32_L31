from __future__ import annotations
import logging, threading, time, os
from typing import Any
logger = logging.getLogger(__name__)
_INSTALLED = False
OK_TOKENS = ('TONOSAMA_DEDICATED_GATE_OK', 'TONOSAMA_OK', 'RULE_OK')
BAD_TOKENS = ('TONOSAMA_BUY_REJECT', 'TONOSAMA_SELL_REJECT', 'TONOSAMA_REJECT', 'HARD_REJECT', 'VOLUME_DIRECTION_REJECT', 'NO_STRONG_3M5M', '3M5M_CANDLE_REJECT')

def _txt(*vals: Any) -> str:
    try: return ' '.join(str(v or '') for v in vals).upper()
    except Exception: return ''

def _is_tonosama(row: Any) -> bool:
    return isinstance(row, dict) and (str(row.get('source') or '').upper() == 'TONOSAMA' or str(row.get('entry_type') or '').upper() == 'TONOSAMA')

def _score(row: Any, side: Any) -> float:
    try:
        if not isinstance(row, dict): return 0.0
        vals = [row.get('score'), row.get('_tonosama_score'), row.get('final_score')]
        vals += [row.get('score_sell')] if str(side or row.get('side') or '').upper() == 'SELL' else [row.get('score_buy')]
        out = 0.0
        for v in vals:
            try: out = max(out, abs(float(v)))
            except Exception: pass
        return out
    except Exception: return 0.0

def _ai_text(ai: Any) -> str:
    if isinstance(ai, dict): return _txt(ai.get('reason'), ai.get('gate_reason'), ai.get('ai_reason'), ai.get('detail'))
    return _txt(ai)

def _has_ok_without_bad(text: str) -> bool:
    return any(t in text for t in OK_TOKENS) and not any(t in text for t in BAD_TOKENS)

def _patch_once() -> bool:
    try:
        import trading.handlers.entry_controller as ec
        cur = getattr(ec, '_passes_ai_gate', None)
        if not callable(cur): return False
        if getattr(cur, '_tonosama_dedicated_ok_final_accept_v2', False) or getattr(cur, '_tonosama_dedicated_ok_final_accept_v1', False): return True
        orig = cur
        def patched(row, ai, side):
            if _is_tonosama(row):
                text = _ai_text(ai)
                if _has_ok_without_bad(text):
                    s = _score(row, side)
                    if s >= 0.01:
                        logger.warning('[TONOSAMA DEDICATED OK FINAL ACCEPT] accept symbol=%s side=%s score=%.4f reason=%s', row.get('symbol') if isinstance(row, dict) else None, side, s, text[:200])
                        return True, f'TONOSAMA_OK score={s:.3f}'
            ret = orig(row, ai, side)
            try:
                if _is_tonosama(row) and isinstance(ret, tuple) and ret and ret[0] is False:
                    reason = ret[1] if len(ret) > 1 else ''
                    text = _txt(reason, _ai_text(ai))
                    if _has_ok_without_bad(text):
                        s = _score(row, side)
                        if s >= 0.01:
                            logger.warning('[TONOSAMA DEDICATED OK FINAL ACCEPT] override NG->OK symbol=%s side=%s score=%.4f reason=%s', row.get('symbol') if isinstance(row, dict) else None, side, s, text[:200])
                            return True, f'TONOSAMA_OK score={s:.3f}'
            except Exception: pass
            return ret
        patched._tonosama_dedicated_ok_final_accept_v1 = True
        patched._tonosama_dedicated_ok_final_accept_v2 = True
        patched._original = orig
        ec._passes_ai_gate = patched
        logger.warning('[TONOSAMA DEDICATED OK FINAL ACCEPT] patched _passes_ai_gate v2')
        return True
    except Exception:
        logger.exception('[TONOSAMA DEDICATED OK FINAL ACCEPT] patch failed')
        return False

def _watch():
    loops = max(1, min(int(float(os.getenv('TONOSAMA_DEDICATED_OK_WATCH_LOOPS', '8') or 8)), 30))
    sleep_sec = max(0.5, min(float(os.getenv('TONOSAMA_DEDICATED_OK_WATCH_SLEEP_SEC', '2.0') or 2.0), 5.0))
    for i in range(loops):
        ok = _patch_once()
        if i in (0, loops - 1): logger.warning('[TONOSAMA DEDICATED OK FINAL ACCEPT] enforce v2 i=%s/%s ok=%s', i, loops, ok)
        time.sleep(sleep_sec)

def install() -> bool:
    global _INSTALLED
    if _INSTALLED: return True
    ok = _patch_once()
    threading.Thread(target=_watch, name='tonosama-dedicated-ok-final-accept', daemon=True).start()
    _INSTALLED = True
    logger.warning('[TONOSAMA DEDICATED OK FINAL ACCEPT] installed V2 watcher=True ok=%s', ok)
    return True
try: install()
except Exception: logger.exception('[TONOSAMA DEDICATED OK FINAL ACCEPT] auto install failed')
__all__ = ['install']
