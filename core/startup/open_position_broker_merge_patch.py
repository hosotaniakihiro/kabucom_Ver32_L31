# ============================================================
# File   : core/startup/open_position_broker_merge_patch.py
# Version: V1.3-BROKER-EMPTY-AUTHORITATIVE-NO-STALE-DB-FALLBACK
# ------------------------------------------------------------
# broker reader の信用実建玉を authoritative source として
# global_data.open_positions / protected / EXIT監視へ渡す runtime patch。
#
# 重要:
#   - 現物はEXIT監視しない。
#   - 実保有していない positions.db の残骸はEXIT監視しない。
#   - broker側の信用建玉が読めた場合は、0件でも broker側を正とする。
#   - broker API失敗時のみ、DB由来の信用らしい建玉を fallback 採用する。
#   - これにより、手動返済済み/証券会社側0件の 9716 などを保有扱いしない。
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_SYNC = None


def _normalize_symbol(v: Any) -> str:
    try:
        if v is None:
            return ""
        s = str(v).strip()
        if s.endswith(".0"):
            s = s[:-2]
        return s
    except Exception:
        return ""


def _text(v: Any) -> str:
    try:
        return str(v or "").strip()
    except Exception:
        return ""


def _is_credit_position(pos: Dict[str, Any], *, source: str = "") -> bool:
    if not isinstance(pos, dict):
        return False

    src = _text(source or pos.get("_position_source")).upper()
    mt = _text(pos.get("margin_trade_type") or pos.get("MarginTradeType"))
    at = _text(pos.get("account_type") or pos.get("AccountType"))
    pr = _text(pos.get("product") or pos.get("Product"))
    side = _text(pos.get("side") or pos.get("Side"))

    joined = f"{src} {mt} {at} {pr} {side}".upper()

    if any(x in joined for x in ("CASH", "現物", "GENBUTSU", "PRODUCT=1")):
        return False

    if "CREDIT_ONLY" in src:
        return True
    if pr == "2":
        return True
    if "信用" in joined or "MARGIN" in joined:
        return True

    # DB由来の margin_trade_type 空は positions.db の残骸/現物/不明として除外する。
    if source.lower().startswith("db") and not mt:
        return False

    return bool(mt)


def _ensure_open_positions() -> Dict[str, Dict[str, Any]]:
    try:
        from global_state import global_data

        d = getattr(global_data, "open_positions", None)
        if isinstance(d, dict):
            return d
        d = {}
        setattr(global_data, "open_positions", d)
        return d
    except Exception:
        logger.debug("[OPEN POSITION BROKER PATCH] ensure open_positions failed", exc_info=True)
        return {}


def _read_broker_positions_with_status() -> Tuple[Dict[str, Dict[str, Any]], bool, dict]:
    """
    Returns:
      positions, read_ok, status

    read_ok=True なら、信用建玉0件でも broker側を正としてDB fallbackしない。
    read_ok=False の場合だけ DB fallback を許可する。
    """
    try:
        import trading.position.kabu_position_reader as reader

        rows = reader.read_kabu_open_positions() or {}
        status = {}
        try:
            status = reader.get_last_read_status() or {}
        except Exception:
            status = {}
        read_ok = bool(status.get("ok", True))

        out: Dict[str, Dict[str, Any]] = {}
        skipped_non_credit = 0
        for k, v in rows.items():
            s = _normalize_symbol(k or (v or {}).get("symbol"))
            if not s or not isinstance(v, dict):
                continue
            if not _is_credit_position(v, source="broker"):
                skipped_non_credit += 1
                continue
            out[s] = v
        if skipped_non_credit:
            logger.warning("[OPEN POSITION BROKER PATCH] broker non-credit skipped=%d", skipped_non_credit)
        return out, read_ok, status
    except Exception as e:
        logger.warning("[OPEN POSITION BROKER PATCH] broker reader failed err=%s", e, exc_info=True)
        return {}, False, {"ok": False, "error": str(e)}


def _filter_db_credit_positions(db_positions: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    skipped = 0
    for k, v in (db_positions or {}).items():
        s = _normalize_symbol(k or (v or {}).get("symbol"))
        if not s or not isinstance(v, dict):
            continue
        if not _is_credit_position(v, source="db"):
            skipped += 1
            continue
        out[s] = v
    if skipped:
        logger.warning("[OPEN POSITION BROKER PATCH] db non-credit/stale skipped=%d", skipped)
    return out


def _merge_and_publish(
    db_positions: Dict[str, Dict[str, Any]],
    broker_positions: Dict[str, Dict[str, Any]],
    *,
    broker_read_ok: bool,
    broker_status: dict | None = None,
) -> Dict[str, Dict[str, Any]]:
    db_credit = _filter_db_credit_positions(db_positions)
    broker_status = broker_status or {}

    # broker APIが正常に読めた場合は、0件でも broker を正とする。
    # これで「証券会社側0件なのにpositions.dbの9716を保有扱いする」事故を防ぐ。
    if broker_read_ok:
        merged = dict(broker_positions)
        source_mode = "broker_credit_authoritative_empty_ok" if not broker_positions else "broker_credit_authoritative"
    else:
        merged = dict(db_credit)
        source_mode = "db_credit_fallback_broker_read_failed"

    gd_positions = _ensure_open_positions()
    before_keys = {_normalize_symbol(k) for k in gd_positions.keys()}
    merged_keys = set(merged.keys())

    # 古いDB/broker由来建玉をいったん消して、今回の信用建玉だけを入れ直す。
    for k in list(gd_positions.keys()):
        try:
            src = str((gd_positions.get(k) or {}).get("_position_source") or "")
            if src.startswith("DB.positions") or src.startswith("KABU.positions"):
                gd_positions.pop(k, None)
        except Exception:
            pass

    for s, pos in merged.items():
        gd_positions[s] = pos

    try:
        from global_state import global_data

        global_data.open_positions_synced_at = dt.datetime.now()
        global_data.open_positions_synced_count = len(merged)
        global_data.open_positions_source_mode = source_mode
        global_data.open_positions_broker_read_ok = bool(broker_read_ok)
        global_data.open_positions_broker_status = broker_status
    except Exception:
        pass

    changed = before_keys != merged_keys
    logger.warning(
        "[OPEN POSITION BROKER PATCH] merged credit open positions count=%d changed=%s mode=%s broker_read_ok=%s broker_status=%s db_count=%d db_credit=%d broker_count=%d symbols=%s",
        len(merged),
        changed,
        source_mode,
        broker_read_ok,
        broker_status,
        len(db_positions or {}),
        len(db_credit),
        len(broker_positions or {}),
        sorted(merged.keys()),
    )
    return merged


def install() -> bool:
    global _INSTALLED, _ORIGINAL_SYNC

    if _INSTALLED:
        return True

    try:
        import trading.position.open_position_sync as target
    except Exception:
        logger.exception("[OPEN POSITION BROKER PATCH] import target failed")
        return False

    original = getattr(target, "sync_open_positions_from_db", None)
    if not callable(original):
        logger.warning("[OPEN POSITION BROKER PATCH] target sync function unavailable")
        return False

    _ORIGINAL_SYNC = original

    def patched_sync_open_positions_from_db(*, force_log: bool = False):
        try:
            db_positions = _ORIGINAL_SYNC(force_log=force_log) or {}
        except Exception:
            logger.exception("[OPEN POSITION BROKER PATCH] original sync failed")
            db_positions = {}

        broker_positions, broker_read_ok, broker_status = _read_broker_positions_with_status()
        return _merge_and_publish(
            db_positions,
            broker_positions,
            broker_read_ok=broker_read_ok,
            broker_status=broker_status,
        )

    target.sync_open_positions_from_db = patched_sync_open_positions_from_db

    try:
        target.load_open_positions_from_broker = lambda: _read_broker_positions_with_status()[0]
    except Exception:
        pass

    _INSTALLED = True
    logger.warning("[OPEN POSITION BROKER PATCH] installed broker_credit_authoritative=True broker_empty_ok=True no_cash_exit=True")
    return True


__all__ = ["install"]
