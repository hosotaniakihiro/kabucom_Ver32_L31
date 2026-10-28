# ============================================================
# File   : trading/strategies/stop_reentry.py
# 2026-10-28: 損切りした銘柄が下げ止まって上がり始めたら入り直す (STOP_REENTRY)
# ------------------------------------------------------------
# 損切りを 0.3% に狭めた (ユーザー判断) のと組で入れる。3042 は 14:41 に 1355 円で損切りした
# 25 秒後に 1380 円へ戻り、5 分後には買値 +3.4%。狭い損切りでノイズに切られても、戻りに乗り直す。
#
# 流れ:
#   1) executor が ENTRY_PRICE_STOP で決済を終えたら register() (side・株数・決済値を覚える)
#   2) 5 秒ごとに現在値を見て、決済後の安値 (売りなら高値) を更新する
#   3) 安値から +REBOUND_PCT% 戻り、かつ安値が HOLD_SEC 秒更新されていなければ、同じ株数で入り直す
#      (買い: 売気配の指値、売り: 買気配の指値)。入り直した建玉の決済は通常のルール (0.3% 損切りなど)。
#   - 決済から WINDOW_MIN 分を過ぎたら見るのをやめる。同じ銘柄は 1 日 MAX_PER_SYMBOL 回まで。
#   - 安値から MAX_REBOUND_PCT% より上に行ってしまったら乗り遅れとして入らない (そのまま見続ける)。
#   - 建玉がまだ残っている (決済の約定待ち) 間は入らない。
#   - symbol_trade_guard の負け後クールダウン (15 分) と 1 日 2 敗の止めは、この経路では見ない。
#
# 状態は %LOCALAPPDATA%\autostock\runtime\stop_reentry_YYYYMMDD.json。
# 止め方: STOP_REENTRY_ENABLED=0。売り建玉の入り直しは STOP_REENTRY_SELL_ENABLED=1 で有効 (既定は買いだけ)。
# ============================================================
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from trading.strategies.surge_short import _env_f, _env_on, _hm, _ts, _pt

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()


def cfg() -> Dict[str, Any]:
    return {
        "enabled": _env_on("STOP_REENTRY_ENABLED", True),
        "sell_enabled": _env_on("STOP_REENTRY_SELL_ENABLED", False),
        "rebound_pct": _env_f("STOP_REENTRY_REBOUND_PCT", 0.5),
        "max_rebound_pct": _env_f("STOP_REENTRY_MAX_REBOUND_PCT", 1.5),
        "hold_sec": _env_f("STOP_REENTRY_HOLD_SEC", 30.0),
        "window_min": _env_f("STOP_REENTRY_WINDOW_MIN", 30.0),
        "max_per_symbol": int(_env_f("STOP_REENTRY_MAX_PER_SYMBOL", 2)),
        "max_spread_pct": _env_f("STOP_REENTRY_MAX_SPREAD_PCT", 0.3),
        "end": _hm("STOP_REENTRY_END", "15:00"),
    }


def _state_path(day: Optional[dt.date] = None) -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = (day or dt.date.today()).strftime("%Y%m%d")
    p = Path(base) / "autostock" / "runtime" / f"stop_reentry_{d}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> Dict[str, Any]:
    p = _state_path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[STOP REENTRY] state read failed path=%s", p, exc_info=True)
    return {"watch": {}, "count": {}, "log": []}


def _save(st: Dict[str, Any]) -> None:
    p = _state_path()
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        logger.warning("[STOP REENTRY] state write failed path=%s", p, exc_info=True)


def register(symbol: str, side: str, qty: float, exit_price: float, reason: str) -> None:
    """executor が損切りの決済を終えたときに呼ぶ。"""
    try:
        c = cfg()
        sym = str(symbol or "").strip()
        side_u = "BUY" if str(side or "").upper().startswith("BUY") else "SELL"
        if not c["enabled"] or not sym or exit_price <= 0 or qty <= 0:
            return
        if side_u == "SELL" and not c["sell_enabled"]:
            return
        now = dt.datetime.now()
        with _LOCK:
            st = _load()
            n = int((st.get("count") or {}).get(sym) or 0)
            if n >= c["max_per_symbol"]:
                logger.warning("[STOP REENTRY] not watched symbol=%s reason=max_per_symbol count=%d", sym, n)
                return
            st.setdefault("watch", {})[sym] = {
                "side": side_u, "qty": int(qty), "exit_price": float(exit_price), "exit_ts": _ts(now),
                "extreme": float(exit_price), "extreme_ts": _ts(now), "reason": str(reason or "").split()[0],
            }
            _save(st)
        logger.warning("[STOP REENTRY] WATCH symbol=%s side=%s qty=%d exit=%.1f window=%.0fm rebound=%.2f%%",
                       sym, side_u, int(qty), exit_price, c["window_min"], c["rebound_pct"])
    except Exception:
        logger.exception("[STOP REENTRY] register failed symbol=%s", symbol)


def _price(symbol: str) -> float:
    try:
        from trading.exit.exit_price_source import get_latest_exit_price
        return float((get_latest_exit_price(symbol) or (0, None))[0] or 0)
    except Exception:
        logger.debug("[STOP REENTRY] price failed symbol=%s", symbol, exc_info=True)
        return 0.0


def _still_holding(symbol: str) -> bool:
    try:
        from trading.position.open_position_sync import is_symbol_in_open_position
        return bool(is_symbol_in_open_position(symbol))
    except Exception:
        return True


def _board(symbol: str) -> Optional[Dict[str, Any]]:
    try:
        from trading.handlers.entry_order_builder import _rfb_fetch_board
        return _rfb_fetch_board(symbol, force=True)
    except Exception:
        logger.debug("[STOP REENTRY] board failed symbol=%s", symbol, exc_info=True)
        return None


def _try_entry(sym: str, w: Dict[str, Any], c: Dict[str, Any]):
    bd = _board(sym)
    if not bd:
        return "skipped", "board_unavailable", {}
    bid, ask = float(bd.get("bid") or 0), float(bd.get("ask") or 0)
    if not (ask > bid > 0):
        return "skipped", "board_invalid", {"bid": bid, "ask": ask}
    spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
    detail = {"bid": bid, "ask": ask, "spread_pct": round(spread_pct, 3), "qty": w["qty"]}
    if spread_pct > c["max_spread_pct"]:
        return "skipped", "spread_too_wide", detail
    from trading.handlers.entry_handler import place_entry_buy, place_entry_sell
    if w["side"] == "BUY":
        oid = place_entry_buy(sym, sym, ask, "STOP_REENTRY", order_type="LIMIT", qty=w["qty"])
    else:
        oid = place_entry_sell(sym, sym, bid, "STOP_REENTRY", order_type="LIMIT", qty=w["qty"])
    if not oid:
        return "skipped", "send_failed", detail
    return "ordered", "ok", {**detail, "order_id": str(oid)}


def run_cycle(now: Optional[dt.datetime] = None) -> None:
    c = cfg()
    if not c["enabled"]:
        return
    now = now or dt.datetime.now()
    with _LOCK:
        st = _load()
        watch: Dict[str, Dict[str, Any]] = st.setdefault("watch", {})
        if not watch:
            return
        changed = False
        for sym in list(watch):
            w = watch[sym]
            if now - _pt(w["exit_ts"]) > dt.timedelta(minutes=c["window_min"]) or now.time() >= c["end"]:
                logger.warning("[STOP REENTRY] EXPIRE symbol=%s side=%s exit=%.1f extreme=%.1f",
                               sym, w["side"], w["exit_price"], w["extreme"])
                watch.pop(sym); changed = True
                continue
            px = _price(sym)
            if px <= 0:
                continue
            buy = w["side"] == "BUY"
            # 決済後の安値 (売りなら高値) を更新
            if (buy and px < w["extreme"]) or (not buy and px > w["extreme"]):
                w.update(extreme=px, extreme_ts=_ts(now)); changed = True
                continue
            move = (px / w["extreme"] - 1) * 100 if buy else (1 - px / w["extreme"]) * 100
            held = (now - _pt(w["extreme_ts"])).total_seconds()
            if move < c["rebound_pct"] or held < c["hold_sec"]:
                continue
            if move > c["max_rebound_pct"]:
                continue  # 乗り遅れ。戻ってくる (安値が更新される) かもしれないので見続ける
            if _still_holding(sym):
                continue  # 決済の約定待ち
            try:
                result, why, detail = _try_entry(sym, w, c)
            except Exception as ex:
                logger.exception("[STOP REENTRY] entry failed symbol=%s", sym)
                result, why, detail = "skipped", "exception", {"error": repr(ex)[:120]}
            logger.warning("[STOP REENTRY] %s symbol=%s side=%s reason=%s price=%.1f extreme=%.1f move=%.2f%% "
                           "exit=%.1f detail=%s", result.upper(), sym, w["side"], why, px, w["extreme"], move,
                           w["exit_price"], detail)
            if result == "ordered":
                st.setdefault("count", {})[sym] = int(st["count"].get(sym) or 0) + 1
                st.setdefault("log", []).append({**w, "symbol": sym, "entry_ts": _ts(now), "price": px, **detail})
                watch.pop(sym); changed = True
        if changed:
            _save(st)


def _run_guarded() -> None:
    try:
        from core.startup.scheduler.market_guard import is_market_time_for_entry
        if not is_market_time_for_entry():
            return
        run_cycle()
    except Exception:
        logger.exception("[STOP REENTRY] cycle failed")


def register_scheduler_safe() -> bool:
    try:
        import schedule
        from core.startup.scheduler.helpers import has_schedule_tag
        if has_schedule_tag("stop_reentry"):
            return True
        every = int(_env_f("STOP_REENTRY_INTERVAL_SEC", 5))
        schedule.every(every).seconds.do(_run_guarded).tag("stop_reentry", "entry")
        logger.warning("[STOP REENTRY] scheduler registered every=%ss cfg=%s", every,
                       {k: (str(v) if isinstance(v, dt.time) else v) for k, v in cfg().items()})
        return True
    except Exception:
        logger.exception("[STOP REENTRY] scheduler register failed")
        return False


__all__ = ["register", "run_cycle", "register_scheduler_safe", "cfg"]
