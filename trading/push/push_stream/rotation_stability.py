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

_RECENT_MOVE_CACHE: dict[str, Any] = {"at": 0.0, "key": "", "moves": {}}


def _recent_move_active(now: _dt.datetime) -> bool:
    """寄りの値動きが落ち着いた後 (既定 10:00〜) だけ直近の動き順で選ぶ。"""
    if not _b("PUSH_ROTATION_RECENT_MOVE_PRIORITY", True):
        return False
    start = str(os.environ.get("PUSH_ROTATION_RECENT_MOVE_START", "10:00")).strip() or "10:00"
    return now.strftime("%H:%M") >= start


def _recent_move_map(now: _dt.datetime) -> dict[str, float]:
    """直近 N 分 (既定 15) の 1 分あたり平均変化率をランキング DB から求める。

    ranking_snapshot_1min はローカル DB で 15 分ぶん 1.4 万行を 10ms 以下で読める。
    分が抜けている箇所 (11:25 などで実際に抜ける) は分数で割って 1 分あたりに直す。
    売買代金が下限 (既定 10 億円) 未満、株価が下限 (既定 200 円、エントリー下限と同じ) 未満の
    銘柄は入れない。低位株は 1 ティックで率が跳ねるだけなので (8918 = 11 円で 0.9%/分)。
    """
    import time as _time

    lookback = max(5, _i("PUSH_ROTATION_RECENT_MOVE_LOOKBACK_MIN", 15))
    min_value = _f("PUSH_ROTATION_RECENT_MOVE_MIN_TRADING_VALUE", 1e9)
    min_price = _f("PUSH_ROTATION_RECENT_MOVE_MIN_PRICE", 200.0)
    key = f"{now:%Y%m%d}-{lookback}-{min_value}-{min_price}"
    cache_sec = max(0.0, _f("PUSH_ROTATION_RECENT_MOVE_CACHE_SEC", 20.0))
    if _RECENT_MOVE_CACHE["key"] == key and _time.time() - _RECENT_MOVE_CACHE["at"] < cache_sec:
        return _RECENT_MOVE_CACHE["moves"]

    from trading.ranking.ranking_db_location import ranking_db_path

    path = ranking_db_path(now.date())
    if not path.exists():
        return {}
    since = (now - _dt.timedelta(minutes=lookback + 1)).strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    try:
        rows = con.execute(
            "SELECT symbol, datetime, MAX(COALESCE(current_price, price)), MAX(trading_value)"
            " FROM ranking_snapshot_1min WHERE datetime >= ?"
            " GROUP BY symbol, datetime ORDER BY symbol, datetime",
            (since,),
        ).fetchall()
    finally:
        con.close()

    moves: dict[str, float] = {}
    prev_sym, prev_t, prev_p = None, None, None
    acc: list[float] = []
    last_val = 0.0

    def _flush(sym, acc, last_p, last_val):
        if sym and acc and last_p and last_p >= min_price and last_val >= min_value:
            moves[sym] = (sum(acc) / len(acc)) / last_p

    for sym, dts, price, value in rows:
        sym = str(sym).strip().upper()
        try:
            t = _dt.datetime.strptime(str(dts)[:16], "%Y-%m-%d %H:%M")
            p = float(price or 0.0)
        except Exception:
            continue
        if sym != prev_sym:
            _flush(prev_sym, acc, prev_p, last_val)
            prev_sym, prev_t, prev_p, acc = sym, None, None, []
        if p <= 0:
            continue
        if prev_t is not None and prev_p:
            gap = max(1.0, (t - prev_t).total_seconds() / 60.0)
            acc.append(abs(p - prev_p) / gap)
        prev_t, prev_p = t, p
        last_val = float(value or 0.0)
    _flush(prev_sym, acc, prev_p, last_val)

    _RECENT_MOVE_CACHE.update(at=_time.time(), key=key, moves=moves)
    return moves


def _sort_by_recent_move(symbols: list[str], now: _dt.datetime | None = None) -> list[str]:
    """10:00 以降は「直近 15 分の 1 分平均変化率」が大きい順に 50 枠を選ぶ (2026-10-05)。

    寄り後はボラが急に縮み (10/5 は候補の 1 分 ATR 中央値が 9 時台 0.5% → 11 時台 0.13%)、
    当日変化率で選んだ銘柄の多くがボラ判定 (ATR 0.15%) で落ちていた。
    当日変化率は朝の動きの累積なので、昼には既に止まった銘柄が上に残る。
    過去 26 営業日 × 10:00〜14:15 の 7 時点で比べると、その後 30 分の 1 分 ATR が
    0.15% を超える銘柄の割合は 変化率順 28.4% → 直近の動き順 39.4%、182 回中 97% で上回った。

    候補はいまの monitor 候補 + ランキングに出ている売買代金 10 億円以上の銘柄。
    保有銘柄 (protected) は先頭に残す。直近の値が取れない銘柄は元の順で後ろに付ける。
    PUSH_ROTATION_RECENT_MOVE_PRIORITY=0 で無効 (当日変化率順だけに戻る)。
    """
    now = now or _dt.datetime.now()
    if not symbols or not _recent_move_active(now):
        return symbols
    try:
        moves = _recent_move_map(now)
    except Exception:
        logger.debug("[PUSH ROTATION TARGET TOPUP] recent move map failed", exc_info=True)
        return symbols
    if not moves:
        return symbols

    try:
        from .protected_symbols import resolve_protected_push_symbols

        protected = [s for s in _d_core(resolve_protected_push_symbols() or ()) if s]
    except Exception:
        protected = []

    # _d は PUSH_ROTATION_TARGET_MAX_KEEP で切り詰めるので、並べ替える前は _d_core を使う
    pool = _d_core(list(symbols) + list(moves.keys()))
    ranked = sorted((s for s in pool if s in moves), key=lambda s: moves[s], reverse=True)
    rest = [s for s in symbols if s not in moves]
    # ここで切り詰めると後段 (_drop_wide_spread) で後ろへ回した銘柄が枠内に残るので _d_core
    out = _d_core([s for s in protected if s in set(pool)] + ranked + rest)

    try:
        logger.info(
            "[PUSH ROTATION TARGET TOPUP] recent move sort applied n=%d scored=%d added=%d head=%s",
            len(out), len(ranked), len([s for s in ranked if s not in set(symbols)]),
            [(s, round(moves.get(s, 0.0) * 100, 3)) for s in out[:5]],
        )
    except Exception:
        pass
    return out


_BURST_CACHE: dict[str, Any] = {"at": 0.0, "key": "", "syms": []}


def _recent_burst_symbols(now: _dt.datetime) -> list[str]:
    """直近 3 分で急に動いた銘柄 (2026-10-26 ユーザー要望「登録の遅れを今すぐ直して」)。

    _sort_by_recent_move は 15 分の 1 分平均なので、動き出しが 15 分で薄まる。
    10/26 の 6203 (豊和工業) は 10:48 1,952 → 10:51 2,010 (+3%) と動き出し 11:05 にストップ高、
    PUSH 登録は 10:54:41 で、登録時は既に +9% だった。
    3 分の変化率 (絶対値) が 1.5% 以上かつ 3 分の売買代金 1 億円以上を、変化の大きい順に最大 8 件。
    10/26 のランキング DB で 1 分あたり平均 1.3 件 (最大 8 件)、6203 は 10:50 に拾える。
    PUSH_ROTATION_BURST_PRIORITY=0 で無効。
    """
    import time as _time

    if not _b("PUSH_ROTATION_BURST_PRIORITY", True):
        return []
    win = max(2, _i("PUSH_ROTATION_BURST_MINUTES", 3))
    min_chg = _f("PUSH_ROTATION_BURST_MIN_CHANGE_PCT", 1.5)
    min_tv = _f("PUSH_ROTATION_BURST_MIN_TRADING_VALUE", 1e8)
    max_n = max(0, _i("PUSH_ROTATION_BURST_MAX_SYMBOLS", 8))
    min_price = _f("PUSH_ROTATION_RECENT_MOVE_MIN_PRICE", 200.0)
    key = f"{now:%Y%m%d%H%M}-{win}-{min_chg}-{min_tv}-{max_n}"
    cache_sec = max(0.0, _f("PUSH_ROTATION_BURST_CACHE_SEC", 10.0))
    if _BURST_CACHE["key"] == key and _time.time() - _BURST_CACHE["at"] < cache_sec:
        return list(_BURST_CACHE["syms"])

    from trading.ranking.ranking_db_location import ranking_db_path

    path = ranking_db_path(now.date())
    if not path.exists() or max_n <= 0:
        return []
    since = (now - _dt.timedelta(minutes=win + 2)).strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    try:
        rows = con.execute(
            "SELECT symbol, substr(datetime,1,16), MAX(COALESCE(current_price, price)), MAX(trading_value)"
            " FROM ranking_snapshot_1min WHERE datetime >= ? AND datetime <= ?"
            " GROUP BY symbol, substr(datetime,1,16) ORDER BY symbol, 2",
            (since, now.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchall()
    finally:
        con.close()

    series: dict[str, list[tuple[_dt.datetime, float, float]]] = {}
    for sym, t, price, value in rows:
        try:
            p = float(price or 0.0)
            if p <= 0:
                continue
            series.setdefault(str(sym).strip().upper(), []).append(
                (_dt.datetime.strptime(str(t), "%Y-%m-%d %H:%M"), p, float(value or 0.0))
            )
        except Exception:
            continue
    scored: list[tuple[float, str]] = []
    for sym, pts in series.items():
        t1, p1, v1 = pts[-1]
        if p1 < min_price:
            continue
        # 最新点から win 分以内で最も古い点と比べる (ランキングは 1〜2 分おきに抜ける)
        base = None
        for t0, p0, v0 in pts:
            if (t1 - t0).total_seconds() <= win * 60:
                base = (t0, p0, v0)
                break
        if base is None or (t1 - base[0]).total_seconds() < 120:
            continue
        chg = (p1 - base[1]) / base[1] * 100.0
        if abs(chg) >= min_chg and (v1 - base[2]) >= min_tv:
            scored.append((abs(chg), sym))
    out = [s for _, s in sorted(scored, reverse=True)[:max_n]]
    _BURST_CACHE.update(at=_time.time(), key=key, syms=out)
    if out:
        logger.info("[PUSH ROTATION TARGET TOPUP] burst first=%s win=%dm min_chg=%.1f%% min_tv=%.0f",
                    [(s, round(c, 2)) for c, s in sorted(scored, reverse=True)[:max_n]], win, min_chg, min_tv)
    return out


_SPREAD_CACHE: dict[str, Any] = {"at": 0.0, "spreads": {}}


def _recent_spread_map(symbols: list[str], now: _dt.datetime) -> dict[str, float]:
    """直近 N 分 (既定 5) の最良気配スプレッド (%) の中央値を板情報 DB から求める。

    order_book は 1 日 1GB あり datetime 単独の索引が無い (全走査 4 秒) ので、
    (symbol, datetime) の索引で銘柄ごとに引く。板が記録されていない銘柄
    (= まだ PUSH 登録していない銘柄) は返さない。
    """
    import statistics
    import time as _time

    cache_sec = max(0.0, _f("PUSH_ROTATION_SPREAD_CACHE_SEC", 20.0))
    cached = _SPREAD_CACHE["spreads"]
    fresh = _time.time() - _SPREAD_CACHE["at"] < cache_sec
    todo = [s for s in symbols if not (fresh and s in cached)]
    if fresh and not todo:
        return cached
    if not fresh:
        cached = {}

    from trading.push.order_book_db_location import order_book_db_path

    path = order_book_db_path(now.date())
    if not path.exists():
        return cached
    lookback = max(1, _i("PUSH_ROTATION_SPREAD_LOOKBACK_MIN", 5))
    since = (now - _dt.timedelta(minutes=lookback)).strftime("%Y-%m-%d %H:%M:%S")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    try:
        for sym in todo:
            rows = con.execute(
                "SELECT sequence, MAX(CASE WHEN side='BUY' THEN price END),"
                " MIN(CASE WHEN side='SELL' THEN price END)"
                " FROM order_book WHERE symbol=? AND datetime>=? AND level=1 AND price>0"
                " GROUP BY sequence",
                (sym, since),
            ).fetchall()
            sps = [
                (ask - bid) / ((ask + bid) / 2.0) * 100.0
                for _, bid, ask in rows
                if bid and ask and ask > bid
            ]
            if sps:
                cached[sym] = statistics.median(sps)
    finally:
        con.close()
    _SPREAD_CACHE.update(at=_time.time() if not fresh else _SPREAD_CACHE["at"], spreads=cached)
    return cached


def _drop_wide_spread(symbols: list[str], now: _dt.datetime | None = None) -> list[str]:
    """直近のスプレッドが広い銘柄を監視枠から後ろへ回す (2026-10-05)。

    最後の FINAL ENTRY SAFETY GUARD が ENTRY_MAX_SPREAD_PCT (0.20%) 超を必ず弾くので、
    そういう銘柄に PUSH 枠を使っても発注に届かない。10/5 後場は 13:56〜引けまで
    約定ゼロで、最終段の却下は SPREAD_TOO_WIDE が最多 (4288 / 2962 / 338A が繰り返し)。
    板情報 DB 15 日分では、今 0.20% 超の銘柄は 10〜20 分後も 77.5% が 0.20% 超
    (今 0.20% 以下なら 8.7%)。登録銘柄の 32% がこの状態だった。

    除外ではなく末尾へ回すので、枠が余れば使われる。板が未記録の銘柄
    (未登録) はそのまま残し、登録後に広いと分かれば次の周回で後ろへ回る。
    保有・候補 (protected) は動かさない。PUSH_ROTATION_SPREAD_FILTER=0 で無効。
    """
    now = now or _dt.datetime.now()
    if not symbols or not _b("PUSH_ROTATION_SPREAD_FILTER", True):
        return symbols
    start = str(os.environ.get("PUSH_ROTATION_SPREAD_FILTER_START", "09:10")).strip() or "09:10"
    if now.strftime("%H:%M") < start:
        return symbols
    max_sp = _f("PUSH_ROTATION_MAX_SPREAD_PCT", _f("ENTRY_MAX_SPREAD_PCT", 0.20))
    try:
        spreads = _recent_spread_map(list(symbols), now)
    except Exception:
        logger.debug("[PUSH ROTATION TARGET TOPUP] spread map failed", exc_info=True)
        return symbols
    if not spreads:
        return symbols
    wide = [s for s in symbols if spreads.get(s, 0.0) > max_sp]
    if not wide:
        return symbols
    # 保有銘柄の解決は証券 API を叩く (約 0.6 秒) ので、広い銘柄があるときだけ
    try:
        from .protected_symbols import resolve_protected_push_symbols

        protected = set(_d_core(resolve_protected_push_symbols() or ()))
    except Exception:
        protected = set()
    wide = [s for s in wide if s not in protected]
    if not wide:
        return symbols
    wide_set = set(wide)
    out = [s for s in symbols if s not in wide_set] + wide
    try:
        logger.info(
            "[PUSH ROTATION TARGET TOPUP] wide spread moved back n=%d known=%d max=%.2f%% wide=%s",
            len(wide), len(spreads), max_sp,
            [(s, round(spreads[s], 3)) for s in wide[:10]],
        )
    except Exception:
        pass
    return out


def _latest_price_map(symbols: list[str], now: _dt.datetime) -> dict[str, float]:
    """銘柄ごとの直近の株価。ランキング DB (ローカル、全銘柄 1ms 未満) を先に見て、
    無い銘柄 (ETF など) だけ PUSH DB を (symbol, datetime) の索引で引く。"""
    out: dict[str, float] = {}
    from trading.ranking.ranking_db_location import ranking_db_path

    rpath = ranking_db_path(now.date())
    if rpath.exists():
        con = sqlite3.connect(f"file:{rpath}?mode=ro", uri=True, timeout=1.0)
        try:
            for sym in symbols:
                row = con.execute(
                    "SELECT COALESCE(current_price, price) FROM ranking_snapshot_1min"
                    " WHERE symbol=? ORDER BY datetime DESC LIMIT 1",
                    (sym,),
                ).fetchone()
                if row and row[0] and float(row[0]) > 0:
                    out[sym] = float(row[0])
        finally:
            con.close()
    todo = [s for s in symbols if s not in out]
    if todo:
        from trading.push.push_db_location import push_db_path

        ppath = push_db_path(now.date())
        if ppath.exists():
            con = sqlite3.connect(f"file:{ppath}?mode=ro", uri=True, timeout=1.0)
            try:
                for sym in todo:
                    row = con.execute(
                        "SELECT price FROM stream_data_raw WHERE symbol=? ORDER BY datetime DESC LIMIT 1",
                        (sym,),
                    ).fetchone()
                    if row and row[0] and float(row[0]) > 0:
                        out[sym] = float(row[0])
            finally:
                con.close()
    return out


def _drop_over_max_price(symbols: list[str], now: _dt.datetime | None = None) -> list[str]:
    """売買対象の上限 (既定 7,000 円) を超える銘柄を PUSH 監視から外す (2026-10-26 ユーザー要望)。

    summary の価格条件 (trade_universe_filter: 150 < close <= 7000) で足が作られず、
    エントリーもできないのに PUSH の 50 枠を使っていた (10/26 前場は 6857/285A/6532 など
    8 銘柄、受信 57 銘柄の 14%)。外した分は後ろの銘柄が繰り上がる。
    保有・候補 (protected) と株価が取れない銘柄は残す。
    PUSH_ROTATION_PRICE_FILTER=0 で無効、上限は PUSH_ROTATION_MAX_PRICE で変えられる。
    """
    now = now or _dt.datetime.now()
    if not symbols or not _b("PUSH_ROTATION_PRICE_FILTER", True):
        return symbols
    try:
        from trading.summary.pipeline.trade_universe_filter import resolve_pipeline_max_price

        default_max = float(resolve_pipeline_max_price())
    except Exception:
        default_max = 7000.0
    max_price = _f("PUSH_ROTATION_MAX_PRICE", default_max)
    try:
        prices = _latest_price_map(_d_core(symbols), now)
    except Exception:
        logger.debug("[PUSH ROTATION TARGET TOPUP] price map failed", exc_info=True)
        return symbols
    over = [s for s in symbols if prices.get(s, 0.0) > max_price]
    if not over:
        return symbols
    try:
        from .protected_symbols import resolve_protected_push_symbols

        protected = set(_d_core(resolve_protected_push_symbols() or ()))
    except Exception:
        protected = set()
    over_set = {s for s in over if s not in protected}
    if not over_set:
        return symbols
    try:
        logger.info(
            "[PUSH ROTATION TARGET TOPUP] over max price dropped n=%d max=%.0f dropped=%s",
            len(over_set), max_price, [(s, prices[s]) for s in over if s in over_set][:15],
        )
    except Exception:
        pass
    return [s for s in symbols if s not in over_set]


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
    merged = _sort_by_momentum(_d(merged))
    # 2026-10-05: 10:00 以降は直近 15 分の動き順で上書きする (寄り後はボラが縮むため)。
    merged = _sort_by_recent_move(merged)
    # 2026-10-08 (ユーザー要望): 資金が入っているテーマの銘柄 (theme_flow が 5 分ごとに書く、最大 10) を
    #   保有銘柄のすぐ後ろに入れて確実に監視する。THEME_FLOW_PUSH_PRIORITY=0 で無効。
    try:
        from trading.monitors.theme_flow import hot_symbols as _hot_symbols

        _hot = _d_core(_hot_symbols())
        if _hot:
            try:
                from .protected_symbols import resolve_protected_push_symbols

                _prot = [s for s in _d_core(resolve_protected_push_symbols() or ()) if s]
            except Exception:
                _prot = []
            merged = _d_core(_prot + _hot + merged)
            logger.info("[PUSH ROTATION TARGET TOPUP] theme flow hot first=%s protected=%d", _hot, len(_prot))
    except Exception:
        logger.debug("[PUSH ROTATION TARGET TOPUP] theme flow hot failed", exc_info=True)
    # 2026-10-26: 直近 3 分で急に動いた銘柄を保有のすぐ後ろへ (テーマ株より前)。保有と合わせて
    #   PUSH_ROTATION_FIXED_SYMBOLS (15) 以内なら固定枠に入り、A/B 両方で受信する。
    try:
        _burst = _d_core(_recent_burst_symbols(_dt.datetime.now()))
        if _burst:
            try:
                from .protected_symbols import resolve_protected_push_symbols

                _prot_b = [s for s in _d_core(resolve_protected_push_symbols() or ()) if s]
            except Exception:
                _prot_b = []
            merged = _d_core(_prot_b + _burst + merged)
    except Exception:
        logger.debug("[PUSH ROTATION TARGET TOPUP] burst failed", exc_info=True)
    # 2026-10-26: 7,000 円超は足もエントリーも無いので外す (切り詰める前に外して枠を埋める)。
    merged = _drop_over_max_price(merged)
    # 2026-10-05: 直近のスプレッドが 0.20% 超の銘柄は最後のガードで必ず弾かれるので後ろへ回す。
    merged = _drop_wide_spread(merged)[:max_keep]

    # 2026-10-03 (ユーザー要望): 前日の材料株 (株探【明日の好悪材料】) と夜間 PTS で動いた銘柄を、
    #   朝 (既定 8:00〜10:00) は先頭に入れて確実に監視する。取得は materials_watch が裏で 1 日 1 回。
    try:
        from trading.watch import materials_watch as _mw

        _pri = _mw.watch_symbols()
        if _pri:
            _pri = _drop_over_max_price(_d_core(_pri))
        if _pri:
            merged = _d(list(_pri) + merged)[:max_keep]
            logger.info("[PUSH ROTATION TARGET TOPUP] materials watch first=%s", _pri)
    except Exception:
        logger.debug("[PUSH ROTATION TARGET TOPUP] materials watch failed", exc_info=True)

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

