# ============================================================
# File   : trading/entry/tonosama_master_ai.py
# Version: Ver30.0-COMPAT-WRAPPER-TONOSAMA-PACKAGE
# ------------------------------------------------------------
# 旧import互換ラッパー。
# 実処理は trading.entry.tonosama パッケージへ分割済み。
# ============================================================
from __future__ import annotations
from trading.entry.tonosama.runner import tonosama_loop, build_tonosama_entries, iter_tonosama_candidate_rows
from trading.entry.tonosama.scheduler import register_tonosama_scheduler

def _safe_http_get(url: str, *, timeout: int = 5):
    return None

def fetch_one_ranking():
    return None

__all__ = ["tonosama_loop", "build_tonosama_entries", "iter_tonosama_candidate_rows", "register_tonosama_scheduler", "_safe_http_get", "fetch_one_ranking"]

if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    tonosama_loop()
