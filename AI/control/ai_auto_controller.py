# ============================================================
# AI/control/ai_auto_controller.py
# ------------------------------------------------------------
# STEP3-② AI 自動調整コントローラ
#
# ・AI Health State を参照
# ・threshold / lot / AI ON-OFF を自動制御
# ・人間判断なしで暴走を止める
# ============================================================

import json
import logging
from pathlib import Path
from datetime import datetime

logger = logging.getLogger(__name__)

# ============================================================
# PATH
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[2]

STATE_FILE = PROJECT_ROOT / "AI" / "monitoring" / "ai_health_state.json"
CONTROL_FILE = PROJECT_ROOT / "AI" / "control" / "ai_control_state.json"

CONTROL_FILE.parent.mkdir(parents=True, exist_ok=True)

# ============================================================
# PARAMS（調整容易）
# ============================================================

THRESHOLD_UP_WARNING = 1.10   # +10%
THRESHOLD_UP_CRITICAL = 1.30  # +30%

LOT_DOWN_WARNING = 0.7
LOT_DOWN_CRITICAL = 0.3

# ============================================================
# MAIN
# ============================================================

def run_ai_auto_control():

    if not STATE_FILE.exists():
        logger.warning("AI health state not found")
        return None

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    health = state.get("health", "NORMAL")

    control = {
        "timestamp": datetime.now().isoformat(),
        "health": health,

        # default
        "ai_enabled": True,
        "threshold_multiplier": 1.0,
        "lot_multiplier": 1.0,
    }

    # --------------------------------------------------------
    # 判定
    # --------------------------------------------------------
    if health == "WARNING":
        control.update(
            ai_enabled=True,
            threshold_multiplier=THRESHOLD_UP_WARNING,
            lot_multiplier=LOT_DOWN_WARNING,
        )

    elif health == "CRITICAL":
        control.update(
            ai_enabled=False,              # ← ENTRY 停止
            threshold_multiplier=THRESHOLD_UP_CRITICAL,
            lot_multiplier=LOT_DOWN_CRITICAL,
        )

    CONTROL_FILE.write_text(
        json.dumps(control, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    logger.info(f"🧠 AI AUTO CONTROL = {control}")

    return control


# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_ai_auto_control()
