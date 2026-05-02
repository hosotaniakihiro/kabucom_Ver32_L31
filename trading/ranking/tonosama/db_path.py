# ============================================================
# File   : trading/ranking/tonosama/db_path.py
# Version: PRODUCTION-STABLE-REV1.0
# Purpose:
#   殿様イナゴ用 ranking DB path resolver
#
# Example:
#   \\192.168.0.22\AutoStockBuyAndSell
#     \raw_data\kabu_station\ranking\ranking20260426.db
# ============================================================

from __future__ import annotations

import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


DEFAULT_NAS_ROOT = r"\\192.168.0.22\AutoStockBuyAndSell"


def get_nas_root() -> str:
    return (
        os.getenv("NAS_ROOT")
        or os.getenv("KABU_NAS_ROOT")
        or DEFAULT_NAS_ROOT
    )


def get_ranking_db_dir(nas_root: Optional[str] = None) -> Path:
    root = nas_root or get_nas_root()
    return Path(root) / "raw_data" / "kabu_station" / "ranking"


def get_ranking_db_path(
    target_date: Optional[datetime] = None,
    *,
    nas_root: Optional[str] = None,
) -> Path:
    dt = target_date or datetime.now()
    ymd = dt.strftime("%Y%m%d")
    return get_ranking_db_dir(nas_root) / f"ranking{ymd}.db"


def resolve_existing_ranking_db_path(
    target_date: Optional[datetime] = None,
    *,
    nas_root: Optional[str] = None,
    fallback_latest: bool = True,
) -> Optional[Path]:
    path = get_ranking_db_path(target_date, nas_root=nas_root)

    if path.exists():
        return path

    logger.warning("[TONOSAMA DB PATH] today ranking db not found path=%s", path)

    if not fallback_latest:
        return None

    db_dir = get_ranking_db_dir(nas_root)

    try:
        files = sorted(
            db_dir.glob("ranking*.db"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except Exception:
        logger.exception("[TONOSAMA DB PATH] glob failed dir=%s", db_dir)
        return None

    if not files:
        logger.warning("[TONOSAMA DB PATH] no ranking db found dir=%s", db_dir)
        return None

    latest = files[0]
    logger.warning("[TONOSAMA DB PATH] fallback latest ranking db=%s", latest)
    return latest