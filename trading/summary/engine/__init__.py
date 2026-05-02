# ============================================================
# File   : trading/summary/engine/__init__.py
# Ver    : PRODUCTION-STABLE-SUMMARY-ENGINE-PACKAGE-V1.0
# ------------------------------------------------------------
# ✔ engine パッケージ入口
# ✔ 旧 summary_engine import 経路との互換を維持
# ✔ 実体は push_summary_engine を優先
# ✔ ranking は adapter 側へ分離
# ============================================================

from __future__ import annotations

from .push_summary_engine import (
    build_push_summary,
    build_summary,
    push_summary_engine,
    run,
    run_push_summary_engine,
    run_summary_engine,
)
from .ranking_summary_engine_adapter import (
    build_ranking_summary,
    run_ranking_summary_engine,
)

__all__ = [
    # push
    "build_summary",
    "build_push_summary",
    "push_summary_engine",
    "run_push_summary_engine",
    "run_summary_engine",
    "run",

    # ranking
    "build_ranking_summary",
    "run_ranking_summary_engine",
]