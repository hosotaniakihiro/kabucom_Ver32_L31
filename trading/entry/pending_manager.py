# ============================================================
# File   : trading/entry/pending_manager.py
# Purpose: pending_entries 完全一元管理（FINAL）
# Version: Ver02-NO-MIXED-BUY-SELL-PER-SYMBOL
# ------------------------------------------------------------
# ✔ pending_entries = dict[str, list[dict]] を絶対保証
# ✔ 直代入 / 型崩れを STACKTRACE 付きで検出
# ✔ source 単位ではなく identity(source, entry_type, side, interval) で重複防止
# ✔ entry_controller 向け安全API追加（iter / pop / prune）
# ✔ Scheduler Loop を絶対に落とさない
# ✔ ROOT 可視化・件数監視・空状態の原因追跡を強化
# ✔ reject理由を可視化
# ✔ interval違い / SELL不可 / 古い候補を安全に掃除できる prune_entries を追加
#
# Ver02:
# ✔ 同一銘柄 bucket 内の BUY / SELL 混在を禁止
# ✔ 既存 BUY に対する SELL 追加、既存 SELL に対する BUY 追加を拒否
# ✔ 既に混在している bucket は発注列挙時に全削除して安全停止
# ✔ 「表示はBUYなのに実発注はSELL」事故を防止
# ============================================================

from __future__ import annotations

import logging
import os
import traceback
from typing import Dict, List, Any, Iterator, Tuple, Callable, Set

from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# ENV
# ============================================================
def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None:
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on"}:
            return True
        if s in {"0", "false", "no", "n", "off", ""}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


PENDING_REJECT_MIXED_SIDE = _env_bool("PENDING_REJECT_MIXED_SIDE", True)
PENDING_CLEAR_ALREADY_MIXED_BUCKET = _env_bool("PENDING_CLEAR_ALREADY_MIXED_BUCKET", True)


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


def _norm_side(v: Any) -> str:
    s = _norm_str(v)
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _entry_side(entry: Dict[str, Any]) -> str:
    if not isinstance(entry, dict):
        return ""
    return _norm_side(entry.get("side") or entry.get("entry_decision") or entry.get("ai_side"))


def _bucket_sides(bucket: List[Dict]) -> Set[str]:
    sides: Set[str] = set()
    for e in bucket:
        side = _entry_side(e)
        if side in {"BUY", "SELL"}:
            sides.add(side)
    return sides


def _bucket_has_mixed_side(bucket: List[Dict]) -> bool:
    sides = _bucket_sides(bucket)
    return "BUY" in sides and "SELL" in sides


def _entry_identity(entry: Dict[str, Any]) -> Tuple[str, str, str, str]:
    """
    重複判定用 identity。
    """
    try:
        return (
            _norm_str(entry.get("source")),
            _norm_str(entry.get("entry_type")),
            _entry_side(entry),
            _norm_interval(entry.get("interval")),
        )
    except Exception:
        return ("", "", "", "")


def _entry_debug(entry: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return {
            "source": entry.get("source"),
            "entry_type": entry.get("entry_type"),
            "side": entry.get("side"),
            "entry_decision": entry.get("entry_decision"),
            "ai_side": entry.get("ai_side"),
            "interval": entry.get("interval"),
            "score": entry.get("score"),
            "score_buy": entry.get("score_buy"),
            "score_sell": entry.get("score_sell"),
        }
    except Exception:
        return {}


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
# 公開API: symbol bucket が BUY/SELL 混在か
# ============================================================
def has_mixed_side(symbol: str) -> bool:
    bucket = get_bucket(symbol)
    return _bucket_has_mixed_side(bucket)


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
    new_side = _entry_side(entry)

    bucket = get_bucket(sym)

    # --------------------------------------------------------
    # BUY / SELL 混在禁止
    # --------------------------------------------------------
    if PENDING_REJECT_MIXED_SIDE:
        existing_sides = _bucket_sides(bucket)

        if _bucket_has_mixed_side(bucket):
            logger.warning(
                "⛔ pending mixed-side bucket detected -> clear symbol=%s sides=%s bucket=%s",
                sym,
                sorted(existing_sides),
                [_entry_debug(e) for e in bucket],
            )
            if PENDING_CLEAR_ALREADY_MIXED_BUCKET:
                replace_bucket(sym, [])
            return False

        if new_side in {"BUY", "SELL"} and existing_sides and new_side not in existing_sides:
            logger.warning(
                "⛔ pending opposite-side rejected symbol=%s existing_sides=%s new_side=%s new=%s bucket=%s",
                sym,
                sorted(existing_sides),
                new_side,
                _entry_debug(entry),
                [_entry_debug(e) for e in bucket],
            )
            return False

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
            if PENDING_REJECT_MIXED_SIDE and _bucket_has_mixed_side(normalized):
                logger.warning(
                    "⛔ pending mixed-side bucket skipped before dispatch symbol=%s sides=%s bucket=%s",
                    sym,
                    sorted(_bucket_sides(normalized)),
                    [_entry_debug(e) for e in normalized],
                )
                if PENDING_CLEAR_ALREADY_MIXED_BUCKET:
                    global_data.pending_entries.pop(sym, None)
                    logger.warning("🧹 pending mixed-side bucket cleared symbol=%s", sym)
                continue
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
                        _entry_debug(entry),
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


__all__ = [
    "snapshot_root",
    "get_bucket",
    "replace_bucket",
    "has_source",
    "has_identity",
    "has_mixed_side",
    "add_pending",
    "iter_entries",
    "pop_entry",
    "prune_entries",
    "clear_symbol",
    "clear_all",
]
