from __future__ import annotations

import datetime as _dt
import logging
import os
import sys
import sqlite3
from typing import Any, Iterable, Sequence
from nas_root import nas_path, nas_root

logger = logging.getLogger(__name__)
VERSION = "V1.4-ROTATION-DB-FALLBACK-AB100"
_INSTALLED = False


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, str(default)).replace(',', '')))
    except Exception:
        return default


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)).replace(',', ''))
    except Exception:
        return default


def _b(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).lower() in {"1", "true", "yes", "on", "enabled"}


def _d_core(xs: Iterable[Any]) -> list[str]:
    out, seen = [], set()
    for x in xs or []:
        s = str(x).strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def _rot(reason: Any) -> bool:
    s = str(reason or "").lower()
    return s.startswith("rotation_") or s.startswith("push_rotation_") or s in {"rotation", "rotate"}


def _onopen(reason: Any) -> bool:
    s = str(reason or "").lower()
    return s.startswith("on_open") or s.startswith("onopen") or "on_open" in s


def _today_yyyymmdd() -> str:
    for name in ("TRADE_DATE", "KABU_TRADE_DATE", "ATS_TRADE_DATE", "TARGET_DATE"):
        v = str(os.environ.get(name, "")).strip()
        if len(v) >= 8:
            digits = "".join(ch for ch in v if ch.isdigit())
            if len(digits) >= 8:
                return digits[:8]
    return _dt.date.today().strftime("%Y%m%d")


def _iter_existing_db_paths(kind: str) -> list[str]:
    ymd = _today_yyyymmdd()
    roots = _d([
        os.environ.get("AUTOSTOCK_ROOT"),
        os.environ.get("AUTO_STOCK_ROOT"),
        os.environ.get("KABU_DATA_ROOT"),
        os.environ.get("RAW_DATA_ROOT"),
        nas_path(r"raw_data"),
    ])
    paths: list[str] = []
    for root in roots:
        if not root:
            continue
        root = str(root).rstrip("\\/")
        if kind == "ranking":
            # 2026-09-07: ローカルを先に見る (ranking DB をローカルへ移したため)
            try:
                from trading.ranking import ranking_db_location

                paths.append(str(ranking_db_location.ranking_db_dir() / f"ranking{ymd}.db"))
            except Exception:
                pass
            paths.extend([
                rf"{root}\kabu_station\ranking\ranking{ymd}.db",
                rf"{root}\ranking\ranking{ymd}.db",
            ])
        elif kind == "summary":
            paths.extend([
                rf"{root}\kabu_station\summary\summary{ymd}.db",
                rf"{root}\summary\summary{ymd}.db",
            ])
    return [p for p in _d(paths) if os.path.exists(p)]


def _sqlite_symbols_from_db(path: str, *, max_rows: int = 250) -> list[str]:
    out: list[str] = []
    try:
        con = sqlite3.connect(path, timeout=0.25)
        try:
            cur = con.cursor()
            tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            preferred = [
                t for t in tables
                if any(k in t.lower() for k in ("ranking", "snapshot", "summary", "stock_summary"))
            ] or tables
            for table in preferred:
                if len(out) >= max_rows:
                    break
                try:
                    cols = [r[1] for r in cur.execute(f'PRAGMA table_info("{table}")').fetchall()]
                except Exception:
                    continue
                col = next((c for c in cols if c.lower() in {"symbol", "code", "銘柄コード"}), None)
                if not col:
                    continue
                order_col = next(
                    (c for c in cols if c.lower() in {"datetime", "created_at", "updated_at", "currentpricetime", "time", "timestamp"}),
                    None,
                )
                sql = f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL'
                if order_col:
                    sql += f' ORDER BY "{order_col}" DESC'
                sql += f' LIMIT {int(max_rows)}'
                try:
                    vals = [r[0] for r in cur.execute(sql).fetchall()]
                except Exception:
                    continue
                out = _d(out + vals)
            return out[:max_rows]
        finally:
            con.close()
    except Exception:
        logger.debug("[PUSH ROTATION DB FALLBACK] sqlite read failed path=%s", path, exc_info=True)
        return []

def _tr_clean(src: Any) -> tuple[list[str], int, int, int]:
    from . import rotation_symbols

    try:
        return rotation_symbols.clean_symbol_list(src)
    except Exception:
        real = _d(src)
        return real, len(real), 0, 0

def _tr_add(dst: list[str], src: Any, *, source: str) -> None:
    real, raw_count, filler_count, invalid_count = _tr_clean(src)
    if not real:
        logger.info(
            "[PUSH ROTATION TARGET TOPUP] source=%s raw=%d real=0 filler=%d invalid=%d total=%d",
            source,
            raw_count,
            filler_count,
            invalid_count,
            len(dst),
        )
        return
    before = len(dst)
    dst[:] = _d(list(dst) + list(real))
    logger.warning(
        "[PUSH ROTATION TARGET TOPUP] source=%s raw=%d real=%d added=%d total=%d head=%s",
        source,
        raw_count,
        len(real),
        len(dst) - before,
        len(dst),
        dst[:10],
    )

def _tr_call(fn: Any) -> Any:
    from . import rotation_symbols

    if not callable(fn):
        return None
    for caller in (
        lambda: fn(limit=rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS),
        lambda: fn(max_symbols=rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS),
        lambda: fn(n=rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS),
        lambda: fn(rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS),
        lambda: fn(),
    ):
        try:
            return caller()
        except TypeError:
            continue
        except Exception:
            logger.debug("[PUSH ROTATION TARGET TOPUP] provider failed fn=%s", fn, exc_info=True)
            return None
    return None

def _tr_collect_runtime_all(dst: list[str]) -> None:
    from . import rotation_symbols

    for key in getattr(rotation_symbols, "_RUNTIME_SYMBOL_KEYS", ()):  # collect all, not first-hit only
        try:
            src = rotation_symbols._safe_get_runtime(key)
        except Exception:
            src = None
        if src is not None:
            _tr_add(dst, src, source=f"runtime.{key}")

def _tr_collect_global_data_all(dst: list[str]) -> None:
    from . import rotation_symbols

    try:
        gd = rotation_symbols._get_global_data()
    except Exception:
        gd = None
    if gd is None:
        return

    attrs = list(getattr(rotation_symbols, "_GLOBAL_DATA_SYMBOL_ATTRS", ())) + [
        "ranking_candidate_symbols",
        "ranking_symbols",
        "last_ranking_symbols",
        "latest_ranking_symbols",
        "daily_active_symbols",
        "daily_watchlist_symbols",
        "watchlist_symbols",
        "optional_watchlist_symbols",
    ]
    for attr in attrs:
        try:
            val = getattr(gd, attr, None)
            val = _tr_call(val) if callable(val) else val
            if val is not None:
                _tr_add(dst, val, source=f"global_data.{attr}")
        except Exception:
            logger.debug("[PUSH ROTATION TARGET TOPUP] global_data attr failed attr=%s", attr, exc_info=True)

    for name in (
        "get_monitor_symbols",
        "get_active_symbols",
        "get_push_symbols",
        "get_register_symbols",
        "get_ats_targets",
        "get_ats_register_targets",
        "get_ranking_symbols",
        "get_daily_watchlist_symbols",
    ):
        try:
            val = _tr_call(getattr(gd, name, None))
            if val is not None:
                _tr_add(dst, val, source=f"global_data.{name}()")
        except Exception:
            logger.debug("[PUSH ROTATION TARGET TOPUP] global_data getter failed name=%s", name, exc_info=True)

def _rebuild_active_symbols_enabled() -> bool:
    """push_receiver プロセスで active_symbols を再構築するか (2026-09-19)。

    既定 0。update_active_symbols(force=True) は summary と liquidity_map を
    まるごと作り直すため、push_receiver プロセス内で走らせると GIL を長時間
    占有し、ローテーションスレッドが進めなくなる (9/18 は 1 サイクル 38 秒、
    うち 33 秒が待ち)。結果を読むだけの取得は下でそのまま行うので、
    ranking_collector が更新した内容は従来どおり拾える。
    """
    import os

    v = str(os.getenv("PUSH_ROTATION_REBUILD_ACTIVE_SYMBOLS", "0")).strip().lower()
    return v in ("1", "true", "yes", "on")


def _tr_collect_active_symbol_manager(dst: list[str]) -> None:
    try:
        import trading.ranking.active_symbol_manager as asm
    except Exception:
        return

    try:
        fn = getattr(asm, "update_active_symbols", None) if _rebuild_active_symbols_enabled() else None
        if callable(fn):
            rebuilt = fn(force=True)
            _tr_add(dst, rebuilt, source="active_symbol_manager.update_active_symbols(force=True)")
    except TypeError:
        try:
            rebuilt = getattr(asm, "update_active_symbols")()
            _tr_add(dst, rebuilt, source="active_symbol_manager.update_active_symbols()")
        except Exception:
            logger.warning("[PUSH ROTATION TARGET TOPUP] update_active_symbols failed; continue", exc_info=True)
    except Exception:
        logger.warning("[PUSH ROTATION TARGET TOPUP] update_active_symbols failed; continue", exc_info=True)

    for name in (
        "get_rotation_symbols",
        "get_register_symbols",
        "get_push_symbols",
        "get_monitor_symbols",
        "get_active_symbols",
        "get_current_active_symbols",
        "get_subscription_symbols",
        "get_daily_watchlist_symbols",
        "load_daily_watchlist_symbols",
    ):
        try:
            val = _tr_call(getattr(asm, name, None))
            if val is not None:
                _tr_add(dst, val, source=f"active_symbol_manager.{name}()")
        except Exception:
            logger.debug("[PUSH ROTATION TARGET TOPUP] active getter failed name=%s", name, exc_info=True)

def _tr_collect_dynamic_providers_all(dst: list[str]) -> None:
    from . import rotation_symbols

    import importlib

    for module_name, func_name in getattr(rotation_symbols, "_DYNAMIC_SYMBOL_PROVIDERS", ()):
        try:
            mod = importlib.import_module(module_name)
            val = _tr_call(getattr(mod, func_name, None))
            if val is not None:
                _tr_add(dst, val, source=f"{module_name}.{func_name}()")
        except Exception:
            logger.debug(
                "[PUSH ROTATION TARGET TOPUP] dynamic provider failed %s.%s",
                module_name,
                func_name,
                exc_info=True,
            )

def _tr_collect_db_fallback(dst: list[str]) -> None:
    if not _b("PUSH_ROTATION_DB_FALLBACK", True):
        return
    max_rows = max(50, _i("PUSH_ROTATION_DB_FALLBACK_MAX", 300))
    for kind in ("ranking", "summary"):
        for path in _iter_existing_db_paths(kind):
            vals = _sqlite_symbols_from_db(path, max_rows=max_rows)
            if vals:
                _tr_add(dst, vals, source=f"db.{kind}:{path}")
            if len(dst) >= max(100, _i("PUSH_ROTATION_TARGET_MIN_KEEP", 100)):
                return

def _momentum_sort_enabled() -> bool:
    """PUSH 50 枠を当日変化率順で選ぶか (2026-09-25)。0 で従来どおり。"""
    import os

    v = str(os.getenv("ACTIVE_SYMBOL_MOMENTUM_PRIORITY", "1")).strip().lower()
    return v not in ("0", "false", "no", "off")


def _sort_by_momentum(symbols: list[str]) -> list[str]:
    """当日変化率の絶対値が大きい順に並べ替える。

    9/18 の実測では急騰 134 件のうち、その瞬間に PUSH のティックが
    あったのは 22 件 (16%) だけだった。50 枠の選び方を売買代金順から
    変化率順に変えると、枠に入る急騰銘柄が 5.7〜6.8 倍になる
    (ローカル 10 日 27->184 件 / NAS 21 日 46->262 件、21 勝 0 敗)。

    絶対値を使うのは下落側もリバウンドで急騰しやすいため
    (10 日検証で当日 -2% 以下の群の急騰率 38.6%、全体平均 17.3%)。
    保有銘柄 (protected) は必ず先頭に残す。
    変化率が取れない銘柄は 0 扱いで後ろに回るが、除外はしない。
    """
    if not symbols or not _momentum_sort_enabled():
        return symbols
    try:
        from trading.ranking.active_symbols.ranking_source import build_liquidity_map

        liq = build_liquidity_map() or {}
    except Exception:
        logger.debug("[PUSH ROTATION TARGET TOPUP] liquidity map unavailable", exc_info=True)
        return symbols
    if not liq:
        return symbols

    try:
        from .protected_symbols import resolve_protected_push_symbols

        protected = set(resolve_protected_push_symbols() or ())
    except Exception:
        protected = set()

    def _key(sym: str):
        info = liq.get(sym) or {}
        try:
            chg = abs(float(info.get("change_percentage") or 0.0))
        except Exception:
            chg = 0.0
        try:
            val = float(info.get("trading_value") or 0.0)
        except Exception:
            val = 0.0
        return (1 if sym in protected else 0, chg, val)

    try:
        out = sorted(symbols, key=_key, reverse=True)
    except Exception:
        logger.debug("[PUSH ROTATION TARGET TOPUP] momentum sort failed", exc_info=True)
        return symbols

    try:
        head = out[:5]
        logger.info(
            "[PUSH ROTATION TARGET TOPUP] momentum sort applied n=%d protected=%d head=%s",
            len(out), len(protected),
            [(s, round(abs(float((liq.get(s) or {}).get("change_percentage") or 0.0)), 2)) for s in head],
        )
    except Exception:
        pass
    return out

def resolve_monitor_symbols() -> list[str]:
    from . import rotation_core, rotation_symbols

    max_keep = max(1, _i("PUSH_ROTATION_TARGET_MAX_KEEP", rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS))
    min_keep = max(1, _i("PUSH_ROTATION_TARGET_MIN_KEEP", min(100, max_keep)))

    merged: list[str] = []

    _tr_collect_runtime_all(merged)
    _tr_collect_global_data_all(merged)

    if len(merged) < min_keep:
        _tr_collect_active_symbol_manager(merged)
        _tr_collect_global_data_all(merged)

    if len(merged) < min_keep:
        _tr_collect_dynamic_providers_all(merged)

    # 最後の保険: 共有メモリが全て空でも、ランキングDB/サマリーDBから復元する。
    if len(merged) < min_keep:
        _tr_collect_db_fallback(merged)

    # 2026-09-25: 切り捨てる前に当日変化率 (絶対値) で並べ替える。
    #   ここを通らないと ACTIVE の約 90 銘柄から 50 枠を選ぶ優先順位が
    #   無く、set の順序任せ (= 銘柄コード順) になる。
    merged = _sort_by_momentum(_d(merged))[:max_keep]

    if merged:
        level = logger.warning if len(merged) < min_keep else logger.info
        level(
            "[PUSH ROTATION TARGET TOPUP] resolved total=%d min_keep=%d max_keep=%d head=%s",
            len(merged),
            min_keep,
            max_keep,
            merged[:20],
        )
        return merged

    logger.warning("[PUSH ROTATION TARGET TOPUP] no symbols resolved after topup including db fallback")
    return []

def resolve_register_targets() -> list[str]:
    from . import rotation_core, rotation_symbols

    # 旧実装 (rotation_stability_patch) では同じクロージャ内の
    # resolve_monitor_symbols_patched() を直接呼んでいた。
    # モジュール関数へ切り出した際に呼び出し側の改名が漏れていたので合わせる。
    # rotation_symbols 経由にすると同じ実装へ二重に入るだけなので直接呼ぶ。
    targets = resolve_monitor_symbols()
    targets, raw_count, filler_count, invalid_count = rotation_symbols.clean_symbol_list(targets)

    if not targets:
        logger.warning(
            "[push_stream] no real register targets resolved raw=%d filler=%d invalid=%d",
            raw_count,
            filler_count,
            invalid_count,
        )
        return []

    before_limit = len(targets)

    try:
        if callable(rotation_symbols.merge_protected_first):
            merged = rotation_symbols.merge_protected_first(
                targets,
                max_symbols=rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS,
            )
            if merged:
                logger.warning(
                    "[push_stream] protected merge applied before=%d after=%d protected_head=%s",
                    len(targets),
                    len(merged),
                    merged[:20],
                )
                targets = merged
    except Exception:
        logger.exception("[push_stream] protected merge failed")

    targets = targets[:rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS]

    logger.info(
        "[push_stream] resolved register targets before_liquidity total=%d limited=%d max=%d chunk=%d head=%s",
        before_limit,
        len(targets),
        rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS,
        rotation_symbols.DEFAULT_REGISTER_CHUNK_SIZE,
        targets[:10],
    )

    targets = rotation_symbols.apply_register_liquidity_guard(targets)

    if not targets:
        logger.warning(
            "[push_stream] no register targets after liquidity guard before_limit=%d max=%d",
            before_limit,
            rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS,
        )
        return []

    targets = targets[:rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS]

    logger.info(
        "[push_stream] resolved register targets after_liquidity total=%d max=%d chunk=%d head=%s",
        len(targets),
        rotation_symbols.DEFAULT_REGISTER_MAX_SYMBOLS,
        rotation_symbols.DEFAULT_REGISTER_CHUNK_SIZE,
        targets[:10],
    )
    return targets

def build_protected_rotation_batches(targets: list[str]):
    from . import rotation_core

    targets = _d(targets)
    try:
        protected = _d(rotation_core._resolve_protected_safe())
    except Exception:
        protected = []
    chunk = int(getattr(rotation_core, "DEFAULT_REGISTER_CHUNK_SIZE", 50) or 50)
    fixed_n = min(max(0, _i("PUSH_ROTATION_FIXED_SYMBOLS", 15)), chunk)
    fixed = _d(protected[:fixed_n])
    slots = max(0, chunk - len(fixed))
    fixed_set = set(fixed)
    pool = _d([x for x in protected[fixed_n:] if x not in fixed_set] + [x for x in targets if x not in fixed_set])
    a = _d(fixed + pool[:slots])[:chunk]
    b = _d(fixed + pool[slots:slots * 2])[:chunk]
    if fixed and not b and pool:
        b = _d(fixed + pool[:slots])[:chunk]
    logger.warning("[PUSH ROTATION STABILITY] batches fixed=%d pool=%d A=%d B=%d", len(fixed), len(pool), len(a), len(b))
    return a, b, fixed

def _rotation_liq_guard_enabled() -> bool:
    """ローテーションの直近流動性ガードを呼ぶか (2026-09-25)。既定 0 = 呼ばない。

    recent_liquidity は SUMMARY_DB_PATH (NAS の summary DB) を銘柄ごとに読むため
    9/25 後場の実測で 1 回 5〜11 分かかり、その間ローテーションが止まっていた
    (周期 5分25秒 -> 11分08秒 と悪化。理想は約 8 秒)。

    しかも結果は使われない。min_keep=50 に対し targets は常にちょうど 50 件
    (PUSH_ROTATION_TARGET_MAX_KEEP=50) なので、絞られれば fail-open で元の 50、
    絞られなければ同じ 50 が返る。実測でも fail-open 19 回 / 完了 24 回 (79%)。
    構造上、返り値を変えられないので呼ばない。
    """
    return _b("PUSH_ROTATION_LIQ_GUARD_ENABLED", False)


def apply_register_liquidity_guard(targets: Sequence[str], orig) -> list[str]:
    from . import rotation_symbols

    if not _rotation_liq_guard_enabled():
        out = _d(targets)
        logger.info(
            "[PUSH ROTATION STABILITY] liquidity guard skipped n=%d "
            "(NAS summary DB を銘柄ごとに読むため 1 回 5〜11 分かかり、"
            "結果は min_keep=50 の fail-open で必ず捨てられていた)",
            len(out),
        )
        return out

    try:
        cleaned, _, _, _ = rotation_symbols.clean_symbol_list(targets)
    except Exception:
        cleaned = _d(targets)
    if not cleaned or not _b("PUSH_ROTATION_LIQ_GUARD_FAILOPEN_ON_COLLAPSE", True):
        return orig(targets)
    filtered = orig(targets)
    try:
        fc, _, _, _ = rotation_symbols.clean_symbol_list(filtered)
    except Exception:
        fc = _d(filtered)
    min_keep = max(1, _i("PUSH_ROTATION_LIQ_GUARD_MIN_KEEP", 50))
    if len(cleaned) >= min_keep and len(fc) < min_keep:
        logger.warning("[PUSH ROTATION STABILITY] liquidity fail-open before=%d after=%d", len(cleaned), len(fc))
        return cleaned
    return fc

def refresh_subscriptions(orig, core, symbols: Any = None, *, reason: str = "manual", force: bool = False,
            max_symbols: Any = None, clear_first: Any = None,
            unregister_first: Any = None, **kwargs: Any) -> bool:
    # 旧実装は既定値に core.REGISTER_MAX_SYMBOLS を直接書いていた
    # (クロージャ変数だったため def 時に評価できた)。core を引数で受ける形に
    # したので、None のときに同じ値へ解決する。
    if max_symbols is None:
        max_symbols = core.REGISTER_MAX_SYMBOLS
    target = None
    if _rot(reason) or _onopen(reason):
        target = core.build_target_symbols(symbols=symbols, max_symbols=max_symbols, reason=reason)
        target = core.enforce_register_limit(target, register_chunk_size=core.REGISTER_CHUNK_SIZE, reason=reason)
    if not _rot(reason):
        if _onopen(reason) and target:
            wait = max(0.0, _f("PUSH_ROTATION_UNREGISTER_WAIT_SEC", 0.2))
            logger.info("[PUSH ROTATION STABILITY] onopen clear/register reason=%s target=%d wait=%.3f", reason, len(target), wait)
            return orig(symbols=target, reason=reason, force=True, max_symbols=max_symbols,
                        clear_first=True, unregister_first=True,
                        wait_after_clear_sec=wait, unregister_wait_sec=wait, **kwargs)
        return orig(symbols=symbols, reason=reason, force=force, max_symbols=max_symbols,
                    clear_first=clear_first, unregister_first=unregister_first, **kwargs)
    if not target:
        return True
    with core.state.manager_lock:
        current = list(core.state.last_registered_symbols)
    wait = max(0.0, _f("PUSH_ROTATION_UNREGISTER_WAIT_SEC", 0.2))
    logger.info("[PUSH ROTATION STABILITY] rotate clear/register reason=%s current=%d target=%d wait=%.3f", reason, len(current), len(target), wait)
    ok = core.run_refresh_sequence(current_symbols=current, target_symbols=target,
                                   clear_first=True, unregister_first=True,
                                   wait_after_clear_sec=wait, unregister_wait_sec=wait)
    if ok:
        with core.state.manager_lock:
            core.state.last_registered_symbols = list(target)
            core.state.last_refresh_ts = core.now_ts()
            core.state.last_refresh_target_fingerprint = core.target_fingerprint(target)
        core.mark_reason(reason)
    return bool(ok)

__all__ = [
    "VERSION",
    "resolve_monitor_symbols",
    "resolve_register_targets",
    "build_protected_rotation_batches",
    "apply_register_liquidity_guard",
    "refresh_subscriptions",
]


# ============================================================
# 旧 core/startup/intraday_load_guard_patch._patch_push_rotation_hard_clamp
# を本文化。中間ログでも 100 件を超えないよう _d をクランプする。
# 適用スコープは旧 _safe_install の allowed 集合
# (DB|TRADING|HELPER = ほぼ全 context) を _ilg_scope_enabled() で再現。
# ============================================================

_ILG_TRUE = {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
_ILG_CONTEXTS = {
    "main_database", "db_prepare", "push_receiver", "summary_database",
    "ranking_collector", "yahoo_complement", "main", "helper",
}


def _ilg_argv_context() -> str:
    """core/startup/runtime_env/installer.py の _argv_context() と同一。"""
    try:
        text = " ".join(str(x).replace("\\", "/").lower() for x in sys.argv)
        if "main_database.py" in text:
            return "main_database"
        if text.endswith("main.py") or "/main.py" in text:
            return "main"
        if "db_prepare_runner.py" in text:
            return "db_prepare"
        if "push_receiver_runner.py" in text:
            return "push_receiver"
        if "summary_database_runner.py" in text:
            return "summary_database"
        if "ranking_collector_runner.py" in text:
            return "ranking_collector"
        if "yahoo_complement_runner.py" in text:
            return "yahoo_complement"
        if "data_collectors_runner.py" in text:
            return "main_database"
        if text:
            return "helper"
    except Exception:
        pass
    return "unknown"


def _ilg_scope_enabled() -> bool:
    if str(os.environ.get("DISABLE_INTRADAY_LOAD_GUARD_PATCH", "")).strip() == "1":
        return False
    if _ilg_env_bool("DISABLE_INTRADAY_LOAD_GUARD_PATCH", False):
        return False
    return _ilg_argv_context() in _ILG_CONTEXTS


def _ilg_env_bool(name: str, default: bool = False) -> bool:
    try:
        raw = str(os.getenv(name, "")).strip().lower()
        if raw in _ILG_TRUE:
            return True
        if raw in {"0", "false", "no", "n", "off", "disable", "disabled"}:
            return False
    except Exception:
        pass
    return bool(default)


def _ilg_env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        v = int(float(str(os.getenv(name, str(default))).replace(",", "")))
    except Exception:
        v = int(default)
    if min_value is not None:
        v = max(v, min_value)
    if max_value is not None:
        v = min(v, max_value)
    return int(v)


def _ilg_norm_symbol(symbol: Any) -> str:
    s = str(symbol or "").strip().upper()
    if s.endswith(".T"):
        s = s[:-2]
    return s


def _ilg_dedupe_limit(items: Iterable[Any], limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for x in items or []:
        s = _ilg_norm_symbol(x)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= limit:
            break
    return out


def _d(xs: Iterable[Any]) -> list[str]:
    if not _ilg_scope_enabled():
        return _d_core(xs)
    limit = _ilg_env_int("PUSH_ROTATION_TARGET_MAX_KEEP", 100, min_value=1, max_value=100)
    return _ilg_dedupe_limit(xs or [], limit)

