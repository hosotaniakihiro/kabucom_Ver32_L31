# ============================================================
# File   : core/startup/nas_sqlite_io_guard_patch.py
# Version: V1.0-NAS-SQLITE-IO-GUARD
# ------------------------------------------------------------
# 目的:
#   NAS/SMB 上の sqlite DB が一時的に open 不能 / disk I/O error になると、
#   ranking selector / symbol_flags filter が同じDBを短時間に何度も開き、
#   PUSH購読更新やschedulerを重くする。
#
# 対策:
#   - sqlite OperationalError / disk I/O error を短時間ネガティブキャッシュ
#   - キャッシュ中は同じDBを再openせず空DF/None扱いで即fallback
#   - symbol_flags はI/O失敗時に pass-through し、購読更新自体は止めない
# ============================================================

from __future__ import annotations

import logging
import os
import sqlite3
import time
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)
_INSTALLED = False
_BAD_UNTIL: dict[str, float] = {}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(v)
    except Exception:
        return float(default)


def _norm_path(path: Any) -> str:
    try:
        return str(path or "").replace("/", "\\").lower()
    except Exception:
        return str(path or "")


def _cooldown_sec() -> float:
    return max(5.0, _env_float("NAS_SQLITE_IO_GUARD_COOLDOWN_SEC", 20.0))


def _is_bad(path: Any) -> bool:
    key = _norm_path(path)
    until = float(_BAD_UNTIL.get(key, 0.0) or 0.0)
    return until > time.time()


def _mark_bad(path: Any, err: BaseException | None = None) -> None:
    key = _norm_path(path)
    until = time.time() + _cooldown_sec()
    _BAD_UNTIL[key] = until
    logger.warning("[NAS SQLITE IO GUARD] mark bad path=%s cooldown=%.1fs err=%s", path, _cooldown_sec(), err)


def _looks_io_error(err: BaseException) -> bool:
    s = str(err).lower()
    return (
        isinstance(err, sqlite3.OperationalError)
        and (
            "unable to open database file" in s
            or "disk i/o error" in s
            or "database is locked" in s
            or "interrupted" in s
        )
    ) or "disk i/o error" in s or "unable to open database file" in s


def _patch_ranking_selector() -> bool:
    try:
        import trading.push.subscription_manager.ranking_selector as rs
        old_load = getattr(rs, "_load_ranking_df", None)
        if not callable(old_load) or getattr(old_load, "_nas_io_guard_v1", False):
            return True

        def _load_ranking_df_guarded(*args, **kwargs):
            path = None
            try:
                path = kwargs.get("db_path")
                if path is None:
                    path = rs._resolve_ranking_db_path(db_path=None, yyyymmdd=kwargs.get("yyyymmdd"))
                if path and _is_bad(path):
                    logger.warning("[NAS SQLITE IO GUARD] skip ranking_selector bad path=%s", path)
                    return pd.DataFrame()
                return old_load(*args, **kwargs)
            except Exception as e:
                if path and _looks_io_error(e):
                    _mark_bad(path, e)
                    return pd.DataFrame()
                raise

        _load_ranking_df_guarded._nas_io_guard_v1 = True  # type: ignore[attr-defined]
        _load_ranking_df_guarded._original = old_load  # type: ignore[attr-defined]
        rs._load_ranking_df = _load_ranking_df_guarded
        logger.warning("[NAS SQLITE IO GUARD] patched ranking_selector._load_ranking_df")
        return True
    except Exception:
        logger.exception("[NAS SQLITE IO GUARD] patch ranking_selector failed")
        return False


def _patch_ranking_source_selector() -> bool:
    try:
        import trading.push.subscription_manager.ranking_source_selector as rss
        old_read = getattr(rss, "read_ranking_df_from_db", None)
        if not callable(old_read) or getattr(old_read, "_nas_io_guard_v1", False):
            return True

        def _read_ranking_df_from_db_guarded(path: str, *args, **kwargs):
            if path and _is_bad(path):
                logger.warning("[NAS SQLITE IO GUARD] skip ranking_source_selector bad path=%s", path)
                return pd.DataFrame(), []
            try:
                df, tables = old_read(path, *args, **kwargs)
                return df, tables
            except Exception as e:
                if _looks_io_error(e):
                    _mark_bad(path, e)
                    return pd.DataFrame(), []
                raise

        _read_ranking_df_from_db_guarded._nas_io_guard_v1 = True  # type: ignore[attr-defined]
        _read_ranking_df_from_db_guarded._original = old_read  # type: ignore[attr-defined]
        rss.read_ranking_df_from_db = _read_ranking_df_from_db_guarded
        logger.warning("[NAS SQLITE IO GUARD] patched ranking_source_selector.read_ranking_df_from_db")
        return True
    except Exception:
        logger.exception("[NAS SQLITE IO GUARD] patch ranking_source_selector failed")
        return False


def _patch_symbol_flags_filters() -> bool:
    try:
        import trading.push.subscription_manager.filters as flt
        old_targets = getattr(flt, "read_symbol_flags_target_sets", None)
        old_keep = getattr(flt, "read_symbol_flags_keep_set", None)
        ok = True

        if callable(old_targets) and not getattr(old_targets, "_nas_io_guard_v1", False):
            def _read_symbol_flags_target_sets_guarded(db_path=None, *args, **kwargs):
                path = db_path or getattr(flt, "SYMBOL_FLAGS_DB_PATH", "")
                if path and _is_bad(path):
                    logger.warning("[NAS SQLITE IO GUARD] skip symbol_flags target bad path=%s", path)
                    return set(), set()
                try:
                    return old_targets(db_path=db_path, *args, **kwargs)
                except Exception as e:
                    if _looks_io_error(e):
                        _mark_bad(path, e)
                        return set(), set()
                    raise

            _read_symbol_flags_target_sets_guarded._nas_io_guard_v1 = True  # type: ignore[attr-defined]
            _read_symbol_flags_target_sets_guarded._original = old_targets  # type: ignore[attr-defined]
            flt.read_symbol_flags_target_sets = _read_symbol_flags_target_sets_guarded

        if callable(old_keep) and not getattr(old_keep, "_nas_io_guard_v1", False):
            def _read_symbol_flags_keep_set_guarded(db_path: str, *args, **kwargs):
                if db_path and _is_bad(db_path):
                    logger.warning("[NAS SQLITE IO GUARD] skip symbol_flags keep bad path=%s", db_path)
                    return set()
                try:
                    return old_keep(db_path, *args, **kwargs)
                except Exception as e:
                    if _looks_io_error(e):
                        _mark_bad(db_path, e)
                        return set()
                    raise

            _read_symbol_flags_keep_set_guarded._nas_io_guard_v1 = True  # type: ignore[attr-defined]
            _read_symbol_flags_keep_set_guarded._original = old_keep  # type: ignore[attr-defined]
            flt.read_symbol_flags_keep_set = _read_symbol_flags_keep_set_guarded

        logger.warning("[NAS SQLITE IO GUARD] patched symbol_flags filters")
        return ok
    except Exception:
        logger.exception("[NAS SQLITE IO GUARD] patch symbol_flags filters failed")
        return False


def _patch_ats_db_path() -> bool:
    try:
        import ats.ats_ranking.db_path as dp
        old_has = getattr(dp, "_db_has_usable_ranking_tables", None)
        if not callable(old_has) or getattr(old_has, "_nas_io_guard_v1", False):
            return True

        def _db_has_usable_ranking_tables_guarded(db_path: str, *args, **kwargs):
            if db_path and _is_bad(db_path):
                logger.warning("[NAS SQLITE IO GUARD] skip ats usable check bad path=%s", db_path)
                return False
            try:
                return old_has(db_path, *args, **kwargs)
            except Exception as e:
                if _looks_io_error(e):
                    _mark_bad(db_path, e)
                    return False
                raise

        _db_has_usable_ranking_tables_guarded._nas_io_guard_v1 = True  # type: ignore[attr-defined]
        _db_has_usable_ranking_tables_guarded._original = old_has  # type: ignore[attr-defined]
        dp._db_has_usable_ranking_tables = _db_has_usable_ranking_tables_guarded
        logger.warning("[NAS SQLITE IO GUARD] patched ats db usable check")
        return True
    except Exception:
        logger.exception("[NAS SQLITE IO GUARD] patch ats db_path failed")
        return False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    ok = True
    ok = _patch_ats_db_path() and ok
    ok = _patch_ranking_selector() and ok
    ok = _patch_ranking_source_selector() and ok
    ok = _patch_symbol_flags_filters() and ok
    _INSTALLED = bool(ok)
    logger.warning("[NAS SQLITE IO GUARD] installed=%s cooldown=%.1fs", _INSTALLED, _cooldown_sec())
    return _INSTALLED


try:
    install()
except Exception:
    logger.exception("[NAS SQLITE IO GUARD] auto install failed")

__all__ = ["install"]
