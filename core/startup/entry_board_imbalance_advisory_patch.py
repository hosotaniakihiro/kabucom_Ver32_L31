# ============================================================
# File   : core/startup/entry_board_imbalance_advisory_patch.py
# Version: V1-RANKING-TONOSAMA-ADVISORY
# ------------------------------------------------------------
# 目的:
#   final_entry_safety_guard は ALL_OK まで通っているのに、後段の
#   board_wall_stall_exit_patch / board_signal が
#   ENTRY_BOARD_SELL_STRONG_BID 等で order build 前に False を返し、
#   RANKING / TONOSAMA の実注文が止まるケースを救済する。
#
# 方針:
#   - RANKING / TONOSAMA だけ board imbalance を advisory 扱いにする。
#   - Summary / その他 source は既存挙動を維持。
#   - 方向逆の強い板はログに残すが、最終の発注可否は価格改善/指値側に任せる。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _source_text(*args: Any, **kwargs: Any) -> str:
    vals = []
    vals.extend([kwargs.get("source"), kwargs.get("pipeline_source"), kwargs.get("entry_type")])
    for a in args:
        if isinstance(a, dict):
            vals.extend([a.get("source"), a.get("pipeline_source"), a.get("entry_type"), a.get("reason")])
    return "|".join(str(v or "").upper() for v in vals)


def _is_rank_or_tono(*args: Any, **kwargs: Any) -> bool:
    s = _source_text(*args, **kwargs)
    return "RANKING" in s or "TONOSAMA" in s


def _looks_like_imbalance_ng(result: Any) -> bool:
    try:
        if result is True:
            return False
        text = str(result)
        return (
            "ENTRY_BOARD_SELL_STRONG_BID" in text
            or "ENTRY_BOARD_BUY_STRONG_ASK" in text
            or "BOARD_ENTRY_IMBALANCE" in text
            or "board_imbalance" in text.lower()
        )
    except Exception:
        return False


def _wrap_func(fn, label: str):
    if not callable(fn) or getattr(fn, "_board_imbalance_advisory_v1", False):
        return fn

    def _wrapped(*args: Any, **kwargs: Any):
        ret = fn(*args, **kwargs)
        if _env_bool("ENTRY_BOARD_IMBALANCE_ADVISORY_ENABLED", True) and _is_rank_or_tono(*args, **kwargs) and _looks_like_imbalance_ng(ret):
            logger.warning("[BOARD IMBALANCE ADVISORY] allow source=RANKING_OR_TONOSAMA label=%s ret=%s", label, ret)
            return True
        return ret

    _wrapped._board_imbalance_advisory_v1 = True  # type: ignore[attr-defined]
    _wrapped._original = fn  # type: ignore[attr-defined]
    return _wrapped


def _patch_once() -> bool:
    ok = False
    # 既知/候補モジュールを広めに探索する。存在しないものは無視。
    targets = [
        ("trading.board.board_signal", ["check_entry_board_imbalance", "entry_board_imbalance_check", "check_board_imbalance", "is_entry_board_ok"]),
        ("core.startup.board_wall_stall_exit_patch", ["check_entry_board_imbalance", "entry_board_imbalance_check", "_check_entry_board_imbalance", "_board_entry_imbalance_guard"]),
        ("trading.handlers.entry_order_builder", ["check_entry_board_imbalance", "entry_board_imbalance_check", "_board_imbalance_guard"]),
        ("trading.handlers.entry_controller", ["check_entry_board_imbalance", "entry_board_imbalance_check", "_board_imbalance_guard"]),
    ]
    for mod_name, names in targets:
        try:
            mod = __import__(mod_name, fromlist=["*"])
        except Exception:
            continue
        for name in names:
            try:
                cur = getattr(mod, name, None)
                if callable(cur) and not getattr(cur, "_board_imbalance_advisory_v1", False):
                    setattr(mod, name, _wrap_func(cur, f"{mod_name}.{name}"))
                    logger.warning("[BOARD IMBALANCE ADVISORY] patched %s.%s", mod_name, name)
                    ok = True
            except Exception:
                logger.debug("[BOARD IMBALANCE ADVISORY] patch failed %s.%s", mod_name, name, exc_info=True)
    return ok


def _watch() -> None:
    loops = 60
    for i in range(loops):
        ok = _patch_once()
        if i in (0, 1, 5, 15, 30, 59):
            logger.warning("[BOARD IMBALANCE ADVISORY] enforce i=%s ok=%s", i, ok)
        time.sleep(1.0)


def install() -> bool:
    global _INSTALLED
    os.environ.setdefault("ENTRY_BOARD_IMBALANCE_ADVISORY_ENABLED", "1")
    ok = _patch_once()
    if not _INSTALLED:
        threading.Thread(target=_watch, name="board-imbalance-advisory-enforcer", daemon=True).start()
        _INSTALLED = True
    logger.warning("[BOARD IMBALANCE ADVISORY] installed ok=%s watcher=True", ok)
    return True


try:
    install()
except Exception:
    logger.exception("[BOARD IMBALANCE ADVISORY] auto install failed")

__all__ = ["install"]
