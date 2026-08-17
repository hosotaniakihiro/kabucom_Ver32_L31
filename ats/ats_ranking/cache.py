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

# ranking DB パスの解決は NAS 上の存在確認 + sqlite への接続を伴う。
# 3.0 秒では 1分Summary の 1 tick (実測 48 秒) の中で毎回期限切れになり、
# 1 tick あたり 11 回フル解決していた (実測 13 秒 = tick の約 1/4)。
# 日付が変わった場合は TTL とは別に
# db_path.get_usable_ranking_db_path() が
# 「場中に cached != today_path なら破棄」で必ず捕まえるので、
# TTL を延ばしても過去DBを掴み続けることはない。
_ATS_RANKING_DB_PATH_CACHE_SEC: float = 60.0