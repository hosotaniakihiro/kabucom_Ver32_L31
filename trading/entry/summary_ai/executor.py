# ============================================================
# File   : trading/entry/summary_ai/executor.py
# Version: REV12-INLINE-FRESH-ONE-BY-ONE-TIMEOUT-CONTINUE
# ------------------------------------------------------------
# AI_OK rows -> approved_rows -> entry_pipeline.
#
# REV12:
#   - Also inlined core/startup/summary_ai_direct_timeout_continue_patch.py
#     (V15), discovered mid-merge to be a 5th real patch layered on top of
#     summary_ai_async_direct_dispatch_patch.py's _fallback_direct_dispatch
#     extension point. _dd_fallback_direct_dispatch is now the "fresh
#     one-by-one" version: dispatch batch size is fixed to 1 so a slow
#     board/snapshot call for one symbol cannot delay siblings, a per-candidate
#     timeout only drops that candidate (does not abort the whole retry run),
#     and candidates older than SUMMARY_AI_OK_VALID_SEC are skipped so a stale
#     AI_OK decision never gets dispatched late. The pending-registration
#     freshness guard in that same patch file (wrapping the separately-chained
#     trading.summary.summary_entry.register_pending_entries, which already
#     has 2 unrelated patches of its own) was left as-is -- out of scope for
#     this merge.
#
# REV11:
#   - Inlined the 4 monkeypatches that used to wrap execute_ai_ok_entries_bulk
#     at runtime (core/startup/summary_ai_blowoff_prefilter_patch.py,
#     summary_ai_async_entry_patch.py, summary_ai_async_direct_dispatch_patch.py,
#     summary_ai_low_move_softpass_patch.py's rolling-retry part). The install
#     order in main.py/sitecustomize.py made low_move_softpass's rolling-retry
#     wrapper the outermost layer in production, and it never delegated to the
#     wrapped chain in its success path (only on rolling_retry-disabled/exception
#     fallback) -- so blowoff_prefilter's ai_results filtering (removing
#     dangerous blow-off-top candidates) was silently never applied to real
#     order placement. Fixed by running the blowoff filter unconditionally
#     first, before the rolling-retry logic ever sees ai_results.
#   - _select_ai_ok_items / execute_entry_pipeline / _low_move_hard_block /
#     _range_5m_filter_from_entry_row / _filter_blowoff / _positive_result /
#     _entry_price_bounds remain separate monkeypatch targets (not part of this
#     merge) and are untouched.
#
# REV10:
#   - no-order cleanup now always includes every approved row symbol, even when
#     result contains one attempted symbol. Rolling retry used to prune only the
#     final attempted symbol, leaving prior STRICT_BOARD_MISSING / ATR_NG
#     SUMMARY_AI pending rows in global_data.pending_entries.
#   - Board-missing remains fail-close; this only prevents stale pending buildup.
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import math
import os
import sys
import threading
import time
from collections import Counter, deque
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd

from .utils import get_bulk_entry_pipeline, is_market_open, safe_float

logger = logging.getLogger(__name__)

DEFAULT_MAX_ENTRIES = 3
DEFAULT_MIN_BUY_APPROVED = 0
DEFAULT_MAX_PRICE_FOR_100_SHARE_ENTRY = 7000.0
DEFAULT_MIN_PRICE_FOR_ENTRY = 3000.0
VERSION = "REV12-INLINE-FRESH-ONE-BY-ONE-TIMEOUT-CONTINUE"
_FILTER_WATCHER_STARTED = False


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0"):
            ss = s[:-2]
            if ss.isdigit():
                return ss
        return s
    except Exception:
        return ""


def _norm_side(v: Any, default: str = "BUY") -> str:
    try:
        s = str(v or default).strip().upper()
        return s if s in {"BUY", "SELL"} else default
    except Exception:
        return default


def _as_dict(v: Any) -> Dict[str, Any]:
    return dict(v) if isinstance(v, dict) else {}


def _dict_sources(item: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    ai_row = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    return item, ai_row, src


def _pick_symbol(item: Dict[str, Any]) -> str:
    ai_row = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    return _norm_symbol(item.get("symbol") or ai_row.get("symbol") or src.get("symbol"))


def _pick_side(item: Dict[str, Any]) -> str:
    ai_row = _as_dict(item.get("ai_row"))
    src = _as_dict(item.get("source_row"))
    return _norm_side(
        item.get("side") or item.get("ai_side") or ai_row.get("side") or ai_row.get("ai_side")
        or ai_row.get("entry_decision") or src.get("side") or src.get("ai_side") or src.get("entry_decision"),
        "BUY",
    )


def _row_side(item: Dict[str, Any]) -> str:
    return _pick_side(item)


def _pick_first_positive(item: Dict[str, Any], keys: Sequence[str]) -> float:
    for d in _dict_sources(item):
        for k in keys:
            x = safe_float(d.get(k), 0.0)
            if x > 0:
                return x
    return 0.0


def _pick_price(item: Dict[str, Any]) -> float:
    return _pick_first_positive(item, ("close_price", "price", "current_price", "close", "last_price", "CurrentPrice"))


def _pick_high(item: Dict[str, Any]) -> float:
    return _pick_first_positive(item, ("high", "high_price", "HighPrice", "day_high"))


def _pick_low(item: Dict[str, Any]) -> float:
    return _pick_first_positive(item, ("low", "low_price", "LowPrice", "day_low"))


def _pick_volume(item: Dict[str, Any]) -> float:
    return _pick_first_positive(item, ("volume", "Volume", "trading_volume", "TradingVolume", "出来高"))


def _pick_turnover(item: Dict[str, Any]) -> float:
    for d in _dict_sources(item):
        for k in ("turnover", "trading_value", "TradingValue", "売買代金"):
            x = safe_float(d.get(k), 0.0)
            if x > 0:
                return x
    price = _pick_price(item)
    vol = _pick_volume(item)
    return price * vol if price > 0 and vol > 0 else 0.0


# ------------------------------------------------------------
# range_pct の単位 (2026-09-01 に統一)
# ------------------------------------------------------------
# range_pct は **百分率 (percent)** で扱う。1.88% は 1.88。
#   range_pct = (high - low) / close * 100
#
# それ以前は
#   _pick_range_pct        比率 (0.0188) を返す
#   _blowoff_range_pct_value  raw<=0.5 なら 100 倍する推測変換
#   SUMMARY_AI_BLOWOFF_DANGER_RANGE_PCT=2.5   「2.5%」のつもりの閾値
# が同居しており、0.0188 >= 2.5 が成立しないため blowoff 危険判定が
# 一度も発火していなかった。計算・閾値・ログをすべて percent に揃える。
#
# 閾値の既定値も percent へ直した:
#   SUMMARY_AI_EXECUTOR_MIN_RANGE_PCT     0.005 (比率) -> 0.5 (percent)
#   SUMMARY_AI_STRATEGY_MIN_RANGE_PCT_SOFT 0.004 (比率) -> 0.4 (percent)
# 既存の ENV / settings に比率で書かれた値が残っていても壊れないよう、
# _as_percent_threshold() が入力境界で 1 回だけ換算する。
RANGE_PCT_UNIT = "percent"
RANGE_PCT_SCALE = 100.0

# これ未満は「比率で書かれた古い設定」とみなして 100 倍する。
# 0.05% 未満の値幅を閾値にする運用は無いので誤変換しない。
_RANGE_PCT_RATIO_HINT = 0.05


def _as_percent_threshold(value: float, *, name: str = "") -> float:
    """閾値を percent に正規化する (入力境界で 1 回だけ)。"""
    try:
        v = float(value)
    except Exception:
        return 0.0
    if 0.0 < v < _RANGE_PCT_RATIO_HINT:
        converted = v * RANGE_PCT_SCALE
        logger.warning(
            "[SUMMARY AI EXECUTOR] range 閾値が比率で指定されている name=%s raw=%s -> percent=%s "
            "(range_pct は percent 単位。設定側も percent へ直すこと)",
            name or "-", v, converted,
        )
        return converted
    return v


def _pick_range_pct(item: Dict[str, Any]) -> float:
    """その足の値幅を percent で返す。0.0 は「値幅ゼロ」ではなく未算出も含む。"""
    for d in _dict_sources(item):
        for k in ("range_pct", "intrabar_range_pct", "_intrabar_range_pct"):
            x = safe_float(d.get(k), 0.0)
            if x > 0:
                return x
    high = _pick_high(item)
    low = _pick_low(item)
    close = _pick_price(item)
    if close > 0 and high > 0 and low > 0 and high > low:
        return abs(high - low) / close * RANGE_PCT_SCALE
    return 0.0


# ============================================================
# F: REAL_AI と fail-open を数える
# ------------------------------------------------------------
# AI.entry_gate は fail-open 行に ai_inference=False /
# model_used="FALLBACK_NO_AI:..." / confidence_source="fixed_failopen:..."
# を必ず刻んでいるが、集計側は allow だけを見て AI_OK と数えていた。
# ここでは判定を変えず、内訳を数えられるようにする。
# ============================================================

AI_SOURCE_REAL = "REAL_AI_OK"
AI_SOURCE_FALLBACK = "FALLBACK_OK"
AI_SOURCE_UNKNOWN = "AI_SOURCE_UNKNOWN"


def classify_ai_source(item: Dict[str, Any]) -> str:
    """1 候補が実 AI 推論由来か fail-open 由来かを返す。"""
    for d in _dict_sources(item):
        if not isinstance(d, dict):
            continue
        if d.get("ai_inference") is False:
            return AI_SOURCE_FALLBACK
        src = str(d.get("confidence_source") or "")
        if src.startswith("fixed_failopen"):
            return AI_SOURCE_FALLBACK
        model = str(d.get("model_used") or "")
        if model.startswith("FALLBACK_NO_AI"):
            return AI_SOURCE_FALLBACK
    for d in _dict_sources(item):
        if isinstance(d, dict) and d.get("ai_inference") is True:
            return AI_SOURCE_REAL
    return AI_SOURCE_UNKNOWN


def live_requires_real_ai() -> bool:
    """実売買で AI 推論由来だけを発注対象にするか (既定 True = fail-closed)。"""
    return _env_bool("SUMMARY_AI_LIVE_REQUIRE_REAL_AI", True)


def drop_failopen_for_live(
    ai_results: Any, *, dry_run: bool, interval: Any = None
) -> List[Dict[str, Any]]:
    """order path へ渡す前に fail-open 由来の AI_OK を落とす。

    AI.entry_gate は救済行に
        ai_inference=False / model_used="FALLBACK_NO_AI:..." /
        confidence_source="fixed_failopen:..."
    を刻んでいる。これは AI 推論結果ではなく固定値の fail-open なので、
    実売買 (dry_run=False) の新規 ENTRY 根拠にしてはいけない。

    dry_run では落とさない (診断・候補表示は従来どおり)。
    """
    items = [x for x in (ai_results or [])]
    if dry_run or not live_requires_real_ai():
        return items

    kept: List[Dict[str, Any]] = []
    dropped: List[Dict[str, Any]] = []
    for x in items:
        if isinstance(x, dict) and bool(x.get("allow")) and classify_ai_source(x) == AI_SOURCE_FALLBACK:
            dropped.append(x)
            continue
        kept.append(x)

    if dropped:
        logger.error(
            "[SUMMARY AI EXECUTOR][LIVE FAIL-CLOSED] fail-open 由来の AI_OK を発注対象から除外した "
            "interval=%s dropped=%d kept_ok=%d symbols=%s "
            "(AI 推論ではない固定 confidence のため実売買では ENTRY させない。"
            "SUMMARY_AI_LIVE_REQUIRE_REAL_AI=0 で従来動作)",
            interval, len(dropped),
            sum(1 for x in kept if isinstance(x, dict) and x.get("allow")),
            [
                {
                    "symbol": _pick_symbol(x),
                    "side": _pick_side(x),
                    "model": str(x.get("model_used") or "")[:40],
                    "conf_source": str(x.get("confidence_source") or "")[:40],
                }
                for x in dropped[:10]
            ],
        )
    return kept


def _ai_source_counts(items: Any) -> Counter:
    c: Counter = Counter()
    try:
        for it in items or []:
            if isinstance(it, dict):
                c[classify_ai_source(it)] += 1
    except Exception:
        logger.debug("[SUMMARY AI FUNNEL] ai source count failed", exc_info=True)
    return c


def _pick_hist_len(item: Dict[str, Any]) -> float:
    """この銘柄の履歴本数。足りていなければ range は「小さい」ではなく「出せない」。"""
    for d in _dict_sources(item):
        for k in ("symbol_hist_len", "hist_len", "bar_count", "history_len"):
            x = safe_float(d.get(k), -1.0)
            if x >= 0:
                return x
    return -1.0


# ============================================================
# range_pct=0 の切り分け
# ------------------------------------------------------------
# range_pct は _pick_range_pct() の通り「その足の中の値幅」(intrabar) で、
# 5 分レンジではない。仕様どおり。
#
# 問題は high == low == close になった時に、それが
#   (a) 本当にその足で値が動かなかった (真の低ボラ)
#   (b) その足が 1 tick しか無く値幅を出せない (データ不足)
# のどちらか区別できず、両方 low_move_range_too_small として
# 落ちていたこと。ログからは (b) と (a) が見分けられなかった。
#
# ここでは判定結果 (落とすこと) は変えない。理由を分けるだけ。
# 不足データを 0 で埋めて通すことはしない。
# ============================================================

_RANGE_NOT_READY_MIN_HIST = 2.0


def _classify_zero_range(item: Dict[str, Any], high: float, low: float, close: float) -> Tuple[str, Dict[str, Any]]:
    """range が閾値未満のとき、その理由を data_not_ready と低ボラに分ける。

    range の定義は trading.entry.summary_ai.range_defs に集約した。
    _pick_high/_pick_low は day_high/day_low へフォールバックするため、
    「その足で動いたか」を見たいのに当日値幅が混ざることがある。
    ここでは用途別 (intrabar / day) を別々に実測してログへ残す。
    """
    hist_len = _pick_hist_len(item)
    flat_bar = bool(high > 0 and low > 0 and high == low)
    hl_missing = bool(high <= 0 or low <= 0)

    detail = {
        "high": high,
        "low": low,
        "close": close,
        "hist_len": hist_len,
        "flat_bar": flat_bar,
        "high_low_missing": hl_missing,
    }
    try:
        from .range_defs import compute_day_range_pct, compute_intrabar_range_pct, describe

        intrabar = compute_intrabar_range_pct(item)
        day = compute_day_range_pct(item)
        detail["range_intrabar"] = describe(intrabar)
        detail["range_day"] = describe(day)
        # 判定に使った値がどちらの定義由来かを明示する
        # (mixed = 1本足 high/low が無く day_high/day_low が使われた可能性)。
        detail["range_source_mixed"] = bool(
            intrabar.get("state") != "OK" and day.get("state") == "OK"
        )
        if intrabar.get("state") != "OK":
            return "data_not_ready_intrabar_range", detail
    except Exception:
        logger.debug("[SUMMARY AI PREFILTER] range_defs probe failed", exc_info=True)

    if hl_missing:
        return "data_not_ready_high_low_missing", detail
    if flat_bar and 0 <= hist_len < _RANGE_NOT_READY_MIN_HIST:
        # 1 本しか履歴が無い足の high==low==close は「動かなかった」ではなく
        # 「値幅を測れていない」。PUSH が薄いときにここへ来る。
        return "data_not_ready_single_bar_no_range", detail
    return "low_move_range_too_small", detail


def _score_for_side(item: Dict[str, Any]) -> float:
    side = _pick_side(item)
    if side == "SELL":
        return max(safe_float(item.get("sell_score")), abs(safe_float(item.get("score_total"))), abs(safe_float(item.get("final_score"))))
    return max(safe_float(item.get("buy_score")), safe_float(item.get("score_total")), safe_float(item.get("final_score")))


def _row_score_for_side(item: Dict[str, Any]) -> float:
    return _score_for_side(item)


def _sort_key_core(item: Dict[str, Any]) -> tuple[float, float, float]:
    side = _pick_side(item)
    return (safe_float(item.get("confidence")), _score_for_side(item), safe_float(item.get("sell_score")) if side == "SELL" else safe_float(item.get("buy_score")))


def _price_bounds() -> tuple[float, float, dict[str, Any]]:
    min_price = DEFAULT_MIN_PRICE_FOR_ENTRY
    max_price = DEFAULT_MAX_PRICE_FOR_100_SHARE_ENTRY
    diag: dict[str, Any] = {"source": "fallback"}
    try:
        from trading.entry.entry_budget import get_entry_min_price, get_entry_max_price, get_effective_entry_max_price, get_max_entry_oneshot_yen, get_order_lot_size
        min_price = float(get_entry_min_price())
        max_price = float(get_effective_entry_max_price() or get_entry_max_price() or max_price)
        diag.update({"source": "entry_budget", "entry_min_price": min_price, "entry_max_price_effective": max_price, "max_oneshot_yen": float(get_max_entry_oneshot_yen()), "lot_size": int(get_order_lot_size())})
    except Exception:
        pass
    min_price = _env_float("SUMMARY_AI_ENTRY_MIN_PRICE", _env_float("ENTRY_MIN_PRICE", min_price))
    max_price = _env_float("SUMMARY_AI_ENTRY_MAX_PRICE_FOR_100_SHARE", _env_float("ENTRY_MAX_PRICE", max_price))
    diag.update({"effective_min_price": min_price, "effective_max_price": max_price})
    return min_price, max_price, diag


def _entry_price_bounds() -> tuple[float, float, dict[str, Any]]:
    return _price_bounds()


def _is_trade_restricted_symbol(symbol: str) -> tuple[bool, Any]:
    try:
        from global_state import global_data
        root = getattr(global_data, "trade_restricted", {}) or {}
        until = root.get(symbol)
        if not until:
            return False, None
        if isinstance(until, dt.datetime) and dt.datetime.now() >= until:
            try:
                root.pop(symbol, None)
            except Exception:
                pass
            return False, None
        return True, until
    except Exception:
        return False, None


def _is_trade_restricted(symbol: str) -> bool:
    return bool(_is_trade_restricted_symbol(symbol)[0])


def _sell_credit_prefilter_norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    return s[:-2] if s.endswith(".0") else s


def _is_sell_reject_cached_core(symbol: str, side: str) -> tuple[bool, Any]:
    if side != "SELL":
        return False, None
    try:
        from AI.sell_order_reject_cache import is_sell_rejected, get_sell_reject_reason
        if bool(is_sell_rejected(symbol)):
            try:
                return True, get_sell_reject_reason(symbol)
            except Exception:
                return True, "sell_reject_cache"
    except Exception:
        pass
    return False, None


def _is_sell_reject_cached(symbol: str, side: str) -> tuple[bool, Any]:
    """SELL却下キャッシュ + 信用売り可否ガード (公開エントリポイント)。

    旧 core/startup/summary_ai_sell_credit_prefilter_patch がこの関数を
    (_is_sell_reject_cached_core の逐語的な再実装 + 信用売り可否チェック追加で)
    無条件に差し替えていた。_is_sell_reject_cached_core は素の却下キャッシュ判定のみで
    到達不能 (旧パッチは _original を保持するだけで一度も委譲していなかった)。
    """
    symbol_s = _sell_credit_prefilter_norm_symbol(symbol)
    side_s = _norm_side(side)
    if side_s != "SELL" or not symbol_s:
        return False, None

    try:
        from AI.sell_order_reject_cache import is_sell_rejected, get_sell_reject_reason
        if is_sell_rejected(symbol_s):
            return True, {"reason": "sell_reject_cache", "detail": str(get_sell_reject_reason(symbol_s))}
    except Exception:
        pass

    try:
        from AI.sell_credit_guard import can_sell_symbol
        if not can_sell_symbol(symbol_s, default=False):
            logger.warning(
                "[SUMMARY AI EXECUTOR] SELL candidate removed before approved symbol=%s reason=sell_credit_guard_ng",
                symbol_s,
            )
            return True, {"reason": "sell_credit_guard_ng"}
    except Exception:
        logger.exception("[SUMMARY AI EXECUTOR] sell credit guard failed fail-open symbol=%s", symbol_s)
        return False, None

    return False, None


def _daily_risk_block_reason(symbol: str, side: str) -> tuple[bool, str, Dict[str, Any]]:
    if not _env_bool("SUMMARY_AI_PRE_FILTER_DAILY_RISK", True):
        return False, "", {}
    # 旧 core/startup/entry_daily_risk_runtime_patch の _risk_block_reason を
    # getattr で探していたが、同パッチは trading/handlers/entry_controller.py の
    # _daily_risk_block_reason へインライン化され空スタブになっており、
    # この参照は常に None を引いて fail-open していた (= ここでは何も判定しない)。
    # 実際の日次リスク判定は entry_controller._daily_risk_block_reason が行う。
    # 挙動を変えないため、ここでは従来どおり fail-open のままにしている。
    return False, "", {}


def _passes_strict_candidate_quality_core(item: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
    if not _env_bool("SUMMARY_AI_EXECUTOR_STRICT_QUALITY_PREFILTER", True):
        return True, {}
    symbol = _pick_symbol(item)
    side = _pick_side(item)
    # 2026-10-20: 買いは 1 分足 1 本の値幅 (0.5%) では落とさない (ユーザー判断「買う回数を増やす」)。
    #   ランキング由来の行は 1 分足が high=low=close の平らな足になりやすく、10/20 11 時台は
    #   買い候補 48〜53 件が毎回すべてここで落ちていた。値動きの小ささは発注前の VOL FILTER
    #   (5 分値幅 0.35%・当日値幅 1%) が見る。SUMMARY_AI_EXECUTOR_BUY_LOW_MOVE_CHECK=1 で戻す。
    if str(side or "").upper() == "BUY" and not _env_bool("SUMMARY_AI_EXECUTOR_BUY_LOW_MOVE_CHECK", False):
        return True, {}
    range_pct = _pick_range_pct(item)
    min_range_pct = _as_percent_threshold(
        _env_float("SUMMARY_AI_EXECUTOR_MIN_RANGE_PCT", _env_float("SUMMARY_AI_LOW_MOVE_MIN_RANGE_PCT", 0.5)),
        name="SUMMARY_AI_EXECUTOR_MIN_RANGE_PCT",
    )
    if min_range_pct > 0 and range_pct < min_range_pct:
        high = _pick_high(item)
        low = _pick_low(item)
        close = _pick_price(item)
        reason, range_detail = _classify_zero_range(item, high, low, close)
        # 判定は変えない (どちらでも落とす)。ログで区別できるようにするだけ。
        return False, {
            "symbol": symbol,
            "side": side,
            "reason": reason,
            "range_pct": range_pct,
            "min_range_pct": min_range_pct,
            "score": _score_for_side(item),
            "price": close,
            **range_detail,
        }
    return True, {}


def _base_filter_blocked_ai_ok_items_core(ok_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not ok_items:
        return []
    min_price, max_price, diag = _price_bounds()
    min_volume = _env_float("SUMMARY_AI_EXECUTOR_MIN_VOLUME", _env_float("ENTRY_MIN_VOLUME", 30000.0))
    min_turnover = _env_float("SUMMARY_AI_EXECUTOR_MIN_TURNOVER", _env_float("ENTRY_MIN_TURNOVER", 10000000.0))
    kept: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for item in ok_items:
        symbol = _pick_symbol(item)
        side = _pick_side(item)
        price = _pick_price(item)
        volume = _pick_volume(item)
        turnover = _pick_turnover(item)
        if price > 0 and min_price > 0 and price < min_price:
            skipped.append({"symbol": symbol, "side": side, "reason": "price_below_min", "price": price})
            continue
        if price > 0 and max_price > 0 and price > max_price:
            skipped.append({"symbol": symbol, "side": side, "reason": "price_over_max", "price": price})
            continue
        if volume > 0 and volume < min_volume:
            skipped.append({"symbol": symbol, "side": side, "reason": "low_volume", "volume": volume, "min_volume": min_volume})
            continue
        if turnover > 0 and turnover < min_turnover:
            skipped.append({"symbol": symbol, "side": side, "reason": "low_turnover", "turnover": turnover, "min_turnover": min_turnover})
            continue
        restricted, until = _is_trade_restricted_symbol(symbol)
        if restricted:
            skipped.append({"symbol": symbol, "side": side, "reason": "trade_restricted", "until": str(until)})
            continue
        sell_cached, sell_reason = _is_sell_reject_cached(symbol, side)
        if sell_cached:
            skipped.append({"symbol": symbol, "side": side, "reason": "sell_reject_cached", "detail": str(sell_reason)})
            continue
        risk_blocked, risk_reason, risk_detail = _daily_risk_block_reason(symbol, side)
        if risk_blocked:
            skipped.append({"symbol": symbol, "side": side, "reason": risk_reason, "detail": risk_detail})
            continue
        quality_ok, quality_detail = _passes_strict_candidate_quality(item)
        if not quality_ok:
            skipped.append(quality_detail)
            continue
        kept.append(item)
    if skipped:
        # D/F: 何件がどの理由で落ちたか、AI 実推論と fail-open の内訳を 1 行で出す。
        # sample=skipped[:10] だけでは 67 件全滅の内訳が読めなかった。
        reason_counts = Counter(str(s.get("reason") or "unknown") for s in skipped)
        ai_counts = _ai_source_counts(ok_items)
        logger.warning(
            "[SUMMARY AI FUNNEL] stage=strict_prefilter in=%s kept=%s skipped=%s "
            "by_reason=%s ai_source=%s version=%s",
            len(ok_items), len(kept), len(skipped),
            dict(reason_counts.most_common()),
            dict(ai_counts),
            VERSION,
        )
        _emit_prefilter_cycle_funnel(ok_items, kept, skipped, reason_counts, ai_counts)
        logger.warning("[SUMMARY AI EXECUTOR] strict prefilter skipped=%s kept=%s diag=%s sample=%s version=%s", len(skipped), len(kept), diag, skipped[:10], VERSION)
    return kept


# prefilter の却下理由 -> candidate_trace の terminal_reason
_PREFILTER_TERMINAL_MAP = {
    "data_not_ready_intrabar_range": "RANGE_NOT_READY",
    "data_not_ready_single_bar_no_range": "RANGE_NOT_READY",
    "data_not_ready_high_low_missing": "RANGE_NOT_READY",
    "low_move_range_too_small": "LOW_MOVE",
    "price_below_min": "PRICE_OUT_OF_RANGE",
    "price_over_max": "PRICE_OUT_OF_RANGE",
    "low_volume": "LOW_LIQUIDITY",
    "low_turnover": "LOW_LIQUIDITY",
    "trade_restricted": "TRADE_RESTRICTED",
    "sell_reject_cached": "SELL_CREDIT_NG",
}


def _emit_prefilter_cycle_funnel(ok_items, kept, skipped, reason_counts, ai_counts) -> None:
    """サイクル単位 FUNNEL を 1 行で出す (件数のみ。判定はしない)。"""
    try:
        from .candidate_trace import CycleFunnel

        f = CycleFunnel(source="strict_prefilter")
        f.set("candidates", len(ok_items or []))
        f.set("prefilter_kept", len(kept or []))
        f.set("real_ai_ok", int(ai_counts.get("REAL_AI_OK", 0)))
        f.set("fallback_ok", int(ai_counts.get("FALLBACK_OK", 0)))
        not_ready = sum(n for r, n in reason_counts.items() if str(r).startswith("data_not_ready"))
        f.set("data_not_ready", int(not_ready))
        f.set("data_ready", max(0, len(ok_items or []) - int(not_ready)))
        for reason, n in reason_counts.items():
            f.add_terminal(_PREFILTER_TERMINAL_MAP.get(str(reason), str(reason).upper()), n)
        f.emit()
    except Exception:
        logger.debug("[SUMMARY AI FUNNEL CYCLE] emit failed", exc_info=True)


# ============================================================
# BREAKOUT SUPPORT/RESISTANCE GUARD (SUMMARY_AI 側)
# (旧 core/startup/breakout_support_resistance_guard_patch.py を本文化)
# ------------------------------------------------------------
# 支持線/抵抗線に近すぎる or ブレイクが未確定な候補を落とし、
# 並び替えのキーにブレイクのボーナスを足す。
# ネイティブは _base_filter_blocked_ai_ok_items_core へ改名した。
#
# 旧パッチは _base_ / _filter_ / _ORIGINAL_ の 3 つへ同じラッパーを
# 代入していた。本文化後は _base_ 自体がこのラッパーなので、
# 下の _ensure_core_filter() が _filter_ を _base_ へ戻しても
# ガードは維持される。
# ============================================================

_BSR_VERSION = "V1-BREAKOUT-SUPPORT-RESISTANCE-GUARD"
_BSR__INSTALLED = False
_BSR__ORIG_BASE_FILTER = None
_BSR__ORIG_SORT_KEY = None
_BSR__ORIG_BUILD_ORDER = None


def _bsr_env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _bsr_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _bsr_safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _bsr_norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return "BUY"


def _bsr_as_dict(v: Any) -> dict[str, Any]:
    return dict(v) if isinstance(v, dict) else {}


def _bsr_iter_sources(item: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield item
    for k in ("entry_row", "source_row", "ai_row", "row", "conditions", "entry_conditions"):
        d = item.get(k)
        if isinstance(d, dict):
            yield d


def _bsr_first(item: dict[str, Any], names: tuple[str, ...]) -> float:
    for d in _bsr_iter_sources(item):
        for n in names:
            x = _bsr_safe_float(d.get(n), 0.0)
            if x > 0:
                return x
    return 0.0


def _bsr_pick_symbol(item: dict[str, Any]) -> str:
    for d in _bsr_iter_sources(item):
        v = d.get("symbol") or d.get("Symbol") or d.get("code") or d.get("銘柄コード")
        if v:
            s = str(v).strip()
            if s.endswith(".0") and s[:-2].isdigit():
                s = s[:-2]
            return s
    return ""


def _bsr_pick_side(item: dict[str, Any]) -> str:
    for d in _bsr_iter_sources(item):
        v = d.get("side") or d.get("entry_decision") or d.get("ai_side")
        if v:
            return _bsr_norm_side(v)
    return "BUY"


def _bsr_pick_price(item: dict[str, Any]) -> float:
    return _bsr_first(item, ("price", "close_price", "current_price", "close", "last_price", "display_price", "CurrentPrice"))


def _bsr_pick_volume(item: dict[str, Any]) -> float:
    return _bsr_first(item, ("volume", "vol", "latest_volume", "display_volume", "Volume", "TradingVolume"))


def _bsr_pick_recent_avg_volume(item: dict[str, Any]) -> float:
    return _bsr_first(item, ("avg_volume_5", "volume_ma5", "avg_5m_volume", "recent_avg_volume", "volume_avg", "rolling_volume_avg"))


def _bsr_pick_level(item: dict[str, Any], names: tuple[str, ...]) -> float:
    return _bsr_first(item, names)


def _bsr_level_names() -> dict[str, tuple[str, ...]]:
    return {
        "prev_high": ("prev_high", "previous_high", "prev_day_high", "previous_day_high", "yesterday_high", "prior_high", "daily_prev_high", "前日高値"),
        "prev_low": ("prev_low", "previous_low", "prev_day_low", "previous_day_low", "yesterday_low", "prior_low", "daily_prev_low", "前日安値"),
        "resistance": ("resistance", "resistance_price", "upper_resistance", "recent_high", "recent_swing_high", "breakout_high", "rolling_high", "high_20", "high_10", "day_high", "today_high", "session_high", "intraday_high"),
        "support": ("support", "support_price", "lower_support", "recent_low", "recent_swing_low", "breakdown_low", "rolling_low", "low_20", "low_10", "day_low", "today_low", "session_low", "intraday_low"),
    }


def _bsr_score(item: dict[str, Any]) -> float:
    side = _bsr_pick_side(item)
    keys = ("sell_score", "score_total", "final_score", "score") if side == "SELL" else ("buy_score", "score_total", "final_score", "score")
    return max(_bsr_safe_float(item.get(k), 0.0) for k in keys)


def _bsr_diagnose(item: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    side = _bsr_pick_side(item)
    symbol = _bsr_pick_symbol(item)
    price = _bsr_pick_price(item)
    if price <= 0:
        return True, {"reason": "no_price", "symbol": symbol, "side": side}
    names = _bsr_level_names()
    prev_high = _bsr_pick_level(item, names["prev_high"])
    prev_low = _bsr_pick_level(item, names["prev_low"])
    resistance = max(prev_high, _bsr_pick_level(item, names["resistance"]))
    support_candidates = [x for x in (prev_low, _bsr_pick_level(item, names["support"])) if x > 0]
    support = min(support_candidates) if support_candidates else 0.0
    near_pct = _bsr_env_float("BREAKOUT_SR_NEAR_BLOCK_PCT", 0.0015)
    break_pct = _bsr_env_float("BREAKOUT_SR_BREAK_CONFIRM_PCT", 0.0005)
    strong_break_pct = _bsr_env_float("BREAKOUT_SR_STRONG_BREAK_PCT", 0.0020)
    require_volume = _bsr_env_bool("BREAKOUT_SR_REQUIRE_VOLUME_FOR_BREAK", False)
    vol = _bsr_pick_volume(item)
    avg_vol = _bsr_pick_recent_avg_volume(item)
    volume_ok = True
    if require_volume and avg_vol > 0 and vol > 0:
        volume_ok = vol >= avg_vol * _bsr_env_float("BREAKOUT_SR_VOLUME_RATIO", 1.15)
    detail = {
        "symbol": symbol,
        "side": side,
        "price": price,
        "prev_high": prev_high,
        "prev_low": prev_low,
        "resistance": resistance,
        "support": support,
        "near_pct": near_pct,
        "break_pct": break_pct,
        "strong_break_pct": strong_break_pct,
        "volume": vol,
        "avg_volume": avg_vol,
        "volume_ok": volume_ok,
        "score": _bsr_score(item),
    }
    if side == "BUY":
        if resistance > 0:
            gap = (resistance - price) / price
            break_margin = (price - resistance) / resistance if resistance > 0 else 0.0
            detail.update({"gap_to_resistance_pct": gap, "break_margin_pct": break_margin})
            if 0 <= gap <= near_pct:
                detail["reason"] = "near_resistance_block"
                return False, detail
            if break_margin >= break_pct and volume_ok:
                detail["reason"] = "resistance_breakout_ok"
                return True, detail
            if break_margin >= strong_break_pct:
                detail["reason"] = "strong_resistance_breakout_ok"
                return True, detail
        detail["reason"] = "no_resistance_level_or_not_near"
        return True, detail
    # SELL
    if support > 0:
        gap = (price - support) / price
        break_margin = (support - price) / support if support > 0 else 0.0
        detail.update({"gap_to_support_pct": gap, "break_margin_pct": break_margin})
        if 0 <= gap <= near_pct:
            detail["reason"] = "near_support_block"
            return False, detail
        if break_margin >= break_pct and volume_ok:
            detail["reason"] = "support_breakdown_ok"
            return True, detail
        if break_margin >= strong_break_pct:
            detail["reason"] = "strong_support_breakdown_ok"
            return True, detail
    detail["reason"] = "no_support_level_or_not_near"
    return True, detail


def _bsr_breakout_bonus(item: dict[str, Any]) -> float:
    if not isinstance(item, dict):
        return 0.0
    ok, detail = _bsr_diagnose(item)
    reason = str(detail.get("reason") or "")
    if not ok:
        return -1.0 * _bsr_env_float("BREAKOUT_SR_BLOCK_SORT_PENALTY", 1000.0)
    if "strong_" in reason:
        return _bsr_env_float("BREAKOUT_SR_STRONG_BONUS", 0.30)
    if "breakout_ok" in reason or "breakdown_ok" in reason:
        return _bsr_env_float("BREAKOUT_SR_BREAK_BONUS", 0.15)
    return 0.0


def _sort_key_bsr(item):
    """並び替えキーにブレイクのボーナスを足す (旧 breakout SR guard)。"""
    base_key = _sort_key_core(item)
    try:
        if isinstance(base_key, tuple):
            return tuple(float(x) if isinstance(x, (int, float)) else x for x in base_key) + (_bsr_breakout_bonus(item),)
    except Exception:
        pass
    return base_key


def _base_filter_blocked_ai_ok_items(ok_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows = _base_filter_blocked_ai_ok_items_core(ok_items)
    if not _bsr_env_bool("BREAKOUT_SR_GUARD_ENABLED", True):
        return rows
    kept = []
    for item in rows or []:
        try:
            ok, detail = _bsr_diagnose(item if isinstance(item, dict) else {})
        except Exception:
            ok, detail = True, {"reason": "diagnose_error"}
        if ok:
            if isinstance(item, dict):
                item["breakout_sr_detail"] = detail
                item["breakout_sr_reason"] = detail.get("reason")
            kept.append(item)
        else:
            logger.warning("[BREAKOUT SR GUARD] blocked detail=%s", detail)
    return kept


_ORIGINAL_FILTER_BLOCKED_AI_OK_ITEMS = _base_filter_blocked_ai_ok_items
_filter_blocked_ai_ok_items = _base_filter_blocked_ai_ok_items


def _ensure_core_filter(reason: str = "") -> bool:
    global _filter_blocked_ai_ok_items
    if _filter_blocked_ai_ok_items is not _ORIGINAL_FILTER_BLOCKED_AI_OK_ITEMS:
        _filter_blocked_ai_ok_items = _ORIGINAL_FILTER_BLOCKED_AI_OK_ITEMS
        logger.warning("[SUMMARY AI EXECUTOR] core filter restored reason=%s version=%s", reason, VERSION)
    return True


def _start_filter_watcher() -> None:
    global _FILTER_WATCHER_STARTED
    if _FILTER_WATCHER_STARTED or not _env_bool("SUMMARY_AI_PROTECT_CORE_FILTER", True):
        return
    _FILTER_WATCHER_STARTED = True

    def _loop() -> None:
        for i in range(180):
            try:
                _ensure_core_filter(reason=f"watcher:{i}")
            except Exception:
                pass
            time.sleep(0.5)

    try:
        threading.Thread(target=_loop, name="summary-ai-core-filter-watch", daemon=True).start()
        logger.warning("[SUMMARY AI EXECUTOR] core filter watcher started version=%s", VERSION)
    except Exception:
        logger.debug("[SUMMARY AI EXECUTOR] core filter watcher start failed", exc_info=True)


def _effective_max_entries(max_entries: int) -> int:
    try:
        requested = int(max_entries or DEFAULT_MAX_ENTRIES)
    except Exception:
        requested = DEFAULT_MAX_ENTRIES
    hard_cap = _env_int("SUMMARY_AI_MAX_REAL_ENTRIES", DEFAULT_MAX_ENTRIES)
    return max(1, min(requested, hard_cap, 3))


def _selected_pool(ok_items: List[Dict[str, Any]], *, max_entries: int) -> List[Dict[str, Any]]:
    kept = _base_filter_blocked_ai_ok_items(ok_items)
    if not kept:
        logger.warning("[SUMMARY AI EXECUTOR] no AI_OK rows after strict prefilter ok_total=%s version=%s", len(ok_items or []), VERSION)
        return []
    hard_cap = _effective_max_entries(max_entries)
    pool_n = max(hard_cap, _env_int("SUMMARY_AI_ROLLING_RETRY_POOL", _env_int("SUMMARY_AI_EXECUTOR_SELECTION_POOL", 20)))
    pool_n = min(max(1, pool_n), len(kept))
    return sorted(kept, key=_sort_key, reverse=True)[:pool_n]


def _select_ai_ok_items(ok_items: List[Dict[str, Any]], *, max_entries: int) -> List[Dict[str, Any]]:
    _ensure_core_filter(reason="select")
    pool = _selected_pool(ok_items, max_entries=max_entries)
    selected = pool[:_effective_max_entries(max_entries)]
    logger.warning("[SUMMARY AI EXECUTOR] top selection requested=%s cap=%s pool=%s ok_total=%s selected=%s version=%s", max_entries, _effective_max_entries(max_entries), len(pool), len(ok_items or []), [{"symbol": _pick_symbol(x), "side": _pick_side(x), "price": _pick_price(x), "conf": round(safe_float(x.get("confidence")), 3), "score": round(_score_for_side(x), 3), "range_pct": round(_pick_range_pct(x), 6)} for x in selected], VERSION)
    return selected


def _iter_dicts_deep(obj: Any, *, depth: int = 0) -> Iterable[Dict[str, Any]]:
    if depth > 5:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts_deep(v, depth=depth + 1)
    elif isinstance(obj, (list, tuple, set)):
        for v in obj:
            yield from _iter_dicts_deep(v, depth=depth + 1)


def _has_real_order_evidence(result: Any) -> bool:
    if result is None:
        return False
    if isinstance(result, bool):
        return result
    if isinstance(result, dict):
        for d in _iter_dicts_deep(result):
            for k in ("order_id", "OrderId", "order_no", "OrderNo", "execution_id", "ExecutionID"):
                if d.get(k):
                    return True
            for k in ("orders", "order_ids", "sent_orders", "submitted_orders", "accepted_orders", "executed_symbols"):
                v = d.get(k)
                if isinstance(v, (list, tuple, set, dict)) and len(v) > 0:
                    return True
            for k in ("order_sent", "order_submitted", "entry_executed"):
                if bool(d.get(k)):
                    return True
        return bool(result.get("executed"))
    if isinstance(result, (list, tuple, set)):
        return len(result) > 0
    return bool(result)


# ============================================================
# _positive_result の strict 化
# ------------------------------------------------------------
# 旧 core/startup/summary_ai_async_direct_dispatch_patch.py
# (_strict_result_executed) を本文化。
#
# 「approved だけでは True にせず、実際に発注/約定した証跡があるときだけ True」
# にする。_has_real_order_evidence との主な違い:
#   - result["executed"] が False なら他のキーを見ずに False
#   - executed_count / order_count 等の件数キーも証跡として見る
#   - result / pipeline_result / direct_dispatch_result へ再帰する
#   - list/tuple/set は len>0 ではなく要素ごとの再帰判定
#
# 適用スコープは旧 install 導線 (main.py の runtime patches) を再現する。
# 無効化は従来どおり SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC=0。
# ============================================================

_STRICT_POSITIVE_SCOPE: Optional[bool] = None


def _strict_positive_enabled() -> bool:
    global _STRICT_POSITIVE_SCOPE
    if _STRICT_POSITIVE_SCOPE is not None:
        return _STRICT_POSITIVE_SCOPE
    enabled = False
    try:
        if _env_bool("SUMMARY_AI_DIRECT_DISPATCH_ON_QUEUED_ASYNC", True):
            argv_text = " ".join(str(x) for x in sys.argv).replace("\\", "/").lower()
            db_names = (
                "main_database.py", "data_collectors_runner.py", "db_prepare_runner.py",
                "push_receiver_runner.py", "yahoo_complement_runner.py",
                "summary_database_runner.py", "ranking_collector_runner.py",
            )
            if not any(n in argv_text for n in db_names):
                enabled = "main.py" in argv_text
    except Exception:
        enabled = False
    _STRICT_POSITIVE_SCOPE = enabled
    return enabled


def _strict_result_executed(result: Any) -> bool:
    """発注/約定の証跡があるときだけ True。approved だけでは True にしない。"""
    try:
        if result is None:
            return False
        if isinstance(result, bool):
            return bool(result)
        if isinstance(result, dict):
            if result.get("executed") is False:
                return False
            for key in ("executed", "order_sent", "order_submitted", "success", "entry_executed"):
                if bool(result.get(key)):
                    return True
            for key in ("executed_count", "order_count", "submitted_count", "sent_count", "entries"):
                if _strict_safe_int(result.get(key), 0) > 0:
                    return True
            for key in ("order_id", "OrderId", "orders", "order_ids", "sent_orders", "executed_symbols"):
                v = result.get(key)
                if isinstance(v, (list, tuple, set, dict)) and len(v) > 0:
                    return True
                if v and not isinstance(v, (list, tuple, set, dict)):
                    return True
            for key in ("result", "pipeline_result", "direct_dispatch_result"):
                child = result.get(key)
                if child is not result and _strict_result_executed(child):
                    return True
            return False
        if isinstance(result, (list, tuple, set)):
            return any(_strict_result_executed(x) for x in result)
        return False
    except Exception:
        return False


def _strict_safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).replace(",", "")))
    except Exception:
        return int(default)


def _positive_result(result: Any) -> bool:
    if _strict_positive_enabled():
        return _strict_result_executed(result)
    return _has_real_order_evidence(result)


def _collect_symbols_for_pending_cleanup(result: Any, approved_rows: Sequence[Dict[str, Any]]) -> Set[str]:
    symbols: Set[str] = set()
    try:
        for d in _iter_dicts_deep(result):
            for key in ("symbol", "Symbol", "銘柄コード", "code", "stock_code"):
                sym = _norm_symbol(d.get(key))
                if sym:
                    symbols.add(sym)
            pending_root = d.get("pending_root")
            if isinstance(pending_root, dict):
                for sym in pending_root.keys():
                    ss = _norm_symbol(sym)
                    if ss:
                        symbols.add(ss)
    except Exception:
        logger.debug("[SUMMARY AI EXECUTOR] collect cleanup symbols from result failed", exc_info=True)
    # REV10: always include all approved symbols. Before REV10 this ran only
    # when result had no symbol, so rolling retry pruned only the final attempted
    # symbol and left prior STRICT_BOARD_MISSING pending rows behind.
    for row in approved_rows or []:
        if isinstance(row, dict):
            sym = _norm_symbol(row.get("symbol") or row.get("Symbol"))
            if sym:
                symbols.add(sym)
    return symbols


def _cleanup_pending_after_no_order(result: Any, approved_rows: Sequence[Dict[str, Any]], *, reason: str) -> int:
    if not _env_bool("SUMMARY_AI_CLEAN_PENDING_ON_NO_ORDER", True):
        return 0
    symbols = _collect_symbols_for_pending_cleanup(result, approved_rows)
    if not symbols:
        return 0
    try:
        from trading.entry.pending_manager import prune_entries, snapshot_root
        def _predicate(sym: str, entry: Dict[str, Any]) -> bool:
            if _norm_symbol(sym) not in symbols:
                return False
            source = str(entry.get("source") or "").strip().upper()
            entry_type = str(entry.get("entry_type") or "").strip().upper()
            return entry_type == "SUMMARY_AI" or source in {"SUMMARY", "SUMMARY_AI", "PUSH", "PUSH_SUMMARY"}
        removed = int(prune_entries(_predicate, reason=f"SUMMARY_AI_NO_ORDER:{reason}"))
        logger.warning("[SUMMARY AI EXECUTOR] pending cleanup after no-order reason=%s symbols=%s removed=%s root=%s result=%s version=%s", reason, sorted(symbols), removed, snapshot_root(), _summarize_no_order_result(result), VERSION)
        return removed
    except Exception:
        logger.exception("[SUMMARY AI EXECUTOR] pending cleanup after no-order failed reason=%s symbols=%s", reason, sorted(symbols))
        return 0


def _summarize_no_order_result(result: Any) -> Any:
    try:
        if not isinstance(result, dict):
            return result
        summary: Dict[str, Any] = {}
        for k in ("executed", "entries", "attempted_count", "approved", "registered", "skip_reason", "reason", "error", "pending_root", "skipped"):
            if k in result:
                summary[k] = result.get(k)
        nested = result.get("result") or result.get("pipeline_result")
        if isinstance(nested, dict):
            summary["nested"] = {k: nested.get(k) for k in ("executed", "entries", "attempted_count", "approved", "registered", "skip_reason", "reason", "error", "pending_root", "skipped") if k in nested}
        return summary or result
    except Exception:
        return str(result)


def _retryable_no_tradable(result: Any) -> bool:
    try:
        if not isinstance(result, dict):
            return False
        texts: list[str] = []
        for d in _iter_dicts_deep(result):
            for k in ("skip_reason", "reason", "error"):
                v = d.get(k)
                if v:
                    texts.append(str(v).lower())
            skipped = d.get("skipped")
            if isinstance(skipped, dict) and sum(int(v or 0) for v in skipped.values() if isinstance(v, (int, float))) > 0:
                texts.append("filtered")
        s = "|".join(texts)
        return any(x in s for x in ("no_tradable_rows_after_filters", "filtered", "entry_pipeline_no_order"))
    except Exception:
        return False


def build_approved_row_core(ai_ok_item: Dict[str, Any]) -> Dict[str, Any]:
    ai_row = _as_dict(ai_ok_item.get("ai_row"))
    src = _as_dict(ai_ok_item.get("source_row"))
    side = _pick_side(ai_ok_item)
    price = ai_row.get("close_price") or ai_row.get("price") or src.get("close") or src.get("price") or ai_ok_item.get("price")
    buy_score = ai_row.get("buy_score", ai_ok_item.get("buy_score"))
    sell_score = ai_row.get("sell_score", ai_ok_item.get("sell_score"))
    score_total = ai_row.get("score_total", ai_ok_item.get("score_total"))
    final_score = ai_row.get("final_score", ai_ok_item.get("final_score"))
    row = dict(src)
    row.update({
        "symbol": ai_ok_item.get("symbol") or ai_row.get("symbol") or src.get("symbol"),
        "symbolname": ai_ok_item.get("symbolname") or ai_row.get("symbolname") or src.get("symbolname"),
        "side": side,
        "ai_side": side,
        "entry_decision": side,
        "source": ai_row.get("source", src.get("source", "SUMMARY")),
        "interval": ai_row.get("interval", src.get("interval", 1)),
        "price": price,
        "close_price": price,
        "close": price,
        "confidence": ai_ok_item.get("confidence", 0.0),
        "ai_confidence": ai_ok_item.get("confidence", 0.0),
        "lot_multiplier": ai_ok_item.get("lot_multiplier", 1.0),
        "ai_reason": ai_ok_item.get("reason", ""),
        "reason": ai_ok_item.get("reason", ""),
        "model_used": ai_ok_item.get("model_used", ""),
        "score_total": score_total,
        "total_score": score_total,
        "score": score_total,
        "buy_score": buy_score,
        "sell_score": sell_score,
        "score_buy": buy_score,
        "score_sell": sell_score,
        "final_score": final_score,
        "display_score": final_score,
        "turnover": ai_row.get("turnover", src.get("turnover") or src.get("trading_value")),
        "volume": ai_row.get("volume", src.get("volume")),
        "datetime": ai_row.get("datetime", src.get("datetime")),
        "entry_type": ai_row.get("entry_type") or src.get("entry_type") or "SUMMARY_AI",
        "ai_gate_allow": True,
    })
    logger.info("[SUMMARY AI EXECUTOR] approved row built symbol=%s side=%s conf=%.3f total=%.3f close=%s range_pct=%.6f version=%s", row.get("symbol"), side, safe_float(row.get("ai_confidence")), safe_float(row.get("score_total")), row.get("close_price"), _pick_range_pct(row), VERSION)
    return row


def build_ai_ok_approved_rows(ai_results: Sequence[Dict[str, Any]], *, max_entries: int = DEFAULT_MAX_ENTRIES) -> List[Dict[str, Any]]:
    _ensure_core_filter(reason="approved_build")
    ok_items = [x for x in ai_results if isinstance(x, dict) and bool(x.get("allow"))]
    approved = [build_approved_row(x) for x in _select_ai_ok_items(ok_items, max_entries=max_entries)]
    logger.info("[SUMMARY AI EXECUTOR] approved selection max_entries=%s rows=%s version=%s", max_entries, len(approved), VERSION)
    return approved


# ============================================================
# blow-off top prefilter (ported from
# core/startup/summary_ai_blowoff_prefilter_patch.py V9)
# ============================================================

def _blowoff_latest_rows_per_symbol(df: pd.DataFrame) -> pd.DataFrame:
    try:
        if df is None or df.empty or "symbol" not in df.columns:
            return df
        x = df.copy()
        x["__sym_norm__"] = x["symbol"].astype(str).str.replace(r"\.0$", "", regex=True)
        if "datetime" in x.columns:
            x["__dt_sort__"] = pd.to_datetime(x["datetime"], errors="coerce")
            x = x.sort_values(["__sym_norm__", "__dt_sort__"])
        else:
            x = x.reset_index().rename(columns={"index": "__dt_sort__"}).sort_values(["__sym_norm__", "__dt_sort__"])
        latest = x.groupby("__sym_norm__", as_index=False, sort=False).tail(1)
        return latest.drop(columns=[c for c in ("__sym_norm__", "__dt_sort__") if c in latest.columns], errors="ignore")
    except Exception:
        logger.debug("[SUMMARY AI PREFILTER] latest row extraction failed; using original df", exc_info=True)
        return df


def _blowoff_latest_row_map(df_summary: Any) -> Dict[str, Dict[str, Any]]:
    try:
        if df_summary is None or not isinstance(df_summary, pd.DataFrame) or df_summary.empty or "symbol" not in df_summary.columns:
            return {}
        latest = _blowoff_latest_rows_per_symbol(df_summary)
        if latest is None or not isinstance(latest, pd.DataFrame) or latest.empty:
            return {}
        out: Dict[str, Dict[str, Any]] = {}
        for _, row in latest.iterrows():
            d = row.to_dict() if hasattr(row, "to_dict") else dict(row)
            sym = _norm_symbol(d.get("symbol"))
            if sym:
                out[sym] = d
        return out
    except Exception:
        logger.debug("[SUMMARY AI PREFILTER] latest row map failed", exc_info=True)
        return {}


def _blowoff_extract_symbols_from_tops(tops: Any) -> Set[str]:
    try:
        if tops is None or not isinstance(tops, pd.DataFrame) or tops.empty or "symbol" not in tops.columns:
            return set()
        return {_norm_symbol(x) for x in tops["symbol"].dropna().astype(str).tolist() if _norm_symbol(x)}
    except Exception:
        return set()


def _blowoff_range_pct_value(row: Dict[str, Any], *, close: float, high: float, low: float) -> float:
    raw = 0.0
    for key in ("range_pct", "intrabar_range_pct", "_intrabar_range_pct"):
        raw = safe_float(row.get(key), 0.0)
        if raw > 0:
            break
    if raw > 0:
        # range_pct は percent で統一済み。ここで推測変換しない。
        return raw
    base = close if close > 0 else max(high, low, 1.0)
    if high > 0 and low > 0 and high > low and base > 0:
        return abs(high - low) / base * RANGE_PCT_SCALE
    return 0.0


def _blowoff_close_position(row: Dict[str, Any], *, close: float, high: float, low: float) -> float:
    if high > low and close > 0:
        return max(0.0, min(1.0, (close - low) / max(high - low, 1e-9)))
    return 0.5


def _is_dangerous_blowoff_row(row: Dict[str, Any], *, side: str) -> Tuple[bool, Dict[str, Any]]:
    close = safe_float(row.get("close") or row.get("close_price") or row.get("price") or row.get("current_price"), 0.0)
    high = safe_float(row.get("high") or row.get("high_price") or row.get("day_high"), 0.0)
    low = safe_float(row.get("low") or row.get("low_price") or row.get("day_low"), 0.0)
    rsi = safe_float(row.get("rsi"), 50.0)
    slope = safe_float(row.get("slope_atr_scaled") or row.get("slope") or row.get("score_slope"), 0.0)
    range_pct = _blowoff_range_pct_value(row, close=close, high=high, low=low)
    close_pos = _blowoff_close_position(row, close=close, high=high, low=low)
    min_rsi = _env_float("SUMMARY_AI_BLOWOFF_DANGER_RSI", 80.0)
    min_range = _as_percent_threshold(
        _env_float("SUMMARY_AI_BLOWOFF_DANGER_RANGE_PCT", 2.5),
        name="SUMMARY_AI_BLOWOFF_DANGER_RANGE_PCT",
    )
    min_close_pos = _env_float("SUMMARY_AI_BLOWOFF_DANGER_CLOSE_POS", 0.85)

    side = _norm_side(side, "BUY")
    if side == "BUY":
        ok = rsi >= min_rsi and range_pct >= min_range and slope > 0 and close_pos >= min_close_pos
    else:
        ok = rsi <= (100.0 - min_rsi) and range_pct >= min_range and slope < 0 and close_pos <= (1.0 - min_close_pos)
    return bool(ok), {
        "side": side, "rsi": round(rsi, 3), "range_pct": round(range_pct, 3), "slope": round(slope, 6),
        "close_pos": round(close_pos, 3), "close": close, "high": high, "low": low,
        "need_rsi": min_rsi, "need_range_pct": min_range, "need_close_pos": min_close_pos,
    }


def _detect_blowoff_symbols(df_summary: Any) -> Tuple[Set[str], int, int, Dict[str, Dict[str, Any]]]:
    try:
        if df_summary is None or not isinstance(df_summary, pd.DataFrame) or df_summary.empty:
            return set(), 0, 0, {}
        from trading.ai.blowoff_top_detector import detect_blowoff_top

        source_rows = len(df_summary)
        latest_df = _blowoff_latest_rows_per_symbol(df_summary)
        latest_rows = len(latest_df) if isinstance(latest_df, pd.DataFrame) else 0
        latest_map = _blowoff_latest_row_map(latest_df)

        top_symbols = _blowoff_extract_symbols_from_tops(detect_blowoff_top(df_summary))
        if not top_symbols:
            top_symbols = _blowoff_extract_symbols_from_tops(detect_blowoff_top(latest_df))
        return top_symbols, source_rows, latest_rows, latest_map
    except Exception as e:
        # re-raising via logger.exception here would recurse on formatting; keep fail-open with logger.error.
        logger.error("[SUMMARY AI PREFILTER] blowoff detect failed; fail-open err=%s", e)
        return set(), 0, 0, {}


def _blowoff_filter_ai_results(ai_results: Any, df_summary: Any) -> Tuple[List[Any], Dict[str, List[str]], Set[str], Dict[str, Any]]:
    items = list(ai_results or [])
    top_symbols, source_rows, latest_rows, latest_map = _detect_blowoff_symbols(df_summary) if _env_bool("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED", True) else (set(), 0, 0, {})
    block_sell = _env_bool("SUMMARY_AI_BLOWOFF_BLOCK_SELL", False)
    kept: List[Any] = []
    skipped: Dict[str, List[str]] = {"blowoff": [], "low_move": []}
    side_counts: Dict[str, int] = {}
    checked: List[Dict[str, Any]] = []

    for item in items:
        sym = _pick_symbol(item) if isinstance(item, dict) else ""
        side = _pick_side(item) if isinstance(item, dict) else "BUY"
        side_counts[side] = side_counts.get(side, 0) + 1
        if sym and sym in top_symbols and (side == "BUY" or block_sell):
            merged = dict(latest_map.get(sym, {}))
            merged.update(item if isinstance(item, dict) else {})
            danger, diag = _is_dangerous_blowoff_row(merged, side=side)
            diag["symbol"] = sym
            diag["detected"] = True
            checked.append(diag)
            if danger:
                skipped["blowoff"].append(sym)
                continue
        kept.append(item)

    logger.warning(
        "[SUMMARY AI PREFILTER] applied before Top3 before=%s after=%s blowoff=%s low_move=%s top_symbols_count=%s source_rows=%s latest_rows=%s block_sell=%s side_counts=%s checked=%s version=%s",
        len(items), len(kept), sorted(set(skipped["blowoff"])), sorted(set(skipped["low_move"])),
        len(top_symbols), source_rows, latest_rows, block_sell, side_counts, checked[:10], VERSION,
    )
    return kept, skipped, top_symbols, {"source_rows": source_rows, "latest_rows": latest_rows, "block_sell": block_sell, "side_counts": side_counts, "checked": checked[:20]}


def _attach_blowoff_diagnostics(result: Any, *, before_n: int, after_n: int, skipped: Dict[str, List[str]], top_symbols: Set[str], meta: Dict[str, Any]) -> Any:
    try:
        if isinstance(result, dict):
            pre = {
                "before": before_n, "after": after_n,
                "skipped": {k: sorted(set(v)) for k, v in skipped.items()},
                "top_symbols_count": len(top_symbols),
                "source_rows": meta.get("source_rows"), "latest_rows": meta.get("latest_rows"),
                "block_sell": meta.get("block_sell"), "side_counts": meta.get("side_counts"),
                "checked": meta.get("checked"), "version": VERSION,
            }
            result["summary_ai_prefilter"] = pre
            result["blowoff_prefilter"] = pre
            if after_n == 0 and before_n > 0:
                result["skip_reason"] = "summary_ai_prefilter_all_blocked"
    except Exception:
        pass
    return result


# ============================================================
# direct-dispatch rolling snapshot fallback (ported from
# core/startup/summary_ai_async_direct_dispatch_patch.py V11)
# ============================================================

_DD_RETRYABLE_NO_ORDER_MARKERS = (
    "queued_async", "snapshot_no_order", "entry_controller_no_order", "summary_entry_executor_no_order",
    "entry_pipeline_no_order", "pending_moved_without_order", "order_id_empty_retryable",
    "entry_controller_lock_timeout", "pipeline_busy", "already_running", "no_pending_registered",
    "pipeline_filter_mismatch", "no_tradable_rows_after_filters",
)


def _dd_summary_ai_price_floor() -> float:
    return max(0.0, _env_float("SUMMARY_AI_APPROVAL_MIN_PRICE_OVERRIDE", 2500.0))


def _dd_force_direct_sync_env() -> None:
    os.environ["SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC"] = "1"
    os.environ.setdefault("SUMMARY_AI_ASYNC_ENTRY", "1")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", "1")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_MAX_ATTEMPTS", "2")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_RETRY_SLEEP_SEC", "0.7")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_PIPELINE_SOURCE", "SUMMARY")
    os.environ.setdefault("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", "8.0")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_ROLLING", "1")
    os.environ.setdefault("SUMMARY_AI_DIRECT_DISPATCH_SCAN_LIMIT", "12")
    # one-by-one dispatch (batch size fixed to 1) so a slow board/snapshot call
    # for one symbol cannot delay or block sibling candidates in the same batch.
    os.environ["SUMMARY_AI_DIRECT_DISPATCH_BATCH_SIZE"] = "1"
    os.environ.setdefault("SUMMARY_AI_DIRECT_FRESH_MAX_ATTEMPTS", "1")
    os.environ.setdefault("SUMMARY_AI_OK_VALID_SEC", "12.0")
    os.environ.setdefault("SUMMARY_AI_DIRECT_SNAPSHOT_FRESH_TIMEOUT_SEC", "2.0")
    floor = _dd_summary_ai_price_floor()
    if floor > 0:
        os.environ["SUMMARY_AI_APPROVAL_MIN_PRICE_OVERRIDE"] = str(floor)
        os.environ["SUMMARY_AI_ENTRY_MIN_PRICE"] = str(floor)
        os.environ["ENTRY_MIN_PRICE"] = str(floor)
        os.environ["SUMMARY_AI_LIQ_MIN_PRICE"] = str(floor)


def _dd_resolve_pipeline_source(rows: List[Any]) -> str:
    try:
        counts: Dict[str, int] = {}
        for r in list(rows or [])[:50]:
            d = _as_dict(r)
            nested_ai = _as_dict(d.get("ai"))
            nested_entry = _as_dict(d.get("entry"))
            nested_row = _as_dict(d.get("entry_row"))
            vals = [
                d.get("pipeline_source"), d.get("source"),
                nested_entry.get("pipeline_source"), nested_entry.get("source"),
                nested_row.get("pipeline_source"), nested_row.get("source"),
                nested_ai.get("pipeline_source"), nested_ai.get("source"),
                d.get("entry_type"), nested_entry.get("entry_type"), nested_ai.get("entry_type"),
            ]
            for v in vals:
                s = str(v or "").strip().upper()
                if not s:
                    continue
                if s in {"SUMMARY_AI", "AI"}:
                    continue
                if s in {"PUSH", "SUMMARY"}:
                    s = "SUMMARY"
                if s in {"SUMMARY", "RANKING", "TONOSAMA"}:
                    counts[s] = counts.get(s, 0) + 1
                    break
        if counts:
            return max(counts.items(), key=lambda kv: kv[1])[0]
    except Exception:
        logger.debug("[SUMMARY AI DIRECT DISPATCH] pipeline_source resolve failed", exc_info=True)
    return os.getenv("SUMMARY_AI_DIRECT_DISPATCH_PIPELINE_SOURCE", "SUMMARY").strip().upper() or "SUMMARY"


def _dd_flatten_reasons(result: Any) -> str:
    reasons: List[str] = []
    seen: Set[int] = set()

    def walk(v: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        try:
            oid = id(v)
            if oid in seen:
                return
            seen.add(oid)
        except Exception:
            pass
        if isinstance(v, dict):
            for k in ("skip_reason", "reason", "status", "lock_wait_reason"):
                r = v.get(k)
                if r:
                    reasons.append(str(r))
            for k in ("result", "pipeline_result", "direct_dispatch_result"):
                child = v.get(k)
                if child is not None and child is not v:
                    walk(child, depth + 1)
            if isinstance(v.get("result"), list):
                walk(v.get("result"), depth + 1)
        elif isinstance(v, (list, tuple, set)):
            for x in list(v)[:30]:
                walk(x, depth + 1)

    walk(result)
    return "|".join(reasons)


def _dd_strict_result_executed(result: Any) -> bool:
    """True only when an order/entry was actually submitted/executed."""
    try:
        if result is None:
            return False
        if isinstance(result, bool):
            return bool(result)
        if isinstance(result, dict):
            if result.get("executed") is False:
                return False
            for key in ("executed", "order_sent", "order_submitted", "success", "entry_executed"):
                if bool(result.get(key)):
                    return True
            for key in ("executed_count", "order_count", "submitted_count", "sent_count", "entries"):
                if int(safe_float(result.get(key), 0)) > 0:
                    return True
            for key in ("order_id", "OrderId", "orders", "order_ids", "sent_orders", "executed_symbols"):
                v = result.get(key)
                if isinstance(v, (list, tuple, set, dict)) and len(v) > 0:
                    return True
                if v and not isinstance(v, (list, tuple, set, dict)):
                    return True
            for key in ("result", "pipeline_result", "direct_dispatch_result"):
                child = result.get(key)
                if child is not result and _dd_strict_result_executed(child):
                    return True
            return False
        if isinstance(result, (list, tuple, set)):
            return any(_dd_strict_result_executed(x) for x in result)
        return False
    except Exception:
        return False


def _dd_is_queued_async(result: Any) -> bool:
    try:
        if not isinstance(result, dict):
            return False
        if bool(result.get("submitted_async")) or bool(result.get("queued_async")):
            return True
        child = result.get("result")
        if isinstance(child, dict) and str(child.get("status") or "").lower() == "queued_async":
            return True
    except Exception:
        pass
    return False


def _dd_registered_count(result: Any) -> int:
    try:
        if isinstance(result, dict):
            direct = result.get("registered")
            if direct is not None:
                return int(safe_float(direct, 0))
            for key in ("result", "pipeline_result", "direct_dispatch_result"):
                n = _dd_registered_count(result.get(key))
                if n > 0:
                    return n
    except Exception:
        pass
    return 0


def _dd_is_retryable_no_order(result: Any) -> bool:
    try:
        if _dd_strict_result_executed(result):
            return False
        text = _dd_flatten_reasons(result).lower()
        if any(x in text for x in _DD_RETRYABLE_NO_ORDER_MARKERS):
            return True
        if _dd_registered_count(result) > 0:
            return True
    except Exception:
        pass
    return False


def _dd_is_timeout_result(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get("timeout"))


def _dd_call_with_timeout(label: str, rows: List[Any], timeout_sec: float, fn: Callable[[], Any]) -> Any:
    timeout_sec = float(timeout_sec or 0.0)
    if timeout_sec <= 0:
        return fn()
    box: Dict[str, Any] = {"done": False, "result": None, "error": None}

    def _target() -> None:
        try:
            box["result"] = fn()
        except Exception as e:
            box["error"] = e
        finally:
            box["done"] = True

    symbols = [str(_pick_symbol(r)) for r in list(rows or [])[:20] if isinstance(r, dict)]
    started = time.time()
    th = threading.Thread(target=_target, daemon=True, name=f"summary-ai-direct-{label}")
    th.start()
    th.join(timeout_sec)
    elapsed = time.time() - started
    if th.is_alive():
        logger.error(
            "[SUMMARY AI DIRECT DISPATCH] %s timeout timeout=%.3fs elapsed=%.3fs symbols=%s version=%s note=inner_thread_left_daemon_to_avoid_blocking",
            label, timeout_sec, elapsed, symbols, VERSION,
        )
        return {"executed": False, "timeout": True, "skip_reason": f"{label}_timeout", "elapsed_sec": elapsed, "symbols": symbols}
    if box.get("error") is not None:
        raise box["error"]
    return box.get("result")


def _dd_direct_snapshot_execute(approved_rows: List[Any], interval: Any) -> Any:
    if not _env_bool("SUMMARY_AI_DIRECT_DISPATCH_SNAPSHOT_FIRST", True):
        return None
    try:
        from trading.summary import summary_entry as se
        fn = getattr(se, "execute_entry_pipeline", None)
        if not callable(fn):
            return None
        pipeline_source = _dd_resolve_pipeline_source(approved_rows)
        logger.warning(
            "[SUMMARY AI DIRECT DISPATCH] direct snapshot pipeline_source resolved=%s timeout=%.3fs price_floor=%.0f version=%s",
            pipeline_source, _env_float("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", 8.0), _dd_summary_ai_price_floor(), VERSION,
        )
        return fn(approved_rows, pipeline_source=pipeline_source, interval=interval)
    except TypeError:
        try:
            from trading.summary import summary_entry as se
            fn = getattr(se, "execute_entry_pipeline", None)
            if callable(fn):
                return fn(approved_rows)
        except Exception:
            logger.exception("[SUMMARY AI DIRECT DISPATCH] direct snapshot fallback failed")
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] direct snapshot failed")
    return None


def _dd_batch_size() -> int:
    return max(1, min(_env_int("SUMMARY_AI_DIRECT_DISPATCH_BATCH_SIZE", 3), 3))


def _dd_rows_from_result(result: Any) -> List[Any]:
    try:
        if isinstance(result, dict):
            rows = result.get("approved_rows")
            if isinstance(rows, list):
                return list(rows)
            rows = result.get("entries")
            if isinstance(rows, list):
                return list(rows)
            for key in ("result", "pipeline_result"):
                child = result.get(key)
                rows = _dd_rows_from_result(child)
                if rows:
                    return rows
    except Exception:
        pass
    return []


def _dd_build_rolling_rows_from_ai_results(ai_results: Any, existing_rows: List[Any]) -> List[Any]:
    """Build additional approved rows from AI_OK candidates. Final guards are not bypassed."""
    if not _env_bool("SUMMARY_AI_DIRECT_DISPATCH_ROLLING", True):
        return []
    try:
        existing_symbols = {str(_pick_symbol(r)) for r in list(existing_rows or []) if isinstance(r, dict)}
        ok_items = [x for x in list(ai_results or []) if isinstance(x, dict) and bool(x.get("allow"))]
        try:
            kept = _filter_blocked_ai_ok_items(ok_items)
        except Exception:
            kept = ok_items
        try:
            ordered = sorted(kept, key=_sort_key, reverse=True)
        except Exception:
            ordered = kept
        scan_limit = max(_dd_batch_size(), _env_int("SUMMARY_AI_DIRECT_DISPATCH_SCAN_LIMIT", 12))
        rows: List[Any] = []
        for item in ordered[:scan_limit]:
            sym = str(item.get("symbol") or "").strip()
            if not sym or sym in existing_symbols:
                continue
            try:
                row = build_approved_row(item)
            except Exception:
                logger.debug("[SUMMARY AI DIRECT DISPATCH] build approved row failed symbol=%s", sym, exc_info=True)
                continue
            if row:
                rows.append(row)
                existing_symbols.add(sym)
        if rows:
            logger.warning("[SUMMARY AI DIRECT DISPATCH] rolling extra approved rows built existing=%s extra=%s version=%s", len(existing_rows or []), len(rows), VERSION)
        return rows
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] rolling row build failed")
        return []


# ---- fresh one-by-one timeout-continue state (ported from
# core/startup/summary_ai_direct_timeout_continue_patch.py V15) ----
# symbol -> {approved_at_ts, valid_until_ts, max_age_sec}
_DD_FRESHNESS_BY_SYMBOL: Dict[str, Dict[str, float]] = {}

_DD_NO_RETRY_SAME_ROW_MARKERS = (
    "entry_pipeline_no_order", "no_tradable_rows_after_filters", "blowoff",
    "liquidity", "sell_credit", "position", "low_move", "range_atr",
)


def _dd_should_skip_original_rows(result: Any) -> bool:
    try:
        reason = str(_dd_flatten_reasons(result) or "").lower()
        return any(x in reason for x in _DD_NO_RETRY_SAME_ROW_MARKERS)
    except Exception:
        return False


def _dd_dedupe_rows(rows: List[Any]) -> List[Any]:
    out: List[Any] = []
    seen: Set[str] = set()
    try:
        for r in list(rows or []):
            try:
                sym = str(_pick_symbol(r) or "").strip()
            except Exception:
                sym = ""
            key = sym or str(id(r))
            if key in seen:
                continue
            seen.add(key)
            out.append(r)
    except Exception:
        return list(rows or [])
    return out


def _dd_row_to_mutable_dict(row: Any) -> Optional[Dict[str, Any]]:
    try:
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        return None
    return None


def _dd_mark_rows_fresh(rows: List[Any], *, approved_at_ts: float, max_age_sec: float) -> List[Any]:
    out: List[Any] = []
    valid_until_ts = float(approved_at_ts) + max(0.1, float(max_age_sec or 0.0))
    try:
        now = time.time()
        for sym, meta in list(_DD_FRESHNESS_BY_SYMBOL.items()):
            try:
                if float(meta.get("valid_until_ts") or 0.0) < now - 30.0:
                    _DD_FRESHNESS_BY_SYMBOL.pop(sym, None)
            except Exception:
                _DD_FRESHNESS_BY_SYMBOL.pop(sym, None)

        for row in list(rows or []):
            try:
                sym = str(_pick_symbol(row) or "").strip()
            except Exception:
                sym = ""
            if sym:
                _DD_FRESHNESS_BY_SYMBOL[sym] = {
                    "approved_at_ts": float(approved_at_ts),
                    "valid_until_ts": float(valid_until_ts),
                    "max_age_sec": float(max_age_sec),
                }
            d = _dd_row_to_mutable_dict(row)
            if d is not None:
                d.setdefault("summary_ai_approved_at_ts", float(approved_at_ts))
                d.setdefault("summary_ai_valid_until_ts", float(valid_until_ts))
                d.setdefault("summary_ai_max_age_sec", float(max_age_sec))
            out.append(row)
    except Exception:
        logger.debug("[SUMMARY AI DIRECT FRESHNESS] mark rows failed", exc_info=True)
        return list(rows or [])
    return out


def _dd_is_row_fresh(row: Any, *, now_ts: Optional[float] = None) -> bool:
    try:
        now = float(now_ts if now_ts is not None else time.time())
        d = _dd_row_to_mutable_dict(row) or {}
        valid_until = d.get("summary_ai_valid_until_ts")
        if valid_until is None:
            try:
                sym = str(_pick_symbol(row) or "").strip()
            except Exception:
                sym = ""
            meta = _DD_FRESHNESS_BY_SYMBOL.get(sym) if sym else None
            if meta:
                valid_until = meta.get("valid_until_ts")
        if valid_until is None:
            return True
        return now <= float(valid_until)
    except Exception:
        return True


def _dd_filter_fresh_rows(rows: List[Any]) -> Tuple[List[Any], List[str]]:
    fresh: List[Any] = []
    stale_symbols: List[str] = []
    now = time.time()
    for row in list(rows or []):
        if _dd_is_row_fresh(row, now_ts=now):
            fresh.append(row)
        else:
            try:
                sym = str(_pick_symbol(row) or "").strip()
            except Exception:
                sym = ""
            stale_symbols.append(sym or "?")
    return fresh, stale_symbols


def _dd_fallback_direct_dispatch(result: Any, kwargs: Dict[str, Any]) -> Any:
    """Rolling, one-by-one, freshness-aware retry across AI_OK candidates.

    A per-candidate timeout only drops that candidate (does not abort the
    whole batch), and a candidate is skipped once it's older than
    SUMMARY_AI_OK_VALID_SEC so a slow retry chain never dispatches a stale
    AI_OK decision late.
    """
    try:
        if _dd_strict_result_executed(result):
            return result
        if not (_dd_is_queued_async(result) or _dd_is_retryable_no_order(result)):
            return result
        approved_rows = _dd_rows_from_result(result)
        if not approved_rows:
            return result

        ai_results = kwargs.get("ai_results")
        extra_rows = _dd_build_rolling_rows_from_ai_results(ai_results, approved_rows)
        skip_original = _dd_should_skip_original_rows(result)
        candidate_rows = list(extra_rows) if skip_original else (list(approved_rows) + list(extra_rows))
        candidate_rows = _dd_dedupe_rows(candidate_rows)
        if not candidate_rows:
            return result

        interval = kwargs.get("interval", 1)
        approved_at_ts = time.time()
        max_age_sec = max(1.0, _env_float("SUMMARY_AI_OK_VALID_SEC", 12.0))
        candidate_rows = _dd_mark_rows_fresh(candidate_rows, approved_at_ts=approved_at_ts, max_age_sec=max_age_sec)
        candidate_rows, stale_symbols = _dd_filter_fresh_rows(candidate_rows)
        if not candidate_rows:
            if isinstance(result, dict):
                out = dict(result)
                out["executed"] = False
                out["skip_reason"] = "summary_ai_ok_expired_before_dispatch"
                out["summary_ai_expired_symbols"] = stale_symbols
                out["summary_ai_ok_valid_sec"] = max_age_sec
                return out
            return result

        attempts = max(1, _env_int("SUMMARY_AI_DIRECT_FRESH_MAX_ATTEMPTS", 1))
        retry_sleep = max(0.1, _env_float("SUMMARY_AI_DIRECT_DISPATCH_RETRY_SLEEP_SEC", 0.3))
        configured_timeout = _env_float("SUMMARY_AI_DIRECT_SNAPSHOT_TIMEOUT_SEC", 8.0)
        fresh_timeout = _env_float("SUMMARY_AI_DIRECT_SNAPSHOT_FRESH_TIMEOUT_SEC", 2.0)
        timeout_sec = max(0.3, min(float(configured_timeout), float(fresh_timeout), float(max_age_sec)))

        batches = [[r] for r in candidate_rows]
        last_result: Any = None
        timeout_seen = False
        freshness_expired_seen = False
        attempt_records: List[Dict[str, Any]] = []

        for batch_idx, batch in enumerate(batches, start=1):
            fresh_batch, stale_batch_symbols = _dd_filter_fresh_rows(batch)
            if stale_batch_symbols:
                freshness_expired_seen = True
                attempt_records.append({
                    "batch": batch_idx, "attempt": 0, "symbols": stale_batch_symbols,
                    "executed": False, "timeout": False, "retryable": False,
                    "reason_chain": "summary_ai_ok_expired_before_snapshot",
                })
            if not fresh_batch:
                continue

            for attempt in range(1, attempts + 1):
                if time.time() > approved_at_ts + max_age_sec:
                    freshness_expired_seen = True
                    break

                started = time.time()
                snap_result = _dd_call_with_timeout("direct_snapshot", fresh_batch, timeout_sec, lambda b=fresh_batch: _dd_direct_snapshot_execute(b, interval))
                last_result = snap_result
                executed = _dd_strict_result_executed(snap_result)
                timeout = _dd_is_timeout_result(snap_result)
                retryable = _dd_is_retryable_no_order(snap_result)
                timeout_seen = bool(timeout_seen or timeout)
                attempt_records.append({
                    "batch": batch_idx, "attempt": attempt, "executed": executed, "timeout": timeout,
                    "retryable": retryable, "reason_chain": _dd_flatten_reasons(snap_result),
                })
                logger.warning(
                    "[SUMMARY AI DIRECT DISPATCH] fresh one-by-one snapshot done batch=%s/%s attempt=%s/%s elapsed=%.3fs executed=%s timeout=%s retryable=%s reason_chain=%s version=%s",
                    batch_idx, len(batches), attempt, attempts, time.time() - started, executed, timeout, retryable, _dd_flatten_reasons(snap_result), VERSION,
                )
                if executed:
                    break
                if timeout:
                    # timeoutした銘柄だけ捨てる。fresh window内なら次のAI_OK候補へ進む。
                    break
                if not retryable:
                    break
                if attempt < attempts:
                    time.sleep(retry_sleep)

            if _dd_strict_result_executed(last_result):
                break
            # timeout / no-order / hard-filter NG でも次の1銘柄候補へ進む。

        if isinstance(result, dict):
            out = dict(result)
            out["direct_dispatch_sync_fallback"] = True
            out["direct_dispatch_rolling"] = True
            out["direct_dispatch_timeout_continue"] = True
            out["direct_dispatch_one_by_one"] = True
            out["direct_dispatch_fresh_guard"] = True
            out["direct_dispatch_skip_original_failed_rows"] = bool(skip_original)
            out["direct_dispatch_timeout_seen"] = bool(timeout_seen)
            out["direct_dispatch_freshness_expired_seen"] = bool(freshness_expired_seen)
            out["direct_dispatch_attempts"] = attempt_records
            out["direct_dispatch_result"] = last_result
            out["summary_ai_ok_valid_sec"] = max_age_sec
            out["summary_ai_direct_snapshot_timeout_sec"] = timeout_sec
            if _dd_strict_result_executed(last_result):
                out["executed"] = True
                out["skip_reason"] = None
            elif freshness_expired_seen:
                out["executed"] = False
                out["skip_reason"] = "summary_ai_ok_expired_before_dispatch"
            elif timeout_seen:
                out["executed"] = False
                out["skip_reason"] = "direct_snapshot_timeout_after_candidates"
                out["direct_dispatch_timeout"] = True
            else:
                out["executed"] = False
                out["skip_reason"] = _dd_flatten_reasons(last_result) or "entry_pipeline_no_order"
            return out
    except Exception:
        logger.exception("[SUMMARY AI DIRECT DISPATCH] sync fallback failed")
    return result


def _direct_dispatch_layer(ai_results: Any, *, df_summary: Any, interval: Any, max_entries: int, dry_run: bool, require_market_open: bool, entry_pipeline: Optional[Callable[..., Any]]) -> Dict[str, Any]:
    _dd_force_direct_sync_env()
    result = _async_entry_layer(ai_results, df_summary=df_summary, interval=interval, max_entries=max_entries, dry_run=dry_run, require_market_open=require_market_open, entry_pipeline=entry_pipeline)
    if dry_run:
        return result
    return _dd_fallback_direct_dispatch(result, {"ai_results": ai_results, "interval": interval})


# ============================================================
# async-entry sync/queue dispatch (ported from
# core/startup/summary_ai_async_entry_patch.py V12)
# ============================================================

_ASYNC_LOCK = threading.Lock()
_ASYNC_QUEUE: "deque[Dict[str, Any]]" = deque()
_ASYNC_WORKER_RUNNING = False
_ASYNC_SEQ = 0


def _async_market_open_now() -> bool:
    try:
        return bool(is_market_open())
    except Exception:
        return True


def _async_execute_original(item: Dict[str, Any]) -> Any:
    return _rev10_execute_ai_ok_entries_bulk(
        item["ai_results"], df_summary=item["df_summary"], interval=item["interval"], max_entries=item["max_entries"],
        dry_run=False, require_market_open=item["require_market_open"], entry_pipeline=item["entry_pipeline"],
    )


def _async_unwrap_result(result: Any) -> Any:
    cur = result
    try:
        for _ in range(12):
            if isinstance(cur, dict) and isinstance(cur.get("result"), dict):
                cur = cur.get("result")
                continue
            if isinstance(cur, dict) and isinstance(cur.get("pipeline_result"), dict):
                cur = cur.get("pipeline_result")
                continue
            break
    except Exception:
        return result
    return cur


def _async_skip_reason(result: Any) -> str:
    reasons: List[str] = []
    try:
        cur = result
        for _ in range(16):
            if not isinstance(cur, dict):
                break
            for k in ("skip_reason", "lock_wait_reason", "reason", "status"):
                r = cur.get(k)
                if r:
                    reasons.append(str(r))
            nxt = cur.get("result") or cur.get("pipeline_result")
            if not isinstance(nxt, dict):
                break
            cur = nxt
    except Exception:
        pass
    return "|".join(reasons)


def _async_is_retryable_controller_busy(result: Any) -> bool:
    try:
        text = _async_skip_reason(result).lower()
        unwrapped = _async_unwrap_result(result)
        retryable = bool(unwrapped.get("retryable")) if isinstance(unwrapped, dict) else False
        retry_markers = (
            "entry_controller_lock_timeout", "lock_timeout", "pipeline_busy", "already_running",
            "queued_async", "pending_moved_without_order", "order_id_empty_retryable",
        )
        if retryable or any(x in text for x in retry_markers):
            return True
        hard_no_retry = ("snapshot_no_ai_approved_candidates", "no_tradable_rows_after_filters", "market_closed", "risk_guard_ng", "ai_health_ng", "index_shock")
        if any(x in text for x in hard_no_retry):
            return False
    except Exception:
        return False
    return False


def _async_summarize_result(result: Any) -> Dict[str, Any]:
    try:
        if isinstance(result, dict):
            return {
                "executed": bool(result.get("executed")), "submitted_async": bool(result.get("submitted_async")),
                "skip_reason": result.get("skip_reason"), "reason_chain": _async_skip_reason(result),
            }
        return {"result_type": type(result).__name__}
    except Exception as e:
        return {"summary_error": str(e)}


def _async_run_worker_loop() -> None:
    global _ASYNC_WORKER_RUNNING
    while True:
        with _ASYNC_LOCK:
            if not _ASYNC_QUEUE:
                _ASYNC_WORKER_RUNNING = False
                logger.info("[SUMMARY AI ASYNC ENTRY] queue worker idle")
                return
            item = _ASYNC_QUEUE.popleft()
        seq = item["seq"]
        started = time.time()
        queued_at = float(item.get("queued_at") or started)
        stale_sec = _env_float("SUMMARY_AI_ASYNC_ENTRY_STALE_SEC", 20.0)
        try:
            if stale_sec > 0 and time.time() - queued_at > stale_sec:
                logger.warning("[SUMMARY AI ASYNC ENTRY] worker skip stale seq=%s", seq)
                continue
            if bool(item.get("require_market_open")) and not _async_market_open_now():
                logger.warning("[SUMMARY AI ASYNC ENTRY] worker skip market_closed seq=%s", seq)
                continue
            retry_enabled = _env_bool("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY", True)
            retry_max = max(1, _env_int("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_MAX", 3))
            retry_sleep = max(0.2, _env_float("SUMMARY_AI_ASYNC_ENTRY_LOCK_RETRY_SLEEP_SEC", 1.0))
            result: Any = None
            for attempt in range(1, retry_max + 1):
                attempt_started = time.time()
                result = _async_execute_original(item)
                summary = _async_summarize_result(result)
                logger.warning("[SUMMARY AI ASYNC ENTRY] worker attempt done seq=%s attempt=%s/%s elapsed=%.3fs summary=%s", seq, attempt, retry_max, time.time() - attempt_started, summary)
                if bool(summary.get("executed")):
                    break
                if not retry_enabled or not _async_is_retryable_controller_busy(result):
                    break
                if stale_sec > 0 and time.time() - queued_at + retry_sleep > stale_sec:
                    break
                if attempt < retry_max:
                    time.sleep(retry_sleep)
            logger.warning("[SUMMARY AI ASYNC ENTRY] worker done seq=%s elapsed=%.3fs final_summary=%s", seq, time.time() - started, _async_summarize_result(result))
        except Exception as e:
            logger.exception("[SUMMARY AI ASYNC ENTRY] worker failed seq=%s err=%s", seq, e)


def _async_ensure_worker_started() -> None:
    global _ASYNC_WORKER_RUNNING
    with _ASYNC_LOCK:
        if _ASYNC_WORKER_RUNNING:
            return
        _ASYNC_WORKER_RUNNING = True
    threading.Thread(target=_async_run_worker_loop, name="SummaryAiAsyncEntry", daemon=True).start()


def _async_entry_layer(ai_results: Any, *, df_summary: Any, interval: Any, max_entries: int, dry_run: bool, require_market_open: bool, entry_pipeline: Optional[Callable[..., Any]]) -> Dict[str, Any]:
    global _ASYNC_SEQ
    if dry_run or not _env_bool("SUMMARY_AI_ASYNC_ENTRY", True) or _env_bool("SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC", False):
        return _rev10_execute_ai_ok_entries_bulk(ai_results, df_summary=df_summary, interval=interval, max_entries=max_entries, dry_run=dry_run, require_market_open=require_market_open, entry_pipeline=entry_pipeline)

    approved_rows = build_ai_ok_approved_rows(ai_results, max_entries=max_entries)
    if not approved_rows:
        return {"executed": False, "submitted_async": False, "dry_run": False, "approved_rows": [], "result": None, "skip_reason": "no_ai_ok"}
    if require_market_open and not _async_market_open_now():
        return {"executed": False, "submitted_async": False, "dry_run": False, "approved_rows": approved_rows, "result": None, "skip_reason": "market_closed"}

    queue_max = max(1, min(_env_int("SUMMARY_AI_ASYNC_ENTRY_QUEUE_MAX", 1), 3))
    with _ASYNC_LOCK:
        dropped = 0
        while len(_ASYNC_QUEUE) >= queue_max:
            _ASYNC_QUEUE.popleft()
            dropped += 1
        _ASYNC_SEQ += 1
        seq = _ASYNC_SEQ
        _ASYNC_QUEUE.append({
            "seq": seq, "queued_at": time.time(), "ai_results": ai_results, "df_summary": df_summary, "interval": interval,
            "max_entries": max_entries, "require_market_open": require_market_open, "entry_pipeline": entry_pipeline,
        })
        q_size = len(_ASYNC_QUEUE)
    _async_ensure_worker_started()
    logger.warning("[SUMMARY AI ASYNC ENTRY] queued seq=%s interval=%s approved=%s queue_size=%s dropped=%s", seq, interval, len(approved_rows), q_size, dropped)
    return {"executed": False, "submitted_async": True, "queued_async": True, "async_seq": seq, "queue_size": q_size, "dry_run": False, "approved_rows": approved_rows, "result": {"status": "queued_async", "seq": seq, "queue_size": q_size}, "skip_reason": "queued_async"}


def _lowmove_batch_size(default: int = 3) -> int:
    return max(1, min(_env_int("SUMMARY_AI_EXECUTOR_BATCH_SIZE", default), 3))


def _approved_batches(ai_results: Sequence[Dict[str, Any]], *, max_entries: int) -> Iterable[List[Dict[str, Any]]]:
    _ensure_core_filter(reason="approved_batches")
    ok_items = [x for x in ai_results if isinstance(x, dict) and bool(x.get("allow"))]
    pool = _selected_pool(ok_items, max_entries=max_entries)
    cap = _effective_max_entries(max_entries)
    max_rounds = max(1, _env_int("SUMMARY_AI_ROLLING_RETRY_ROUNDS", 3))
    used: Set[str] = set()
    round_no = 0
    while pool and round_no < max_rounds:
        batch_items: List[Dict[str, Any]] = []
        rest: List[Dict[str, Any]] = []
        for item in pool:
            sym = _pick_symbol(item)
            key = f"{sym}:{_pick_side(item)}"
            if key in used:
                continue
            if len(batch_items) < cap:
                batch_items.append(item)
                used.add(key)
            else:
                rest.append(item)
        pool = rest
        if not batch_items:
            break
        round_no += 1
        logger.warning("[SUMMARY AI EXECUTOR] rolling batch round=%s/%s cap=%s symbols=%s version=%s", round_no, max_rounds, cap, [_pick_symbol(x) for x in batch_items], VERSION)
        yield [build_approved_row(x) for x in batch_items]


def _rev10_execute_ai_ok_entries_bulk(ai_results: Sequence[Dict[str, Any]], *, df_summary: pd.DataFrame, interval: int | str = 1, max_entries: int = DEFAULT_MAX_ENTRIES, dry_run: bool = True, require_market_open: bool = True, entry_pipeline: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    # 実売買では fail-open 由来 (FALLBACK_NO_AI / FIXED_FAILOPEN) を発注対象にしない。
    ai_results = drop_failopen_for_live(ai_results, dry_run=dry_run, interval=interval)
    _ensure_core_filter(reason="execute_start")
    if require_market_open and not is_market_open():
        approved_rows = build_ai_ok_approved_rows(ai_results, max_entries=max_entries)
        return {"executed": False, "dry_run": dry_run, "approved_rows": approved_rows, "result": None, "skip_reason": "market_closed"}
    if entry_pipeline is None:
        entry_pipeline = get_bulk_entry_pipeline()
    if entry_pipeline is None:
        approved_rows = build_ai_ok_approved_rows(ai_results, max_entries=max_entries)
        return {"executed": False, "dry_run": dry_run, "approved_rows": approved_rows, "result": None, "skip_reason": "entry_pipeline_not_found"}

    all_approved: List[Dict[str, Any]] = []
    last_result: Any = None
    last_removed = 0
    last_skip = "no_ai_ok"
    try:
        batches = list(_approved_batches(ai_results, max_entries=max_entries)) if _env_bool("SUMMARY_AI_ROLLING_RETRY_ON_FILTERED_NO_ORDER", True) else [build_ai_ok_approved_rows(ai_results, max_entries=max_entries)]
        if not batches or not batches[0]:
            return {"executed": False, "dry_run": dry_run, "approved_rows": [], "result": None, "skip_reason": "no_ai_ok_after_strict_prefilter"}
        for idx, approved_rows in enumerate(batches, start=1):
            all_approved.extend(approved_rows)
            if dry_run:
                return {"executed": False, "dry_run": True, "approved_rows": approved_rows, "result": None, "skip_reason": "dry_run"}
            logger.info("[SUMMARY AI EXECUTOR] REAL bulk entry start approved=%s interval=%s symbols=%s round=%s/%s version=%s", len(approved_rows), interval, [str(x.get("symbol")) for x in approved_rows], idx, len(batches), VERSION)
            result = entry_pipeline(approved_rows, df_summary, interval)
            executed = _positive_result(result)
            last_result = result
            if executed:
                logger.info("[SUMMARY AI EXECUTOR] REAL bulk entry done approved=%s executed=True round=%s result=%s", len(approved_rows), idx, result)
                return {"executed": True, "dry_run": False, "approved_rows": approved_rows, "all_approved_rows": all_approved, "result": result, "skip_reason": None, "pending_removed": 0, "rolling_retry_round": idx}
            last_skip = "entry_pipeline_no_order"
            if _retryable_no_tradable(result) and idx < len(batches):
                _cleanup_pending_after_no_order(result, approved_rows, reason="entry_pipeline_no_order_retry")
                logger.warning("[SUMMARY AI EXECUTOR] no tradable rows -> retry next AI_OK batch round=%s/%s symbols=%s detail=%s version=%s", idx, len(batches), [str(x.get("symbol")) for x in approved_rows], _summarize_no_order_result(result), VERSION)
                continue
            last_removed = _cleanup_pending_after_no_order(result, approved_rows, reason=last_skip)
            logger.warning("[SUMMARY AI EXECUTOR] NO REAL ORDER DETAIL approved=%s interval=%s symbols=%s skip=%s pending_removed=%s detail=%s", len(approved_rows), interval, [str(x.get("symbol")) for x in approved_rows], last_skip, last_removed, _summarize_no_order_result(result))
            break
        # REV10: also clean all rows selected across the rolling run, because wrapper patches may return only the final attempted result.
        if all_approved:
            last_removed = max(last_removed, _cleanup_pending_after_no_order(last_result, all_approved, reason=f"{last_skip}_all_approved"))
        logger.info("[SUMMARY AI EXECUTOR] REAL bulk entry done approved=%s executed=False pending_removed=%s result=%s", len(all_approved), last_removed, last_result)
        return {"executed": False, "dry_run": False, "approved_rows": all_approved, "result": last_result, "skip_reason": last_skip, "pending_removed": last_removed, "rolling_retry_attempted": len(batches)}
    except Exception:
        logger.exception("[SUMMARY AI EXECUTOR] REAL bulk entry failed")
        _cleanup_pending_after_no_order(last_result, all_approved, reason="entry_exception")
        return {"executed": False, "dry_run": False, "approved_rows": all_approved, "result": last_result, "skip_reason": "entry_exception"}


def execute_ai_ok_entries_bulk(ai_results: Sequence[Dict[str, Any]], *, df_summary: pd.DataFrame, interval: int | str = 1, max_entries: int = DEFAULT_MAX_ENTRIES, dry_run: bool = True, require_market_open: bool = True, entry_pipeline: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """Entry point. Layering (outer -> inner), matching the production install
    order that used to be assembled at runtime via 4 monkeypatches:

      1. blow-off top prefilter (always applied first -- this was the bug fix,
         see REV11 changelog above: it used to be bypassed)
      2. low_move_softpass rolling-retry (batches AI_OK candidates, retries
         next batch on no-order)
      3. [fallback only, when rolling-retry disabled or raises] direct_dispatch
         (forces sync dispatch + its own rolling snapshot retry)
      4. [fallback only] async_entry (sync passthrough, or queue+worker thread
         when SUMMARY_AI_ASYNC_ENTRY_DIRECT_SYNC=0)
      5. [fallback only] the native REV10 single/rolling-batch executor
    """
    _ensure_core_filter(reason="execute_start")
    before_n = len(list(ai_results or []))
    ai_results, blowoff_skipped, blowoff_top_symbols, blowoff_meta = _blowoff_filter_ai_results(ai_results, df_summary)
    after_n = len(ai_results)

    if not _env_bool("SUMMARY_AI_EXECUTOR_ROLLING_RETRY", True):
        result = _direct_dispatch_layer(ai_results, df_summary=df_summary, interval=interval, max_entries=max_entries, dry_run=dry_run, require_market_open=require_market_open, entry_pipeline=entry_pipeline)
        return _attach_blowoff_diagnostics(result, before_n=before_n, after_n=after_n, skipped=blowoff_skipped, top_symbols=blowoff_top_symbols, meta=blowoff_meta)

    try:
        ok_items = [x for x in ai_results if isinstance(x, dict) and bool(x.get("allow"))]
        kept = _filter_blocked_ai_ok_items(ok_items)
        if not kept:
            result = {"executed": False, "dry_run": dry_run, "approved_rows": [], "result": None, "skip_reason": "no_ai_ok"}
            return _attach_blowoff_diagnostics(result, before_n=before_n, after_n=after_n, skipped=blowoff_skipped, top_symbols=blowoff_top_symbols, meta=blowoff_meta)
        if require_market_open and not is_market_open():
            approved_preview = [build_approved_row(x) for x in sorted(kept, key=_sort_key, reverse=True)[:_lowmove_batch_size(max_entries)]]
            result = {"executed": False, "dry_run": dry_run, "approved_rows": approved_preview, "result": None, "skip_reason": "market_closed"}
            return _attach_blowoff_diagnostics(result, before_n=before_n, after_n=after_n, skipped=blowoff_skipped, top_symbols=blowoff_top_symbols, meta=blowoff_meta)
        if dry_run:
            approved_preview = [build_approved_row(x) for x in sorted(kept, key=_sort_key, reverse=True)[:_lowmove_batch_size(max_entries)]]
            result = {"executed": False, "dry_run": True, "approved_rows": approved_preview, "result": None, "skip_reason": "dry_run"}
            return _attach_blowoff_diagnostics(result, before_n=before_n, after_n=after_n, skipped=blowoff_skipped, top_symbols=blowoff_top_symbols, meta=blowoff_meta)
        if entry_pipeline is None:
            entry_pipeline = get_bulk_entry_pipeline()
        if entry_pipeline is None:
            result = {"executed": False, "dry_run": False, "approved_rows": [], "result": None, "skip_reason": "entry_pipeline_not_found"}
            return _attach_blowoff_diagnostics(result, before_n=before_n, after_n=after_n, skipped=blowoff_skipped, top_symbols=blowoff_top_symbols, meta=blowoff_meta)

        batch_n = _lowmove_batch_size(max_entries)
        scan_limit = max(batch_n, _env_int("SUMMARY_AI_EXECUTOR_CANDIDATE_SCAN_LIMIT", 12))
        ordered = sorted(kept, key=_sort_key, reverse=True)[:scan_limit]
        all_rows: List[Dict[str, Any]] = []
        attempts: List[Dict[str, Any]] = []
        logger.warning("[SUMMARY AI EXECUTOR ROLLING] start ok_total=%s kept=%s scan=%s batch=%s interval=%s version=%s", len(ok_items), len(kept), len(ordered), batch_n, interval, VERSION)

        for start in range(0, len(ordered), batch_n):
            batch_items = ordered[start:start + batch_n]
            approved_rows = [build_approved_row(x) for x in batch_items]
            all_rows.extend(approved_rows)
            symbols = [str(x.get("symbol")) for x in approved_rows]
            logger.warning("[SUMMARY AI EXECUTOR ROLLING] batch start offset=%s size=%s symbols=%s", start, len(approved_rows), symbols)
            result = entry_pipeline(approved_rows, df_summary, interval)
            executed = _positive_result(result)
            attempts.append({"offset": start, "symbols": symbols, "executed": executed, "result": _summarize_no_order_result(result)})
            if executed:
                logger.warning("[SUMMARY AI EXECUTOR ROLLING] executed offset=%s symbols=%s result=%s", start, symbols, result)
                result_out = {"executed": True, "dry_run": False, "approved_rows": all_rows, "result": result, "skip_reason": None, "attempts": attempts, "rolling_retry": True}
                return _attach_blowoff_diagnostics(result_out, before_n=before_n, after_n=after_n, skipped=blowoff_skipped, top_symbols=blowoff_top_symbols, meta=blowoff_meta)
            logger.warning("[SUMMARY AI EXECUTOR ROLLING] batch no-order offset=%s symbols=%s detail=%s", start, symbols, _summarize_no_order_result(result))

        removed_pending = 0
        if all_rows:
            try:
                removed_pending = _cleanup_pending_after_no_order(attempts[-1].get("result") if attempts else None, all_rows, reason="entry_pipeline_no_order_all_batches")
            except Exception:
                logger.exception("[SUMMARY AI EXECUTOR ROLLING] final pending cleanup failed")
        result = {"executed": False, "dry_run": False, "approved_rows": all_rows, "result": attempts[-1].get("result") if attempts else None, "skip_reason": "entry_pipeline_no_order_all_batches", "attempts": attempts, "pending_removed": removed_pending, "rolling_retry": True}
        return _attach_blowoff_diagnostics(result, before_n=before_n, after_n=after_n, skipped=blowoff_skipped, top_symbols=blowoff_top_symbols, meta=blowoff_meta)
    except Exception:
        logger.exception("[SUMMARY AI EXECUTOR ROLLING] rolling retry failed; fallback to direct-dispatch chain version=%s", VERSION)
        result = _direct_dispatch_layer(ai_results, df_summary=df_summary, interval=interval, max_entries=max_entries, dry_run=dry_run, require_market_open=require_market_open, entry_pipeline=entry_pipeline)
        return _attach_blowoff_diagnostics(result, before_n=before_n, after_n=after_n, skipped=blowoff_skipped, top_symbols=blowoff_top_symbols, meta=blowoff_meta)


_start_filter_watcher()

__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MIN_BUY_APPROVED",
    "DEFAULT_MAX_PRICE_FOR_100_SHARE_ENTRY",
    "DEFAULT_MIN_PRICE_FOR_ENTRY",
    "build_approved_row",
    "build_ai_ok_approved_rows",
    "drop_failopen_for_live",
    "live_requires_real_ai",
    "execute_ai_ok_entries_bulk",
    "_select_ai_ok_items",
    "_filter_blocked_ai_ok_items",
    "_effective_max_entries",
    "_pick_symbol",
    "_pick_side",
    "_row_side",
    "_row_score_for_side",
    "_score_for_side",
    "_sort_key",
    "_price_bounds",
    "_entry_price_bounds",
    "_positive_result",
]


# ============================================================
# approved row のレンジ補修
# ------------------------------------------------------------
# 旧 core/startup/summary_ai_approved_row_range_repair_patch.py を本文化。
# AI承認行の high/low/range/atr を元 item 側の値とマージして整合させる。
#
# 【重要】同じ build_approved_row を summary_ai_strategy_mode_patch も包むが、
# 旧 range_repair は `getattr(cur, "_original", cur)` で素の実装を掴み直す
# 設計だったため、install順 (strategy_mode -> range_repair) により
# strategy_mode 側の build_approved_row ラッパは実運用では捨てられていた。
# 本文化に合わせて strategy_mode 側の当該ラッパも撤去してある
# (残すと今まで無効だった enrich が有効化されてしまう)。
# 合成順シミュレーションで確認済み。
#
# 適用スコープは旧 install 導線 (summary_ai_more_candidates_patch <- main.py) と
# env SUMMARY_AI_APPROVED_ROW_RANGE_REPAIR (既定True) を再現する。
# ============================================================

# _arr_env_bool が参照する真偽値セット。
# 本文化時に定義を移し忘れており、NameError が try/except に飲まれて
# env 指定が常に既定値へ落ちていたので補った。
_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_FALSE = {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}


def _arr_env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        s = str(raw).strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    except Exception:
        pass
    return bool(default)


def _arr_safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        if x != x or x in (float("inf"), float("-inf")):
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def _arr_row_dict(row: Any) -> dict[str, Any]:
    try:
        if isinstance(row, dict):
            return row
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _arr_get_first(d: dict[str, Any], names: tuple[str, ...], default: float = 0.0) -> float:
    for name in names:
        try:
            if name in d:
                x = _arr_safe_float(d.get(name), 0.0)
                if x > 0:
                    return x
        except Exception:
            continue
    return float(default)


def _arr_put(row: Any, key: str, value: float) -> None:
    try:
        if isinstance(row, dict):
            row[key] = value
            return
        if hasattr(row, "__setitem__"):
            row[key] = value
            return
        setattr(row, key, value)
    except Exception:
        pass


def _arr_merge_sources(approved_row: Any, item: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    try:
        merged.update(_arr_row_dict(item))
    except Exception:
        pass
    try:
        merged.update(_arr_row_dict(approved_row))
    except Exception:
        pass
    return merged


def _arr_repair_approved_row_range(approved_row: Any, item: Any = None) -> Any:
    try:
        if approved_row is None:
            return approved_row
        d = _arr_merge_sources(approved_row, item)
        symbol = str(d.get("symbol") or d.get("Symbol") or "").strip()

        close = _arr_get_first(d, (
            "close", "close_price", "current_price", "price", "last_price", "ai_disp_close",
            "entry_price", "limit_price",
        ))
        if close <= 0:
            return approved_row

        high = _arr_get_first(d, ("high", "high_price", "bar_high", "summary_high"))
        low = _arr_get_first(d, ("low", "low_price", "bar_low", "summary_low"))
        current_range_ok = high > 0 and low > 0 and high >= low and abs(high - low) > 1e-9
        if current_range_ok:
            return approved_row

        range_high = _arr_get_first(d, (
            "day_high", "today_high", "intraday_high", "session_high",
            "range_high", "ranking_high", "snapshot_high", "high_price_day",
            "ai_disp_day_high", "ai_disp_high",
        ))
        range_low = _arr_get_first(d, (
            "day_low", "today_low", "intraday_low", "session_low",
            "range_low", "ranking_low", "snapshot_low", "low_price_day",
            "ai_disp_day_low", "ai_disp_low",
        ))

        if not (range_high > 0 and range_low > 0 and range_high >= range_low):
            return approved_row
        if abs(range_high - range_low) <= 1e-9:
            return approved_row

        # 発注直前価格が日中レンジ外に少し出ている場合でも、high/lowが破綻しないよう包含する。
        repaired_high = max(float(range_high), float(close))
        repaired_low = min(float(range_low), float(close))
        if repaired_high <= 0 or repaired_low <= 0 or repaired_high < repaired_low:
            return approved_row

        _arr_put(approved_row, "high", repaired_high)
        _arr_put(approved_row, "low", repaired_low)
        _arr_put(approved_row, "range_high", repaired_high)
        _arr_put(approved_row, "range_low", repaired_low)
        _arr_put(approved_row, "day_high", repaired_high)
        _arr_put(approved_row, "day_low", repaired_low)
        try:
            # percent 単位 (range_pct の唯一の定義)
            range_pct = abs(repaired_high - repaired_low) / max(float(close), 1.0) * RANGE_PCT_SCALE
            _arr_put(approved_row, "range_pct", range_pct)
            _arr_put(approved_row, "intraday_range_pct", range_pct)
            _arr_put(approved_row, "day_range_pct", range_pct)
            _arr_put(approved_row, "summary_ai_approved_row_range_repaired", True)
            _arr_put(approved_row, "summary_ai_approved_row_range_repair_version", _ARR_VERSION)
        except Exception:
            pass

        logger.warning(
            "[SUMMARY AI APPROVED ROW RANGE REPAIR] repaired symbol=%s close=%s old_high=%s old_low=%s new_high=%s new_low=%s version=%s",
            symbol,
            close,
            high,
            low,
            repaired_high,
            repaired_low,
            _ARR_VERSION,
        )
        return approved_row
    except Exception:
        logger.exception("[SUMMARY AI APPROVED ROW RANGE REPAIR] repair failed; return original")
        return approved_row


_ARR_VERSION = "V1-SUMMARY-AI-APPROVED-ROW-RANGE-REPAIR"
_ARR_SCOPE: Optional[bool] = None


def _arr_enabled() -> bool:
    global _ARR_SCOPE
    if _ARR_SCOPE is not None:
        return _ARR_SCOPE
    enabled = False
    try:
        if _arr_env_bool("SUMMARY_AI_APPROVED_ROW_RANGE_REPAIR", True):
            argv_text = " ".join(str(x) for x in sys.argv).replace("\\", "/").lower()
            db_names = (
                "main_database.py", "data_collectors_runner.py", "db_prepare_runner.py",
                "push_receiver_runner.py", "yahoo_complement_runner.py",
                "summary_database_runner.py", "ranking_collector_runner.py",
            )
            if not any(n in argv_text for n in db_names):
                enabled = "main.py" in argv_text
    except Exception:
        enabled = False
    _ARR_SCOPE = enabled
    return enabled


def build_approved_row(ai_ok_item: Dict[str, Any]) -> Dict[str, Any]:
    row = build_approved_row_core(ai_ok_item)
    if not _arr_enabled():
        return row
    return _arr_repair_approved_row_range(row, ai_ok_item)


# ============================================================
# SUMMARY AI 承認の最低価格 (旧
# core/startup/summary_ai_entry_hook_dataframe_truth_patch.py の
# _install_summary_ai_approval_diag_and_price_floor を本文化)
# ------------------------------------------------------------
# 2026-08-11 のユーザー判断「エントリー最低価格は 200 円」がここに載っている。
# SUMMARY_AI_APPROVAL_MIN_PRICE_OVERRIDE (既定 200.0) を
#   - env SUMMARY_AI_ENTRY_MIN_PRICE / ENTRY_MIN_PRICE へハード代入
#   - DEFAULT_MIN_PRICE_FOR_ENTRY (ソース既定 3000.0) を上書き
# の 2 つへ反映する。2500 へ戻したい場合は
# SUMMARY_AI_APPROVAL_MIN_PRICE_OVERRIDE=2500 を指定する。
#
# 旧パッチはさらに _entry_price_bounds をラップしていたが、この属性は
# リポジトリ全体で読み手が無い (executor 内の呼び出しも __all__ の記載だけ)。
# 死んだラッパーなので移していない。実際に効いていたのは上の 2 つ。
#
# 適用スコープは旧 install 元 (sitecustomize BACKGROUND_MAIN_PATCHES) と
# 同じく _dft_scope_enabled() で判定する。DB プロセスでは従来どおり
# ソース既定の 3000.0 のまま。
# ============================================================


# ------------------------------------------------------------
# 旧 core/startup/summary_ai_entry_hook_dataframe_truth_patch.py の
# install スコープ (sitecustomize BACKGROUND_MAIN_PATCHES) の再現。
# scheduler_jobs/summary/summary_ai_entry_hook_v20._df_truth_scope_enabled と
# 同じ判定。SUMMARY_AI_DF_TRUTH_SCOPE_ENABLED=1/0 で明示上書きできる。
# ------------------------------------------------------------
_DFT_DB_PROCESS_NAMES = (
    "main_database.py",
    "data_collectors_runner.py",
    "db_prepare_runner.py",
    "push_receiver_runner.py",
    "yahoo_complement_runner.py",
    "summary_database_runner.py",
    "ranking_collector_runner.py",
)


def _dft_env_on(name: str, default: bool = False) -> bool:
    """sitecustomize._env_on と同じ解釈。"""
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return bool(default)


def _dft_scope_enabled() -> bool:
    try:
        raw = os.getenv("SUMMARY_AI_DF_TRUTH_SCOPE_ENABLED")
        if raw is not None and str(raw).strip() != "":
            return _dft_env_on("SUMMARY_AI_DF_TRUTH_SCOPE_ENABLED", True)
        # sitecustomize._install_module の disabled_env は "1" 完全一致で判定していた
        if str(os.environ.get("DISABLE_SUMMARY_AI_DF_TRUTH_PATCH", "")).strip() == "1":
            return False
        argv_parts = [str(x).replace("\\", "/").lower() for x in sys.argv]
        argv = " ".join(argv_parts)
        # sitecustomize は database context を main.py 判定より先に見る
        if any(name in argv for name in _DFT_DB_PROCESS_NAMES):
            return False
        is_main_py = any(a.endswith("/main.py") or a == "main.py" for a in argv_parts)
        if is_main_py and _dft_env_on("SITECUSTOMIZE_MAIN_LITE", True):
            return True
        return _dft_env_on("SITECUSTOMIZE_ENABLE_FULL_NONMAIN", False)
    except Exception:
        return False



def _dft_apply_entry_price_floor() -> None:
    global DEFAULT_MIN_PRICE_FOR_ENTRY
    if not _dft_scope_enabled():
        return
    try:
        raw = os.getenv("SUMMARY_AI_APPROVAL_MIN_PRICE_OVERRIDE")
        min_override = float(raw) if raw not in (None, "") else 200.0
    except Exception:
        min_override = 200.0
    min_override = max(0.0, min_override)
    os.environ["SUMMARY_AI_ENTRY_MIN_PRICE"] = str(min_override)
    os.environ["ENTRY_MIN_PRICE"] = str(min_override)
    try:
        DEFAULT_MIN_PRICE_FOR_ENTRY = float(min_override)
    except Exception:
        pass
    logger.warning(
        "[SUMMARY AI APPROVAL DIAG] installed price_min_override=%.0f executor_default_min=%s",
        min_override, DEFAULT_MIN_PRICE_FOR_ENTRY,
    )


_dft_apply_entry_price_floor()


# ============================================================
# SUMMARY-AI 戦略モード (BREAKOUT / PULLBACK / REVERSAL)
# (旧 core/startup/summary_ai_strategy_mode_patch.py を本文化)
# ------------------------------------------------------------
# 旧 install() は加算型の 2 枚被せで、巻き戻し (`_original` 辿り) は
# 行っていなかった:
#   _passes_strict_candidate_quality  <- 戦略モードで不許可なら落とす
#   _sort_key                         <- ソート鍵に mode_rank / score を足す
# build_approved_row のラップは、range_repair が install 時に素の実装を
# 掴み直すため実運用で必ず捨てられており、旧パッチの時点で撤去済み。
#
# 戦略判定 (_sm_classify / _sm_strategy_enabled) には旧
# summary_ai_fibonacci_ev_strategy_patch / summary_ai_nve_projection_strategy_patch
# の 2 段オーバーレイが既に取り込まれている。合成順は
#   native <- fibonacci_ev <- nve_projection
# のままで、有効/無効は各層が
#   SUMMARY_AI_FIB_EV_STRATEGY_ENABLED / SUMMARY_AI_NVE_STRATEGY_ENABLED
# を見る。
# ============================================================

_SM_VERSION = "V1-SUMMARY-AI-STRATEGY-MODE-BREAKOUT-PULLBACK-REVERSAL"


_SM_INSTALLED = False


_SM_ORIG_BUILD_APPROVED_ROW = None


_SM_ORIG_PASSES_QUALITY = None


_SM_ORIG_SORT_KEY = None


def _sm_env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _sm_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _sm_f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        if isinstance(v, str):
            s = v.strip().replace(",", "").replace("円", "").replace("株", "").replace("%", "")
            if s == "" or s.lower() in {"none", "nan", "null", "<na>", "pd.na", "-", "－", "—"}:
                return float(default)
            v = s
        x = float(v)
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _sm_s(v: Any) -> str:
    try:
        return str(v or "").strip().upper()
    except Exception:
        return ""


def _sm_side(row: dict[str, Any]) -> str:
    s = _sm_s(row.get("side") or row.get("ai_side") or row.get("entry_decision"))
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return "BUY"


def _sm_first(row: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip() != "":
            return v
    raw = row.get("source_row") or row.get("ai_row") or row.get("_raw") or row.get("raw")
    if isinstance(raw, dict):
        for k in keys:
            v = raw.get(k)
            if v is not None and str(v).strip() != "":
                return v
    return default


def _sm_num(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    return _sm_f(_sm_first(row, tuple(keys), default), default)


def _sm_boolish(row: dict[str, Any], *keys: str) -> bool:
    for k in keys:
        v = _sm_first(row, (k,), None)
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return float(v) != 0.0
        if isinstance(v, str):
            return v.strip().lower() in {"1", "true", "yes", "y", "on", "ok"}
    return False


def _sm_ma_state(row: dict[str, Any]) -> tuple[float, float, float]:
    ma5 = _sm_num(row, "ma5", "ma_5", "MA5", "display_ma5")
    ma25 = _sm_num(row, "ma25", "ma_25", "MA25", "display_ma25")
    ma75 = _sm_num(row, "ma75", "ma_75", "MA75", "display_ma75")
    return ma5, ma25, ma75


def _sm_trend_alignment(row: dict[str, Any], side: str) -> tuple[int, list[str]]:
    reasons: list[str] = []
    aligned = 0
    slopes = {
        "1m": _sm_num(row, "slope_1m", "slope", "score_slope", "slope_atr_scaled"),
        "3m": _sm_num(row, "slope_3m", "slope_3"),
        "5m": _sm_num(row, "slope_5m", "slope_5"),
    }
    eps = _sm_env_float("SUMMARY_AI_STRATEGY_SLOPE_EPS", 0.0)
    for tf, slope in slopes.items():
        if side == "BUY" and slope > eps:
            aligned += 1
            reasons.append(f"{tf}_slope_up")
        elif side == "SELL" and slope < -eps:
            aligned += 1
            reasons.append(f"{tf}_slope_down")
    return aligned, reasons


def _sm_classify_core(row: dict[str, Any]) -> dict[str, Any]:
    side = _sm_side(row)
    close = _sm_num(row, "close_price", "price", "current_price", "close", "last_price")
    high = _sm_num(row, "high", "high_price", "HighPrice", "day_high")
    low = _sm_num(row, "low", "low_price", "LowPrice", "day_low")
    vwap = _sm_num(row, "vwap", "VWAP", "display_vwap")
    rsi = _sm_num(row, "rsi", "RSI", default=50.0)
    macd = _sm_num(row, "macd", "MACD")
    signal = _sm_num(row, "signal", "macd_signal", "MACDSignal")
    hist = _sm_num(row, "hist", "macd_hist", "histogram")
    atr = _sm_num(row, "atr", "ATR")
    range_pct = _sm_num(row, "range_pct", "intrabar_range_pct", "_intrabar_range_pct")
    if range_pct <= 0 and close > 0 and high > low:
        range_pct = abs(high - low) / close * RANGE_PCT_SCALE
    ma5, ma25, ma75 = _sm_ma_state(row)
    above_ma5 = close > 0 and ma5 > 0 and close >= ma5
    below_ma5 = close > 0 and ma5 > 0 and close <= ma5
    above_vwap = close > 0 and vwap > 0 and close >= vwap
    below_vwap = close > 0 and vwap > 0 and close <= vwap
    high_break = _sm_boolish(row, "above_vwap_recent", "golden_cross_recent", "golden_cross_continuation")
    low_break = _sm_boolish(row, "below_vwap_recent", "dead_cross_recent", "dead_cross_continuation")
    bullish_stack = _sm_boolish(row, "ma_stack_bullish") or (ma5 > 0 and ma25 > 0 and ma75 > 0 and ma5 >= ma25 >= ma75)
    bearish_stack = _sm_boolish(row, "ma_stack_bearish") or (ma5 > 0 and ma25 > 0 and ma75 > 0 and ma5 <= ma25 <= ma75)
    aligned, slope_reasons = _sm_trend_alignment(row, side)

    reasons: list[str] = []
    breakout = 0.0
    pullback = 0.0
    reversal = 0.0

    if side == "BUY":
        if above_ma5:
            breakout += 1.0; reasons.append("price_above_ma5")
        if above_vwap:
            breakout += 1.0; reasons.append("price_above_vwap")
        if bullish_stack:
            breakout += 1.0; reasons.append("ma_stack_bullish")
        if hist > 0 or macd >= signal:
            breakout += 0.5; reasons.append("macd_not_bad")
        if high_break or (high > 0 and close >= high * 0.995):
            breakout += 0.7; reasons.append("near_high_break")
        breakout += min(1.5, aligned * 0.5)
        # Pullback is still trend-following: larger trend OK, short-term dip/reclaim near MA/VWAP.
        if bullish_stack or aligned >= 1:
            pullback += 1.0; reasons.append("larger_trend_buy")
        if ma5 > 0 and close > 0 and abs(close - ma5) / close <= _sm_env_float("SUMMARY_AI_PULLBACK_MA5_NEAR_PCT", 0.004):
            pullback += 1.0; reasons.append("near_ma5")
        if vwap > 0 and close > 0 and abs(close - vwap) / close <= _sm_env_float("SUMMARY_AI_PULLBACK_VWAP_NEAR_PCT", 0.006):
            pullback += 0.8; reasons.append("near_vwap")
        if rsi <= _sm_env_float("SUMMARY_AI_PULLBACK_BUY_RSI_MAX", 58.0):
            pullback += 0.4; reasons.append("rsi_not_overheated")
        if above_ma5 or above_vwap:
            pullback += 0.5; reasons.append("reclaim_short_average")
        if rsi <= _sm_env_float("SUMMARY_AI_REVERSAL_BUY_RSI", 35.0):
            reversal += 1.2; reasons.append("rsi_oversold")
        if below_vwap:
            reversal += 0.6; reasons.append("below_vwap_mean_revert")
        if below_ma5:
            reversal += 0.4; reasons.append("below_ma5_mean_revert")
    else:
        if below_ma5:
            breakout += 1.0; reasons.append("price_below_ma5")
        if below_vwap:
            breakout += 1.0; reasons.append("price_below_vwap")
        if bearish_stack:
            breakout += 1.0; reasons.append("ma_stack_bearish")
        if hist < 0 or macd <= signal:
            breakout += 0.5; reasons.append("macd_not_bad_sell")
        if low_break or (low > 0 and close <= low * 1.005):
            breakout += 0.7; reasons.append("near_low_break")
        breakout += min(1.5, aligned * 0.5)
        if bearish_stack or aligned >= 1:
            pullback += 1.0; reasons.append("larger_trend_sell")
        if ma5 > 0 and close > 0 and abs(close - ma5) / close <= _sm_env_float("SUMMARY_AI_PULLBACK_MA5_NEAR_PCT", 0.004):
            pullback += 1.0; reasons.append("near_ma5")
        if vwap > 0 and close > 0 and abs(close - vwap) / close <= _sm_env_float("SUMMARY_AI_PULLBACK_VWAP_NEAR_PCT", 0.006):
            pullback += 0.8; reasons.append("near_vwap")
        if rsi >= _sm_env_float("SUMMARY_AI_PULLBACK_SELL_RSI_MIN", 42.0):
            pullback += 0.4; reasons.append("rsi_not_oversold")
        if below_ma5 or below_vwap:
            pullback += 0.5; reasons.append("reclaim_short_average_sell")
        if rsi >= _sm_env_float("SUMMARY_AI_REVERSAL_SELL_RSI", 70.0):
            reversal += 1.2; reasons.append("rsi_overbought")
        if above_vwap:
            reversal += 0.6; reasons.append("above_vwap_mean_revert")
        if above_ma5:
            reversal += 0.4; reasons.append("above_ma5_mean_revert")

    # Penalize low-volatility pseudo signals, but do not replace existing low-vol guard.
    if range_pct > 0 and range_pct < _as_percent_threshold(
        _sm_env_float("SUMMARY_AI_STRATEGY_MIN_RANGE_PCT_SOFT", 0.4),
        name="SUMMARY_AI_STRATEGY_MIN_RANGE_PCT_SOFT",
    ):
        breakout -= 0.4
        pullback -= 0.2
        reasons.append("strategy_low_range_soft_penalty")
    if atr <= 0:
        reasons.append("atr_missing_strategy_only")
    reasons.extend(slope_reasons)

    scores = {"BREAKOUT": breakout, "PULLBACK": pullback, "REVERSAL": reversal}
    mode = max(scores, key=scores.get)
    mode_score = float(scores[mode])
    if mode == "REVERSAL" and not _sm_env_bool("SUMMARY_AI_STRATEGY_ENABLE_REVERSAL", False):
        # Default: do not actively prefer counter-trend reversal. Route it as WATCH_ONLY unless very strong.
        strong_min = _sm_env_float("SUMMARY_AI_STRATEGY_REVERSAL_STRONG_MIN", 3.2)
        if mode_score < strong_min:
            mode = "WATCH_ONLY_REVERSAL"
    return {
        "strategy_mode": mode,
        "strategy_score": round(mode_score, 4),
        "strategy_breakout_score": round(breakout, 4),
        "strategy_pullback_score": round(pullback, 4),
        "strategy_reversal_score": round(reversal, 4),
        "strategy_reasons": "/".join(dict.fromkeys(reasons))[:500],
        "strategy_aligned_mtf_count": aligned,
        "strategy_range_pct": round(range_pct, 6),
    }


def _sm_enrich(row: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return row
    try:
        tags = _sm_classify(row)
        row.update(tags)
        # Keep reason human-readable without losing original AI reason.
        base = str(row.get("reason") or row.get("ai_reason") or "")
        add = f"strategy={tags['strategy_mode']} score={tags['strategy_score']} reasons={tags['strategy_reasons']}"
        if add not in base:
            row["strategy_reason"] = add
            row["reason"] = (base + " | " + add).strip(" |")
    except Exception:
        logger.debug("[SUMMARY AI STRATEGY MODE] enrich failed row=%s", row, exc_info=True)
    return row


def _sm_strategy_enabled_core(row: dict[str, Any]) -> tuple[bool, str]:
    if not _sm_env_bool("SUMMARY_AI_STRATEGY_MODE_GUARD_ENABLED", True):
        return True, "disabled"
    mode = str(row.get("strategy_mode") or "").strip().upper()
    score = _sm_f(row.get("strategy_score"), 0.0)
    if mode == "BREAKOUT":
        return score >= _sm_env_float("SUMMARY_AI_STRATEGY_BREAKOUT_MIN", 2.0), "breakout_score_low"
    if mode == "PULLBACK":
        return score >= _sm_env_float("SUMMARY_AI_STRATEGY_PULLBACK_MIN", 2.0), "pullback_score_low"
    if mode == "REVERSAL":
        if not _sm_env_bool("SUMMARY_AI_STRATEGY_ENABLE_REVERSAL", False):
            return False, "reversal_disabled"
        return score >= _sm_env_float("SUMMARY_AI_STRATEGY_REVERSAL_MIN", 3.2), "reversal_score_low"
    if mode == "WATCH_ONLY_REVERSAL":
        return False, "watch_only_reversal"
    # Unknown mode should not fail-open into order entry.
    return False, "strategy_unknown"


_SM_FIB_INSTALLED = False


_SM_FIB_ORIG_CLASSIFY = None


_SM_FIB_ORIG_STRATEGY_ENABLED = None


def _sm_fib_env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _sm_fib_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _sm_fib_f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        if isinstance(v, str):
            s = v.strip().replace(",", "").replace("円", "").replace("株", "").replace("%", "")
            if s == "" or s.lower() in {"none", "nan", "null", "<na>", "pd.na", "-", "－", "—"}:
                return float(default)
            v = s
        x = float(v)
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _sm_fib_side(row: dict[str, Any]) -> str:
    try:
        s = str(row.get("side") or row.get("ai_side") or row.get("entry_decision") or "BUY").strip().upper()
        return "SELL" if s in {"SELL", "SHORT", "1", "売", "売り"} else "BUY"
    except Exception:
        return "BUY"


def _sm_fib_first(row: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    for root_key in ("source_row", "ai_row", "_raw", "raw", "features", "metrics"):
        raw = row.get(root_key)
        if isinstance(raw, dict):
            for k in keys:
                v = raw.get(k)
                if v is not None and str(v).strip() != "":
                    return v
    return default


def _sm_fib_num(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    return _sm_fib_f(_sm_fib_first(row, tuple(keys), default), default)


def _sm_fib_pct_distance(price: float, level: float) -> float:
    if price <= 0 or level <= 0:
        return 999.0
    return abs(price - level) / price


def _sm_fib_levels(row: dict[str, Any]) -> tuple[float, float, float]:
    close = _sm_fib_num(row, "close_price", "price", "current_price", "close", "last_price")
    high = _sm_fib_num(
        row,
        "swing_high", "recent_high", "intraday_high", "day_high", "today_high", "range_high",
        "high_1m_max", "high", "high_price", "HighPrice",
    )
    low = _sm_fib_num(
        row,
        "swing_low", "recent_low", "intraday_low", "day_low", "today_low", "range_low",
        "low_1m_min", "low", "low_price", "LowPrice",
    )
    return close, high, low


def _sm_fib_ev_from_row(row: dict[str, Any]) -> tuple[float | None, dict[str, Any]]:
    # Direct EV / expectancy columns first.
    for key in (
        "expected_value", "expectancy", "ev", "EV", "e_value", "e_calc", "e_calculated",
        "emp_expected_value", "summary_ai_expected_value", "prob_expected_value",
    ):
        raw = _sm_fib_first(row, (key,), None)
        if raw is not None and str(raw).strip() != "":
            return _sm_fib_f(raw, 0.0), {"source": key}

    # Otherwise compute a small EV from empirical target/risk probabilities if present.
    p_target = _sm_fib_num(row, "target_prob", "take_profit_prob", "tp_prob", "emp_target_prob", "prob_take", "p_take", default=-1.0)
    p_risk = _sm_fib_num(row, "risk_prob", "stop_loss_prob", "sl_prob", "emp_risk_prob", "prob_stop", "p_stop", default=-1.0)
    take = _sm_fib_num(row, "take_profit_pct", "target_pct", "tp_pct", default=_sm_fib_env_float("SUMMARY_AI_EV_DEFAULT_TAKE_PCT", 0.002))
    stop = _sm_fib_num(row, "stop_loss_pct", "risk_pct", "sl_pct", default=_sm_fib_env_float("SUMMARY_AI_EV_DEFAULT_STOP_PCT", 0.003))
    # Accept 0-100 or 0-1 probability forms.
    if p_target > 1.0:
        p_target /= 100.0
    if p_risk > 1.0:
        p_risk /= 100.0
    if p_target >= 0.0 and p_risk >= 0.0:
        ev = p_target * abs(take) - p_risk * abs(stop)
        return ev, {"source": "computed_prob", "p_target": p_target, "p_risk": p_risk, "take": take, "stop": stop}
    return None, {"source": "missing"}


def _sm_fib_overlay(row: dict[str, Any], tags: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict) or not isinstance(tags, dict):
        return tags
    side = _sm_fib_side(row)
    close, high, low = _sm_fib_levels(row)
    rng = high - low if high > low else 0.0
    reasons = [x for x in str(tags.get("strategy_reasons") or "").split("/") if x]
    fib_score = 0.0
    fib_zone = "NONE"
    fib_retrace = 0.0
    fib_extension = 0.0
    near = _sm_fib_env_float("SUMMARY_AI_FIB_NEAR_PCT", 0.0035)

    if close > 0 and rng > 0:
        if side == "BUY":
            fib382 = high - rng * 0.382
            fib500 = high - rng * 0.500
            fib618 = high - rng * 0.618
            ext1272 = high + rng * 0.272
            ext1618 = high + rng * 0.618
            fib_retrace = (high - close) / rng
            fib_extension = (close - high) / rng if close > high else 0.0
            if fib382 >= close >= fib618:
                fib_score += 1.0
                fib_zone = "BUY_PULLBACK_382_618"
                reasons.append("fib_buy_pullback_382_618")
            if _sm_fib_pct_distance(close, fib500) <= near:
                fib_score += 0.5
                fib_zone = "BUY_PULLBACK_500"
                reasons.append("fib_buy_near_50")
            if close > high:
                fib_score += 0.4
                fib_zone = "BUY_EXTENSION_BREAKOUT"
                reasons.append("fib_buy_extension_breakout")
            if close >= ext1272:
                fib_score -= 0.3
                reasons.append("fib_buy_extension_1272_caution")
            if close >= ext1618:
                fib_score -= 0.8
                reasons.append("fib_buy_extension_1618_overheat")
                # Overextension is more useful as REVERSAL/exit caution than a fresh BUY add.
                tags["strategy_reversal_score"] = round(_sm_fib_f(tags.get("strategy_reversal_score"), 0.0) + 0.8, 4)
        else:
            fib382 = low + rng * 0.382
            fib500 = low + rng * 0.500
            fib618 = low + rng * 0.618
            ext1272 = low - rng * 0.272
            ext1618 = low - rng * 0.618
            fib_retrace = (close - low) / rng
            fib_extension = (low - close) / rng if close < low else 0.0
            if fib382 <= close <= fib618:
                fib_score += 1.0
                fib_zone = "SELL_PULLBACK_382_618"
                reasons.append("fib_sell_pullback_382_618")
            if _sm_fib_pct_distance(close, fib500) <= near:
                fib_score += 0.5
                fib_zone = "SELL_PULLBACK_500"
                reasons.append("fib_sell_near_50")
            if close < low:
                fib_score += 0.4
                fib_zone = "SELL_EXTENSION_BREAKOUT"
                reasons.append("fib_sell_extension_breakout")
            if close <= ext1272:
                fib_score -= 0.3
                reasons.append("fib_sell_extension_1272_caution")
            if close <= ext1618:
                fib_score -= 0.8
                reasons.append("fib_sell_extension_1618_overheat")
                tags["strategy_reversal_score"] = round(_sm_fib_f(tags.get("strategy_reversal_score"), 0.0) + 0.8, 4)

        mode = str(tags.get("strategy_mode") or "").upper()
        if "PULLBACK" in fib_zone:
            tags["strategy_pullback_score"] = round(_sm_fib_f(tags.get("strategy_pullback_score"), 0.0) + max(0.0, fib_score), 4)
            # Prefer PULLBACK when it becomes clearly stronger than breakout.
            if _sm_fib_f(tags.get("strategy_pullback_score"), 0.0) >= _sm_fib_env_float("SUMMARY_AI_STRATEGY_PULLBACK_MIN", 2.0):
                if mode not in {"REVERSAL", "WATCH_ONLY_REVERSAL"}:
                    tags["strategy_mode"] = "PULLBACK"
        elif "EXTENSION_BREAKOUT" in fib_zone:
            tags["strategy_breakout_score"] = round(_sm_fib_f(tags.get("strategy_breakout_score"), 0.0) + max(0.0, fib_score), 4)
        elif fib_score < 0:
            # Do not fully block here; let the strategy guard / blowoff / exit checks do final safety.
            tags["strategy_breakout_score"] = round(_sm_fib_f(tags.get("strategy_breakout_score"), 0.0) + fib_score, 4)

    ev, ev_detail = _sm_fib_ev_from_row(row)
    ev_score = 0.0
    if ev is not None:
        min_positive = _sm_fib_env_float("SUMMARY_AI_EV_POSITIVE_MIN", 0.0)
        if ev > min_positive:
            ev_score = min(0.8, ev / max(0.0001, _sm_fib_env_float("SUMMARY_AI_EV_SCORE_UNIT", 0.002)))
            reasons.append("ev_positive")
        elif ev < _sm_fib_env_float("SUMMARY_AI_EV_NEGATIVE_MAX", -0.0005):
            ev_score = -0.8
            reasons.append("ev_negative")
        # EV is strategy-agnostic quality. Apply to the current winning trend-follow mode.
        mode = str(tags.get("strategy_mode") or "").upper()
        if mode == "PULLBACK":
            tags["strategy_pullback_score"] = round(_sm_fib_f(tags.get("strategy_pullback_score"), 0.0) + ev_score, 4)
        elif mode in {"BREAKOUT", ""}:
            tags["strategy_breakout_score"] = round(_sm_fib_f(tags.get("strategy_breakout_score"), 0.0) + ev_score, 4)
        elif mode == "REVERSAL":
            tags["strategy_reversal_score"] = round(_sm_fib_f(tags.get("strategy_reversal_score"), 0.0) + ev_score, 4)

    # Recompute mode/score after overlay, but keep disabled reversal as watch-only unless explicitly enabled.
    scores = {
        "BREAKOUT": _sm_fib_f(tags.get("strategy_breakout_score"), 0.0),
        "PULLBACK": _sm_fib_f(tags.get("strategy_pullback_score"), 0.0),
        "REVERSAL": _sm_fib_f(tags.get("strategy_reversal_score"), 0.0),
    }
    mode = max(scores, key=scores.get)
    if mode == "REVERSAL" and not _sm_fib_env_bool("SUMMARY_AI_STRATEGY_ENABLE_REVERSAL", False):
        if scores[mode] < _sm_fib_env_float("SUMMARY_AI_STRATEGY_REVERSAL_STRONG_MIN", 3.2):
            mode = "WATCH_ONLY_REVERSAL"
    tags.update({
        "strategy_mode": mode,
        "strategy_score": round(scores.get(mode.replace("WATCH_ONLY_", ""), scores.get(mode, 0.0)), 4),
        "strategy_fib_score": round(fib_score, 4),
        "strategy_fib_zone": fib_zone,
        "strategy_fib_retrace": round(fib_retrace, 4),
        "strategy_fib_extension": round(fib_extension, 4),
        "strategy_expected_value": None if ev is None else round(float(ev), 6),
        "strategy_ev_score": round(ev_score, 4),
        "strategy_ev_detail": ev_detail,
        "strategy_reasons": "/".join(dict.fromkeys(reasons))[:700],
    })
    return tags


_SM_NVE_INSTALLED = False


_SM_NVE_ORIG_CLASSIFY = None


_SM_NVE_ORIG_STRATEGY_ENABLED = None


def _sm_nve_env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _sm_nve_env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _sm_nve_f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return float(default)
        if isinstance(v, str):
            s = v.strip().replace(",", "").replace("円", "").replace("株", "").replace("%", "")
            if s == "" or s.lower() in {"none", "nan", "null", "<na>", "pd.na", "-", "－", "—"}:
                return float(default)
            v = s
        x = float(v)
        if x != x:
            return float(default)
        return x
    except Exception:
        return float(default)


def _sm_nve_side(row: dict[str, Any]) -> str:
    try:
        s = str(row.get("side") or row.get("ai_side") or row.get("entry_decision") or "BUY").strip().upper()
        return "SELL" if s in {"SELL", "SHORT", "1", "売", "売り"} else "BUY"
    except Exception:
        return "BUY"


def _sm_nve_first(row: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    for root_key in ("source_row", "ai_row", "_raw", "raw", "features", "metrics"):
        raw = row.get(root_key)
        if isinstance(raw, dict):
            for k in keys:
                v = raw.get(k)
                if v is not None and str(v).strip() != "":
                    return v
    return default


def _sm_nve_num(row: dict[str, Any], *keys: str, default: float = 0.0) -> float:
    return _sm_nve_f(_sm_nve_first(row, tuple(keys), default), default)


def _sm_nve_levels(row: dict[str, Any]) -> tuple[float, float, float]:
    close = _sm_nve_num(row, "close_price", "price", "current_price", "close", "last_price")
    high = _sm_nve_num(
        row,
        "swing_high", "recent_high", "intraday_high", "day_high", "today_high", "range_high",
        "high_1m_max", "high", "high_price", "HighPrice",
    )
    low = _sm_nve_num(
        row,
        "swing_low", "recent_low", "intraday_low", "day_low", "today_low", "range_low",
        "low_1m_min", "low", "low_price", "LowPrice",
    )
    return close, high, low


def _sm_nve_near_pct(price: float, target: float) -> float:
    if price <= 0 or target <= 0:
        return 999.0
    return abs(price - target) / price


def _sm_nve_projection(close: float, high: float, low: float, side: str) -> dict[str, float]:
    rng = high - low
    if close <= 0 or high <= 0 or low <= 0 or rng <= 0:
        return {}
    if side == "BUY":
        a = low
        b = high
        c = close
        return {
            "n": c + (b - a),
            "v": b + max(0.0, b - c),
            "e": b + (b - a),
            "range": rng,
            "progress_to_n": (close - c) / max(1e-9, (c + (b - a)) - c),
        }
    a = high
    b = low
    c = close
    return {
        "n": c - (a - b),
        "v": b - max(0.0, c - b),
        "e": b - (a - b),
        "range": rng,
        "progress_to_n": (c - close) / max(1e-9, c - (c - (a - b))),
    }


def _sm_nve_target_room_pct(close: float, target: float, side: str) -> float:
    if close <= 0 or target <= 0:
        return 0.0
    if side == "BUY":
        return (target - close) / close
    return (close - target) / close


def _sm_nve_overlay(row: dict[str, Any], tags: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(row, dict) or not isinstance(tags, dict):
        return tags
    close, high, low = _sm_nve_levels(row)
    side = _sm_nve_side(row)
    proj = _sm_nve_projection(close, high, low, side)
    reasons = [x for x in str(tags.get("strategy_reasons") or "").split("/") if x]
    nve_score = 0.0
    nve_zone = "NONE"
    nearest_name = ""
    nearest_dist = 999.0
    room_to_n = room_to_v = room_to_e = 0.0

    if proj:
        n = proj["n"]
        v = proj["v"]
        e = proj["e"]
        room_to_n = _sm_nve_target_room_pct(close, n, side)
        room_to_v = _sm_nve_target_room_pct(close, v, side)
        room_to_e = _sm_nve_target_room_pct(close, e, side)
        distances = {"N": _sm_nve_near_pct(close, n), "V": _sm_nve_near_pct(close, v), "E": _sm_nve_near_pct(close, e)}
        nearest_name = min(distances, key=distances.get)
        nearest_dist = float(distances[nearest_name])
        near_pct = _sm_nve_env_float("SUMMARY_AI_NVE_TARGET_NEAR_PCT", 0.0035)
        min_room = _sm_nve_env_float("SUMMARY_AI_NVE_MIN_ROOM_PCT", 0.0020)

        # Positive: target room still exists. This helps avoid buying after the move is already exhausted.
        if room_to_n >= min_room:
            nve_score += 0.6
            reasons.append("n_value_room")
        if room_to_v >= min_room:
            nve_score += 0.4
            reasons.append("v_value_room")
        if room_to_e >= min_room:
            nve_score += 0.3
            reasons.append("e_value_room")

        # Near targets: treat as take-profit / caution zone for fresh entry.
        if nearest_dist <= near_pct:
            nve_zone = f"NEAR_{nearest_name}_VALUE"
            reasons.append(f"near_{nearest_name.lower()}_value_target")
            if nearest_name == "N":
                nve_score -= 0.2
            elif nearest_name == "V":
                nve_score -= 0.4
            elif nearest_name == "E":
                nve_score -= 0.8
                reasons.append("e_value_overextension_caution")
                tags["strategy_reversal_score"] = round(_sm_nve_f(tags.get("strategy_reversal_score"), 0.0) + 0.6, 4)

        # Already beyond targets: do not chase; strong E overshoot is reversal/exit caution.
        if side == "BUY":
            if close >= n:
                nve_score -= 0.25; reasons.append("above_n_value_caution")
            if close >= v:
                nve_score -= 0.45; reasons.append("above_v_value_caution")
            if close >= e:
                nve_score -= 0.9; reasons.append("above_e_value_overheat")
                tags["strategy_reversal_score"] = round(_sm_nve_f(tags.get("strategy_reversal_score"), 0.0) + 0.9, 4)
        else:
            if close <= n:
                nve_score -= 0.25; reasons.append("below_n_value_caution")
            if close <= v:
                nve_score -= 0.45; reasons.append("below_v_value_caution")
            if close <= e:
                nve_score -= 0.9; reasons.append("below_e_value_overheat")
                tags["strategy_reversal_score"] = round(_sm_nve_f(tags.get("strategy_reversal_score"), 0.0) + 0.9, 4)

        mode = str(tags.get("strategy_mode") or "").upper()
        if mode == "PULLBACK":
            tags["strategy_pullback_score"] = round(_sm_nve_f(tags.get("strategy_pullback_score"), 0.0) + nve_score, 4)
        elif mode == "REVERSAL":
            tags["strategy_reversal_score"] = round(_sm_nve_f(tags.get("strategy_reversal_score"), 0.0) + nve_score, 4)
        else:
            tags["strategy_breakout_score"] = round(_sm_nve_f(tags.get("strategy_breakout_score"), 0.0) + nve_score, 4)

        scores = {
            "BREAKOUT": _sm_nve_f(tags.get("strategy_breakout_score"), 0.0),
            "PULLBACK": _sm_nve_f(tags.get("strategy_pullback_score"), 0.0),
            "REVERSAL": _sm_nve_f(tags.get("strategy_reversal_score"), 0.0),
        }
        mode2 = max(scores, key=scores.get)
        if mode2 == "REVERSAL" and not _sm_nve_env_bool("SUMMARY_AI_STRATEGY_ENABLE_REVERSAL", False):
            if scores[mode2] < _sm_nve_env_float("SUMMARY_AI_STRATEGY_REVERSAL_STRONG_MIN", 3.2):
                mode2 = "WATCH_ONLY_REVERSAL"
        tags["strategy_mode"] = mode2
        tags["strategy_score"] = round(scores.get(mode2.replace("WATCH_ONLY_", ""), scores.get(mode2, 0.0)), 4)
        tags["strategy_n_value"] = round(n, 4)
        tags["strategy_v_value"] = round(v, 4)
        tags["strategy_e_value"] = round(e, 4)

    tags.update({
        "strategy_nve_score": round(nve_score, 4),
        "strategy_nve_zone": nve_zone,
        "strategy_nve_nearest": nearest_name,
        "strategy_nve_nearest_dist_pct": round(nearest_dist, 6) if nearest_dist < 900 else None,
        "strategy_n_room_pct": round(room_to_n, 6),
        "strategy_v_room_pct": round(room_to_v, 6),
        "strategy_e_room_pct": round(room_to_e, 6),
        "strategy_reasons": "/".join(dict.fromkeys(reasons))[:900],
    })
    return tags


def _sm_classify(row: dict[str, Any]) -> dict[str, Any]:
    tags = _sm_classify_core(row)
    if _sm_fib_env_bool("SUMMARY_AI_FIB_EV_STRATEGY_ENABLED", True):
        try:
            tags = _sm_fib_overlay(row, dict(tags or {}))
        except Exception:
            logger.debug("[SUMMARY AI FIB EV] overlay failed row=%s", row, exc_info=True)
    if _sm_nve_env_bool("SUMMARY_AI_NVE_STRATEGY_ENABLED", True):
        try:
            tags = _sm_nve_overlay(row, dict(tags or {}))
        except Exception:
            logger.debug("[SUMMARY AI NVE] overlay failed row=%s", row, exc_info=True)
    return tags


def _sm_strategy_enabled(row: dict[str, Any]) -> tuple[bool, str]:
    ok, reason = _sm_strategy_enabled_core(row)

    # 1層目: 期待値がはっきり負なら弾く (旧 fibonacci_ev)
    if ok and _sm_fib_env_bool("SUMMARY_AI_FIB_EV_STRATEGY_ENABLED", True):
        ev = row.get("strategy_expected_value")
        if ev is not None and _sm_fib_env_bool("SUMMARY_AI_EV_BLOCK_NEGATIVE", True):
            if _sm_fib_f(ev, 0.0) < _sm_fib_env_float("SUMMARY_AI_EV_BLOCK_BELOW", -0.0005):
                return False, "ev_negative"

    # 2層目: 目標が E 値寄り / N 値までの余地が無いなら弾く (旧 nve_projection)
    if ok and _sm_nve_env_bool("SUMMARY_AI_NVE_STRATEGY_ENABLED", True):
        if not _sm_nve_env_bool("SUMMARY_AI_NVE_BLOCK_EXHAUSTED", True):
            return ok, reason
        zone = str(row.get("strategy_nve_zone") or "").upper()
        room_n = _sm_nve_f(row.get("strategy_n_room_pct"), 0.0)
        if zone == "NEAR_E_VALUE":
            return False, "near_e_value_target"
        if room_n < _sm_nve_env_float("SUMMARY_AI_NVE_MIN_ENTRY_ROOM_PCT", 0.0010):
            return False, "n_value_room_too_small"

    return ok, reason


def _passes_strict_candidate_quality(item: Dict[str, Any]) -> tuple[bool, Dict[str, Any]]:
    """旧 _passes_quality_with_strategy (ex._passes_strict_candidate_quality 差し替え)。"""
    ok, detail = _passes_strict_candidate_quality_core(item)
    if not ok:
        return ok, detail
    if not isinstance(item, dict):
        return ok, detail
    row = dict(item)
    try:
        src = item.get("source_row")
        ai = item.get("ai_row")
        if isinstance(src, dict):
            row.update({k: v for k, v in src.items() if k not in row})
        if isinstance(ai, dict):
            row.update({k: v for k, v in ai.items() if k not in row})
    except Exception:
        pass
    _sm_enrich(row)
    allow, reason = _sm_strategy_enabled(row)
    if not allow:
        return False, {
            "symbol": row.get("symbol") or item.get("symbol"),
            "side": row.get("side") or item.get("side"),
            "reason": reason,
            "strategy_mode": row.get("strategy_mode"),
            "strategy_score": row.get("strategy_score"),
            "strategy_reasons": row.get("strategy_reasons"),
        }
    try:
        item.update({k: row.get(k) for k in (
            "strategy_mode", "strategy_score", "strategy_breakout_score", "strategy_pullback_score",
            "strategy_reversal_score", "strategy_reasons", "strategy_aligned_mtf_count", "strategy_range_pct",
        )})
    except Exception:
        pass
    return True, detail


def _sort_key(item):
    """旧 _sort_key_with_strategy (ex._sort_key 差し替え)。"""
    base = _sort_key_bsr(item)
    row = dict(item or {})
    try:
        if isinstance(item.get("source_row"), dict):
            row.update({k: v for k, v in item.get("source_row").items() if k not in row})
        if isinstance(item.get("ai_row"), dict):
            row.update({k: v for k, v in item.get("ai_row").items() if k not in row})
        _sm_enrich(row)
        mode = str(row.get("strategy_mode") or "").upper()
        mode_rank = {"BREAKOUT": 3, "PULLBACK": 2, "REVERSAL": 1, "WATCH_ONLY_REVERSAL": -1}.get(mode, 0)
        strat_score = _sm_f(row.get("strategy_score"), 0.0)
        if isinstance(base, tuple):
            return tuple(list(base) + [mode_rank, strat_score])
        return (base, mode_rank, strat_score)
    except Exception:
        return base
