# ============================================================
# summary_kind.py
# ------------------------------------------------------------
# ✔ summary の種別を型で分離
# ✔ realtime が confirmed を壊す事故を防止
# ============================================================

from enum import Enum


class SummaryKind(str, Enum):
    INITIAL = "initial"
    CONFIRMED = "confirmed"
    REALTIME = "realtime"
