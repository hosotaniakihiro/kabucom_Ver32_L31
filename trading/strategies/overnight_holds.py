# ============================================================
# File   : trading/strategies/overnight_holds.py
# 2026-10-19: 持ち越してよい建玉の一覧 (15:24:30 の強制決済から外す)
# ------------------------------------------------------------
# kabu_api/daytrade_credit.py の強制決済は DAYTRADE_FORCE_CLOSE_ONLY_MARGIN_TYPE="0" で
# 制度信用 (MarginTradeType=1) の建玉も返済する。持ち越す前提の建玉 (OVEREXT_SHORT と、
# 15:00 以降に制度信用で建てた新規) はここに登録し、強制決済で飛ばす。
#
# 状態: %LOCALAPPDATA%\autostock\runtime\overnight_holds.json  {symbol: {"strategy", "since", "until"}}
#   until (YYYY-MM-DD) を過ぎた登録は無視する (登録したまま残っても永久には持ち越さない)。
# ============================================================
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)
_LOCK = threading.RLock()


def _path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    p = Path(base) / "autostock" / "runtime" / "overnight_holds.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load() -> Dict[str, Any]:
    p = _path()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("[OVERNIGHT HOLDS] read failed path=%s", p, exc_info=True)
    return {}


def _save(d: Dict[str, Any]) -> None:
    p = _path()
    tmp = p.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:
        logger.warning("[OVERNIGHT HOLDS] write failed path=%s", p, exc_info=True)


def register(symbol: str, strategy: str, *, days: int = 5) -> None:
    sym = str(symbol).strip()
    if not sym:
        return
    today = dt.date.today()
    with _LOCK:
        d = _load()
        d[sym] = {"strategy": strategy, "since": today.isoformat(), "until": (today + dt.timedelta(days=days)).isoformat()}
        _save(d)
    logger.warning("[OVERNIGHT HOLDS] register symbol=%s strategy=%s until=%s", sym, strategy, d[sym]["until"])


def unregister(symbol: str) -> None:
    with _LOCK:
        d = _load()
        if d.pop(str(symbol).strip(), None) is not None:
            _save(d)
            logger.warning("[OVERNIGHT HOLDS] unregister symbol=%s", symbol)


def is_overnight(symbol: str) -> bool:
    try:
        with _LOCK:
            v = _load().get(str(symbol).strip())
        if not v:
            return False
        return dt.date.today().isoformat() <= str(v.get("until") or "")
    except Exception:
        return False


__all__ = ["register", "unregister", "is_overnight"]
