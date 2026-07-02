# -*- coding: utf-8 -*-
# ============================================================
# File   : core/startup/summary_ai_low_move_softpass_patch.py
# Version: V3.1-STRICT-DISABLED-BLOWOFF-PREFILTER-CHAIN
# ------------------------------------------------------------
# Purpose:
#   SUMMARY_AI の低ATR/低レンジ soft-pass を既定で無効化する。
#
# Important:
#   - 前回までの V2 は atr_1m_filter / range_5m_filter を watcher で再ラップし、
#     SUMMARY_AI 候補だけ低変動ガードを通す救済を行っていた。
#   - 低出来高・低変動銘柄を緩和せず排除する運用では、この挙動は不要。
#   - 既定では低変動soft-passは何も patch しない。明示的に
#       SUMMARY_AI_LOW_MOVE_SOFTPASS=1
#     を設定した場合でも、このstrict buildでは実装を入れない。
#
# V3.1:
#   - このpatchは sitecustomize から既に自動installされるため、
#     blowoff guardを緩和せずに「blowoff銘柄をtop3選定前に除外する」
#     summary_ai_blowoff_prefilter_patch を連鎖installする。
# ============================================================
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

VERSION = "V3.1-STRICT-DISABLED-BLOWOFF-PREFILTER-CHAIN"
_INSTALLED = False

_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in _TRUE
    except Exception:
        return bool(default)


def _install_blowoff_prefilter() -> bool:
    try:
        from core.startup.summary_ai_blowoff_prefilter_patch import install as _install
        ok = bool(_install())
        logger.warning("[LOW MOVE GUARD] chained SUMMARY_AI blowoff prefilter installed=%s version=%s", ok, VERSION)
        return ok
    except Exception:
        logger.exception("[LOW MOVE GUARD] chained SUMMARY_AI blowoff prefilter install failed")
        return False


def install() -> bool:
    """
    Strict mode:
      - デフォルトでは SUMMARY_AI 低変動 soft-pass を一切入れない。
      - watcher も起動しない。
      - entry_controller.atr_1m_filter / range_5m_filter を再ラップしない。

    これにより、ATR/range ガードで NG になった SUMMARY_AI 候補は、
    他ソースと同じく NG のまま維持される。

    ただし blowoff prefilter は低変動緩和ではなく、既存blowoff除外を
    executor選定前へ前倒しするだけなので、ここから連鎖installする。
    """
    global _INSTALLED

    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS", "0")
    os.environ.setdefault("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCHER", "0")
    os.environ.setdefault("SUMMARY_AI_BLOWOFF_PREFILTER_ENABLED", "1")

    blowoff_ok = _install_blowoff_prefilter()

    if not _env_bool("SUMMARY_AI_LOW_MOVE_SOFTPASS", False):
        _INSTALLED = bool(blowoff_ok)
        logger.warning(
            "[LOW MOVE GUARD] SUMMARY_AI low move softpass disabled strict mode version=%s "
            "SUMMARY_AI_LOW_MOVE_SOFTPASS=%s watcher=%s blowoff_prefilter=%s",
            VERSION,
            os.getenv("SUMMARY_AI_LOW_MOVE_SOFTPASS"),
            os.getenv("SUMMARY_AI_LOW_MOVE_SOFTPASS_WATCHER"),
            blowoff_ok,
        )
        return bool(blowoff_ok)

    # Safety: this file intentionally no longer installs the soft-pass implementation.
    # If a future experiment needs it, implement it in a separate opt-in patch so that
    # strict production behavior cannot be silently relaxed by import side effects.
    _INSTALLED = bool(blowoff_ok)
    logger.warning(
        "[LOW MOVE GUARD] SUMMARY_AI low move softpass requested but implementation is disabled in strict build version=%s blowoff_prefilter=%s",
        VERSION,
        blowoff_ok,
    )
    return bool(blowoff_ok)


try:
    install()
except Exception:
    logger.exception("[LOW MOVE GUARD] SUMMARY_AI low move softpass strict stub auto install failed")


__all__ = ["VERSION", "install"]
