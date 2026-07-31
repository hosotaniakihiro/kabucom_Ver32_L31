# ============================================================
# File   : trading/handlers/entry_controller.py
# Function:
#   - pending_entries に入った候補を最終審査して発注する
#   - AI final gate により全候補を評価し、期待値順にランキングする
#   - BUY / SELL 候補を比較し、priority の高い銘柄から発注する
#   - market / risk / AI health / index shock / credit / volatility
#     などの各種ガードを通過した銘柄のみエントリーする
# ------------------------------------------------------------
# Version: Ver2.8-INLINE-RUN-ENTRY-PIPELINE-LOCK-WAIT-AND-DIRECTION-GUARD
# ------------------------------------------------------------
# Ver2.8:
#   - run_entry_pipeline() に2本の monkeypatch を統合本文化:
#     (1) core/startup/entry_controller_pipeline_lock_wait_patch.py の
#         RANKING/TONOSAMA/SUMMARY lock-wait + stale-lock-reset + 古いpending prune
#     (2) core/startup/summary_ai_entry_controller_bridge_patch.py の
#         run_entry_pipeline 部分 (SUMMARY専用の長め lock-wait 35秒、戻り値の
#         厳密な実発注判定への正規化、no-order時の1回リトライ)
#     両パッチとも from-import (summary_entry.py 等) 経由で run_entry_pipeline を
#     直接参照するモジュールへは効かない「別名参照バイパス」問題があったが、
#     本体へインライン化したことで解消。
#   - _build_scored_candidates に core/startup/ranking_direction_entry_guard_patch.py
#     の「ランキング方向逆張り禁止」ガードを統合。旧パッチは
#     _passes_side_filter 等の存在しない関数への差し込みを想定しており、
#     フォールバック先の run_entry_pipeline ラップも存在しない "entries" 引数を
#     探すだけで実際には一度もガードが適用されていなかった (デッドコード)。
#     side確定後の正しい位置に差し込み、RANKING由来候補にのみ実際に機能させた。
#   - core/startup/entry_controller_pipeline_bucket_filter_patch.py と
#     entry_pipeline_pending_root_prefilter_patch.py (pipeline_source/interval
#     不一致のpendingを事前に間引く2つの重複した性能最適化) は、
#     get_bucket() 直後の軽量フィルタへ一本化した。正しさ自体は既存の
#     _prefilter_entries_for_pipeline (_build_scored_candidates 内) が
#     保証しているため、ここでの絞り込みは性能目的のみ。
#
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
import queue
import sqlite3
import threading
import time
from pathlib import Path
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


# ==========================================================
# ランキング方向逆張り禁止ガード
# (旧 core/startup/ranking_direction_entry_guard_patch.py)
#
# 元のパッチは _passes_side_filter 等の候補フィルタ関数への差し込みを想定していたが、
# entry_controller.py にはそれらの関数が存在せず、フォールバック先の
# run_entry_pipeline ラップも "entries" という存在しない引数を探すだけで
# 実際には一度もガードが適用されていなかった (デッドコード)。ここで
# _build_scored_candidates の side 確定後に正しく差し込み、実際に機能させる。
# ==========================================================

def _ranking_direction_contains_any(text: str, words: tuple[str, ...]) -> bool:
    t = str(text or "").lower()
    return any(w.lower() in t for w in words)


def _infer_ranking_direction(row: dict) -> tuple[str, str]:
    """Returns (UP|DOWN|UNKNOWN, reason)."""
    text_keys = ("ranking_type", "rank_type", "ranking_name", "source", "entry_source", "market", "reason", "category", "ranking_category")
    text = " ".join(str(row.get(k, "")) for k in text_keys if row.get(k) is not None)

    down_words = ("下落", "値下", "値下がり", "decline", "decliner", "down", "fall", "drop", "minus", "negative", "loser", "sell", "short")
    up_words = ("上昇", "値上", "値上がり", "rise", "riser", "up", "gain", "gainer", "plus", "positive", "buy", "long")

    if _ranking_direction_contains_any(text, down_words):
        return "DOWN", f"text_down:{text}"
    if _ranking_direction_contains_any(text, up_words):
        return "UP", f"text_up:{text}"

    for k in ("change_rate", "change_pct", "rate", "騰落率", "price_change_rate", "ranking_change_rate"):
        if k in row:
            x = _safe_float(row.get(k), 0.0)
            if x <= -0.1:
                return "DOWN", f"{k}={x}"
            if x >= 0.1:
                return "UP", f"{k}={x}"

    score_buy = _safe_float(row.get("score_buy"), 0.0)
    score_sell = _safe_float(row.get("score_sell"), 0.0)
    score_total = _safe_float(row.get("score_total", row.get("final_score", row.get("score", 0.0))), 0.0)

    if score_sell >= max(1.0, score_buy + 0.5):
        return "DOWN", f"score_sell_dominant sell={score_sell:.2f} buy={score_buy:.2f} total={score_total:.2f}"
    if score_buy >= max(1.0, score_sell + 0.5):
        return "UP", f"score_buy_dominant buy={score_buy:.2f} sell={score_sell:.2f} total={score_total:.2f}"

    if score_total <= -1.0:
        return "DOWN", f"score_total_negative={score_total:.2f}"
    if score_total >= 1.0:
        return "UP", f"score_total_positive={score_total:.2f}"

    return "UNKNOWN", "no_direction_signal"


def _ranking_direction_guard_ok(entry_row: dict, side: str) -> tuple[bool, dict]:
    if not _env_bool("RANKING_DIRECTION_GUARD_ENABLED", True):
        return True, {}
    # RANKING由来の候補のみに適用する。SUMMARY_AI/TONOSAMA等の候補は
    # score_buy/score_sell等の汎用フィールドを持つことがあり、無条件適用すると
    # ranking以外にも direction 判定が誤爆しかねないため対象を絞る。
    is_ranking = (
        _normalize_source(entry_row.get("source")) == "RANKING"
        or _normalize_source(entry_row.get("entry_type")) == "RANKING"
        or entry_row.get("rank_type") is not None
        or entry_row.get("ranking_type") is not None
    )
    if not is_ranking:
        return True, {}
    direction, reason = _infer_ranking_direction(entry_row)
    detail = {"direction": direction, "reason": reason}
    if direction == "DOWN" and side == "BUY":
        detail["block_reason"] = "RANKING_DIRECTION_BUY_AGAINST_DOWN"
        return False, detail
    if direction == "UP" and side == "SELL":
        detail["block_reason"] = "RANKING_DIRECTION_SELL_AGAINST_UP"
        return False, detail
    return True, detail


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

            direction_ok, direction_detail = _ranking_direction_guard_ok(entry_row, side)
            if not direction_ok:
                _log_skip(symbol, direction_detail.get("block_reason", "RANKING_DIRECTION_NG"), side=side, direction=direction_detail.get("direction"), reason=direction_detail.get("reason"))
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


# ==========================================================
# _execute_best_candidate: 9本の monkeypatch を統合本文化
# (旧 core/startup/entry_controller_runtime_reject_patch.py [kabu APIエラー診断] +
#  core/startup/entry_daily_risk_runtime_patch.py [日次リスクガード] +
#  core/startup/final_entry_safety_guard_patch.py [時間帯/流動性/反転/板ガード] +
#  core/startup/final_entry_liquidity_movement_hard_guard_patch.py [出来高/値動きハードガード] +
#  core/startup/entry_liquidity_runtime_patch.py [直近summary DB流動性ガード] +
#  core/startup/final_entry_duplicate_cooldown_patch.py [重複発注クールダウン] +
#  core/startup/entry_execute_timeout_guard_patch.py [経過候補ガード+タイムアウト実行] +
#  trading/audit_logging/entry_controller_audit_patch.py [監査ログ] +
#  core/startup/entry_summary_retry_rotation_runtime_patch.py [未約定キャンセル後の再queue用item記憶])
#
# 発見した不具合と対応:
#   - entry_daily_risk_runtime_patch は final_entry_safety_guard_patch の
#     _unwrap_true_original が汎用の "_original" 属性を無条件に辿って
#     しまうバグにより、両パッチとも main.py の起動順で毎回確実に
#     日次リスクガードだけが読み飛ばされ、一度も機能していなかった
#     (銘柄別上限・日次損失上限・連敗停止が常に無効)。本文化で修復し有効化する。
#   - final_entry_duplicate_cooldown_patch はどこからも import されておらず
#     完全なデッドコードだった。本文化して実際に機能させる。
#   - entry_summary_retry_rotation_runtime_patch は対象の item を
#     threading.local() 経由で受け渡していたが、entry_execute_timeout_guard_patch
#     が別スレッドで発注本体を実行するため、そのスレッドからは空に見え
#     再queue用の記憶が機能しないケースがあった。本文化で item を直接渡す形にして解消。
# ==========================================================

# ---- 共有ヘルパー ----


def _guard_row_to_dict(v: Any) -> dict:
    try:
        if v is None:
            return {}
        if isinstance(v, dict):
            return v
        if hasattr(v, "to_dict"):
            d = v.to_dict()
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _guard_norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _guard_first(row: dict, keys: tuple[str, ...], default=None):
    for k in keys:
        try:
            v = row.get(k)
            if v is not None and str(v).strip() != "":
                return v
        except Exception:
            pass
    return default


# ---- 日次リスクガード (旧 entry_daily_risk_runtime_patch.py V1.7) ----

_DAILY_RISK_DB_SCHEMA_READY: set[str] = set()


def _daily_risk_today() -> str:
    return dt.datetime.now().strftime("%Y%m%d")


def _daily_risk_now_iso() -> str:
    return dt.datetime.now().replace(microsecond=0).isoformat(sep=" ")


def _daily_risk_db_path() -> str:
    base = os.getenv(
        "TRADE_GUARD_DB_DIR",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\trade_guard",
    )
    return os.getenv("TRADE_GUARD_DB_PATH", str(Path(base) / f"trade_guard{_daily_risk_today()}.db"))


def _daily_risk_ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS symbol_daily_entry_risk (
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            entry_count INTEGER NOT NULL DEFAULT 0,
            entry_sent_count INTEGER NOT NULL DEFAULT 0,
            daily_pnl REAL NOT NULL DEFAULT 0,
            win_count INTEGER NOT NULL DEFAULT 0,
            loss_count INTEGER NOT NULL DEFAULT 0,
            last_entry_time TEXT DEFAULT '',
            last_exit_time TEXT DEFAULT '',
            updated_at TEXT DEFAULT '',
            PRIMARY KEY (trade_date, symbol)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS global_daily_entry_risk (
            trade_date TEXT PRIMARY KEY,
            trade_count INTEGER NOT NULL DEFAULT 0,
            entry_sent_count INTEGER NOT NULL DEFAULT 0,
            daily_pnl REAL NOT NULL DEFAULT 0,
            win_count INTEGER NOT NULL DEFAULT 0,
            loss_count INTEGER NOT NULL DEFAULT 0,
            consecutive_losses INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT DEFAULT ''
        )
        """
    )
    for ddl in (
        "ALTER TABLE symbol_daily_entry_risk ADD COLUMN win_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE symbol_daily_entry_risk ADD COLUMN loss_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE symbol_daily_entry_risk ADD COLUMN entry_sent_count INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE global_daily_entry_risk ADD COLUMN entry_sent_count INTEGER NOT NULL DEFAULT 0",
    ):
        try:
            conn.execute(ddl)
        except sqlite3.OperationalError:
            pass
    conn.commit()


def _daily_risk_connect() -> sqlite3.Connection:
    path = _daily_risk_db_path()
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    conn = sqlite3.connect(path, timeout=3.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=3000")
    if path not in _DAILY_RISK_DB_SCHEMA_READY:
        _daily_risk_ensure_schema(conn)
        _DAILY_RISK_DB_SCHEMA_READY.add(path)
    return conn


def _daily_risk_get_symbol_row(symbol: str) -> dict[str, Any]:
    symbol = normalize_symbol(symbol)
    with _daily_risk_connect() as conn:
        cur = conn.execute(
            """
            SELECT entry_count, entry_sent_count, daily_pnl, win_count, loss_count, last_entry_time, last_exit_time
            FROM symbol_daily_entry_risk
            WHERE trade_date=? AND symbol=?
            """,
            (_daily_risk_today(), symbol),
        )
        row = cur.fetchone()
        if row:
            return {
                "entry_count": int(row[0] or 0),
                "entry_sent_count": int(row[1] or 0),
                "daily_pnl": float(row[2] or 0.0),
                "win_count": int(row[3] or 0),
                "loss_count": int(row[4] or 0),
                "last_entry_time": row[5] or "",
                "last_exit_time": row[6] or "",
            }
    return {"entry_count": 0, "entry_sent_count": 0, "daily_pnl": 0.0, "win_count": 0, "loss_count": 0, "last_entry_time": "", "last_exit_time": ""}


def _daily_risk_get_global_row() -> dict[str, Any]:
    with _daily_risk_connect() as conn:
        cur = conn.execute(
            """
            SELECT trade_count, entry_sent_count, daily_pnl, win_count, loss_count, consecutive_losses, updated_at
            FROM global_daily_entry_risk
            WHERE trade_date=?
            """,
            (_daily_risk_today(),),
        )
        row = cur.fetchone()
        if row:
            return {
                "trade_count": int(row[0] or 0),
                "entry_sent_count": int(row[1] or 0),
                "daily_pnl": float(row[2] or 0.0),
                "win_count": int(row[3] or 0),
                "loss_count": int(row[4] or 0),
                "consecutive_losses": int(row[5] or 0),
                "updated_at": row[6] or "",
            }
    return {"trade_count": 0, "entry_sent_count": 0, "daily_pnl": 0.0, "win_count": 0, "loss_count": 0, "consecutive_losses": 0, "updated_at": ""}


def _daily_risk_record_entry_sent(symbol: str) -> None:
    """発注成功時点で、同一銘柄の当日再エントリー抑止用カウントを増やす。"""
    symbol = normalize_symbol(symbol)
    if not symbol:
        return
    with _daily_risk_connect() as conn:
        conn.execute(
            """
            INSERT INTO symbol_daily_entry_risk
                (trade_date, symbol, entry_count, entry_sent_count, daily_pnl, win_count, loss_count, last_entry_time, updated_at)
            VALUES (?, ?, 0, 1, 0, 0, 0, ?, ?)
            ON CONFLICT(trade_date, symbol) DO UPDATE SET
                entry_sent_count = entry_sent_count + 1,
                last_entry_time = excluded.last_entry_time,
                updated_at = excluded.updated_at
            """,
            (_daily_risk_today(), symbol, _daily_risk_now_iso(), _daily_risk_now_iso()),
        )
        conn.execute(
            """
            INSERT INTO global_daily_entry_risk
                (trade_date, trade_count, entry_sent_count, daily_pnl, win_count, loss_count, consecutive_losses, updated_at)
            VALUES (?, 0, 1, 0, 0, 0, 0, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                entry_sent_count = entry_sent_count + 1,
                updated_at = excluded.updated_at
            """,
            (_daily_risk_today(), _daily_risk_now_iso()),
        )
        conn.commit()
    logger.info("[ENTRY DAILY RISK] entry_sent recorded symbol=%s", symbol)


def _daily_risk_record_actual_trade(symbol: str, pnl: float) -> None:
    """実際に約定して返済された時、当日実現損益/勝敗を更新する。trading/exit/symbol_trade_guard.py の
    record_exit_event から呼ばれる。"""
    symbol = normalize_symbol(symbol)
    if not symbol:
        return
    pnl_f = float(pnl or 0.0)
    is_win = 1 if pnl_f > 0 else 0
    is_loss = 1 if pnl_f < 0 else 0
    new_consecutive_losses_expr = "consecutive_losses + 1" if is_loss else "0"
    with _daily_risk_connect() as conn:
        conn.execute(
            """
            INSERT INTO symbol_daily_entry_risk
                (trade_date, symbol, entry_count, entry_sent_count, daily_pnl, win_count, loss_count, last_entry_time, last_exit_time, updated_at)
            VALUES (?, ?, 1, 0, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date, symbol) DO UPDATE SET
                entry_count = entry_count + 1,
                daily_pnl = daily_pnl + excluded.daily_pnl,
                win_count = win_count + excluded.win_count,
                loss_count = loss_count + excluded.loss_count,
                last_exit_time = excluded.last_exit_time,
                updated_at = excluded.updated_at
            """,
            (_daily_risk_today(), symbol, pnl_f, is_win, is_loss, _daily_risk_now_iso(), _daily_risk_now_iso(), _daily_risk_now_iso()),
        )
        conn.execute(
            """
            INSERT INTO global_daily_entry_risk
                (trade_date, trade_count, entry_sent_count, daily_pnl, win_count, loss_count, consecutive_losses, updated_at)
            VALUES (?, 1, 0, ?, ?, ?, ?, ?)
            ON CONFLICT(trade_date) DO UPDATE SET
                trade_count = trade_count + 1,
                daily_pnl = daily_pnl + excluded.daily_pnl,
                win_count = win_count + excluded.win_count,
                loss_count = loss_count + excluded.loss_count,
                consecutive_losses = """ + new_consecutive_losses_expr + """,
                updated_at = excluded.updated_at
            """,
            (_daily_risk_today(), pnl_f, is_win, is_loss, 1 if is_loss else 0, _daily_risk_now_iso()),
        )
        conn.commit()
    logger.info("[ENTRY DAILY RISK] actual_trade recorded symbol=%s pnl=%s", symbol, pnl_f)


def _daily_risk_is_winning_symbol(srow: dict[str, Any]) -> bool:
    if not _env_bool("ENTRY_WINNING_SYMBOL_REENTRY_ENABLED", True):
        return False
    win_count = int(srow.get("win_count") or 0)
    loss_count = int(srow.get("loss_count") or 0)
    daily_pnl = float(srow.get("daily_pnl") or 0.0)
    min_pnl = _env_float("ENTRY_WINNING_SYMBOL_MIN_DAILY_PNL", 1.0)
    if daily_pnl < min_pnl:
        return False
    if _env_bool("ENTRY_WINNING_SYMBOL_REQUIRE_WIN_GT_LOSS", True):
        return win_count > loss_count
    return win_count >= 1


def _daily_risk_block_reason(symbol: str, side: str) -> tuple[bool, str, dict[str, Any]]:
    if not _env_bool("ENTRY_DAILY_RISK_GUARD_ENABLED", True):
        return False, "", {}
    symbol = normalize_symbol(symbol)
    side_u = str(side or "").upper()
    if side_u == "BUY" and not _env_bool("ENTRY_BUY_ENABLED", True):
        return True, "BUY_DISABLED_BY_DAILY_RISK_PATCH", {"symbol": symbol, "side": side_u}

    try:
        srow = _daily_risk_get_symbol_row(symbol)
        grow = _daily_risk_get_global_row()
    except Exception:
        logger.exception("[ENTRY DAILY RISK] db read failed symbol=%s -> fail-open", symbol)
        return False, "", {}

    max_entries = _env_int("ENTRY_MAX_DAILY_ENTRIES_PER_SYMBOL", 2)
    winning_max_entries = _env_int("ENTRY_WINNING_SYMBOL_MAX_DAILY_ENTRIES", 4)
    symbol_max_loss = _env_float("ENTRY_SYMBOL_MAX_DAILY_LOSS_YEN", -1500.0)
    stop_after_first_loss = _env_bool("ENTRY_STOP_SYMBOL_AFTER_FIRST_LOSS", True)
    stop_after_first_loss_only_net_negative = _env_bool("ENTRY_STOP_AFTER_FIRST_LOSS_ONLY_IF_NET_NEGATIVE", True)
    global_max_loss = _env_float("ENTRY_GLOBAL_MAX_DAILY_LOSS_YEN", -50000.0)
    global_max_trades = _env_int("ENTRY_GLOBAL_MAX_DAILY_TRADES", 30)
    global_max_consec_losses = _env_int("ENTRY_GLOBAL_MAX_CONSECUTIVE_LOSSES", 20)

    entry_count = int(srow.get("entry_count") or 0)
    entry_sent_count = int(srow.get("entry_sent_count") or 0)
    loss_count = int(srow.get("loss_count") or 0)
    daily_pnl = float(srow.get("daily_pnl") or 0.0)
    winning_symbol = _daily_risk_is_winning_symbol(srow)

    if winning_symbol and _env_bool("ENTRY_WINNING_SYMBOL_IGNORE_SENT_ONLY", True):
        symbol_seen_entries = entry_count
    else:
        symbol_seen_entries = max(entry_count, entry_sent_count)

    effective_max_entries = max_entries
    if winning_symbol:
        effective_max_entries = max(max_entries, winning_max_entries)

    if global_max_trades > 0 and int(grow.get("trade_count") or 0) >= global_max_trades:
        return True, "GLOBAL_DAILY_TRADE_LIMIT", {"symbol": symbol, "side": side_u, "max_trades": global_max_trades, **grow}

    if float(grow.get("daily_pnl") or 0.0) <= global_max_loss:
        return True, "GLOBAL_DAILY_LOSS_LIMIT", {"symbol": symbol, "side": side_u, "max_loss": global_max_loss, **grow}

    if global_max_consec_losses > 0 and int(grow.get("consecutive_losses") or 0) >= global_max_consec_losses:
        return True, "GLOBAL_CONSECUTIVE_LOSS_LIMIT", {"symbol": symbol, "side": side_u, "max_consecutive_losses": global_max_consec_losses, **grow}

    if stop_after_first_loss and loss_count >= 1:
        if not (stop_after_first_loss_only_net_negative and winning_symbol and daily_pnl > 0):
            return True, "SYMBOL_STOP_AFTER_FIRST_LOSS", {"symbol": symbol, "side": side_u, "winning_symbol": winning_symbol, **srow}

    if float(srow.get("daily_pnl") or 0.0) <= symbol_max_loss:
        return True, "SYMBOL_DAILY_LOSS_LIMIT", {"symbol": symbol, "side": side_u, "max_loss": symbol_max_loss, **srow}

    if effective_max_entries > 0 and symbol_seen_entries >= effective_max_entries:
        return True, "SYMBOL_DAILY_ENTRY_LIMIT", {
            "symbol": symbol,
            "side": side_u,
            "max_entries": effective_max_entries,
            "winning_symbol": winning_symbol,
            "symbol_seen_entries": symbol_seen_entries,
            **srow,
        }

    return False, "", {}


# ---- 時間帯/流動性/反転/板ガード (旧 final_entry_safety_guard_patch.py V12) ----

_FINAL_GUARD_BOARD_COOLDOWN_UNTIL = 0.0
_FINAL_GUARD_BOARD_CACHE: dict[str, tuple[float, tuple[float, float, float, float]]] = {}


def _final_guard_parse_hhmm(s: str, default_h: int, default_m: int) -> tuple[int, int]:
    try:
        hh, mm = str(s).strip().split(":", 1)
        return int(hh), int(mm)
    except Exception:
        return default_h, default_m


def _final_guard_entry_time_ok(symbol: str, side: str) -> bool:
    if not _env_bool("ENTRY_TIME_GUARD_ENABLED", True):
        return True
    now = dt.datetime.now().time()
    bh, bm = _final_guard_parse_hhmm(os.getenv("ENTRY_NO_NEW_BEFORE", "09:05"), 9, 5)
    ah, am = _final_guard_parse_hhmm(os.getenv("ENTRY_NO_NEW_AFTER", "15:20"), 15, 20)
    if now < dt.time(bh, bm) or now >= dt.time(ah, am):
        return False
    if _env_bool("ENTRY_LUNCH_GUARD_ENABLED", True):
        sh, sm = _final_guard_parse_hhmm(os.getenv("ENTRY_LUNCH_BLOCK_START", "11:30"), 11, 30)
        eh, em = _final_guard_parse_hhmm(os.getenv("ENTRY_LUNCH_BLOCK_END", "12:30"), 12, 30)
        if dt.time(sh, sm) <= now < dt.time(eh, em):
            return False
    return True


def _final_guard_liquidity_ok(row: dict, symbol: str, side: str) -> bool:
    if not _env_bool("ENTRY_FINAL_LIQUIDITY_GUARD_ENABLED", True):
        return True
    close = _safe_float(_guard_first(row, ("close", "close_price", "price", "current_price"), 0.0), 0.0)
    volume = _safe_float(_guard_first(row, ("volume", "Volume", "出来高"), 0.0), 0.0)
    turnover = _safe_float(_guard_first(row, ("turnover", "trading_value", "売買代金"), 0.0), 0.0)
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume
    min_volume = _env_float("ENTRY_MIN_VOLUME", 30000.0)
    min_turnover = _env_float("ENTRY_MIN_TURNOVER", 10000000.0)
    if volume <= 0 or volume < min_volume or turnover < min_turnover:
        return False
    return True


def _final_guard_recent_reverse_ok(row: dict, symbol: str, side: str) -> bool:
    if not _env_bool("ENTRY_RECENT_REVERSE_GUARD_ENABLED", True):
        return True
    slope = _safe_float(_guard_first(row, ("slope_5s", "recent_slope", "slope_atr_scaled", "score_slope", "slope"), 0.0), 0.0)
    max_bad_slope = _env_float("ENTRY_RECENT_REVERSE_MAX_BAD_SLOPE", 0.12)
    if side == "BUY" and slope <= -max_bad_slope:
        return False
    if side == "SELL" and slope >= max_bad_slope:
        return False
    return True


def _final_guard_extract_bid_ask(row: dict) -> tuple[float, float, float, float]:
    bid = _safe_float(_guard_first(row, ("bid", "best_bid", "BidPrice", "bid_price"), 0.0), 0.0)
    ask = _safe_float(_guard_first(row, ("ask", "best_ask", "AskPrice", "ask_price"), 0.0), 0.0)
    bid_qty = _safe_float(_guard_first(row, ("bid_qty", "best_bid_qty", "BidQty", "bid_volume"), 0.0), 0.0)
    ask_qty = _safe_float(_guard_first(row, ("ask_qty", "best_ask_qty", "AskQty", "ask_volume"), 0.0), 0.0)
    return bid, ask, bid_qty, ask_qty


def _final_guard_try_get_bid_ask_from_api(symbol: str) -> tuple[float, float, float, float]:
    global _FINAL_GUARD_BOARD_COOLDOWN_UNTIL
    if not _env_bool("ENTRY_BOARD_API_LOOKUP_ENABLED", False):
        return 0.0, 0.0, 0.0, 0.0
    now = time.time()
    if now < _FINAL_GUARD_BOARD_COOLDOWN_UNTIL:
        return 0.0, 0.0, 0.0, 0.0
    ttl = max(0.1, _env_float("ENTRY_BOARD_API_CACHE_TTL_SEC", 2.0))
    cached = _FINAL_GUARD_BOARD_CACHE.get(symbol)
    if cached and now - cached[0] <= ttl:
        return cached[1]
    try:
        from utils_common import get_latest_bid_ask

        res = get_latest_bid_ask(symbol)
        bid = ask = bid_qty = ask_qty = 0.0
        if isinstance(res, dict):
            bid = _safe_float(res.get("bid") or res.get("best_bid") or res.get("BidPrice") or res.get("bid_price"), 0.0)
            ask = _safe_float(res.get("ask") or res.get("best_ask") or res.get("AskPrice") or res.get("ask_price"), 0.0)
            bid_qty = _safe_float(res.get("bid_qty") or res.get("BidQty") or res.get("bid_volume"), 0.0)
            ask_qty = _safe_float(res.get("ask_qty") or res.get("AskQty") or res.get("ask_volume"), 0.0)
        elif isinstance(res, (list, tuple)) and len(res) >= 2:
            bid, ask = _safe_float(res[0], 0.0), _safe_float(res[1], 0.0)
            if len(res) >= 4:
                bid_qty, ask_qty = _safe_float(res[2], 0.0), _safe_float(res[3], 0.0)
        if bid > 0 and ask > 0:
            _FINAL_GUARD_BOARD_CACHE[symbol] = (now, (bid, ask, bid_qty, ask_qty))
            return bid, ask, bid_qty, ask_qty
    except Exception as e:
        msg = repr(e)
        if "429" in msg or "4001006" in msg or "4002006" in msg or "API実行回数" in msg or "レジスト数" in msg:
            _FINAL_GUARD_BOARD_COOLDOWN_UNTIL = time.time() + max(10.0, _env_float("ENTRY_BOARD_API_ERROR_COOLDOWN_SEC", 60.0))
        logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_API_NG symbol=%s error=%s", symbol, msg)
    return 0.0, 0.0, 0.0, 0.0


def _final_guard_board_missing_fallback_ok(row: dict, symbol: str, side: str) -> bool:
    # 緩和しない: ENTRY_ALLOW_ENTRY_WITHOUT_BOARD=1 を明示した場合だけ許可。
    if not _env_bool("ENTRY_ALLOW_ENTRY_WITHOUT_BOARD", False):
        return False
    close = _safe_float(_guard_first(row, ("close", "close_price", "price", "current_price"), 0.0), 0.0)
    volume = _safe_float(_guard_first(row, ("volume", "Volume", "出来高"), 0.0), 0.0)
    turnover = _safe_float(_guard_first(row, ("turnover", "trading_value", "売買代金"), 0.0), 0.0)
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume
    score = abs(_safe_float(_guard_first(row, ("score", "score_total", "final_score", "display_score", "score_sell", "score_buy"), 0.0), 0.0))
    if close < _env_float("ENTRY_ALLOW_WITHOUT_BOARD_MIN_PRICE", 200.0):
        return False
    if volume < _env_float("ENTRY_ALLOW_WITHOUT_BOARD_MIN_VOLUME", 30000.0):
        return False
    if turnover < _env_float("ENTRY_ALLOW_WITHOUT_BOARD_MIN_TURNOVER", 10000000.0):
        return False
    if score < _env_float("ENTRY_ALLOW_WITHOUT_BOARD_MIN_SCORE", 0.90):
        return False
    return True


def _final_guard_board_ok(row: dict, item: dict, symbol: str, side: str) -> bool:
    if not _env_bool("ENTRY_BOARD_GUARD_ENABLED", True):
        return True
    bid, ask, bid_qty, ask_qty = _final_guard_extract_bid_ask(row)
    if bid <= 0 or ask <= 0:
        bid2, ask2, bidq2, askq2 = _final_guard_try_get_bid_ask_from_api(symbol)
        bid, ask, bid_qty, ask_qty = bid or bid2, ask or ask2, bid_qty or bidq2, ask_qty or askq2
    if bid <= 0 or ask <= 0:
        if _final_guard_board_missing_fallback_ok(row, symbol, side):
            return True
        logger.warning("[FINAL ENTRY SAFETY GUARD] BOARD_MISSING symbol=%s side=%s bid=%s ask=%s", symbol, side, bid, ask)
        return False
    mid = (bid + ask) / 2.0
    spread_pct = ((ask - bid) / mid) * 100.0 if mid > 0 else 999.0
    if spread_pct > _env_float("ENTRY_MAX_SPREAD_PCT", 0.20):
        logger.warning("[FINAL ENTRY SAFETY GUARD] SPREAD_TOO_WIDE symbol=%s side=%s spread_pct=%.4f", symbol, side, spread_pct)
        return False
    try:
        row.update({"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty})
        if isinstance(item.get("entry_row"), dict):
            item["entry_row"].update({"bid": bid, "ask": ask, "bid_qty": bid_qty, "ask_qty": ask_qty})
    except Exception:
        pass
    return True


# ---- 出来高/値動きハードガード (旧 final_entry_liquidity_movement_hard_guard_patch.py V5) ----


def _hard_guard_copy_nested(prefix: str, src: Any, dst: dict) -> None:
    try:
        if hasattr(src, "to_dict"):
            src = src.to_dict()
        if not isinstance(src, dict):
            return
        for k, v in src.items():
            if k not in dst or dst.get(k) in (None, ""):
                dst[k] = v
            if isinstance(v, dict):
                _hard_guard_copy_nested(f"{prefix}_{k}", v, dst)
    except Exception:
        pass


def _hard_guard_merge_item_row(item: dict) -> dict:
    row: dict = {}
    try:
        row.update(_guard_row_to_dict(item.get("entry_row")))
        _hard_guard_copy_nested("entry_alias", item.get("entry"), row)
        _hard_guard_copy_nested("ai_alias", item.get("ai"), row)
        for k, v in item.items():
            if k not in row or row.get(k) in (None, ""):
                row[k] = v
    except Exception:
        pass
    return row


def _hard_guard_range_pct(row: dict, close: float) -> float:
    high = _safe_float(_guard_first(row, ("high", "high_price", "HighPrice"), 0.0), 0.0)
    low = _safe_float(_guard_first(row, ("low", "low_price", "LowPrice"), 0.0), 0.0)
    if close > 0 and high > 0 and low > 0 and high >= low:
        return (high - low) / close
    raw = _safe_float(_guard_first(row, ("_intrabar_range_pct", "intrabar_range_pct", "range_pct", "price_range_pct", "range_1m_pct", "range_3m_pct", "range_5m_pct", "disp_range_pct"), 0.0), 0.0)
    if raw > 1.0:
        raw = raw / 100.0
    return max(0.0, raw)


def _hard_guard_abs_score(row: dict, side: str) -> float:
    side_u = _guard_norm_side(side)
    if side_u == "BUY":
        keys = ("score_buy", "buy_score", "ai_buy_score", "priority_score", "priority", "confidence")
    elif side_u == "SELL":
        keys = ("score_sell", "sell_score", "ai_sell_score", "priority_score", "priority", "confidence")
    else:
        keys = ("score", "score_total", "final_score", "display_score", "priority_score", "priority", "confidence")
    for k in keys:
        v = _safe_float(row.get(k), 0.0)
        if v:
            return abs(v)
    return 0.0


def _hard_guard_is_summary_ai(row: dict) -> bool:
    text = " ".join(str(row.get(k) or "") for k in ("source", "pipeline_source", "entry_type", "strategy", "model", "model_used", "reason", "ai_reason")).upper()
    return "SUMMARY_AI" in text or "SUMMARY" in text or "MTF" in text


def _hard_guard_summary_ai_low_movement_rescue(row: dict, *, symbol: str, side: str, volume: float, turnover: float, close: float) -> bool:
    if not _env_bool("ENTRY_HARD_SUMMARY_AI_LOW_MOVEMENT_RESCUE", True):
        return False
    if not _hard_guard_is_summary_ai(row):
        return False
    score = _hard_guard_abs_score(row, side)
    min_score = _env_float("ENTRY_HARD_SUMMARY_AI_RESCUE_MIN_SCORE", 3.0)
    min_volume = _env_float("ENTRY_HARD_SUMMARY_AI_RESCUE_MIN_VOLUME", _env_float("ENTRY_HARD_MIN_VOLUME", 30000.0))
    min_turnover = _env_float("ENTRY_HARD_SUMMARY_AI_RESCUE_MIN_TURNOVER", _env_float("ENTRY_HARD_MIN_TURNOVER", 10000000.0))
    if score >= min_score and volume >= min_volume and turnover >= min_turnover and close > 0:
        logger.warning("[ENTRY HARD GUARD] SUMMARY_AI_LOW_MOVEMENT_RESCUE symbol=%s side=%s score=%.3f", symbol, side, score)
        return True
    return False


def _hard_guard_ok(item: dict) -> bool:
    if not _env_bool("ENTRY_HARD_LIQUIDITY_MOVEMENT_GUARD_ENABLED", True):
        return True
    row = _hard_guard_merge_item_row(item)
    symbol = normalize_symbol(_guard_first(row, ("symbol", "Symbol", "code", "銘柄コード"), ""))
    side = _guard_norm_side(_guard_first(row, ("side", "entry_decision", "ai_side"), ""))
    close = _safe_float(_guard_first(row, ("close", "close_price", "price", "current_price"), 0.0), 0.0)

    volume = _safe_float(_guard_first(row, ("volume", "Volume", "出来高", "day_volume", "acc_volume", "trading_volume", "_latest_volume", "latest_volume", "recent_volume", "recent_volume_1m", "recent_volume_3m", "recent_volume_5m", "display_volume_1m", "volume_1m", "volume_3m", "volume_5m"), 0.0), 0.0)
    turnover = _safe_float(_guard_first(row, ("turnover", "trading_value", "売買代金", "day_turnover", "acc_turnover", "turnover_value"), 0.0), 0.0)
    if turnover <= 0 and close > 0 and volume > 0:
        turnover = close * volume
    if volume <= 0 and close > 0 and turnover > 0:
        volume = turnover / close

    min_volume = _env_float("ENTRY_HARD_MIN_VOLUME", 30000.0)
    min_turnover = _env_float("ENTRY_HARD_MIN_TURNOVER", 10000000.0)
    if volume <= 0 or volume < min_volume:
        logger.warning("[ENTRY HARD GUARD] NG symbol=%s side=%s reason=low_volume volume=%.0f min_volume=%.0f", symbol, side, volume, min_volume)
        return False
    if turnover < min_turnover:
        logger.warning("[ENTRY HARD GUARD] NG symbol=%s side=%s reason=low_turnover turnover=%.0f min_turnover=%.0f", symbol, side, turnover, min_turnover)
        return False

    if not _env_bool("ENTRY_HARD_REQUIRE_MOVEMENT", True):
        return True

    range_value = _hard_guard_range_pct(row, close)
    atr = _safe_float(_guard_first(row, ("atr", "atr_1m", "atr_3m", "atr_5m"), 0.0), 0.0)
    atr_ratio = atr / close if close > 0 and atr > 0 else 0.0
    slope = _safe_float(_guard_first(row, ("slope_atr_scaled", "slope", "score_slope", "disp_slope", "_slope"), 0.0), 0.0)

    min_range = _env_float("ENTRY_HARD_MIN_RANGE_PCT", 0.006)
    min_atr_ratio = _env_float("ENTRY_HARD_MIN_ATR_RATIO", 0.003)
    min_abs_slope = _env_float("ENTRY_HARD_MIN_ABS_SLOPE", 0.001)

    movement_ok = range_value >= min_range or atr_ratio >= min_atr_ratio or abs(slope) >= min_abs_slope
    if not movement_ok:
        if _hard_guard_summary_ai_low_movement_rescue(row, symbol=symbol, side=side, volume=volume, turnover=turnover, close=close):
            return True
        logger.warning("[ENTRY HARD GUARD] NG symbol=%s side=%s reason=low_movement range_pct=%.5f atr_ratio=%.5f slope=%.6f", symbol, side, range_value, atr_ratio, slope)
        return False
    return True


# ---- 直近summary DB流動性ガード (旧 entry_liquidity_runtime_patch.py V1.4) ----


def _recent_liq_calc_turnover_yen(close: float, volume: float) -> float:
    close = _safe_float(close, 0.0)
    volume = _safe_float(volume, 0.0)
    if close <= 0 or volume <= 0:
        return 0.0
    return close * volume


def _recent_liq_normalize_turnover_yen(turnover: float, close: float, volume: float) -> float:
    raw = max(0.0, _safe_float(turnover, 0.0))
    calc = _recent_liq_calc_turnover_yen(close, volume)
    if calc > 0 and (raw <= 0 or raw < calc * 0.5):
        return calc
    return raw


def _recent_liq_entry_row_values(row: dict) -> dict:
    close = _safe_float(_guard_first(row, ("close_price", "close", "price", "current_price"), 0.0), 0.0)
    volume = _safe_float(_guard_first(row, ("volume", "Volume", "vol", "出来高"), 0.0), 0.0)
    high = _safe_float(_guard_first(row, ("high_price", "high"), 0.0), 0.0)
    low = _safe_float(_guard_first(row, ("low_price", "low"), 0.0), 0.0)
    atr = _safe_float(_guard_first(row, ("atr", "atr_1m", "atr_3m", "atr_5m"), 0.0), 0.0)
    turnover_raw = _safe_float(_guard_first(row, ("turnover_yen", "turnover", "trading_value", "売買代金"), 0.0), 0.0)
    turnover = _recent_liq_normalize_turnover_yen(turnover_raw, close, volume)
    return {
        "close": close,
        "volume": volume,
        "turnover": turnover,
        "range_pct": ((high - low) / close) if close > 0 and high >= low and high > 0 and low > 0 else 0.0,
        "atr_pct": (atr / close) if close > 0 and atr > 0 else 0.0,
    }


def _recent_liq_summary_db_path() -> str:
    base = os.getenv(
        "SUMMARY_DB_DIR",
        r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\kabu_station\summary",
    )
    return os.getenv("SUMMARY_DB_PATH", str(Path(base) / f"summary{_daily_risk_today()}.db"))


def _recent_liq_col(conn: sqlite3.Connection, table: str, names: list[str]) -> str:
    try:
        cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for n in names:
            if n in cols:
                return n
    except Exception:
        return ""
    return ""


def _recent_liq_recent_values(symbol: str, bars: int) -> dict:
    path = _recent_liq_summary_db_path()
    table = os.getenv("ENTRY_LIQ_SUMMARY_TABLE", "stock_summary_1min")
    if not symbol or not Path(path).exists():
        return {}
    try:
        with sqlite3.connect(path, timeout=1.0) as conn:
            conn.execute("PRAGMA busy_timeout=1000")
            sym = _recent_liq_col(conn, table, ["symbol", "code", "stock_code"])
            tm = _recent_liq_col(conn, table, ["datetime", "dt", "timestamp", "time"])
            cl = _recent_liq_col(conn, table, ["close_price", "close", "price", "current_price"])
            hi = _recent_liq_col(conn, table, ["high_price", "high"])
            lo = _recent_liq_col(conn, table, ["low_price", "low"])
            vo = _recent_liq_col(conn, table, ["volume", "Volume", "vol"])
            tv = _recent_liq_col(conn, table, ["turnover_yen", "turnover", "trading_value", "売買代金"])
            at = _recent_liq_col(conn, table, ["atr", "atr_1m", "atr_3m", "atr_5m"])
            if not sym or not tm or not cl:
                return {}
            select = f"{tm}, {cl}, {hi or '0'}, {lo or '0'}, {vo or '0'}, {tv or '0'}, {at or '0'}"
            sql = f"SELECT {select} FROM {table} WHERE CAST({sym} AS TEXT)=? ORDER BY {tm} DESC LIMIT ?"
            rows = conn.execute(sql, (normalize_symbol(symbol), max(1, bars))).fetchall()
            if not rows:
                return {}
        close = _safe_float(rows[0][1], 0.0)
        highs = [_safe_float(r[2], 0.0) for r in rows if _safe_float(r[2], 0.0) > 0]
        lows = [_safe_float(r[3], 0.0) for r in rows if _safe_float(r[3], 0.0) > 0]
        volume = sum(max(0.0, _safe_float(r[4], 0.0)) for r in rows)
        turnover_raw = sum(max(0.0, _safe_float(r[5], 0.0)) for r in rows)
        turnover_calc_yen = sum(_recent_liq_calc_turnover_yen(_safe_float(r[1], 0.0), _safe_float(r[4], 0.0)) for r in rows)
        turnover = _recent_liq_normalize_turnover_yen(turnover_raw, close, volume)
        if turnover_calc_yen > 0 and turnover < turnover_calc_yen * 0.5:
            turnover = turnover_calc_yen
        atrs = [_safe_float(r[6], 0.0) for r in rows if _safe_float(r[6], 0.0) > 0]
        return {
            "close": close,
            "volume": volume,
            "turnover": turnover,
            "range_pct": ((max(highs) - min(lows)) / close) if close > 0 and highs and lows else 0.0,
            "atr_pct": ((sum(atrs) / len(atrs)) / close) if close > 0 and atrs else 0.0,
        }
    except Exception:
        logger.debug("[ENTRY LIQ GUARD] recent read failed symbol=%s path=%s", symbol, path, exc_info=True)
        return {}


def _recent_liq_values(row: dict) -> dict:
    if _env_bool("ENTRY_LIQ_USE_RECENT_SUMMARY", True):
        v = _recent_liq_recent_values(normalize_symbol(row.get("symbol")), _env_int("ENTRY_LIQ_RECENT_BARS", 5))
        if v:
            return v
    return _recent_liq_entry_row_values(row)


def _recent_liq_ok(row: dict, symbol: str, side: str) -> bool:
    if not _env_bool("ENTRY_LIQ_GUARD_ENABLED", True):
        return True
    v = _recent_liq_values(row)
    close = _safe_float(v.get("close"), 0.0)
    volume = _safe_float(v.get("volume"), 0.0)
    turnover = _safe_float(v.get("turnover"), 0.0)
    range_pct = _safe_float(v.get("range_pct"), 0.0)
    atr_pct = _safe_float(v.get("atr_pct"), 0.0)
    min_volume = _env_float("ENTRY_LIQ_MIN_VOLUME", 30000.0)
    min_turnover = _env_float("ENTRY_LIQ_MIN_TURNOVER_YEN", 10000000.0)
    min_range_pct = _env_float("ENTRY_LIQ_MIN_RANGE_PCT", 0.0015)
    min_atr_pct = _env_float("ENTRY_LIQ_MIN_ATR_PCT", 0.0010)
    if _env_bool("ENTRY_LIQ_REQUIRE_DATA", True):
        if close <= 0 or volume <= 0:
            logger.warning("[ENTRY LIQ GUARD] NG symbol=%s side=%s reason=no_recent_data close=%s volume=%s", symbol, side, close, volume)
            return False
    if volume < min_volume:
        logger.warning("[ENTRY LIQ GUARD] NG symbol=%s side=%s reason=volume_low volume=%.0f min=%.0f", symbol, side, volume, min_volume)
        return False
    if turnover < min_turnover:
        logger.warning("[ENTRY LIQ GUARD] NG symbol=%s side=%s reason=turnover_low turnover=%.0f min=%.0f", symbol, side, turnover, min_turnover)
        return False
    if range_pct < min_range_pct and atr_pct < min_atr_pct:
        logger.warning("[ENTRY LIQ GUARD] NG symbol=%s side=%s reason=movement_low range_pct=%.5f atr_pct=%.5f", symbol, side, range_pct, atr_pct)
        return False
    return True


# ---- 重複発注クールダウン (旧 final_entry_duplicate_cooldown_patch.py, デッドコードから復活) ----

_DUP_COOLDOWN_LAST_ATTEMPT_TS: dict[tuple[str, str], float] = {}
_DUP_COOLDOWN_INFLIGHT: set[tuple[str, str]] = set()
_DUP_COOLDOWN_LOCK = threading.Lock()


def _dup_cooldown_check_and_enter(symbol: str, side: str) -> bool:
    """True=発注続行可。inflight中/cooldown中は False。呼び出し側は finally で
    _dup_cooldown_leave() を呼ぶこと。"""
    if not _env_bool("ENTRY_FINAL_DUPLICATE_COOLDOWN_ENABLED", True):
        return True
    if not symbol or side not in {"BUY", "SELL"}:
        return True
    key = (symbol, side)
    now = time.time()
    cooldown = _env_float("ENTRY_FINAL_DUPLICATE_COOLDOWN_SEC", 45.0)
    cooldown = max(0.0, min(300.0, cooldown))
    with _DUP_COOLDOWN_LOCK:
        if key in _DUP_COOLDOWN_INFLIGHT:
            logger.warning("[FINAL ENTRY DUP COOLDOWN] skip inflight symbol=%s side=%s", symbol, side)
            return False
        last = _DUP_COOLDOWN_LAST_ATTEMPT_TS.get(key, 0.0)
        if cooldown > 0 and last > 0 and (now - last) < cooldown:
            logger.warning("[FINAL ENTRY DUP COOLDOWN] skip cooldown symbol=%s side=%s elapsed=%.1fs cooldown=%.1fs", symbol, side, now - last, cooldown)
            return False
        _DUP_COOLDOWN_LAST_ATTEMPT_TS[key] = now
        _DUP_COOLDOWN_INFLIGHT.add(key)
    return True


def _dup_cooldown_leave(symbol: str, side: str) -> None:
    with _DUP_COOLDOWN_LOCK:
        _DUP_COOLDOWN_INFLIGHT.discard((symbol, side))


# ---- kabu APIエラー診断 (旧 entry_controller_runtime_reject_patch.py V5) ----

KABU_CODE_CREDIT_NEW_ORDER_SUPPRESSED = "100368"
KABU_CODE_SYMBOL_TRADE_RESTRICTED = "100033"


def _kabu_norm_code(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        return s[:-2] if s.endswith(".0") else s
    except Exception:
        return ""


def _kabu_last_send_error() -> dict:
    try:
        from kabu_api.send_order import get_last_send_order_error
        err = get_last_send_order_error()
        return dict(err) if isinstance(err, dict) else {}
    except Exception:
        return {}


def _kabu_same_order_error(err: dict, *, symbol: str, side: str) -> bool:
    try:
        if not err:
            return False
        return str(err.get("symbol") or "").strip() == str(symbol) and str(err.get("side") or "").strip().upper() == str(side).upper()
    except Exception:
        return False


def _kabu_is_100368_error(err: dict) -> bool:
    code = _kabu_norm_code(err.get("code"))
    msg = str(err.get("message") or "")
    return code == KABU_CODE_CREDIT_NEW_ORDER_SUPPRESSED or ("信用新規" in msg and "抑止" in msg)


def _kabu_is_100033_error(err: dict) -> bool:
    code = _kabu_norm_code(err.get("code"))
    msg = str(err.get("message") or "")
    return code == KABU_CODE_SYMBOL_TRADE_RESTRICTED or ("この銘柄" in msg and "取引" in msg and "制限" in msg)


# ---- 未約定キャンセル後の再queue用item記憶 (旧 entry_summary_retry_rotation_runtime_patch.py) ----


def _remember_order_for_summary_retry(order_id: str, symbol: str, side: str, entry: Any) -> None:
    try:
        from core.startup.entry_summary_retry_rotation_runtime_patch import remember_order_for_retry
        remember_order_for_retry(order_id, symbol, side, entry)
    except Exception:
        logger.debug("[ENTRY CONTROLLER] summary retry remember failed order_id=%s symbol=%s", order_id, symbol, exc_info=True)


# ---- 監査ログ (旧 trading/audit_logging/entry_controller_audit_patch.py V02) ----


def _audit_candidate_ok_safe(symbol: str, side: str, entry_row: dict, ai: dict, ai_msg: str) -> str:
    entry_id = ""
    try:
        from trading.audit_logging.entry_audit import audit_candidate_ok, _build_entry_id
        entry_id = _build_entry_id(symbol, side, entry_row)
        audit_candidate_ok(symbol=symbol, side=side, entry_row=entry_row, ai=ai, ai_msg=ai_msg)
    except Exception:
        pass
    return entry_id


def _audit_order_safe(*, symbol: str, side: str, qty: int, order_type: str, price: Any, status: str, reason: str, entry_row: dict, ai: dict, ai_msg: str, entry_id: str) -> None:
    try:
        from trading.audit_logging.entry_audit import audit_order
        audit_order(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            price=price,
            status=status,
            reason=reason,
            entry_row=entry_row,
            ai=ai,
            ai_msg=ai_msg,
            entry_id=entry_id,
        )
    except Exception:
        pass


# ---- 経過候補ガード + タイムアウト実行 (旧 entry_execute_timeout_guard_patch.py V6) ----

_EXECUTE_INFLIGHT: dict[tuple[str, str], dict[str, Any]] = {}
_EXECUTE_INFLIGHT_LOCK = threading.RLock()
_EXECUTE_LOCAL = threading.local()


def _execute_first_dt(*values: Any) -> dt.datetime | None:
    for v in values:
        try:
            if isinstance(v, dt.datetime):
                return v.replace(tzinfo=None)
            if v is None or str(v).strip() == "":
                continue
            s = str(v).strip()
            try:
                return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                pass
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
                try:
                    return dt.datetime.strptime(s, fmt)
                except Exception:
                    continue
        except Exception:
            continue
    return None


def _execute_is_summary_ai_candidate(item: dict, row: dict) -> bool:
    try:
        ai = item.get("ai") if isinstance(item.get("ai"), dict) else {}
        source = str(item.get("source") or row.get("source") or ai.get("source") or "").upper()
        entry_type = str(item.get("entry_type") or row.get("entry_type") or ai.get("entry_type") or "").upper()
        return source == "SUMMARY" or entry_type == "SUMMARY_AI"
    except Exception:
        return False


def _execute_item_age_sec(item: dict, row: dict) -> tuple[float | None, str]:
    ai = item.get("ai") if isinstance(item.get("ai"), dict) else {}
    now = dt.datetime.now()
    if _execute_is_summary_ai_candidate(item, row):
        ts = _execute_first_dt(item.get("updated_at"), row.get("updated_at"), ai.get("updated_at"), item.get("created_at"), row.get("created_at"), item.get("pending_created_at"))
        if ts is not None:
            return max(0.0, (now - ts).total_seconds()), "created_at"
        return 0.0, "summary_ai_now_fallback"
    ts = _execute_first_dt(item.get("created_at"), item.get("pending_created_at"), ai.get("created_at"))
    if ts is not None:
        return max(0.0, (now - ts).total_seconds()), "created_at"
    ts = _execute_first_dt(item.get("datetime"), row.get("datetime"), item.get("entry_time"), row.get("entry_time"))
    if ts is not None:
        return max(0.0, (now - ts).total_seconds()), "datetime"
    return None, "missing"


def _execute_prune_pending_for_symbol(symbol: str, side: str, reason: str) -> int:
    try:
        from trading.entry import pending_manager
        side_u = _guard_norm_side(side)

        def pred(sym: str, entry: dict) -> bool:
            if normalize_symbol(sym) != symbol:
                return False
            e_side = _guard_norm_side(entry.get("side") or entry.get("entry_decision") or entry.get("ai_side"))
            return not e_side or e_side == side_u

        return int(pending_manager.prune_entries(pred, reason=reason))
    except Exception:
        logger.exception("[ENTRY EXEC TIMEOUT GUARD] pending prune failed symbol=%s side=%s reason=%s", symbol, side, reason)
        return 0


def _execute_candidate_stale_ok(item: dict, row: dict, symbol: str, side: str) -> bool:
    if not _env_bool("ENTRY_EXECUTE_STALE_CANDIDATE_GUARD_ENABLED", True):
        return True
    age, source = _execute_item_age_sec(item, row)
    if age is None:
        return True
    max_age = _env_float("ENTRY_EXECUTE_MAX_CANDIDATE_AGE_SEC", 90.0)
    if source == "datetime":
        max_age = _env_float("ENTRY_EXECUTE_MAX_BAR_AGE_SEC", 180.0)
    if source == "summary_ai_now_fallback":
        return True
    if age <= max_age:
        return True
    removed = _execute_prune_pending_for_symbol(symbol, side, "execute_candidate_stale")
    logger.warning("[ENTRY EXEC TIMEOUT GUARD] STALE_CANDIDATE_SKIP symbol=%s side=%s age=%.1fs max_age=%.1fs pruned=%s", symbol, side, age, max_age, removed)
    return False


def _execute_cleanup_inflight() -> None:
    now = time.time()
    ttl = _env_float("ENTRY_EXECUTE_INFLIGHT_TTL_SEC", 120.0)
    with _EXECUTE_INFLIGHT_LOCK:
        for key, info in list(_EXECUTE_INFLIGHT.items()):
            started = float(info.get("started", now))
            if bool(info.get("done")) or now - started > ttl:
                _EXECUTE_INFLIGHT.pop(key, None)


def _execute_best_candidate_core(item: dict, boost_active: bool) -> bool:
    symbol = item["symbol"]
    entry_row = item["entry_row"]
    entry_type = item["entry_type"]
    side = item["side"]
    ai = item["ai"]
    ai_msg = item.get("ai_msg") or ""
    entry = item.get("entry")

    # 1. 日次リスクガード (銘柄別上限/日次損失上限/連敗停止/勝ち銘柄再エントリー許可)
    blocked, reason, detail = _daily_risk_block_reason(symbol, side)
    if blocked:
        _log_skip(symbol, reason, side=side)
        return False

    # 2. 時間帯/流動性/反転/板ガード
    if not _final_guard_entry_time_ok(symbol, side):
        _log_skip(symbol, "time_guard_ng", side=side)
        return False
    if not _final_guard_liquidity_ok(entry_row, symbol, side):
        _log_skip(symbol, "liquidity_guard_ng", side=side)
        return False
    if not _final_guard_recent_reverse_ok(entry_row, symbol, side):
        _log_skip(symbol, "recent_reverse_guard_ng", side=side)
        return False
    if not _final_guard_board_ok(entry_row, item, symbol, side):
        _log_skip(symbol, "board_missing", side=side, retryable=True)
        return False

    # 3. 出来高/値動きハードガード (SUMMARY_AI高スコア救済つき)
    if not _hard_guard_ok(item):
        _log_skip(symbol, "liquidity_movement_hard_guard_ng", side=side)
        return False

    # 4. 直近summary DB流動性ガード
    if not _recent_liq_ok(entry_row, symbol, side):
        _log_skip(symbol, "recent_liquidity_guard_ng", side=side)
        return False

    # 5. 重複発注クールダウン (同一銘柄・同一方向の連続発注を抑止)
    if not _dup_cooldown_check_and_enter(symbol, side):
        return False
    try:
        return _execute_best_candidate_dispatch(item, boost_active, symbol=symbol, entry_row=entry_row, entry_type=entry_type, side=side, ai=ai, ai_msg=ai_msg, entry=entry)
    finally:
        _dup_cooldown_leave(symbol, side)


def _execute_best_candidate_dispatch(item: dict, boost_active: bool, *, symbol: str, entry_row: dict, entry_type: str, side: str, ai: dict, ai_msg: str, entry: Any) -> bool:
    entry_id = _audit_candidate_ok_safe(symbol, side, entry_row, ai, ai_msg)

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
        last_err = _kabu_last_send_error()
        same_err = _kabu_same_order_error(last_err, symbol=symbol, side=side)

        if same_err and _kabu_is_100368_error(last_err):
            # 100368「信用新規の注文は抑止されております」は銘柄個別停止にしない。
            # 今回の注文は失敗だが、次候補・次サイクルは止めない。
            logger.warning(
                "🚫 CREDIT_NEW_ORDER_API_REJECT_NO_LOCAL_SUPPRESS_CONFIRMED symbol=%s side=%s qty=%s order_type=%s price=%s last_error=%s",
                symbol, side, order_qty, order_type, order_price, last_err,
            )
            _audit_order_safe(symbol=symbol, side=side, qty=0, order_type="ENTRY_PIPELINE", price=price, status="FAILED_OR_SKIPPED", reason="execute_best_candidate_result", entry_row=entry_row, ai=ai, ai_msg=ai_msg, entry_id=entry_id)
            _log_skip(symbol, "CREDIT_NEW_ORDER_API_REJECT_NO_LOCAL_SUPPRESS", side=side, qty=order_qty, order_type=order_type, price=order_price, code=last_err.get("code"), message=last_err.get("message"))
            return False

        if same_err and _kabu_is_100033_error(last_err):
            # 100033「この銘柄のお取引は制限されています」は銘柄個別制限として扱う。
            logger.warning(
                "🚫 SYMBOL_TRADE_RESTRICTED_BY_KABU_API_CONFIRMED symbol=%s side=%s qty=%s order_type=%s price=%s code=%s message=%s",
                symbol, side, order_qty, order_type, order_price, last_err.get("code"), last_err.get("message"),
            )
            _audit_order_safe(symbol=symbol, side=side, qty=0, order_type="ENTRY_PIPELINE", price=price, status="FAILED_OR_SKIPPED", reason="execute_best_candidate_result", entry_row=entry_row, ai=ai, ai_msg=ai_msg, entry_id=entry_id)
            _log_skip(symbol, "SYMBOL_TRADE_RESTRICTED_BY_KABU_API", side=side, qty=order_qty, order_type=order_type, price=order_price, code=last_err.get("code"), message=last_err.get("message"))
            return False

        logger.warning(
            "⚠ ORDER_ID_EMPTY_NO_LONG_RESTRICT symbol=%s side=%s qty=%s order_type=%s price=%s last_error=%s -> retry allowed next cycle",
            symbol,
            side,
            order_qty,
            order_type,
            order_price,
            last_err,
        )
        _audit_order_safe(symbol=symbol, side=side, qty=0, order_type="ENTRY_PIPELINE", price=price, status="FAILED_OR_SKIPPED", reason="execute_best_candidate_result", entry_row=entry_row, ai=ai, ai_msg=ai_msg, entry_id=entry_id)
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
    try:
        _daily_risk_record_entry_sent(symbol)
    except Exception:
        logger.exception("[ENTRY DAILY RISK] record entry_sent failed symbol=%s", symbol)
    _remember_order_for_summary_retry(str(order_id), symbol, side, entry)
    _audit_order_safe(symbol=symbol, side=side, qty=0, order_type="ENTRY_PIPELINE", price=price, status="ORDER_ACCEPTED", reason="execute_best_candidate_result", entry_row=entry_row, ai=ai, ai_msg=ai_msg, entry_id=entry_id)

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


def _execute_best_candidate(item: dict, boost_active: bool) -> bool:
    if not isinstance(item, dict):
        logger.warning("[ENTRY EXEC TIMEOUT GUARD] INVALID_ITEM type=%s", type(item))
        return False
    symbol = normalize_symbol(item.get("symbol"))
    side = _guard_norm_side(item.get("side"))
    row = _guard_row_to_dict(item.get("entry_row"))
    if not symbol or side not in {"BUY", "SELL"}:
        logger.warning("[ENTRY EXEC TIMEOUT GUARD] INVALID_SYMBOL_SIDE symbol=%s side=%s", symbol, side)
        return False
    if not _execute_candidate_stale_ok(item, row, symbol, side):
        return False

    if bool(getattr(_EXECUTE_LOCAL, "inside_timeout_runner", False)):
        # entry_execute_timeout_guard のタイムアウト実行スレッド内から再入した場合は
        # 二重タイムアウト・二重スレッド生成を避け、直接実行する。
        return bool(_execute_best_candidate_core(item, boost_active))

    if not _env_bool("ENTRY_EXECUTE_TIMEOUT_GUARD_ENABLED", True):
        return bool(_execute_best_candidate_core(item, boost_active))

    timeout = max(0.5, _env_float("ENTRY_EXECUTE_ORIG_TIMEOUT_SEC", 8.0))
    q: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
    key = (symbol, side)

    def runner() -> None:
        prev = bool(getattr(_EXECUTE_LOCAL, "inside_timeout_runner", False))
        _EXECUTE_LOCAL.inside_timeout_runner = True
        try:
            q.put_nowait(("ok", bool(_execute_best_candidate_core(item, boost_active))))
        except Exception as exc:
            try:
                q.put_nowait(("err", exc))
            except Exception:
                pass
        finally:
            _EXECUTE_LOCAL.inside_timeout_runner = prev
            with _EXECUTE_INFLIGHT_LOCK:
                info = _EXECUTE_INFLIGHT.get(key)
                if isinstance(info, dict):
                    info["done"] = True

    with _EXECUTE_INFLIGHT_LOCK:
        _execute_cleanup_inflight()
        existing = _EXECUTE_INFLIGHT.get(key)
        if isinstance(existing, dict) and not bool(existing.get("done")):
            logger.warning("[ENTRY EXEC TIMEOUT GUARD] INFLIGHT_DUPLICATE_SKIP symbol=%s side=%s", symbol, side)
            return False
        _EXECUTE_INFLIGHT[key] = {"started": time.time(), "done": False}

    th = threading.Thread(target=runner, name=f"entry-execute-timeout-{symbol}-{side}", daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        removed = _execute_prune_pending_for_symbol(symbol, side, "execute_orig_timeout")
        logger.warning("[ENTRY EXEC TIMEOUT GUARD] ORIG_TIMEOUT_RETURN_FALSE symbol=%s side=%s timeout=%.1fs pruned=%s", symbol, side, timeout, removed)
        return False
    try:
        status, value = q.get_nowait()
    except Exception:
        logger.warning("[ENTRY EXEC TIMEOUT GUARD] ORIG_EMPTY_RESULT symbol=%s side=%s", symbol, side)
        return False
    finally:
        with _EXECUTE_INFLIGHT_LOCK:
            _EXECUTE_INFLIGHT.pop(key, None)
    if status == "err":
        logger.warning("[ENTRY EXEC TIMEOUT GUARD] ORIG_ERROR_RETURN_FALSE symbol=%s side=%s error=%r", symbol, side, value)
        return False
    return bool(value)


# ==========================================================
# run_entry_pipeline: lock-wait + stale-reset + 戻り値正規化
# (旧 core/startup/entry_controller_pipeline_lock_wait_patch.py +
#  summary_ai_entry_controller_bridge_patch.py の run_entry_pipeline 部分をインライン化)
# ==========================================================

_LAST_STALE_PIPELINE_LOCK_RESET_AT = 0.0

_PIPELINE_ORDER_KEYS = ("order_id", "OrderId", "orders", "order_ids", "sent_orders", "executed_symbols")
_PIPELINE_EXECUTED_COUNT_KEYS = ("executed_count", "order_count", "submitted_count", "sent_count")


def _pipeline_wait_enabled_for_source(source_u: str) -> bool:
    if not source_u:
        return False
    if source_u == "RANKING" and not _env_bool("ENTRY_CONTROLLER_RANKING_LOCK_WAIT_ENABLED", True):
        return False
    if not _env_bool("ENTRY_CONTROLLER_LOCK_WAIT_ENABLED", True):
        return False
    sources = {s.strip().upper() for s in os.getenv("ENTRY_CONTROLLER_LOCK_WAIT_SOURCES", "RANKING,TONOSAMA,SUMMARY").replace(";", ",").split(",") if s.strip()}
    return source_u in sources


def _pipeline_lock_wait_timeout_sec(source_u: str) -> float:
    # SUMMARY だけ大幅に長く待つ。8秒の既定では TONOSAMA controller が timeout で
    # scheduler に戻っても thread_alive=True の間ロックを保持するケースを取りこぼしていた
    # (旧 summary_ai_entry_controller_bridge_patch.py が 35秒へ延長した根拠)。
    if source_u == "SUMMARY":
        base = _env_float("ENTRY_CONTROLLER_SUMMARY_LOCK_WAIT_SEC", 35.0)
        cap = _env_float("ENTRY_CONTROLLER_SUMMARY_LOCK_WAIT_MAX_SEC", 35.0)
        return max(0.0, min(base, cap))
    base = _env_float("ENTRY_CONTROLLER_LOCK_WAIT_SEC", 12.0)
    cap = _env_float("ENTRY_CONTROLLER_LOCK_WAIT_MAX_SEC", 12.0)
    return max(0.0, min(base, cap))


def _pipeline_lock_wait_poll_sec() -> float:
    return max(0.05, _env_float("ENTRY_CONTROLLER_LOCK_WAIT_POLL_SEC", 0.20))


def _pipeline_pending_count_for_source(source_u: str) -> int:
    total = 0
    try:
        root = getattr(global_data, "pending_entries", None)
        if isinstance(root, dict):
            for bucket in root.values():
                entries = bucket if isinstance(bucket, (list, tuple, set)) else [bucket]
                for entry in entries:
                    if isinstance(entry, dict) and (not source_u or _normalize_source(entry.get("source")) == source_u):
                        total += 1
    except Exception:
        pass
    return int(total)


def _pipeline_snapshot_pending_count() -> tuple[dict, int]:
    # snapshot_root() returns {symbol: bucket_size}, not {symbol: [entries]}.
    try:
        root = snapshot_root()
        if isinstance(root, dict):
            total = sum(int(v or 0) for v in root.values() if isinstance(v, (int, float)))
            return dict(root), total
    except Exception:
        pass
    return {}, 0


def _pipeline_inflight_count() -> int:
    try:
        import core.startup.entry_execute_timeout_guard_patch as eg
        inflight = getattr(eg, "_INFLIGHT", {})
        if isinstance(inflight, dict):
            return sum(1 for info in inflight.values() if isinstance(info, dict) and not bool(info.get("done")))
    except Exception:
        pass
    return 0


def _pipeline_lock_is_held() -> bool:
    try:
        return bool(_pipeline_lock.locked())
    except Exception:
        return False


def _wait_entry_pipeline_lock_if_needed(source_u: str, *, before_pending: int) -> tuple[bool, float]:
    """RANKING/TONOSAMA/SUMMARY はロック使用中でも即失敗させず、一定時間待つ。"""
    if not _pipeline_wait_enabled_for_source(source_u):
        return True, 0.0
    if not _pipeline_lock_is_held():
        return True, 0.0

    timeout = _pipeline_lock_wait_timeout_sec(source_u)
    poll = _pipeline_lock_wait_poll_sec()
    started = time.perf_counter()
    waited = 0.0
    logger.warning("[ENTRY CONTROLLER LOCK WAIT] dispatch start source=%s pending=%s timeout=%.3fs", source_u, before_pending, timeout)
    while _pipeline_lock_is_held():
        waited = time.perf_counter() - started
        if waited >= timeout:
            logger.warning("[ENTRY CONTROLLER LOCK WAIT] timeout source=%s waited=%.3fs pending=%s", source_u, waited, before_pending)
            return False, waited
        time.sleep(poll)
    if waited > 0:
        logger.warning("[ENTRY CONTROLLER LOCK WAIT] lock free source=%s waited=%.3fs pending=%s", source_u, waited, before_pending)
    return True, waited


def _pipeline_entry_age_sec(entry: dict) -> float | None:
    if not isinstance(entry, dict):
        return None
    for key in ("created_at", "pending_created_at", "updated_at", "entry_time", "datetime"):
        v = entry.get(key)
        ts = None
        if isinstance(v, dt.datetime):
            ts = v.replace(tzinfo=None)
        elif isinstance(v, str) and v.strip():
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
                try:
                    ts = dt.datetime.strptime(v.strip(), fmt)
                    break
                except Exception:
                    continue
            if ts is None:
                try:
                    ts = dt.datetime.fromisoformat(v.strip().replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    ts = None
        if ts is not None:
            try:
                return max(0.0, (dt.datetime.now() - ts).total_seconds())
            except Exception:
                return None
    return None


def _prune_old_pending_for_source(source_u: str, *, reason: str = "lock_timeout_old_pending") -> int:
    max_age_sec = (
        _env_float("ENTRY_CONTROLLER_SUMMARY_PENDING_PRUNE_AGE_SEC", 90.0)
        if source_u == "SUMMARY"
        else _env_float("ENTRY_CONTROLLER_PENDING_PRUNE_AGE_SEC", 120.0)
    )
    try:
        from trading.entry.pending_manager import prune_entries

        def pred(_sym: str, entry: dict) -> bool:
            if not isinstance(entry, dict):
                return False
            if _normalize_source(entry.get("source")) != source_u:
                return False
            age = _pipeline_entry_age_sec(entry)
            return age is not None and age >= max_age_sec

        removed = int(prune_entries(pred, reason=reason))
        if removed:
            logger.warning(
                "[ENTRY CONTROLLER LOCK WAIT] old pending pruned source=%s removed=%s max_age=%.1fs reason=%s",
                source_u, removed, max_age_sec, reason,
            )
        return removed
    except Exception:
        logger.exception("[ENTRY CONTROLLER LOCK WAIT] old pending prune failed source=%s", source_u)
        return 0


def _can_reset_stale_pipeline_lock(source_u: str) -> tuple[bool, str]:
    if source_u != "SUMMARY" and not _env_bool("ENTRY_CONTROLLER_STALE_LOCK_RESET_NON_SUMMARY", False):
        return False, "source_not_allowed"
    if not _env_bool("ENTRY_CONTROLLER_STALE_LOCK_RESET_ENABLED", True):
        return False, "disabled"
    pending = _pipeline_pending_count_for_source(source_u)
    min_pending = int(_env_float("ENTRY_CONTROLLER_STALE_LOCK_MIN_PENDING", 1.0))
    if pending < min_pending:
        return False, f"pending_low:{pending}<{min_pending}"
    if _pipeline_inflight_count() > 0:
        return False, "entry_execute_inflight"
    cooldown = _env_float("ENTRY_CONTROLLER_STALE_LOCK_RESET_COOLDOWN_SEC", 10.0)
    now = time.time()
    if now - float(_LAST_STALE_PIPELINE_LOCK_RESET_AT or 0.0) < cooldown:
        return False, "cooldown"
    return True, "ok"


def _reset_pipeline_lock_if_stale(source_u: str, *, waited: float) -> bool:
    global _LAST_STALE_PIPELINE_LOCK_RESET_AT, _pipeline_lock
    ok, why = _can_reset_stale_pipeline_lock(source_u)
    if not ok:
        logger.warning("[ENTRY CONTROLLER LOCK WAIT] stale lock reset skipped source=%s reason=%s waited=%.3fs", source_u, why, waited)
        return False
    try:
        old_lock = _pipeline_lock
        _pipeline_lock = threading.RLock()
        _LAST_STALE_PIPELINE_LOCK_RESET_AT = time.time()
        logger.warning(
            "[ENTRY CONTROLLER LOCK WAIT] STALE_LOCK_RESET source=%s waited=%.3fs old_lock=%r new_lock=%r",
            source_u, waited, old_lock, _pipeline_lock,
        )
        return True
    except Exception:
        logger.exception("[ENTRY CONTROLLER LOCK WAIT] stale lock reset failed source=%s", source_u)
        return False


def _pipeline_result_has_payload(v: Any) -> bool:
    if isinstance(v, (list, tuple, set, dict)):
        return len(v) > 0
    return bool(v)


def _pipeline_strict_order_executed(result: Any) -> bool:
    """実注文が確認できた場合だけ True。approved/registered/pending減少だけでは成功扱いしない。"""
    try:
        if result is None:
            return False
        if isinstance(result, bool):
            return result
        if isinstance(result, dict):
            for key in _PIPELINE_EXECUTED_COUNT_KEYS:
                try:
                    if int(result.get(key) or 0) > 0:
                        return True
                except Exception:
                    pass
            for key in _PIPELINE_ORDER_KEYS:
                if _pipeline_result_has_payload(result.get(key)):
                    return True
            for key in ("result", "pipeline_result", "order_result"):
                child = result.get(key)
                if child is not result and _pipeline_strict_order_executed(child):
                    return True
            return False
        if isinstance(result, (list, tuple, set)):
            return any(_pipeline_strict_order_executed(x) for x in result)
        return False
    except Exception:
        return False


def _should_retry_run_entry_pipeline_after_no_order(*, is_summary: bool, before_pending: int, before_inflight: int, result: Any) -> bool:
    if not _env_bool("SUMMARY_AI_ENTRY_CONTROLLER_RETRY_AFTER_SKIP", True):
        return False
    if not is_summary or before_pending <= 0:
        return False
    if _pipeline_strict_order_executed(result):
        return False
    _, after_pending = _pipeline_snapshot_pending_count()
    after_inflight = _pipeline_inflight_count()
    return after_inflight <= before_inflight and after_pending > 0


def _normalize_run_entry_pipeline_result(
    result: Any, *, before_root: dict, before_pending: int, before_inflight: int,
    waited_sec: float, is_summary: bool, retry_count: int,
) -> dict:
    after_root, after_pending = _pipeline_snapshot_pending_count()
    after_inflight = _pipeline_inflight_count()
    pending_decreased = after_pending < before_pending
    inflight_increased = after_inflight > before_inflight
    executed = bool(_pipeline_strict_order_executed(result) or inflight_increased)
    approved_count = max(0, before_pending - after_pending, after_inflight - before_inflight)

    if executed:
        skip_reason = None
    elif pending_decreased:
        skip_reason = "pending_moved_without_order"
    elif retry_count > 0:
        skip_reason = "entry_controller_no_order_after_retry"
    elif is_summary and waited_sec > 0:
        skip_reason = "entry_controller_no_order_after_lock_wait"
    else:
        skip_reason = "entry_controller_no_order"

    out = dict(result) if isinstance(result, dict) else {"result": result}
    out.update({
        "executed": executed,
        "approved_count": approved_count,
        "skip_reason": skip_reason,
        "pending_moved_without_order": bool(pending_decreased and not executed),
        "pending_before": before_root,
        "pending_after": after_root,
        "pending_count_before": before_pending,
        "pending_count_after": after_pending,
        "inflight_before": before_inflight,
        "inflight_after": after_inflight,
        "waited_sec": waited_sec,
        "retry_count": retry_count,
    })
    return out


def _pipeline_lock_timeout_result(source_u: str, waited: float, before_root: dict, before_pending: int, before_inflight: int) -> dict:
    return {
        "executed": False,
        "approved_count": 0,
        "result": None,
        "skip_reason": "entry_controller_lock_timeout_retryable",
        "lock_wait_source": source_u,
        "pending_before": before_root,
        "pending_after": before_root,
        "pending_count_before": before_pending,
        "pending_count_after": before_pending,
        "inflight_before": before_inflight,
        "inflight_after": before_inflight,
        "waited_sec": round(float(waited), 3),
        "retry_count": 0,
        "retryable": True,
        "retry_next_cycle": True,
        "pending_kept": True,
    }


# ==========================================================
# メイン
# ==========================================================

def run_entry_pipeline(*, pipeline_source: str | None = None, interval: int | None = None):
    source_u = _normalize_source(pipeline_source) if pipeline_source else ""
    is_summary = source_u == "SUMMARY"
    before_root, before_pending = _pipeline_snapshot_pending_count()
    before_inflight = _pipeline_inflight_count()

    waited_ok, waited_sec = _wait_entry_pipeline_lock_if_needed(source_u, before_pending=before_pending)
    if not waited_ok:
        _prune_old_pending_for_source(source_u, reason="lock_timeout_old_pending")
        if not _reset_pipeline_lock_if_stale(source_u, waited=waited_sec):
            if _env_bool("ENTRY_CONTROLLER_LOCK_WAIT_TIMEOUT_SKIP_ORIGINAL", True):
                return _pipeline_lock_timeout_result(source_u, waited_sec, before_root, before_pending, before_inflight)

    result = _run_entry_pipeline_core(pipeline_source=pipeline_source, interval=interval)
    retry_count = 0

    if _should_retry_run_entry_pipeline_after_no_order(is_summary=is_summary, before_pending=before_pending, before_inflight=before_inflight, result=result):
        retry_count = 1
        retry_ok, retry_waited = _wait_entry_pipeline_lock_if_needed(source_u or "SUMMARY", before_pending=before_pending)
        waited_sec += retry_waited
        if retry_ok:
            logger.warning("[ENTRY CONTROLLER LOCK WAIT] retry run_entry_pipeline after no-order pending_before=%s waited_total=%.3fs", before_pending, waited_sec)
            result = _run_entry_pipeline_core(pipeline_source=pipeline_source, interval=interval)

    out = _normalize_run_entry_pipeline_result(
        result, before_root=before_root, before_pending=before_pending, before_inflight=before_inflight,
        waited_sec=waited_sec, is_summary=is_summary, retry_count=retry_count,
    )
    logger.info("[ENTRY CONTROLLER] run_entry_pipeline return normalized %s", out)
    return out


def _run_entry_pipeline_core(
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

                if pipeline_source or interval is not None:
                    # 他 pipeline_source/interval 由来の候補しか無い symbol は
                    # _build_scored_candidates を呼ぶ前にスキップする (性能最適化;
                    # 正しさ自体は _build_scored_candidates 内の
                    # _prefilter_entries_for_pipeline が保証する)。
                    bucket = [e for e in bucket if _entry_matches_pipeline(e, pipeline_source, interval)]
                    if not bucket:
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
