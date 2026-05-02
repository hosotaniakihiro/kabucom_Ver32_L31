# ============================================================
# File   : trading/summary/engine/summary_engine.py
# Version: Ver31.0-PRODUCTION-SUMMARY-ENGINE-COMPAT
# ------------------------------------------------------------
# ✔ 旧 import 経路との互換を維持
# ✔ 実体は push_summary_engine へ委譲
# ✔ ranking と混線しない
# ============================================================

from __future__ import annotations

from .push_summary_engine import (
    build_summary,
    build_push_summary,
    push_summary_engine,
    run,
    run_push_summary_engine,
    run_summary_engine,
)

__all__ = [
    "build_summary",
    "build_push_summary",
    "push_summary_engine",
    "run_push_summary_engine",
    "run_summary_engine",
    "run",
]