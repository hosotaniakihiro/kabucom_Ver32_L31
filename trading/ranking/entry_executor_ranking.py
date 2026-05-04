# ============================================================
# trading/ranking/entry_executor_ranking.py
# Ver27.2-RANKING-NO-BOARD-IMMEDIATE-FINAL-STABLE
# ------------------------------------------------------------
# ✔ ランキングENTRYは完全ノーボード
# ✔ kabu API 制限を完全回避
# ✔ rank_price のみで ENTRY
# ✔ PUSH / immediate 両トリガー対応
# ✔ EntryLog 完全対応
# ✔ ★ info=list を完全禁止（設計完成）
# ============================================================

import datetime as dt
import logging
from typing import Any

from global_state import global_data
from utils_common import calculate_shares
from trading.ranking.entry_executor import confirm_and_entry

# DB
from database import Session_position
from database.models import EntryLog

logger = logging.getLogger(__name__)


# ============================================================
# 内部ユーティリティ（最終型ガード）
# ============================================================

def _normalize_info(info: Any, symbol: str) -> dict | None:
    """
    executor は dict のみを扱う。
    list はここに来てはいけない（設計違反）。
    """
    if info is None:
        return None

    if isinstance(info, dict):
        return info

    logger.error(
        "[RANKING EXECUTOR] invalid info type symbol=%s type=%s value=%s",
        symbol, type(info), info
    )
    return None


# ============================================================
# ENTRY エントリーポイント（ランキング専用）
# ============================================================

def try_entry_from_push(push_row: dict | None = None):
    """
    ランキング由来 ENTRY（完全ノーボード）

    ・push_row はトリガー用途のみ（内容は使わない）
    ・pending_entries[symbol] は bucket(list) 前提
    ・bucket から ranking 用 entry を1件抽出して処理
    ・board API は一切使用しない
    """

    pending = getattr(global_data, "pending_entries", {})
    if not isinstance(pending, dict) or not pending:
        return

    now = dt.datetime.now()

    # --------------------------------------------------------
    # 全 pending をスキャン（ランキング専用）
    # --------------------------------------------------------
    for symbol, bucket in list(pending.items()):

        if not isinstance(bucket, list) or not bucket:
            continue

        # ----------------------------------------------
        # ★ ランキング由来 entry を1件抽出
        # ----------------------------------------------
        info = None
        for e in bucket:
            if not isinstance(e, dict):
                continue
            src = str(e.get("source", "")).upper()
            if src.startswith("RANKING"):
                info = e
                break

        if not info:
            continue

        info = _normalize_info(info, symbol)
        if not info:
            continue

        # immediate_entry フラグ（なければ True 扱い）
        if info.get("immediate_entry") is False:
            continue

        # ----------------------------------------------------
        # 有効期限チェック
        # ----------------------------------------------------
        entry_conditions = info.get("entry_conditions", {}) or {}
        expire_at = entry_conditions.get("expire_at")

        if expire_at and now > expire_at:
            pending.pop(symbol, None)
            logger.info("⏳ ENTRY期限切れ → pending削除: %s", symbol)
            continue

        is_buy = info.get("is_buy", True)
        symbolname = info.get("symbolname", "")
        reason = info.get("reason", "ランキング由来ENTRY")

        # ----------------------------------------------------
        # 価格決定（🔥 完全ノーボード）
        # ----------------------------------------------------
        rank_price = info.get("rank_price")
        if rank_price is None:
            logger.warning("[RANKING ENTRY] rank_price missing → skip %s", symbol)
            continue

        try:
            exec_price = float(rank_price)
        except Exception:
            logger.warning(
                "[RANKING ENTRY] invalid rank_price %s=%s",
                symbol, rank_price
            )
            continue

        # ----------------------------------------------------
        # 株数計算
        # ----------------------------------------------------
        qty = calculate_shares(
            exec_price,
            budget=500_000,
            unit_size=100
        )

        if qty <= 0:
            logger.warning(
                "[RANKING ENTRY] qty<=0 symbol=%s price=%.2f",
                symbol, exec_price
            )
            continue

        # ----------------------------------------------------
        # ENTRY 実行
        # ----------------------------------------------------
        logger.info(
            "🚀 RANKING ENTRY (NO BOARD) %s price=%.2f qty=%d reason=%s",
            symbol, exec_price, qty, reason
        )

        success = confirm_and_entry(
            symbol=symbol,
            symbolname=symbolname,
            qty=qty,
            is_buy=is_buy,
            reason=reason,
        )

        # ----------------------------------------------------
        # ENTRY成功 → EntryLog 保存
        # ----------------------------------------------------
        if success:
            session = None
            try:
                session = Session_position()

                trade_id = f"{symbol}_{now:%Y%m%d%H%M%S}"

                if now.hour == 9 and now.minute < 30:
                    time_bucket = "09:00-09:30"
                elif now.hour < 11:
                    time_bucket = "09:30-11:30"
                else:
                    time_bucket = "12:30-15:00"

                log = EntryLog(
                    trade_id=trade_id,
                    symbol=symbol,
                    symbolname=symbolname,
                    entry_time=now,
                    time_bucket=time_bucket,

                    is_buy=1 if is_buy else 0,
                    entry_price=exec_price,
                    qty=qty,

                    entry_source=info.get("source"),
                    trigger_type="ranking_no_board",

                    ranking_type=info.get("type_name"),
                    ranking_strength=info.get("ranking_strength"),

                    volume_speed=None,
                    volume_ratio=None,
                    rank_price=exec_price,

                    best_ask=None,
                    best_bid=None,
                    spread=None,
                    price_vs_rank=0.0,
                )

                session.add(log)
                session.commit()

                logger.info(
                    "🧠 EntryLog 保存完了: %s trade_id=%s",
                    symbol, trade_id
                )

            except Exception as e:
                if session:
                    session.rollback()
                logger.error(
                    "❌ EntryLog 保存失敗: %s %s",
                    symbol, e,
                    exc_info=True
                )
            finally:
                if session:
                    session.close()

            pending.pop(symbol, None)
            logger.info("✅ RANKING ENTRY 成功 → pending削除: %s", symbol)

        else:
            logger.warning("❌ RANKING ENTRY 失敗 → pending維持: %s", symbol)
