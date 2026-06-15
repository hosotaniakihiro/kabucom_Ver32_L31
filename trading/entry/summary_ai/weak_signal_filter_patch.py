# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
_INSTALLED = False


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _summary_like(source: Any) -> bool:
    s = str(source or "").upper()
    return ("SUMMARY" in s or "PUSH" in s) and "RANKING" not in s


def _zero_confirm_reason(reason: str) -> bool:
    r = str(reason or "")
    return (
        "rankScore=0.000" in r
        and ("3m=0.00" in r or "3m=0.0" in r or "3m=0|" in r)
        and ("5m=0.00" in r or "5m=0.0" in r or "5m=0|" in r)
    )


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        from trading.entry.summary_ai import runner as runner_mod
    except Exception:
        logger.exception("[SUMMARY AI WEAK FILTER] import failed")
        return False

    original = getattr(runner_mod, "run_ai_gate_for_candidates", None)
    if not callable(original):
        return False

    def _wrapped(*args: Any, **kwargs: Any):
        results = original(*args, **kwargs)
        if not isinstance(results, list):
            return results
        if not _env_bool("SUMMARY_AI_REJECT_ZERO_3M5M_CONFIRM", True):
            return results
        source = kwargs.get("source", "")
        if not _summary_like(source):
            return results
        out = []
        changed = []
        for row in results:
            if isinstance(row, dict) and bool(row.get("allow")) and _zero_confirm_reason(str(row.get("reason") or "")):
                new_row = dict(row)
                new_row["allow"] = False
                new_row["reason"] = f"{new_row.get('reason', '')}|zero_3m5m_confirm_ng"
                changed.append(str(new_row.get("symbol") or ""))
                out.append(new_row)
            else:
                out.append(row)
        if changed:
            logger.warning("[SUMMARY AI WEAK FILTER] zero 3m/5m confirmation ng source=%s symbols=%s", source, changed[:20])
        return out

    runner_mod.run_ai_gate_for_candidates = _wrapped
    _INSTALLED = True
    logger.warning("[SUMMARY AI WEAK FILTER] installed")
    return True


__all__ = ["install"]
