# ============================================================
# File   : trading/strategies/tonosama_exit.py
# 2026-10-03: 殿様イナゴ (出来高急増) 由来の買い建玉の決済
# ------------------------------------------------------------
# 根拠 (backtest.db 2026/08/17〜09/25、出来高 10 倍以上・最終チェック通過・1 銘柄 1 日 1 回 191 件、
#        往復コスト 0.1% 控除後):
#   今の決済 (損切り ~0.8% + 0.2% トレール)  +0.12% (前半 +0.04 / 後半 +0.25)
#   損切り 1.5% + 60 分保有                   +0.29% (前半 +0.17 / 後半 +0.47)
#   一部の大きく伸びる回で稼ぐ形 (中央値は ~0、下位 5% は ~-1.1%) なので、早いトレールより持つ方が良い。
#
# 構成:
#   record_entry(): entry_handler.place_entry_buy が TONOSAMA 由来の発注を送った時に呼ぶ。
#   handle_exit() : exit_loop の先頭で呼ぶ。この建玉なら 損切り 1.5% / 60 分 / 15:20 だけで決済する。
#   状態は %LOCALAPPDATA%\autostock\runtime\tonosama_exit_YYYYMMDD.json。
# 止め方: TONOSAMA_SPECIAL_EXIT_ENABLED=0 (通常の決済ルールに戻る)。
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


def _enabled() -> bool:
    return str(os.getenv("TONOSAMA_SPECIAL_EXIT_ENABLED", "1")).strip().lower() not in ("0", "false", "no", "off")


def _path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    p = Path(base) / "autostock" / "runtime" / f"tonosama_exit_{dt.date.today():%Y%m%d}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> Dict[str, Any]:
    try:
        p = _path()
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("[TONOSAMA EXIT] state read failed", exc_info=True)
    return {"trades": {}}


def _save(st: Dict[str, Any]) -> None:
    p = _path()
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        logger.warning("[TONOSAMA EXIT] state write failed", exc_info=True)


def record_entry(symbol: str, order_id: str, price: Optional[float], qty: Optional[int], reason: str = "") -> None:
    if not _enabled():
        return
    now = dt.datetime.now()
    with _LOCK:
        st = _load()
        st.setdefault("trades", {})[str(symbol)] = {
            "order_ts": now.strftime("%Y-%m-%d %H:%M:%S"), "order_id": str(order_id),
            "price": price, "qty": qty, "reason": str(reason)[:200],
        }
        _save(st)
    logger.warning("[TONOSAMA EXIT] recorded symbol=%s oid=%s price=%s qty=%s", symbol, order_id, price, qty)


def handle_exit(symbol: str, pos: Dict[str, Any], now: Optional[dt.datetime] = None) -> Optional[bool]:
    """この建玉なら True/False (呼び出し側は以降の決済ルールを通さない)。そうでなければ None。"""
    if not _enabled():
        return None
    try:
        with _LOCK:
            t = dict((_load().get("trades") or {}).get(str(symbol)) or {})
        if not t or t.get("closed"):
            return None
        if not str((pos or {}).get("side") or "").upper().startswith("BUY"):
            return None
        now = now or dt.datetime.now()
        stop_pct = _env_f("TONOSAMA_EXIT_STOP_PCT", 1.5)
        hold_min = _env_f("TONOSAMA_EXIT_HOLD_MIN", 60.0)
        fc = str(os.getenv("TONOSAMA_EXIT_FORCE_CLOSE") or "15:20")
        fh, fm = [int(x) for x in fc.split(":")[:2]]
        from trading.exit.exit_position_runner import _resolve_entry_price
        from trading.exit.exit_price_source import get_latest_exit_price
        entry = float(_resolve_entry_price(pos) or 0)
        price = float((get_latest_exit_price(symbol) or (0, None))[0] or 0)
        if entry <= 0 or price <= 0:
            return False
        reason = None
        if price <= entry * (1 - stop_pct / 100.0):
            reason = f"TONOSAMA_STOP pct={stop_pct:.1f} entry={entry:.1f} price={price:.1f}"
        elif now >= dt.datetime.strptime(t["order_ts"], "%Y-%m-%d %H:%M:%S") + dt.timedelta(minutes=hold_min):
            reason = f"TONOSAMA_TIME hold={hold_min:.0f}m entry={entry:.1f} price={price:.1f}"
        elif now.time() >= dt.time(fh, fm):
            reason = f"TONOSAMA_FORCE_CLOSE at={fc} entry={entry:.1f} price={price:.1f}"
        if not reason:
            return False
        from trading.exit.exit_finalize import finalize_exit
        finalize_exit(symbol=symbol, price=price, reason=reason, cluster_id=0, regime=0, inago_state=0,
                      pnl=price - entry, collapse_prob=0.0, ctx=None)
        logger.warning("[TONOSAMA EXIT] EXIT symbol=%s reason=%s", symbol, reason)
        with _LOCK:
            st = _load()
            tt = st.get("trades", {}).get(str(symbol))
            if tt is not None:
                tt.update(closed=True, exit_ts=now.strftime("%Y-%m-%d %H:%M:%S"), exit_price=price, exit_reason=reason.split()[0])
                _save(st)
        return True
    except Exception:
        logger.exception("[TONOSAMA EXIT] handle_exit failed symbol=%s", symbol)
        return None


__all__ = ["record_entry", "handle_exit"]
