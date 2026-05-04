# ============================================================
# scoring/config/ini_consistency_checker.py
# ------------------------------------------------------------
# ・ini に定義された全スコアキーを取得
# ・DataFrame の columns と突合
# ・未定義フラグを WARN / ERROR
# ============================================================

import logging
from typing import Set
from scoring.config.score_table import SCORE_TABLE

logger = logging.getLogger(__name__)


def get_ini_score_keys() -> Set[str]:
    """
    ini（score_config.ini）から読み込まれた
    全スコアキーを返す
    """
    return set(SCORE_TABLE.keys())


def check_df_columns_against_ini(df, *, raise_error: bool = False):
    """
    df.columns と ini のキーを突合し、
    足りないフラグを検出する
    """
    ini_keys = get_ini_score_keys()
    df_cols = set(df.columns)

    missing = sorted(k for k in ini_keys if k not in df_cols)

    if not missing:
        logger.info("✅ ini / DataFrame consistency OK")
        return True

    msg = (
        "❌ ini に定義されているが DataFrame に存在しない列:\n"
        + "\n".join(f"  - {k}" for k in missing)
    )

    if raise_error:
        raise RuntimeError(msg)

    logger.warning(msg)
    return False
