# ============================================================
# File   : trading/handlers/entry_controller.py
# Function:
#   - pending_entries に入った候補を最終審査して発注する
#   - AI final gate により全候補を評価し、期待値順にランキングする
#   - BUY / SELL 候補を比較し、priority の高い銘柄から発注する
#   - market / risk / AI health / index shock / credit / volatility
#     などの各種ガードを通過した銘柄のみエントリーする
# ------------------------------------------------------------
# Version: Ver2.3-PRODUCTION-SUMMARY-AI-NO-TONOSAMA-GATE
# ------------------------------------------------------------
# ✔ TOP候補を全件AI確認
# ✔ AI allow / confidence / summary score で最終判定
# ✔ confidence × score を主軸に priority_score を算出
# ✔ 全候補を期待値順に並べて上位から発注
# ✔ BUY / SELL 両対応
# ✔ pending_manager 連携
# ✔ boost size 対応
# ✔ pending 可視化強化
# ✔ symbol一括clearではなく executed entry のみ pop
# ✔ pipeline_source / interval フィルタ対応
# ✔ open_positions 型揺れ耐性
# ✔ quantity / order build / submit ログ強化
# ✔ entry_handler / kabu_api.buy_sell_entry の qty passthrough と整合
# ✔ SUMMARY_AI は tonosama gate を通さない
# ✔ SUMMARY_AI の最終 score threshold を 5点台候補に合わせる
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
from threading import Lock
from copy import deepcopy
from typing import Any

from global_state import global_data
from trading.summary.position_filter import can_entry_symbol

from trading.entry.pending_manager import (
    get_bucket,
    pop_entry,
    snapshot_root,
)

from trading.handlers.entry_handler import (
    place_entry_buy,
    place_entry_sell,
)

from AI.entry_row_builder import build_entry_row
from AI.entry_gate import ai_final_entry_check
from AI.entry_gate_tonosama import allow_tonosama_entry
from AI.entry_gate_sell_tonosama import allow_sell_tonosama_entry

from AI.sell_credit_guard import can_sell_symbol
from AI.orchestrator_buy_sell_tonosama import BuySellTonosamaOrchestrator
from ops.market_guard import allow_entry_by_market

from AI.risk.risk_guard import risk_ok
from AI.monitor.ai_health_guard import ai_health_ok
from AI.index_shock_detector import detect_index_shock

from trading.handlers.entry_precheck_ranking import (
    precheck_ranking_entry,
    log_precheck_result,
)

from trading.handlers.entry_order_builder import build_entry_order
from trading.filters.volatility_filter import (
    atr_1m_filter,
    range_5m_filter,
)

from trading.entry.lot_sizer import calculate_entry_quantity

from trading.risk.boost_engine import BoostEngine
from trading.monitor.boost_monitor import BoostMonitor
from utils.business_day_utils import is_market_open

from AI.ranking_feature_builder import build_ranking_feature_1min
from AI.ranking_entry_predictor import predict_ranking_entry

from utils_common import normalize_symbol

logger = logging.getLogger("entry_controller")

# ==========================================================
# 定数
# ==========================================================

ENTRY_TYPE_PRIORITY = {
    "EARLY_SCALP": 4,
    "SUMMARY_AI": 3,
    "RANKING_5S": 2,
    "TONOSAMA": 1,
}

PIPELINE_SOURCE = {
    "SUMMARY",
    "RANKING",
    "TONOSAMA",
    "EARLY_SCALP",
}

API_RATE_LIMIT_COOLDOWN_SEC = 60
SYMBOL_TRADE_RESTRICT_SEC = 1800
GLOBAL_RATE_LIMIT_KEY = "__GLOBAL__"

MIN_ENTRY_QTY = 100
BOOST_SIZE_MULTIPLIER = 1.5

# ==========================================================
# AI GATE TUNING
# ==========================================================

MIN_AI_CONFIDENCE_BUY = 0.60
MIN_AI_CONFIDENCE_SELL = 0.55

# SUMMARY AI gate 側では 5点台の候補が AI_OK になっているため、
# entry_controller の最終gateだけ 8点必須にすると全落ちする。
MIN_SUMMARY_SCORE_BUY = 5.0
MIN_SUMMARY_SCORE_SELL = 5.0

MIN_COMPOSITE_SCORE_BUY = 5.0
MIN_COMPOSITE_SCORE_SELL = 4.5

MAX_CANDIDATES_PER_SYMBOL = 10
MAX_APPROVED_PER_RUN = 3

# ==========================================================
# Orchestrator
# ==========================================================

_orchestrator = BuySellTonosamaOrchestrator()

# ==========================================================
# ロック
# ==========================================================

_pipeline_lock = Lock()
_entry_lock = Lock()
_entry_locked_symbols: set[str] = set()

boost_engine = BoostEngine()
boost_monitor = BoostMonitor()


def lock_symbol(sym: str) -> bool:
    with _entry_lock:
        if sym in _entry_locked_symbols:
            return False
        _entry_locked_symbols.add(sym)
        return True


def reset_entry_lock():
    with _entry_lock:
        _entry_locked_symbols.clear()


def _is_trading_hours() -> bool:
    now = dt.datetime.now()
    if now.hour < 9:
        return False
    if now.hour > 15:
        return False
    if now.hour == 15 and now.minute > 30:
        return False
    return True


# ==========================================================
# 補助
# ==========================================================

def _safe_float(v, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _safe_str(v, default: str = "") -> str:
    try:
        if v is None:
            return default
        return str(v).strip()
    except Exception:
        return default


def _safe_int(v, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return default
        return int(float(v))
    except Exception:
        return default


def _normalize_source(v: Any) -> str:
    try:
        if v is None:
            return ""
        return str(v).strip().upper()
    except Exception:
        return ""


def _normalize_interval(v: Any) -> int | None:
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except Exception:
        return None


def _resolve_price(entry_row: dict) -> float:
    return _safe_float(
        entry_row.get("close_price")
        or entry_row.get("price")
        or entry_row.get("current_price")
        or entry_row.get("close"),
        0.0,
    )


def _log_skip(symbol: str, reason: str, **detail):
    logger.info(
        "⛔ ENTRY_SKIP %s reason=%s detail=%s",
        symbol,
        reason,
        detail,
    )


def _is_tonosama_entry(entry_type: Any, source: Any) -> bool:
    et = _normalize_source(entry_type)
    src = _normalize_source(source)
    return et == "TONOSAMA" or src == "TONOSAMA"


def _api_rate_limited() -> bool:
    t = global_data.trade_restricted.get(GLOBAL_RATE_LIMIT_KEY)
    if not t:
        return False

    if isinstance(t, dt.datetime):
        if (dt.datetime.now() - t).total_seconds() < API_RATE_LIMIT_COOLDOWN_SEC:
            return True
        global_data.trade_restricted.pop(GLOBAL_RATE_LIMIT_KEY, None)
        logger.warning("🟢 API rate limit cooldown finished → ENTRY resumed")
        return False

    return False


def _is_symbol_trade_restricted(symbol: str) -> bool:
    try:
        until = global_data.trade_restricted.get(symbol)
        if not until:
            return False

        if isinstance(until, dt.datetime):
            if dt.datetime.now() < until:
                return True

            global_data.trade_restricted.pop(symbol, None)
            logger.info("🟢 symbol trade restriction finished symbol=%s", symbol)
            return False

        return False

    except Exception:
        logger.exception("symbol trade restriction check failed symbol=%s", symbol)
        return False


def mark_symbol_trade_restricted(symbol: str):
    until = dt.datetime.now() + dt.timedelta(seconds=SYMBOL_TRADE_RESTRICT_SEC)
    global_data.trade_restricted[symbol] = until
    logger.warning("🚫 SYMBOL TRADE RESTRICTED %s until %s", symbol, until)


def _resolve_entry_scores(entry_row: dict, side: str) -> tuple[float, float]:
    score = _safe_float(entry_row.get("score"), 0.0)
    score_buy = _safe_float(entry_row.get("score_buy"), score)
    score_sell = _safe_float(entry_row.get("score_sell"), 0.0)

    if side == "BUY" and score_buy <= 0:
        score_buy = score
    return score_buy, score_sell


def _passes_ai_gate(entry_row: dict, ai: dict, side: str) -> tuple[bool, str]:
    if not isinstance(ai, dict):
        return False, "AI_RESULT_INVALID"

    allow = bool(ai.get("allow", False))
    if not allow:
        return False, f"AI_ALLOW_FALSE:{_safe_str(ai.get('reason'))}"

    confidence = _safe_float(ai.get("confidence"), 0.0)
    score_buy, score_sell = _resolve_entry_scores(entry_row, side=side)

    if side == "BUY":
        if confidence < MIN_AI_CONFIDENCE_BUY:
            return False, f"BUY_CONF_LOW:{confidence:.3f}"

        if score_buy < MIN_SUMMARY_SCORE_BUY:
            return False, f"BUY_SCORE_LOW:{score_buy:.3f}"

        composite = confidence * score_buy
        if composite < MIN_COMPOSITE_SCORE_BUY:
            return False, f"BUY_COMPOSITE_LOW:{composite:.3f}"

        return True, f"BUY_OK conf={confidence:.3f} score_buy={score_buy:.3f} comp={composite:.3f}"

    if side == "SELL":
        if confidence < MIN_AI_CONFIDENCE_SELL:
            return False, f"SELL_CONF_LOW:{confidence:.3f}"

        if score_sell < MIN_SUMMARY_SCORE_SELL:
            return False, f"SELL_SCORE_LOW:{score_sell:.3f}"

        composite = confidence * score_sell
        if composite < MIN_COMPOSITE_SCORE_SELL:
            return False, f"SELL_COMPOSITE_LOW:{composite:.3f}"

        return True, f"SELL_OK conf={confidence:.3f} score_sell={score_sell:.3f} comp={composite:.3f}"

    return False, f"SIDE_INVALID:{side}"


def _calc_priority_score(entry_row: dict, ai: dict, side: str) -> float:
    confidence = _safe_float(ai.get("confidence"), 0.0)
    lot_multiplier = _safe_float(ai.get("lot_multiplier"), 1.0)

    score_buy, score_sell = _resolve_entry_scores(entry_row, side=side)
    base_score = score_sell if side == "SELL" else score_buy

    priority = (confidence * base_score) + max(0.0, lot_multiplier - 1.0) * 0.5
    return priority


def _normalize_open_positions(open_positions: Any) -> set[str]:
    symbols: set[str] = set()

    try:
        if open_positions is None:
            return symbols

        if isinstance(open_positions, dict):
            for k in open_positions.keys():
                sym = normalize_symbol(k)
                if sym:
                    symbols.add(sym)
            return symbols

        if isinstance(open_positions, (list, tuple, set)):
            for p in open_positions:
                if isinstance(p, dict):
                    sym = normalize_symbol(p.get("symbol"))
                else:
                    sym = normalize_symbol(p)
                if sym:
                    symbols.add(sym)
            return symbols

        return symbols

    except Exception:
        logger.exception("open_positions normalization failed")
        return symbols


def _entry_matches_pipeline(entry: dict, pipeline_source: str | None, interval: int | None) -> bool:
    try:
        if not isinstance(entry, dict):
            return False

        if pipeline_source:
            src = _normalize_source(entry.get("source"))
            if src != _normalize_source(pipeline_source):
                return False

        if interval is not None:
            ent_interval = _normalize_interval(entry.get("interval"))
            if ent_interval is not None and ent_interval != int(interval):
                return False

        return True

    except Exception:
        logger.exception("entry pipeline filter failed entry=%s", entry)
        return False


def _build_scored_candidates(
    symbol: str,
    entries: list[dict],
    open_position_symbols: set[str],
    boost_active: bool,
    pipeline_source: str | None = None,
    interval: int | None = None,
) -> list[dict]:
    scored_candidates: list[dict] = []

    for entry in entries[:MAX_CANDIDATES_PER_SYMBOL]:
        try:
            if not _entry_matches_pipeline(entry, pipeline_source=pipeline_source, interval=interval):
                _log_skip(
                    symbol,
                    "PIPELINE_FILTER_MISMATCH",
                    pipeline_source=pipeline_source,
                    interval=interval,
                    entry_source=entry.get("source"),
                    entry_interval=entry.get("interval"),
                )
                continue

            entry_row = build_entry_row(deepcopy(entry))
            if not entry_row:
                _log_skip(symbol, "ENTRY_ROW_EMPTY")
                continue

            if "source" not in entry_row or not entry_row.get("source"):
                entry_row["source"] = entry.get("source")

            if _normalize_interval(entry_row.get("interval")) is None:
                entry_row["interval"] = _normalize_interval(entry.get("interval"))
            else:
                entry_row["interval"] = _normalize_interval(entry_row.get("interval"))

            entry_type = (
                entry_row.get("entry_type")
                or entry.get("entry_type")
                or entry_row.get("source")
                or entry.get("source")
            )
            source = entry_row.get("source") or entry.get("source")

            side = entry_row.get("entry_decision") or entry.get("entry_decision") or entry.get("side")
            side = _safe_str(side).upper()

            if side not in ("BUY", "SELL"):
                _log_skip(symbol, "SIDE_INVALID", side=side)
                continue

            if symbol in open_position_symbols:
                _log_skip(symbol, "ALREADY_OPEN_POSITION", side=side)
                continue

            if _is_symbol_trade_restricted(symbol):
                _log_skip(symbol, "SYMBOL_TRADE_RESTRICTED", side=side)
                continue

            if not can_entry_symbol(symbol, side, source=_normalize_source(source) or "SUMMARY"):
                _log_skip(symbol, "POSITION_FILTER_NG", side=side)
                continue

            tonosama_required = _is_tonosama_entry(entry_type, source)

            if side == "SELL":
                try:
                    if not can_sell_symbol(symbol):
                        _log_skip(symbol, "SELL_CREDIT_GUARD_NG", side=side)
                        continue
                except Exception:
                    logger.exception("SELL_CREDIT_GUARD failed symbol=%s", symbol)
                    continue

                if tonosama_required:
                    try:
                        if not allow_sell_tonosama_entry(symbol):
                            _log_skip(symbol, "SELL_TONOSAMA_NG", side=side, entry_type=entry_type, source=source)
                            continue
                    except Exception:
                        logger.exception("SELL_TONOSAMA failed symbol=%s", symbol)
                        continue

            if side == "BUY" and tonosama_required:
                try:
                    if not allow_tonosama_entry(symbol):
                        _log_skip(symbol, "BUY_TONOSAMA_NG", side=side, entry_type=entry_type, source=source)
                        continue
                except Exception:
                    logger.exception("BUY_TONOSAMA failed symbol=%s", symbol)
                    continue

            try:
                if not atr_1m_filter(entry_row):
                    _log_skip(symbol, "ATR_1M_FILTER_NG", side=side)
                    continue
            except Exception:
                logger.exception("ATR_1M_FILTER failed symbol=%s", symbol)
                continue

            try:
                if not range_5m_filter(entry_row):
                    _log_skip(symbol, "RANGE_5M_FILTER_NG", side=side)
                    continue
            except Exception:
                logger.exception("RANGE_5M_FILTER failed symbol=%s", symbol)
                continue

            ai = ai_final_entry_check(entry_row)

            ai_ok, ai_msg = _passes_ai_gate(entry_row, ai, side=side)
            if not ai_ok:
                _log_skip(
                    symbol,
                    "AI_GATE_NG",
                    side=side,
                    ai_reason=_safe_str(ai.get("reason")) if isinstance(ai, dict) else "",
                    ai_confidence=_safe_float(ai.get("confidence"), 0.0) if isinstance(ai, dict) else 0.0,
                    gate_reason=ai_msg,
                    score=_safe_float(entry_row.get("score"), 0.0),
                    score_buy=_safe_float(entry_row.get("score_buy"), 0.0),
                    score_sell=_safe_float(entry_row.get("score_sell"), 0.0),
                )
                continue

            priority_score = _calc_priority_score(entry_row, ai, side=side)

            logger.info(
                "✅ AI_GATE_OK symbol=%s side=%s detail=%s ai_reason=%s priority=%.4f source=%s interval=%s",
                symbol,
                side,
                ai_msg,
                _safe_str(ai.get("reason")),
                priority_score,
                entry_row.get("source"),
                entry_row.get("interval"),
            )

            scored_candidates.append(
                {
                    "symbol": symbol,
                    "entry": entry,
                    "entry_row": entry_row,
                    "entry_type": entry_type,
                    "side": side,
                    "ai": ai,
                    "ai_msg": ai_msg,
                    "priority_score": priority_score,
                    "confidence": _safe_float(ai.get("confidence"), 0.0),
                    "score_buy": _resolve_entry_scores(entry_row, side)[0],
                    "score_sell": _resolve_entry_scores(entry_row, side)[1],
                }
            )

        except Exception:
            logger.exception("🔥 ENTRY_CANDIDATE_BUILD_EXCEPTION symbol=%s", symbol)

    scored_candidates.sort(
        key=lambda x: (
            x.get("priority_score", 0.0),
            x.get("confidence", 0.0),
            x.get("score_buy", 0.0) if x.get("side") == "BUY" else x.get("score_sell", 0.0),
        ),
        reverse=True,
    )

    return scored_candidates


def _execute_best_candidate(item: dict, boost_active: bool) -> bool:
    symbol = item["symbol"]
    entry_row = item["entry_row"]
    entry_type = item["entry_type"]
    side = item["side"]
    ai = item["ai"]

    price = _resolve_price(entry_row)

    qty = calculate_entry_quantity(
        symbol=symbol,
        price=price,
        confidence=ai.get("confidence", 0.0),
        lot_multiplier=ai.get("lot_multiplier", 1.0),
        atr=entry_row.get("atr")
        or entry_row.get("atr_1m")
        or entry_row.get("atr_5m"),
    )

    logger.info(
        "🧮 ENTRY_QTY_CALC symbol=%s side=%s price=%s confidence=%s lot_multiplier=%s qty_raw=%s boost_active=%s",
        symbol,
        side,
        price,
        ai.get("confidence", 0.0),
        ai.get("lot_multiplier", 1.0),
        qty,
        boost_active,
    )

    if qty <= 0:
        logger.warning(
            "⚠ ENTRY_QTY_FALLBACK symbol=%s qty_raw=%s -> MIN_ENTRY_QTY=%s",
            symbol,
            qty,
            MIN_ENTRY_QTY,
        )
        qty = MIN_ENTRY_QTY

    if boost_active:
        qty = int(qty * BOOST_SIZE_MULTIPLIER)
        if qty <= 0:
            qty = MIN_ENTRY_QTY

    logger.info(
        "🧮 ENTRY_QTY_FINAL symbol=%s side=%s qty_final=%s boost_active=%s",
        symbol,
        side,
        qty,
        boost_active,
    )

    order = build_entry_order(
        symbol=symbol,
        side=side,
        source=entry_type,
        entry_row=entry_row,
        qty_override=qty,
    )

    if not isinstance(order, dict):
        _log_skip(symbol, "ORDER_BUILD_INVALID", side=side, order_type=type(order).__name__)
        return False

    if not order.get("ok"):
        _log_skip(symbol, "ORDER_BUILD_NG", side=side, detail=order)
        return False

    d = order.get("detail") or {}
    order_qty = _safe_int(d.get("qty"), qty)
    order_type = _safe_str(d.get("order_type"), "LIMIT").upper()
    order_price = d.get("price")

    logger.info(
        "📝 ORDER_BUILD_OK symbol=%s side=%s qty=%s order_type=%s price=%s source=%s entry_type=%s",
        symbol,
        side,
        order_qty,
        order_type,
        order_price,
        entry_row.get("source"),
        entry_type,
    )

    logger.info(
        "📤 ENTRY_DISPATCH symbol=%s side=%s qty=%s order_type=%s price=%s handler=%s",
        symbol,
        side,
        order_qty,
        order_type,
        order_price,
        "place_entry_buy" if side == "BUY" else "place_entry_sell",
    )

    order_id = (
        place_entry_buy(
            symbol,
            entry_row.get("symbolname"),
            order_price,
            ai.get("reason", ""),
            order_type,
            order_qty,
        )
        if side == "BUY"
        else place_entry_sell(
            symbol,
            entry_row.get("symbolname"),
            order_price,
            ai.get("reason", ""),
            order_type,
            order_qty,
        )
    )

    if not order_id:
        mark_symbol_trade_restricted(symbol)
        _log_skip(
            symbol,
            "ORDER_ID_EMPTY",
            side=side,
            qty=order_qty,
            order_type=order_type,
            price=order_price,
        )
        return False

    global_data.add_entry_inflight(symbol, order_id, side)

    logger.info(
        "🚀 ENTRY_APPROVED symbol=%s side=%s qty=%s order_type=%s price=%s priority=%.4f order_id=%s",
        symbol,
        side,
        order_qty,
        order_type,
        order_price,
        item.get("priority_score", 0.0),
        order_id,
    )
    return True


# ==========================================================
# メイン
# ==========================================================

def run_entry_pipeline(
    *,
    pipeline_source: str | None = None,
    interval: int | None = None,
):
    if not is_market_open() or not _is_trading_hours():
        logger.info(
            "⛔ ENTRY blocked (market closed or out of trading hours)"
        )
        return

    if pipeline_source:
        pipeline_source = _normalize_source(pipeline_source)

    if interval is not None:
        interval = _normalize_interval(interval)

    if pipeline_source and pipeline_source not in PIPELINE_SOURCE:
        logger.error("❌ invalid pipeline_source=%s", pipeline_source)
        return

    if not _pipeline_lock.acquire(blocking=False):
        logger.warning("⏸ ENTRY PIPELINE already running → skip")
        return

    try:
        logger.info(
            "🔥 ENTRY PIPELINE START pipeline_source=%s interval=%s pending_root=%s",
            pipeline_source,
            interval,
            snapshot_root(),
        )

        if pipeline_source == "RANKING":
            pre = precheck_ranking_entry()
            log_precheck_result(pre)
            if not pre.get("is_ready", False):
                _log_skip("__GLOBAL__", "RANKING_PRECHECK_NG", **pre)
                return

        if _api_rate_limited():
            _log_skip("__GLOBAL__", "API_RATE_LIMIT")
            return

        if not ai_health_ok():
            _log_skip("__GLOBAL__", "AI_HEALTH_NG")
            return

        if not risk_ok():
            _log_skip("__GLOBAL__", "RISK_GUARD_NG")
            return

        if detect_index_shock() != 0:
            _log_skip("__GLOBAL__", "INDEX_SHOCK")
            return

        if not allow_entry_by_market(
            now=dt.datetime.now(),
            nikkei_velocity=getattr(global_data, "nikkei_velocity", None),
            api_429_count=getattr(global_data, "api_429_count", 0),
            board_update_delay_sec=getattr(global_data, "board_delay_sec", None),
        ):
            _log_skip("__GLOBAL__", "MARKET_GUARD_NG")
            return

        regime = getattr(global_data, "current_regime", 0)
        drawdown = getattr(global_data, "current_drawdown", 0.0)
        collapse_prob = getattr(global_data, "collapse_prob", 0.0)
        consecutive_losses = getattr(global_data, "consecutive_losses", 0)
        win_rate = getattr(global_data, "recent_win_rate", 0.5)

        boost_active = boost_engine.update(
            win_rate=win_rate,
            regime=regime,
            drawdown=drawdown,
            collapse_prob=collapse_prob,
            consecutive_losses=consecutive_losses,
            regime_changed=getattr(global_data, "regime_changed", False),
        )

        boost_monitor.update(
            active=boost_active,
            win_rate=win_rate,
            drawdown=drawdown,
            collapse_prob=collapse_prob,
            regime=regime,
        )

        reset_entry_lock()

        pending_root = getattr(global_data, "pending_entries", {})
        if not pending_root:
            logger.info("📭 no pending entries")
            return

        approved_count = 0
        open_position_symbols = _normalize_open_positions(getattr(global_data, "open_positions", None))
        global_scored_candidates: list[dict] = []

        logger.info(
            "📦 PENDING_SCAN_START symbols=%s open_positions=%s",
            list(pending_root.keys()),
            sorted(open_position_symbols),
        )

        for raw_symbol in list(pending_root.keys()):
            try:
                bucket = get_bucket(raw_symbol)
                if not bucket:
                    logger.info("📭 empty pending bucket raw_symbol=%s", raw_symbol)
                    continue

                symbol = normalize_symbol(raw_symbol)
                if not symbol:
                    _log_skip(str(raw_symbol), "SYMBOL_NORMALIZE_NG")
                    continue

                entries = sorted(
                    bucket,
                    key=lambda e: (
                        ENTRY_TYPE_PRIORITY.get(e.get("entry_type"), 0),
                        _safe_float(e.get("score"), 0.0),
                        e.get("created_at") or dt.datetime.min,
                    ),
                    reverse=True,
                )

                logger.info(
                    "📦 PENDING_BUCKET symbol=%s raw_symbol=%s size=%s entries=%s",
                    symbol,
                    raw_symbol,
                    len(entries),
                    [
                        {
                            "source": e.get("source"),
                            "entry_type": e.get("entry_type"),
                            "side": e.get("side"),
                            "interval": e.get("interval"),
                            "score": e.get("score"),
                        }
                        for e in entries[:10]
                    ],
                )

                scored_candidates = _build_scored_candidates(
                    symbol=symbol,
                    entries=entries,
                    open_position_symbols=open_position_symbols,
                    boost_active=boost_active,
                    pipeline_source=pipeline_source,
                    interval=interval,
                )

                if scored_candidates:
                    global_scored_candidates.extend(scored_candidates)

            except Exception:
                logger.exception("🔥 ENTRY_EXCEPTION symbol=%s", raw_symbol)

        if not global_scored_candidates:
            logger.info(
                "📭 no AI-approved candidates after full evaluation pending_root=%s",
                snapshot_root(),
            )
            return

        global_scored_candidates.sort(
            key=lambda x: (
                x.get("priority_score", 0.0),
                x.get("confidence", 0.0),
            ),
            reverse=True,
        )

        logger.info(
            "📊 AI_RANKED_CANDIDATES total=%d top=%s",
            len(global_scored_candidates),
            [
                {
                    "symbol": x.get("symbol"),
                    "side": x.get("side"),
                    "priority": round(_safe_float(x.get("priority_score"), 0.0), 4),
                    "conf": round(_safe_float(x.get("confidence"), 0.0), 4),
                    "source": x.get("entry", {}).get("source") if isinstance(x.get("entry"), dict) else None,
                    "interval": x.get("entry", {}).get("interval") if isinstance(x.get("entry"), dict) else None,
                }
                for x in global_scored_candidates[:10]
            ],
        )

        executed_symbols: set[str] = set()

        for item in global_scored_candidates:
            if approved_count >= MAX_APPROVED_PER_RUN:
                break

            symbol = item["symbol"]
            side = item["side"]
            entry = item["entry"]

            if symbol in executed_symbols:
                _log_skip(symbol, "SYMBOL_ALREADY_EXECUTED_THIS_RUN", side=side)
                continue

            if symbol in open_position_symbols:
                _log_skip(symbol, "ALREADY_OPEN_POSITION_LATE", side=side)
                continue

            if _is_symbol_trade_restricted(symbol):
                _log_skip(symbol, "SYMBOL_TRADE_RESTRICTED_LATE", side=side)
                continue

            if not lock_symbol(symbol):
                _log_skip(symbol, "SYMBOL_LOCKED_LATE", side=side)
                continue

            ok = _execute_best_candidate(item, boost_active=boost_active)
            if not ok:
                continue

            approved_count += 1
            executed_symbols.add(symbol)

            try:
                pop_entry(symbol, entry)
                logger.info(
                    "🧹 EXECUTED_ENTRY_POPPED symbol=%s side=%s source=%s interval=%s root_after=%s",
                    symbol,
                    entry.get("side") if isinstance(entry, dict) else None,
                    entry.get("source") if isinstance(entry, dict) else None,
                    entry.get("interval") if isinstance(entry, dict) else None,
                    snapshot_root(),
                )
            except Exception:
                logger.exception("pop_entry failed symbol=%s", symbol)

        logger.info(
            "📌 [ENTRY] APPROVED=%d boost=%s pending_root_after=%s",
            approved_count,
            boost_active,
            snapshot_root(),
        )

    finally:
        _pipeline_lock.release()
