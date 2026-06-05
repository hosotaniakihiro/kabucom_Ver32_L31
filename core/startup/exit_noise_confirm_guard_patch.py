# ============================================================
# File   : core/startup/exit_noise_confirm_guard_patch.py
# Version: V1-NOISE-EXIT-CONFIRM
# ------------------------------------------------------------
# 目的:
#   エントリー後のノイズで即EXITしないようにする。
#   特に EARLY_STAGNATION / EARLY_NO_PROGRESS / BREAKEVEN / PROFIT_LOCK など、
#   1回の判定で売ると早すぎる理由は、連続確認してからEXITする。
#
# 方針:
#   - 損切り系/急落系は止めない。
#   - 利益停滞・建値保護・利益ロックなどのノイズ系だけ確認回数を要求。
#   - デフォルト: 同じ symbol + reason が2回連続、かつ初回検出から10秒以上。
# ============================================================
from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_FINALIZE = None
_STATE: dict[tuple[str, str], dict[str, Any]] = {}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _reason_is_noise(reason: str) -> bool:
    r = str(reason or "").upper()
    noise_keys = tuple(str(os.getenv("EXIT_NOISE_CONFIRM_REASONS", "EARLY_STAGNATION,EARLY_NO_PROGRESS,BREAKEVEN,PROFIT_LOCK,PROFIT_TO_LOSS")).upper().replace(";", ",").split(","))
    hard_keys = ("STOP", "LOSS_LIMIT", "HARD", "急落", "PANIC", "CRASH")
    if any(k and k in r for k in hard_keys):
        return False
    return any(k and k in r for k in noise_keys)


def _extract_symbol(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    try:
        if kwargs.get("symbol"):
            return str(kwargs.get("symbol")).strip()
        if args:
            first = args[0]
            if isinstance(first, dict):
                return str(first.get("symbol") or first.get("Symbol") or "").strip()
            return str(first or "").strip()
    except Exception:
        pass
    return ""


def _extract_reason(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
    try:
        if kwargs.get("reason"):
            return str(kwargs.get("reason") or "")
        for x in args:
            if isinstance(x, str) and ("EXIT" in x.upper() or "EARLY" in x.upper() or "STOP" in x.upper() or "PROFIT" in x.upper() or "BREAKEVEN" in x.upper()):
                return x
    except Exception:
        pass
    return str(kwargs.get("exit_reason") or "")


def _allow_exit(symbol: str, reason: str) -> tuple[bool, dict[str, Any]]:
    now = dt.datetime.now()
    need_count = max(1, _env_int("EXIT_NOISE_CONFIRM_COUNT", 2))
    need_sec = max(0.0, _env_float("EXIT_NOISE_CONFIRM_MIN_SEC", 10.0))
    key = (symbol, reason)
    st = _STATE.get(key)
    if not st:
        _STATE[key] = {"first": now, "last": now, "count": 1}
        return False, {"count": 1, "need_count": need_count, "elapsed": 0.0, "need_sec": need_sec}
    st["count"] = int(st.get("count") or 0) + 1
    st["last"] = now
    elapsed = (now - st.get("first", now)).total_seconds()
    ok = st["count"] >= need_count and elapsed >= need_sec
    return ok, {"count": st["count"], "need_count": need_count, "elapsed": round(elapsed, 3), "need_sec": need_sec}


def _patched_finalize(*args, **kwargs):
    if not _env_bool("EXIT_NOISE_CONFIRM_GUARD_ENABLED", True):
        return _ORIG_FINALIZE(*args, **kwargs)  # type: ignore[misc]
    symbol = _extract_symbol(args, kwargs)
    reason = _extract_reason(args, kwargs)
    if symbol and reason and _reason_is_noise(reason):
        ok, diag = _allow_exit(symbol, reason)
        if not ok:
            logger.warning("[EXIT NOISE CONFIRM GUARD] hold exit symbol=%s reason=%s diag=%s", symbol, reason, diag)
            return {"ok": False, "executed": False, "skipped": True, "reason": "noise_exit_wait_confirm", "symbol": symbol, "exit_reason": reason, "diag": diag}
        logger.warning("[EXIT NOISE CONFIRM GUARD] confirmed exit symbol=%s reason=%s diag=%s", symbol, reason, diag)
    return _ORIG_FINALIZE(*args, **kwargs)  # type: ignore[misc]


def install() -> bool:
    global _INSTALLED, _ORIG_FINALIZE
    try:
        import trading.exit.exit_finalize as ef
        target_name = None
        for name in ("execute_exit", "finalize_exit", "run_exit", "exit_execute"):
            if callable(getattr(ef, name, None)):
                target_name = name
                break
        if target_name is None:
            logger.warning("[EXIT NOISE CONFIRM GUARD] target not found in trading.exit.exit_finalize")
            return False
        cur = getattr(ef, target_name)
        if getattr(cur, "_exit_noise_confirm_guard_v1", False):
            _INSTALLED = True
            return True
        original = getattr(cur, "_original", None) if callable(cur) else None
        _ORIG_FINALIZE = original if callable(original) else cur
        _patched_finalize._exit_noise_confirm_guard_v1 = True  # type: ignore[attr-defined]
        _patched_finalize._original = _ORIG_FINALIZE  # type: ignore[attr-defined]
        setattr(ef, target_name, _patched_finalize)
        _INSTALLED = True
        logger.warning("[EXIT NOISE CONFIRM GUARD] installed v1 target=%s enabled=%s count=%s min_sec=%s", target_name, _env_bool("EXIT_NOISE_CONFIRM_GUARD_ENABLED", True), _env_int("EXIT_NOISE_CONFIRM_COUNT", 2), _env_float("EXIT_NOISE_CONFIRM_MIN_SEC", 10.0))
        return True
    except Exception:
        logger.exception("[EXIT NOISE CONFIRM GUARD] install failed")
        return False

try:
    install()
except Exception:
    logger.exception("[EXIT NOISE CONFIRM GUARD] auto install failed")

__all__ = ["install"]
