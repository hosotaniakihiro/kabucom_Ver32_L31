# ============================================================
# File   : trading/ranking/entry_from_ranking.py
# Ver    : RANKING-ONLY-ENTRY-v5.0.0
# ------------------------------------------------------------
# ✔ ranking_snapshot / ranking_raw → pending 生成の唯一の入口
# ✔ ランキング表示情報と、そこから算出できる数値だけで ENTRY 判定
# ✔ SUMMARY / PUSH / 板 / 5秒足 / 日足MA / technical score は使わない
# ✔ 現在値推移・順位改善・連続出現・出来高・売買代金で判定
# ✔ BUY  : ランキング価格が直近高値更新のときのみ許可
# ✔ SELL : ランキング価格が直近安値更新のときのみ許可
# ✔ 急騰急落しすぎた銘柄は追いかけない
# ✔ AI gate は使わず、ランキング専用スコアのみで pending 生成
# ============================================================

from __future__ import annotations

import logging
import datetime as dt
from typing import Dict, Any, Optional, Tuple, List

import pandas as pd

from global_state import global_data
from trading.entry.pending_manager import add_pending
from trading.ranking.side_infer import infer_side_from_rank_type
from AI.entry_row_builder import build_entry_row
from config.ranking_entry_config import RANKING_ENTRY_CONFIG, is_time_allowed

logger = logging.getLogger(__name__)


# ============================================================
# ランキング種別ごとの重み
# ============================================================

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


# ============================================================
# 共通ユーティリティ
# ============================================================

def _now() -> dt.datetime:
    return dt.datetime.now()


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        s = str(v).strip()
        if s == "" or s.lower() == "nan" or s.lower() == "none":
            return default
        s = s.replace(",", "").replace("%", "")
        return float(s)
    except Exception:
        return default


def _safe_int(v: Any, default: int = 999999) -> int:
    try:
        f = _safe_float(v, float(default))
        return int(f)
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


def _reject(reason: str, row: Dict[str, Any], detail: str = "") -> None:
    logger.info(
        "[RANKING DROP] symbol=%s side=%s rank=%s price=%s score=%s reason=%s detail=%s",
        row.get("symbol"),
        row.get("side"),
        row.get("rank_position"),
        row.get("price") or row.get("current_price"),
        row.get("score_total") or row.get("score"),
        reason,
        detail,
    )


# ============================================================
# 入力データ取得・正規化
# ============================================================

def _get_ranking_source_df() -> Optional[pd.DataFrame]:
    snapshot = getattr(global_data, "latest_ranking_snapshot", None)
    if isinstance(snapshot, list) and snapshot:
        logger.info("[RANKING ENTRY LOOP] source=latest_ranking_snapshot rows=%s", len(snapshot))
        return pd.DataFrame(snapshot)

    for name in (
        "latest_ranking_raw",
        "latest_ranking_df",
        "ranking_raw_df",
    ):
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
    # 代表的な表記揺れを吸収
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
    v = _first(
        row,
        (
            "change_rate",
            "change_pct",
            "price_change_rate",
            "rate",
            "前日比率",
            "騰落率",
        ),
        None,
    )
    if v is not None:
        return _safe_float(v, 0.0)

    price = _safe_float(row.get("current_price") or row.get("price"), 0.0)
    prev_close = _safe_float(
        _first(row, ("prev_close", "previous_close", "base_price", "基準値", "前日終値"), 0.0),
        0.0,
    )
    if price > 0 and prev_close > 0:
        return _pct_change(price, prev_close)
    return 0.0


def _normalize_ranking_row_for_entry(row: Dict[str, Any]) -> Dict[str, Any]:
    """
    ranking_snapshot / ranking_raw のカラム名揺れを entry_row_builder が読める形に正規化する。
    """
    row = dict(row)

    symbol = str(_first(row, ("symbol", "Symbol", "code", "コード", "銘柄コード"), "")).strip()
    if symbol:
        row["symbol"] = symbol

    price = _safe_float(
        _first(
            row,
            (
                "close_price",
                "current_price",
                "CurrentPrice",
                "price",
                "close",
                "現在値",
            ),
            0.0,
        ),
        0.0,
    )

    volume = _safe_float(
        _first(
            row,
            (
                "volume",
                "trading_volume",
                "TradingVolume",
                "出来高",
                "売買高",
            ),
            0.0,
        ),
        0.0,
    )

    turnover = _safe_float(
        _first(
            row,
            (
                "turnover",
                "trading_value",
                "TradingValue",
                "売買代金",
                "売買代金上位",
                "value",
                "Value",
            ),
            0.0,
        ),
        0.0,
    )

    if turnover <= 0 and price > 0 and volume > 0:
        turnover = price * volume

    rank_position = _safe_int(
        _first(row, ("rank_position", "rank", "ranking", "順位", "Rank"), 999999),
        999999,
    )

    rank_type = _normalize_rank_type(
        _first(row, ("rank_type", "ranking_type", "type", "ランキング種別", "種別"), "")
    )

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


# ============================================================
# ランキング履歴
# ============================================================

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
        "side": side,
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
    """
    今回のランキングに出なかった銘柄は連続出現を切る。
    履歴自体は少し残すが consecutive は 0 に戻す。
    """
    store = _get_history_store()
    for key, h in list(store.items()):
        if key not in current_keys and isinstance(h, dict):
            h["consecutive"] = 0
            store[key] = h


# ============================================================
# ランキング専用スコア・判定
# ============================================================

def _infer_side(row: Dict[str, Any]) -> str:
    rt = str(row.get("rank_type") or "")
    side = infer_side_from_rank_type(rt)
    if side in ("BUY", "SELL"):
        return side

    day_change = _safe_float(row.get("day_change_pct"), 0.0)
    if day_change < 0:
        return "SELL"
    return "BUY"


def _calc_ranking_only_score(
    row: Dict[str, Any],
    side: str,
    prev_price: float,
    prev_rank: int,
    consecutive: int,
) -> Tuple[float, Dict[str, float]]:
    cfg_rank = RANKING_ENTRY_CONFIG["RANKING"]
    cfg_vol = RANKING_ENTRY_CONFIG["VOLUME"]

    price = _safe_float(row.get("current_price") or row.get("price"), 0.0)
    volume = _safe_float(row.get("volume"), 0.0)
    turnover = _safe_float(row.get("turnover"), 0.0)
    rank_position = _safe_int(row.get("rank_position"), 999999)
    day_change_pct = _safe_float(row.get("day_change_pct"), 0.0)

    max_rank = max(1, int(cfg_rank.get("MAX_RANK_POSITION", 30)))
    min_volume = max(1.0, _safe_float(cfg_vol.get("MIN_VOLUME", 30000), 30000))
    min_turnover = max(1.0, _safe_float(cfg_vol.get("MIN_TURNOVER", 10000000), 10000000))

    # 順位は 1位=満点、MAX_RANK_POSITION=最低点
    rank_score = _clip((max_rank - rank_position + 1) / max_rank, 0.0, 1.0) * 25.0

    turnover_score = _clip(turnover / (min_turnover * 5.0), 0.0, 1.0) * 20.0
    volume_score = _clip(volume / (min_volume * 5.0), 0.0, 1.0) * 15.0

    rank_improve = 0.0
    if prev_rank and prev_rank < 999999:
        # 前回 20位 → 今回 10位なら +10
        rank_improve = float(prev_rank - rank_position)
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

    raw_score = (
        rank_score
        + turnover_score
        + volume_score
        + rank_improve_score
        + price_momentum_score
        + day_direction_score
        + consecutive_score
    )
    final_score = _clip(raw_score * rank_type_weight * market_weight, 0.0, 100.0)

    parts = {
        "rank_score": rank_score,
        "turnover_score": turnover_score,
        "volume_score": volume_score,
        "rank_improve_score": rank_improve_score,
        "price_momentum_score": price_momentum_score,
        "day_direction_score": day_direction_score,
        "consecutive_score": consecutive_score,
        "rank_type_weight": rank_type_weight,
        "market_weight": market_weight,
        "rank_improve": rank_improve,
        "step_pct": step_pct,
    }
    return final_score, parts


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
    rank_type = str(row.get("rank_type") or "")
    day_change_pct = _safe_float(row.get("day_change_pct"), 0.0)

    if not symbol:
        return False, "NO_SYMBOL"

    allow_type = cfg_rank.get("TYPE")
    if allow_type and str(allow_type) not in rank_type:
        return False, f"RANK_TYPE_NG rank_type={rank_type} allow={allow_type}"

    if price < _safe_float(cfg_price.get("MIN", 300), 300) or price > _safe_float(cfg_price.get("MAX", 5000), 5000):
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
        window = max(1, int(cfg_rank.get("PRICE_BREAKOUT_WINDOW", 3)))
        recent = prices[-window:]
        if recent:
            if side == "BUY" and price < max(recent):
                return False, f"BUY_NOT_RECENT_HIGH price={price} recent_high={max(recent)}"
            if side == "SELL" and price > min(recent):
                return False, f"SELL_NOT_RECENT_LOW price={price} recent_low={min(recent)}"

    min_score = _safe_float(cfg_score.get("MIN_ENTRY_SCORE", 70.0), 70.0)
    if score < min_score:
        return False, f"SCORE_NG score={score:.2f} min={min_score:.2f}"

    return True, "OK"


# ============================================================
# メイン処理
# ============================================================

def entry_from_ranking():
    started = dt.datetime.now()
    logger.info("[RANKING ENTRY LOOP] start at=%s mode=RANKING_ONLY", started.strftime("%Y-%m-%d %H:%M:%S"))

    if not is_time_allowed(started):
        logger.info("[RANKING ENTRY LOOP] skip reason=TIME_GUARD now=%s", started.strftime("%H:%M:%S"))
        return 0

    ranking_df = _get_ranking_source_df()
    if ranking_df is None or ranking_df.empty:
        logger.info("[RANKING ENTRY LOOP] skip reason=no_ranking_df")
        return 0

    now = _now()
    rows = _prepare_rows(ranking_df)
    if not rows:
        logger.info("[RANKING ENTRY LOOP] skip reason=no_normalized_rows")
        return 0

    created = 0
    build_reject = 0
    filter_reject = 0
    pending_reject = 0
    reject_samples: List[Dict[str, Any]] = []
    current_keys: set[str] = set()

    # 同一銘柄・同一sideが複数ランキングに出る場合は、scoreが高いものだけ pending する
    best_by_symbol_side: Dict[Tuple[str, str], Dict[str, Any]] = {}

    for row in rows:
        symbol = str(row.get("symbol") or "").strip()
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

        ok, reason = _passes_ranking_only_filters(row, side, prev_h, score, parts)

        # 履歴はフィルターNGでも更新する。次回の価格推移・順位改善判定に使うため。
        _update_history(
            symbol=symbol,
            side=side,
            price=_safe_float(row.get("price") or row.get("current_price"), 0.0),
            rank_position=_safe_int(row.get("rank_position"), 999999),
            rank_type=str(row.get("rank_type") or ""),
            now=now,
        )

        if not ok:
            filter_reject += 1
            if len(reject_samples) < 20:
                reject_samples.append(
                    {
                        "symbol": symbol,
                        "side": side,
                        "rank_type": row.get("rank_type"),
                        "rank": row.get("rank_position"),
                        "price": row.get("price"),
                        "prev_price": prev_price,
                        "volume": row.get("volume"),
                        "turnover": row.get("turnover"),
                        "day_change_pct": row.get("day_change_pct"),
                        "score": round(score, 2),
                        "reason": reason,
                    }
                )
            _reject(reason, row)
            continue

        key = (symbol, side)
        old = best_by_symbol_side.get(key)
        if old is None or score > _safe_float(old.get("score_total"), 0.0):
            best_by_symbol_side[key] = {
                "row": row,
                "parts": parts,
                "prev_price": prev_price,
                "prev_rank": prev_rank,
                "consecutive": consecutive,
            }

    _reset_missing_histories(current_keys)

    for (symbol, side), pack in best_by_symbol_side.items():
        row = pack["row"]
        parts = pack["parts"]
        prev_price = pack["prev_price"]
        prev_rank = pack["prev_rank"]
        consecutive = pack["consecutive"]
        final_score = _safe_float(row.get("score_total"), 0.0)

        entry_row = build_entry_row(row)
        if not entry_row:
            build_reject += 1
            _reject("BUILD_ENTRY_ROW_FAILED", row)
            continue

        entry_row["side"] = side
        entry_row["source"] = "RANKING"
        entry_row["symbol"] = symbol
        entry_row.setdefault("entry_type", "RANKING")
        entry_row.setdefault("interval", 1)
        entry_row["score"] = final_score
        entry_row["score_total"] = final_score
        entry_row["ranking_only_score"] = final_score
        entry_row["ranking_entry_mode"] = "RANKING_ONLY"
        entry_row["ranking_prev_price"] = prev_price
        entry_row["ranking_prev_rank"] = prev_rank
        entry_row["ranking_consecutive"] = consecutive
        entry_row["ranking_step_pct"] = parts.get("step_pct")
        entry_row["ranking_rank_improve"] = parts.get("rank_improve")
        entry_row["ranking_score_parts"] = parts

        pending_entry = {
            **entry_row,
            "source": "RANKING",
            "created_at": now,
            "ranking_fallback_used": False,
            "ranking_strength": final_score,
            "technical_score": 0.0,
            "snapshot_score": final_score,
        }

        if add_pending(pending_entry):
            created += 1
            logger.info(
                "[RANKING PENDING ADD] mode=RANKING_ONLY symbol=%s side=%s rank_type=%s rank=%s price=%.2f prev_price=%.2f step=%.3f%% day=%.3f%% volume=%.0f turnover=%.0f consecutive=%s rank_improve=%.1f score=%.2f",
                symbol,
                side,
                row.get("rank_type"),
                row.get("rank_position"),
                _safe_float(row.get("price") or row.get("current_price"), 0.0),
                prev_price,
                _safe_float(parts.get("step_pct"), 0.0),
                _safe_float(row.get("day_change_pct"), 0.0),
                _safe_float(row.get("volume"), 0.0),
                _safe_float(row.get("turnover"), 0.0),
                consecutive,
                _safe_float(parts.get("rank_improve"), 0.0),
                final_score,
            )
        else:
            pending_reject += 1

    elapsed = (dt.datetime.now() - started).total_seconds()
    if reject_samples:
        logger.warning("[RANKING ENTRY LOOP] ranking_only_reject_samples=%s", reject_samples)

    logger.info(
        "[RANKING ENTRY LOOP] done mode=RANKING_ONLY created=%s total=%s candidates=%s filter_reject=%s build_reject=%s pending_reject=%s elapsed=%.3fs",
        created,
        len(rows),
        len(best_by_symbol_side),
        filter_reject,
        build_reject,
        pending_reject,
        elapsed,
    )

    return created


def run_ranking_entry_pipeline():
    return entry_from_ranking()
