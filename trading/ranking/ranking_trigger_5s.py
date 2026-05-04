# ============================================================
# File   : trading/ranking/ranking_trigger_5s.py
# Ver27-RANKING-5S-TRIGGER-FINAL-PENDING-MANAGED
# ------------------------------------------------------------
# ✔ pending_manager 完全準拠
# ✔ list[dict] 構造保証
# ✔ is_initializing 完全対応
# ✔ expire entry 単位管理
# ✔ entry_controller / summary 完全互換
# ✔ pending_entries 直接操作 完全禁止
# ✔ 循環 import 完全排除
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Dict, List

from global_state import global_data
from trading.entry.pending_manager import (
    add_pending,
    get_bucket,
    replace_bucket,   # ★ 追加（必須）
)

logger = logging.getLogger("ranking_trigger_5s")


# ============================================================
# 内部 util：expire 済み pending entry を掃除
# ============================================================
def _cleanup_expired_entries(symbol: str, now: dt.datetime) -> None:
    """
    指定 symbol の bucket から expire 済み entry を削除
    ※ pending_manager 経由のみ
    """
    bucket = get_bucket(symbol)
    if not bucket:
        return

    survived: List[Dict] = []
    removed = False

    for e in bucket:
        expire_at = (
            e.get("entry_conditions", {})
             .get("expire_at")
        )
        if expire_at and expire_at < now:
            logger.debug(
                "[RANKING] expired entry removed symbol=%s source=%s",
                symbol,
                e.get("source"),
            )
            removed = True
            continue

        survived.append(e)

    # ★ 差分があるときのみ置換
    if removed:
        replace_bucket(symbol, survived)


# ============================================================
# コア：5秒ランキング ENTRY 登録
# ============================================================
def trigger_ranking_entry_5s(
    symbol: str,
    symbolname: str,
    type_name: str,
    rank_price: float,
    ranking_strength: int,
    reason: str,
    market: str = "ALL",
):
    """
    5秒ランキング由来の ENTRY 候補を pending_entries に登録する
    ※ ENTRY 可否判断は一切しない
    """

    # --------------------------------------------------------
    # 初期化中は登録しない（entry_controller と整合）
    # --------------------------------------------------------
    if getattr(global_data, "is_initializing", False):
        logger.debug("[RANKING] initializing → skip register")
        return

    now = dt.datetime.now()
    sym = str(symbol)

    # --------------------------------------------------------
    # 売買方向判定
    # --------------------------------------------------------
    is_buy = not (
        "値下がり" in type_name
        or "down" in type_name.lower()
    )

    # --------------------------------------------------------
    # expire 済み掃除（symbol 単位）
    # --------------------------------------------------------
    _cleanup_expired_entries(sym, now)

    # --------------------------------------------------------
    # 登録 entry 構築
    # --------------------------------------------------------
    entry: Dict = {
        "symbol": sym,
        "symbolname": symbolname,
        "side": "BUY" if is_buy else "SELL",
        "source": "RANKING_5S",

        # ranking 情報
        "type_name": type_name,
        "market": market,
        "ranking_strength": int(ranking_strength),
        "rank_price": float(rank_price),

        # ENTRY 条件
        "entry_conditions": {
            "need_push": False,
            "max_price_gap": 0.003,
            "expire_at": now + dt.timedelta(seconds=20),
        },

        # メタ情報
        "created_at": now,
        "reason": reason,
    }

    # --------------------------------------------------------
    # pending 登録（重複 source は自動防止）
    # --------------------------------------------------------
    if not add_pending(entry):
        logger.debug(
            "[RANKING] duplicate or rejected → skip symbol=%s",
            sym,
        )
        return

    logger.info(
        "⚡ RANKING ENTRY REGISTERED → %s %s (%s)",
        sym,
        entry["side"],
        type_name,
    )


# ============================================================
# tosama_detector 用ラッパー
# ============================================================
def trigger_ranking_from_tosama(
    symbol: str,
    symbolname: str,
    rank_price: float,
    ranking_strength: int,
    reason: str,
    type_name: str = "殿様ランキング",
    market: str = "ALL",
):
    """
    tosama_detector から呼ばれる専用ラッパー
    """
    trigger_ranking_entry_5s(
        symbol=symbol,
        symbolname=symbolname,
        type_name=type_name,
        rank_price=rank_price,
        ranking_strength=ranking_strength,
        reason=reason,
        market=market,
    )
