# ============================================================
# trading/scoring/tools/verify_conditions_vs_ini.py
# ------------------------------------------------------------
# ✔ conditions が返す flag と score_config.ini の完全一致検証
# ✔ 起動時 / デバッグ時に1回だけ実行
# ============================================================

import inspect
import logging
from trading.signals import conditions_buy
from trading.scoring.config.score_table import SCORE_TABLE

logger = logging.getLogger(__name__)


def _collect_condition_flags():
    """
    conditions_buy 内の関数が返す reason（flag）名を静的解析で収集
    """
    flags = set()

    for fn in conditions_buy.conditions_buy:
        try:
            src = inspect.getsource(fn)
            for line in src.splitlines():
                line = line.strip()
                if "return True" in line and "," in line:
                    # return True, "xxx"
                    flag = line.split(",")[-1].strip().strip('"').strip("'")
                    flags.add(flag)
        except Exception:
            continue

    return flags


def verify_conditions_vs_ini():
    cond_flags = _collect_condition_flags()
    ini_flags = set(SCORE_TABLE.keys())

    missing_in_ini = cond_flags - ini_flags
    missing_in_conditions = ini_flags - cond_flags

    if missing_in_ini:
        logger.error("❌ conditions にあるが ini に無い flag:")
        for k in sorted(missing_in_ini):
            logger.error(f"   - {k}")

    if missing_in_conditions:
        logger.warning("⚠ ini にあるが conditions に無い flag:")
        for k in sorted(missing_in_conditions):
            logger.warning(f"   - {k}")

    if not missing_in_ini and not missing_in_conditions:
        logger.info("✅ conditions ↔ score_config.ini 完全一致")

    return {
        "missing_in_ini": missing_in_ini,
        "missing_in_conditions": missing_in_conditions,
    }
