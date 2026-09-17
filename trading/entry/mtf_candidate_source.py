# ============================================================
# trading/entry/mtf_candidate_source.py
# ------------------------------------------------------------
# 2026-09-16 (B6): 3 分足・5 分足から直接エントリー候補を作る。
#
# 背景:
#   候補は 1 分足 summary のスコアリングからしか出ていなかった。9/15〜16 は
#   3m/5m ゲート (trading/filters/mtf_ma_gate) を通る銘柄があっても、
#   1 分足の点数が足りず候補にならない / 候補になった時には 3m/5m が
#   崩れている、が続いた。ユーザー要件は「3 分足・5 分足の 5MA/25MA
#   (+RSI/MACD) をもとにエントリー」なので、候補の出所も 3m/5m に寄せる。
#
# 動き:
#   - main.py が MTF_CANDIDATE_SCAN_INTERVAL_SEC (既定 60 秒) ごとに scan() を呼ぶ
#   - 対象はローカル push DB に直近 MTF_CANDIDATE_ACTIVE_MIN 分以内のティックがある銘柄
#     (= 今 PUSH 登録されている ≤50 銘柄)
#   - BUY / SELL それぞれについて
#       1) mtf_ma_gate (3m/5m の 終値>5MA>25MA + RSI + MACD、ANY)
#       2) 鮮度: 3 分足の並びが直近 MTF_CANDIDATE_FRESH_BARS 本以内に成立した
#          (ずっと成立している = 伸び切った銘柄を追わない)
#       3) mtf_score_gate (score_config.ini の 3m/5m 点数)
#     を通ったものを pending に入れる (source=SUMMARY_AI、既存の
#     entry pipeline がそのまま拾う。pipeline 側のゲート・AI・板・余力は従来どおり)
#   - 同じ銘柄・同じ side は MTF_CANDIDATE_COOLDOWN_MIN 分あけ、建玉/pending がある
#     銘柄は出さない
#
# 環境変数:
#   MTF_CANDIDATE_SOURCE_ENABLED   1/0 (既定 1)
#   MTF_CANDIDATE_SCAN_INTERVAL_SEC 既定 60
#   MTF_CANDIDATE_ACTIVE_MIN        既定 3  (直近何分にティックがある銘柄を対象にするか)
#   MTF_CANDIDATE_FRESH_BARS        既定 4  (0 で鮮度条件なし)
#   MTF_CANDIDATE_COOLDOWN_MIN      既定 10
#   MTF_CANDIDATE_MAX_PER_SCAN      既定 5
#   MTF_CANDIDATE_TTL_SEC           既定 180 (pending の expire_at)
#   MTF_CANDIDATE_START / END       既定 09:15 / 14:50
#   MTF_CANDIDATE_SIDES             既定 BUY,SELL
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

VERSION = "V1.0-MTF-CANDIDATE-SOURCE-3M5M"

_LOCK = threading.Lock()
_LAST_EMIT: Dict[Tuple[str, str], float] = {}
_THREAD_STARTED = False
_STATS: Dict[str, Any] = {"scans": 0, "emitted": 0, "last_scan": None, "last_emitted": []}


# ------------------------------------------------------------
# env
# ------------------------------------------------------------
def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off")


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, float(default)))


def _env_time(name: str, default: str) -> dt.time:
    s = (os.getenv(name) or default).strip()
    try:
        hh, mm = s.split(":")[:2]
        return dt.time(int(hh), int(mm))
    except Exception:
        hh, mm = default.split(":")[:2]
        return dt.time(int(hh), int(mm))


def enabled() -> bool:
    return _env_bool("MTF_CANDIDATE_SOURCE_ENABLED", True)


def _sides() -> List[str]:
    raw = os.getenv("MTF_CANDIDATE_SIDES", "BUY,SELL")
    out = [x.strip().upper() for x in raw.split(",") if x.strip()]
    return [x for x in out if x in ("BUY", "SELL")] or ["BUY", "SELL"]


# ------------------------------------------------------------
# 対象銘柄 (ローカル push DB に直近ティックがある銘柄)
# ------------------------------------------------------------
def _active_symbols(now: dt.datetime, minutes: int) -> List[str]:
    try:
        from trading.summary.mtf_local_bars import _push_db_path
        p = _push_db_path(now.date())
    except Exception:
        p = None
    if p is None:
        return []
    since = (now - dt.timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")
    until = now.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1.0)
        try:
            con.execute("PRAGMA busy_timeout=1000")
            rows = con.execute(
                "SELECT DISTINCT symbol FROM stream_data_raw WHERE datetime >= ? AND datetime <= ?", (since, until)
            ).fetchall()
        finally:
            con.close()
    except Exception:
        logger.debug("[MTF CANDIDATE] active symbols read failed", exc_info=True)
        return []
    out: List[str] = []
    for (s,) in rows:
        s = str(s or "").strip()
        if s.endswith(".0"):
            s = s[:-2]
        if s:
            out.append(s)
    return sorted(set(out))


# ------------------------------------------------------------
# 除外: 建玉あり / pending あり / クールダウン
# ------------------------------------------------------------
def _open_symbols() -> set:
    try:
        from trading.position.position_state_service import get_open_positions
        pos = get_open_positions(caller="mtf_candidate_source") or {}
        out = set()
        for k, v in pos.items():
            s = str((v or {}).get("symbol") or k or "").strip()
            if s.endswith(".0"):
                s = s[:-2]
            if s:
                out.add(s)
        return out
    except Exception:
        logger.debug("[MTF CANDIDATE] open positions read failed", exc_info=True)
        return set()


def _has_pending(symbol: str) -> bool:
    try:
        from trading.entry.pending_manager import get_bucket
        return bool(get_bucket(symbol))
    except Exception:
        return False


def _in_cooldown(symbol: str, side: str, now_ts: float) -> bool:
    cd = _env_float("MTF_CANDIDATE_COOLDOWN_MIN", 10.0) * 60.0
    with _LOCK:
        last = _LAST_EMIT.get((symbol, side))
    return last is not None and (now_ts - last) < cd


# ------------------------------------------------------------
# 鮮度: 3 分足の並びが直近 N 本以内に成立したか
# ------------------------------------------------------------
def _aligned(row: pd.Series, side: str) -> bool:
    try:
        c, m5, m25 = float(row["close"]), float(row["ma5"]), float(row["ma25"])
    except Exception:
        return False
    if side == "BUY":
        return c > m5 > m25
    return c < m5 < m25


def _fresh_alignment(symbol: str, side: str, now: dt.datetime, fresh_bars: int) -> Tuple[Optional[bool], str]:
    """戻り値 (判定, 説明)。判定 None = データ不足。"""
    try:
        from trading.summary.mtf_local_bars import get_bars, is_stale
        bars = get_bars(symbol, 3, now)
    except Exception:
        logger.debug("[MTF CANDIDATE] bars failed symbol=%s", symbol, exc_info=True)
        return None, "bars_error"
    if bars is None or bars.empty:
        return None, "no_bars"
    if is_stale(bars, 3, now):
        return None, "stale"
    done = bars[bars["completed"]] if "completed" in bars.columns else bars
    if len(done) < 2:
        return None, "few_bars"
    last = done.iloc[-1]
    if not _aligned(last, side):
        return False, "last_not_aligned"
    if fresh_bars <= 0:
        return True, "aligned"
    # 直近 fresh_bars 本 (最後の 1 本を除く) のどれかが未成立なら「成立したて」
    prev = done.iloc[max(0, len(done) - 1 - fresh_bars): len(done) - 1]
    if prev.empty:
        return True, "aligned_first_bars"
    if all(_aligned(r, side) for _, r in prev.iterrows()):
        return False, f"aligned_too_long(>{fresh_bars}bars)"
    n_since = 0
    for _, r in prev.iloc[::-1].iterrows():
        if _aligned(r, side):
            n_since += 1
        else:
            break
    return True, f"fresh(bars_since_align={n_since + 1})"


# ------------------------------------------------------------
# 1 回の走査
# ------------------------------------------------------------
def _time_ok(now: dt.datetime) -> Tuple[bool, str]:
    try:
        from utils.market_session import is_market_open
        if not is_market_open(now):
            return False, "market_closed"
    except Exception:
        pass
    t = now.time()
    if t < _env_time("MTF_CANDIDATE_START", "09:15"):
        return False, "before_start"
    if t >= _env_time("MTF_CANDIDATE_END", "14:50"):
        return False, "after_end"
    return True, "ok"


def scan(now: Optional[dt.datetime] = None) -> Dict[str, Any]:
    now = now or dt.datetime.now()
    out: Dict[str, Any] = {"enabled": enabled(), "checked": 0, "emitted": [], "reasons": {}, "skipped": ""}
    if not enabled():
        out["skipped"] = "disabled"
        return out
    ok, why = _time_ok(now)
    if not ok:
        out["skipped"] = why
        return out

    from trading.filters.mtf_ma_gate import mtf_ma_gate
    from trading.filters.mtf_score_gate import mtf_score_gate, last_scores
    from trading.entry.pending_manager import add_pending

    fresh_bars = _env_int("MTF_CANDIDATE_FRESH_BARS", 4)
    max_emit = _env_int("MTF_CANDIDATE_MAX_PER_SCAN", 5)
    ttl = _env_float("MTF_CANDIDATE_TTL_SEC", 180.0)
    now_ts = time.time()
    symbols = _active_symbols(now, _env_int("MTF_CANDIDATE_ACTIVE_MIN", 3))
    open_syms = _open_symbols()
    reasons: Dict[str, int] = {}

    def _bump(k: str) -> None:
        reasons[k] = reasons.get(k, 0) + 1

    for sym in symbols:
        if len(out["emitted"]) >= max_emit:
            _bump("max_per_scan")
            break
        if sym in open_syms:
            _bump("open_position")
            continue
        if _has_pending(sym):
            _bump("pending_exists")
            continue
        for side in _sides():
            out["checked"] += 1
            if _in_cooldown(sym, side, now_ts):
                _bump("cooldown")
                continue
            g_ok, g_reason = mtf_ma_gate(sym, side, None)
            if not g_ok:
                _bump("ma_gate_ng")
                continue
            f_ok, f_reason = _fresh_alignment(sym, side, now, fresh_bars)
            if f_ok is not True:
                _bump(f"fresh_ng:{f_reason.split('(')[0]}")
                continue
            s_ok, s_reason = mtf_score_gate(sym, side)
            if not s_ok:
                _bump("score_gate_ng")
                continue
            scores = {}
            try:
                scores = last_scores(sym, side) or {}
            except Exception:
                pass
            price = 0.0
            try:
                from trading.summary.mtf_local_bars import latest_bar
                lb = latest_bar(sym, 3, now)
                price = float(lb.get("close") or 0.0) if lb is not None else 0.0
            except Exception:
                price = 0.0
            # last_scores は {"3m": score, "5m": score}。pipeline の score lift と同じく最小値を採る。
            try:
                score_total = float(min(float(v) for v in scores.values())) if scores else 0.0
            except Exception:
                score_total = 0.0
            entry = {
                "symbol": sym,
                "side": side,
                "entry_decision": side,
                "ai_side": side,
                "source": "SUMMARY_AI",
                "entry_type": "SUMMARY_AI",
                "interval": 1,
                "score": score_total,
                "score_total": score_total,
                "score_buy": score_total if side == "BUY" else 0.0,
                "score_sell": score_total if side == "SELL" else 0.0,
                "mtf_ini_score": score_total,
                "mtf_scores": dict(scores),
                "price": price,
                "close_price": price,
                "current_price": price,
                "datetime": now,
                "created_at": now,
                "candidate_origin": "MTF_CANDIDATE_SOURCE",
                "entry_reason": f"MTF_3M5M_ALIGN {g_reason} {f_reason} {s_reason}",
                "entry_conditions": {"expire_at": now + dt.timedelta(seconds=ttl), "price": price},
            }
            try:
                added = bool(add_pending(entry))
            except Exception:
                logger.exception("[MTF CANDIDATE] add_pending failed symbol=%s side=%s", sym, side)
                added = False
            with _LOCK:
                _LAST_EMIT[(sym, side)] = now_ts
            if added:
                out["emitted"].append({"symbol": sym, "side": side, "score": score_total, "price": price})
                logger.warning(
                    "[MTF CANDIDATE] emit symbol=%s side=%s price=%s score=%.2f ma=%s fresh=%s ini=%s",
                    sym, side, price, score_total, g_reason, f_reason, s_reason,
                )
            else:
                _bump("add_pending_false")
                logger.info("[MTF CANDIDATE] add_pending rejected symbol=%s side=%s", sym, side)
            break  # 同じ銘柄で BUY/SELL 両方は出さない

    out["reasons"] = reasons
    out["symbols"] = len(symbols)
    with _LOCK:
        _STATS["scans"] += 1
        _STATS["emitted"] += len(out["emitted"])
        _STATS["last_scan"] = now
        _STATS["last_emitted"] = list(out["emitted"])
    logger.info(
        "[MTF CANDIDATE] scan symbols=%s checked=%s emitted=%s reasons=%s",
        len(symbols), out["checked"], len(out["emitted"]), reasons,
    )
    return out


def stats() -> Dict[str, Any]:
    with _LOCK:
        return dict(_STATS)


# ------------------------------------------------------------
# main.py 用スレッド
# ------------------------------------------------------------
def start_background_scanner() -> bool:
    global _THREAD_STARTED
    if _THREAD_STARTED or not enabled():
        return False
    _THREAD_STARTED = True
    # 2026-09-17 実測: 本番プロセス内では 1 走査 45〜75 秒 (94 銘柄、GIL 競合) で、走査中は
    #   1 分 summary の tier1 が 2 秒→9 秒に伸びた。既定を 30→60 秒にして重なりを減らす。
    interval = max(10.0, _env_float("MTF_CANDIDATE_SCAN_INTERVAL_SEC", 60.0))

    def _loop():
        # 起動直後は push DB がまだ薄いので少し待つ
        time.sleep(60.0)
        while True:
            t0 = time.time()
            try:
                scan()
            except Exception:
                logger.exception("[MTF CANDIDATE] scan failed")
            time.sleep(max(5.0, interval - (time.time() - t0)))

    threading.Thread(target=_loop, daemon=True, name="mtf_candidate_source").start()
    logger.warning("[MTF CANDIDATE] scanner started interval=%.0fs version=%s", interval, VERSION)
    return True
