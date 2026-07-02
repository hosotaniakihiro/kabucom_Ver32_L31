# ============================================================
# File   : core/startup/summary_ai_volatility_rescue_patch.py
# Version: V5-STRICT-DISABLED-BY-DEFAULT
# ------------------------------------------------------------
# SUMMARY_AI volatility rescue patch strict stub.
#
# Important:
#   - 旧 V4 は atr_1m_filter / range_5m_filter を再ラップし、
#     強い SUMMARY_AI 候補を ATR/RANGE NG から rescue していた。
#   - さらに zero-score PUSH bootstrap で暫定 score を合成し、
#     起動直後の候補を AI へ流す rescue も行っていた。
#   - 低変動・低出来高・ゼロスコア候補を緩和しない運用では、
#     これらは誤発注候補を増やすため既定無効にする。
# ============================================================

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)
VERSION = "V5-STRICT-DISABLED-BY-DEFAULT"
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


def install() -> bool:
    """
    Strict mode:
      - SUMMARY_AI_VOL_RESCUE_ENABLED は既定 0。
      - SUMMARY_AI_ZERO_SCORE_PUSH_BOOTSTRAP_ENABLED は既定 0。
      - SUMMARY_AI_DB_FRESH_INPUT_ENABLED は既定 1 のまま維持。
        これは fresh DB を読むだけで、ATR/RANGE NG の rescue ではないため。
      - entry_controller.atr_1m_filter / range_5m_filter は再ラップしない。
      - watcher は起動しない。
    """
    global _INSTALLED

    os.environ.setdefault("SUMMARY_AI_VOL_RESCUE_ENABLED", "0")
    os.environ.setdefault("SUMMARY_AI_ZERO_SCORE_PUSH_BOOTSTRAP_ENABLED", "0")
    os.environ.setdefault("SUMMARY_AI_DB_FRESH_INPUT_ENABLED", "1")
    os.environ.setdefault("SUMMARY_AI_VOL_RESCUE_WATCH_LOOPS", "0")

    if not _env_bool("SUMMARY_AI_VOL_RESCUE_ENABLED", False):
        _INSTALLED = False
        logger.warning(
            "[SUMMARY AI VOL RESCUE] disabled strict mode version=%s rescue=%s zero_score_bootstrap=%s db_fresh_input=%s",
            VERSION,
            os.getenv("SUMMARY_AI_VOL_RESCUE_ENABLED"),
            os.getenv("SUMMARY_AI_ZERO_SCORE_PUSH_BOOTSTRAP_ENABLED"),
            os.getenv("SUMMARY_AI_DB_FRESH_INPUT_ENABLED"),
        )
        return False

    # Safety: the rescue implementation is intentionally disabled in this strict build.
    # If it is needed again, add it as a separate explicitly opt-in patch rather than
    # silently wrapping entry filters during import.
    _INSTALLED = False
    logger.warning(
        "[SUMMARY AI VOL RESCUE] requested but implementation is disabled in strict build version=%s",
        VERSION,
    )
    return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY AI VOL RESCUE] strict stub auto install failed")


__all__ = ["install"]
