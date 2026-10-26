# ============================================================
# File   : trading/monitors/earnings_watch.py
# 2026-10-26: 場中の決算・業績修正の直後の値動きを記録する (売買はしない)
# ------------------------------------------------------------
# 1 分ごと (場中) に TDnet 一覧の 1 ページ目 (新しい順 100 件) を読み、決算短信・業績予想・配当予想の
# 開示を拾う。発表から 35 分たったら ランキング DB (全市場、ほぼ 1 分ごと) の価格で
#   - 発表 1 分前の価格 (基準)、+1/+2/+3/+5/+10/+15/+20/+30 分の価格
#   - k=1,2 分で ±2% 以上動いたとき、その逆に張って 15 分後に返した損益 (往復 0.15% を引く)
# を %LOCALAPPDATA%\autostock\runtime\earnings_watch_YYYYMMDD.json とログ [EARNINGS WATCH] に残す。
#
# 根拠 (2026-10-26 検証、TDnet 過去 1 か月 184 件、Yahoo 1 分足):
#   発表の向きに乗る: すべてマイナス (k=1・2% で -1.17%)。
#   15 分後の戻りに張る: k=1・2% で +0.70% (21 件、勝率 62%、プラス日 6/11)。件数が少なく損切りも無いので
#   記録だけで前向きに確かめる (ユーザー判断 10/26)。価格は約定ではなく最終値なので、スプレッドは別途。
#
# 止め方: EARNINGS_WATCH_ENABLED=0
# ============================================================
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)
_LOCK = threading.Lock()

KEYS = ("決算短信", "業績予想", "配当予想", "業績予想の修正", "通期業績")
OFFSETS = (1, 2, 3, 5, 10, 15, 20, 30)
COST = 0.15


def _env_f(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        return float(v) if v not in (None, "") else float(default)
    except Exception:
        return float(default)


def _enabled() -> bool:
    return str(os.getenv("EARNINGS_WATCH_ENABLED", "1")).strip().lower() not in ("0", "false", "no", "off")


def _state_path(day: Optional[dt.date] = None) -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = (day or dt.date.today()).strftime("%Y%m%d")
    p = Path(base) / "autostock" / "runtime" / f"earnings_watch_{d}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> Dict[str, Any]:
    p = _state_path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[EARNINGS WATCH] state read failed path=%s", p, exc_info=True)
    return {"events": {}}


def _save(st: Dict[str, Any]) -> None:
    p = _state_path()
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        logger.warning("[EARNINGS WATCH] state write failed path=%s", p, exc_info=True)


def _ranking_db(day: str) -> str:
    base = os.getenv("RANKING_DB_DIR") or os.path.join(os.environ.get("LOCALAPPDATA") or str(Path.home()),
                                                       "autostock", "ranking")
    return os.path.join(base, f"ranking{day}.db")


def in_session_event(when: dt.datetime, title: str) -> bool:
    if not any(k in (title or "") for k in KEYS):
        return False
    t = when.time()
    return dt.time(9, 0) <= t <= dt.time(15, 15) and not (dt.time(11, 30) <= t < dt.time(12, 30))


def minute_prices(symbol: str, t0: dt.datetime, minutes: int = 35) -> Dict[str, float]:
    """ランキング DB の 1 分ごとの価格 (同じ分に複数行あれば最後の行)。{"HH:MM": price}"""
    path = _ranking_db(t0.strftime("%Y%m%d"))
    if not os.path.exists(path):
        return {}
    a = (t0 - dt.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    b = (t0 + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    c = sqlite3.connect("file:" + path + "?mode=ro", uri=True, timeout=5)
    try:
        rows = c.execute("select datetime, coalesce(current_price, price) from ranking_snapshot_1min"
                         " where symbol=? and datetime>=? and datetime<=? order by rowid", (symbol, a, b)).fetchall()
    finally:
        c.close()
    out: Dict[str, float] = {}
    for t, p in rows:
        if p and float(p) > 0:
            out[str(t)[11:16]] = float(p)
    return out


def evaluate(t0: dt.datetime, px: Dict[str, float]) -> Dict[str, Any]:
    """基準 (発表 1 分前までの最後の価格) と各分の変化、逆張り 15 分の損益。"""
    def at(m: int) -> Optional[float]:
        # その分に値が無ければ 2 分前まで遡る
        for back in range(0, 3):
            k = (t0 + dt.timedelta(minutes=m - back)).strftime("%H:%M")
            if k in px:
                return px[k]
        return None

    base = None
    for back in range(1, 6):
        k = (t0 - dt.timedelta(minutes=back)).strftime("%H:%M")
        if k in px:
            base = px[k]
            break
    res: Dict[str, Any] = {"base": base}
    if not base:
        return res
    for m in OFFSETS:
        p = at(m)
        res[f"chg_{m}m"] = round((p / base - 1) * 100, 3) if p else None
    for k in (1, 2):
        p_k = at(k)
        p_in = at(k + 1)
        p_out = at(k + 1 + 15)
        if not (p_k and p_in and p_out):
            continue
        chg = (p_k / base - 1) * 100
        if abs(chg) < 2.0:
            continue
        sgn = -1 if chg > 0 else 1   # 逆に張る
        res[f"fade15_k{k}"] = round(sgn * (p_out / p_in - 1) * 100 - COST, 3)
    return res


def run_cycle(now: Optional[dt.datetime] = None) -> None:
    if not _enabled():
        return
    now = now or dt.datetime.now()
    with _LOCK:
        st = _load()
        ev: Dict[str, Dict[str, Any]] = st.setdefault("events", {})
        changed = False
        if now.time() <= dt.time(15, 16):
            try:
                os.environ.pop("SSLKEYLOGFILE", None)  # Norton の SSLKEYLOGFILE で HTTPS が固まる対策
                from news_pipeline import fetch_tdnet_disclosures
                rows = fetch_tdnet_disclosures(now.strftime("%Y%m%d"), max_pages=1)
            except Exception:
                logger.warning("[EARNINGS WATCH] tdnet fetch failed", exc_info=True)
                rows = []
            for when, code, title, _url in rows:
                if not code or when.date() != now.date() or not in_session_event(when, title):
                    continue
                key = f"{code}"
                if key in ev:
                    continue
                ev[key] = {"symbol": code, "t0": when.strftime("%Y-%m-%d %H:%M:%S"), "title": title[:60],
                           "seen_ts": now.strftime("%Y-%m-%d %H:%M:%S"), "status": "watch"}
                changed = True
                logger.warning("[EARNINGS WATCH] disclosure symbol=%s at=%s title=%s (seen %.0fs later)",
                               code, when.strftime("%H:%M"), title[:40], (now - when).total_seconds())
        for key, e in ev.items():
            if e.get("status") != "watch":
                continue
            t0 = dt.datetime.strptime(e["t0"], "%Y-%m-%d %H:%M:%S")
            end = t0 + dt.timedelta(minutes=35)
            close = t0.replace(hour=11, minute=30) if t0.time() < dt.time(11, 30) else t0.replace(hour=15, minute=30)
            if now < min(end, close):
                continue
            try:
                r = evaluate(t0, minute_prices(e["symbol"], t0))
            except Exception:
                logger.warning("[EARNINGS WATCH] evaluate failed symbol=%s", e["symbol"], exc_info=True)
                r = {}
            e.update(status="done", **r)
            changed = True
            logger.warning("[EARNINGS WATCH] result symbol=%s at=%s base=%s chg1=%s chg2=%s chg15=%s fade15_k1=%s fade15_k2=%s",
                           e["symbol"], e["t0"][11:16], r.get("base"), r.get("chg_1m"), r.get("chg_2m"),
                           r.get("chg_15m"), r.get("fade15_k1"), r.get("fade15_k2"))
        if changed:
            _save(st)


def _run_guarded() -> None:
    def _job():
        try:
            from core.startup.scheduler.market_guard import is_market_time_for_entry
            if not is_market_time_for_entry():
                return
            run_cycle()
        except Exception:
            logger.exception("[EARNINGS WATCH] cycle failed")
    if _LOCK.locked():
        return
    # TDnet への HTTP をスケジューラのスレッドで待たない
    threading.Thread(target=_job, name="earnings-watch", daemon=True).start()


def register_scheduler_safe() -> bool:
    try:
        if not _enabled():
            return False
        import schedule
        from core.startup.scheduler.helpers import has_schedule_tag
        if has_schedule_tag("earnings_watch"):
            return True
        every = int(_env_f("EARNINGS_WATCH_INTERVAL_SEC", 60))
        schedule.every(every).seconds.do(_run_guarded).tag("earnings_watch", "monitor")
        logger.warning("[EARNINGS WATCH] scheduler registered (record only) every=%ss", every)
        return True
    except Exception:
        logger.exception("[EARNINGS WATCH] scheduler register failed")
        return False


__all__ = ["run_cycle", "register_scheduler_safe", "evaluate", "in_session_event"]
