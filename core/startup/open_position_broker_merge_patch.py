# ============================================================
# File   : core/startup/open_position_broker_merge_patch.py
# Version: V1.7-BROKER-AUTHORITATIVE-CLOSE-STALE-DB
# ------------------------------------------------------------
# broker reader の信用実建玉を authoritative source として
# global_data.open_positions / protected / EXIT監視へ渡す runtime patch。
#
# V1.7:
#   - 株ステーションを正とする。
#   - broker read_ok=True かつ broker信用建玉0件なら、positions.db の古いOPEN残骸を
#     global_data/GCへ戻さない。
#   - さらに DB側の stale OPEN/PENDING 行を CLOSED 扱いへ更新する。
#   - 9716 のような「実際は未保有だがDBにOPENが残る」銘柄を
#     protected / EXIT対象に戻さない。
#
# ENV:
#   OPEN_POSITION_BROKER_AUTHORITATIVE=1              # default
#   OPEN_POSITION_CLOSE_STALE_DB_WHEN_BROKER_EMPTY=1 # default
#   OPEN_POSITION_ALLOW_DB_FALLBACK_ON_PARSE_SUSPICIOUS=0 # default
#   OPEN_POSITION_KEEP_EXISTING_ON_PARSE_SUSPICIOUS=0      # default
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

_INSTALLED = False
_ORIGINAL_SYNC = None

CLOSED_STATUSES = {
    "CLOSED", "CLOSE", "EXITED", "EXIT", "DONE", "CANCELED", "CANCELLED", "REJECTED", "FAILED"
}


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        s = str(v).strip().lower()
        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True
        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False
        return bool(default)
    except Exception:
        return bool(default)


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


def _safe_int(v: Any, default: int = 0) -> int:
    try:
        if v is None or v == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


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


def _existing_credit_positions() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    try:
        gd_positions = _ensure_open_positions()
        for k, v in (gd_positions or {}).items():
            s = _normalize_symbol(k or (v or {}).get("symbol"))
            if not s or not isinstance(v, dict):
                continue
            if not _is_credit_position(v, source="existing"):
                continue
            out[s] = v
    except Exception:
        pass
    return out


def _read_broker_positions_with_status() -> Tuple[Dict[str, Dict[str, Any]], bool, dict]:
    try:
        import trading.position.kabu_position_reader as reader
        rows = reader.read_kabu_open_positions() or {}
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


def _broker_parse_suspicious(*, broker_positions: Dict[str, Dict[str, Any]], broker_read_ok: bool, broker_status: dict) -> bool:
    if not broker_read_ok:
        return False
    if broker_positions:
        return False
    raw_count = _safe_int(broker_status.get("raw_count"), 0)
    credit_candidates = _safe_int(broker_status.get("credit_candidate_count"), 0)
    skipped_qty = _safe_int(broker_status.get("skipped_qty"), 0)
    skipped_price = _safe_int(broker_status.get("skipped_price"), 0)
    credit_open = _safe_int(broker_status.get("credit_open_count"), 0)
    if raw_count > 0 and credit_candidates > 0 and credit_open == 0 and (skipped_qty + skipped_price) >= credit_candidates:
        return True
    if raw_count > 0 and credit_candidates == 0 and skipped_qty >= raw_count:
        return True
    return False


def _clear_open_positions_dict(gd_positions: Dict[str, Dict[str, Any]]) -> None:
    for k in list(gd_positions.keys()):
        try:
            src = str((gd_positions.get(k) or {}).get("_position_source") or "")
            if (not src) or src.startswith("DB.positions") or src.startswith("KABU.positions") or src.startswith("BROKER") or src.startswith("EXISTING"):
                gd_positions.pop(k, None)
        except Exception:
            pass


def _close_stale_db_positions(*, stale_symbols: set[str], reason: str) -> int:
    if not stale_symbols:
        return 0
    if not _env_bool("OPEN_POSITION_CLOSE_STALE_DB_WHEN_BROKER_EMPTY", True):
        return 0
    try:
        from database import Session_position
        from database.models import Position
    except Exception:
        logger.exception("[OPEN POSITION BROKER PATCH] stale DB close import failed")
        return 0

    session = None
    changed = 0
    now = dt.datetime.now()
    try:
        session = Session_position()
        rows = session.query(Position).all()
        for p in rows or []:
            s = _normalize_symbol(getattr(p, "symbol", ""))
            if not s or s not in stale_symbols:
                continue
            st = _text(getattr(p, "status", "OPEN")).upper()
            if st in CLOSED_STATUSES:
                continue
            try:
                setattr(p, "status", "CLOSED")
            except Exception:
                pass
            for attr, value in (
                ("exit_reason", reason),
                ("close_reason", reason),
                ("updated_at", now),
                ("exit_time", now),
                ("closed_time", now),
                ("close_time", now),
            ):
                try:
                    if hasattr(p, attr):
                        setattr(p, attr, value)
                except Exception:
                    pass
            changed += 1
        if changed:
            session.commit()
            logger.warning(
                "[OPEN POSITION BROKER PATCH] closed stale DB positions by broker sync count=%d symbols=%s reason=%s",
                changed,
                sorted(stale_symbols),
                reason,
            )
        else:
            session.rollback()
    except Exception:
        try:
            if session is not None:
                session.rollback()
        except Exception:
            pass
        logger.exception("[OPEN POSITION BROKER PATCH] close stale DB positions failed symbols=%s", sorted(stale_symbols))
        return 0
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:
            pass
    return changed


def _merge_and_publish(db_positions: Dict[str, Dict[str, Any]], broker_positions: Dict[str, Dict[str, Any]], *, broker_read_ok: bool, broker_status: dict | None = None) -> Dict[str, Dict[str, Any]]:
    db_credit = _filter_db_credit_positions(db_positions)
    existing_credit = _existing_credit_positions()
    broker_status = broker_status or {}
    parse_suspicious = _broker_parse_suspicious(broker_positions=broker_positions, broker_read_ok=broker_read_ok, broker_status=broker_status)

    broker_authoritative = _env_bool("OPEN_POSITION_BROKER_AUTHORITATIVE", True)
    allow_parse_suspicious_db_fallback = _env_bool("OPEN_POSITION_ALLOW_DB_FALLBACK_ON_PARSE_SUSPICIOUS", False)
    keep_existing_on_parse_suspicious = _env_bool("OPEN_POSITION_KEEP_EXISTING_ON_PARSE_SUSPICIOUS", False)

    stale_symbols: set[str] = set()

    if broker_read_ok and broker_authoritative:
        if broker_positions:
            merged = dict(broker_positions)
            source_mode = "broker_credit_authoritative"
            stale_symbols = set(db_credit.keys()) - set(broker_positions.keys())
        else:
            # 株ステーションが読めていて信用建玉0件なら、DB/既存を正に戻さない。
            merged = {}
            source_mode = "broker_credit_authoritative_empty_parse_suspicious_cleared" if parse_suspicious else "broker_credit_authoritative_empty_ok"
            stale_symbols = set(db_credit.keys()) | set(existing_credit.keys())
    elif broker_read_ok:
        if broker_positions:
            merged = dict(broker_positions)
            source_mode = "broker_credit_authoritative"
            stale_symbols = set(db_credit.keys()) - set(broker_positions.keys())
        elif parse_suspicious and allow_parse_suspicious_db_fallback and db_credit:
            merged = dict(db_credit)
            source_mode = "db_credit_fallback_broker_parse_suspicious"
        elif parse_suspicious and keep_existing_on_parse_suspicious and existing_credit:
            merged = dict(existing_credit)
            source_mode = "existing_credit_kept_broker_parse_suspicious"
        else:
            merged = {}
            source_mode = "broker_credit_authoritative_empty_parse_suspicious_no_fallback" if parse_suspicious else "broker_credit_authoritative_empty_ok"
            stale_symbols = set(db_credit.keys()) | set(existing_credit.keys())
    else:
        merged = dict(db_credit or existing_credit)
        source_mode = "db_credit_fallback_broker_read_failed"

    if stale_symbols and broker_read_ok:
        _close_stale_db_positions(stale_symbols=stale_symbols, reason=f"BROKER_SYNC_NO_POSITION:{source_mode}")

    gd_positions = _ensure_open_positions()
    before_keys = {_normalize_symbol(k) for k in gd_positions.keys()}
    merged_keys = set(merged.keys())
    _clear_open_positions_dict(gd_positions)

    for s, pos in merged.items():
        try:
            if isinstance(pos, dict):
                if source_mode.startswith("db_"):
                    pos.setdefault("_position_source", "DB.positions")
                elif source_mode.startswith("existing_"):
                    pos.setdefault("_position_source", "EXISTING.positions.parse_suspicious")
                else:
                    pos.setdefault("_position_source", "KABU.positions")
        except Exception:
            pass
        gd_positions[s] = pos

    try:
        from global_state import global_data
        global_data.open_positions_synced_at = dt.datetime.now()
        global_data.open_positions_synced_count = len(merged)
        global_data.open_positions_source_mode = source_mode
        global_data.open_positions_broker_read_ok = bool(broker_read_ok)
        global_data.open_positions_broker_parse_suspicious = bool(parse_suspicious)
        global_data.open_positions_broker_status = broker_status
        global_data.open_positions_db_fallback_on_parse_suspicious = bool(allow_parse_suspicious_db_fallback)
        global_data.open_positions_keep_existing_on_parse_suspicious = bool(keep_existing_on_parse_suspicious)
        global_data.open_positions_broker_authoritative = bool(broker_authoritative)
        global_data.open_positions_stale_db_symbols_closed = sorted(stale_symbols)
    except Exception:
        pass

    changed = before_keys != merged_keys
    logger.warning(
        "[OPEN POSITION BROKER PATCH] merged credit open positions count=%d changed=%s mode=%s broker_read_ok=%s parse_suspicious=%s broker_authoritative=%s db_fallback_on_parse_suspicious=%s keep_existing_on_parse_suspicious=%s stale_closed=%s broker_status=%s db_count=%d db_credit=%d existing_credit=%d broker_count=%d symbols=%s",
        len(merged), changed, source_mode, broker_read_ok, parse_suspicious,
        broker_authoritative, allow_parse_suspicious_db_fallback, keep_existing_on_parse_suspicious,
        sorted(stale_symbols), broker_status, len(db_positions or {}), len(db_credit), len(existing_credit), len(broker_positions or {}), sorted(merged.keys()),
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
        return _merge_and_publish(db_positions, broker_positions, broker_read_ok=broker_read_ok, broker_status=broker_status)

    target.sync_open_positions_from_db = patched_sync_open_positions_from_db
    try:
        target.load_open_positions_from_broker = lambda: _read_broker_positions_with_status()[0]
    except Exception:
        pass

    os.environ.setdefault("OPEN_POSITION_BROKER_AUTHORITATIVE", "1")
    os.environ.setdefault("OPEN_POSITION_CLOSE_STALE_DB_WHEN_BROKER_EMPTY", "1")
    os.environ.setdefault("OPEN_POSITION_ALLOW_DB_FALLBACK_ON_PARSE_SUSPICIOUS", "0")
    os.environ.setdefault("OPEN_POSITION_KEEP_EXISTING_ON_PARSE_SUSPICIOUS", "0")

    _INSTALLED = True
    logger.warning(
        "[OPEN POSITION BROKER PATCH] installed v1.7 broker_authoritative=%s close_stale_db=%s parse_suspicious_db_fallback=%s keep_existing=%s no_cash_exit=True",
        os.environ.get("OPEN_POSITION_BROKER_AUTHORITATIVE"),
        os.environ.get("OPEN_POSITION_CLOSE_STALE_DB_WHEN_BROKER_EMPTY"),
        os.environ.get("OPEN_POSITION_ALLOW_DB_FALLBACK_ON_PARSE_SUSPICIOUS"),
        os.environ.get("OPEN_POSITION_KEEP_EXISTING_ON_PARSE_SUSPICIOUS"),
    )
    return True


try:
    install()
except Exception:
    logger.exception("[OPEN POSITION BROKER PATCH] auto install failed")


__all__ = ["install"]
