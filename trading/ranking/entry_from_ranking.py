# ============================================================
# File   : trading/ranking/entry_from_ranking.py
# Ver    : RANKING-ONLY-ENTRY-v5.3.0-MIN-PENDING-ON-TIMEOUT-INLINE
# ------------------------------------------------------------
# ✔ ranking_snapshot / ranking_raw → pending 生成の唯一の入口
# ✔ ランキング表示情報と、そこから算出できる数値だけで ENTRY 判定
# ✔ Ver5.3.0:
#     - core/startup/ranking_entry_min_pending_on_timeout_patch.py の
#       budget/grace deadline ロジックを本体へインライン化。
#       スコアリング・pending追加の両ループにタイムアウト予算を設け、
#       強い候補が見つかっているのに created=0 で終わる事象を防止する。
# ✔ Ver5.2.0:
#     - latest_ranking_df が 1000件超の時、全件にテクニカル保存/attachを走らせて
#       20秒timeoutしていた問題を修正
#     - rank/price/volume/turnover/rank_typeで軽量prefilterしてから重い処理へ進む
#     - DROPログは大量出力せず、理由別集計 + sample に圧縮
#     - ENVで対象数を調整可能
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
import os
import time
from collections import Counter
from typing import Dict, Any, Optional, Tuple, List

import pandas as pd

from global_state import global_data
from trading.entry.pending_manager import add_pending
from trading.ranking.side_infer import infer_side_from_rank_type
from AI.entry_row_builder import build_entry_row
from config.ranking_entry_config import RANKING_ENTRY_CONFIG, is_time_allowed

try:
    from trading.ranking.ranking_technical_store import (
        save_ranking_pseudo_technicals,
        attach_ranking_technicals,
    )
except Exception:  # pragma: no cover
    save_ranking_pseudo_technicals = None
    attach_ranking_technicals = None

logger = logging.getLogger(__name__)

RANK_TYPE_WEIGHT = {
    "値上がり率": 1.15,
    "値下がり率": 1.15,
    "売買高上位": 0.85,
    "売買代金": 1.00,
    "売買代金上位": 1.00,
    "TICK回数": 0.90,
    "TICK回数上位": 0.90,
    "売買高急増": 1.10,
    "売買代金急増": 1.10,
}

EXCHANGE_WEIGHT = {
    "TP": 1.05,
    "TS": 1.00,
    "TG": 1.05,
    "ALL": 1.00,
}

PREFILTER_PRIORITY_TYPES = (
    "値上がり率",
    "値下がり率",
    "売買高上位",
    "売買代金",
    "売買代金上位",
    "TICK回数",
    "TICK回数上位",
    "売買高急増",
    "売買代金急増",
)


def _env_int(name: str, default: int) -> int:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(v))
    except Exception:
        return int(default)


def _env_bool(name: str, default: bool) -> bool:
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


def _now() -> dt.datetime:
    return dt.datetime.now()


def _is_ranking_pending_entry(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    src = str(entry.get("source") or "").strip().upper()
    et = str(entry.get("entry_type") or "").strip().upper()
    mode = str(entry.get("ranking_entry_mode") or "").strip().upper()
    return src == "RANKING" or et == "RANKING" or mode.startswith("RANKING")


def _ranking_pending_created_at(entry: Any) -> Optional[dt.datetime]:
    if not isinstance(entry, dict):
        return None
    v = entry.get("created_at") or entry.get("created") or entry.get("timestamp")
    if isinstance(v, dt.datetime):
        return v
    if isinstance(v, str) and v.strip():
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return dt.datetime.strptime(v.strip(), fmt)
            except Exception:
                pass
        try:
            return dt.datetime.fromisoformat(v.strip())
        except Exception:
            return None
    return None


def _cleanup_stale_ranking_pending(reason: str = "TTL") -> int:
    """古いRANKING pendingが残留してrolling-retryの枠を食い潰さないよう、
    起動時に一度だけ TTL 超過分を掃除する
    (旧 core/startup/entry_log_skip_reason_collision_patch.py)。
    """
    ttl = _env_float("RANKING_PENDING_TTL_SEC", 20.0)
    if ttl <= 0:
        return 0
    now = dt.datetime.now()
    try:
        from trading.entry.pending_manager import prune_entries, snapshot_root

        def _pred(_symbol: str, entry: Dict[str, Any]) -> bool:
            if not _is_ranking_pending_entry(entry):
                return False
            created = _ranking_pending_created_at(entry)
            if created is None:
                return False
            return (now - created).total_seconds() >= ttl

        removed = prune_entries(_pred, reason=f"RANKING_STALE:{reason}")
        if removed:
            logger.warning(
                "[RANKING PENDING STALE CLEANUP] removed=%s ttl=%.1fs reason=%s root=%s",
                removed, ttl, reason, snapshot_root(),
            )
        return int(removed or 0)
    except Exception:
        logger.exception("[RANKING PENDING STALE CLEANUP] failed reason=%s", reason)
        return 0


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip()
        if s == "" or s.lower() in ("nan", "none", "nat", "<na>"):
            return default
        s = s.replace(",", "").replace("%", "")
        return float(s)
    except Exception:
        return default


def _safe_int(v: Any, default: int = 999999) -> int:
    try:
        return int(_safe_float(v, float(default)))
    except Exception:
        return default


def _first(row: Dict[str, Any], keys: Tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        try:
            if k in row:
                v = row.get(k)
                if v is not None and str(v).strip() != "":
                    return v
        except Exception:
            pass
    return default


def _pct_change(now_price: float, prev_price: float) -> float:
    if prev_price <= 0:
        return 0.0
    return ((now_price - prev_price) / prev_price) * 100.0


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _get_ranking_source_df() -> Optional[pd.DataFrame]:
    snapshot = getattr(global_data, "latest_ranking_snapshot", None)
    if isinstance(snapshot, list) and snapshot:
        logger.info("[RANKING ENTRY LOOP] source=latest_ranking_snapshot rows=%s", len(snapshot))
        return pd.DataFrame(snapshot)

    for name in ("latest_ranking_raw", "latest_ranking_df", "ranking_raw_df"):
        df = getattr(global_data, name, None)
        if isinstance(df, pd.DataFrame) and not df.empty:
            logger.info("[RANKING ENTRY LOOP] source=%s rows=%s", name, len(df))
            return df.copy()

    logger.warning("[RANKING ENTRY LOOP] ranking source dataframe not found")
    return None


def _normalize_rank_type(v: Any) -> str:
    s = str(v or "").strip()
    if not s:
        return ""
    s = s.replace("ランキング", "").strip()
    if "値上" in s or "上昇" in s:
        return "値上がり率"
    if "値下" in s or "下落" in s:
        return "値下がり率"
    if "売買代金" in s:
        return "売買代金"
    if "TICK" in s.upper() or "ティック" in s:
        return "TICK回数"
    if "出来高" in s or "売買高" in s:
        return "売買高上位"
    return s


def _extract_day_change_pct(row: Dict[str, Any]) -> float:
    v = _first(row, ("change_rate", "change_pct", "price_change_rate", "rate", "前日比率", "騰落率"), None)
    if v is not None:
        return _safe_float(v, 0.0)
    price = _safe_float(row.get("current_price") or row.get("price"), 0.0)
    prev_close = _safe_float(_first(row, ("prev_close", "previous_close", "base_price", "基準値", "前日終値"), 0.0), 0.0)
    if price > 0 and prev_close > 0:
        return _pct_change(price, prev_close)
    return 0.0


def _normalize_ranking_row_for_entry(row: Dict[str, Any]) -> Dict[str, Any]:
    row = dict(row)
    symbol = str(_first(row, ("symbol", "Symbol", "code", "コード", "銘柄コード"), "")).strip()
    if symbol.endswith(".0") and symbol[:-2].isdigit():
        symbol = symbol[:-2]
    if symbol:
        row["symbol"] = symbol

    price = _safe_float(
        _first(row, ("close_price", "current_price", "CurrentPrice", "price", "close", "現在値"), 0.0),
        0.0,
    )
    volume = _safe_float(
        _first(row, ("volume", "trading_volume", "TradingVolume", "出来高", "売買高"), 0.0),
        0.0,
    )
    turnover = _safe_float(
        _first(row, ("turnover", "trading_value", "TradingValue", "売買代金", "売買代金上位", "value", "Value"), 0.0),
        0.0,
    )
    if turnover <= 0 and price > 0 and volume > 0:
        turnover = price * volume

    rank_position = _safe_int(_first(row, ("rank_position", "rank", "ranking", "順位", "Rank"), 999999), 999999)
    rank_type = _normalize_rank_type(_first(row, ("rank_type", "ranking_type", "type", "ランキング種別", "種別"), ""))
    day_change_pct = _extract_day_change_pct({**row, "current_price": price, "price": price})

    if price > 0:
        for k in ("close_price", "current_price", "price", "close"):
            row[k] = price
    if volume > 0:
        row["volume"] = volume
        row["trading_volume"] = volume
    if turnover > 0:
        row["turnover"] = turnover
        row["trading_value"] = turnover

    row["rank_position"] = rank_position
    row["rank_type"] = rank_type
    row["day_change_pct"] = day_change_pct
    if not row.get("datetime"):
        row["datetime"] = row.get("snapshot_time") or row.get("time") or row.get("created_at")
    return row


def _prepare_rows(ranking_df: pd.DataFrame) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for _, raw_row in ranking_df.iterrows():
        row = _normalize_ranking_row_for_entry(raw_row.to_dict())
        row["source"] = "RANKING"
        if not str(row.get("symbol") or "").strip():
            continue
        rows.append(row)
    return rows


def _get_history_store() -> Dict[str, Any]:
    store = getattr(global_data, "ranking_entry_history", None)
    if not isinstance(store, dict):
        store = {}
        setattr(global_data, "ranking_entry_history", store)
    return store


def _history_key(symbol: str, side: str) -> str:
    return f"{symbol}:{side}"


def _get_prev_history(symbol: str, side: str) -> Dict[str, Any]:
    store = _get_history_store()
    h = store.get(_history_key(symbol, side), {})
    return h if isinstance(h, dict) else {}


def _update_history(symbol: str, side: str, price: float, rank_position: int, rank_type: str, now: dt.datetime) -> Dict[str, Any]:
    store = _get_history_store()
    key = _history_key(symbol, side)
    old = store.get(key, {}) if isinstance(store.get(key, {}), dict) else {}
    prices = list(old.get("prices", []))
    prices.append(price)
    prices = prices[-10:]
    old_side_seen = bool(old)
    consecutive = int(old.get("consecutive", 0)) + 1 if old_side_seen else 1
    new_h = {
        "symbol": symbol,
        "prev_price": old.get("last_price"),
        "prev_rank_position": old.get("last_rank_position"),
        "last_price": price,
        "last_rank_position": rank_position,
        "last_rank_type": rank_type,
        "last_seen": now,
        "prices": prices,
        "consecutive": consecutive,
    }
    store[key] = new_h
    return new_h


def _reset_missing_histories(current_keys: set[str]) -> None:
    store = _get_history_store()
    for key, h in list(store.items()):
        if key not in current_keys and isinstance(h, dict):
            h["consecutive"] = 0
            store[key] = h


def _infer_side(row: Dict[str, Any]) -> str:
    rt = str(row.get("rank_type") or "")
    side = infer_side_from_rank_type(rt)
    if side in ("BUY", "SELL"):
        return side
    day_change = _safe_float(row.get("day_change_pct"), 0.0)
    return "SELL" if day_change < 0 else "BUY"


def _prefilter_rank_type_weight(rt: str) -> float:
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


def _prefilter_row_priority(row: Dict[str, Any]) -> tuple:
    rank = _safe_int(row.get("rank_position") or row.get("rank"), 999999)
    turnover = _safe_float(row.get("turnover") or row.get("trading_value"), 0.0)
    volume = _safe_float(row.get("volume") or row.get("trading_volume"), 0.0)
    day_abs = abs(_safe_float(row.get("day_change_pct"), 0.0))
    rt_w = _prefilter_rank_type_weight(str(row.get("rank_type") or ""))
    return (rank, -rt_w, -turnover, -volume, -day_abs)


def _light_prefilter_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """重いtechnical保存/attach前に、明らかに対象外の行を削る。

    旧 core/startup/ranking_entry_fast_runtime_patch.py (V5) の_ultra_prefilter_rowsを
    インライン化した高速版。latest_ranking_df が1000件超の時に元の(全件ソート+全件走査の)
    実装が20秒以上timeoutしていたため、走査自体をRANKING_ENTRY_ULTRA_MAX_SOURCE_ROWS件で
    打ち切り、必要数(RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS)集まった時点で止める。
    """
    if not _env_bool("RANKING_ENTRY_PREFILTER_ENABLED", True):
        return rows

    cfg_rank = RANKING_ENTRY_CONFIG["RANKING"]
    cfg_vol = RANKING_ENTRY_CONFIG["VOLUME"]
    cfg_price = RANKING_ENTRY_CONFIG["PRICE"]
    cfg_move = RANKING_ENTRY_CONFIG["PRICE_MOVE"]

    max_source = max(100, _env_int("RANKING_ENTRY_ULTRA_MAX_SOURCE_ROWS", 600))
    max_rows = max(5, _env_int("RANKING_ENTRY_FAST_MAX_PREFILTER_ROWS", 40))
    max_rank = _env_int("RANKING_ENTRY_PREFILTER_MAX_RANK", int(cfg_rank.get("MAX_RANK_POSITION", 30)))
    max_per_type = max(3, _env_int("RANKING_ENTRY_FAST_MAX_PER_TYPE", 10))
    max_per_side = max(3, _env_int("RANKING_ENTRY_FAST_MAX_PER_SIDE", 22))
    min_price = _safe_float(cfg_price.get("MIN", 300), 300)
    max_price = _safe_float(cfg_price.get("MAX", 7000), 7000)
    min_volume = _safe_float(cfg_vol.get("MIN_VOLUME", 30000), 30000)
    min_turnover = _safe_float(cfg_vol.get("MIN_TURNOVER", 10000000), 10000000)
    max_day = abs(_safe_float(cfg_move.get("MAX_DAY_CHANGE_PCT", 10.0), 10.0))
    buy_min_day = _safe_float(cfg_move.get("BUY_MIN_DAY_CHANGE_PCT", 0.0), 0.0)
    sell_max_day = _safe_float(cfg_move.get("SELL_MAX_DAY_CHANGE_PCT", 0.0), 0.0)

    ordered = sorted([dict(r) for r in rows[:max_source]], key=_prefilter_row_priority)
    kept: List[Dict[str, Any]] = []
    rejects = Counter()
    samples: List[Dict[str, Any]] = []
    per_type = Counter()
    per_side = Counter()
    seen: set[tuple[str, str, str]] = set()

    def _sample_reject(reason: str, row: Dict[str, Any]) -> None:
        rejects[reason] += 1
        if len(samples) < 10:
            samples.append({
                "symbol": row.get("symbol"),
                "rank_type": row.get("rank_type"),
                "rank": row.get("rank_position"),
                "price": row.get("price") or row.get("current_price"),
                "volume": row.get("volume"),
                "turnover": row.get("turnover"),
                "day_change_pct": row.get("day_change_pct"),
                "reason": reason,
            })

    for row in ordered:
        symbol = str(row.get("symbol") or "").strip()
        rank_type = str(row.get("rank_type") or "")
        side = _infer_side(row)
        row["side"] = side
        key = (symbol, side, rank_type)
        if not symbol:
            _sample_reject("NO_SYMBOL", row)
            continue
        if key in seen:
            _sample_reject("DUP_SYMBOL_SIDE_TYPE", row)
            continue
        seen.add(key)

        price = _safe_float(row.get("price") or row.get("current_price"), 0.0)
        volume = _safe_float(row.get("volume"), 0.0)
        turnover = _safe_float(row.get("turnover"), 0.0)
        rank = _safe_int(row.get("rank_position"), 999999)
        day = _safe_float(row.get("day_change_pct"), 0.0)

        if rank > max_rank:
            _sample_reject("PREFILTER_RANK", row)
            continue
        if price < min_price or price > max_price:
            _sample_reject("PREFILTER_PRICE", row)
            continue
        if volume < min_volume:
            _sample_reject("PREFILTER_VOLUME", row)
            continue
        if turnover < min_turnover:
            _sample_reject("PREFILTER_TURNOVER", row)
            continue
        if abs(day) > max_day:
            _sample_reject("PREFILTER_DAY_TOO_LARGE", row)
            continue
        if side == "BUY" and day <= buy_min_day:
            _sample_reject("PREFILTER_BUY_DAY", row)
            continue
        if side == "SELL" and day >= sell_max_day:
            _sample_reject("PREFILTER_SELL_DAY", row)
            continue
        if rank_type not in PREFILTER_PRIORITY_TYPES:
            _sample_reject("PREFILTER_TYPE", row)
            continue
        if per_type[rank_type] >= max_per_type:
            _sample_reject("PREFILTER_TYPE_LIMIT", row)
            continue
        if per_side[side] >= max_per_side:
            _sample_reject("PREFILTER_SIDE_LIMIT", row)
            continue

        kept.append(row)
        per_type[rank_type] += 1
        per_side[side] += 1
        if len(kept) >= max_rows:
            break

    logger.warning(
        "[RANKING ENTRY PREFILTER] before=%s scanned=%s after=%s max_rows=%s max_rank=%s per_type=%s per_side=%s rejects=%s samples=%s",
        len(rows), min(len(rows), max_source), len(kept), max_rows, max_rank, dict(per_type), dict(per_side), dict(rejects), samples,
    )
    return kept


def _ranking_technical_filter(row: Dict[str, Any], side: str) -> Tuple[bool, str, float]:
    cfg = RANKING_ENTRY_CONFIG.get("TECHNICAL", {}) or {}
    if not bool(cfg.get("ENABLED", True)):
        return True, "TECH_DISABLED", 0.0

    ready = _safe_int(row.get("ranking_tech_ready"), 0)
    if bool(cfg.get("REQUIRE_READY", False)) and ready <= 0:
        return False, f"RANKING_TECH_NOT_READY reason={row.get('ranking_tech_reason')}", 0.0

    if not bool(cfg.get("REQUIRE_DIRECTION", True)):
        return True, "TECH_DIRECTION_DISABLED", 0.0

    close = _safe_float(row.get("close") or row.get("current_price") or row.get("price"), 0.0)
    ma5 = _safe_float(row.get("ma5"), 0.0)
    ma25 = _safe_float(row.get("ma25"), 0.0)
    rsi = _safe_float(row.get("rsi"), 50.0)
    macd = _safe_float(row.get("macd"), 0.0)
    signal = _safe_float(row.get("signal"), 0.0)
    slope = _safe_float(row.get("slope"), 0.0)
    tech_score = _safe_float(row.get("ranking_tech_score") or row.get("score_total"), 0.0)
    min_slope = abs(_safe_float(cfg.get("MIN_SLOPE", 0.0001), 0.0001))

    if bool(cfg.get("REQUIRE_CLOSE_VS_MA5", True)) and close > 0 and ma5 > 0:
        if side == "BUY" and close < ma5:
            return False, f"RANKING_TECH_BUY_CLOSE_BELOW_MA5 close={close:.2f} ma5={ma5:.2f}", tech_score
        if side == "SELL" and close > ma5:
            return False, f"RANKING_TECH_SELL_CLOSE_ABOVE_MA5 close={close:.2f} ma5={ma5:.2f}", tech_score

    if bool(cfg.get("REQUIRE_MA5_MA25", True)) and ma5 > 0 and ma25 > 0:
        if side == "BUY" and ma5 < ma25:
            return False, f"RANKING_TECH_BUY_MA5_LT_MA25 ma5={ma5:.2f} ma25={ma25:.2f}", tech_score
        if side == "SELL" and ma5 > ma25:
            return False, f"RANKING_TECH_SELL_MA5_GT_MA25 ma5={ma5:.2f} ma25={ma25:.2f}", tech_score

    if bool(cfg.get("REQUIRE_SLOPE", True)):
        if side == "BUY" and slope < min_slope:
            return False, f"RANKING_TECH_BUY_SLOPE_NG slope={slope:.6f} min={min_slope:.6f}", tech_score
        if side == "SELL" and slope > -min_slope:
            return False, f"RANKING_TECH_SELL_SLOPE_NG slope={slope:.6f} min=-{min_slope:.6f}", tech_score

    if bool(cfg.get("REQUIRE_MACD_SIGNAL", False)):
        if side == "BUY" and macd < signal:
            return False, f"RANKING_TECH_BUY_MACD_NG macd={macd:.4f} signal={signal:.4f}", tech_score
        if side == "SELL" and macd > signal:
            return False, f"RANKING_TECH_SELL_MACD_NG macd={macd:.4f} signal={signal:.4f}", tech_score

    if side == "BUY" and rsi > _safe_float(cfg.get("BUY_RSI_MAX", 82.0), 82.0):
        return False, f"RANKING_TECH_BUY_RSI_TOO_HIGH rsi={rsi:.2f}", tech_score
    if side == "SELL" and rsi < _safe_float(cfg.get("SELL_RSI_MIN", 18.0), 18.0):
        return False, f"RANKING_TECH_SELL_RSI_TOO_LOW rsi={rsi:.2f}", tech_score
    return True, "OK", tech_score


def _calc_ranking_only_score(row: Dict[str, Any], side: str, prev_price: float, prev_rank: int, consecutive: int) -> Tuple[float, Dict[str, float]]:
    cfg_rank = RANKING_ENTRY_CONFIG["RANKING"]
    cfg_vol = RANKING_ENTRY_CONFIG["VOLUME"]
    cfg_tech = RANKING_ENTRY_CONFIG.get("TECHNICAL", {}) or {}
    price = _safe_float(row.get("current_price") or row.get("price"), 0.0)
    volume = _safe_float(row.get("volume"), 0.0)
    turnover = _safe_float(row.get("turnover"), 0.0)
    rank_position = _safe_int(row.get("rank_position"), 999999)
    day_change_pct = _safe_float(row.get("day_change_pct"), 0.0)
    max_rank = max(1, int(cfg_rank.get("MAX_RANK_POSITION", 30)))
    min_volume = max(1.0, _safe_float(cfg_vol.get("MIN_VOLUME", 30000), 30000))
    min_turnover = max(1.0, _safe_float(cfg_vol.get("MIN_TURNOVER", 10000000), 10000000))
    rank_score = _clip((max_rank - rank_position + 1) / max_rank, 0.0, 1.0) * 25.0
    turnover_score = _clip(turnover / (min_turnover * 5.0), 0.0, 1.0) * 20.0
    volume_score = _clip(volume / (min_volume * 5.0), 0.0, 1.0) * 15.0
    rank_improve = float(prev_rank - rank_position) if prev_rank and prev_rank < 999999 else 0.0
    rank_improve_score = _clip(rank_improve / 10.0, 0.0, 1.0) * 15.0
    step_pct = _pct_change(price, prev_price) if prev_price > 0 else 0.0
    if side == "BUY":
        price_momentum_score = _clip(step_pct / 0.7, 0.0, 1.0) * 15.0
        day_direction_score = _clip(day_change_pct / 5.0, 0.0, 1.0) * 5.0
    else:
        price_momentum_score = _clip((-step_pct) / 0.7, 0.0, 1.0) * 15.0
        day_direction_score = _clip((-day_change_pct) / 5.0, 0.0, 1.0) * 5.0
    consecutive_score = _clip((consecutive - 1) / 2.0, 0.0, 1.0) * 5.0
    rank_type_weight = RANK_TYPE_WEIGHT.get(str(row.get("rank_type") or ""), 1.0)
    market_weight = EXCHANGE_WEIGHT.get(str(row.get("market") or "ALL"), 1.0)
    tech_score_raw = _safe_float(row.get("ranking_tech_score"), 0.0)
    tech_weight = _safe_float(cfg_tech.get("SCORE_WEIGHT", 2.0), 2.0) if bool(cfg_tech.get("ENABLED", True)) else 0.0
    tech_bonus = max(0.0, tech_score_raw) * tech_weight if side == "BUY" else max(0.0, -tech_score_raw) * tech_weight
    raw_score = rank_score + turnover_score + volume_score + rank_improve_score + price_momentum_score + day_direction_score + consecutive_score + tech_bonus
    final_score = _clip(raw_score * rank_type_weight * market_weight, 0.0, 100.0)
    return final_score, {
        "rank_score": rank_score, "turnover_score": turnover_score, "volume_score": volume_score,
        "rank_improve_score": rank_improve_score, "price_momentum_score": price_momentum_score,
        "day_direction_score": day_direction_score, "consecutive_score": consecutive_score,
        "ranking_tech_score": tech_score_raw, "ranking_tech_bonus": tech_bonus,
        "rank_type_weight": rank_type_weight, "market_weight": market_weight,
        "rank_improve": rank_improve, "step_pct": step_pct,
    }


def _passes_ranking_only_filters(row: Dict[str, Any], side: str, prev_h: Dict[str, Any], score: float, parts: Dict[str, float]) -> Tuple[bool, str]:
    cfg_rank = RANKING_ENTRY_CONFIG["RANKING"]
    cfg_vol = RANKING_ENTRY_CONFIG["VOLUME"]
    cfg_price = RANKING_ENTRY_CONFIG["PRICE"]
    cfg_move = RANKING_ENTRY_CONFIG["PRICE_MOVE"]
    cfg_score = RANKING_ENTRY_CONFIG["SCORE"]
    symbol = str(row.get("symbol") or "").strip()
    price = _safe_float(row.get("current_price") or row.get("price"), 0.0)
    volume = _safe_float(row.get("volume"), 0.0)
    turnover = _safe_float(row.get("turnover"), 0.0)
    rank_position = _safe_int(row.get("rank_position"), 999999)
    day_change_pct = _safe_float(row.get("day_change_pct"), 0.0)
    if not symbol:
        return False, "NO_SYMBOL"
    allow_type = cfg_rank.get("TYPE")
    if allow_type and str(allow_type) not in str(row.get("rank_type") or ""):
        return False, f"RANK_TYPE_NG rank_type={row.get('rank_type')} allow={allow_type}"
    if price < _safe_float(cfg_price.get("MIN", 300), 300) or price > _safe_float(cfg_price.get("MAX", 7000), 7000):
        return False, f"PRICE_RANGE_NG price={price}"
    if rank_position > int(cfg_rank.get("MAX_RANK_POSITION", 30)):
        return False, f"RANK_POSITION_NG rank={rank_position}"
    if volume < _safe_float(cfg_vol.get("MIN_VOLUME", 30000), 30000):
        return False, f"VOLUME_NG volume={volume}"
    if turnover < _safe_float(cfg_vol.get("MIN_TURNOVER", 10000000), 10000000):
        return False, f"TURNOVER_NG turnover={turnover}"
    prev_price = _safe_float(prev_h.get("last_price"), 0.0)
    prev_rank = _safe_int(prev_h.get("last_rank_position"), 999999)
    if bool(cfg_rank.get("REQUIRE_PREVIOUS_SNAPSHOT", True)) and prev_price <= 0:
        return False, "NO_PREVIOUS_RANKING_SNAPSHOT"
    consecutive = int(prev_h.get("consecutive", 0)) + 1 if prev_h else 1
    if consecutive < int(cfg_rank.get("MIN_CONSECUTIVE_APPEAR", 2)):
        return False, f"CONSECUTIVE_NG consecutive={consecutive}"
    if bool(cfg_rank.get("REQUIRE_RANK_NOT_WORSE", True)) and prev_rank < 999999 and rank_position > prev_rank:
        return False, f"RANK_WORSE prev={prev_rank} now={rank_position}"
    step_pct = _safe_float(parts.get("step_pct"), 0.0)
    max_step = abs(_safe_float(cfg_move.get("MAX_STEP_MOVE_PCT", 1.0), 1.0))
    if abs(step_pct) > max_step:
        return False, f"STEP_MOVE_TOO_LARGE step_pct={step_pct:.3f} max={max_step}"
    max_day = abs(_safe_float(cfg_move.get("MAX_DAY_CHANGE_PCT", 10.0), 10.0))
    if abs(day_change_pct) > max_day:
        return False, f"DAY_CHANGE_TOO_LARGE day_change_pct={day_change_pct:.3f} max={max_day}"
    if side == "BUY":
        if day_change_pct <= _safe_float(cfg_move.get("BUY_MIN_DAY_CHANGE_PCT", 0.0), 0.0):
            return False, f"BUY_DAY_DIRECTION_NG day_change_pct={day_change_pct:.3f}"
        if prev_price > 0 and price <= prev_price:
            return False, f"BUY_PRICE_NOT_UP prev={prev_price} now={price}"
    else:
        if day_change_pct >= _safe_float(cfg_move.get("SELL_MAX_DAY_CHANGE_PCT", 0.0), 0.0):
            return False, f"SELL_DAY_DIRECTION_NG day_change_pct={day_change_pct:.3f}"
        if prev_price > 0 and price >= prev_price:
            return False, f"SELL_PRICE_NOT_DOWN prev={prev_price} now={price}"
    if bool(cfg_rank.get("REQUIRE_PRICE_BREAKOUT", True)):
        prices = list(prev_h.get("prices", []))
        recent = prices[-max(1, int(cfg_rank.get("PRICE_BREAKOUT_WINDOW", 3))):]
        if recent:
            if side == "BUY" and price < max(recent):
                return False, f"BUY_NOT_RECENT_HIGH price={price} recent_high={max(recent)}"
            if side == "SELL" and price > min(recent):
                return False, f"SELL_NOT_RECENT_LOW price={price} recent_low={min(recent)}"
    tech_ok, tech_reason, _tech_score = _ranking_technical_filter(row, side)
    if not tech_ok:
        return False, tech_reason
    min_score = _safe_float(cfg_score.get("MIN_ENTRY_SCORE", 70.0), 70.0)
    if score < min_score:
        return False, f"SCORE_NG score={score:.2f} min={min_score:.2f}"
    return True, "OK"


def entry_from_ranking():
    _cleanup_stale_ranking_pending("before_ranking_entry")
    started = dt.datetime.now()
    started_perf = time.perf_counter()
    budget_sec = max(5.0, _env_float("RANKING_ENTRY_RUNTIME_BUDGET_SEC", 18.0))
    add_grace = max(0.0, min(_env_float("RANKING_ENTRY_MIN_PENDING_GRACE_SEC", 8.0), 15.0))
    deadline = started_perf + budget_sec
    add_deadline = deadline + add_grace
    max_pending = max(1, _env_int("RANKING_ENTRY_MAX_PENDING_PER_RUN", 5))
    min_force_score = _env_float("RANKING_ENTRY_MIN_PENDING_FORCE_SCORE", 70.0)

    logger.info(
        "[RANKING ENTRY LOOP] start at=%s mode=RANKING_ONLY budget_sec=%.1f add_grace=%.1f max_pending=%s",
        started.strftime("%Y-%m-%d %H:%M:%S"), budget_sec, add_grace, max_pending,
    )
    if not is_time_allowed(started):
        logger.info("[RANKING ENTRY LOOP] skip reason=TIME_GUARD now=%s", started.strftime("%H:%M:%S"))
        return 0
    ranking_df = _get_ranking_source_df()
    if ranking_df is None or ranking_df.empty:
        logger.info("[RANKING ENTRY LOOP] skip reason=no_ranking_df")
        return 0
    now = _now()
    rows_all = _prepare_rows(ranking_df)
    if not rows_all:
        logger.info("[RANKING ENTRY LOOP] skip reason=no_normalized_rows")
        return 0
    rows = _light_prefilter_rows(rows_all)
    if not rows:
        logger.info("[RANKING ENTRY LOOP] skip reason=no_prefilter_rows raw_rows=%s", len(rows_all))
        return 0

    tech_map: Dict[str, Dict[str, Any]] = {}
    cfg_tech = RANKING_ENTRY_CONFIG.get("TECHNICAL", {}) or {}
    if bool(cfg_tech.get("ENABLED", True)) and callable(save_ranking_pseudo_technicals):
        try:
            tech_map = save_ranking_pseudo_technicals(rows)
        except Exception:
            logger.exception("[RANKING ENTRY LOOP] technical attach failed -> continue without tech")
            tech_map = {}
        logger.info("[RANKING ENTRY LOOP] ranking_technical attached symbols=%s prefiltered_rows=%s raw_rows=%s", len(tech_map), len(rows), len(rows_all))

    created = 0
    build_reject = 0
    filter_reject = 0
    pending_reject = 0
    reject_samples: List[Dict[str, Any]] = []
    reject_counts = Counter()
    current_keys: set[str] = set()
    best_by_symbol_side: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for row in rows:
        if time.perf_counter() >= deadline and best_by_symbol_side:
            logger.warning(
                "[RANKING ENTRY LOOP] stop scoring with candidates elapsed=%.3fs candidates=%s",
                time.perf_counter() - started_perf, len(best_by_symbol_side),
            )
            break
        if time.perf_counter() >= add_deadline:
            logger.warning(
                "[RANKING ENTRY LOOP] hard stop scoring elapsed=%.3fs candidates=%s",
                time.perf_counter() - started_perf, len(best_by_symbol_side),
            )
            break
        if callable(attach_ranking_technicals):
            row = attach_ranking_technicals(row, tech_map)
        symbol = str(row.get("symbol") or "").strip()
        if not symbol:
            continue
        side = _infer_side(row)
        row["side"] = side
        current_keys.add(_history_key(symbol, side))
        prev_h = _get_prev_history(symbol, side)
        prev_price = _safe_float(prev_h.get("last_price"), 0.0)
        prev_rank = _safe_int(prev_h.get("last_rank_position"), 999999)
        consecutive = int(prev_h.get("consecutive", 0)) + 1 if prev_h else 1
        score, parts = _calc_ranking_only_score(row, side, prev_price, prev_rank, consecutive)
        row["score"] = score
        row["score_total"] = score
        row["ranking_only_score"] = score
        row["ranking_score_parts"] = parts
        ok, reason = _passes_ranking_only_filters(row, side, prev_h, score, parts)
        _update_history(symbol, side, _safe_float(row.get("price") or row.get("current_price"), 0.0), _safe_int(row.get("rank_position"), 999999), str(row.get("rank_type") or ""), now)
        if not ok:
            filter_reject += 1
            reason_key = str(reason).split()[0].split("=")[0]
            reject_counts[reason_key] += 1
            if len(reject_samples) < 20:
                reject_samples.append({
                    "symbol": symbol, "side": side, "rank_type": row.get("rank_type"), "rank": row.get("rank_position"),
                    "price": row.get("price"), "prev_price": prev_price, "volume": row.get("volume"),
                    "turnover": row.get("turnover"), "day_change_pct": row.get("day_change_pct"),
                    "ranking_tech_score": row.get("ranking_tech_score"), "ma5": row.get("ma5"), "ma25": row.get("ma25"),
                    "rsi": row.get("rsi"), "macd": row.get("macd"), "signal": row.get("signal"),
                    "slope": row.get("slope"), "score": round(score, 2), "reason": reason,
                })
            continue
        key = (symbol, side)
        old = best_by_symbol_side.get(key)
        old_score = _safe_float(old["row"].get("score_total"), 0.0) if isinstance(old, dict) and isinstance(old.get("row"), dict) else 0.0
        if old is None or score > old_score:
            best_by_symbol_side[key] = {"row": row, "parts": parts, "prev_price": prev_price, "prev_rank": prev_rank, "consecutive": consecutive}

    _reset_missing_histories(current_keys)

    packs = list(best_by_symbol_side.items())
    packs.sort(key=lambda kv: _safe_float(kv[1]["row"].get("score_total"), 0.0), reverse=True)

    for (symbol, side), pack in packs:
        if created >= max_pending:
            break
        final_score = _safe_float(pack["row"].get("score_total"), 0.0)
        if time.perf_counter() >= deadline and created > 0:
            logger.warning(
                "[RANKING ENTRY LOOP] stop pending_add after created elapsed=%.3fs created=%s",
                time.perf_counter() - started_perf, created,
            )
            break
        if time.perf_counter() >= add_deadline and final_score < min_force_score:
            logger.warning(
                "[RANKING ENTRY LOOP] hard stop pending_add elapsed=%.3fs created=%s score=%.2f",
                time.perf_counter() - started_perf, created, final_score,
            )
            break

        row = pack["row"]
        parts = pack["parts"]
        prev_price = pack["prev_price"]
        prev_rank = pack["prev_rank"]
        consecutive = pack["consecutive"]
        entry_row = build_entry_row(row)
        if not entry_row:
            build_reject += 1
            continue
        entry_row["side"] = side
        entry_row["source"] = "RANKING"
        entry_row["symbol"] = symbol
        entry_row.setdefault("entry_type", "RANKING")
        entry_row.setdefault("interval", 1)
        entry_row["score"] = final_score
        entry_row["score_total"] = final_score
        entry_row["ranking_only_score"] = final_score
        entry_row["ranking_entry_mode"] = "RANKING_ONLY_WITH_TECH"
        entry_row["ranking_prev_price"] = prev_price
        entry_row["ranking_prev_rank"] = prev_rank
        entry_row["ranking_consecutive"] = consecutive
        entry_row["ranking_step_pct"] = parts.get("step_pct")
        entry_row["ranking_rank_improve"] = parts.get("rank_improve")
        entry_row["ranking_score_parts"] = parts
        entry_row["ranking_min_pending_timeout_rescue"] = bool(time.perf_counter() >= deadline)
        for k in ("ma5", "ma25", "ma75", "rsi", "macd", "signal", "macd_hist", "atr", "slope", "slope_atr_scaled", "vwap", "ranking_tech_score", "ranking_tech_ready", "ranking_tech_reason", "ranking_tech_datetime", "ranking_tech_db"):
            if k in row:
                entry_row[k] = row.get(k)
        pending_entry = {
            **entry_row,
            "source": "RANKING",
            "created_at": now,
            "ranking_fallback_used": False,
            "ranking_strength": final_score,
            "technical_score": _safe_float(row.get("ranking_tech_score"), 0.0),
            "snapshot_score": final_score,
        }
        if add_pending(pending_entry):
            created += 1
            logger.info(
                "[RANKING PENDING ADD] mode=RANKING_ONLY_WITH_TECH symbol=%s side=%s rank_type=%s rank=%s price=%.2f prev_price=%.2f step=%.3f%% day=%.3f%% volume=%.0f turnover=%.0f consecutive=%s rank_improve=%.1f score=%.2f tech=%.2f ma5=%.2f ma25=%.2f rsi=%.2f slope=%.6f timeout_rescue=%s",
                symbol, side, row.get("rank_type"), row.get("rank_position"),
                _safe_float(row.get("price") or row.get("current_price"), 0.0), prev_price,
                _safe_float(parts.get("step_pct"), 0.0), _safe_float(row.get("day_change_pct"), 0.0),
                _safe_float(row.get("volume"), 0.0), _safe_float(row.get("turnover"), 0.0), consecutive,
                _safe_float(parts.get("rank_improve"), 0.0), final_score, _safe_float(row.get("ranking_tech_score"), 0.0),
                _safe_float(row.get("ma5"), 0.0), _safe_float(row.get("ma25"), 0.0), _safe_float(row.get("rsi"), 0.0), _safe_float(row.get("slope"), 0.0),
                entry_row.get("ranking_min_pending_timeout_rescue"),
            )
        else:
            pending_reject += 1

    elapsed = time.perf_counter() - started_perf
    if reject_samples:
        logger.warning("[RANKING ENTRY LOOP] ranking_only_reject_counts=%s samples=%s", dict(reject_counts), reject_samples)
    logger.info(
        "[RANKING ENTRY LOOP] done mode=RANKING_ONLY_WITH_TECH created=%s raw_total=%s prefiltered=%s candidates=%s filter_reject=%s build_reject=%s pending_reject=%s budget_sec=%.1f add_grace=%.1f elapsed=%.3fs",
        created, len(rows_all), len(rows), len(best_by_symbol_side), filter_reject, build_reject, pending_reject, budget_sec, add_grace, elapsed,
    )
    return created


def run_ranking_entry_pipeline():
    return entry_from_ranking()
