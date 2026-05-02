# ============================================================
# File   : ats/ats_ranking/cache.py
# Version: Ver1.0-ATS-RANKING-CACHE
# ============================================================

from __future__ import annotations

from typing import Optional
import pandas as pd

_ATS_RANKING_CACHE_DF: Optional[pd.DataFrame] = None
_ATS_RANKING_CACHE_TS: float = 0.0
_ATS_RANKING_CACHE_SEC: float = 5.0

_ATS_RANKING_DB_PATH_CACHE: Optional[str] = None
_ATS_RANKING_DB_PATH_CACHE_TS: float = 0.0
_ATS_RANKING_DB_PATH_CACHE_SEC: float = 3.0