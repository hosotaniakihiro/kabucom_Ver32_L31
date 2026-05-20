# ============================================================
# File   : core/startup/entry_direction_failopen_runtime_patch.py
# Version: Ver01-SUMMARY-AI-DIRECTION-FAILOPEN
# ------------------------------------------------------------
# SUMMARY_AI の方向確認ガードで RecursionError / pure guard False が出た場合、
# 発注直前候補を ATR_1M_FILTER_NG で全落ちさせないための runtime patch。
#
# 背景:
#   entry_direction_confirm_guard_patch は純粋判定化済みだが、
#   ログ上 [ENTRY DIRECTION CONFIRM] recursion detected in pure guard が出て、
#   low_movement 側で ATR_1M_FILTER_NG 扱いになっている。
#
# 方針:
#   - SUMMARY_AI に限り、方向確認ガードの例外/Falseを fail-open する
#   - 低変動ガード、流動性、信用売り可否、数量、注文APIガードは維持
#   - envで無効化可能
#
# ENV:
#   ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI=1  # default ON
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_CHECK = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _row_to_dict(row: Any) -> dict:
    try:
        if row is None:
            return {}
        if isinstance(row, dict):
            return dict(row)
        if hasattr(row, "to_dict"):
            d = row.to_dict()
            if isinstance(d, dict):
                return dict(d)
        return {}
    except Exception:
        return {}


def _is_summary_ai(row: Any) -> bool:
    d = _row_to_dict(row)
    src = str(d.get("source") or "").upper()
    et = str(d.get("entry_type") or "").upper()
    return src == "SUMMARY" or et == "SUMMARY_AI"


def _symbol(row: Any) -> str:
    d = _row_to_dict(row)
    s = str(d.get("symbol") or d.get("code") or d.get("stock_code") or "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        return s[:-2]
    return s


def _side(row: Any) -> str:
    d = _row_to_dict(row)
    return str(d.get("side") or d.get("entry_decision") or d.get("ai_side") or "").upper()


def _patched_check_entry_direction_confirm(entry_row: Any = None, *args, **kwargs) -> bool:
    if not callable(_ORIG_CHECK):
        return True

    try:
        ok = bool(_ORIG_CHECK(entry_row, *args, **kwargs))
        if ok:
            return True

        if _env_bool("ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI", True) and _is_summary_ai(entry_row):
            logger.warning(
                "[ENTRY DIRECTION FAILOPEN] allow SUMMARY_AI despite direction guard NG symbol=%s side=%s",
                _symbol(entry_row),
                _side(entry_row),
            )
            return True

        return False

    except RecursionError:
        if _env_bool("ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI", True) and _is_summary_ai(entry_row):
            logger.warning(
                "[ENTRY DIRECTION FAILOPEN] allow SUMMARY_AI after RecursionError symbol=%s side=%s",
                _symbol(entry_row),
                _side(entry_row),
            )
            return True
        return False

    except Exception as e:
        if _env_bool("ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI", True) and _is_summary_ai(entry_row):
            logger.warning(
                "[ENTRY DIRECTION FAILOPEN] allow SUMMARY_AI after guard error symbol=%s side=%s err=%s",
                _symbol(entry_row),
                _side(entry_row),
                e,
                exc_info=False,
            )
            return True
        logger.warning("[ENTRY DIRECTION FAILOPEN] direction guard error: %s", e, exc_info=False)
        return False


def install() -> bool:
    global _INSTALLED, _ORIG_CHECK
    if _INSTALLED:
        return True

    try:
        from core.startup import entry_direction_confirm_guard_patch as ed

        cur = getattr(ed, "check_entry_direction_confirm", None)
        if getattr(cur, "_entry_direction_failopen_patch", False):
            _INSTALLED = True
            return True

        _ORIG_CHECK = cur
        _patched_check_entry_direction_confirm._entry_direction_failopen_patch = True  # type: ignore[attr-defined]
        ed.check_entry_direction_confirm = _patched_check_entry_direction_confirm

        _INSTALLED = True
        logger.warning(
            "[ENTRY DIRECTION FAILOPEN] installed summary_ai_failopen=%s",
            _env_bool("ENTRY_DIRECTION_FAILOPEN_FOR_SUMMARY_AI", True),
        )
        return True
    except Exception as e:
        logger.warning("[ENTRY DIRECTION FAILOPEN] install failed: %s", e, exc_info=False)
        return False


try:
    install()
except Exception as e:
    logger.warning("[ENTRY DIRECTION FAILOPEN] auto install failed: %s", e, exc_info=False)

__all__ = ["install"]
