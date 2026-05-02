# ============================================================
# File   : database/migrate/migrate_yahoo.py
# Version: Ver32-STRUCTURED-YAHOO-MIGRATION-TRACKING-STATE-REV2
# ------------------------------------------------------------
# ✔ Yahoo intraday 1min DB bootstrap
# ✔ yahoo_tracking_state bootstrap
# ✔ ADD ONLY 原則厳守
# ✔ PRIMARY KEY(symbol, datetime) 保証
# ✔ PRIMARY KEY(symbol, trade_date) 保証
# ✔ 日付ローテーション対応
# ✔ 既存データ破壊なし
# ✔ 将来列追加対応
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path
from typing import Any

from database.paths.yahoo_paths import get_yahoo_1min_db_path
from database.schema.yahoo_tracking_state_schema import ensure_yahoo_schema

logger = logging.getLogger(__name__)


# ============================================================
# MAIN ENTRY
# ============================================================

def migrate_yahoo(
    trade_date: Any = None,
    *,
    db_path: str | Path | None = None,
) -> str:
    """
    Yahoo intraday DB migration.

    Ensures:
      - yahoo_1min
      - yahoo_tracking_state

    Returns:
      Resolved DB path.
    """
    target_date = trade_date or dt.date.today()
    resolved = str(db_path or get_yahoo_1min_db_path(target_date))

    print("📈 Yahoo intraday migration start")
    logger.info("[MIGRATE] ensure yahoo intraday db: %s", resolved)

    ensure_yahoo_schema(
        resolved,
        trade_date=target_date,
        ensure_1min=True,
        ensure_tracking=True,
    )

    print("📈 Yahoo intraday migration complete")
    return resolved


# Backward-compatible alias candidates.
def run_migrate_yahoo(*args: Any, **kwargs: Any) -> str:
    return migrate_yahoo(*args, **kwargs)


def ensure_yahoo_migration(*args: Any, **kwargs: Any) -> str:
    return migrate_yahoo(*args, **kwargs)


if __name__ == "__main__":
    migrate_yahoo()


__all__ = [
    "migrate_yahoo",
    "run_migrate_yahoo",
    "ensure_yahoo_migration",
]
