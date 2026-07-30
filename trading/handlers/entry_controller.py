# ============================================================
# File   : trading/handlers/entry_controller.py
# Function:
#   - pending_entries に入った候補を最終審査して発注する
#   - AI final gate により全候補を評価し、期待値順にランキングする
#   - BUY / SELL 候補を比較し、priority の高い銘柄から発注する
#   - market / risk / AI health / index shock / credit / volatility
#     などの各種ガードを通過した銘柄のみエントリーする
# ------------------------------------------------------------
# Version: Ver2.7-INLINE-LOG-SKIP-AUDIT-AND-RANKING-PRUNE
# ------------------------------------------------------------
# Ver2.7:
#   - _log_skip() に3本の monkeypatch を統合本文化:
#     (1) core/startup/entry_log_skip_reason_collision_patch.py の
#         reason kwargs 衝突回避 + HARD_PRUNE_REASONS 該当時のランキングpending即prune
#     (2) trading/audit_logging/entry_controller_audit_patch.py の
#         ENTRY_SKIP 監査DB記録 (audit_entry_skip)
#     (3) core/startup/entry_controller_pipeline_lock_wait_patch.py の
#         _safe_log_skip が run_entry_pipeline 呼び出しの度に _log_skip を
#         「元を一切呼ばない版」へ強制上書きしていたため、(1)(2)の機能が
#         本番で恒久的に無効化されていた不具合を修正 (両機能を実際に動かす)。
#   - 引数名を reason -> skip_reason に変更し、precheck_ranking_entry() の
#     戻り値など **detail 経由で "reason" キーが来ても衝突しないようにした。
# Ver2.6:
#   - 旧 core/startup/entry_controller_source_prefilter_patch.py が単独で
#     差し替えていた _entry_matches_pipeline の SUMMARY_AI/SUMMARY/PUSH 互換判定を
#     本文へインライン化。
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
# ✔ SUMMARY_AI BUY は5点台候補に合わせる
# ✔ SUMMARY_AI SELL はAI gate側 minScore=1.00 と整合
# ✔ ORDER_ID_EMPTY では30分の銘柄停止にしない
# ✔ production hardened
# ============================================================

from __future__ import annotations

import logging
import os
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

# SUMMARY AI gate 側の実際の閾値と合わせる。
# BUY: runner/entry_gate は強い買いだけを入れるため5.0維持。
# SELL: runner/entry_gate は MIN_ENTRY_SCORE_SELL_SUMMARY=1.0 でAI_OKにしている。
#       ここが5.0のままだと SELL_SCORE_LOW:1.xxx で最終的に全落ちする。
MIN_SUMMARY_SCORE_BUY = 5.0
MIN_SUMMARY_SCORE_SELL = 1.0

MIN_COMPOSITE_SCORE_BUY = 5.0
MIN_COMPOSITE_SCORE_SELL = 1.0

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


_LOG_SKIP_HARD_PRUNE_REASONS = {
    "RANGE_5M_FILTER_NG",
    "ATR_1M_FILTER_NG",
    "ATR_FILTER_NG",
    "ENTRY_ROW_RANGE_NG",
    "DIRECTION_FILTER_NG",
    "FINAL_ENTRY_SAFETY_NG",
    "BOARD_GUARD_NG",
    "FRESH_QUOTE_NG",
    "SELL_CREDIT_GUARD_NG",
    "POSITION_FILTER_NG",
}


def _log_skip_norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        return s
    except Exception:
        return ""


def _log_skip_is_ranking_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    src = _normalize_source(entry.get("source"))
    et = _normalize_source(entry.get("entry_type"))
    mode = _normalize_source(entry.get("ranking_entry_mode"))
    return src == "RANKING" or et == "RANKING" or mode.startswith("RANKING")


def _log_skip_entry_side(entry: Any) -> str:
    if not isinstance(entry, dict):
        return ""
    side = _normalize_source(entry.get("side") or entry.get("entry_decision") or entry.get("ai_side"))
    if side in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if side in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return side


def _prune_ranking_pending_on_hard_reason(symbol: Any, reason: str, *, side: Any = None) -> int:
    if not _env_bool("RANKING_PENDING_PRUNE_ON_FILTER_NG", True):
        return 0
    sym = _log_skip_norm_symbol(symbol)
    side_n = _normalize_source(side)
    try:
        from trading.entry.pending_manager import prune_entries, snapshot_root

        def _pred(s: str, entry: dict) -> bool:
            if sym and _log_skip_norm_symbol(s) != sym:
                return False
            if not _log_skip_is_ranking_entry(entry):
                return False
            if side_n and _log_skip_entry_side(entry) != side_n:
                return False
            return True

        removed = prune_entries(_pred, reason=f"RANKING_FINAL_NG:{reason}")
        if removed:
            logger.warning(
                "[RANKING PENDING CLEANUP] removed=%s symbol=%s side=%s reason=%s root=%s",
                removed,
                sym,
                side_n,
                reason,
                snapshot_root(),
            )
        return int(removed or 0)
    except Exception:
        logger.exception("[RANKING PENDING CLEANUP] failed symbol=%s side=%s reason=%s", sym, side_n, reason)
        return 0


def _log_skip(symbol: str, skip_reason: str = None, **detail):
    # skip_reason という引数名にしているのは、呼び出し元が **detail 経由で
    # "reason" キーを渡すケース（precheck_ranking_entry()の戻り値など）と
    # 衝突して TypeError: multiple values for argument 'reason' になるのを防ぐため。
    if "reason" in detail:
        detail.setdefault("detail_reason", detail.pop("reason"))

    logger.info(
        "⛔ ENTRY_SKIP %s reason=%s detail=%s",
        symbol,
        skip_reason,
        detail,
    )

    try:
        from trading.audit_logging.entry_audit import audit_entry_skip
        audit_entry_skip(symbol=symbol, reason=skip_reason, detail=detail)
    except Exception:
        pass

    try:
        reason_key = str(skip_reason or "").split()[0].strip()
        if reason_key in _LOG_SKIP_HARD_PRUNE_REASONS:
            # 最終評価で落ちたランキング候補は、再評価しても同じ理由で詰まりやすい。
            # max_pending=1 のため即削除して次のランキング候補へ回す。
            _prune_ranking_pending_on_hard_reason(symbol, reason_key, side=detail.get("side"))
    except Exception:
        logger.exception("[RANKING PENDING CLEANUP] _log_skip hook failed symbol=%s reason=%s", symbol, skip_reason)


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
            ps = _normalize_source(pipeline_source)
            es = _normalize_source(entry.get("source"))
            if es != ps:
                # SUMMARY_AI のpipelineはSUMMARY/PUSH(未設定含む)由来の候補も互換として受け付ける。
                # 旧 core/startup/entry_controller_source_prefilter_patch.py の判定をインライン化。
                if not (
                    (ps == "SUMMARY_AI" and es in {"SUMMARY", "PUSH", ""})
                    or (ps == "SUMMARY" and es == "SUMMARY_AI")
                ):
                    return False

        if interval is not None:
            ent_interval = _normalize_interval(entry.get("interval"))
            if ent_interval is not None and ent_interval != int(interval):
                return False

        return True

    except Exception:
        logger.exception("entry pipeline filter failed entry=%s", entry)
        return False


def _env_bool(name: str, default: bool = True) -> bool:
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
        return int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _normalize_entry_for_pipeline(entry: Any, pipeline_source: str | None) -> Any:
    """旧 core/startup/entry_controller_source_prefilter_patch.py の_normalize_entry_for_pipeline。

    SUMMARY_AI 向けpipelineでは、SUMMARY/PUSH(未設定含む)由来のentryのsource/entry_typeを
    SUMMARY_AIへ書き換えて、以降の処理でSUMMARY_AI起因として扱えるようにする。
    """
    if not isinstance(entry, dict):
        return entry
    ps = _normalize_source(pipeline_source)
    if ps != "SUMMARY_AI":
        return entry
    es = _normalize_source(entry.get("source"))
    et = _normalize_source(entry.get("entry_type"))
    if es in {"SUMMARY", "PUSH", ""} or et in {"SUMMARY", "PUSH", ""}:
        e = deepcopy(entry)
        e["source"] = "SUMMARY_AI"
        e["entry_type"] = "SUMMARY_AI"
        try:
            row = e.get("entry_row")
            if isinstance(row, dict):
                row["source"] = "SUMMARY_AI"
                row["entry_type"] = "SUMMARY_AI"
        except Exception:
            pass
        return e
    return entry


def _describe_prefilter_entry(e: Any) -> dict[str, Any]:
    if not isinstance(e, dict):
        return {"type": type(e).__name__}
    return {
        "source": e.get("source"),
        "entry_type": e.get("entry_type"),
        "side": e.get("side"),
        "interval": e.get("interval"),
        "score": e.get("score"),
    }


def _prefilter_entries_for_pipeline(symbol: str, entries: list[dict], *, pipeline_source: str | None, interval: int | None) -> list[dict]:
    """旧 core/startup/entry_controller_source_prefilter_patch.py の_patched_build_scored_candidatesを
    インライン化。entries[:MAX_CANDIDATES_PER_SYMBOL] で切り詰める前に pipeline_source/interval で
    事前フィルタし、SUMMARY_AI向けにsource/entry_typeを正規化する。

    切り詰め前にこれをしないと、pending_rootにSUMMARY/RANKING/TONOSAMAが混在している場合、
    実行元と違う候補が先頭を占有し、正しい候補が後ろにあるのに評価されないことがある。
    """
    if not _env_bool("ENTRY_CONTROLLER_SOURCE_PREFILTER_ENABLED", True):
        return entries
    if not (pipeline_source or interval is not None):
        return entries
    try:
        filtered = [e for e in entries if _entry_matches_pipeline(e, pipeline_source, interval)]
        normalized = [_normalize_entry_for_pipeline(e, pipeline_source) for e in filtered]
        if len(filtered) != len(entries):
            skipped = [_describe_prefilter_entry(e) for e in entries if not _entry_matches_pipeline(e, pipeline_source, interval)][:20]
            logger.warning(
                "[ENTRY SOURCE PREFILTER] symbol=%s source=%s interval=%s before=%s after=%s skipped=%s",
                symbol,
                pipeline_source,
                interval,
                len(entries),
                len(filtered),
                skipped,
            )
        return normalized
    except Exception:
        logger.exception("[ENTRY SOURCE PREFILTER] failed symbol=%s -> use original entries", symbol)
        return entries


def _ma5_third_bar_tfs() -> list[int]:
    raw = os.getenv("ENTRY_MA5_THIRD_BAR_REQUIRED_TFS", "3,5")
    out: list[int] = []
    for x in str(raw).replace(";", ",").split(","):
        try:
            n = int(float(x.strip()))
            if n in (3, 5) and n not in out:
                out.append(n)
        except Exception:
            pass
    return out or [3, 5]


def _ma5_third_bar_row_sources(item: dict[str, Any]):
    try:
        for src in (item, item.get("entry_row"), item.get("entry"), item.get("row"), item.get("raw"), item.get("_raw")):
            if isinstance(src, dict):
                yield src
    except Exception:
        return


def _ma5_third_bar_symbol_from_item(item: dict[str, Any]) -> str:
    try:
        for src in _ma5_third_bar_row_sources(item):
            s = str(src.get("symbol") or "").strip()
            if s.endswith(".0"):
                s = s[:-2]
            if s:
                return s
    except Exception:
        pass
    return ""


def _ma5_third_bar_side_from_item(item: dict[str, Any]) -> str:
    try:
        for src in _ma5_third_bar_row_sources(item):
            s = str(src.get("side") or src.get("entry_decision") or src.get("resolved_side") or "").strip().upper()
            if s in {"BUY", "SELL"}:
                return s
    except Exception:
        pass
    return ""


def _ma5_third_bar_source_from_item(item: dict[str, Any]) -> str:
    keys = ("source", "entry_source", "entry_type", "pipeline_source", "ranking_entry_mode", "ranking_source")
    try:
        vals = []
        for src in _ma5_third_bar_row_sources(item):
            for k in keys:
                v = src.get(k)
                if v:
                    vals.append(str(v).strip().upper())
        return ",".join(vals)
    except Exception:
        pass
    return ""


def _ma5_third_bar_score_from_item(item: dict[str, Any]) -> float:
    keys = (
        "priority",
        "pending_score",
        "score",
        "final_score",
        "display_score",
        "score_total",
        "total_score",
        "ranking_only_score",
        "ranking_strength",
        "snapshot_score",
    )
    try:
        vals = []
        for src in _ma5_third_bar_row_sources(item):
            for k in keys:
                if k in src:
                    vals.append(abs(_safe_float(src.get(k), 0.0)))
        return max(vals, default=0.0)
    except Exception:
        return 0.0


def _ma5_third_bar_strong_failopen_ok(item: dict[str, Any], diag: dict[str, Any]) -> bool:
    if not _env_bool("ENTRY_MA5_THIRD_BAR_STRONG_SCORE_FAILOPEN", True):
        return False

    source = _ma5_third_bar_source_from_item(item)
    score = _ma5_third_bar_score_from_item(item)
    min_score = _env_float(
        "ENTRY_MA5_THIRD_BAR_STRONG_FAILOPEN_MIN_SCORE",
        _env_float("ENTRY_MA5_THIRD_BAR_RANKING_FAILOPEN_MIN_SCORE", 75.0),
    )
    if score < min_score:
        return False

    # 明示的にRANKINGなら通す。sourceが欠落していても high score は通す。
    allow_unknown_source = _env_bool("ENTRY_MA5_THIRD_BAR_STRONG_FAILOPEN_ALLOW_UNKNOWN_SOURCE", True)
    if source and "RANKING" not in source and not _env_bool("ENTRY_MA5_THIRD_BAR_STRONG_FAILOPEN_ALL_SOURCES", False):
        return False
    if not source and not allow_unknown_source:
        return False

    max_bad_slope_abs = _env_float("ENTRY_MA5_THIRD_BAR_RANKING_MAX_BAD_SLOPE_ABS", 999999.0)
    try:
        bad_slopes = []
        for c in diag.get("checks", []) or []:
            if isinstance(c, dict) and c.get("ok") is False:
                bad_slopes.append(abs(_safe_float(c.get("ma5_slope"), 0.0)))
        if bad_slopes and max(bad_slopes) > max_bad_slope_abs:
            return False
    except Exception:
        pass

    logger.warning(
        "[ENTRY MA5 THIRD BAR GUARD] STRONG_SCORE_FAILOPEN symbol=%s side=%s source=%s score=%.3f min_score=%.3f diag=%s",
        diag.get("symbol"),
        diag.get("side"),
        source or "UNKNOWN",
        score,
        min_score,
        diag,
    )
    return True


def _ma5_third_bar_ranking_strong_failopen_ok(item: dict[str, Any], diag: dict[str, Any]) -> bool:
    if not _env_bool("ENTRY_MA5_THIRD_BAR_RANKING_STRONG_FAILOPEN", True):
        return False
    return _ma5_third_bar_strong_failopen_ok(item, diag)


def _ma5_third_bar_latest_rows_for_symbol(tf: int, symbol: str):
    try:
        import pandas as pd
        getter = getattr(global_data, "get_summary_history", None)
        df = getter(tf, source="push") if callable(getter) else None
        if df is None or not isinstance(df, pd.DataFrame) or df.empty or "symbol" not in df.columns:
            return None
        d = df.copy()
        s = d["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
        d = d[s == str(symbol).strip()]
        if d.empty:
            return None
        time_col = next((c for c in ("datetime", "end_time", "time", "start_time") if c in d.columns), None)
        if time_col:
            d["__dt"] = pd.to_datetime(d[time_col], errors="coerce")
            d = d.sort_values("__dt")
        return d.tail(max(3, _env_int("ENTRY_MA5_THIRD_BAR_MIN_BARS", 3)))
    except Exception:
        logger.exception("[ENTRY MA5 THIRD BAR GUARD] get rows failed tf=%s symbol=%s", tf, symbol)
        return None


def _ma5_third_bar_check_tf(tf: int, symbol: str, side: str) -> tuple[bool | None, dict[str, Any]]:
    rows = _ma5_third_bar_latest_rows_for_symbol(tf, symbol)
    if rows is None or getattr(rows, "empty", True):
        return None, {"tf": tf, "reason": "no_history"}
    min_bars = max(3, _env_int("ENTRY_MA5_THIRD_BAR_MIN_BARS", 3))
    if len(rows) < min_bars:
        return None, {"tf": tf, "reason": "not_enough_bars", "rows": len(rows), "need": min_bars}
    if "ma5" not in rows.columns:
        return None, {"tf": tf, "reason": "ma5_missing"}
    price_col = next((c for c in ("close", "close_price", "price", "current_price") if c in rows.columns), "close")
    last3 = rows.tail(3)
    closes = [_safe_float(x, 0.0) for x in list(last3[price_col])]
    ma5s = [_safe_float(x, 0.0) for x in list(last3["ma5"])]
    if any(x <= 0 for x in closes) or any(x <= 0 for x in ma5s):
        return None, {"tf": tf, "reason": "bad_close_or_ma5", "closes": closes, "ma5s": ma5s}
    slope = ma5s[-1] - ma5s[-2]
    if side == "BUY":
        ok = all(c > m for c, m in zip(closes, ma5s)) and slope > 0
    else:
        ok = all(c < m for c, m in zip(closes, ma5s)) and slope < 0
    return bool(ok), {"tf": tf, "side": side, "closes": [round(x, 4) for x in closes], "ma5s": [round(x, 4) for x in ma5s], "ma5_slope": round(float(slope), 6), "ok": bool(ok)}


def _ma5_third_bar_passes_guard(item: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    symbol = _ma5_third_bar_symbol_from_item(item)
    side = _ma5_third_bar_side_from_item(item)
    if not symbol or side not in {"BUY", "SELL"}:
        return True, {"reason": "no_symbol_or_side", "symbol": symbol, "side": side}
    checks = []
    seen = 0
    for tf in _ma5_third_bar_tfs():
        ok, diag = _ma5_third_bar_check_tf(tf, symbol, side)
        checks.append(diag)
        if ok is None:
            continue
        seen += 1
        if not ok:
            out = {"symbol": symbol, "side": side, "checks": checks, "reason": "ma5_third_bar_slope_ng"}
            if _ma5_third_bar_ranking_strong_failopen_ok(item, out):
                out["reason"] = "ma5_third_bar_strong_score_failopen"
                return True, out
            return False, out
    if seen <= 0:
        if _env_bool("ENTRY_MA5_THIRD_BAR_FAIL_OPEN", True):
            return True, {"symbol": symbol, "side": side, "checks": checks, "reason": "no_tf_data_fail_open"}
        return False, {"symbol": symbol, "side": side, "checks": checks, "reason": "no_tf_data"}
    return True, {"symbol": symbol, "side": side, "checks": checks, "reason": "ma5_third_bar_slope_ok"}


def _apply_ma5_third_bar_slope_guard(symbol: str, scored_candidates: list[dict]) -> list[dict]:
    """旧 core/startup/entry_ma5_third_bar_slope_guard_patch.py の_patched_build_scored_candidatesを
    インライン化。3分足・5分足でMA5を超えて(下抜けて)1〜2本目では入らず、3本目でMA5傾きが
    順行している時だけ許可する。strong scoreの候補はsourceが取れなくても単独では落とさない。
    """
    if not _env_bool("ENTRY_MA5_THIRD_BAR_SLOPE_GUARD_ENABLED", True):
        return scored_candidates
    try:
        kept = []
        skipped = []
        for item in list(scored_candidates or []):
            if not isinstance(item, dict):
                kept.append(item)
                continue
            ok, diag = _ma5_third_bar_passes_guard(item)
            if ok:
                kept.append(item)
            else:
                skipped.append(diag)
        if skipped:
            logger.warning("[ENTRY MA5 THIRD BAR GUARD] filtered symbol=%s before=%s after=%s skipped=%s", symbol, len(list(scored_candidates or [])), len(kept), skipped[:30])
        return kept
    except Exception:
        logger.exception("[ENTRY MA5 THIRD BAR GUARD] failed; fail-open symbol=%s", symbol)
        return scored_candidates


def _build_scored_candidates(
    symbol: str,
    entries: list[dict],
    open_position_symbols: set[str],
    boost_active: bool,
    pipeline_source: str | None = None,
    interval: int | None = None,
) -> list[dict]:
    entries = _prefilter_entries_for_pipeline(symbol, entries, pipeline_source=pipeline_source, interval=interval)

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

    scored_candidates = _apply_ma5_third_bar_slope_guard(symbol, scored_candidates)

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
        # lot_sizer が oneshot cap / price / confidence 等で 0 を返した場合、
        # ここで MIN_ENTRY_QTY に戻すと、資金上限を超えた注文を無理に送って
        # 「ENTRY_DISPATCH したが実注文が発火しない」状態を作る。
        logger.warning(
            "⚠ ENTRY_QTY_ZERO_SKIP symbol=%s qty_raw=%s side=%s price=%s -> no order dispatch",
            symbol,
            qty,
            side,
            price,
        )
        _log_skip(symbol, "ENTRY_QTY_ZERO", side=side, price=price, qty_raw=qty)
        return False

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
        # ORDER_ID_EMPTY は、板取得失敗・一時的な API 応答なし・kabu station 側の瞬断でも発生する。
        # ここで30分 trade_restricted にすると、候補が生きていても再試行できず、
        # 「ENTRY_DISPATCH まで行くが発火しない」状態が長く続く。
        # API 429 等の明確な rate limit は send_order 側や global guard 側で別途制御する。
        logger.warning(
            "⚠ ORDER_ID_EMPTY_NO_LONG_RESTRICT symbol=%s side=%s qty=%s order_type=%s price=%s -> retry allowed next cycle",
            symbol,
            side,
            order_qty,
            order_type,
            order_price,
        )
        _log_skip(
            symbol,
            "ORDER_ID_EMPTY_RETRYABLE",
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
