# ============================================================
# File   : trading/strategies/surge_short.py
# 2026-10-03: 急騰銘柄の空売り (SURGE_SHORT)
# ------------------------------------------------------------
# 当日はじめて前日終値 +12% を超えた銘柄を、一般信用デイトレで空売りし、
# 60 分後に買い戻す。+5% 逆行したら損切り。
#
# 根拠 (2026-10-02 検証):
#   TradingView 5 分足 2015〜2025 / 1,531 銘柄 / 5,390 回:
#     60 分後 平均 +2.05% (コスト前)、損切り 5% で +1.58%・勝率 64%・最悪 -5.3%。
#     2019〜2025 の各年、2023/09〜2025/12 の各月がプラス。損切りなしは最悪 -38%。
#   push DB 実気配 (2026/09、システムの銘柄): 60 分後 +0.78% (スプレッド込み)。
#   一日信用の在庫: 候補 294 銘柄中 277 で可、プレミアム料は株価の 0.07〜0.37%。
#
# 構成:
#   run_cycle()   : 20 秒ごと (main.py の schedule)。候補を探して発注する。
#   handle_exit() : exit_loop の先頭で呼ぶ。この戦略の建玉なら 損切り/時間 だけで決済し、
#                   通常の決済ルール (日足 ATR 損切り・トレール・blowoff など) は通さない。
#   状態は %LOCALAPPDATA%\autostock\runtime\surge_short_YYYYMMDD.json (再起動しても残る)。
#
# 止め方: SURGE_SHORT_ENABLED=0 (新規だけ止まる。保有中の決済は続く)。
# ============================================================
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, Optional

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
        "enabled": _env_on("SURGE_SHORT_ENABLED", True),
        "chg_pct": _env_f("SURGE_SHORT_MIN_CHANGE_PCT", 12.0),
        "start": _hm("SURGE_SHORT_START", "09:15"),
        "end": _hm("SURGE_SHORT_END", "14:30"),
        "hold_min": _env_f("SURGE_SHORT_HOLD_MIN", 60.0),
        "stop_pct": _env_f("SURGE_SHORT_STOP_PCT", 5.0),
        "max_yen": _env_f("SURGE_SHORT_MAX_YEN", 300000.0),
        "max_per_day": int(_env_f("SURGE_SHORT_MAX_PER_DAY", 5)),
        "max_open": int(_env_f("SURGE_SHORT_MAX_OPEN", 3)),
        "min_trading_value": _env_f("SURGE_SHORT_MIN_TRADING_VALUE", 1e8),
        "min_price": _env_f("SURGE_SHORT_MIN_PRICE", 300.0),
        "max_spread_pct": _env_f("SURGE_SHORT_MAX_SPREAD_PCT", 0.30),
        "max_premium_pct": _env_f("SURGE_SHORT_MAX_PREMIUM_PCT", 0.40),
        "retry_sec": _env_f("SURGE_SHORT_RETRY_SEC", 180.0),
        # 2026-10-03: 踏み上げ対策。ストップ高まで この % 以上残っている時だけ売る。
        "min_limit_dist_pct": _env_f("SURGE_SHORT_MIN_LIMIT_DIST_PCT", 3.0),
        # 2026-10-03: 利確 (建値から -5%) と、証券会社側の逆指値 (損切り) を使う。
        "take_profit_pct": _env_f("SURGE_SHORT_TAKE_PROFIT_PCT", 5.0),
        "broker_stop": _env_on("SURGE_SHORT_BROKER_STOP", True),
        "backup_stop_pct": _env_f("SURGE_SHORT_BACKUP_STOP_PCT", 7.0),
        "force_close": _hm("SURGE_SHORT_FORCE_CLOSE", "15:20"),
    }


# ------------------------------------------------------------
# 踏み上げ対策: ストップ高の値段 (東証の制限値幅、前日終値基準)
#   TradingView 5 分足 5,390 回: 空売り後 14:50 以降ずっとストップ高に張り付いたのは 5.1%。
#   張り付くと当日中に買い戻せず、翌日寄りで建値比 平均 +9.0% (90% 点 +18%、最悪 +176%) の損。
#   張り付き率は ストップ高までの残りが 5% 以下で 6.6%、5〜8% で 3.5%、8〜12% で 2.7%、12〜20% で 1.1%。
#   張り付いた時の損も含めた 60 分損益: 制限なし +1.59% (最悪 -16.4%) / 残り 3% 以上 +1.12% (最悪 -7.0%)
#   / 5% 以上 +0.79% / 8% 以上 +0.55%。ユーザー判断で 3% 以上 (10/3)。
#   損切りの逆指値はストップ高の 1 刻み手前を上限にする (ストップ高に張り付く前に発動させる)。
# ------------------------------------------------------------
_PRICE_LIMITS = [
    (100, 30), (200, 50), (500, 80), (700, 100), (1000, 150), (1500, 300), (2000, 400),
    (3000, 500), (5000, 700), (7000, 1000), (10000, 1500), (15000, 3000), (20000, 4000),
    (30000, 5000), (50000, 7000), (70000, 10000), (100000, 15000), (150000, 30000),
    (200000, 40000), (300000, 50000), (500000, 70000), (700000, 100000), (1000000, 150000),
]


def limit_up_price(prev_close: float) -> float:
    for upper, width in _PRICE_LIMITS:
        if prev_close < upper:
            return prev_close + width
    return prev_close * 1.3


# ------------------------------------------------------------
# 状態 (日付ごとの JSON)
# ------------------------------------------------------------
def _state_path(day: Optional[dt.date] = None) -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    d = (day or dt.date.today()).strftime("%Y%m%d")
    p = Path(base) / "autostock" / "runtime" / f"surge_short_{d}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> Dict[str, Any]:
    p = _state_path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[SURGE SHORT] state read failed path=%s", p, exc_info=True)
    return {"seen": {}, "trades": {}}


def _save(st: Dict[str, Any]) -> None:
    p = _state_path()
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        logger.warning("[SURGE SHORT] state write failed path=%s", p, exc_info=True)


def _ts(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _pt(s: str) -> dt.datetime:
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------
# 外部データ
# ------------------------------------------------------------
def _liquidity_map() -> Dict[str, Dict[str, float]]:
    from trading.ranking.active_symbols.ranking_source import build_liquidity_map
    return build_liquidity_map() or {}


def _margin_premium(symbol: str) -> Optional[Dict[str, Any]]:
    """一般信用デイトレの空売り可否とプレミアム料 (円/株)。取れなければ None。"""
    try:
        from kabu_api import kabu_http
        from kabu_api.utils import API_URL, get_valid_token
        j = kabu_http.request_json(f"{API_URL}/margin/marginpremium/{symbol}", method="GET",
                                   api_key=get_valid_token(), timeout=3.0)
        return (j or {}).get("DayTrade") if isinstance(j, dict) else None
    except Exception:
        logger.debug("[SURGE SHORT] marginpremium failed symbol=%s", symbol, exc_info=True)
        return None


def _board(symbol: str) -> Optional[Dict[str, Any]]:
    """REST 板。/board は PUSH 登録銘柄しか返さないので、先に登録を依頼する。"""
    try:
        from trading.entry import candidate_bridge
        candidate_bridge.publish([symbol])
    except Exception:
        logger.debug("[SURGE SHORT] candidate publish failed symbol=%s", symbol, exc_info=True)
    try:
        from trading.handlers.entry_order_builder import _rfb_fetch_board
        return _rfb_fetch_board(symbol, force=True)
    except Exception:
        logger.debug("[SURGE SHORT] board failed symbol=%s", symbol, exc_info=True)
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
    with _LOCK:
        st = _load()
        try:
            lm = _liquidity_map()
        except Exception:
            logger.warning("[SURGE SHORT] liquidity map unavailable", exc_info=True)
            return
        cands = sorted(((s, v) for s, v in lm.items() if float(v.get("change_percentage") or 0) >= c["chg_pct"]),
                       key=lambda kv: -float(kv[1].get("change_percentage") or 0))
        changed = False
        for sym, v in cands:
            seen = st["seen"].get(sym)
            if seen is None:
                seen = st["seen"][sym] = {"first_ts": _ts(now), "chg": float(v.get("change_percentage") or 0), "status": "new"}
                changed = True
                logger.warning("[SURGE SHORT] first cross symbol=%s chg=%.2f%% price=%s tv=%.0f",
                               sym, seen["chg"], v.get("current_price"), float(v.get("trading_value") or 0))
            if seen["status"] != "new":
                continue
            if now - _pt(seen["first_ts"]) > dt.timedelta(seconds=c["retry_sec"]):
                seen.update(status="skipped", reason="retry_expired"); changed = True
                continue
            result, reason, detail = _try_entry(sym, v, st, now, c)
            if result in ("ordered", "skipped"):
                seen.update(status=result, reason=reason, detail=detail, decided_ts=_ts(now)); changed = True
                logger.warning("[SURGE SHORT] %s symbol=%s reason=%s detail=%s", result.upper(), sym, reason, detail)
            else:
                seen["last_retry_reason"] = reason; changed = True
        if changed:
            _save(st)


def _try_entry(sym: str, v: Dict[str, Any], st: Dict[str, Any], now: dt.datetime, c: Dict[str, Any]):
    """('ordered'|'skipped'|'retry', reason, detail)"""
    entries = sum(1 for t in (st.get("trades") or {}).values())
    if entries >= c["max_per_day"]:
        return "skipped", "max_per_day", {"entries": entries}
    if _open_count(st, now, c["hold_min"]) >= c["max_open"]:
        return "retry", "max_open", {}
    price = float(v.get("current_price") or 0)
    tv = float(v.get("trading_value") or 0)
    if price < c["min_price"]:
        return "skipped", "price_too_low", {"price": price}
    chg = float(v.get("change_percentage") or 0)
    # ranking の騰落率から前日終値を逆算する。端数で制限値幅の境目 (1,000 円など) をまたがないよう 1 円に丸める。
    prev_close = float(round(price / (1 + chg / 100.0))) if chg > -100 else 0.0
    if prev_close > 0:
        lu = limit_up_price(prev_close)
        dist = (lu / price - 1) * 100
        if price >= lu * 0.999:
            return "skipped", "at_limit_up", {"price": price, "limit_up": lu}
        if dist < c["min_limit_dist_pct"]:
            return "skipped", "too_close_to_limit_up", {"price": price, "prev_close": round(prev_close, 1),
                                                         "limit_up": lu, "dist_pct": round(dist, 2)}
    if tv < c["min_trading_value"]:
        return "retry", "trading_value_low", {"trading_value": tv}
    try:
        from trading.entry.daily_risk_guard import should_allow_new_entry
        dec = should_allow_new_entry(sym)
        if not getattr(dec, "allow", True):
            return "skipped", f"daily_risk:{getattr(dec, 'reason', '')}", {}
    except Exception:
        logger.debug("[SURGE SHORT] daily risk guard failed", exc_info=True)
    mp = _margin_premium(sym)
    if mp is None:
        return "retry", "marginpremium_unavailable", {}
    if mp.get("MarginPremiumType") is None:
        return "skipped", "no_daytrade_short", {"marginpremium": mp}
    prem = float(mp.get("MarginPremium") or 0.0)
    prem_pct = prem / price * 100 if price > 0 else 999
    if prem_pct > c["max_premium_pct"]:
        return "skipped", "premium_too_high", {"premium_yen": prem, "premium_pct": round(prem_pct, 3)}
    bd = _board(sym)
    if not bd:
        return "retry", "board_unavailable", {}
    bid, ask = float(bd.get("bid") or 0), float(bd.get("ask") or 0)
    if not (ask > bid > 0):
        return "retry", "board_invalid", {"bid": bid, "ask": ask}
    spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
    if spread_pct > c["max_spread_pct"]:
        return "retry", "spread_too_wide", {"spread_pct": round(spread_pct, 3)}
    if prev_close > 0 and (limit_up_price(prev_close) / bid - 1) * 100 < c["min_limit_dist_pct"]:
        return "skipped", "too_close_to_limit_up_at_board", {"bid": bid, "limit_up": limit_up_price(prev_close)}
    qty = int(c["max_yen"] // (bid * 100)) * 100
    if qty <= 0:
        return "skipped", "too_expensive", {"bid": bid, "max_yen": c["max_yen"]}
    from trading.handlers.entry_handler import place_entry_sell
    oid = place_entry_sell(sym, sym, bid, "SURGE_SHORT", order_type="LIMIT", qty=qty)
    detail = {"bid": bid, "ask": ask, "spread_pct": round(spread_pct, 3), "qty": qty,
              "premium_pct": round(prem_pct, 3), "chg": float(v.get("change_percentage") or 0), "trading_value": tv}
    if not oid:
        return "skipped", "send_failed", detail
    st.setdefault("trades", {})[sym] = {"order_ts": _ts(now), "order_id": str(oid), "price": bid, "qty": qty,
                                        "limit_up": limit_up_price(prev_close) if prev_close > 0 else 0.0, **detail}
    return "ordered", "ok", {**detail, "order_id": str(oid)}


# ------------------------------------------------------------
# 決済 (exit_loop から呼ぶ)
#   約定を見つけたら証券会社に「買い戻しの逆指値」を置く (建値 +5%、発動後は成行)。
#   システムが止まっても取引所側で損切りされる。kabu には OCO が無いので、利確 (-5%) と
#   時間決済 (60 分 / 15:20) はシステムが監視し、逆指値を取消してから買い戻す。
#   逆指値を置けなかった時と、+7% でもまだ建玉が残っている時はシステムが成行で損切りする。
# ------------------------------------------------------------
def _broker_short_qty(symbol: str) -> Optional[int]:
    """証券会社の建玉一覧から、この銘柄の売り建て残数。読めなければ None。"""
    try:
        from kabu_api.positions import get_positions
        rows = get_positions()
        if not isinstance(rows, list):
            return None
        q = 0
        for r in rows:
            if not isinstance(r, dict) or str(r.get("Symbol") or "") != str(symbol):
                continue
            if str(r.get("Side")) != "1":   # 1=売建
                continue
            q += int(float(r.get("LeavesQty") or 0))
        return q
    except Exception:
        logger.warning("[SURGE SHORT] broker positions read failed symbol=%s", symbol, exc_info=True)
        return None


def _tick_up(price: float):
    import math
    try:
        from utils_common import get_order_tick_size
        t = float(get_order_tick_size(price))
    except Exception:
        t = 1.0
    return math.ceil(price / t - 1e-9) * t, t


def _place_broker_stop(symbol: str, pos: Dict[str, Any], t: Dict[str, Any], entry: float, c: Dict[str, Any]) -> Optional[str]:
    qty = _broker_short_qty(symbol)
    if not qty:
        logger.warning("[SURGE SHORT] broker stop skipped (no broker short qty) symbol=%s qty=%s", symbol, qty)
        return None
    trig, tick = _tick_up(entry * (1 + c["stop_pct"] / 100.0))
    lu = float(t.get("limit_up") or 0)
    if lu > 0 and trig >= lu:
        trig = lu - tick
    try:
        from kabu_api.close import Password
        from kabu_api.send_order import send_order_common
        exch = int(float((pos or {}).get("exchange") or 1))
        if exch == 9:   # SOR は返済に使えない
            exch = 1
        mtt = int(float((pos or {}).get("margin_trade_type") or 3))
        payload = {
            "Password": Password, "Symbol": str(symbol), "Exchange": exch, "SecurityType": 1,
            "Side": "2", "CashMargin": 3, "MarginTradeType": mtt, "DelivType": 2,
            "AccountType": int(float((pos or {}).get("account_type") or 4)),
            "Qty": int(qty), "ClosePositionOrder": 0,
            "FrontOrderType": 30, "Price": 0, "ExpireDay": 0,
            "ReverseLimitOrder": {"TriggerSec": 1, "TriggerPrice": float(trig), "UnderOver": 2,
                                  "AfterHitOrderType": 1, "AfterHitPrice": 0},
        }
        res = send_order_common(payload)
        oid = (res or {}).get("OrderId") if isinstance(res, dict) else None
        logger.warning("[SURGE SHORT] broker stop %s symbol=%s qty=%s trigger=%.1f entry=%.1f limit_up=%.1f res=%s",
                       "PLACED" if oid else "FAILED", symbol, qty, trig, entry, lu, res)
        return str(oid) if oid else None
    except Exception:
        logger.exception("[SURGE SHORT] broker stop send failed symbol=%s", symbol)
        return None


def _cancel(order_id: str, symbol: str, reason: str) -> bool:
    try:
        from kabu_api.cancel_order import cancel_order_common
        return bool(cancel_order_common(order_id, symbol=symbol, reason=reason))
    except Exception:
        logger.exception("[SURGE SHORT] cancel failed oid=%s", order_id)
        return False


def _update_trade(symbol: str, **kw) -> None:
    with _LOCK:
        st = _load()
        tt = st.get("trades", {}).get(str(symbol))
        if tt is not None:
            tt.update(kw)
            _save(st)


def protected_order_ids() -> set:
    """force_cancel_loop が取消してはいけない注文 (この戦略の逆指値)。"""
    try:
        with _LOCK:
            st = _load()
        return {str(t.get("stop_oid")) for t in (st.get("trades") or {}).values() if t.get("stop_oid")}
    except Exception:
        return set()


def handle_exit(symbol: str, pos: Dict[str, Any], now: Optional[dt.datetime] = None) -> Optional[bool]:
    """この戦略の建玉なら True/False を返す (呼び出し側は以降の決済ルールを通さない)。
    この戦略の建玉でなければ None。"""
    try:
        with _LOCK:
            st = _load()
            t = dict((st.get("trades") or {}).get(str(symbol)) or {})
        if not t or t.get("closed"):
            return None
        side = str((pos or {}).get("side") or "").upper()
        if not side.startswith("SELL"):
            return None
        c = cfg()
        now = now or dt.datetime.now()
        from trading.exit.exit_position_runner import _resolve_entry_price
        from trading.exit.exit_price_source import get_latest_exit_price
        entry = float(_resolve_entry_price(pos) or 0)
        price, _bar = get_latest_exit_price(symbol)
        price = float(price or 0)
        if entry <= 0 or price <= 0:
            return False

        # 約定後はじめて見えたら証券会社に逆指値を置く (3 回まで試す)
        if c["broker_stop"] and not t.get("stop_oid") and int(t.get("stop_attempts") or 0) < 3:
            oid = _place_broker_stop(symbol, pos, t, entry, c)
            t["stop_attempts"] = int(t.get("stop_attempts") or 0) + 1
            t["stop_oid"] = oid
            _update_trade(symbol, stop_attempts=t["stop_attempts"], stop_oid=oid, entry_fill=entry)

        stop_oid = t.get("stop_oid")
        # 逆指値の発動ラインを超えたら、証券会社側で約定済みか確かめて記録を閉じる
        if stop_oid and price >= entry * (1 + c["stop_pct"] / 100.0) and _broker_short_qty(symbol) == 0:
            logger.warning("[SURGE SHORT] broker stop filled symbol=%s entry=%.1f price=%.1f", symbol, entry, price)
            _update_trade(symbol, closed=True, exit_ts=_ts(now), exit_price=price, exit_reason="BROKER_STOP")
            return True
        reason = None
        if not stop_oid and price >= entry * (1 + c["stop_pct"] / 100.0):
            reason = f"SURGE_SHORT_STOP pct={c['stop_pct']:.1f} entry={entry:.1f} price={price:.1f} (system)"
        elif stop_oid and price >= entry * (1 + c["backup_stop_pct"] / 100.0):
            reason = f"SURGE_SHORT_BACKUP_STOP pct={c['backup_stop_pct']:.1f} entry={entry:.1f} price={price:.1f}"
        elif c["take_profit_pct"] > 0 and price <= entry * (1 - c["take_profit_pct"] / 100.0):
            reason = f"SURGE_SHORT_TAKE_PROFIT pct={c['take_profit_pct']:.1f} entry={entry:.1f} price={price:.1f}"
        elif now >= _pt(t["order_ts"]) + dt.timedelta(minutes=c["hold_min"]):
            reason = f"SURGE_SHORT_TIME hold={c['hold_min']:.0f}m entry={entry:.1f} price={price:.1f}"
        elif now.time() >= c["force_close"]:
            reason = f"SURGE_SHORT_FORCE_CLOSE at={c['force_close']} entry={entry:.1f} price={price:.1f}"
        if not reason:
            return False

        # 逆指値を取消してから、建玉が残っていることを確かめて買い戻す (二重返済しない)
        if stop_oid:
            _cancel(stop_oid, symbol, reason.split()[0].lower())
        left = _broker_short_qty(symbol)
        if left == 0:
            logger.warning("[SURGE SHORT] no broker short left (broker stop filled?) symbol=%s reason=%s", symbol, reason)
            _update_trade(symbol, closed=True, exit_ts=_ts(now), exit_reason="BROKER_STOP_OR_GONE")
            return True
        from trading.exit.exit_finalize import finalize_exit
        pnl = entry - price
        finalize_exit(symbol=symbol, price=price, reason=reason, cluster_id=0, regime=0, inago_state=0,
                      pnl=pnl, collapse_prob=0.0, ctx=None)
        logger.warning("[SURGE SHORT] EXIT symbol=%s reason=%s pnl_per_share=%.1f", symbol, reason, pnl)
        _update_trade(symbol, closed=True, exit_ts=_ts(now), exit_price=price, exit_reason=reason.split()[0])
        return True
    except Exception:
        logger.exception("[SURGE SHORT] handle_exit failed symbol=%s", symbol)
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
        logger.exception("[SURGE SHORT] cycle failed")


def register_scheduler_safe() -> bool:
    try:
        import schedule
        from core.startup.scheduler.helpers import has_schedule_tag
        if has_schedule_tag("surge_short"):
            return True
        every = int(_env_f("SURGE_SHORT_INTERVAL_SEC", 20))
        schedule.every(every).seconds.do(_run_guarded).tag("surge_short", "entry")
        logger.warning("[SURGE SHORT] scheduler registered every=%ss cfg=%s", every,
                       {k: (str(v) if isinstance(v, dt.time) else v) for k, v in cfg().items()})
        return True
    except Exception:
        logger.exception("[SURGE SHORT] scheduler register failed")
        return False


__all__ = ["run_cycle", "handle_exit", "register_scheduler_safe", "cfg", "protected_order_ids", "limit_up_price"]
