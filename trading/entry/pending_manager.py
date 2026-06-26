# ============================================================
# File   : trading/entry/pending_manager.py
# Purpose: pending_entries 完全一元管理
# Version: Ver03-STALE-DUPLICATE-REFRESH
# ------------------------------------------------------------
# ✔ pending_entries = dict[str, list[dict]] を絶対保証
# ✔ 直代入 / 型崩れを STACKTRACE 付きで検出
# ✔ source 単位ではなく identity(source, entry_type, side, interval) で重複防止
# ✔ entry_controller 向け安全API追加（iter / pop / prune）
# ✔ Scheduler Loop を絶対に落とさない
# ✔ ROOT 可視化・件数監視・空状態の原因追跡を強化
# ✔ reject理由を可視化
# ✔ interval違い / SELL不可 / 古い候補を安全に掃除できる prune_entries を追加
# ✔ 同一銘柄 bucket 内の BUY / SELL 混在を禁止
# ✔ 既に混在している bucket は発注列挙時に全削除して安全停止
#
# Ver03:
# ✔ pending duplicate が残り続けて新しい SUMMARY_AI 候補を捨てる問題を修正
# ✔ 同一 identity の既存 pending が一定秒数以上古い場合は、新しい entry で置換
# ✔ 置換時に stale_duplicate_replaced ログを出し、古い候補で永久停止しないようにする
# ============================================================

from __future__ import annotations

import datetime as dt
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
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


PENDING_REJECT_MIXED_SIDE = _env_bool("PENDING_REJECT_MIXED_SIDE", True)
PENDING_CLEAR_ALREADY_MIXED_BUCKET = _env_bool("PENDING_CLEAR_ALREADY_MIXED_BUCKET", True)
PENDING_REPLACE_STALE_DUPLICATE = _env_bool("PENDING_REPLACE_STALE_DUPLICATE", True)
PENDING_DUPLICATE_STALE_SEC = max(1.0, _env_float("PENDING_DUPLICATE_STALE_SEC", 20.0))
PENDING_DEFAULT_MAX_AGE_SEC = max(PENDING_DUPLICATE_STALE_SEC, _env_float("PENDING_DEFAULT_MAX_AGE_SEC", 180.0))


# ============================================================
# 内部: pending_entries root ガード
# ============================================================
def _ensure_root() -> None:
    """pending_entries の存在・型を保証する唯一の場所。"""
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
    """bucket を必ず list[dict] に正規化。"""
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
                    "❌ INVALID pending entry dropped symbol=%s type=%s value=%r",
                    symbol,
                    type(e),
                    e,
                )
        return cleaned

    logger.error(
        "❌ INVALID pending bucket type reset symbol=%s type=%s value=%r",
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
    """重複判定用 identity。"""
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
            "created_at": entry.get("created_at"),
            "updated_at": entry.get("updated_at"),
        }
    except Exception:
        return {}


def _now() -> dt.datetime:
    return dt.datetime.now()


def _parse_dt(v: Any) -> dt.datetime | None:
    try:
        if isinstance(v, dt.datetime):
            return v.replace(tzinfo=None)
        if v is None or str(v).strip() == "":
            return None
        s = str(v).strip()
        try:
            # pandas Timestamp / ISO文字列をできる範囲で吸収
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
            try:
                return dt.datetime.strptime(s, fmt)
            except Exception:
                continue
    except Exception:
        return None
    return None


def _entry_created_at(entry: Dict[str, Any]) -> dt.datetime | None:
    if not isinstance(entry, dict):
        return None
    for key in ("created_at", "pending_created_at", "entry_time", "datetime", "dt", "time"):
        ts = _parse_dt(entry.get(key))
        if ts is not None:
            return ts
    return None


def _entry_age_sec(entry: Dict[str, Any]) -> float | None:
    ts = _entry_created_at(entry)
    if ts is None:
        return None
    try:
        return max(0.0, (_now() - ts).total_seconds())
    except Exception:
        return None


def _prepare_new_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(entry)
    now = _now()
    if _entry_created_at(out) is None:
        out["created_at"] = now
    out["updated_at"] = now
    return out


def _is_stale_duplicate(old_entry: Dict[str, Any], new_entry: Dict[str, Any]) -> tuple[bool, str, float | None]:
    if not PENDING_REPLACE_STALE_DUPLICATE:
        return False, "disabled", None
    old_age = _entry_age_sec(old_entry)
    if old_age is None:
        # 作成時刻の無い古いpendingは永久残留の原因になりやすいので置換対象にする。
        return True, "old_created_at_missing", None
    stale_sec = PENDING_DUPLICATE_STALE_SEC
    try:
        # SUMMARY_AI は実行周期が短く、古い重複が残ると発火不能になるため短めに置換。
        if _norm_str(new_entry.get("entry_type")) == "SUMMARY_AI" or _norm_str(new_entry.get("source")) == "SUMMARY":
            stale_sec = max(5.0, _env_float("PENDING_SUMMARY_AI_DUPLICATE_STALE_SEC", PENDING_DUPLICATE_STALE_SEC))
    except Exception:
        pass
    if old_age >= stale_sec:
        return True, f"old_age_sec>={stale_sec:.1f}", old_age
    return False, f"old_age_sec<{stale_sec:.1f}", old_age


def _drop_expired_entries(bucket: List[Dict[str, Any]], *, symbol: str) -> List[Dict[str, Any]]:
    kept: List[Dict[str, Any]] = []
    removed = 0
    for e in bucket:
        age = _entry_age_sec(e)
        if age is not None and age >= PENDING_DEFAULT_MAX_AGE_SEC:
            removed += 1
            logger.info(
                "🧹 pending expired before add symbol=%s age=%.1fs max_age=%.1fs identity=%s entry=%s",
                symbol,
                age,
                PENDING_DEFAULT_MAX_AGE_SEC,
                _entry_identity(e),
                _entry_debug(e),
            )
            continue
        kept.append(e)
    if removed:
        logger.info("🧹 pending expired cleanup symbol=%s removed=%d kept=%d", symbol, removed, len(kept))
    return kept


# ============================================================
# 公開API: pending root snapshot（デバッグ用）
# ============================================================
def snapshot_root() -> Dict[str, int]:
    """Returns: {symbol: bucket_size}"""
    _ensure_root()
    snap: Dict[str, int] = {}
    for sym, bucket in list(global_data.pending_entries.items()):
        normalized = _normalize_bucket(bucket, sym)
        normalized = _drop_expired_entries(normalized, symbol=str(sym))
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
    normalized = _drop_expired_entries(normalized, symbol=sym)
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
    normalized = _drop_expired_entries(normalized, symbol=sym)
    if normalized:
        global_data.pending_entries[sym] = normalized
    else:
        global_data.pending_entries.pop(sym, None)
    logger.debug("🔁 pending bucket replaced symbol=%s size=%d", sym, len(normalized))


# ============================================================
# 公開API: source 重複チェック
# ============================================================
def has_source(symbol: str, source: str) -> bool:
    if not source:
        return False
    bucket = get_bucket(symbol)
    src = _norm_str(source)
    return any(_norm_str(e.get("source")) == src for e in bucket if isinstance(e, dict))


# ============================================================
# 公開API: identity 重複チェック
# ============================================================
def has_identity(symbol: str, entry: Dict[str, Any]) -> bool:
    if not symbol or not isinstance(entry, dict):
        return False
    target = _entry_identity(entry)
    bucket = get_bucket(symbol)
    return any(_entry_identity(e) == target for e in bucket if isinstance(e, dict))


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

    entry = _prepare_new_entry(entry)
    symbol = entry.get("symbol")
    source = entry.get("source")

    if not symbol or not source:
        logger.error("❌ invalid pending entry (symbol/source missing): %r", entry)
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

    new_bucket: List[Dict[str, Any]] = []
    replaced = False
    for e in bucket:
        old_identity = _entry_identity(e)
        if old_identity == new_identity:
            stale, why, old_age = _is_stale_duplicate(e, entry)
            if stale:
                new_bucket.append(entry)
                replaced = True
                logger.warning(
                    "🔁 pending duplicate stale replaced symbol=%s source=%s identity=%s old_age=%s reason=%s old=%s new=%s",
                    sym,
                    src,
                    new_identity,
                    None if old_age is None else round(old_age, 1),
                    why,
                    _entry_debug(e),
                    _entry_debug(entry),
                )
                continue
            new_bucket.append(e)
            logger.info(
                "⏭ pending duplicate skipped symbol=%s source=%s identity=%s old_age=%s reason=%s",
                sym,
                src,
                new_identity,
                None if old_age is None else round(old_age, 1),
                why,
            )
            # 同一identityが複数ある異常状態を避けるため、残りはそのまま維持して終了する。
            for rest in bucket[len(new_bucket):]:
                if rest is not e:
                    new_bucket.append(rest)
            replace_bucket(sym, new_bucket)
            return False
        new_bucket.append(e)

    if replaced:
        replace_bucket(sym, new_bucket)
        logger.info("📦 pending_root_snapshot=%s", snapshot_root())
        return True

    new_bucket.append(entry)
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
    logger.debug("📦 pending_root_snapshot=%s", snapshot_root())
    return True


# ============================================================
# entry_controller 用: 全 pending を安全に列挙
# ============================================================
def iter_entries() -> Iterator[Tuple[str, Dict]]:
    """Yields: (symbol, entry_dict)"""
    _ensure_root()
    for sym, bucket in list(global_data.pending_entries.items()):
        normalized = _normalize_bucket(bucket, sym)
        normalized = _drop_expired_entries(normalized, symbol=str(sym))
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
    """指定 entry を bucket から1件だけ削除。"""
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
    logger.info("🧹 pending popped symbol=%s removed=%s remain=%d", sym, removed, len(new_bucket))


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
