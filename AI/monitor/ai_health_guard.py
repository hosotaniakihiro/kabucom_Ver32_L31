# ============================================================
# AI/monitor/ai_health_guard.py
# ------------------------------------------------------------
# STEP3-② AI 劣化時 ENTRY ブレーキ
#
# ・ai_health_state.json を参照
# ・WARNING / CRITICAL で ENTRY を制限
# ・Runtime 安全（失敗時は通す）
# ============================================================

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = PROJECT_ROOT / "AI" / "monitor" / "ai_health_state.json"


def ai_health_ok() -> bool:
    """
    Returns
    -------
    bool
        True  : ENTRY 許可
        False : ENTRY ブロック
    """

    if not STATE_FILE.exists():
        # 初期状態は通す
        return True

    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        health = state.get("health", "NORMAL")

        if health == "NORMAL":
            return True

        if health == "WARNING":
            logger.warning("🟡 AI HEALTH WARNING → ENTRY BLOCKED")
            return False

        if health == "CRITICAL":
            logger.error("🔴 AI HEALTH CRITICAL → ENTRY BLOCKED")
            return False

        return True

    except Exception:
        logger.exception("[AI_HEALTH_CHECK_ERROR]")
        return True  # fail-safe
