# ============================================================
# trading/entry/candidate_bridge.py
# ------------------------------------------------------------
# エントリー候補 (pending) の銘柄を、プロセスをまたいで PUSH 登録側へ渡す。
#
# 2026-09-15 の実測:
#   候補は main.py のメモリ (global_data.pending_entries) にあり、PUSH 登録を
#   行う push_receiver プロセスからは見えない。保護バケット P2_PENDING_* は
#   一日中 0 件で、候補銘柄が登録 50 銘柄に入らないままだった。
#   その結果、kabu の /board は未登録銘柄に 400 を返すので
#   BOARD_MISSING (12 件) と MTF ゲートの「データ無し」(84 件) になった。
#
# 仕組み:
#   main.py 側 (pending_manager.add_pending) が候補銘柄をローカルの JSON に書く。
#   push_receiver 側 (protected_symbols) がそれを P2_CANDIDATE_FILE として読み、
#   ローテーションの次の周回 (5 秒) で登録に含める。
#   ファイルは %LOCALAPPDATA%\autostock\runtime\entry_candidates.json
#   (ローカル書き。NAS は使わない)。古い候補 (CANDIDATE_BRIDGE_MAX_AGE_SEC、既定 180 秒)
#   は読み手が捨てる。
# ============================================================
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Iterable, List

logger = logging.getLogger(__name__)

VERSION = "V1.0-ENTRY-CANDIDATE-BRIDGE-FILE"
_lock = threading.Lock()
_last_write_ts = 0.0


def _path() -> Path:
    env = os.getenv("ENTRY_CANDIDATE_BRIDGE_PATH", "").strip()
    if env:
        return Path(env)
    base = os.getenv("LOCALAPPDATA") or os.getenv("TEMP") or "."
    return Path(base) / "autostock" / "runtime" / "entry_candidates.json"


def _enabled() -> bool:
    v = os.getenv("ENTRY_CANDIDATE_BRIDGE_ENABLED", "1")
    return str(v).strip().lower() not in ("0", "false", "no", "off")


def _norm(v) -> str:
    s = str(v or "").strip()
    if s.endswith(".0"):
        s = s[:-2]
    return s


def publish(symbols: Iterable, *, min_interval_sec: float = 1.0) -> bool:
    """候補銘柄を書く。短時間の連続呼び出しは間引く。失敗しても例外を投げない。"""
    global _last_write_ts
    if not _enabled():
        return False
    now = dt.datetime.now()
    ts = now.timestamp()
    with _lock:
        if ts - _last_write_ts < min_interval_sec:
            return False
        _last_write_ts = ts
    syms = sorted({_norm(s) for s in (symbols or []) if _norm(s)})
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        payload = {"updated_at": now.isoformat(timespec="seconds"), "symbols": syms, "pid": os.getpid()}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, p)
        return True
    except Exception:
        logger.debug("[CANDIDATE BRIDGE] publish failed", exc_info=True)
        return False


def load(max_age_sec: float | None = None) -> List[str]:
    """候補銘柄を読む。古ければ空。"""
    if not _enabled():
        return []
    if max_age_sec is None:
        try:
            max_age_sec = float(os.getenv("CANDIDATE_BRIDGE_MAX_AGE_SEC", "180"))
        except Exception:
            max_age_sec = 180.0
    try:
        p = _path()
        if not p.exists():
            return []
        with open(p, "r", encoding="utf-8") as f:
            payload = json.load(f)
        upd = dt.datetime.fromisoformat(str(payload.get("updated_at")))
        if (dt.datetime.now() - upd).total_seconds() > max_age_sec:
            return []
        return [_norm(s) for s in payload.get("symbols", []) if _norm(s)]
    except Exception:
        logger.debug("[CANDIDATE BRIDGE] load failed", exc_info=True)
        return []
