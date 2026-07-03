# ============================================================
# File   : core/startup/summary_entry_pending_duplicate_registered_patch.py
# Version: V2-DIRECT-PENDING-REGISTER-NO-BLOCK
# ------------------------------------------------------------
# 目的:
#   SUMMARY AI direct dispatch が approved rows を持っていても、
#   pending_manager.add_pending() の wrapper chain が詰まると、
#   pending登録完了ログまで戻らず発注パイプラインが止まる。
#
# 対策:
#   SUMMARY_ENTRY 用の pending 登録は、この patch 内で pending root へ
#   直接・短時間で登録する。既存 identity は duplicate registered 扱い。
#   BUY/SELL 混在は引き続き reject し、板なし hard block 等の後段安全ガードは維持する。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)
VERSION = "V2-DIRECT-PENDING-REGISTER-NO-BLOCK"

_INSTALLED = False
_ORIG_REGISTER_PENDING_ENTRIES = None
_TRUE_SET = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in _TRUE_SET
    except Exception:
        return bool(default)


def _safe_symbol(row: dict[str, Any]) -> str:
    try:
        s = str(row.get("symbol") or "").strip().upper()
        if s.endswith(".0") and s[:-2].isdigit():
            return s[:-2]
        if s.endswith(".T"):
            return s[:-2]
        if not s or s in {"NONE", "NULL", "NAN", "NA", "-", "0"}:
            return ""
        return s
    except Exception:
        return ""


def _norm(v: Any) -> str:
    try:
        if v is None:
            return ""
        return str(v).strip().upper()
    except Exception:
        return ""


def _norm_interval(v: Any) -> str:
    try:
        if v is None or str(v).strip() == "":
            return ""
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _norm_side(v: Any) -> str:
    s = _norm(v)
    if s in {"BUY", "LONG", "2", "買", "買い"}:
        return "BUY"
    if s in {"SELL", "SHORT", "1", "売", "売り"}:
        return "SELL"
    return s


def _identity(entry: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        _norm(entry.get("source")),
        _norm(entry.get("entry_type")),
        _norm_side(entry.get("side") or entry.get("entry_decision") or entry.get("ai_side")),
        _norm_interval(entry.get("interval")),
    )


def _bucket_sides(bucket: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for e in bucket:
        if not isinstance(e, dict):
            continue
        s = _norm_side(e.get("side") or e.get("entry_decision") or e.get("ai_side"))
        if s in {"BUY", "SELL"}:
            out.add(s)
    return out


def _ensure_root() -> dict[str, list[dict[str, Any]]]:
    from global_state import global_data
    root = getattr(global_data, "pending_entries", None)
    if not isinstance(root, dict):
        root = {}
        global_data.pending_entries = root
    return root


def _normalize_bucket(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if isinstance(raw, dict):
        return [raw]
    return []


def _protect_symbol(symbol: str, *, source: Any = None, side: Any = None) -> None:
    try:
        if not _env_bool("PENDING_PROTECT_PUSH_SYMBOLS", True):
            return
        from global_state import global_data
        max_keep = int(float(os.getenv("PENDING_PROTECT_PUSH_MAX_KEEP", "50") or "50"))
        for attr in ("recent_entry_symbols", "last_entry_candidates", "recent_ai_ok_symbols"):
            cur = getattr(global_data, attr, None)
            if cur is None:
                cur_list: list[Any] = []
            elif isinstance(cur, (list, tuple, set)):
                cur_list = list(cur)
            elif isinstance(cur, dict):
                cur_list = list(cur.keys())
            else:
                cur_list = [cur]
            seen: set[str] = set()
            out: list[str] = []
            for x in [symbol] + cur_list:
                s = str(x or "").strip().upper()
                if s and s not in seen:
                    seen.add(s)
                    out.append(s)
            if max_keep > 0:
                out = out[:max_keep]
            setattr(global_data, attr, out)
        logger.warning("[SUMMARY ENTRY DUP REGISTER PATCH] protected pending symbol=%s source=%s side=%s", symbol, source, side)
    except Exception:
        logger.debug("[SUMMARY ENTRY DUP REGISTER PATCH] protect symbol failed symbol=%s", symbol, exc_info=True)


def _direct_add_pending(entry: dict[str, Any]) -> tuple[bool, bool, str]:
    """Returns (registered, duplicate_existing, reason)."""
    root = _ensure_root()
    symbol = _safe_symbol(entry)
    if not symbol:
        return False, False, "no_symbol"

    bucket = _normalize_bucket(root.get(symbol))
    new_side = _norm_side(entry.get("side") or entry.get("entry_decision") or entry.get("ai_side"))
    existing_sides = _bucket_sides(bucket)
    if new_side in {"BUY", "SELL"} and existing_sides and new_side not in existing_sides:
        return False, False, f"opposite_side existing={sorted(existing_sides)} new={new_side}"

    new_identity = _identity(entry)
    for old in bucket:
        if _identity(old) == new_identity:
            # Refresh stale-looking duplicates so the controller sees the latest row.
            old.update({k: v for k, v in entry.items() if v is not None})
            old["updated_at"] = dt.datetime.now()
            root[symbol] = bucket
            _protect_symbol(symbol, source=entry.get("source"), side=new_side)
            return True, True, "duplicate_identity_refreshed"

    now = dt.datetime.now()
    new_entry = dict(entry)
    new_entry.setdefault("created_at", now)
    new_entry["updated_at"] = now
    bucket.append(new_entry)
    root[symbol] = bucket
    _protect_symbol(symbol, source=entry.get("source"), side=new_side)
    logger.info(
        "🧩 pending direct added symbol=%s source=%s side=%s interval=%s entry_type=%s score=%s bucket_size=%d identity=%s",
        symbol,
        entry.get("source"),
        new_side,
        entry.get("interval"),
        entry.get("entry_type"),
        entry.get("score"),
        len(bucket),
        new_identity,
    )
    return True, False, "direct_added"


def _snapshot_root_safe() -> dict[str, int]:
    try:
        root = _ensure_root()
        return {str(k): len(_normalize_bucket(v)) for k, v in list(root.items()) if _normalize_bucket(v)}
    except Exception:
        return {}


def _patched_register_pending_entries(entries):
    try:
        import trading.summary.summary_entry as se

        registered = 0
        rejected = 0
        duplicate_existing = 0

        if not entries:
            logger.info("[SUMMARY ENTRY DUP REGISTER PATCH] skipped reason=no_entries version=%s", VERSION)
            return 0

        for entry in list(entries or []):
            try:
                if not isinstance(entry, dict):
                    rejected += 1
                    continue

                symbol = _safe_symbol(entry)
                if not symbol:
                    rejected += 1
                    logger.warning("[SUMMARY ENTRY DUP REGISTER PATCH] skip reason=no_symbol entry=%s version=%s", entry, VERSION)
                    continue

                entry["symbol"] = symbol
                entry["entry_type"] = entry.get("entry_type") or getattr(se, "DEFAULT_ENTRY_TYPE", "SUMMARY_AI")
                entry["source"] = entry.get("source") or getattr(se, "DEFAULT_SOURCE", "SUMMARY")

                try:
                    side = se._normalize_side(entry)
                except Exception:
                    side = _norm_side(entry.get("side") or entry.get("entry_decision") or entry.get("ai_side") or "BUY")
                entry["side"] = side
                entry["entry_decision"] = side

                try:
                    entry["interval"] = se._safe_interval(entry.get("interval"))
                except Exception:
                    pass

                logger.info(
                    "[SUMMARY ENTRY DUP REGISTER PATCH] pending add request symbol=%s side=%s entry_type=%s source=%s interval=%s version=%s",
                    entry.get("symbol"), entry.get("side"), entry.get("entry_type"), entry.get("source"), entry.get("interval"), VERSION,
                )

                ok, dup, reason = _direct_add_pending(entry)
                if ok:
                    registered += 1
                    if dup:
                        duplicate_existing += 1
                    logger.info("[SUMMARY ENTRY DUP REGISTER PATCH] pending registered symbol=%s side=%s reason=%s version=%s", symbol, side, reason, VERSION)
                    continue

                rejected += 1
                logger.warning(
                    "[SUMMARY ENTRY DUP REGISTER PATCH] pending rejected symbol=%s side=%s reason=%s root=%s version=%s",
                    symbol, side, reason, _snapshot_root_safe(), VERSION,
                )
            except Exception:
                rejected += 1
                logger.exception("[SUMMARY ENTRY DUP REGISTER PATCH] pending add failed entry=%s version=%s", entry, VERSION)

        logger.warning(
            "[SUMMARY ENTRY DUP REGISTER PATCH] pending registration done entries=%s registered=%s duplicate_existing=%s rejected=%s root=%s version=%s",
            len(entries or []), registered, duplicate_existing, rejected, _snapshot_root_safe(), VERSION,
        )
        return registered
    except Exception:
        logger.exception("[SUMMARY ENTRY DUP REGISTER PATCH] patched register failed version=%s", VERSION)
        if callable(_ORIG_REGISTER_PENDING_ENTRIES):
            return _ORIG_REGISTER_PENDING_ENTRIES(entries)
        return 0


def install() -> bool:
    global _INSTALLED, _ORIG_REGISTER_PENDING_ENTRIES
    if _INSTALLED:
        return True
    try:
        import trading.summary.summary_entry as se
        cur = getattr(se, "register_pending_entries", None)
        if not callable(cur):
            logger.warning("[SUMMARY ENTRY DUP REGISTER PATCH] target missing version=%s", VERSION)
            return False
        if getattr(cur, "_summary_entry_dup_registered_v2", False):
            _INSTALLED = True
            return True
        _ORIG_REGISTER_PENDING_ENTRIES = getattr(cur, "_original", cur)
        _patched_register_pending_entries._summary_entry_dup_registered_v1 = True  # type: ignore[attr-defined]
        _patched_register_pending_entries._summary_entry_dup_registered_v2 = True  # type: ignore[attr-defined]
        _patched_register_pending_entries._original = _ORIG_REGISTER_PENDING_ENTRIES  # type: ignore[attr-defined]
        se.register_pending_entries = _patched_register_pending_entries
        _INSTALLED = True
        logger.warning("[SUMMARY ENTRY DUP REGISTER PATCH] installed version=%s direct_pending_register=True", VERSION)
        return True
    except Exception:
        logger.exception("[SUMMARY ENTRY DUP REGISTER PATCH] install failed version=%s", VERSION)
        return False


try:
    install()
except Exception:
    logger.exception("[SUMMARY ENTRY DUP REGISTER PATCH] auto install failed version=%s", VERSION)


__all__ = ["install", "VERSION"]
