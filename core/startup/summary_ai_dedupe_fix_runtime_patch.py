# ============================================================
# File   : core/startup/summary_ai_dedupe_fix_runtime_patch.py
# Version: Ver01-NO-PREORDER-COOLDOWN
# ------------------------------------------------------------
# SUMMARY_AI が AI_OK の時点で dedupe/cooldown を付けてしまい、
# 実発注が失敗しても次回候補が dedupe_skip で消える問題を止める。
#
# 問題ログ例:
#   executor returned executed=False approved=0 skip=no_ai_ok
#   ng_reason_counts={'...|dedupe_skip:cooldown active elapsed=9.9s < 30s': 9}
#
# 方針:
#   - trading.entry.summary_ai.runner._apply_optional_entry_dedupe_guard を差し替え
#   - デフォルトでは pre-order dedupe を完全スキップ
#   - 実注文前に cooldown を付けない
#
# ENV:
#   SUMMARY_AI_PREORDER_DEDUPE_ENABLED=0  # default OFF
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False
_ORIG_DEDUPE = None


def _env_bool(name: str, default: bool = False) -> bool:
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


def _patched_apply_optional_entry_dedupe_guard(ai_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not ai_results:
        return ai_results

    if not _env_bool("SUMMARY_AI_PREORDER_DEDUPE_ENABLED", False):
        try:
            ok_symbols = [str(x.get("symbol")) for x in ai_results if isinstance(x, dict) and bool(x.get("allow"))]
        except Exception:
            ok_symbols = []
        logger.info(
            "[SUMMARY AI DEDUPE FIX] pre-order dedupe skipped ai_results=%s ai_ok_symbols=%s",
            len(ai_results),
            ok_symbols[:30],
        )
        return ai_results

    if callable(_ORIG_DEDUPE):
        return _ORIG_DEDUPE(ai_results)
    return ai_results


def install() -> bool:
    global _INSTALLED, _ORIG_DEDUPE
    if _INSTALLED:
        return True

    try:
        os.environ.setdefault("SUMMARY_AI_PREORDER_DEDUPE_ENABLED", "0")
        import trading.entry.summary_ai.runner as runner

        cur = getattr(runner, "_apply_optional_entry_dedupe_guard", None)
        if getattr(cur, "_summary_ai_dedupe_fix_v1", False):
            _INSTALLED = True
            return True

        _ORIG_DEDUPE = cur
        _patched_apply_optional_entry_dedupe_guard._summary_ai_dedupe_fix_v1 = True  # type: ignore[attr-defined]
        runner._apply_optional_entry_dedupe_guard = _patched_apply_optional_entry_dedupe_guard

        _INSTALLED = True
        logger.warning(
            "[SUMMARY AI DEDUPE FIX] installed preorder_dedupe_enabled=%s",
            _env_bool("SUMMARY_AI_PREORDER_DEDUPE_ENABLED", False),
        )
        return True
    except Exception as e:
        logger.exception("[SUMMARY AI DEDUPE FIX] install failed err=%s", e)
        return False


try:
    install()
except Exception as e:
    logger.exception("[SUMMARY AI DEDUPE FIX] auto install failed err=%s", e)

__all__ = ["install"]
