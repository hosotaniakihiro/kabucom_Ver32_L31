# ============================================================
# File   : trading/strategies/selling_climax.py
# 2026-10-06: セリングクライマックスの買い (SELLING_CLIMAX)
# ------------------------------------------------------------
# 出来高を伴う投げ売りの急落を見つけて、その足の確定直後に買い、反発を取る。
# 順張りの通常経路 (3分/5分足の MACD・RSI 確認) は急落直後を必ず落とすので、独立した経路にする。
#
# シグナル (確定した 1 分足):
#   - 直近 30 分の終値の高値から 2% 以上下落
#   - その 1 分で 1% 以上下げた (前の足の終値比)
#   - 出来高が直前 20 本平均の 3 倍以上
# 決済: 買値 -1% で損切り / 30 分 / 15:20。通常の決済ルール (トレール等) は通さない。
#
# 根拠 (2026-10-06 検証、backtest.db 1 分足 8/17〜9/25、売買代金 10 億円以上、終値のみ):
#   328 回 (1 銘柄 1 日 1 回)、損切りのすべり 0.2% と往復コスト 0.1% を引いて +0.36%、勝率 51%、
#   前半 12 日 +0.63% / 後半 12 日 +0.28%。同じ時間帯に条件なしで買うと ±0%。
#   10/6 の 593A (13:06、出来高 24.8 倍) は 30 分後 +1.03%。
#   期間外 (push 実気配 9/26〜) の確認は未了。まず少量 (既定 100 株) で動かす。
#
# 構成:
#   run_cycle()   : 20 秒ごと。push DB を rowid で差分読みし、銘柄ごとの 1 分足 (終値・出来高) を作って判定。
#   handle_exit() : exit_loop の先頭で呼ぶ。この戦略の建玉なら 損切り/時間 だけで決済する。
#   状態は %LOCALAPPDATA%\autostock\runtime\selling_climax_YYYYMMDD.json (再起動しても残る)。
#
# 止め方: SELLING_CLIMAX_ENABLED=0 (新規だけ止まる。保有中の決済は続く)。
# ============================================================
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()


def _env_f(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _env_on(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() not in ("0", "false", "no", "off")


def _hm(name: str, default: str) -> dt.time:
    s = str(os.getenv(name) or default)
    h, m = [int(x) for x in s.split(":")[:2]]
    return dt.time(h, m)


def cfg() -> Dict[str, Any]:
    return {
        "enabled": _env_on("SELLING_CLIMAX_ENABLED", True),
        "start": _hm("SELLING_CLIMAX_START", "09:15"),
        "end": _hm("SELLING_CLIMAX_END", "14:30"),
        "drop30_pct": _env_f("SELLING_CLIMAX_DROP30_PCT", 2.0),
        "bar_drop_pct": _env_f("SELLING_CLIMAX_BAR_DROP_PCT", 1.0),
        "vol_mult": _env_f("SELLING_CLIMAX_VOL_MULT", 3.0),
        "stop_pct": _env_f("SELLING_CLIMAX_STOP_PCT", 1.0),
        "hold_min": _env_f("SELLING_CLIMAX_HOLD_MIN", 30.0),
        "qty": int(_env_f("SELLING_CLIMAX_QTY", 100)),
        "max_yen": _env_f("SELLING_CLIMAX_MAX_YEN", 500000.0),
        "min_price": _env_f("SELLING_CLIMAX_MIN_PRICE", 200.0),
        "max_spread_pct": _env_f("SELLING_CLIMAX_MAX_SPREAD_PCT", 0.20),
        "max_per_day": int(_env_f("SELLING_CLIMAX_MAX_PER_DAY", 10)),
        "max_open": int(_env_f("SELLING_CLIMAX_MAX_OPEN", 2)),
        # 足の確定からこの秒数を過ぎたシグナルは捨てる (反発に乗り遅れた買いをしない)
        "max_signal_age_sec": _env_f("SELLING_CLIMAX_MAX_SIGNAL_AGE_SEC", 45.0),
        "force_close": _hm("SELLING_CLIMAX_FORCE_CLOSE", "15:20"),
    }


# ------------------------------------------------------------
# 状態 (日付ごとの JSON)
# ------------------------------------------------------------
def _state_path(day: Optional[dt.date] = None) -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = (day or dt.date.today()).strftime("%Y%m%d")
    p = Path(base) / "autostock" / "runtime" / f"selling_climax_{d}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> Dict[str, Any]:
    p = _state_path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[SELLING CLIMAX] state read failed path=%s", p, exc_info=True)
    return {"signals": {}, "trades": {}}


def _save(st: Dict[str, Any]) -> None:
    p = _state_path()
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        logger.warning("[SELLING CLIMAX] state write failed path=%s", p, exc_info=True)


def _ts(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _pt(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------
# 1 分足 (push DB の差分読み)
#   push の price はティック価格、volume は当日累計。high/low 列は当日高安なので使わない。
#   足 = その分の最後の price / 最後の累計出来高、足の出来高 = 累計の差。
# ------------------------------------------------------------
class _Bars:
    def __init__(self) -> None:
        self.day: Optional[str] = None
        self.path: Optional[str] = None
        self.last_rowid = 0
        # symbol -> {minute(datetime): [close, cumvol]}
        self.bars: Dict[str, Dict[dt.datetime, List[float]]] = {}

    def _reset(self, day: str, path: str) -> None:
        self.day, self.path, self.last_rowid, self.bars = day, path, 0, {}

    @staticmethod
    def _parse(s: str) -> Optional[dt.datetime]:
        try:
            return dt.datetime.strptime(str(s)[:16].replace("T", " "), "%Y-%m-%d %H:%M")
        except Exception:
            return None

    def _start_rowid(self, c: sqlite3.Connection, since: dt.datetime) -> int:
        """rowid は時刻順に増えるので、二分探索で since 以降の最初の rowid を探す (起動直後の全件読みを避ける)。"""
        hi = int(c.execute("select max(rowid) from stream_data_raw").fetchone()[0] or 0)
        lo = 1
        key = since.strftime("%Y-%m-%dT%H:%M")
        while lo < hi:
            mid = (lo + hi) // 2
            r = c.execute("select datetime from stream_data_raw where rowid>=? order by rowid limit 1", (mid,)).fetchone()
            if r is None or str(r[0])[:16] >= key:
                hi = mid
            else:
                lo = mid + 1
        return max(lo - 1, 0)

    def update(self, now: dt.datetime) -> None:
        from scheduler_jobs.summary.memory_latest_1m import _push_db_path
        path = str(_push_db_path())
        day = now.strftime("%Y%m%d")
        if self.day != day or self.path != path:
            self._reset(day, path)
        if not os.path.exists(path):
            return
        c = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=3)
        try:
            if self.last_rowid == 0:
                self.last_rowid = self._start_rowid(c, now - dt.timedelta(minutes=40))
            rows = c.execute(
                "select rowid, symbol, datetime, price, volume from stream_data_raw where rowid>? order by rowid limit 400000",
                (self.last_rowid,)).fetchall()
        finally:
            c.close()
        for rowid, sym, ts, price, vol in rows:
            self.last_rowid = rowid
            if not price or price <= 0:
                continue
            m = self._parse(ts)
            if m is None:
                continue
            b = self.bars.setdefault(str(sym), {}).setdefault(m, [0.0, 0.0])
            b[0] = float(price)
            if vol and float(vol) > b[1]:
                b[1] = float(vol)
        # 古い足を捨てる
        cutoff = now.replace(second=0, microsecond=0) - dt.timedelta(minutes=45)
        for sym, d in self.bars.items():
            for k in [k for k in d if k < cutoff]:
                del d[k]

    def series(self, sym: str, upto: dt.datetime) -> List[Tuple[dt.datetime, float, float]]:
        """upto (含まない) までの確定足 [(minute, close, bar_volume)]。出来高は累計の差 (先頭は捨てる)。"""
        d = self.bars.get(sym) or {}
        ks = sorted(k for k in d if k < upto)
        out = []
        prev_cum = None
        for k in ks:
            close, cum = d[k]
            if prev_cum is not None and cum >= prev_cum > 0:
                out.append((k, close, cum - prev_cum))
            if cum > 0:
                prev_cum = cum
        return out


_BARS = _Bars()


def detect(series: List[Tuple[dt.datetime, float, float]], c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """最後の確定足がクライマックスなら詳細を返す。"""
    if len(series) < 21:
        return None
    t, close, vol = series[-1]
    prev_close = series[-2][1]
    closes30 = [x[1] for x in series[-31:-1]]
    vols20 = [x[2] for x in series[-21:-1]]
    if len(closes30) < 10 or prev_close <= 0:
        return None
    high30 = max(closes30)
    avg20 = sum(vols20) / len(vols20)
    bar_ret = (close / prev_close - 1) * 100
    drop30 = (1 - close / high30) * 100
    vol_x = vol / avg20 if avg20 > 0 else 0.0
    ok = drop30 >= c["drop30_pct"] and bar_ret <= -c["bar_drop_pct"] and vol_x >= c["vol_mult"]
    if not ok:
        return None
    return {"bar": _ts(t), "close": close, "bar_ret": round(bar_ret, 2), "drop30": round(drop30, 2),
            "vol_x": round(vol_x, 1), "vol": vol, "high30": high30}


# ------------------------------------------------------------
# 新規
# ------------------------------------------------------------
def _board(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        from trading.handlers.entry_order_builder import _rfb_fetch_board
        return _rfb_fetch_board(symbol, force=True)
    except Exception:
        logger.debug("[SELLING CLIMAX] board failed symbol=%s", symbol, exc_info=True)
        return None


def _open_count(st: Dict[str, Any], now: dt.datetime, hold_min: float) -> int:
    n = 0
    for t in (st.get("trades") or {}).values():
        if t.get("closed"):
            continue
        try:
            if now - _pt(t["order_ts"]) <= dt.timedelta(minutes=hold_min + 10):
                n += 1
        except Exception:
            pass
    return n


def run_cycle(now: Optional[dt.datetime] = None) -> None:
    c = cfg()
    now = now or dt.datetime.now()
    try:
        _BARS.update(now)
    except Exception:
        logger.warning("[SELLING CLIMAX] bars update failed", exc_info=True)
        return
    if not c["enabled"] or not (c["start"] <= now.time() <= c["end"]):
        return
    cur_min = now.replace(second=0, microsecond=0)
    # 確定したばかりの足だけを見る (足の終わり = cur_min)
    if (now - cur_min).total_seconds() > c["max_signal_age_sec"]:
        return
    with _LOCK:
        st = _load()
        changed = False
        for sym in list(_BARS.bars.keys()):
            ser = _BARS.series(sym, cur_min)
            if not ser or ser[-1][0] != cur_min - dt.timedelta(minutes=1):
                continue
            sig = detect(ser, c)
            if not sig:
                continue
            if sym in st["signals"]:
                continue   # 1 銘柄 1 日 1 回
            st["signals"][sym] = {"ts": _ts(now), **sig, "status": "new"}
            changed = True
            logger.warning("[SELLING CLIMAX] signal symbol=%s %s", sym, sig)
            result, reason, detail = _try_entry(sym, sig, st, now, c)
            st["signals"][sym].update(status=result, reason=reason, detail=detail)
            logger.warning("[SELLING CLIMAX] %s symbol=%s reason=%s detail=%s", result.upper(), sym, reason, detail)
        if changed:
            _save(st)


def _try_entry(sym: str, sig: Dict[str, Any], st: Dict[str, Any], now: dt.datetime, c: Dict[str, Any]):
    entries = len(st.get("trades") or {})
    if entries >= c["max_per_day"]:
        return "skipped", "max_per_day", {"entries": entries}
    if _open_count(st, now, c["hold_min"]) >= c["max_open"]:
        return "skipped", "max_open", {}
    if float(sig["close"]) < c["min_price"]:
        return "skipped", "price_too_low", {"price": sig["close"]}
    try:
        from trading.entry.daily_risk_guard import should_allow_new_entry
        dec = should_allow_new_entry(sym)
        if not getattr(dec, "allow", True):
            return "skipped", f"daily_risk:{getattr(dec, 'reason', '')}", {}
    except Exception:
        logger.debug("[SELLING CLIMAX] daily risk guard failed", exc_info=True)
    # ENTRY_PAUSE ファイルは send_order 側で効く
    bd = _board(sym)
    if not bd:
        return "skipped", "board_unavailable", {}
    bid, ask = float(bd.get("bid") or 0), float(bd.get("ask") or 0)
    if not (ask > bid > 0):
        return "skipped", "board_invalid", {"bid": bid, "ask": ask}
    spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
    if spread_pct > c["max_spread_pct"]:
        return "skipped", "spread_too_wide", {"spread_pct": round(spread_pct, 3)}
    # 足の終値より大きく戻っていたら、もう反発に乗り遅れている
    if ask > float(sig["close"]) * 1.005:
        return "skipped", "already_bounced", {"ask": ask, "bar_close": sig["close"]}
    qty = int(c["qty"])
    if qty <= 0 or ask * qty > c["max_yen"]:
        return "skipped", "too_expensive", {"ask": ask, "qty": qty, "max_yen": c["max_yen"]}
    from trading.handlers.entry_handler import place_entry_buy
    oid = place_entry_buy(sym, sym, ask, "SELLING_CLIMAX", order_type="LIMIT", qty=qty)
    detail = {"bid": bid, "ask": ask, "spread_pct": round(spread_pct, 3), "qty": qty}
    if not oid:
        return "skipped", "send_failed", detail
    st.setdefault("trades", {})[sym] = {"order_ts": _ts(now), "order_id": str(oid), "price": ask, "qty": qty,
                                        "signal": {k: sig[k] for k in ("bar", "close", "bar_ret", "drop30", "vol_x")}, **detail}
    return "ordered", "ok", {**detail, "order_id": str(oid)}


# ------------------------------------------------------------
# 決済 (exit_loop から呼ぶ)
# ------------------------------------------------------------
def handle_exit(symbol: str, pos: Dict[str, Any], now: Optional[dt.datetime] = None) -> Optional[bool]:
    """この戦略の建玉なら True/False を返す (呼び出し側は以降の決済ルールを通さない)。そうでなければ None。"""
    try:
        with _LOCK:
            t = dict(((_load().get("trades") or {}).get(str(symbol))) or {})
        if not t or t.get("closed"):
            return None
        if not str((pos or {}).get("side") or "").upper().startswith("BUY"):
            return None
        c = cfg()
        now = now or dt.datetime.now()
        from trading.exit.exit_position_runner import _resolve_entry_price
        from trading.exit.exit_price_source import get_latest_exit_price
        entry = float(_resolve_entry_price(pos) or 0)
        price = float((get_latest_exit_price(symbol) or (0, None))[0] or 0)
        if entry <= 0 or price <= 0:
            return False
        reason = None
        if price <= entry * (1 - c["stop_pct"] / 100.0):
            reason = f"SELLING_CLIMAX_STOP pct={c['stop_pct']:.1f} entry={entry:.1f} price={price:.1f}"
        elif now >= _pt(t["order_ts"]) + dt.timedelta(minutes=c["hold_min"]):
            reason = f"SELLING_CLIMAX_TIME hold={c['hold_min']:.0f}m entry={entry:.1f} price={price:.1f}"
        elif now.time() >= c["force_close"]:
            reason = f"SELLING_CLIMAX_FORCE_CLOSE at={c['force_close']} entry={entry:.1f} price={price:.1f}"
        if not reason:
            return False
        from trading.exit.exit_finalize import finalize_exit
        finalize_exit(symbol=symbol, price=price, reason=reason, cluster_id=0, regime=0, inago_state=0,
                      pnl=price - entry, collapse_prob=0.0, ctx=None)
        logger.warning("[SELLING CLIMAX] EXIT symbol=%s reason=%s pnl_per_share=%.1f", symbol, reason, price - entry)
        with _LOCK:
            st = _load()
            tt = st.get("trades", {}).get(str(symbol))
            if tt is not None:
                tt.update(closed=True, entry_fill=entry, exit_ts=_ts(now), exit_price=price, exit_reason=reason.split()[0])
                _save(st)
        return True
    except Exception:
        logger.exception("[SELLING CLIMAX] handle_exit failed symbol=%s", symbol)
        return None


# ------------------------------------------------------------
# スケジューラ登録 (core/startup/scheduler/startup.py から)
# ------------------------------------------------------------
def _run_guarded() -> None:
    try:
        from core.startup.scheduler.market_guard import is_market_time_for_entry
        if not is_market_time_for_entry():
            return
        run_cycle()
    except Exception:
        logger.exception("[SELLING CLIMAX] cycle failed")


def register_scheduler_safe() -> bool:
    try:
        import schedule
        from core.startup.scheduler.helpers import has_schedule_tag
        if has_schedule_tag("selling_climax"):
            return True
        every = int(_env_f("SELLING_CLIMAX_INTERVAL_SEC", 20))
        schedule.every(every).seconds.do(_run_guarded).tag("selling_climax", "entry")
        logger.warning("[SELLING CLIMAX] scheduler registered every=%ss cfg=%s", every,
                       {k: (str(v) if isinstance(v, dt.time) else v) for k, v in cfg().items()})
        return True
    except Exception:
        logger.exception("[SELLING CLIMAX] scheduler register failed")
        return False


__all__ = ["run_cycle", "handle_exit", "register_scheduler_safe", "cfg", "detect"]
