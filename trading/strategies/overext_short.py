# ============================================================
# File   : trading/strategies/overext_short.py
# 2026-10-19: 日足で上がりすぎた銘柄の崩れ売り (OVEREXT_SHORT)、制度信用で翌日まで持つ
# ------------------------------------------------------------
# 前日終値が日足 25 日平均から +50% 以上離れた銘柄が、当日の高値から -8% 下げたら制度信用で売り、
# 翌営業日の大引け前 (15:15) に買い戻す。ユーザー判断 (10/19「作って、制度信用で実際に発注して」)。
#
# 根拠 (ops/research/daily_overext_short_scan.py、5 分足 2026/05/25〜10/16):
#   乖離 +50% 以上・高値 -8% で売り: 180 回、翌日大引け 平均 +6.36% 勝率 67% (前半 +6.1% / 後半 +6.7%)、
#   1 銘柄 1 週 1 回にしても 120 回 +6.0%。翌日寄りなら +4.41%。当日中の買い戻しは -0.33% (利益は翌日の下げ)。
#   ただし負けは大きい: 下位 5% -15〜-18%、最悪 -27% (593A 8/28)。当日中の +5% 損切りは成績をマイナスにする。
#   きっかけは 4382 (10/02 855 円 -> 10/16 1,754 円、25 日平均 +81%、10/19 に高値 1,877 円から -19%)。
#
# 構成:
#   run_cycle()   : 20 秒ごと。候補 (日足 DB の乖離 >= 50% かつ当日売買代金が多い) の板を見て、高値 -8% で売る。
#   handle_exit() : exit_loop の先頭で呼ぶ。この戦略の建玉なら 翌営業日 15:15 の時間決済と、証券会社の逆指値
#                   (建値 +20%、毎朝置き直す) だけで扱い、通常の決済ルールは通さない。
#   制度信用は 15:24:30 の強制決済の対象なので、overnight_holds に登録して外す。
#   状態は %LOCALAPPDATA%\autostock\runtime\overext_short_state.json (日をまたいで持つ)。
#
# 止め方: OVEREXT_SHORT_ENABLED=0 (新規だけ止まる。保有中の決済は続く)。
# ============================================================
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from trading.strategies.surge_short import _env_f, _env_on, _hm, _pt, _ts, _tick_up, _cancel, limit_up_price

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_CAND_CACHE: Dict[str, Any] = {"day": None, "ts": None, "map": {}}


def cfg() -> Dict[str, Any]:
    return {
        "enabled": _env_on("OVEREXT_SHORT_ENABLED", True),
        "min_dev25": _env_f("OVEREXT_SHORT_MIN_DEV25_PCT", 50.0),       # 前日終値の 25 日平均乖離 (%)
        "drop_pct": _env_f("OVEREXT_SHORT_DROP_FROM_HIGH_PCT", 8.0),    # 当日高値からの下げ (%)
        "start": _hm("OVEREXT_SHORT_START", "09:30"),
        "end": _hm("OVEREXT_SHORT_END", "14:30"),
        "exit_time": _hm("OVEREXT_SHORT_EXIT_TIME", "15:15"),          # 翌営業日のこの時刻以降に買い戻す
        "max_yen": _env_f("OVEREXT_SHORT_MAX_YEN", 150000.0),
        "max_per_day": int(_env_f("OVEREXT_SHORT_MAX_PER_DAY", 2)),
        "max_open": int(_env_f("OVEREXT_SHORT_MAX_OPEN", 3)),
        "min_trading_value": _env_f("OVEREXT_SHORT_MIN_TRADING_VALUE", 3e8),  # 当日の売買代金 (前日 3 億円の代わり)
        "min_price": _env_f("OVEREXT_SHORT_MIN_PRICE", 300.0),
        "max_spread_pct": _env_f("OVEREXT_SHORT_MAX_SPREAD_PCT", 0.5),
        "stop_pct": _env_f("OVEREXT_SHORT_BROKER_STOP_PCT", 20.0),     # 証券会社の逆指値 (建値 +X%)
        "broker_stop": _env_on("OVEREXT_SHORT_BROKER_STOP", True),
        "margin_trade_type": int(_env_f("OVEREXT_SHORT_MARGIN_TRADE_TYPE", 1)),  # 1 = 制度信用
    }


# ------------------------------------------------------------
# 状態 (日をまたぐので 1 ファイル)
# ------------------------------------------------------------
def _state_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    p = Path(base) / "autostock" / "runtime" / "overext_short_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> Dict[str, Any]:
    p = _state_path()
    try:
        if p.exists():
            st = json.loads(p.read_text(encoding="utf-8"))
            st.setdefault("seen", {})
            st.setdefault("trades", {})
            return st
    except Exception:
        logger.warning("[OVEREXT SHORT] state read failed path=%s", p, exc_info=True)
    return {"seen": {}, "trades": {}}


def _save(st: Dict[str, Any]) -> None:
    p = _state_path()
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        logger.warning("[OVEREXT SHORT] state write failed path=%s", p, exc_info=True)


def _update_trade(symbol: str, **kw) -> None:
    with _LOCK:
        st = _load()
        t = st["trades"].get(str(symbol))
        if t is not None:
            t.update(kw)
            _save(st)


def _open_trades(st: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {s: t for s, t in st["trades"].items() if not t.get("closed")}


# ------------------------------------------------------------
# 外部データ
# ------------------------------------------------------------
def _candidates(now: dt.datetime, c: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """日足 DB で前日終値の 25 日平均乖離が min_dev25 以上の銘柄。30 分ごとに読み直す。"""
    day = now.date().isoformat()
    if _CAND_CACHE["day"] == day and _CAND_CACHE["ts"] and (now - _CAND_CACHE["ts"]).total_seconds() < 1800:
        return _CAND_CACHE["map"]
    out: Dict[str, Dict[str, Any]] = {}
    try:
        from trading.summary.mtf.daily_mtf_loader import load_daily_mtf_latest_map
        for sym, r in (load_daily_mtf_latest_map() or {}).items():
            try:
                close, ma25 = float(r.get("daily_close") or 0), float(r.get("daily_ma25") or 0)
                ddate = str(r.get("daily_date") or "")
            except Exception:
                continue
            # 前営業日の値だけ使う (古い日付の行は乖離が実態と違う)
            if close <= 0 or ma25 <= 0 or not ddate or ddate >= day:
                continue
            if (dt.date.fromisoformat(day) - dt.date.fromisoformat(ddate)).days > 5:
                continue
            dev = (close / ma25 - 1) * 100
            if dev >= c["min_dev25"]:
                out[str(sym)] = {"prev_close": close, "ma25": ma25, "dev25": round(dev, 1), "daily_date": ddate}
    except Exception:
        logger.warning("[OVEREXT SHORT] daily db unavailable", exc_info=True)
        return _CAND_CACHE["map"] if _CAND_CACHE["day"] == day else {}
    _CAND_CACHE.update(day=day, ts=now, map=out)
    logger.warning("[OVEREXT SHORT] candidates day=%s n=%s top=%s", day, len(out),
                   sorted(((s, v["dev25"]) for s, v in out.items()), key=lambda x: -x[1])[:15])
    return out


def _board(symbol: str) -> Optional[Dict[str, Any]]:
    """kabu の REST 板 (当日高値・現在値・気配)。BidPrice/AskPrice は kabu の命名が逆なので min/max で取る。"""
    try:
        from trading.entry import candidate_bridge
        candidate_bridge.publish([symbol])
    except Exception:
        logger.debug("[OVEREXT SHORT] candidate publish failed symbol=%s", symbol, exc_info=True)
    try:
        from kabu_api import kabu_http
        from kabu_api.utils import API_URL, get_valid_token
        j = kabu_http.request_json(f"{API_URL}/board/{symbol}@1", method="GET", api_key=get_valid_token(), timeout=3.0)
        if not isinstance(j, dict):
            return None
        p1, p2 = float(j.get("BidPrice") or 0), float(j.get("AskPrice") or 0)
        return {"high": float(j.get("HighPrice") or 0), "price": float(j.get("CurrentPrice") or 0),
                "prev_close": float(j.get("PreviousClose") or 0), "trading_value": float(j.get("TradingValue") or 0),
                "bid": min(p1, p2) if p1 > 0 and p2 > 0 else 0.0, "ask": max(p1, p2) if p1 > 0 and p2 > 0 else 0.0,
                "name": j.get("SymbolName")}
    except Exception:
        logger.debug("[OVEREXT SHORT] board failed symbol=%s", symbol, exc_info=True)
        return None


def _liquidity_map() -> Dict[str, Dict[str, float]]:
    from trading.ranking.active_symbols.ranking_source import build_liquidity_map
    return build_liquidity_map() or {}


def _other_strategy_has(symbol: str) -> bool:
    """同じ銘柄を SURGE_SHORT が今日持っていれば触らない (決済が二重になる)。"""
    try:
        from trading.strategies.surge_short import _load as _surge_load
        t = (_surge_load().get("trades") or {}).get(str(symbol))
        return bool(t and not t.get("closed"))
    except Exception:
        return False


# ------------------------------------------------------------
# 新規
# ------------------------------------------------------------
def run_cycle(now: Optional[dt.datetime] = None) -> None:
    c = cfg()
    if not c["enabled"]:
        return
    now = now or dt.datetime.now()
    if not (c["start"] <= now.time() <= c["end"]):
        return
    cands = _candidates(now, c)
    if not cands:
        return
    try:
        lm = _liquidity_map()
    except Exception:
        lm = {}
    today = now.date().isoformat()
    with _LOCK:
        st = _load()
        seen_today = st["seen"].setdefault(today, {})
        entries_today = sum(1 for t in st["trades"].values() if str(t.get("order_ts", ""))[:10] == today)
        changed = False
        for sym, info in sorted(cands.items(), key=lambda kv: -kv[1]["dev25"]):
            if seen_today.get(sym, {}).get("status") in ("ordered", "skipped"):
                continue
            if sym in _open_trades(st):
                continue
            v = lm.get(sym) or {}
            # 売買代金がまだ少ない銘柄は板を見に行かない (REST 呼び出しを減らす)
            if v and float(v.get("trading_value") or 0) < c["min_trading_value"]:
                continue
            if not v and len(cands) > 20:
                continue
            bd = _board(sym)
            if not bd or bd["high"] <= 0 or bd["price"] <= 0:
                continue
            drop = (1 - bd["price"] / bd["high"]) * 100
            if drop < c["drop_pct"]:
                continue
            if entries_today >= c["max_per_day"]:
                seen_today[sym] = {"status": "skipped", "reason": "max_per_day", "ts": _ts(now)}
                changed = True
                continue
            res, reason, detail = _try_entry(sym, info, bd, st, now, c)
            detail = {**detail, "dev25": info["dev25"], "drop_pct": round(drop, 2), "high": bd["high"], "price": bd["price"]}
            if res in ("ordered", "skipped"):
                seen_today[sym] = {"status": res, "reason": reason, "detail": detail, "ts": _ts(now)}
                logger.warning("[OVEREXT SHORT] %s symbol=%s reason=%s detail=%s", res.upper(), sym, reason, detail)
                if res == "ordered":
                    entries_today += 1
            else:
                seen_today[sym] = {"status": "retry", "reason": reason, "detail": detail, "ts": _ts(now)}
            changed = True
        if changed:
            _save(st)


def _try_entry(sym: str, info: Dict[str, Any], bd: Dict[str, Any], st: Dict[str, Any], now: dt.datetime, c: Dict[str, Any]):
    """('ordered'|'skipped'|'retry', reason, detail)"""
    if len(_open_trades(st)) >= c["max_open"]:
        return "retry", "max_open", {}
    if _other_strategy_has(sym):
        return "skipped", "surge_short_holds", {}
    price = bd["price"]
    if price < c["min_price"]:
        return "skipped", "price_too_low", {}
    if bd["trading_value"] and bd["trading_value"] < c["min_trading_value"]:
        return "retry", "trading_value_low", {"trading_value": bd["trading_value"]}
    bid, ask = bd["bid"], bd["ask"]
    if not (ask > bid > 0):
        return "retry", "board_invalid", {"bid": bid, "ask": ask}
    spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
    if spread_pct > c["max_spread_pct"]:
        return "retry", "spread_too_wide", {"spread_pct": round(spread_pct, 3)}
    # 制度信用で売れるのは貸借銘柄だけ (symbol_flags.credit_type)
    if c["margin_trade_type"] == 1:
        try:
            from AI.sell_credit_guard import _load_flags_from_symbol_flags_db
            ct = str((_load_flags_from_symbol_flags_db(sym) or {}).get("credit_type") or "")
        except Exception:
            ct = ""
        if ct and ct != "貸借銘柄":
            return "skipped", "not_taishaku", {"credit_type": ct}
    qty = int(c["max_yen"] // (bid * 100)) * 100
    if qty <= 0:
        return "skipped", "too_expensive", {"bid": bid, "max_yen": c["max_yen"]}
    from kabu_api.send_order import margin_trade_type_override
    from trading.handlers.entry_handler import place_entry_sell
    # 強制決済で返済されないよう、発注の前に持ち越し登録する (約定しなくても害はない)
    from trading.strategies.overnight_holds import register as _register_overnight
    _register_overnight(sym, "OVEREXT_SHORT", days=5)
    with margin_trade_type_override(c["margin_trade_type"]):
        oid = place_entry_sell(sym, bd.get("name") or sym, bid, "OVEREXT_SHORT", order_type="LIMIT", qty=qty)
    detail = {"bid": bid, "ask": ask, "spread_pct": round(spread_pct, 3), "qty": qty,
              "prev_close": bd.get("prev_close") or info.get("prev_close"), "margin_trade_type": c["margin_trade_type"]}
    if not oid:
        return "skipped", "send_failed", detail
    st["trades"][sym] = {"order_ts": _ts(now), "entry_day": now.date().isoformat(), "order_id": str(oid),
                         "price": bid, "qty": qty, **detail}
    return "ordered", "ok", {**detail, "order_id": str(oid)}


# ------------------------------------------------------------
# 決済 (exit_loop から呼ぶ)
# ------------------------------------------------------------
def _broker_short(symbol: str) -> Optional[Dict[str, Any]]:
    """証券会社の建玉からこの銘柄の売り建て (残数・信用区分)。読めなければ None、無ければ qty=0。"""
    try:
        from kabu_api.positions import get_positions
        rows = get_positions()
        if not isinstance(rows, list):
            return None
        q, mtt = 0, None
        for r in rows:
            if not isinstance(r, dict) or str(r.get("Symbol") or "") != str(symbol) or str(r.get("Side")) != "1":
                continue
            q += int(float(r.get("LeavesQty") or 0))
            mtt = int(float(r.get("MarginTradeType") or 0)) or mtt
        return {"qty": q, "margin_trade_type": mtt}
    except Exception:
        logger.warning("[OVEREXT SHORT] broker positions read failed symbol=%s", symbol, exc_info=True)
        return None


def _place_broker_stop(symbol: str, pos: Dict[str, Any], entry: float, c: Dict[str, Any], prev_close: float) -> Optional[str]:
    """買い戻しの逆指値 (建値 +stop_pct%、ストップ高の 1 刻み手前まで)。当日限りなので毎朝置き直す。"""
    b = _broker_short(symbol)
    if not b or not b["qty"]:
        logger.warning("[OVEREXT SHORT] broker stop skipped (no broker short) symbol=%s", symbol)
        return None
    trig, tick = _tick_up(entry * (1 + c["stop_pct"] / 100.0))
    lu = limit_up_price(prev_close) if prev_close > 0 else 0.0
    if lu > 0 and trig >= lu:
        trig = lu - tick
    try:
        from kabu_api.close import Password
        from kabu_api.send_order import send_order_common
        exch = int(float((pos or {}).get("exchange") or 1))
        if exch == 9:
            exch = 1
        payload = {
            "Password": Password, "Symbol": str(symbol), "Exchange": exch, "SecurityType": 1,
            "Side": "2", "CashMargin": 3, "MarginTradeType": int(b.get("margin_trade_type") or c["margin_trade_type"]),
            "DelivType": 2, "AccountType": int(float((pos or {}).get("account_type") or 4)),
            "Qty": int(b["qty"]), "ClosePositionOrder": 0, "FrontOrderType": 30, "Price": 0, "ExpireDay": 0,
            "ReverseLimitOrder": {"TriggerSec": 1, "TriggerPrice": float(trig), "UnderOver": 2,
                                  "AfterHitOrderType": 1, "AfterHitPrice": 0},
        }
        res = send_order_common(payload)
        oid = (res or {}).get("OrderId") if isinstance(res, dict) else None
        logger.warning("[OVEREXT SHORT] broker stop %s symbol=%s qty=%s trigger=%.1f entry=%.1f limit_up=%.1f res=%s",
                       "PLACED" if oid else "FAILED", symbol, b["qty"], trig, entry, lu, res)
        return str(oid) if oid else None
    except Exception:
        logger.exception("[OVEREXT SHORT] broker stop send failed symbol=%s", symbol)
        return None


def protected_order_ids() -> set:
    """force_cancel_loop が取消してはいけない注文 (この戦略の逆指値)。"""
    try:
        with _LOCK:
            st = _load()
        return {str(t.get("stop_oid")) for t in st["trades"].values() if t.get("stop_oid") and not t.get("closed")}
    except Exception:
        return set()


def handle_exit(symbol: str, pos: Dict[str, Any], now: Optional[dt.datetime] = None) -> Optional[bool]:
    """この戦略の建玉なら True/False (呼び出し側は以降の決済ルールを通さない)。違えば None。"""
    try:
        with _LOCK:
            t = dict((_load()["trades"]).get(str(symbol)) or {})
        if not t or t.get("closed"):
            return None
        if not str((pos or {}).get("side") or "").upper().startswith("SELL"):
            return None
        c = cfg()
        now = now or dt.datetime.now()
        today = now.date().isoformat()
        from trading.exit.exit_position_runner import _resolve_entry_price
        from trading.exit.exit_price_source import get_latest_exit_price
        entry = float(_resolve_entry_price(pos) or t.get("price") or 0)
        price, _bar = get_latest_exit_price(symbol)
        price = float(price or 0)
        if entry <= 0 or price <= 0:
            return False

        # 逆指値は当日限り。その日まだ置いていなければ置く (3 回まで)。寄り前は置かない
        if c["broker_stop"] and t.get("stop_day") != today and dt.time(9, 0) <= now.time() < dt.time(15, 25):
            att = int(t.get("stop_attempts_day", 0)) if t.get("stop_attempts_on") == today else 0
            if att < 3:
                bd = _board(symbol) or {}
                oid = _place_broker_stop(symbol, pos, entry, c, float(bd.get("prev_close") or t.get("prev_close") or 0))
                upd = {"stop_attempts_on": today, "stop_attempts_day": att + 1, "entry_fill": entry}
                if oid:
                    upd.update(stop_oid=oid, stop_day=today)
                _update_trade(symbol, **upd)
                t.update(upd)

        # 逆指値の発動ラインを超えて建玉が無ければ、証券会社側で損切りされた
        if price >= entry * (1 + c["stop_pct"] / 100.0):
            b = _broker_short(symbol)
            if b is not None and b["qty"] == 0:
                logger.warning("[OVEREXT SHORT] broker stop filled symbol=%s entry=%.1f price=%.1f", symbol, entry, price)
                _close_record(symbol, now, price, "BROKER_STOP")
                return True

        reason = None
        if today > str(t.get("entry_day") or today) and now.time() >= c["exit_time"]:
            reason = f"OVEREXT_SHORT_TIME entry_day={t.get('entry_day')} entry={entry:.1f} price={price:.1f}"
        elif not t.get("stop_oid") and price >= entry * (1 + c["stop_pct"] / 100.0):
            reason = f"OVEREXT_SHORT_STOP pct={c['stop_pct']:.0f} entry={entry:.1f} price={price:.1f} (system)"
        if not reason:
            return False

        stop_oid = t.get("stop_oid") if t.get("stop_day") == today else None
        if stop_oid:
            _cancel(stop_oid, symbol, reason.split()[0].lower())
        b = _broker_short(symbol)
        if b is not None and b["qty"] == 0:
            logger.warning("[OVEREXT SHORT] no broker short left symbol=%s reason=%s", symbol, reason)
            _close_record(symbol, now, price, "BROKER_STOP_OR_GONE")
            return True
        from trading.exit.exit_finalize import finalize_exit
        pnl = entry - price
        finalize_exit(symbol=symbol, price=price, reason=reason, cluster_id=0, regime=0, inago_state=0,
                      pnl=pnl, collapse_prob=0.0, ctx=None)
        logger.warning("[OVEREXT SHORT] EXIT symbol=%s reason=%s pnl_per_share=%.1f", symbol, reason, pnl)
        _close_record(symbol, now, price, reason.split()[0])
        return True
    except Exception:
        logger.exception("[OVEREXT SHORT] handle_exit failed symbol=%s", symbol)
        return None


def _close_record(symbol: str, now: dt.datetime, price: float, reason: str) -> None:
    _update_trade(symbol, closed=True, exit_ts=_ts(now), exit_price=price, exit_reason=reason)
    try:
        from trading.strategies.overnight_holds import unregister
        unregister(symbol)
    except Exception:
        pass


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
        logger.exception("[OVEREXT SHORT] cycle failed")


def register_scheduler_safe() -> bool:
    try:
        import schedule
        from core.startup.scheduler.helpers import has_schedule_tag
        if has_schedule_tag("overext_short"):
            return True
        every = int(_env_f("OVEREXT_SHORT_INTERVAL_SEC", 20))
        schedule.every(every).seconds.do(_run_guarded).tag("overext_short", "entry")
        logger.warning("[OVEREXT SHORT] scheduler registered every=%ss cfg=%s", every,
                       {k: (str(v) if isinstance(v, dt.time) else v) for k, v in cfg().items()})
        return True
    except Exception:
        logger.exception("[OVEREXT SHORT] scheduler register failed")
        return False


__all__ = ["run_cycle", "handle_exit", "register_scheduler_safe", "cfg", "protected_order_ids"]
