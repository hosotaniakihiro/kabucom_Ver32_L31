# ============================================================
# File   : core/startup/entry_final_filter_failopen_patch.py
# Version: V2.5-SUMMARY-AI-ATR-REPAIR-BEFORE-FINAL-FILTER
# ------------------------------------------------------------
# AI_OK後に落ちすぎる最終ガードを補正する。
#
# V2.5:
#   - Summary-AI が entry_controller.atr_1m_filter で
#     ATR_1M_FILTER_NG になる前に、summary_history_1m / merged_summary_1m / day_high/day_low
#     から ATR・レンジを補完してから元の atr_1m_filter を再実行する。
#   - 低ボラガードは緩和しない。補完後も元フィルタがNGなら従来通りNG。
#   - Tonosama の既存 history gap fail-open は維持。
#
# V2.4:
#   - env default set を1件ずつWARNING出力しない。
#   - まとめて1行だけ出す。
#   - 起動時のログI/OとWARNING formatter負荷を削減。
# ============================================================
from __future__ import annotations
import logging
import os
from typing import Any
logger = logging.getLogger(__name__)
_PATCHED = False
_ENV_SET: list[str] = []
_ATR_INSUFFICIENT_WORDS = ('1m未生成','1m本数不足','ATR計算不可','symbol列なし','OHLC列不足','no_atr_data','no_atr','atr=None',"'atr': None",'"atr": None','bars','本数不足','未生成','ATR_1M_FILTER_NG','LOW_MOVE_NO_ATR','LOW_MOVE_ATR_TOO_SMALL')


def _setdefault_env(name: str, value: str) -> None:
    try:
        cur = os.getenv(name)
        if cur is None or str(cur).strip() == '':
            os.environ[name] = str(value)
            _ENV_SET.append(f'{name}={value}')
    except Exception:
        pass


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == '': return bool(default)
        return str(v).strip().lower() in {'1','true','yes','y','on','ok','enable','enabled'}
    except Exception: return bool(default)


def _safe_str(v: Any) -> str:
    try: return str(v or '').strip()
    except Exception: return ''


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == '': return float(default)
        return float(str(v).replace(',', ''))
    except Exception: return float(default)


def _row_dict(entry_row: Any) -> dict:
    try:
        if isinstance(entry_row, dict): return entry_row
        if hasattr(entry_row, 'to_dict'):
            d = entry_row.to_dict(); return d if isinstance(d, dict) else {}
    except Exception: pass
    return {}


def _dicts(entry_row: Any) -> list[dict]:
    base = _row_dict(entry_row); out = []
    if base: out.append(base)
    for k in ('_raw','raw','source_row','candidate_raw','entry_conditions','conditions','metrics','features','detail','ai_detail'):
        try:
            v = base.get(k) if isinstance(base, dict) else None
            d = v if isinstance(v, dict) else (v.to_dict() if hasattr(v, 'to_dict') else None)
            if isinstance(d, dict): out.append(d)
        except Exception: pass
    return out


def _is_tonosama_entry(entry_row: Any) -> bool:
    for row in _dicts(entry_row):
        src = _safe_str(row.get('source') or row.get('pipeline_source') or row.get('entry_source')).upper()
        et = _safe_str(row.get('entry_type') or row.get('type') or row.get('strategy')).upper()
        reason = _safe_str(row.get('ai_reason') or row.get('reason') or row.get('source_reason')).upper()
        if src == 'TONOSAMA' or et == 'TONOSAMA' or 'TONOSAMA' in reason: return True
    return False


def _is_summary_ai_entry(entry_row: Any) -> bool:
    for row in _dicts(entry_row):
        src = _safe_str(row.get('source') or row.get('pipeline_source') or row.get('entry_source')).upper()
        et = _safe_str(row.get('entry_type') or row.get('type') or row.get('strategy')).upper()
        reason = _safe_str(row.get('ai_reason') or row.get('reason') or row.get('source_reason') or row.get('model_used')).upper()
        if src in {'SUMMARY_AI','SUMMARY','PUSH_SUMMARY'}: return True
        if et in {'SUMMARY_AI','SUMMARY'}: return True
        if 'SUMMARY_AI' in src or 'SUMMARY_AI' in et or 'SUMMARY_AI' in reason or 'SRC=SUMMARY' in reason: return True
    return False


def _has_explicit_atr(entry_row: Any) -> bool:
    for row in _dicts(entry_row):
        price = _safe_float(row.get('close_price') or row.get('close') or row.get('price') or row.get('current_price'), 0.0)
        atr = _safe_float(row.get('atr_1m') or row.get('atr') or row.get('ATR') or row.get('atr14') or row.get('atr_14'), 0.0)
        if price > 0 and atr > 0: return True
    return False


def _ret_ok(ret: Any) -> bool:
    try:
        if isinstance(ret, tuple) and len(ret) > 0: return bool(ret[0])
        return bool(ret)
    except Exception: return False


def _ret_detail(ret: Any) -> Any:
    try:
        if isinstance(ret, tuple) and len(ret) > 1: return ret[1]
    except Exception: pass
    return None


def _detail_bars(detail: Any) -> float:
    try:
        if isinstance(detail, dict): return _safe_float(detail.get('bars'), -1.0)
    except Exception: pass
    return -1.0


def _detail_atr_missing(detail: Any) -> bool:
    try:
        if isinstance(detail, dict):
            if detail.get('atr') is None or detail.get('atr_1m') is None: return True
            reason = _safe_str(detail.get('reason'))
            if any(w in reason for w in _ATR_INSUFFICIENT_WORDS): return True
    except Exception: pass
    text = _safe_str(detail)
    return any(w in text for w in _ATR_INSUFFICIENT_WORDS)


def _looks_atr_history_gap(entry_row: Any = None, detail: Any = None) -> bool:
    if _has_explicit_atr(entry_row): return False
    if detail is None: return True
    if _detail_atr_missing(detail): return True
    bars = _detail_bars(detail)
    return bool(0 <= bars <= _safe_float(os.getenv('ATR_1M_FILTER_TONOSAMA_MIN_BARS'), 14.0))


def _repair_summary_ai_atr_row(entry_row: Any) -> tuple[Any, dict]:
    """Summary-AI用。補完できたrowを返すだけで、ガード自体は通さない。"""
    base = _row_dict(entry_row)
    symbol = _safe_str(base.get('symbol'))
    try:
        from core.startup import summary_ai_order_builder_range_repair_patch as repair_mod
        repair_fn = getattr(repair_mod, '_repair_row', None)
        if not callable(repair_fn):
            return entry_row, {'repaired': False, 'reason': 'repair_fn_missing'}
        repaired, diag = repair_fn(entry_row, symbol=symbol, source='SUMMARY_AI')
        if isinstance(repaired, dict) and (diag.get('repaired') or diag.get('atr_repaired')):
            try:
                if isinstance(entry_row, dict):
                    entry_row.update({k: repaired[k] for k in ('close','close_price','current_price','price','high','low','high_price','low_price','day_high','day_low','range_pct','intraday_range_pct','atr','atr_1m','ATR') if k in repaired})
            except Exception:
                pass
            return repaired, diag
        return entry_row, diag if isinstance(diag, dict) else {'repaired': False, 'reason': 'no_diag'}
    except Exception as e:
        return entry_row, {'repaired': False, 'reason': 'exception', 'error': str(e)}


def _apply_scalping_defaults() -> None:
    defaults = {
        'ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL':'2','ENTRY_COUNT_SENT_ORDER_AS_DAILY_ENTRY':'1','ENTRY_WINNING_SYMBOL_REENTRY_ENABLED':'1','ENTRY_WINNING_SYMBOL_MAX_DAILY_ENTRIES':'4','ENTRY_WINNING_SYMBOL_MIN_DAILY_PNL':'1','ENTRY_WINNING_SYMBOL_REQUIRE_WIN_GT_LOSS':'1','ENTRY_WINNING_SYMBOL_IGNORE_SENT_ONLY':'1','ENTRY_STOP_AFTER_FIRST_LOSS_ONLY_IF_NET_NEGATIVE':'1',
        'RANGE_5M_FILTER_NG_FAIL_OPEN':'1','LOW_MOVE_TONOSAMA_MIN_RANGE_PCT':'0.005','LOW_MOVE_TONOSAMA_STRONG_RANGE_PCT':'0.010','LOW_MOVE_MIN_RANGE_PCT_HIGH_PRICE':'0.005','LOW_MOVE_MIN_RANGE_PCT_LOW_PRICE':'0.010','LOW_MOVE_RANKING_MIN_RANGE_PCT_HIGH_PRICE':'0.005','LOW_MOVE_RANKING_MIN_RANGE_PCT_LOW_PRICE':'0.008','LOW_MOVE_RANKING_MIN_SCORE_FOR_NO_HIGHLOW':'55.0','LOW_MOVE_RANKING_MIN_ABS_SLOPE':'0.0005','LOW_MOVE_TONOSAMA_MIN_ABS_SLOPE':'0.00005','LOW_MOVE_MIN_ABS_SLOPE_HIGH_PRICE':'0.0001','LOW_MOVE_MIN_ABS_SLOPE_LOW_PRICE':'0.00015',
        'ENTRY_ORDER_MIN_RANGE_PCT':'0.005','ENTRY_ORDER_MIN_ATR_RATIO':'0.0025','ENTRY_ORDER_REQUIRE_ATR':'0','ENTRY_ORDER_REQUIRE_HIGH_LOW':'0','ENTRY_DIRECTION_CONFIRM_MIN_STRENGTH':'1.0','ENTRY_DIRECTION_CONFIRM_STRICT':'0','ENTRY_ORDER_SHORT_MTF_NEUTRAL_MIN_SCORE':'1.0','ENTRY_ORDER_SHORT_MTF_NEUTRAL_EPS':'0.0',
    }
    for k, v in defaults.items(): _setdefault_env(k, v)


def _patch_import_time_constants() -> None:
    try:
        import trading.handlers.entry_order_builder as eob
        eob.ENTRY_ORDER_MIN_RANGE_PCT = _safe_float(os.getenv('ENTRY_ORDER_MIN_RANGE_PCT'), 0.005)
        eob.ENTRY_ORDER_MIN_ATR_RATIO = _safe_float(os.getenv('ENTRY_ORDER_MIN_ATR_RATIO'), 0.0025)
        eob.ENTRY_ORDER_REQUIRE_ATR = _env_bool('ENTRY_ORDER_REQUIRE_ATR', False)
        eob.ENTRY_ORDER_REQUIRE_HIGH_LOW = _env_bool('ENTRY_ORDER_REQUIRE_HIGH_LOW', False)
        logger.warning('[ENTRY FINAL FILTER FAILOPEN] entry_order_builder constants patched min_range=%.4f min_atr=%.4f require_atr=%s require_high_low=%s', eob.ENTRY_ORDER_MIN_RANGE_PCT, eob.ENTRY_ORDER_MIN_ATR_RATIO, eob.ENTRY_ORDER_REQUIRE_ATR, eob.ENTRY_ORDER_REQUIRE_HIGH_LOW)
    except Exception:
        logger.exception('[ENTRY FINAL FILTER FAILOPEN] entry_order_builder constant patch failed')


def install() -> bool:
    global _PATCHED, _ENV_SET
    if _PATCHED: return True
    _ENV_SET = []
    for k, v in {
        'ENTRY_ALLOW_ENTRY_WITHOUT_BOARD':'1','ATR_1M_FILTER_TONOSAMA_HISTORY_FAIL_OPEN':'1','ATR_1M_FILTER_TONOSAMA_MIN_BARS':'14','PENDING_PROTECT_PUSH_SYMBOLS':'1','PENDING_PROTECT_PUSH_MAX_KEEP':'50','ENTRY_BOARD_RETRY_ENABLED':'1','ENTRY_BOARD_RETRY_WAIT_SEC':'4.5','ENTRY_BOARD_RETRY_COUNT':'1','ENTRY_BOARD_RETRY_EXTRA_WAIT_SEC':'0.3','ENTRY_BOARD_RETRY_EXTRA_COUNT':'1','ENTRY_BOARD_RETRY_SYMBOLS_ONLY_PENDING':'0','ENTRY_SHORT_MTF_REQUIRED':'1','ENTRY_SHORT_MTF_REQUIRE_ALL':'1','ENTRY_SHORT_MTF_SLOPE_EPS':'0.0','ENTRY_DAILY_MTF_OPTIONAL':'1','ENTRY_MA5_BREAKOUT_ENABLED':'1','ENTRY_MA5_BREAKOUT_TFS':'3,5','ENTRY_MA5_BREAKOUT_MIN_BAR':'1','ENTRY_MA5_BREAKOUT_MAX_BAR':'3','ENTRY_MA5_BREAKOUT_LOOKBACK':'20','ENTRY_MA5_BREAKOUT_REQUIRE_DATA':'1','ENTRY_MA5_BREAKOUT_DB_BACKFILL':'1','ENTRY_DIRECTION_CONFIRM_RECURSION_FAIL_OPEN':'1','ENTRY_DIRECTION_CONFIRM_ERROR_FAIL_OPEN':'0'
    }.items(): _setdefault_env(k, v)
    _apply_scalping_defaults()
    if _ENV_SET:
        logger.warning('[ENTRY FINAL FILTER FAILOPEN] env defaults set count=%s keys=%s', len(_ENV_SET), [x.split('=',1)[0] for x in _ENV_SET])
    _patch_import_time_constants()
    try:
        import trading.handlers.entry_controller as ec
    except Exception:
        logger.exception('[ENTRY FINAL FILTER FAILOPEN] entry_controller import failed'); return False
    try:
        orig_atr = getattr(ec, 'atr_1m_filter', None)
        if callable(orig_atr) and not getattr(orig_atr, '_summary_ai_atr_repair_wrapper_v25', False):
            def _atr_summary_ai_repair_then_tonosama_failopen(entry_row: Any = None, *args, **kwargs):
                try:
                    ret = orig_atr(entry_row, *args, **kwargs)
                    if _ret_ok(ret):
                        return ret

                    detail = _ret_detail(ret)
                    if _is_summary_ai_entry(entry_row):
                        repaired_row, diag = _repair_summary_ai_atr_row(entry_row)
                        if isinstance(diag, dict) and (diag.get('repaired') or diag.get('atr_repaired')):
                            ret2 = orig_atr(repaired_row, *args, **kwargs)
                            logger.warning(
                                '[ENTRY FINAL FILTER FAILOPEN] atr_1m_filter SUMMARY_AI repaired -> retry result=%s symbol=%s detail=%s',
                                _ret_ok(ret2), _row_dict(entry_row).get('symbol'), diag,
                            )
                            if _ret_ok(ret2):
                                return ret2
                        else:
                            logger.info('[ENTRY FINAL FILTER FAILOPEN] atr_1m_filter SUMMARY_AI repair no-op symbol=%s detail=%s diag=%s', _row_dict(entry_row).get('symbol'), detail, diag)

                    if (not _ret_ok(ret)) and _env_bool('ATR_1M_FILTER_TONOSAMA_HISTORY_FAIL_OPEN', True):
                        if _is_tonosama_entry(entry_row) and _looks_atr_history_gap(entry_row=entry_row, detail=detail):
                            logger.warning('[ENTRY FINAL FILTER FAILOPEN] atr_1m_filter TONOSAMA history gap -> fail-open symbol=%s', _row_dict(entry_row).get('symbol'))
                            return True
                    return ret
                except Exception as e:
                    allow = _is_tonosama_entry(entry_row) and _env_bool('ATR_1M_FILTER_TONOSAMA_ERROR_FAIL_OPEN', False)
                    logger.warning('[ENTRY FINAL FILTER FAILOPEN] atr_1m_filter error tonosama_fail_open=%s err=%s', allow, e, exc_info=False)
                    return bool(allow)
            _atr_summary_ai_repair_then_tonosama_failopen._tonosama_atr_failopen_wrapper_v23 = True
            _atr_summary_ai_repair_then_tonosama_failopen._tonosama_atr_failopen_wrapper_v24 = True
            _atr_summary_ai_repair_then_tonosama_failopen._summary_ai_atr_repair_wrapper_v25 = True
            _atr_summary_ai_repair_then_tonosama_failopen._original_atr_1m_filter = orig_atr
            setattr(ec, 'atr_1m_filter', _atr_summary_ai_repair_then_tonosama_failopen)
            logger.warning('[ENTRY FINAL FILTER FAILOPEN] atr_1m_filter SUMMARY_AI repair + TONOSAMA wrapper installed v2.5')
    except Exception:
        logger.exception('[ENTRY FINAL FILTER FAILOPEN] atr_1m wrapper install failed')
    try:
        orig_range = getattr(ec, 'range_5m_filter', None)
        if callable(orig_range) and not getattr(orig_range, '_range5m_failopen_wrapper_v24', False):
            def _range5m_failopen(entry_row: Any = None, *args, **kwargs):
                try:
                    ret = orig_range(entry_row, *args, **kwargs)
                    if isinstance(ret, tuple): return ret
                    if ret is False and _env_bool('RANGE_5M_FILTER_NG_FAIL_OPEN', True): return True
                    return ret
                except RecursionError:
                    return bool(_env_bool('RANGE_5M_FILTER_RECURSION_FAIL_OPEN', True))
                except Exception:
                    return bool(_env_bool('RANGE_5M_FILTER_ERROR_FAIL_OPEN', True))
            _range5m_failopen._range5m_failopen_wrapper_v23 = True
            _range5m_failopen._range5m_failopen_wrapper_v24 = True
            _range5m_failopen._original_range_5m_filter = orig_range
            setattr(ec, 'range_5m_filter', _range5m_failopen)
            logger.warning('[ENTRY FINAL FILTER FAILOPEN] range_5m_filter wrapper installed v2.4')
    except Exception:
        logger.exception('[ENTRY FINAL FILTER FAILOPEN] range_5m wrapper install failed')
    for mod_name, label in [('core.startup.entry_direction_failclosed_patch','direction guarded patch'),('core.startup.pending_protect_push_patch','pending protect push patch'),('core.startup.board_retry_patch','board retry patch'),('core.startup.entry_mtf_short_required_daily_optional_patch','short MTF daily optional patch'),('core.startup.entry_ma5_breakout_count_patch','MA5 breakout count patch')]:
        try:
            mod = __import__(mod_name, fromlist=['install']); fn = getattr(mod, 'install', None); ok = fn() if callable(fn) else False
            logger.warning('[ENTRY FINAL FILTER FAILOPEN] %s installed=%s', label, ok)
        except Exception:
            logger.exception('[ENTRY FINAL FILTER FAILOPEN] %s install failed', label)
    _PATCHED = True
    logger.warning('[ENTRY FINAL FILTER FAILOPEN] installed v2.5 summary_ai_atr_repair=True atr_failopen=%s range_failopen=%s allow_without_board=%s defaults_count=%s', _env_bool('ATR_1M_FILTER_TONOSAMA_HISTORY_FAIL_OPEN', True), _env_bool('RANGE_5M_FILTER_NG_FAIL_OPEN', True), os.getenv('ENTRY_ALLOW_ENTRY_WITHOUT_BOARD'), len(_ENV_SET))
    return True
try: install()
except Exception: logger.exception('[ENTRY FINAL FILTER FAILOPEN] auto install failed')
__all__ = ['install']
