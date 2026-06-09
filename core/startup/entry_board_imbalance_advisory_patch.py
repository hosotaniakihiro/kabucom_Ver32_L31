# ============================================================
# File   : core/startup/entry_board_imbalance_advisory_patch.py
# Version: V2-DIRECT-FSG-BOARD-WALL-GUARD
# ------------------------------------------------------------
# 目的:
#   final_entry_safety_guard は ALL_OK まで通っているのに、後段の
#   board_wall_stall_exit_patch / board_signal が
#   ENTRY_BOARD_SELL_STRONG_BID 等で order build 前に False を返し、
#   RANKING / TONOSAMA の実注文が止まるケースを救済する。
#
# V2:
#   - board_wall_stall_exit_patch は final_entry_safety_guard._board_guard を
#     _patched_board_guard に直接差し替えるため、関数名探索では刺さらなかった。
#   - board_wall_stall_exit_patch._patched_board_guard 自体を 4引数互換で差し替え、
#     RANKING/TONOSAMA の board imbalance NG を advisory 化する。
#   - fsg._board_guard も最後段で再差し替えする。
# ============================================================
from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_BW_BOARD_GUARD = None
_ORIG_FSG_BOARD_GUARD = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _get(row: Any, name: str, default=None):
    try:
        if isinstance(row, dict):
            return row.get(name, default)
        return getattr(row, name, default)
    except Exception:
        return default


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip().upper()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        return s
    except Exception:
        return ""


def _norm_side(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s in {"1", "BUY", "LONG", "B"}:
        return "BUY"
    if s in {"2", "SELL", "SHORT", "S"}:
        return "SELL"
    return s


def _source_text(row: Any = None, *args: Any, **kwargs: Any) -> str:
    vals = []
    vals.extend([kwargs.get("source"), kwargs.get("pipeline_source"), kwargs.get("entry_type")])
    for a in (row,) + args:
        if isinstance(a, dict):
            vals.extend([a.get("source"), a.get("pipeline_source"), a.get("entry_type"), a.get("reason"), a.get("pipeline")])
        else:
            vals.extend([
                getattr(a, "source", None),
                getattr(a, "pipeline_source", None),
                getattr(a, "entry_type", None),
            ])
    return "|".join(str(v or "").upper() for v in vals)


def _is_rank_or_tono(row: Any = None, *args: Any, **kwargs: Any) -> bool:
    s = _source_text(row, *args, **kwargs)
    return "RANKING" in s or "TONOSAMA" in s


def _resolve_call(row: Any, item: Any = None, symbol: Any = None, side: Any = None):
    # 互換:
    #   _board_guard(row, symbol, side)
    #   _board_guard(row, item, symbol, side)
    if side is None and symbol is not None:
        side = symbol
        symbol = item
        item = None
    sym = _norm_symbol(symbol or _get(row, "symbol") or _get(row, "Symbol") or _get(row, "code"))
    sd = _norm_side(side or _get(row, "side") or _get(row, "entry_decision") or _get(row, "ai_side"))
    return row, item, sym, sd


def _call_orig(orig, row: Any, item: Any, symbol: str, side: str):
    try:
        return orig(row, item, symbol, side)
    except TypeError:
        return orig(row, symbol, side)


def _analyze_board_imbalance(symbol: str, side: str):
    try:
        from trading.board.board_signal import analyze_entry_board_imbalance
        try:
            return analyze_entry_board_imbalance(symbol, side=side, exchange=int(float(os.getenv("ENTRY_BOARD_EXCHANGE", os.getenv("EXIT_BOARD_WALL_EXCHANGE", "1")))))
        except TypeError:
            return analyze_entry_board_imbalance(symbol, side=side)
    except Exception:
        logger.debug("[BOARD IMBALANCE ADVISORY] analyze failed symbol=%s side=%s", symbol, side, exc_info=True)
        return None


def _should_advisory_allow(row: Any, symbol: str, side: str, ret: Any = None) -> bool:
    if not _env_bool("ENTRY_BOARD_IMBALANCE_ADVISORY_ENABLED", True):
        return False
    if not _is_rank_or_tono(row):
        return False
    if ret is True:
        return False
    if ret is False:
        return True
    text = str(ret)
    return (
        "ENTRY_BOARD_SELL_STRONG_BID" in text
        or "ENTRY_BOARD_BUY_STRONG_ASK" in text
        or "BOARD_ENTRY_IMBALANCE" in text
        or "board_imbalance" in text.lower()
    )


def _advisory_board_guard(row: Any, item: Any = None, symbol: Any = None, side: Any = None, *args: Any, **kwargs: Any) -> bool:
    global _ORIG_BW_BOARD_GUARD, _ORIG_FSG_BOARD_GUARD
    row, item, sym, sd = _resolve_call(row, item, symbol, side)
    source = _source_text(row)

    # RANKING/TONOSAMA は board imbalance を警告扱いにする。
    if _is_rank_or_tono(row):
        # 元の最良気配/スプレッド/薄板 guard は通す。board_wall の imbalance だけ bypass。
        try:
            import core.startup.board_wall_stall_exit_patch as bw
            orig_base = getattr(bw, "_ORIG_BOARD_GUARD", None)
            if callable(orig_base):
                ok_base = _call_orig(orig_base, row, item, sym, sd)
                if not ok_base:
                    logger.warning("[BOARD IMBALANCE ADVISORY] base board guard NG keep block symbol=%s side=%s source=%s", sym, sd, source)
                    return False
        except Exception:
            logger.debug("[BOARD IMBALANCE ADVISORY] base guard check skipped symbol=%s side=%s", sym, sd, exc_info=True)

        detail = _analyze_board_imbalance(sym, sd)
        if detail:
            logger.warning("[BOARD IMBALANCE ADVISORY] allow source=RANKING_OR_TONOSAMA symbol=%s side=%s detail=%s", sym, sd, detail)
            return True
        logger.debug("[BOARD IMBALANCE ADVISORY] no imbalance detail symbol=%s side=%s source=%s", sym, sd, source)
        return True

    # その他 source は既存挙動。
    orig = _ORIG_BW_BOARD_GUARD or _ORIG_FSG_BOARD_GUARD
    if callable(orig):
        return bool(_call_orig(orig, row, item, sym, sd))
    return True


def _patch_once() -> bool:
    global _ORIG_BW_BOARD_GUARD, _ORIG_FSG_BOARD_GUARD
    ok = False
    try:
        import core.startup.board_wall_stall_exit_patch as bw
        cur = getattr(bw, "_patched_board_guard", None)
        if callable(cur) and not getattr(cur, "_board_imbalance_advisory_v2", False):
            _ORIG_BW_BOARD_GUARD = cur
            _advisory_board_guard._board_imbalance_advisory_v2 = True  # type: ignore[attr-defined]
            _advisory_board_guard._original = cur  # type: ignore[attr-defined]
            bw._patched_board_guard = _advisory_board_guard
            logger.warning("[BOARD IMBALANCE ADVISORY] patched board_wall_stall_exit_patch._patched_board_guard")
            ok = True
    except Exception:
        logger.debug("[BOARD IMBALANCE ADVISORY] board_wall patch skipped", exc_info=True)

    try:
        import core.startup.final_entry_safety_guard_patch as fsg
        cur = getattr(fsg, "_board_guard", None)
        # board_wall が fsg._board_guard を持っている場合、直接 advisory に差し替える。
        if callable(cur) and not getattr(cur, "_board_imbalance_advisory_v2", False):
            _ORIG_FSG_BOARD_GUARD = cur
            _advisory_board_guard._board_imbalance_advisory_v2 = True  # type: ignore[attr-defined]
            _advisory_board_guard._original_fsg = cur  # type: ignore[attr-defined]
            fsg._board_guard = _advisory_board_guard
            if hasattr(fsg, "_patched_board_guard"):
                fsg._patched_board_guard = _advisory_board_guard
            logger.warning("[BOARD IMBALANCE ADVISORY] patched final_entry_safety_guard._board_guard direct")
            ok = True
    except Exception:
        logger.debug("[BOARD IMBALANCE ADVISORY] final guard patch skipped", exc_info=True)

    return ok


def _watch() -> None:
    loops = 120
    for i in range(loops):
        ok = _patch_once()
        if i in (0, 1, 5, 15, 30, 60, 119):
            logger.warning("[BOARD IMBALANCE ADVISORY] enforce v2 i=%s ok=%s", i, ok)
        time.sleep(0.5)


def install() -> bool:
    global _INSTALLED
    os.environ["ENTRY_BOARD_IMBALANCE_ADVISORY_ENABLED"] = "1"
    ok = _patch_once()
    if not _INSTALLED:
        threading.Thread(target=_watch, name="board-imbalance-advisory-enforcer", daemon=True).start()
        _INSTALLED = True
    logger.warning("[BOARD IMBALANCE ADVISORY] installed v2 ok=%s watcher=True", ok)
    return True


try:
    install()
except Exception:
    logger.exception("[BOARD IMBALANCE ADVISORY] auto install failed")

__all__ = ["install"]
