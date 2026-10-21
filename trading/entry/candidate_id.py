# -*- coding: utf-8 -*-
"""候補を 1 本の ID で追跡する (P1)。

これまで `approved_count=1 pending_moved_without_order` が出ても、
どの候補が誰に消費されたのか分からなかった。
`_normalize_run_entry_pipeline_result` が pending の **件数の差** だけを見て
「減ったのに発注していない」と推定していたためで、
消えた候補そのものを追えていなかった。

この module は候補に candidate_id を打ち、
Ranking -> pending -> Controller -> Gate -> executor -> dry-run order
の各段を同じ ID で記録する。

    [CANDIDATE] id=RANKING-4424-20260826152530-7 stage=pending_created symbol=4424 ...
    [CANDIDATE] id=RANKING-4424-20260826152530-7 stage=controller_picked ...
    [CANDIDATE] id=RANKING-4424-20260826152530-7 stage=gate_approved ...
    [CANDIDATE] id=RANKING-4424-20260826152530-7 stage=executor_reached ...
    [CANDIDATE] id=RANKING-4424-20260826152530-7 stage=dry_run_order ...

    消えた候補があれば:
    [CANDIDATE VANISHED] id=... last_stage=gate_approved age=12.3s stages=[...]

判定や閾値は一切変更しない。記録だけを行う。

env:
    ENTRY_CANDIDATE_ID_ENABLED=0   -> 無効化 (ID を打たない)
    ENTRY_CANDIDATE_TRACE_MAX=2000 -> 記録しておく候補数の上限
"""
from __future__ import annotations

import collections
import datetime as dt
import logging
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

KEY = "candidate_id"

_LOCK = threading.Lock()
_SEQ = [0]
# candidate_id -> [(stage, ts, info), ...]
_TRACE: "collections.OrderedDict[str, List[Tuple[str, dt.datetime, dict]]]" = collections.OrderedDict()


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return default
        return int(float(str(v).strip()))
    except Exception:
        return default


def enabled() -> bool:
    return _env_bool("ENTRY_CANDIDATE_ID_ENABLED", True)


def _trace_max() -> int:
    return max(50, _env_int("ENTRY_CANDIDATE_TRACE_MAX", 2000))


def _norm_symbol(v: Any) -> str:
    s = str(v or "").strip()
    return s or "?"


def new_id(symbol: Any, source: Any = "", now: Optional[dt.datetime] = None) -> str:
    with _LOCK:
        _SEQ[0] += 1
        seq = _SEQ[0]
    n = now or dt.datetime.now()
    src = str(source or "UNKNOWN").strip().upper() or "UNKNOWN"
    return "%s-%s-%s-%d" % (src, _norm_symbol(symbol), n.strftime("%Y%m%d%H%M%S"), seq)


def of(item: Any) -> Optional[str]:
    """dict / Series などから candidate_id を読む。"""
    if item is None:
        return None
    try:
        if isinstance(item, dict):
            v = item.get(KEY)
        else:
            v = getattr(item, KEY, None)
            if v is None and hasattr(item, "get"):
                v = item.get(KEY)
        if v is None:
            return None
        s = str(v).strip()
        return s or None
    except Exception:
        return None


def ensure(entry: Dict[str, Any], *, source: Any = "", stage: str = "") -> Optional[str]:
    """candidate_id が無ければ打つ。既にあればそれを返す。

    entry は in-place で更新する (呼び出し側の dict にそのまま乗る)。
    """
    if not isinstance(entry, dict):
        return None
    if not enabled():
        return of(entry)
    src = source or entry.get("source") or entry.get("entry_type") or entry.get("pipeline_source") or ""
    cid = of(entry)
    if not cid:
        cid = new_id(entry.get("symbol"), src)
        entry[KEY] = cid
    # 既に上流 (Ranking 等) で採番済みでも、この段は必ず記録する。
    # ここを早期 return にしていたため pending_created が trace から
    # 抜け落ちていた (dry-run で検出)。
    if stage:
        note(cid, stage, symbol=entry.get("symbol"), source=src)
    return cid


def note(cid: Optional[str], stage: str, **info: Any) -> None:
    """1 段を記録してログに出す。"""
    if not cid or not enabled():
        return
    now = dt.datetime.now()
    with _LOCK:
        rec = _TRACE.get(cid)
        if rec is None:
            rec = []
            _TRACE[cid] = rec
        rec.append((str(stage), now, dict(info)))
        _TRACE.move_to_end(cid)
        while len(_TRACE) > _trace_max():
            _TRACE.popitem(last=False)
    tail = " ".join("%s=%s" % (k, v) for k, v in info.items() if v is not None)
    logger.warning("[CANDIDATE] id=%s stage=%s %s", cid, stage, tail)


def note_item(item: Any, stage: str, **info: Any) -> Optional[str]:
    cid = of(item)
    if cid:
        note(cid, stage, **info)
    return cid


# ============================================================
# P0-8: candidate state machine
# ------------------------------------------------------------
# 状態:
#   CREATED / EVALUATING / APPROVED / SUBMITTING
#   SUBMITTED / REJECTED / EXPIRED / CANCELLED
#
# terminal:
#   SUBMITTED / REJECTED / EXPIRED / CANCELLED
#
# terminal に到達した候補は「消えた」のではなく「決着した」ので、
# vanished 監査の対象外にする。
#
# 2026-08-28 の実測では
#   pending pruned symbol=6030 reason=RANKING_FINAL_NG:ATR_1M_FILTER_NG
# と明示的に棄却しているのに、その直後に
#   [CANDIDATE VANISHED] id=RANKING-6030-... reason=pending_moved_without_order
# が出ていた。これは vanish ではなく REJECTED。
# 同様に 3994 / 4443 / 4776 / 2590 も同じ理由だった。
# ============================================================

STATE_CREATED = "CREATED"
STATE_EVALUATING = "EVALUATING"
STATE_APPROVED = "APPROVED"
STATE_SUBMITTING = "SUBMITTING"
STATE_SUBMITTED = "SUBMITTED"
STATE_REJECTED = "REJECTED"
STATE_EXPIRED = "EXPIRED"
STATE_CANCELLED = "CANCELLED"

TERMINAL_STATES = frozenset({
    STATE_SUBMITTED, STATE_REJECTED, STATE_EXPIRED, STATE_CANCELLED,
})

# cid -> (state, reason, at)
_TERMINAL: "collections.OrderedDict[str, Tuple[str, str, dt.datetime]]" = collections.OrderedDict()


def mark_terminal(cid: Optional[str], state: str, *, reason: str = "", **info: Any) -> None:
    """候補を終局状態にする。terminal 後は vanished 監査の対象外。"""
    if not cid or not enabled():
        return
    st = str(state or "").strip().upper()
    if st not in TERMINAL_STATES:
        logger.warning(
            "[CANDIDATE] mark_terminal に terminal でない state が渡された id=%s state=%r "
            "-> REJECTED として扱う", cid, state,
        )
        st = STATE_REJECTED
    with _LOCK:
        if cid in _TERMINAL:
            return
        _TERMINAL[cid] = (st, str(reason or ""), dt.datetime.now())
        _TERMINAL.move_to_end(cid)
        while len(_TERMINAL) > _trace_max():
            _TERMINAL.popitem(last=False)
    note(cid, "terminal:%s" % st, reason=reason or "-", **info)


def mark_terminal_symbol(symbol: Any, state: str, *, reason: str = "", **info: Any) -> int:
    """symbol から未決着の候補を引いて terminal にする。

    pending_manager は candidate_id を持たずに symbol で prune するので、
    symbol -> cid の逆引きで決着させる。
    """
    if not enabled():
        return 0
    s = _norm_symbol(symbol)
    if not s:
        return 0
    hits = []
    with _LOCK:
        for cid in reversed(list(_TRACE.keys())):
            if cid in _TERMINAL:
                continue
            # id は "<SOURCE>-<SYMBOL>-<ts>-<seq>" 形式
            parts = str(cid).split("-")
            if len(parts) >= 2 and _norm_symbol(parts[1]) == s:
                hits.append(cid)
    for cid in hits:
        mark_terminal(cid, state, reason=reason, **info)
    return len(hits)


def terminal_state(cid: str) -> Optional[Tuple[str, str, dt.datetime]]:
    with _LOCK:
        return _TERMINAL.get(cid)


def is_terminal(cid: str) -> bool:
    with _LOCK:
        return cid in _TERMINAL


def terminal_stats() -> dict:
    with _LOCK:
        out: dict = {}
        for _cid, (st, _r, _at) in _TERMINAL.items():
            out[st] = out.get(st, 0) + 1
        return out


def trace(cid: str) -> List[Tuple[str, dt.datetime, dict]]:
    with _LOCK:
        return list(_TRACE.get(cid) or [])


def last_stage(cid: str) -> Optional[str]:
    rec = trace(cid)
    return rec[-1][0] if rec else None


def summary(cid: str) -> str:
    """その候補がたどった段を 1 行にする。"""
    rec = trace(cid)
    if not rec:
        return "-"
    t0 = rec[0][1]
    return ",".join("%s(+%.1fs)" % (s, (t - t0).total_seconds()) for s, t, _ in rec)


def log_vanished(cids, *, reason: str = "") -> List[str]:
    """pending から消えたのに発注へ到達していない候補を明示する。

    P0-8: terminal に到達済みの候補は「消えた」ではなく「決着した」ので
    vanished として扱わない。REJECTED / EXPIRED / CANCELLED / SUBMITTED は
    正常な結末であり、pending_moved_without_order ではない。

    Returns:
        本当に vanished だった cid のリスト。
    """
    if not enabled():
        return []
    real_vanished: List[str] = []
    for cid in cids or ():
        if not cid:
            continue
        term = terminal_state(cid)
        if term is not None:
            st, term_reason, at = term
            logger.info(
                "[CANDIDATE RESOLVED] id=%s terminal=%s terminal_reason=%s "
                "last_stage=%s stages=[%s] (vanish ではない)",
                cid, st, term_reason or "-", last_stage(cid), summary(cid),
            )
            continue
        rec = trace(cid)
        age = None
        if rec:
            age = (dt.datetime.now() - rec[0][1]).total_seconds()
        real_vanished.append(cid)
        logger.warning(
            "[CANDIDATE VANISHED] id=%s last_stage=%s age=%s reason=%s stages=[%s] "
            "(terminal 記録が無いまま pending/inflight の両方から消えた)",
            cid, last_stage(cid), None if age is None else round(age, 1),
            reason or "-", summary(cid),
        )
    return real_vanished


def collect_ids(container: Any) -> set:
    """pending スナップショット等から candidate_id を集める。

    構造がいろいろ (dict of list / list / dict of dict) なので緩く走査する。
    """
    out: set = set()

    def _walk(v: Any, depth: int = 0) -> None:
        if depth > 4 or v is None:
            return
        if isinstance(v, dict):
            cid = v.get(KEY)
            if cid:
                out.add(str(cid))
                return
            for x in v.values():
                _walk(x, depth + 1)
        elif isinstance(v, (list, tuple, set)):
            for x in v:
                _walk(x, depth + 1)

    try:
        _walk(container)
    except Exception:
        logger.debug("[CANDIDATE] collect_ids failed", exc_info=True)
    return out
