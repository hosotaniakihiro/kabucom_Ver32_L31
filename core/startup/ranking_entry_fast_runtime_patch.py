# ============================================================
# File   : core/startup/ranking_entry_fast_runtime_patch.py
# Version: V1-RANKING-ENTRY-FAST-PREFILTER-AND-BATCH-HISTORY
# ------------------------------------------------------------
# 目的:
#   ランキング由来エントリー作成が80秒以上かかる問題を軽減する。
#
# 背景:
#   2026-05-29ログ:
#     [RANKING ENTRY LOOP] done ... raw_total=1358 prefiltered=184 ... elapsed=80.612s
#
# 主因:
#   1) technical前の候補が184件残り、ランキングtechnical保存/attachが重い
#   2) ranking_technical_store._load_history が symbolごとにSQLを発行するため、NAS sqliteで遅い
#
# 方針:
#   - entry_from_ranking._light_prefilter_rows の戻り値をさらに上限圧縮する
#   - ranking_technical_store._load_history を symbol IN のバッチ取得へ差し替える
#   - entry_from_ranking 側に束縛済みの save_ranking_pseudo_technicals も軽量wrapする
# ============================================================

from __future__ import annotations

import logging
import os
import time
from collections import Counter
from typing import Any, Dict, List

import pandas as pd

logger = logging.getLogger(__name__)
_PATCHED = False
_ORIG_LIGHT_PREFILTER = None
_ORIG_SAVE_TECH = None
_ORIG_LOAD_HISTORY = None


def _env_bool(name: str, default: bool = True) -> bool:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return bool(default)
        return str(raw).strip().lower() in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}
    except Exception:
        return bool(default)


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        if raw is None or str(raw).strip() == "":
            return int(default)
        return int(float(raw))
    except Exception:
        return int(default)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip().replace(",", "").replace("%", "")
        if not s or s.lower() in {"nan", "none", "nat", "<na>"}:
            return default
        return float(s)
    except Exception:
        return default


def _safe_int(v: Any, default: int = 999999) -> int:
    try:
        return int(_safe_float(v, float(default)))
    except Exception:
        return default


def _side(row: Dict[str, Any]) -> str:
    s = str(row.get("side") or row.get("entry_decision") or "").upper().strip()
    if s in {"BUY", "SELL"}:
        return s
    rt = str(row.get("rank_type") or "")
    if "値下" in rt or "下落" in rt:
        return "SELL"
    return "BUY"


def _rank_type_weight(rt: str) -> float:
    s = str(rt or "")
    if "値上" in s or "値下" in s:
        return 1.20
    if "売買代金" in s:
        return 1.10
    if "TICK" in s.upper() or "ティック" in s:
        return 1.00
    if "出来高" in s or "売買高" in s:
        return 0.95
    return 0.80


def _row_priority(row: Dict[str, Any]) -> tuple:
    rank = _safe_int(row.get("rank_position") or row.get("rank"), 999999)
    turnover = _safe_float(row.get("turnover") or row.get("trading_value"), 0.0)
    volume = _safe_float(row.get("volume") or row.get("trading_volume"), 0.0)
    day = abs(_safe_float(row.get("day_change_pct"), 0.0))
    rt_w = _rank_type_weight(str(row.get("rank_type") or ""))
    # sorted昇順なので、rankは小さく、他は大きいほど優先。
    return (rank, -rt_w, -turnover, -volume, -day)


def _cap_rows(rows: List[Dict[str, Any]], *, context: str) -> List[Dict[str, Any]]:
    if not _env_bool("RANKING_ENTRY_FAST_CAP_ENABLED", True):
        return rows
    max_rows = _env_int("RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS", 80)
    max_symbols = _env_int("RANKING_ENTRY_FAST_MAX_SYMBOLS", 60)
    max_per_side = _env_int("RANKING_ENTRY_FAST_MAX_PER_SIDE", 45)
    max_per_type = _env_int("RANKING_ENTRY_FAST_MAX_PER_TYPE", 18)
    if len(rows) <= max_rows:
        return rows

    ordered = sorted([dict(r) for r in rows], key=_row_priority)
    kept: List[Dict[str, Any]] = []
    seen_symbols: set[str] = set()
    seen_symbol_side: set[tuple[str, str]] = set()
    per_side = Counter()
    per_type = Counter()
    rejects = Counter()

    for row in ordered:
        symbol = str(row.get("symbol") or "").strip()
        side = _side(row)
        rt = str(row.get("rank_type") or "")
        if not symbol:
            rejects["NO_SYMBOL"] += 1
            continue
        if (symbol, side) in seen_symbol_side:
            rejects["DUP_SYMBOL_SIDE"] += 1
            continue
        if len(seen_symbols) >= max_symbols and symbol not in seen_symbols:
            rejects["SYMBOL_LIMIT"] += 1
            continue
        if per_side[side] >= max_per_side:
            rejects["SIDE_LIMIT"] += 1
            continue
        if per_type[rt] >= max_per_type:
            rejects["TYPE_LIMIT"] += 1
            continue
        kept.append(row)
        seen_symbols.add(symbol)
        seen_symbol_side.add((symbol, side))
        per_side[side] += 1
        per_type[rt] += 1
        if len(kept) >= max_rows:
            break

    logger.warning(
        "[RANKING ENTRY FAST PATCH] cap context=%s before=%s after=%s max_rows=%s max_symbols=%s per_side=%s per_type=%s rejects=%s",
        context,
        len(rows),
        len(kept),
        max_rows,
        max_symbols,
        dict(per_side),
        dict(per_type),
        dict(rejects),
    )
    return kept


def _patched_light_prefilter_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    base = _ORIG_LIGHT_PREFILTER(rows)
    return _cap_rows(base, context="after_light_prefilter")


def _patched_save_ranking_pseudo_technicals(rows: List[Dict[str, Any]], *args: Any, **kwargs: Any) -> Dict[str, Dict[str, Any]]:
    if not _env_bool("RANKING_ENTRY_FAST_TECH_CAP_ENABLED", True):
        return _ORIG_SAVE_TECH(rows, *args, **kwargs)
    capped = _cap_rows(rows, context="before_save_technical")
    lookback = _env_int("RANKING_ENTRY_FAST_TECH_LOOKBACK_ROWS", 60)
    kwargs.setdefault("lookback_rows", lookback)
    t0 = time.time()
    ret = _ORIG_SAVE_TECH(capped, *args, **kwargs)
    logger.warning(
        "[RANKING ENTRY FAST PATCH] technical done rows %s->%s latest=%s elapsed=%.3fs lookback=%s",
        len(rows),
        len(capped),
        len(ret or {}),
        time.time() - t0,
        kwargs.get("lookback_rows"),
    )
    return ret


def _patched_load_history(conn: Any, symbols: List[str], lookback_rows: int = 120) -> pd.DataFrame:
    """symbolごとのSQL発行をやめ、IN句でまとめて取得する。"""
    try:
        import trading.ranking.ranking_technical_store as store

        if not symbols:
            return pd.DataFrame()
        symbols = [str(s) for s in dict.fromkeys(symbols) if str(s).strip()]
        if not symbols:
            return pd.DataFrame()
        batch_size = max(20, _env_int("RANKING_ENTRY_FAST_HISTORY_BATCH_SIZE", 80))
        chunks = []
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i + batch_size]
            placeholders = ",".join(["?"] * len(batch))
            # 少し多めに取り、Python側でsymbol別headする。NAS sqliteでN回SQLより速い。
            q = f"""
                SELECT * FROM {store.TABLE_NAME}
                WHERE symbol IN ({placeholders})
                ORDER BY symbol ASC, datetime DESC
            """
            part = pd.read_sql_query(q, conn, params=tuple(batch))
            if not part.empty:
                part = part.groupby("symbol", group_keys=False).head(int(lookback_rows))
                chunks.append(part)
        if not chunks:
            return pd.DataFrame()
        df = pd.concat(chunks, ignore_index=True)
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        return df.dropna(subset=["datetime"]).sort_values(["symbol", "datetime"], kind="stable")
    except Exception:
        logger.exception("[RANKING ENTRY FAST PATCH] batch load_history failed -> original")
        return _ORIG_LOAD_HISTORY(conn, symbols, lookback_rows=lookback_rows)


def install() -> bool:
    global _PATCHED, _ORIG_LIGHT_PREFILTER, _ORIG_SAVE_TECH, _ORIG_LOAD_HISTORY
    if _PATCHED:
        return True
    try:
        import trading.ranking.entry_from_ranking as efr
        import trading.ranking.ranking_technical_store as store

        patched = []
        cur_pf = getattr(efr, "_light_prefilter_rows", None)
        if callable(cur_pf) and not getattr(cur_pf, "_ranking_entry_fast_patch", False):
            _ORIG_LIGHT_PREFILTER = cur_pf
            _patched_light_prefilter_rows._ranking_entry_fast_patch = True  # type: ignore[attr-defined]
            efr._light_prefilter_rows = _patched_light_prefilter_rows
            patched.append("entry_from_ranking._light_prefilter_rows")

        cur_save = getattr(efr, "save_ranking_pseudo_technicals", None)
        if callable(cur_save) and not getattr(cur_save, "_ranking_entry_fast_patch", False):
            _ORIG_SAVE_TECH = cur_save
            _patched_save_ranking_pseudo_technicals._ranking_entry_fast_patch = True  # type: ignore[attr-defined]
            efr.save_ranking_pseudo_technicals = _patched_save_ranking_pseudo_technicals
            store.save_ranking_pseudo_technicals = _patched_save_ranking_pseudo_technicals
            patched.append("save_ranking_pseudo_technicals")

        cur_load = getattr(store, "_load_history", None)
        if callable(cur_load) and not getattr(cur_load, "_ranking_entry_fast_patch", False):
            _ORIG_LOAD_HISTORY = cur_load
            _patched_load_history._ranking_entry_fast_patch = True  # type: ignore[attr-defined]
            store._load_history = _patched_load_history
            patched.append("ranking_technical_store._load_history")

        _PATCHED = True
        logger.warning(
            "[RANKING ENTRY FAST PATCH] installed patched=%s max_rows=%s max_symbols=%s tech_lookback=%s",
            patched,
            _env_int("RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS", 80),
            _env_int("RANKING_ENTRY_FAST_MAX_SYMBOLS", 60),
            _env_int("RANKING_ENTRY_FAST_TECH_LOOKBACK_ROWS", 60),
        )
        return True
    except Exception:
        logger.exception("[RANKING ENTRY FAST PATCH] install failed")
        return False


try:
    install()
except Exception:
    logger.exception("[RANKING ENTRY FAST PATCH] auto install failed")


__all__ = ["install"]
