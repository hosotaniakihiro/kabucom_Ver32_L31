# ============================================================
# File   : core/startup/ranking_entry_flat_price_guard_patch.py
# Version: V1.3-SILENT-LAST-CHANCE-VOLUME-NORMALIZE
# ------------------------------------------------------------
# 目的:
#   1) ランキング由来ENTRYで、価格横ばいだけで大量DROPされる問題を緩和する。
#   2) ranking_entry が global_data のランキングDFだけを見て no_ranking_df で止まる問題を、
#      ats.ats_ranking.db_path.get_usable_ranking_db_path() からのfallbackで補正する。
#   3) last-chance volume normalize の銘柄別ログを既定OFFにして、ログ大量出力と処理遅延を抑える。
#
# V1.3:
#   - [RANKING FLAT PRICE PATCH] last-chance volume normalize を既定では出さない
#   - 同一(symbol, price, volume, turnover)の補正結果をキャッシュ
#   - 詳細確認時のみ以下でログ出力
#       RANKING_FLAT_PRICE_PATCH_LOG_NORMALIZE=1
#       RANKING_FLAT_PRICE_PATCH_LOG_FIRST_N=30
#
# V1.2:
#   - 存在しない resolve_ranking_db_path import を廃止
#   - 実在API get_usable_ranking_db_path(force_refresh=True, allow_fallback=False,
#     prefer_today_even_if_empty=True) を使用
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Dict, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

_PATCHED = False
_ORIGINAL_FILTER = None
_ORIGINAL_GET_RANKING_SOURCE_DF = None

_NORMALIZE_CACHE: dict[tuple[str, float, float, float], dict[str, Any]] = {}
_LOGGED_NORMALIZE_KEYS: set[tuple[str, float, float, float]] = set()
_LOG_NORMALIZE_COUNT = 0


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        x = float(str(v).replace(",", ""))
        return float(default) if x != x else x
    except Exception:
        return float(default)


def _i(v: Any, default: int = 999999) -> int:
    try:
        return int(float(v))
    except Exception:
        return int(default)


def _get_cfg() -> dict:
    try:
        from config.ranking_entry_config import RANKING_ENTRY_CONFIG
        return RANKING_ENTRY_CONFIG
    except Exception:
        return {}


def _normalize_key(row: Dict[str, Any], price: float, volume: float, turnover: float) -> tuple[str, float, float, float]:
    return (
        str(row.get("symbol") or "").strip(),
        round(float(price), 4),
        round(float(volume), 4),
        round(float(turnover), 4),
    )


def _should_log_normalize(key: tuple[str, float, float, float]) -> bool:
    global _LOG_NORMALIZE_COUNT
    if _env_bool("RANKING_FLAT_PRICE_PATCH_LOG_NORMALIZE", False):
        return True
    limit = _env_int("RANKING_FLAT_PRICE_PATCH_LOG_FIRST_N", 0)
    if limit <= 0:
        return False
    if key in _LOGGED_NORMALIZE_KEYS:
        return False
    _LOGGED_NORMALIZE_KEYS.add(key)
    _LOG_NORMALIZE_COUNT += 1
    return _LOG_NORMALIZE_COUNT <= limit


def _repair_volume_units(row: Dict[str, Any]) -> Dict[str, Any]:
    cfg = _get_cfg()
    vol_cfg = cfg.get("VOLUME", {}) if isinstance(cfg, dict) else {}
    min_volume = _f(vol_cfg.get("MIN_VOLUME", 30000), 30000)
    min_turnover = _f(vol_cfg.get("MIN_TURNOVER", 10000000), 10000000)
    mul = _env_float("RANKING_ENTRY_LAST_CHANCE_VOLUME_MULTIPLIER", 1000.0)

    price = _f(row.get("price") or row.get("current_price") or row.get("close"), 0.0)
    volume = _f(row.get("volume") or row.get("trading_volume"), 0.0)
    turnover = _f(row.get("turnover") or row.get("trading_value"), 0.0)
    raw_v = volume
    raw_t = turnover
    key = _normalize_key(row, price, raw_v, raw_t)

    cached = _NORMALIZE_CACHE.get(key)
    if cached is not None:
        row.update(cached)
        return row

    if 0 < volume < min_volume and mul > 1:
        volume = volume * mul
        row["volume"] = volume
        row["trading_volume"] = volume
        row["ranking_last_chance_volume_fixed"] = True
        row["ranking_last_chance_volume_raw"] = raw_v

    implied = price * volume if price > 0 and volume > 0 else 0.0
    if implied > turnover:
        turnover = implied
    if 0 < turnover < min_turnover and price > 0 and volume > 0:
        turnover = max(turnover, implied)
    if turnover > 0:
        row["turnover"] = turnover
        row["trading_value"] = turnover

    updates = {
        "volume": row.get("volume", volume),
        "trading_volume": row.get("trading_volume", volume),
        "turnover": row.get("turnover", turnover),
        "trading_value": row.get("trading_value", turnover),
    }
    if row.get("ranking_last_chance_volume_fixed"):
        updates["ranking_last_chance_volume_fixed"] = True
        updates["ranking_last_chance_volume_raw"] = raw_v

    if len(_NORMALIZE_CACHE) >= _env_int("RANKING_FLAT_PRICE_PATCH_CACHE_MAX", 20000):
        _NORMALIZE_CACHE.clear()
        _LOGGED_NORMALIZE_KEYS.clear()
    _NORMALIZE_CACHE[key] = dict(updates)

    if (raw_v != volume or raw_t != turnover) and _should_log_normalize(key):
        logger.info(
            "[RANKING FLAT PRICE PATCH] last-chance volume normalize symbol=%s price=%s volume %s->%s turnover %s->%s min_volume=%s",
            row.get("symbol"), price, raw_v, volume, raw_t, turnover, min_volume,
        )
    return row


def _flat_price_allowed(row: Dict[str, Any], prev_h: Dict[str, Any], reason: str) -> bool:
    if not _env_bool("RANKING_ENTRY_ALLOW_FLAT_PRICE_IF_RANK_STRONG", True):
        return False
    if not (str(reason).startswith("BUY_PRICE_NOT_UP") or str(reason).startswith("SELL_PRICE_NOT_DOWN")):
        return False

    cfg = _get_cfg()
    rank_cfg = cfg.get("RANKING", {}) if isinstance(cfg, dict) else {}
    max_rank = _i(rank_cfg.get("FLAT_PRICE_ALLOW_MAX_RANK", 12), 12)
    rank = _i(row.get("rank_position"), 999999)
    prev_rank = _i(prev_h.get("last_rank_position"), 999999)
    consecutive = _i(prev_h.get("consecutive"), 0) + 1 if prev_h else 1
    min_consecutive = _i(rank_cfg.get("MIN_CONSECUTIVE_APPEAR", 2), 2)

    rank_not_worse = prev_rank < 999999 and rank <= prev_rank
    rank_top = rank <= max_rank
    return consecutive >= min_consecutive and (rank_not_worse or rank_top)


def _patched_filter(row: Dict[str, Any], side: str, prev_h: Dict[str, Any], score: float, parts: Dict[str, float]) -> Tuple[bool, str]:
    if callable(_ORIGINAL_FILTER):
        row = _repair_volume_units(row)
        ok, reason = _ORIGINAL_FILTER(row, side, prev_h, score, parts)
        if ok:
            return ok, reason

        if _flat_price_allowed(row, prev_h, reason):
            price = _f(row.get("price") or row.get("current_price"), 0.0)
            patched_prev = dict(prev_h or {})
            if price > 0:
                if str(side).upper() == "BUY":
                    patched_prev["last_price"] = price * 0.999999
                else:
                    patched_prev["last_price"] = price * 1.000001
            ok2, reason2 = _ORIGINAL_FILTER(row, side, patched_prev, score, parts)
            if ok2:
                if _env_bool("RANKING_FLAT_PRICE_PATCH_LOG_PASS", False):
                    logger.info(
                        "[RANKING FLAT PRICE PATCH] pass flat price symbol=%s side=%s rank=%s prev_rank=%s reason=%s",
                        row.get("symbol"), side, row.get("rank_position"), prev_h.get("last_rank_position"), reason,
                    )
                return True, "OK_FLAT_PRICE_RANK_STRONG"
            return False, reason2
        return ok, reason
    return False, "ORIGINAL_FILTER_NOT_AVAILABLE"


def _resolve_ranking_db_path() -> str:
    try:
        from ats.ats_ranking.db_path import get_usable_ranking_db_path
        p = get_usable_ranking_db_path(
            force_refresh=True,
            allow_fallback=False,
            prefer_today_even_if_empty=True,
        )
        return str(p or "")
    except Exception:
        logger.warning("[RANKING DB FALLBACK PATCH] get_usable_ranking_db_path failed", exc_info=True)
        return ""


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None
    except Exception:
        return False


def _cols(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    except Exception:
        return []


def _read_latest_ranking_snapshot_from_db() -> pd.DataFrame:
    if not _env_bool("RANKING_ENTRY_DB_FALLBACK_ENABLED", True):
        return pd.DataFrame()

    db_path = _resolve_ranking_db_path()
    if not db_path or not os.path.exists(db_path):
        logger.warning("[RANKING DB FALLBACK PATCH] db not found path=%s", db_path)
        return pd.DataFrame()

    table = "ranking_snapshot_1min"
    max_rows = max(100, _env_int("RANKING_ENTRY_DB_FALLBACK_MAX_ROWS", 3000))
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=_env_float("RANKING_ENTRY_DB_FALLBACK_TIMEOUT_SEC", 2.0))
        conn.execute("PRAGMA query_only=ON")
        if not _table_exists(conn, table):
            logger.warning("[RANKING DB FALLBACK PATCH] table missing table=%s path=%s", table, db_path)
            return pd.DataFrame()
        cols = _cols(conn, table)
        dt_col = next((c for c in ("datetime", "snapshot_time", "time", "created_at") if c in cols), None)
        if dt_col:
            latest = conn.execute(f"SELECT MAX({dt_col}) FROM {table}").fetchone()
            latest_dt = latest[0] if latest else None
            if latest_dt is not None and str(latest_dt).strip() != "":
                df = pd.read_sql_query(f"SELECT * FROM {table} WHERE {dt_col}=? LIMIT ?", conn, params=(latest_dt, max_rows))
                logger.warning("[RANKING DB FALLBACK PATCH] loaded latest snapshot rows=%s dt_col=%s latest=%s", len(df), dt_col, latest_dt)
                return df
        df = pd.read_sql_query(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", conn, params=(max_rows,))
        logger.warning("[RANKING DB FALLBACK PATCH] loaded by rowid rows=%s", len(df))
        return df
    except Exception as e:
        logger.warning("[RANKING DB FALLBACK PATCH] read failed err=%s", e, exc_info=False)
        return pd.DataFrame()
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _patched_get_ranking_source_df():
    if callable(_ORIGINAL_GET_RANKING_SOURCE_DF):
        try:
            df = _ORIGINAL_GET_RANKING_SOURCE_DF()
            if isinstance(df, pd.DataFrame) and not df.empty:
                return df
        except Exception:
            logger.warning("[RANKING DB FALLBACK PATCH] original getter failed", exc_info=True)

    df = _read_latest_ranking_snapshot_from_db()
    if df is None or df.empty:
        logger.warning("[RANKING DB FALLBACK PATCH] fallback empty")
        return None

    try:
        from global_state import global_data
        setattr(global_data, "latest_ranking_df", df.copy())
        setattr(global_data, "latest_ranking_snapshot", df.to_dict("records"))
    except Exception:
        logger.debug("[RANKING DB FALLBACK PATCH] global_data publish failed", exc_info=True)

    logger.warning("[RANKING DB FALLBACK PATCH] source=db rows=%s cols=%s", len(df), len(df.columns))
    return df


def install() -> bool:
    global _PATCHED, _ORIGINAL_FILTER, _ORIGINAL_GET_RANKING_SOURCE_DF
    if _PATCHED:
        return True
    try:
        import trading.ranking.entry_from_ranking as efr
        cur = getattr(efr, "_passes_ranking_only_filters", None)
        if callable(cur) and not getattr(cur, "_ranking_flat_price_patch", False):
            _ORIGINAL_FILTER = cur
            _patched_filter._ranking_flat_price_patch = True  # type: ignore[attr-defined]
            _patched_filter._original = cur  # type: ignore[attr-defined]
            efr._passes_ranking_only_filters = _patched_filter

        cur_getter = getattr(efr, "_get_ranking_source_df", None)
        if callable(cur_getter) and not getattr(cur_getter, "_ranking_db_fallback_patch", False):
            _ORIGINAL_GET_RANKING_SOURCE_DF = cur_getter
            _patched_get_ranking_source_df._ranking_db_fallback_patch = True  # type: ignore[attr-defined]
            _patched_get_ranking_source_df._original = cur_getter  # type: ignore[attr-defined]
            efr._get_ranking_source_df = _patched_get_ranking_source_df

        _PATCHED = True
        logger.warning(
            "[RANKING FLAT PRICE PATCH] installed v1.3 db_fallback=True silent_normalize=%s log_first_n=%s",
            not _env_bool("RANKING_FLAT_PRICE_PATCH_LOG_NORMALIZE", False),
            _env_int("RANKING_FLAT_PRICE_PATCH_LOG_FIRST_N", 0),
        )
        return True
    except Exception:
        logger.exception("[RANKING FLAT PRICE PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING FLAT PRICE PATCH] auto install failed")


__all__ = ["install"]
