# ============================================================
# File   : trading/entry/pending_manager.py
# Purpose: pending_entries 完全一元管理（FINAL）
# ------------------------------------------------------------
# ✔ pending_entries = dict[str, list[dict]] を絶対保証
# ✔ 直代入 / 型崩れを STACKTRACE 付きで検出
# ✔ source 単位ではなく identity(source, entry_type, side, interval) で重複防止
# ✔ entry_controller 向け安全API追加（iter / pop / prune）
# ✔ Scheduler Loop を絶対に落とさない
# ✔ ROOT 可視化・件数監視・空状態の原因追跡を強化
# ✔ reject理由を可視化
# ✔ interval違い / SELL不可 / 古い候補を安全に掃除できる prune_entries を追加
# ============================================================

from __future__ import annotations

import logging
import traceback
from typing import Dict, List, Any, Iterator, Tuple, Callable

from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# 内部: pending_entries root ガード
# ============================================================
def _ensure_root() -> None:
    """
    pending_entries の存在・型を保証する唯一の場所
    """
    if not hasattr(global_data, "pending_entries"):
        global_data.pending_entries = {}
        logger.debug("🧱 pending_entries root CREATED")
        return

    if not isinstance(global_data.pending_entries, dict):
        logger.critical(
            "\n🚨 pending_entries DIRECT ASSIGN DETECTED 🚨\n"
            "TYPE=%s VALUE=%r\nSTACKTRACE:\n%s",
            type(global_data.pending_entries),
            global_data.pending_entries,
            "".join(traceback.format_stack()),
        )
        global_data.pending_entries = {}
        logger.critical("🧱 pending_entries root RESET")


# ============================================================
# 内部: bucket 正規化
# ============================================================
def _normalize_bucket(bucket: Any, symbol: str) -> List[Dict]:
    """
    bucket を必ず list[dict] に正規化
    """
    if bucket is None:
        return []

    if isinstance(bucket, dict):
        logger.error(
            "\n⚠ pending bucket was DICT (ILLEGAL WRITE)\n"
            "symbol=%s value=%r\nSTACKTRACE:\n%s",
            symbol,
            bucket,
            "".join(traceback.format_stack()),
        )
        return [bucket]

    if isinstance(bucket, list):
        cleaned: List[Dict] = []
        for e in bucket:
            if isinstance(e, dict):
                cleaned.append(e)
            else:
                logger.error(
                    "❌ INVALID pending entry dropped "
                    "symbol=%s type=%s value=%r",
                    symbol,
                    type(e),
                    e,
                )
        return cleaned

    logger.error(
        "❌ INVALID pending bucket type reset "
        "symbol=%s type=%s value=%r",
        symbol,
        type(bucket),
        bucket,
    )
    return []


# ============================================================
# 内部: identity / helper
# ============================================================
def _norm_str(v: Any) -> str:
    try:
        if v is None:
            return ""
        return str(v).strip().upper()
    except Exception:
        return ""


def _norm_interval(v: Any) -> str:
    try:
        if v is None or v == "":
            return ""
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _entry_identity(entry: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """
    重複判定用 identity。
    """
    try:
        return (
            _norm_str(entry.get("source")),
            _norm_str(entry.get("entry_type")),
            _norm_str(entry.get("side") or entry.get("entry_decision")),
            _norm_interval(entry.get("interval")),
        )
    except Exception:
        return ("", "", "", "")


# ============================================================
# 公開API: pending root snapshot（デバッグ用）
# ============================================================
def snapshot_root() -> Dict[str, int]:
    """
    Returns:
        {symbol: bucket_size}
    """
    _ensure_root()
    snap: Dict[str, int] = {}
    for sym, bucket in list(global_data.pending_entries.items()):
        normalized = _normalize_bucket(bucket, sym)
        if normalized:
            global_data.pending_entries[sym] = normalized
            snap[sym] = len(normalized)
        else:
            global_data.pending_entries.pop(sym, None)
    return snap


# ============================================================
# 公開API: bucket 取得（copy）
# ============================================================
def get_bucket(symbol: str) -> List[Dict]:
    _ensure_root()
    sym = str(symbol)
    raw = global_data.pending_entries.get(sym)
    normalized = _normalize_bucket(raw, sym)
    if normalized:
        global_data.pending_entries[sym] = normalized
    else:
        global_data.pending_entries.pop(sym, None)
    return list(normalized)


# ============================================================
# 公開API: bucket 置換（唯一の更新ルート）
# ============================================================
def replace_bucket(symbol: str, new_bucket: List[Dict]) -> None:
    _ensure_root()
    sym = str(symbol)
    normalized = _normalize_bucket(new_bucket, sym)
    if normalized:
        global_data.pending_entries[sym] = normalized
    else:
        global_data.pending_entries.pop(sym, None)
    logger.debug(
        "🔁 pending bucket replaced symbol=%s size=%d",
        sym,
        len(normalized),
    )


# ============================================================
# 公開API: source 重複チェック
# ============================================================
def has_source(symbol: str, source: str) -> bool:
    if not source:
        return False
    bucket = get_bucket(symbol)
    src = _norm_str(source)
    return any(
        _norm_str(e.get("source")) == src
        for e in bucket
        if isinstance(e, dict)
    )


# ============================================================
# 公開API: identity 重複チェック
# ============================================================
def has_identity(symbol: str, entry: Dict[str, Any]) -> bool:
    if not symbol or not isinstance(entry, dict):
        return False

    target = _entry_identity(entry)
    bucket = get_bucket(symbol)

    return any(
        _entry_identity(e) == target
        for e in bucket
        if isinstance(e, dict)
    )


# ============================================================
# 公開API: pending 追加（唯一の入口）
# ============================================================
def add_pending(entry: Dict) -> bool:
    _ensure_root()

    if not isinstance(entry, dict):
        logger.error("❌ entry is not dict: %r", entry)
        return False

    symbol = entry.get("symbol")
    source = entry.get("source")

    if not symbol or not source:
        logger.error(
            "❌ invalid pending entry (symbol/source missing): %r",
            entry,
        )
        return False

    sym = str(symbol)
    src = str(source)
    new_identity = _entry_identity(entry)

    bucket = get_bucket(sym)

    for e in bucket:
        old_identity = _entry_identity(e)

        if old_identity == new_identity:
            logger.info(
                "⏭ pending duplicate skipped symbol=%s source=%s identity=%s",
                sym,
                src,
                new_identity,
            )
            return False

    new_bucket = bucket + [entry]
    replace_bucket(sym, new_bucket)

    logger.info(
        "🧩 pending added symbol=%s source=%s side=%s interval=%s entry_type=%s score=%s bucket_size=%d identity=%s",
        sym,
        src,
        entry.get("side"),
        entry.get("interval"),
        entry.get("entry_type"),
        entry.get("score"),
        len(new_bucket),
        new_identity,
    )

    logger.debug(
        "📦 pending_root_snapshot=%s",
        snapshot_root(),
    )

    return True


# ============================================================
# entry_controller 用: 全 pending を安全に列挙
# ============================================================
def iter_entries() -> Iterator[Tuple[str, Dict]]:
    """
    Yields:
        (symbol, entry_dict)
    """
    _ensure_root()
    for sym, bucket in list(global_data.pending_entries.items()):
        normalized = _normalize_bucket(bucket, sym)
        if normalized:
            global_data.pending_entries[sym] = normalized
        else:
            global_data.pending_entries.pop(sym, None)
            continue
        for entry in normalized:
            yield sym, entry


# ============================================================
# entry_controller 用: 発火後に1件だけ安全に削除
# ============================================================
def pop_entry(symbol: str, entry: Dict) -> None:
    """
    指定 entry を bucket から1件だけ削除
    """
    _ensure_root()
    sym = str(symbol)
    bucket = get_bucket(sym)

    target_identity = _entry_identity(entry) if isinstance(entry, dict) else None
    removed = False
    new_bucket: List[Dict] = []

    for e in bucket:
        if not removed and (e is entry or (target_identity is not None and _entry_identity(e) == target_identity)):
            removed = True
            continue
        new_bucket.append(e)

    replace_bucket(sym, new_bucket)

    logger.info(
        "🧹 pending popped symbol=%s removed=%s remain=%d",
        sym,
        removed,
        len(new_bucket),
    )


# ============================================================
# entry_controller 用: 条件一致 entry を安全に削除
# ============================================================
def prune_entries(
    predicate: Callable[[str, Dict[str, Any]], bool],
    *,
    reason: str = "PRUNE",
    max_remove: int | None = None,
) -> int:
    """
    predicate(symbol, entry) が True を返した pending entry を削除する。

    用途:
      - PIPELINE_FILTER_MISMATCH の古い interval 候補削除
      - SELL_CREDIT_GUARD_NG の空売り不可候補削除
      - POSITION_FILTER_NG 等の再評価不要候補削除

    例:
        prune_entries(lambda sym, e: e.get("source") == "SUMMARY" and e.get("interval") == 1)
    """
    _ensure_root()
    removed = 0

    for sym, bucket in list(global_data.pending_entries.items()):
        normalized = _normalize_bucket(bucket, sym)
        kept: List[Dict[str, Any]] = []

        for entry in normalized:
            try:
                if max_remove is not None and removed >= max_remove:
                    kept.append(entry)
                    continue

                if predicate(str(sym), entry):
                    removed += 1
                    logger.info(
                        "🧹 pending pruned symbol=%s reason=%s identity=%s entry=%s",
                        sym,
                        reason,
                        _entry_identity(entry),
                        {
                            "source": entry.get("source"),
                            "entry_type": entry.get("entry_type"),
                            "side": entry.get("side"),
                            "interval": entry.get("interval"),
                            "score": entry.get("score"),
                        },
                    )
                    continue

                kept.append(entry)

            except Exception:
                logger.exception("pending prune predicate failed symbol=%s entry=%r", sym, entry)
                kept.append(entry)

        replace_bucket(str(sym), kept)

    if removed:
        logger.info("🧹 pending prune done reason=%s removed=%d root=%s", reason, removed, snapshot_root())
    return removed


# ============================================================
# 公開API: symbol 単位で全削除
# ============================================================
def clear_symbol(symbol: str) -> None:
    _ensure_root()
    sym = str(symbol)
    if sym in global_data.pending_entries:
        del global_data.pending_entries[sym]
        logger.info("🧹 pending cleared symbol=%s", sym)


# ============================================================
# 公開API: 全 pending 削除
# ============================================================
def clear_all() -> None:
    global_data.pending_entries = {}
    logger.warning("🧹 ALL pending_entries cleared")