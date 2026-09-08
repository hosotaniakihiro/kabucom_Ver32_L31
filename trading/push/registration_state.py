# ============================================================
# File   : trading/push/registration_state.py
# Version: V1-PUSH-REGISTRATION-STATE-SINGLE-OWNER
# ------------------------------------------------------------
# kabu Station への PUSH 銘柄登録状態の正規所有者。
#
# 背景 (2026-09-01 12:56-13:00 の実測):
#   Ranking API が全滅 -> 登録候補が空 -> それでも unregister は走る
#   -> registered_count が 50 -> 0 -> 50 -> 0 と揺れ、
#   PUSH が止まり、Summary が空になり、Ranking 候補も作れなくなる、
#   という循環障害になっていた。
#
# 原因のひとつは「登録状態」がモジュールごとにバラバラに持たれていて、
#   ・これから登録したい銘柄
#   ・API に投げた銘柄
#   ・実際に kabu Station が受理した銘柄
#   ・監視表示用の銘柄
#   が区別されていなかったこと。
#
# ここでは 5 つを明確に分ける:
#   desired_symbols              まだ API に投げていない「登録したい」集合
#   requested_symbols            register API に投げた集合 (結果は未確定)
#   confirmed_registered_symbols register API が成功を返した集合 (これだけが真)
#   last_good_registered_symbols 直近で confirmed になった集合 (復旧用)
#   active_rotation_batch        A/B ローテーションの現在バッチ
#
# 規則:
#   * confirmed は register API 成功時にしか更新しない
#   * last_good は confirmed が非空になったときだけ更新する
#   * プロバイダーが一時的に空でも confirmed / last_good は消さない
#   * last_good はファイルへ保存し、プロセス再起動後も復旧に使える
# ============================================================

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

VERSION = "V1-PUSH-REGISTRATION-STATE-SINGLE-OWNER"

_LOCK = threading.RLock()

_desired: list[str] = []
_requested: list[str] = []
_confirmed: list[str] = []
_last_good: list[str] = []
_active_batch_label: str = ""
_active_batch: list[str] = []

_confirmed_at: float = 0.0
_last_good_at: float = 0.0
_last_good_loaded = False


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def state_path() -> str:
    """last-good を保存するファイル。プロセス再起動を跨いで復旧に使う。"""
    override = os.getenv("PUSH_REGISTRATION_STATE_PATH")
    if override:
        return override
    return os.path.join(tempfile.gettempdir(), "autostock_push_registration_state.json")


def normalize(symbols: Any) -> list[str]:
    """symbol 列を正規化する (重複除去・順序維持)。"""
    out: list[str] = []
    seen: set[str] = set()
    if not symbols:
        return out
    if isinstance(symbols, (str, bytes)):
        symbols = [symbols]
    try:
        it: Iterable = symbols
    except Exception:
        return out
    for x in it:
        try:
            if isinstance(x, dict):
                x = x.get("Symbol") or x.get("symbol") or ""
            s = str(x).strip().upper()
        except Exception:
            continue
        if not s or s in seen or s.lower() == "nan":
            continue
        seen.add(s)
        out.append(s)
    return out


# ------------------------------------------------------------
# last-good の永続化
# ------------------------------------------------------------

def _load_last_good_once() -> None:
    global _last_good, _last_good_at, _last_good_loaded
    if _last_good_loaded:
        return
    _last_good_loaded = True
    try:
        with open(state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        syms = normalize(data.get("last_good_registered_symbols"))
        if syms:
            _last_good = syms
            _last_good_at = float(data.get("saved_at") or 0.0)
            logger.warning(
                "[PUSH REG STATE] last_good loaded from disk count=%d saved_at=%s path=%s",
                len(syms), data.get("saved_at_str") or "-", state_path(),
            )
    except FileNotFoundError:
        pass
    except Exception:
        logger.debug("[PUSH REG STATE] last_good load failed", exc_info=True)


def _save_last_good() -> None:
    if not _env_bool("PUSH_REGISTRATION_STATE_PERSIST", True):
        return
    try:
        payload = {
            "last_good_registered_symbols": list(_last_good),
            "active_rotation_batch": list(_active_batch),
            "active_rotation_label": _active_batch_label,
            "saved_at": time.time(),
            "saved_at_str": time.strftime("%Y-%m-%d %H:%M:%S"),
            "pid": os.getpid(),
        }
        path = state_path()
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        logger.debug("[PUSH REG STATE] last_good save failed", exc_info=True)


# ------------------------------------------------------------
# 更新 API
# ------------------------------------------------------------

def set_desired(symbols: Any, *, source: str = "") -> list[str]:
    """「登録したい」集合。ここでは confirmed を一切触らない。"""
    global _desired
    syms = normalize(symbols)
    with _LOCK:
        _desired = syms
    logger.debug("[PUSH REG STATE] desired set count=%d source=%s", len(syms), source or "-")
    return syms


def set_requested(symbols: Any, *, label: str = "") -> list[str]:
    """register API に投げる直前に記録する。まだ確定ではない。"""
    global _requested, _active_batch, _active_batch_label
    syms = normalize(symbols)
    with _LOCK:
        _requested = syms
        if label:
            _active_batch = syms
            _active_batch_label = str(label)
    return syms


def mark_registered_confirmed(symbols: Any, *, label: str = "", source: str = "") -> list[str]:
    """register API が成功したときだけ呼ぶ。ここだけが confirmed を更新する。"""
    global _confirmed, _last_good, _confirmed_at, _last_good_at
    syms = normalize(symbols)
    if not syms:
        logger.warning(
            "[PUSH REG STATE] mark_registered_confirmed ignored: empty symbols source=%s", source or "-"
        )
        return list(_confirmed)
    with _LOCK:
        _load_last_good_once()
        _confirmed = syms
        _confirmed_at = time.time()
        _last_good = syms
        _last_good_at = _confirmed_at
        _save_last_good()
    logger.warning(
        "[PUSH REG STATE] confirmed_registered count=%d label=%s source=%s head=%s",
        len(syms), label or "-", source or "-", syms[:10],
    )
    return syms


def note_register_failed(*, label: str = "", reason: str = "") -> None:
    """register に失敗したとき。confirmed / last_good は消さない。"""
    logger.error(
        "[PUSH REG STATE] register failed label=%s reason=%s "
        "confirmed=%d last_good=%d (confirmed/last_good は保持する)",
        label or "-", reason or "-", len(_confirmed), len(_last_good),
    )


def clear_confirmed(*, reason: str = "") -> None:
    """unregister/all が成功して本当に 0 件になったときだけ呼ぶ。"""
    global _confirmed
    with _LOCK:
        _confirmed = []
    logger.warning("[PUSH REG STATE] confirmed cleared reason=%s (last_good は保持)", reason or "-")


# ------------------------------------------------------------
# 参照 API
# ------------------------------------------------------------

def desired_symbols() -> list[str]:
    with _LOCK:
        return list(_desired)


def requested_symbols() -> list[str]:
    with _LOCK:
        return list(_requested)


def confirmed_registered_symbols() -> list[str]:
    with _LOCK:
        return list(_confirmed)


def registered_count() -> int:
    with _LOCK:
        return len(_confirmed)


def last_good_registered_symbols() -> list[str]:
    with _LOCK:
        _load_last_good_once()
        return list(_last_good)


def active_rotation_batch() -> tuple[str, list[str]]:
    with _LOCK:
        return _active_batch_label, list(_active_batch)


def snapshot() -> dict:
    with _LOCK:
        _load_last_good_once()
        return {
            "desired": len(_desired),
            "requested": len(_requested),
            "confirmed": len(_confirmed),
            "last_good": len(_last_good),
            "active_batch_label": _active_batch_label,
            "active_batch": len(_active_batch),
            "confirmed_age_sec": (time.time() - _confirmed_at) if _confirmed_at else None,
        }


def log_snapshot(context: str = "") -> dict:
    snap = snapshot()
    logger.warning(
        "[PUSH REG STATE] pid=%s context=%s desired=%s requested=%s confirmed=%s "
        "last_good=%s batch=%s/%s",
        os.getpid(), context or "-", snap["desired"], snap["requested"], snap["confirmed"],
        snap["last_good"], snap["active_batch_label"] or "-", snap["active_batch"],
    )
    return snap


def reset_for_test() -> None:
    global _desired, _requested, _confirmed, _last_good
    global _active_batch, _active_batch_label, _confirmed_at, _last_good_at, _last_good_loaded
    with _LOCK:
        _desired = []
        _requested = []
        _confirmed = []
        _last_good = []
        _active_batch = []
        _active_batch_label = ""
        _confirmed_at = 0.0
        _last_good_at = 0.0
        _last_good_loaded = True  # テストではディスクを読まない


__all__ = [
    "VERSION",
    "normalize",
    "state_path",
    "set_desired",
    "set_requested",
    "mark_registered_confirmed",
    "note_register_failed",
    "clear_confirmed",
    "desired_symbols",
    "requested_symbols",
    "confirmed_registered_symbols",
    "registered_count",
    "last_good_registered_symbols",
    "active_rotation_batch",
    "snapshot",
    "log_snapshot",
    "reset_for_test",
]
