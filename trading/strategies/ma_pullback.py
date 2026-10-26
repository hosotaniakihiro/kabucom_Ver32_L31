# ============================================================
# File   : trading/strategies/ma_pullback.py
# 2026-10-26: 5 分足 25MA への押し目買い (MA_PULLBACK) — 記録だけ (発注しない)
# ------------------------------------------------------------
# 25MA より上に離れていた株価が 25MA まで下りてきて、その足が陽線で 25MA の上で引けたら買う想定で、
# シグナルと仮想の決済 (30 分・60 分) を記録する。実気配で取れるか確かめてから発注を足す (ユーザー判断 10/26)。
#
# シグナル (5 分足の確定値。1 分足は当日の summary DB、25MA は前日の 5 分足からつなぐ):
#   - 直前 12 本 (60 分) のどこかで 高値が 25MA から +2% 以上離れていた (far_pct を記録、3% 以上が本命)
#   - その足の安値が 25MA の +0.3% 以内まで近づいた
#   - その足が陽線 (終値 > 始値) で、終値が 25MA より上
#   - 足の終わりが 09:15〜14:30、200 円以上、同じ銘柄は 30 分あける
# 仮想約定: 売り気配で買い、買い気配で返す (PUSH の最新気配)。30 分後と 60 分後の損益を記録し、60 分で閉じる。
#
# 根拠 (2026-10-26 検証、9/11〜10/26 の 26 営業日、PUSH の実気配、スプレッド込み):
#   離れ 3%・30 分: 288 件 +0.59%、勝率 55%、プラス日 76%、前半 +0.74 / 後半 +0.51
#   離れ 3%・60 分: 259 件 +0.64%。離れ 2%・30 分: 523 件 +0.33%
#   対照 (決まった時刻に買う): -0.14%。次の足の高値抜けを待つ形は半分に落ちる。損切りを入れると悪くなる。
#   売り (戻り売り) は全部マイナスなので見ない。
#
# 状態は %LOCALAPPDATA%\autostock\runtime\ma_pullback_YYYYMMDD.json。
# 止め方: MA_PULLBACK_ENABLED=0。
# ============================================================
from __future__ import annotations

import datetime as dt
import glob
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from trading.strategies.surge_short import _env_f, _env_on, _hm, _ts, _pt

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()

MA_LEN = 25
LOOKBACK = 12


def cfg() -> Dict[str, Any]:
    return {
        "enabled": _env_on("MA_PULLBACK_ENABLED", True),
        "start": _hm("MA_PULLBACK_START", "09:15"),
        "end": _hm("MA_PULLBACK_END", "14:30"),
        "far_pct": _env_f("MA_PULLBACK_FAR_PCT", 2.0),
        "touch_pct": _env_f("MA_PULLBACK_TOUCH_PCT", 0.3),
        "min_price": _env_f("MA_PULLBACK_MIN_PRICE", 200.0),
        "cooldown_min": _env_f("MA_PULLBACK_COOLDOWN_MIN", 30.0),
        "hold_min": _env_f("MA_PULLBACK_HOLD_MIN", 60.0),
        "mid_min": _env_f("MA_PULLBACK_MID_MIN", 30.0),
        "max_quote_age": _env_f("MA_PULLBACK_MAX_QUOTE_AGE_SEC", 30.0),
        "force_close": _hm("MA_PULLBACK_FORCE_CLOSE", "15:20"),
    }


def _summary_dir() -> str:
    try:
        from nas_root import nas_path
        base = nas_path(r"raw_data\kabu_station\summary")
    except Exception:
        base = os.path.join(os.environ.get("LOCALAPPDATA") or str(Path.home()),
                            "autostock", "nas_local", "raw_data", "kabu_station", "summary")
    return os.getenv("SUMMARY_DB_DIR", str(base))


def _state_path(day: Optional[dt.date] = None) -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = (day or dt.date.today()).strftime("%Y%m%d")
    p = Path(base) / "autostock" / "runtime" / f"ma_pullback_{d}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> Dict[str, Any]:
    p = _state_path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[MA PULLBACK] state read failed path=%s", p, exc_info=True)
    return {"done": {}, "trades": []}


def _save(st: Dict[str, Any]) -> None:
    p = _state_path()
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        logger.warning("[MA PULLBACK] state write failed path=%s", p, exc_info=True)


def _ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=3)


def _to5(rows) -> Dict[str, List[list]]:
    """1 分足 (symbol, datetime, open, high, low, close) -> sym -> [[HH:MM, o, h, l, c], ...] (5 分の頭)。
    始値は 1 分足の open が欠けることがあるので、最初の 1 分足の終値を使う (検証と同じ)。"""
    out: Dict[str, Dict[str, list]] = {}
    for sym, t, _o, h, l, c in rows:
        if c is None or h is None or l is None:
            continue
        s = str(t)
        hh, mm = int(s[11:13]), int(s[14:16])
        key = f"{hh:02d}:{mm - mm % 5:02d}"
        b = out.setdefault(str(sym), {}).get(key)
        if b is None:
            out[str(sym)][key] = [key, float(c), float(h), float(l), float(c)]
        else:
            b[2] = max(b[2], float(h)); b[3] = min(b[3], float(l)); b[4] = float(c)
    return {s: [m[k] for k in sorted(m)] for s, m in out.items()}


class _Feed:
    """当日の 1 分足 (summary DB) を 5 分足にまとめ、最新の気配 (push DB) を持つ。前日の 5 分足は銘柄ごとに 1 回読む。"""

    def __init__(self) -> None:
        self.day: Optional[str] = None
        self.min1: Dict[str, Dict[str, tuple]] = {}   # sym -> {datetime: (o,h,l,c)}
        self.last_dt = ""
        self.last_rowid = 0
        self.quote: Dict[str, Dict[str, Any]] = {}
        self.prev: Dict[str, List[list]] = {}
        self.prev_path: Optional[str] = None

    def _reset(self, day: str) -> None:
        self.day, self.min1, self.last_dt, self.last_rowid, self.quote, self.prev = day, {}, "", 0, {}, {}
        cands = sorted(p for p in glob.glob(os.path.join(_summary_dir(), "summary2*.db"))
                       if os.path.basename(p)[7:15] < day)
        self.prev_path = cands[-1] if cands else None

    def update(self, now: dt.datetime) -> None:
        day = now.strftime("%Y%m%d")
        if self.day != day:
            self._reset(day)
        path = os.path.join(_summary_dir(), f"summary{day}.db")
        if os.path.exists(path):
            since = self.last_dt or f"{now:%Y-%m-%d} 09:00:00"
            c = _ro(path)
            try:
                # 直近の分は書き直されるので、最後に読んだ分から読み直す (datetime の index が効く)
                rows = c.execute("select symbol, datetime, open, high, low, close from stock_summary_1min"
                                 " where datetime>=? and datetime<? order by datetime",
                                 (since, f"{now:%Y-%m-%d} 15:31:00")).fetchall()
            finally:
                c.close()
            for sym, t, o, h, l, cl in rows:
                self.min1.setdefault(str(sym), {})[str(t)[:19]] = (o, h, l, cl)
                if str(t) > self.last_dt:
                    self.last_dt = str(t)[:19]
        self._update_quotes(now)

    def _update_quotes(self, now: dt.datetime) -> None:
        from scheduler_jobs.summary.memory_latest_1m import _push_db_path
        path = str(_push_db_path())
        if not os.path.exists(path):
            return
        c = _ro(path)
        try:
            if self.last_rowid == 0:
                # 初回 (再起動後) は直近 2 分だけ。気配は最新しか使わない
                self.last_rowid = max(0, int(c.execute("select max(rowid) from stream_data_raw").fetchone()[0] or 0) - 20000)
            rows = c.execute("select rowid, symbol, datetime, Sell1Price, Buy1Price from stream_data_raw"
                             " where rowid>? order by rowid limit 400000", (self.last_rowid,)).fetchall()
        finally:
            c.close()
        if rows:
            self.last_rowid = int(rows[-1][0])
        for _rid, sym, ts, ask, bid in rows:
            if ask and bid and float(ask) >= float(bid) > 0:
                self.quote[str(sym)] = {"ask": float(ask), "bid": float(bid), "ts": str(ts)[:19].replace("T", " ")}

    def bars5(self, sym: str) -> List[list]:
        m = self.min1.get(sym) or {}
        return _to5((sym, t, *v) for t, v in sorted(m.items()))[sym] if m else []

    def prev5(self, sym: str) -> List[list]:
        if sym in self.prev:
            return self.prev[sym]
        out: List[list] = []
        if self.prev_path:
            try:
                c = _ro(self.prev_path)
                try:
                    rows = c.execute("select symbol, datetime, open, high, low, close from stock_summary_1min"
                                     " where symbol=? order by datetime", (sym,)).fetchall()
                finally:
                    c.close()
                out = (_to5(rows).get(sym) or [])[-MA_LEN:]
            except sqlite3.DatabaseError:
                logger.debug("[MA PULLBACK] prev summary unreadable path=%s", self.prev_path, exc_info=True)
        self.prev[sym] = out
        return out

    def age(self, sym: str, now: dt.datetime) -> float:
        try:
            return (now - _pt(self.quote[sym]["ts"])).total_seconds()
        except Exception:
            return 1e9


_FEED = _Feed()


def detect(prev: List[list], today: List[list], i: int, c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """today[i] (確定した 5 分足) で押し目買いの形ができたか。prev は前日の 5 分足 (MA のつなぎ用)。"""
    full = prev + today[: i + 1]
    n = len(full)
    if n < MA_LEN + LOOKBACK:
        return None
    closes = [b[4] for b in full]
    ma = [None] * n
    s = sum(closes[:MA_LEN])
    ma[MA_LEN - 1] = s / MA_LEN
    for k in range(MA_LEN, n):
        s += closes[k] - closes[k - MA_LEN]
        ma[k] = s / MA_LEN
    _, o, h, l, cl = full[-1]
    m = ma[-1]
    if cl < c["min_price"] or l > m * (1 + c["touch_pct"] / 100) or not (cl > o and cl > m):
        return None
    far = max((full[k][2] / ma[k] - 1) * 100 for k in range(n - 1 - LOOKBACK, n - 1) if ma[k])
    if far < c["far_pct"]:
        return None
    return {"ma25": round(m, 2), "far_pct": round(far, 2), "open": o, "high": h, "low": l, "close": cl}


def run_cycle(now: Optional[dt.datetime] = None) -> None:
    c = cfg()
    if not c["enabled"]:
        return
    now = now or dt.datetime.now()
    if not (dt.time(9, 0) <= now.time() <= c["force_close"]):
        return
    with _LOCK:
        st = _load()
        done: Dict[str, List[str]] = st.setdefault("done", {})
        trades: List[Dict[str, Any]] = st.setdefault("trades", [])
        changed = False
        try:
            _FEED.update(now)
        except Exception:
            logger.debug("[MA PULLBACK] feed update failed", exc_info=True)
            return
        cur_key = f"{now.hour:02d}:{now.minute - now.minute % 5:02d}"
        last_sig = {t["symbol"]: t["entry_ts"] for t in trades}
        # 1) 確定した 5 分足を見る (気配がある = PUSH 登録中の銘柄だけ。発注するなら同じ条件になる)
        for sym in list(_FEED.quote):
            today = _FEED.bars5(sym)
            seen = set(done.get(sym) or [])
            for i, b in enumerate(today):
                k = b[0]
                if k >= cur_key or k in seen:
                    continue
                done.setdefault(sym, []).append(k); changed = True
                bar_end = (dt.datetime.strptime(k, "%H:%M") + dt.timedelta(minutes=5)).time()
                if not (c["start"] <= bar_end <= c["end"]):
                    continue
                if (now - dt.datetime.combine(now.date(), bar_end)).total_seconds() > 120:
                    continue  # 再起動直後の古い足では入らない
                sig = detect(_FEED.prev5(sym), today, i, c)
                if not sig:
                    continue
                if sym in last_sig and (now - _pt(last_sig[sym])).total_seconds() < c["cooldown_min"] * 60:
                    continue
                q = _FEED.quote.get(sym) or {}
                age = _FEED.age(sym, now)
                if age > c["max_quote_age"]:
                    logger.warning("[MA PULLBACK] SKIP symbol=%s bar=%s reason=quote_stale age=%.0fs", sym, k, age)
                    continue
                ask, bid = q["ask"], q["bid"]
                t = dict(symbol=sym, bar=k, entry_ts=_ts(now), entry=ask, bid=bid,
                         spread_pct=round((ask / bid - 1) * 100, 3), quote_age=round(age, 1),
                         prev_bars=len(_FEED.prev5(sym)), status="open", **sig)
                trades.append(t); last_sig[sym] = t["entry_ts"]; changed = True
                logger.warning("[MA PULLBACK] SIGNAL (record only) symbol=%s bar=%s close=%.1f ma25=%.1f far=%.2f%% "
                               "ask=%.1f bid=%.1f spread=%.3f%%", sym, k, sig["close"], sig["ma25"], sig["far_pct"],
                               ask, bid, t["spread_pct"])
        # 2) 仮想決済 (買い気配で返す)
        for t in trades:
            if t.get("status") != "open":
                continue
            q = _FEED.quote.get(t["symbol"]) or {}
            bid = float(q.get("bid") or 0)
            held = (now - _pt(t["entry_ts"])).total_seconds() / 60.0
            if bid <= 0:
                continue
            pnl = (bid / float(t["entry"]) - 1) * 100
            if held >= c["mid_min"] and "pnl_mid" not in t:
                t.update(pnl_mid=round(pnl, 3), mid_ts=_ts(now), mid_quote_age=round(_FEED.age(t["symbol"], now), 1))
                changed = True
            why = "TIME" if held >= c["hold_min"] else ("FORCE_CLOSE" if now.time() >= c["force_close"] else None)
            if why:
                t.update(status="closed", exit_ts=_ts(now), exit=bid, exit_reason=why, pnl_pct=round(pnl, 3),
                         held_min=round(held, 1), exit_quote_age=round(_FEED.age(t["symbol"], now), 1))
                changed = True
                logger.warning("[MA PULLBACK] VIRTUAL EXIT symbol=%s reason=%s entry=%.1f exit=%.1f pnl=%+.2f%% "
                               "pnl_%dm=%s held=%.1fm", t["symbol"], why, t["entry"], bid, pnl, int(c["mid_min"]),
                               t.get("pnl_mid"), held)
        if changed:
            _save(st)


def _run_guarded() -> None:
    try:
        from core.startup.scheduler.market_guard import is_market_time_for_entry
        if not is_market_time_for_entry():
            return
        run_cycle()
    except Exception:
        logger.exception("[MA PULLBACK] cycle failed")


def register_scheduler_safe() -> bool:
    try:
        import schedule
        from core.startup.scheduler.helpers import has_schedule_tag
        if has_schedule_tag("ma_pullback"):
            return True
        every = int(_env_f("MA_PULLBACK_INTERVAL_SEC", 20))
        schedule.every(every).seconds.do(_run_guarded).tag("ma_pullback", "entry")
        logger.warning("[MA PULLBACK] scheduler registered (record only) every=%ss cfg=%s", every,
                       {k: (str(v) if isinstance(v, dt.time) else v) for k, v in cfg().items()})
        return True
    except Exception:
        logger.exception("[MA PULLBACK] scheduler register failed")
        return False


__all__ = ["run_cycle", "register_scheduler_safe", "cfg", "detect"]
