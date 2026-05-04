# ============================================================
# File   : trading/entry/ai_enricher.py
# Version: Ver8.2-FINAL-PENDING-MANAGER-ONLY-WITH-STATS
# ------------------------------------------------------------
# ✔ Ver8.1 完全保持（削除ゼロ思想）
# ✔ pending_manager 経由のみ（直代入ゼロ）
# ✔ bucket = list[dict] を絶対に破壊しない
# ✔ enrich 専用（判断・発注しない）
# ✔ scheduler 多重呼び出し耐性
# ✔ dict / list 混入事故を完全遮断
# ✔ 差分更新のみ（無駄な replace を防止）
# ✔ スレッド絶対に落ちない
# ✔ NEW: input symbols count log
# ✔ NEW: input entries count log
# ✔ NEW: changed entries count log
# ✔ NEW: ai_allow true / false count log
# ✔ NEW: skipped / unchanged count log
# ============================================================

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Dict, List

from AI.entry_gate import ai_final_entry_check
from trading.entry.pending_manager import (
    get_bucket,
    replace_bucket,
)

logger = logging.getLogger(__name__)


# ============================================================
# helpers
# ============================================================

def _safe_float(v, default: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def _safe_bool(v, default: bool = False) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    try:
        s = str(v).strip().lower()
    except Exception:
        return default

    if s in ("1", "true", "t", "yes", "y", "on"):
        return True
    if s in ("0", "false", "f", "no", "n", "off", ""):
        return False
    return default


def _safe_str(v) -> str:
    try:
        return str(v).strip()
    except Exception:
        return ""


# ============================================================
# AI enrich（唯一の公開API）
# ============================================================

def enrich_pending_entries_with_ai():
    """
    pending_entries に AI 情報を「付与するだけ」

    Rules:
    - global_data.pending_entries には一切触らない
    - pending_manager 経由のみ
    - bucket(list[dict])構造を絶対に壊さない
    - allow / 発注判断は entry_controller 側
    """

    try:
        logger.info("🤖 AI ENRICH START")

        # key 取得のみ（READ ONLY）
        from global_state import global_data

        root = getattr(global_data, "pending_entries", None)
        if not isinstance(root, dict) or not root:
            logger.info(
                "[AI ENRICH] root empty symbols=0 entries=0 changed=0 allow_true=0 allow_false=0"
            )
            logger.info("🤖 AI ENRICH DONE")
            return

        total_symbols = 0
        total_entries = 0
        processed_entries = 0
        changed_entries = 0
        unchanged_entries = 0
        skipped_entries = 0
        allow_true_count = 0
        allow_false_count = 0
        updated_symbols = 0
        empty_bucket_symbols = 0

        root_keys = list(root.keys())
        logger.info("[AI ENRICH] input root symbols=%d", len(root_keys))

        for symbol in root_keys:
            try:
                bucket: List[Dict] = get_bucket(symbol)
                total_symbols += 1

                if not bucket:
                    empty_bucket_symbols += 1
                    continue

                total_entries += len(bucket)

                new_bucket: List[Dict] = []
                changed = False

                for entry_org in bucket:
                    try:
                        if not isinstance(entry_org, dict):
                            skipped_entries += 1
                            new_bucket.append(entry_org)
                            continue

                        entry = deepcopy(entry_org)

                        side = entry.get("side") or entry.get("entry_decision")
                        if not side:
                            skipped_entries += 1
                            new_bucket.append(entry_org)
                            continue

                        processed_entries += 1

                        # ------------------------------------
                        # AI 呼び出し（情報付与のみ）
                        # ------------------------------------
                        ai = ai_final_entry_check(entry)

                        new_conf = _safe_float(ai.get("confidence", 0.0))
                        new_allow = _safe_bool(ai.get("allow", False))
                        new_reason = _safe_str(ai.get("reason", ""))

                        if new_allow:
                            allow_true_count += 1
                        else:
                            allow_false_count += 1

                        # 既存値
                        old_conf = entry_org.get("confidence")
                        old_allow = entry_org.get("ai_allow")
                        old_reason = entry_org.get("ai_reason")

                        # 差分チェック
                        if (
                            old_conf != new_conf
                            or old_allow != new_allow
                            or old_reason != new_reason
                        ):
                            entry["confidence"] = new_conf
                            entry["ai_allow"] = new_allow
                            entry["ai_reason"] = new_reason
                            changed = True
                            changed_entries += 1
                        else:
                            # 変更なし → 元オブジェクト保持
                            entry = entry_org
                            unchanged_entries += 1

                        new_bucket.append(entry)

                    except Exception:
                        skipped_entries += 1
                        logger.exception(
                            "[AI ENRICH ITEM ERROR] symbol=%s",
                            symbol,
                        )
                        new_bucket.append(entry_org)

                # ------------------------------------
                # bucket 更新（差分がある場合のみ）
                # ------------------------------------
                if changed:
                    replace_bucket(symbol, new_bucket)
                    updated_symbols += 1

            except Exception:
                logger.exception("[AI ENRICH SYMBOL ERROR] symbol=%s", symbol)

        logger.info(
            "[AI ENRICH] symbols=%d empty_bucket_symbols=%d total_entries=%d processed_entries=%d",
            total_symbols,
            empty_bucket_symbols,
            total_entries,
            processed_entries,
        )

        logger.info(
            "[AI ENRICH] changed_entries=%d unchanged_entries=%d skipped_entries=%d updated_symbols=%d",
            changed_entries,
            unchanged_entries,
            skipped_entries,
            updated_symbols,
        )

        logger.info(
            "[AI ENRICH] ai_allow_true=%d ai_allow_false=%d",
            allow_true_count,
            allow_false_count,
        )

        logger.info("🤖 AI ENRICH DONE")

    except Exception:
        logger.exception("[AI ENRICH FATAL]")