from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import Any, Iterable

logger = logging.getLogger(__name__)


def _env_default(name: str, value: str) -> None:
    try:
        if os.getenv(name) is None or str(os.getenv(name)).strip() == "":
            os.environ[name] = str(value)
    except Exception:
        pass


def _env_on(name: str, default: bool = False) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _is_database_collector_context() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if any(
            x in argv
            for x in (
                "main_database.py",
                "db_prepare_runner.py",
                "ranking_collector_runner.py",
                "push_receiver_runner.py",
                "yahoo_complement_runner.py",
                "summary_database_runner.py",
                "data_collectors_runner.py",
            )
        ):
            return True
        return any(
            os.getenv(k) == "1"
            for k in (
                "AUTOSTOCK_DATA_COLLECTORS_PROCESS",
                "AUTOSTOCK_MAIN_DATABASE_PROCESS",
                "AUTOSTOCK_SUMMARY_DB_WRITER",
                "AUTOSTOCK_RANKING_COLLECTOR_PROCESS",
            )
        )
    except Exception:
        return False


def _is_main_py() -> bool:
    try:
        argv = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        return "main.py" in argv and not _is_database_collector_context()
    except Exception:
        return False


def _patch_entry_order_builder_logger() -> bool:
    """Prevent NameError('logger') inside entry_order_builder during order build."""
    try:
        from trading.handlers import entry_order_builder as eob

        cur = getattr(eob, "logger", None)
        if cur is None or not hasattr(cur, "warning") or not hasattr(cur, "info"):
            eob.logger = logging.getLogger("trading.handlers.entry_order_builder")
            logger.warning("[USERCUSTOMIZE] entry_order_builder.logger patched version=V1-LOGGER-HOTFIX")
            return True
        logger.warning("[USERCUSTOMIZE] entry_order_builder.logger already ok version=V1-LOGGER-HOTFIX")
        return True
    except Exception:
        logger.exception("[USERCUSTOMIZE] entry_order_builder.logger patch failed version=V1-LOGGER-HOTFIX")
        return False


def _install_summary_ai_fast_order_builder() -> bool:
    """Shorten SUMMARY_AI board retry so order build finishes before snapshot_no_order."""
    try:
        os.environ.setdefault("ENTRY_ORDER_BOARD_RETRY_SEC", "0.8")
        os.environ.setdefault("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC", "0.2")
        from core.startup.summary_ai_fast_order_builder_patch import install as _install_fast_order

        ok = bool(_install_fast_order())
        logger.warning(
            "[USERCUSTOMIZE] summary AI fast order builder ok=%s retry_sec=%s retry_interval=%s",
            ok,
            os.getenv("ENTRY_ORDER_BOARD_RETRY_SEC"),
            os.getenv("ENTRY_ORDER_BOARD_RETRY_INTERVAL_SEC"),
        )
        return ok
    except Exception:
        logger.exception("[USERCUSTOMIZE] summary AI fast order builder failed")
        return False


def _install_runtime_defaults() -> bool:
    """Install centralized default env values without overriding user-provided env."""
    try:
        from core.startup.runtime_env_defaults_patch import install as _install_defaults

        ok = bool(_install_defaults())
        logger.warning("[USERCUSTOMIZE] centralized runtime defaults ok=%s", ok)
        return ok
    except Exception:
        logger.exception("[USERCUSTOMIZE] centralized runtime defaults failed")
        return False


def _install_summary_pending_age_relax() -> bool:
    try:
        from core.startup.summary_entry_pending_age_relax_patch import install as _install_age_relax

        ok = bool(_install_age_relax())
        logger.warning("[USERCUSTOMIZE] summary pending age relax ok=%s max_age=%s", ok, os.getenv("SUMMARY_ENTRY_PENDING_MAX_AGE_SEC"))
        return ok
    except Exception:
        logger.exception("[USERCUSTOMIZE] summary pending age relax failed")
        return False


def _install_summary_mtf_duplicate_datetime_guard() -> bool:
    try:
        from core.startup.summary_mtf_duplicate_datetime_guard_patch import install as _install_guard

        ok = bool(_install_guard())
        logger.warning("[USERCUSTOMIZE] summary MTF duplicate datetime guard ok=%s", ok)
        return ok
    except Exception:
        logger.exception("[USERCUSTOMIZE] summary MTF duplicate datetime guard failed")
        return False


_install_runtime_defaults()
_patch_entry_order_builder_logger()
_install_summary_ai_fast_order_builder()
_install_summary_pending_age_relax()
_install_summary_mtf_duplicate_datetime_guard()

if _is_main_py():
    for k, v in {
        "AUTOSTOCK_MAIN_OPERATION_MODE": "full",
        "AUTOSTOCK_MAIN_DISABLE_SCHEDULED_ENTRY_JOBS": "0",
        "AUTOSTOCK_MAIN_DISABLE_SCHEDULED_EXIT_LOOP": "0",
        "AUTOSTOCK_MAIN_SKIP_RANKING_ENTRY": "0",
        "AUTOSTOCK_MAIN_SKIP_TONOSAMA_ENTRY": "0",
        "AUTOSTOCK_MAIN_SKIP_SUMMARY_PUSH_BG": "0",
        "AUTOSTOCK_MAIN_SKIP_RANKING_SUMMARY_SCHEDULE": "0",
        "AUTOSTOCK_MAIN_SKIP_SUMMARY_PARENT_TICK": "0",
        "AUTOSTOCK_MAIN_SKIP_EXIT_LOOP_WHEN_BROKER_EMPTY": "0",
        "AUTOSTOCK_MAIN_SKIP_YAHOO_COMPLEMENT": "1",
        "YAHOO_COMPLEMENT_RUN_IN_MAIN": "0",
        "AUTOSTOCK_ENABLE_YAHOO_COMPLEMENT_IN_MAIN": "0",
        "AUTOSTOCK_MAIN_ENABLE_EXIT_LOOP": "1",
        "AUTOSTOCK_MAIN_ENABLE_RANKING_ENTRY": "1",
        "AUTOSTOCK_MAIN_ENABLE_TONOSAMA_ENTRY": "1",
        "AUTOSTOCK_MAIN_ENABLE_SUMMARY_AI_ENTRY": "1",
        "AUTOSTOCK_MAIN_ENABLE_SUMMARY_PARENT_TICK": "1",
        "AUTOSTOCK_MAIN_ENABLE_RANKING_SUMMARY_SCHEDULE": "1",
        "FORCE_ENABLE_MAIN_SUMMARY_PARENT_TICK": "1",
    }.items():
        _env_default(k, v)
    logger.warning(
        "[USERCUSTOMIZE] main restore defaults mode=%s exit=%s ranking=%s tonosama=%s summary_ai=%s summary_parent=%s summary_db_save_skip=%s yahoo_skip=%s",
        os.getenv("AUTOSTOCK_MAIN_OPERATION_MODE"),
        os.getenv("AUTOSTOCK_MAIN_ENABLE_EXIT_LOOP"),
        os.getenv("AUTOSTOCK_MAIN_ENABLE_RANKING_ENTRY"),
        os.getenv("AUTOSTOCK_MAIN_ENABLE_TONOSAMA_ENTRY"),
        os.getenv("AUTOSTOCK_MAIN_ENABLE_SUMMARY_AI_ENTRY"),
        os.getenv("AUTOSTOCK_MAIN_ENABLE_SUMMARY_PARENT_TICK"),
        os.getenv("AUTOSTOCK_MAIN_SKIP_SUMMARY_DB_SAVE"),
        os.getenv("AUTOSTOCK_MAIN_SKIP_YAHOO_COMPLEMENT"),
    )

os.environ["ENTRY_ALLOW_ENTRY_WITHOUT_BOARD"] = "1"
os.environ["ENTRY_BOARD_MISSING_HARD_BLOCK"] = "0"


def _uc_float(v, default: float = 0.0) -> float:
    try:
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _uc_patch_tonosama_recent_volume_display() -> bool:
    if not _is_main_py():
        return False
    try:
        import re
        import pandas as pd
        import trading.entry.tonosama.volume_surge as vs
        import trading.entry.tonosama.pending_writer as pw

        def _norm_symbol_series(s):
            return s.astype(str).str.replace(r"\.0$", "", regex=True).str.strip()

        orig_build = getattr(vs.build_scalping_feature_df, "_original", vs.build_scalping_feature_df)
        if not getattr(vs.build_scalping_feature_df, "_uc_recent_volume_display_v1", False):
            def _patched_build_scalping_feature_df():
                out = orig_build()
                try:
                    if out is None or out.empty or "symbol" not in out.columns:
                        return out
                    raw1 = vs.normalize_summary_base(vs.load_merged_summary(1), interval=1)
                    if raw1 is None or raw1.empty or "symbol" not in raw1.columns or "volume" not in raw1.columns:
                        return out
                    raw1 = raw1.copy()
                    raw1["symbol"] = _norm_symbol_series(raw1["symbol"])
                    raw1["volume"] = pd.to_numeric(raw1["volume"], errors="coerce").fillna(0.0)
                    if "datetime" in raw1.columns:
                        raw1["datetime"] = pd.to_datetime(raw1["datetime"], errors="coerce")
                        raw1 = raw1.dropna(subset=["datetime"]).sort_values(["symbol", "datetime"])
                    rows = []
                    for sym, g in raw1.groupby("symbol", sort=False):
                        gv = g.tail(5)["volume"]
                        rows.append({
                            "symbol": str(sym),
                            "recent_volume_1m": float(gv.tail(1).sum()),
                            "recent_volume_3m": float(gv.tail(3).sum()),
                            "recent_volume_5m": float(gv.tail(5).sum()),
                        })
                    if not rows:
                        return out
                    recent = pd.DataFrame(rows)
                    x = out.copy()
                    x["symbol"] = _norm_symbol_series(x["symbol"])
                    x = x.merge(recent, on="symbol", how="left")
                    for c in ("recent_volume_1m", "recent_volume_3m", "recent_volume_5m"):
                        x[c] = pd.to_numeric(x.get(c, 0.0), errors="coerce").fillna(0.0)
                    logger.warning(
                        "[USERCUSTOMIZE][TONOSAMA RECENT VOLUME DISPLAY] attached rows=%s sample=%s",
                        len(x),
                        x[[c for c in ["symbol", "recent_volume_1m", "recent_volume_3m", "recent_volume_5m"] if c in x.columns]].head(10).to_dict("records"),
                    )
                    return x
                except Exception:
                    logger.exception("[USERCUSTOMIZE][TONOSAMA RECENT VOLUME DISPLAY] attach failed")
                    return out
            _patched_build_scalping_feature_df._uc_recent_volume_display_v1 = True  # type: ignore[attr-defined]
            _patched_build_scalping_feature_df._original = orig_build  # type: ignore[attr-defined]
            vs.build_scalping_feature_df = _patched_build_scalping_feature_df

        orig_conditions = getattr(pw._entry_conditions_from_row, "_original", pw._entry_conditions_from_row)
        if not getattr(pw._entry_conditions_from_row, "_uc_recent_volume_display_v1", False):
            def _row_vol(row, names, default=0.0):
                for name in names:
                    try:
                        val = _uc_float(row.get(name), -1.0)
                    except Exception:
                        val = -1.0
                    if val >= 0:
                        return val
                return float(default)

            def _replace_volume_text(reason: str, v1: float, v3: float, v5: float) -> str:
                new_text = f"出来高 1m={v1:.0f} / 3m={v3:.0f} / 5m={v5:.0f}"
                pat = r"出来高 1m=[0-9,\.]+\s*/\s*3m=[0-9,\.]+\s*/\s*5m=[0-9,\.]+"
                if re.search(pat, reason or ""):
                    return re.sub(pat, new_text, reason, count=1)
                return f"{reason} / {new_text}" if reason else new_text

            def _patched_entry_conditions_from_row(row, *, ai_reason: str, side: str, expire_at):
                cond = orig_conditions(row, ai_reason=ai_reason, side=side, expire_at=expire_at)
                try:
                    v1 = _row_vol(row, ["recent_volume_1m", "volume_1m", "latest_volume_1m", "volume", "_latest_volume"])
                    v3 = _row_vol(row, ["recent_volume_3m", "volume_last_3m", "rolling_volume_3m", "volume_3m_display", "volume_3m"], v1)
                    v5 = _row_vol(row, ["recent_volume_5m", "volume_last_5m", "rolling_volume_5m", "volume_5m_display", "volume_5m"], v3)
                    if isinstance(cond, dict):
                        cond["display_volume_1m"] = v1
                        cond["display_volume_3m"] = v3
                        cond["display_volume_5m"] = v5
                        cond["recent_volume_1m"] = v1
                        cond["recent_volume_3m"] = v3
                        cond["recent_volume_5m"] = v5
                        cond["reason"] = _replace_volume_text(str(cond.get("reason") or ""), v1, v3, v5)
                    logger.warning(
                        "[USERCUSTOMIZE][TONOSAMA RECENT VOLUME DISPLAY] reason fixed symbol=%s side=%s v1=%.0f v3=%.0f v5=%.0f",
                        row.get("symbol") if hasattr(row, "get") else None,
                        side,
                        v1,
                        v3,
                        v5,
                    )
                except Exception:
                    logger.exception("[USERCUSTOMIZE][TONOSAMA RECENT VOLUME DISPLAY] reason patch failed")
                return cond
            _patched_entry_conditions_from_row._uc_recent_volume_display_v1 = True  # type: ignore[attr-defined]
            _patched_entry_conditions_from_row._original = orig_conditions  # type: ignore[attr-defined]
            pw._entry_conditions_from_row = _patched_entry_conditions_from_row
        logger.warning("[USERCUSTOMIZE] TONOSAMA recent volume display patch installed")
        return True
    except Exception:
        logger.exception("[USERCUSTOMIZE] TONOSAMA recent volume display patch install failed")
        return False


def _uc_norm_symbol(v: Any) -> str:
    s = str(v or "").strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def _uc_dedupe_symbols(values: Iterable[Any]) -> list[str]:
    out = []
    seen = set()
    for x in values or []:
        s = _uc_norm_symbol(x)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _uc_is_push_rotation_context(context: str) -> bool:
    c = str(context or "").lower()
    return "push.rotation" in c or "apply_register_liquidity_guard" in c or "push_stream.rotation" in c


def _uc_patch_watchlist_recent_liq_rotation_failopen() -> bool:
    """Keep PUSH registration alive when recent summary is missing/stale."""
    if not _is_main_py():
        return False
    try:
        try:
            from core.startup.watchlist_recent_liquidity_bulk_patch import install as _bulk_install
            _bulk_install()
        except Exception:
            logger.exception("[USERCUSTOMIZE][PUSH ROTATION LIQ FAILOPEN] bulk install before wrap failed")

        import core.startup.watchlist_recent_liquidity_guard_patch as base

        orig_filter = getattr(base, "_filter_symbols", None)
        if not callable(orig_filter):
            logger.warning("[USERCUSTOMIZE][PUSH ROTATION LIQ FAILOPEN] base _filter_symbols missing")
            return False
        if getattr(orig_filter, "_uc_push_rotation_failopen_v1", False):
            return True

        def _patched_filter_symbols(symbols: Iterable[Any], *, context: str) -> list[str]:
            items = _uc_dedupe_symbols(symbols)
            out = orig_filter(items, context=context)
            out_items = _uc_dedupe_symbols(out)
            if _uc_is_push_rotation_context(context) and items:
                min_keep = max(1, min(len(items), _env_int("UC_PUSH_ROTATION_LIQ_FAILOPEN_MIN_KEEP", 50)))
                if len(out_items) < min_keep:
                    logger.warning(
                        "[USERCUSTOMIZE][PUSH ROTATION LIQ FAILOPEN] context=%s before=%s after=%s min_keep=%s -> keep original targets",
                        context,
                        len(items),
                        len(out_items),
                        min_keep,
                    )
                    return items
            return out_items

        _patched_filter_symbols._uc_push_rotation_failopen_v1 = True  # type: ignore[attr-defined]
        _patched_filter_symbols._original = orig_filter  # type: ignore[attr-defined]
        base._filter_symbols = _patched_filter_symbols

        try:
            import trading.push.push_stream.rotation_symbols as rot_sym
            old_apply = getattr(rot_sym, "apply_register_liquidity_guard", None)
            if callable(old_apply) and not getattr(old_apply, "_uc_push_rotation_apply_failopen_v1", False):
                def _patched_apply_register_liquidity_guard(targets):
                    items = _uc_dedupe_symbols(targets)
                    out = old_apply(targets)
                    out_items = _uc_dedupe_symbols(out)
                    if items:
                        min_keep = max(1, min(len(items), _env_int("UC_PUSH_ROTATION_LIQ_FAILOPEN_MIN_KEEP", 50)))
                        if len(out_items) < min_keep:
                            logger.warning(
                                "[USERCUSTOMIZE][PUSH ROTATION APPLY FAILOPEN] before=%s after=%s min_keep=%s -> keep original targets",
                                len(items),
                                len(out_items),
                                min_keep,
                            )
                            return items
                    return out_items
                _patched_apply_register_liquidity_guard._uc_push_rotation_apply_failopen_v1 = True  # type: ignore[attr-defined]
                _patched_apply_register_liquidity_guard._original = old_apply  # type: ignore[attr-defined]
                rot_sym.apply_register_liquidity_guard = _patched_apply_register_liquidity_guard
                try:
                    import trading.push.push_stream.rotation as rot
                    rot._apply_register_liquidity_guard = _patched_apply_register_liquidity_guard
                except Exception:
                    pass
        except Exception:
            logger.exception("[USERCUSTOMIZE][PUSH ROTATION LIQ FAILOPEN] rotation apply wrap failed")

        logger.warning(
            "[USERCUSTOMIZE][PUSH ROTATION LIQ FAILOPEN] installed min_keep=%s",
            os.getenv("UC_PUSH_ROTATION_LIQ_FAILOPEN_MIN_KEEP", "50"),
        )
        return True
    except Exception:
        logger.exception("[USERCUSTOMIZE][PUSH ROTATION LIQ FAILOPEN] install failed")
        return False


def _uc_delayed_watchlist_patch_loop() -> None:
    for _ in range(10):
        try:
            _uc_patch_watchlist_recent_liq_rotation_failopen()
            _patch_entry_order_builder_logger()
            _install_summary_ai_fast_order_builder()
        except Exception:
            logger.exception("[USERCUSTOMIZE][PUSH ROTATION LIQ FAILOPEN] delayed retry failed")
        time.sleep(1.0)


if _is_main_py():
    _patch_entry_order_builder_logger()
    _install_summary_ai_fast_order_builder()
    _uc_patch_tonosama_recent_volume_display()
    _uc_patch_watchlist_recent_liq_rotation_failopen()
    threading.Thread(
        target=_uc_delayed_watchlist_patch_loop,
        name="usercustomize-push-rotation-liq-failopen",
        daemon=True,
    ).start()
