# ============================================================
# File   : trading/strategies/limitup_fade.py
# 2026-10-07: ストップ高タッチ後の崩れ売り (LIMITUP_FADE) — 記録だけ (発注しない)
# ------------------------------------------------------------
# ストップ高に「触れただけ」で張り付かなかった銘柄が、ストップ高から 4% 以上崩れたら空売りする想定で、
# シグナルと仮想の決済を記録する。実気配で取れるか確かめてから発注を足す (ユーザー判断 10/7)。
#
# シグナル (5 分足の確定値、push のティックから作る):
#   - 当日の高値がストップ高 (前日終値 + 制限値幅) の 0.2% 以内 = タッチ
#   - タッチ後、5 分足の終値がストップ高のまま確定した足が 1 本も無い (張り付いていない)
#   - 5 分足の終値がストップ高から 4% 以上下 (剥がれた直後は売らない)
#   - 09:15〜14:50、11 時台は除く
# 仮想決済 (売り気配で買い戻し): 売り気配がストップ高の 0.3% 以内 (証券会社の逆指値を 1 刻み手前に置く想定)
#   / 利確 -5% / トレール 1.0%・0.5% 戻し / 30 分 / 15:20。
#
# 根拠 (2026-10-07 検証、TradingView 5 分足 2023〜2025、30 分):
#   剥がれてすぐ (-1%) 売ると 36〜49% が再びストップ高 (15 分以上張り付いていた銘柄は 1 回 -0.47%)。
#   触れただけ + -4% まで待つ: 705 回 +0.78%・勝率 73%・再タッチ 9%・翌日持越し 0.4% (最悪 -20%)。
#   15 分以上張り付いていた銘柄は -4% まで待っても ±0%。11 時台は -0.26%。
#   4493 (10/7) は 14:46・14:59 に 3,120 円に触れただけ (売り板 13 万株)、15:15 に -8%。
#
# 状態は %LOCALAPPDATA%\autostock\runtime\limitup_fade_YYYYMMDD.json。
# 止め方: LIMITUP_FADE_ENABLED=0。
# ============================================================
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from trading.strategies.surge_short import _env_f, _env_on, _hm, _ts, _pt, limit_up_price

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()


def cfg() -> Dict[str, Any]:
    return {
        "enabled": _env_on("LIMITUP_FADE_ENABLED", True),
        "start": _hm("LIMITUP_FADE_START", "09:15"),
        "end": _hm("LIMITUP_FADE_END", "14:50"),
        "skip_hour": int(_env_f("LIMITUP_FADE_SKIP_HOUR", 11)),
        "drop_pct": _env_f("LIMITUP_FADE_DROP_PCT", 4.0),
        "max_stuck_bars": int(_env_f("LIMITUP_FADE_MAX_STUCK_BARS", 0)),
        "watch_pct": _env_f("LIMITUP_FADE_WATCH_PCT", 10.0),   # ストップ高まで この % 以内に来たら見張る
        "hold_min": _env_f("LIMITUP_FADE_HOLD_MIN", 30.0),
        "trail_start_pct": _env_f("LIMITUP_FADE_TRAIL_START_PCT", 1.0),
        "trail_back_pct": _env_f("LIMITUP_FADE_TRAIL_BACK_PCT", 0.5),
        "take_profit_pct": _env_f("LIMITUP_FADE_TAKE_PROFIT_PCT", 5.0),
        "force_close": _hm("LIMITUP_FADE_FORCE_CLOSE", "15:20"),
        "max_publish": int(_env_f("LIMITUP_FADE_MAX_PUBLISH", 8)),
    }


def _state_path(day: Optional[dt.date] = None) -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = (day or dt.date.today()).strftime("%Y%m%d")
    p = Path(base) / "autostock" / "runtime" / f"limitup_fade_{d}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> Dict[str, Any]:
    p = _state_path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[LIMITUP FADE] state read failed path=%s", p, exc_info=True)
    return {"watch": {}}


def _save(st: Dict[str, Any]) -> None:
    p = _state_path()
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        logger.warning("[LIMITUP FADE] state write failed path=%s", p, exc_info=True)


# ------------------------------------------------------------
# push DB の差分読み (見張り中の銘柄だけ 5 分足の 高値・終値 と 最新の気配を持つ)
#   high_price 列は当日高値 (登録前のタッチも分かる)。
# ------------------------------------------------------------
class _Feed:
    def __init__(self) -> None:
        self.day: Optional[str] = None
        self.last_rowid = 0
        self.bars: Dict[str, Dict[str, list]] = {}    # sym -> {"HH:MM"(5 分の頭): [high, last]}
        self.quote: Dict[str, Dict[str, Any]] = {}    # sym -> {ask, bid, price, day_high, ts}

    def update(self, now: dt.datetime, syms: set) -> None:
        from scheduler_jobs.summary.memory_latest_1m import _push_db_path
        path = str(_push_db_path())
        day = now.strftime("%Y%m%d")
        if self.day != day:
            self.day, self.last_rowid, self.bars, self.quote = day, 0, {}, {}
        if not syms or not os.path.exists(path):
            return
        c = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=3)
        try:
            if self.last_rowid == 0:
                # 初回は見張り銘柄の当日分を銘柄インデックスで読む (再起動しても 5 分足を作り直せる)
                q = ",".join("?" * len(syms))
                rows = c.execute(f"select rowid, symbol, datetime, price, high_price, Sell1Price, Buy1Price"
                                 f" from stream_data_raw where symbol in ({q}) order by rowid", tuple(syms)).fetchall()
                self.last_rowid = int(c.execute("select max(rowid) from stream_data_raw").fetchone()[0] or 0)
            else:
                rows = c.execute("select rowid, symbol, datetime, price, high_price, Sell1Price, Buy1Price"
                                 " from stream_data_raw where rowid>? order by rowid limit 400000",
                                 (self.last_rowid,)).fetchall()
                if rows:
                    self.last_rowid = int(rows[-1][0])
        finally:
            c.close()
        for _rid, sym, ts, price, high, ask, bid in rows:
            sym = str(sym)
            if sym not in syms:
                continue
            s = str(ts)[:19].replace("T", " ")
            q = self.quote.setdefault(sym, {})
            q["ts"] = s
            if high and float(high) > float(q.get("day_high") or 0):
                q["day_high"] = float(high)
            if ask and bid and float(ask) > float(bid) > 0:
                q["ask"], q["bid"] = float(ask), float(bid)
            if not price or float(price) <= 0:
                continue
            q["price"] = float(price)
            hh, mm = int(s[11:13]), int(s[14:16])
            key = f"{hh:02d}:{mm - mm % 5:02d}"
            b = self.bars.setdefault(sym, {}).setdefault(key, [0.0, 0.0])
            b[0] = max(b[0], float(price))
            b[1] = float(price)

    def age(self, sym: str, now: dt.datetime) -> float:
        try:
            return (now - _pt(self.quote[sym]["ts"])).total_seconds()
        except Exception:
            return 1e9


_FEED = _Feed()


def _publish(syms) -> None:
    """見張り銘柄を PUSH 登録に回す。候補ファイルは上書き式なので、今の候補に足して書く。"""
    try:
        from trading.entry import candidate_bridge
        cur = candidate_bridge.load() or []
        candidate_bridge.publish(list(cur) + list(syms))
    except Exception:
        logger.debug("[LIMITUP FADE] publish failed", exc_info=True)


def _in_window(hm: str, c: Dict[str, Any]) -> bool:
    t = dt.time(int(hm[:2]), int(hm[3:5]))
    return c["start"] <= t <= c["end"] and t.hour != c["skip_hour"]


def run_cycle(now: Optional[dt.datetime] = None) -> None:
    c = cfg()
    if not c["enabled"]:
        return
    now = now or dt.datetime.now()
    if not (dt.time(9, 0) <= now.time() <= c["force_close"]):
        return
    with _LOCK:
        st = _load()
        w = st.setdefault("watch", {})
        changed = False
        # 1) 見張りに足す (ranking の騰落率から前日終値を逆算)
        try:
            from trading.strategies.surge_short import _liquidity_map
            lm = _liquidity_map()
        except Exception:
            lm = {}
        for sym, v in lm.items():
            if sym in w:
                continue
            price, chg = float(v.get("current_price") or 0), float(v.get("change_percentage") or 0)
            if price <= 0 or chg < 5 or chg <= -100:
                continue
            pc = float(round(price / (1 + chg / 100.0)))
            lu = limit_up_price(pc)
            if price >= lu * (1 - c["watch_pct"] / 100.0):
                w[sym] = {"since": _ts(now), "prev_close": pc, "limit_up": lu, "touch_ts": None,
                          "stuck_bars": 0, "done_bars": [], "status": "watch"}
                changed = True
                logger.warning("[LIMITUP FADE] watch symbol=%s price=%.1f chg=%.2f%% limit_up=%.1f", sym, price, chg, lu)
        live = [s for s, x in w.items() if x["status"] in ("watch", "touched", "virtual_open")]
        if live:
            _publish(live[: c["max_publish"]])
        # 2) push を読む
        try:
            _FEED.update(now, set(live))
        except Exception:
            logger.debug("[LIMITUP FADE] feed update failed", exc_info=True)
        cur_key = f"{now.hour:02d}:{now.minute - now.minute % 5:02d}"
        for sym in live:
            x = w[sym]
            lu = float(x["limit_up"])
            q = _FEED.quote.get(sym) or {}
            bars = _FEED.bars.get(sym) or {}
            # タッチ
            if not x.get("touch_ts"):
                hit = [k for k in sorted(bars) if bars[k][0] >= lu * 0.998]
                if hit or float(q.get("day_high") or 0) >= lu * 0.998:
                    x["touch_ts"] = hit[0] if hit else f"before:{x['since'][11:16]}"
                    x["status"] = "touched"; changed = True
                    logger.warning("[LIMITUP FADE] touched symbol=%s limit_up=%.1f at=%s", sym, lu, x["touch_ts"])
            # 確定した 5 分足を順に見る
            if x["status"] == "touched":
                t0 = x["touch_ts"][-5:] if x["touch_ts"].startswith("before:") else x["touch_ts"]
                for k in sorted(bars):
                    if k >= cur_key or k < t0 or k in x["done_bars"]:
                        continue
                    x["done_bars"].append(k); changed = True
                    last = bars[k][1]
                    if last >= lu * 0.998:
                        x["stuck_bars"] += 1
                        continue
                    if last > lu * (1 - c["drop_pct"] / 100.0):
                        continue
                    bar_close = (dt.datetime.strptime(k, "%H:%M") + dt.timedelta(minutes=5)).strftime("%H:%M")
                    why = None
                    if x["stuck_bars"] > c["max_stuck_bars"]:
                        why = f"stuck_bars={x['stuck_bars']}"
                    elif not _in_window(bar_close, c):
                        why = f"time={bar_close}"
                    elif x["touch_ts"].startswith("before:"):
                        why = "touch_before_watch(stuck_unknown)"
                    else:
                        try:
                            from trading.filters.theme_short_guard import block_reason as _theme_block
                            why = _theme_block(sym, "SELL")
                        except Exception:
                            why = None
                    if why:
                        x["status"] = "skipped"; x["skip"] = why
                        logger.warning("[LIMITUP FADE] SKIP symbol=%s close=%.1f limit_up=%.1f reason=%s", sym, last, lu, why)
                        break
                    ask, bid = q.get("ask"), q.get("bid")
                    prem = None
                    try:
                        from trading.strategies.surge_short import _margin_premium
                        prem = _margin_premium(sym)
                    except Exception:
                        pass
                    entry = float(bid or last)
                    x.update(status="virtual_open", entry_ts=_ts(now), bar=k, bar_close=last, entry=entry,
                             ask=ask, bid=bid, quote_age=round(_FEED.age(sym, now), 1), best=entry,
                             marginpremium=prem, drop_pct=round((1 - last / lu) * 100, 2))
                    logger.warning("[LIMITUP FADE] SIGNAL (record only) symbol=%s bar=%s close=%.1f limit_up=%.1f bid=%s ask=%s "
                                   "drop=%.2f%% touch=%s premium=%s", sym, k, last, lu, bid, ask,
                                   x["drop_pct"], x["touch_ts"], prem)
                    break
            # 仮想決済
            if x["status"] == "virtual_open" and q:
                entry = float(x["entry"])
                ask = float(q.get("ask") or q.get("price") or 0)
                if ask <= 0:
                    continue
                best = min(float(x.get("best") or entry), ask)
                x["best"] = best
                held = (now - _pt(x["entry_ts"])).total_seconds() / 60.0
                why = None
                if ask >= lu * 0.997 or float(q.get("price") or 0) >= lu * 0.998:
                    why = "LIMIT_RETOUCH"
                elif best <= entry * (1 - c["trail_start_pct"] / 100) and ask >= best * (1 + c["trail_back_pct"] / 100):
                    why = "TRAIL"
                elif ask <= entry * (1 - c["take_profit_pct"] / 100):
                    why = "TAKE_PROFIT"
                elif held >= c["hold_min"]:
                    why = "TIME"
                elif now.time() >= c["force_close"]:
                    why = "FORCE_CLOSE"
                if why:
                    pnl = (entry - ask) / entry * 100
                    x.update(status="virtual_closed", exit_ts=_ts(now), exit=ask, exit_reason=why,
                             pnl_pct=round(pnl, 3), held_min=round(held, 1), exit_quote_age=round(_FEED.age(sym, now), 1))
                    changed = True
                    logger.warning("[LIMITUP FADE] VIRTUAL EXIT symbol=%s reason=%s entry=%.1f exit=%.1f pnl=%+.2f%% held=%.1fm",
                                   sym, why, entry, ask, pnl, held)
        if changed or live:
            _save(st)


def _run_guarded() -> None:
    try:
        from core.startup.scheduler.market_guard import is_market_time_for_entry
        if not is_market_time_for_entry():
            return
        run_cycle()
    except Exception:
        logger.exception("[LIMITUP FADE] cycle failed")


def register_scheduler_safe() -> bool:
    try:
        import schedule
        from core.startup.scheduler.helpers import has_schedule_tag
        if has_schedule_tag("limitup_fade"):
            return True
        every = int(_env_f("LIMITUP_FADE_INTERVAL_SEC", 20))
        schedule.every(every).seconds.do(_run_guarded).tag("limitup_fade", "entry")
        logger.warning("[LIMITUP FADE] scheduler registered (record only) every=%ss cfg=%s", every,
                       {k: (str(v) if isinstance(v, dt.time) else v) for k, v in cfg().items()})
        return True
    except Exception:
        logger.exception("[LIMITUP FADE] scheduler register failed")
        return False


__all__ = ["run_cycle", "register_scheduler_safe", "cfg"]
