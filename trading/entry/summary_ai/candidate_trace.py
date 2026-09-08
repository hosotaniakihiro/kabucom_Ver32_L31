# ============================================================
# File   : trading/entry/summary_ai/candidate_trace.py
# Version: V1.0-CANDIDATE-TRACE
# ------------------------------------------------------------
# 候補 1 件を candidate_created から terminal まで同じ id で追う。
#
# 【なぜ必要か】
# 「候補は出ているのに発注されない」を調べるとき、
#   [SUMMARY AI DIAG] candidates=68 ai_ok=67
#   [SUMMARY AI EXECUTOR] strict prefilter skipped=67 kept=0
# のように段ごとの件数は出るが、どの銘柄がどこで消えたのかが辿れなかった。
# 候補ごとに id を振り、通過した段と落ちた理由を 1 行で出す。
#
# 判定は一切行わない。観測だけ。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import threading
from collections import Counter, OrderedDict
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

VERSION = "V1.0-CANDIDATE-TRACE"

# 段階
STAGE_CREATED = "candidate_created"
STAGE_3M = "3m_attach"
STAGE_5M = "5m_attach"
STAGE_AI = "ai_gate"
STAGE_PREFILTER = "prefilter"
STAGE_APPROVED = "approved"
STAGE_PENDING = "pending"
STAGE_EXECUTOR = "executor_reached"
STAGE_ORDER_ATTEMPT = "order_attempt"
STAGE_ORDER_RESPONSE = "order_response"
STAGE_TERMINAL = "terminal"

# terminal_reason の語彙 (ここに無い値も許すが、既知はここへ集約する)
TERM_AI_NG = "AI_NG"
TERM_DATA_NOT_READY_3M = "DATA_NOT_READY_3M"
TERM_DATA_NOT_READY_5M = "DATA_NOT_READY_5M"
TERM_SCORE_ZERO_3M5M = "SCORE_ZERO_3M5M"
TERM_RANGE_NOT_READY = "RANGE_NOT_READY"
TERM_LOW_MOVE = "LOW_MOVE"
TERM_STALE = "STALE"
TERM_POSITION_LIMIT = "POSITION_LIMIT"
TERM_ORDER_REJECTED = "ORDER_REJECTED"
TERM_ORDER_SENT = "ORDER_SENT"
TERM_UNKNOWN = "UNKNOWN"

_lock = threading.Lock()
_seq = 0
_traces: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_MAX_TRACES = 2000


def _now() -> dt.datetime:
    return dt.datetime.now()


def new_candidate_id(symbol: Any, side: Any, when: Optional[dt.datetime] = None) -> str:
    """例: 20260831-125400-598A-BUY-001"""
    global _seq
    when = when or _now()
    with _lock:
        _seq = (_seq + 1) % 1000
        seq = _seq
    return "%s-%s-%s-%03d" % (
        when.strftime("%Y%m%d-%H%M%S"),
        str(symbol or "?").strip() or "?",
        str(side or "?").strip().upper() or "?",
        seq,
    )


def start(symbol: Any, side: Any, *, candidate_datetime: Any = None,
          candidate_id: Optional[str] = None) -> str:
    cid = candidate_id or new_candidate_id(symbol, side)
    rec = {
        "candidate_id": cid,
        "symbol": str(symbol or ""),
        "side": str(side or "").upper(),
        "candidate_datetime": str(candidate_datetime) if candidate_datetime is not None else None,
        "created_at": _now(),
        "stages": OrderedDict(),
        "terminal_reason": None,
    }
    mark(cid, STAGE_CREATED, rec=rec)
    with _lock:
        _traces[cid] = rec
        while len(_traces) > _MAX_TRACES:
            _traces.popitem(last=False)
    return cid


def mark(candidate_id: str, stage: str, *, state: str = "ok",
         rec: Optional[Dict[str, Any]] = None, **info) -> None:
    """段の通過を記録する。存在しない id は黙って無視する (観測が本処理を止めない)。"""
    try:
        if rec is None:
            with _lock:
                rec = _traces.get(candidate_id)
        if rec is None:
            return
        rec["stages"][stage] = {
            "at": _now(),
            "state": state,
            **{k: v for k, v in info.items() if v is not None},
        }
    except Exception:
        logger.debug("[CANDIDATE TRACE] mark failed id=%s stage=%s", candidate_id, stage, exc_info=True)


def _age_sec(rec: Dict[str, Any]) -> Optional[float]:
    try:
        return round((_now() - rec["created_at"]).total_seconds(), 2)
    except Exception:
        return None


def _stage_state(rec: Dict[str, Any], stage: str) -> str:
    s = rec.get("stages", {}).get(stage)
    if not s:
        return "-"
    return str(s.get("state") or "ok")


def terminal(candidate_id: str, reason: str, **info) -> None:
    """候補が消えたら必ずここを通す。理由なしで消えたものを残さない。"""
    try:
        with _lock:
            rec = _traces.get(candidate_id)
        if rec is None:
            return
        rec["terminal_reason"] = str(reason or TERM_UNKNOWN)
        mark(candidate_id, STAGE_TERMINAL, state=str(reason or TERM_UNKNOWN), rec=rec, **info)
        logger.warning(
            "[CANDIDATE TRACE] candidate_id=%s symbol=%s side=%s candidate_datetime=%s age_sec=%s "
            "3m_state=%s 5m_state=%s ai_state=%s prefilter_state=%s executor_state=%s order_state=%s "
            "terminal_reason=%s %s",
            rec.get("candidate_id"), rec.get("symbol"), rec.get("side"),
            rec.get("candidate_datetime"), _age_sec(rec),
            _stage_state(rec, STAGE_3M), _stage_state(rec, STAGE_5M),
            _stage_state(rec, STAGE_AI), _stage_state(rec, STAGE_PREFILTER),
            _stage_state(rec, STAGE_EXECUTOR), _stage_state(rec, STAGE_ORDER_RESPONSE),
            rec.get("terminal_reason"),
            " ".join("%s=%s" % kv for kv in info.items()) if info else "",
        )
    except Exception:
        logger.debug("[CANDIDATE TRACE] terminal failed id=%s", candidate_id, exc_info=True)


def snapshot(candidate_id: str) -> Dict[str, Any]:
    with _lock:
        rec = _traces.get(candidate_id)
    return dict(rec) if rec else {}


def terminal_reason_counts() -> Dict[str, int]:
    with _lock:
        recs = list(_traces.values())
    c: Counter = Counter()
    for r in recs:
        if r.get("terminal_reason"):
            c[str(r["terminal_reason"])] += 1
    return dict(c)


def reset_for_test() -> None:
    global _seq
    with _lock:
        _traces.clear()
        _seq = 0


# ============================================================
# サイクル単位 FUNNEL
# ------------------------------------------------------------
# 1 サイクルでどこまで残ってどこで消えたかを 1 行で出す。
# ============================================================

_FUNNEL_FIELDS = (
    "candidates", "data_ready", "3m_ready", "5m_ready",
    "real_ai_ok", "fallback_ok", "ai_ng", "data_not_ready",
    "prefilter_kept", "approved", "executor_reached",
    "order_attempted", "ordered", "stale",
)


class CycleFunnel:
    """1 サイクル分のカウンタ。加算しか行わない。"""

    def __init__(self, *, interval: Any = None, source: Any = None) -> None:
        self.interval = interval
        self.source = source
        self.counts: Dict[str, int] = {k: 0 for k in _FUNNEL_FIELDS}
        self.terminal: Counter = Counter()
        self.started_at = _now()

    def add(self, field: str, n: int = 1) -> None:
        if not field:
            return
        self.counts[field] = self.counts.get(field, 0) + int(n)

    def set(self, field: str, n: int) -> None:
        self.counts[field] = int(n)

    def add_terminal(self, reason: Any, n: int = 1) -> None:
        self.terminal[str(reason or TERM_UNKNOWN)] += int(n)

    def emit(self, **info) -> Dict[str, Any]:
        elapsed = round((_now() - self.started_at).total_seconds(), 2)
        logger.warning(
            "[SUMMARY AI FUNNEL CYCLE] interval=%s source=%s elapsed=%ss "
            "candidates=%s data_ready=%s 3m_ready=%s 5m_ready=%s "
            "real_ai_ok=%s fallback_ok=%s ai_ng=%s data_not_ready=%s "
            "prefilter_kept=%s approved=%s executor_reached=%s "
            "order_attempted=%s ordered=%s stale=%s terminal=%s %s",
            self.interval, self.source, elapsed,
            self.counts.get("candidates"), self.counts.get("data_ready"),
            self.counts.get("3m_ready"), self.counts.get("5m_ready"),
            self.counts.get("real_ai_ok"), self.counts.get("fallback_ok"),
            self.counts.get("ai_ng"), self.counts.get("data_not_ready"),
            self.counts.get("prefilter_kept"), self.counts.get("approved"),
            self.counts.get("executor_reached"), self.counts.get("order_attempted"),
            self.counts.get("ordered"), self.counts.get("stale"),
            dict(self.terminal.most_common()),
            " ".join("%s=%s" % kv for kv in info.items()) if info else "",
        )
        return dict(self.counts)


__all__ = [
    "VERSION",
    "STAGE_CREATED", "STAGE_3M", "STAGE_5M", "STAGE_AI", "STAGE_PREFILTER",
    "STAGE_APPROVED", "STAGE_PENDING", "STAGE_EXECUTOR",
    "STAGE_ORDER_ATTEMPT", "STAGE_ORDER_RESPONSE", "STAGE_TERMINAL",
    "TERM_AI_NG", "TERM_DATA_NOT_READY_3M", "TERM_DATA_NOT_READY_5M",
    "TERM_SCORE_ZERO_3M5M", "TERM_RANGE_NOT_READY", "TERM_LOW_MOVE",
    "TERM_STALE", "TERM_POSITION_LIMIT", "TERM_ORDER_REJECTED", "TERM_ORDER_SENT",
    "new_candidate_id", "start", "mark", "terminal", "snapshot",
    "terminal_reason_counts", "reset_for_test", "CycleFunnel",
]
