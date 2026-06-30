# ============================================================
# File   : trading/ranking/collectors.py
# Version: Ver2.1-RANKING-COLLECTORS-ONE-MINUTE-SPREAD
# ------------------------------------------------------------
# ✔ ranking API 収集
# ✔ raw_rows / snapshot_rows 生成
# ✔ ETF 除外
# ✔ volume speed 計算
# ✔ rank_type / ranking_type / category を snapshot に保持
# ✔ type別 / market別 件数ログ
# ✔ API call ごとの elapsed ログ
# ✔ 1 call 失敗でも全体継続
# ✔ 売買高 / 売買高上位 表記ゆれ吸収
# ✔ 429対策: 7項目×4市場=28 calls を1分に分散して順番取得
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from collections import Counter
from typing import Any

from .api_client import get_data_from_api
from .normalizers import (
    floor_to_minute,
    safe_float,
    safe_int,
    safe_str,
    first_non_empty,
    normalize_symbol,
    normalize_raw_ranking_rows,
    normalize_snapshot_rows_for_db,
)
from .runtime_state import resolve_symbolname_from_global

logger = logging.getLogger(__name__)

# ============================================================
# ranking type master
# ============================================================

TYPE_TO_NAME = {
    1: "値上がり率",
    2: "値下がり率",
    3: "売買高上位",
    4: "売買代金",
    5: "TICK回数",
    6: "売買高急増",
    7: "売買代金急増",
}

EXCHANGE_DIVISIONS = {
    "ALL": "全市場",
    "TP": "東証プライム",
    "TS": "東証スタンダード",
    "TG": "東証グロース",
}

# 以前は0.05秒で28回を一気に叩いていたため429が出やすかった。
# 既定では58秒に分散し、次の1分足更新に間に合う範囲でAPI呼び出しを平準化する。
DEFAULT_SPREAD_WINDOW_SEC = 58.0

_last_rank_state: dict[str, dict[str, Any]] = {}


# ============================================================
# env helpers
# ============================================================

def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).replace(",", ""))
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool = True) -> bool:
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


def _ranking_call_plan() -> list[tuple[int, str, str]]:
    plan: list[tuple[int, str, str]] = []
    for type_id, type_name_raw in TYPE_TO_NAME.items():
        type_name = normalize_rank_type_name(type_name_raw)
        for exch in EXCHANGE_DIVISIONS.keys():
            plan.append((int(type_id), str(type_name), str(exch)))
    return plan


def _ranking_api_spacing_sec(total_calls: int) -> float:
    """Return per-call wait time.

    RANKING_API_CALL_SLEEP_SEC を明示した場合はそれを優先。
    未指定なら RANKING_API_SPREAD_WINDOW_SEC 秒に total_calls を分散する。
    """
    explicit = os.getenv("RANKING_API_CALL_SLEEP_SEC")
    if explicit is not None and str(explicit).strip() != "":
        return max(0.0, _env_float("RANKING_API_CALL_SLEEP_SEC", 0.0))
    if not _env_bool("RANKING_API_SPREAD_OVER_MINUTE", True):
        return 0.05
    window = max(0.0, _env_float("RANKING_API_SPREAD_WINDOW_SEC", DEFAULT_SPREAD_WINDOW_SEC))
    if total_calls <= 1:
        return 0.0
    return max(0.0, window / float(max(total_calls - 1, 1)))


# ============================================================
# helpers
# ============================================================

def is_etf_symbol(symbol: str, symbolname: str | None = None) -> bool:
    try:
        s = str(symbol)
        if s.startswith(("13", "15", "16", "17")):
            return True
        if symbolname and (
            "ETF" in symbolname
            or "上場投信" in symbolname
            or "ETN" in symbolname
            or "REIT" in symbolname
            or "REIT受益証券" in symbolname
        ):
            return True
        return False
    except Exception:
        return False


def normalize_rank_type_name(type_name: Any) -> str:
    s = safe_str(type_name)
    if not s:
        return "不明"
    if s == "売買高":
        return "売買高上位"
    return s


def calc_volume_speed(symbol: str, volume_now: float, now_time: dt.datetime) -> float:
    prev = _last_rank_state.get(symbol)
    if not prev:
        _last_rank_state[symbol] = {"volume": volume_now, "time": now_time}
        return 0.0

    try:
        prev_volume = float(prev.get("volume", 0) or 0)
    except Exception:
        prev_volume = 0.0

    try:
        volume_now_f = float(volume_now or 0)
    except Exception:
        volume_now_f = 0.0

    try:
        prev_time = prev.get("time")
        minutes = max((now_time - prev_time).total_seconds() / 60.0, 1e-6)
    except Exception:
        minutes = 1e-6

    delta = max(volume_now_f - prev_volume, 0.0)
    _last_rank_state[symbol] = {"volume": volume_now_f, "time": now_time}
    return delta / minutes


def _type_name_from_row(row: dict) -> str:
    try:
        return str(
            first_non_empty(
                row.get("rank_type"),
                row.get("ranking_type"),
                row.get("category"),
                "?",
            )
        )
    except Exception:
        return "?"


def _market_name_from_row(row: dict) -> str:
    try:
        return str(first_non_empty(row.get("market"), row.get("exchange"), "ALL"))
    except Exception:
        return "ALL"


def _log_type_counts(label: str, rows: list[dict]) -> None:
    try:
        cnt = Counter(_type_name_from_row(r) for r in (rows or []))
        logger.info("[%s] type_counts=%s", label, dict(cnt))
    except Exception:
        logger.exception("[%s] type_counts log failed", label)


def _log_market_counts(label: str, rows: list[dict]) -> None:
    try:
        cnt = Counter(_market_name_from_row(r) for r in (rows or []))
        logger.info("[%s] market_counts=%s", label, dict(cnt))
    except Exception:
        logger.exception("[%s] market_counts log failed", label)


def _log_type_market_counts(label: str, rows: list[dict]) -> None:
    try:
        cnt = Counter(
            (str(_type_name_from_row(r)), str(_market_name_from_row(r)))
            for r in (rows or [])
        )
        logger.info("[%s] type_market_counts=%s", label, dict(cnt))
    except Exception:
        logger.exception("[%s] type_market_counts log failed", label)


def _sleep_between_ranking_calls(call_index: int, total_calls: int, spacing_sec: float) -> None:
    if call_index >= total_calls:
        return
    if spacing_sec <= 0:
        return
    try:
        logger.info(
            "[RANKING API SPREAD] sleep %.3fs next_call=%s/%s",
            float(spacing_sec),
            int(call_index) + 1,
            int(total_calls),
        )
        time.sleep(float(spacing_sec))
    except Exception:
        pass


# ============================================================
# main collect
# ============================================================

def collect_ranking_rows(
    now_dt: dt.datetime,
    snapshot_mgr,
) -> tuple[list[dict], list[dict]]:
    raw_rows: list[dict] = []
    snapshot_rows: list[dict] = []
    minute_now = floor_to_minute(now_dt)

    total_api_calls = 0
    total_api_success = 0
    total_api_empty = 0
    total_api_fail = 0

    collect_t0 = time.perf_counter()
    plan = _ranking_call_plan()
    total_planned = len(plan)
    spacing_sec = _ranking_api_spacing_sec(total_planned)

    logger.info(
        "[RANKING COLLECT] start minute=%s planned_calls=%s spread_enabled=%s spacing=%.3fs window=%.1fs",
        minute_now,
        total_planned,
        _env_bool("RANKING_API_SPREAD_OVER_MINUTE", True),
        spacing_sec,
        spacing_sec * max(total_planned - 1, 0),
    )

    for call_index, (type_id, type_name, exch) in enumerate(plan, start=1):
        total_api_calls += 1
        api_t0 = time.perf_counter()

        try:
            logger.info(
                "[RANKING API] start minute=%s call=%s/%s type=%s market=%s spacing=%.3fs",
                minute_now,
                call_index,
                total_planned,
                type_name,
                exch,
                spacing_sec,
            )
            data = get_data_from_api({"type": type_id, "ExchangeDivision": exch})
            api_elapsed = time.perf_counter() - api_t0

        except Exception:
            total_api_fail += 1
            logger.exception(
                "[RANKING API] fetch failed minute=%s call=%s/%s type=%s market=%s",
                minute_now,
                call_index,
                total_planned,
                type_name,
                exch,
            )
            _sleep_between_ranking_calls(call_index, total_planned, spacing_sec)
            continue

        if not isinstance(data, dict):
            total_api_fail += 1
            logger.warning(
                "[RANKING API] invalid response minute=%s call=%s/%s type=%s market=%s elapsed=%.3fs data_type=%s",
                minute_now,
                call_index,
                total_planned,
                type_name,
                exch,
                api_elapsed,
                type(data).__name__,
            )
            _sleep_between_ranking_calls(call_index, total_planned, spacing_sec)
            continue

        rows = data.get("Ranking")
        if not rows:
            total_api_empty += 1
            logger.info(
                "[RANKING API] empty minute=%s call=%s/%s type=%s market=%s elapsed=%.3fs",
                minute_now,
                call_index,
                total_planned,
                type_name,
                exch,
                api_elapsed,
            )
            _sleep_between_ranking_calls(call_index, total_planned, spacing_sec)
            continue

        total_api_success += 1

        try:
            sample = rows[0] if rows else {}
            logger.info(
                "[RANKING API] done minute=%s call=%s/%s type=%s market=%s rows=%s elapsed=%.3fs sample_keys=%s",
                minute_now,
                call_index,
                total_planned,
                type_name,
                exch,
                len(rows),
                api_elapsed,
                sorted(list(sample.keys())) if isinstance(sample, dict) else [],
            )
        except Exception:
            logger.exception("[RANKING API] sample key log failed type=%s market=%s", type_name, exch)

        for idx, r in enumerate(rows, start=1):
            symbol = first_non_empty(
                r.get("Symbol"),
                r.get("symbol"),
                r.get("Code"),
                r.get("code"),
            )
            if not symbol:
                continue

            symbol_norm = normalize_symbol(symbol)

            symbolname = safe_str(
                first_non_empty(
                    r.get("IssueName"),
                    r.get("SymbolName"),
                    r.get("symbolname"),
                    r.get("symbol_name"),
                    r.get("Name"),
                    r.get("name"),
                )
            )
            if not symbolname:
                symbolname = resolve_symbolname_from_global(symbol_norm)

            current_price = safe_float(
                first_non_empty(
                    r.get("CurrentPrice"),
                    r.get("current_price"),
                    r.get("Price"),
                    r.get("price"),
                ),
                0.0,
            )

            change_percentage = safe_float(
                first_non_empty(
                    r.get("ChangePercentage"),
                    r.get("change_percentage"),
                    r.get("ChangeRate"),
                    r.get("change_rate"),
                ),
                0.0,
            )

            change_ratio = safe_float(
                first_non_empty(
                    r.get("ChangeRatio"),
                    r.get("change_ratio"),
                ),
                0.0,
            )

            trading_volume = safe_float(
                first_non_empty(
                    r.get("TradingVolume"),
                    r.get("trading_volume"),
                    r.get("Volume"),
                    r.get("volume"),
                ),
                0.0,
            )

            trading_value = safe_float(
                first_non_empty(
                    r.get("TradingValue"),
                    r.get("trading_value"),
                    r.get("Value"),
                    r.get("value"),
                ),
                0.0,
            )

            turnover = safe_float(
                first_non_empty(
                    r.get("Turnover"),
                    r.get("turnover"),
                    r.get("TradingValue"),
                    r.get("trading_value"),
                ),
                trading_value,
            )

            tick_count = safe_int(
                first_non_empty(
                    r.get("TickCount"),
                    r.get("tick_count"),
                ),
                0,
            )

            speed = calc_volume_speed(symbol_norm, trading_volume, minute_now)

            raw_rows.append(
                {
                    "symbol": symbol_norm,
                    "symbolname": symbolname or None,
                    "rank_type_id": type_id,
                    "rank_type": type_name,
                    "ranking_type": type_name,
                    "category": type_name,
                    "market": exch,
                    "exchange": exch,
                    "rank_position": idx,
                    "rank": idx,
                    "value": change_percentage if change_percentage != 0.0 else change_ratio,
                    "current_price": current_price,
                    "price": current_price,
                    "change_percentage": change_percentage,
                    "change_ratio": change_ratio,
                    "change_rate": change_percentage if change_percentage != 0.0 else change_ratio,
                    "trading_volume": trading_volume,
                    "volume": trading_volume,
                    "trading_value": trading_value,
                    "turnover": turnover,
                    "tick_count": tick_count,
                    "volume_speed": speed,
                    "price_delta_1m": 0.0,
                    "volume_delta_1m": 0.0,
                    "minute_of_day": minute_now.hour * 60 + minute_now.minute,
                    "snapshot_time": minute_now,
                    "datetime": minute_now,
                    "source": "KABU_STATION",
                    "inserted_at": minute_now,
                    "created_at": minute_now,
                }
            )

            if is_etf_symbol(symbol_norm, symbolname):
                continue
            try:
                snap = snapshot_mgr.add(
                    symbol=symbol_norm,
                    symbolname=symbolname or "",
                    rank_type=type_name,
                    market=exch,
                    price=current_price,
                    volume=trading_volume,
                    volume_speed=speed,
                    change_rate=change_percentage if change_percentage != 0.0 else change_ratio,
                    prev_price=r.get("PreviousClose"),
                    now=minute_now,
                    source=f"RANKING_{type_name}",
                )
                if isinstance(snap, dict) and snap:
                    snap = dict(snap)
                    snap["symbol"] = symbol_norm
                    snap["symbolname"] = symbolname or snap.get("symbolname") or ""
                    snap["rank_type_id"] = type_id
                    snap["rank_type"] = type_name
                    snap["ranking_type"] = type_name
                    snap["category"] = type_name
                    snap["market"] = exch
                    snap["exchange"] = exch
                    snap["rank_position"] = idx
                    snap["rank"] = idx
                    snap["current_price"] = current_price
                    snap["price"] = current_price
                    snap["change_percentage"] = change_percentage
                    snap["change_ratio"] = change_ratio
                    snap["change_rate"] = change_percentage if change_percentage != 0.0 else change_ratio
                    snap["trading_volume"] = trading_volume
                    snap["volume"] = trading_volume
                    snap["trading_value"] = trading_value
                    snap["turnover"] = turnover
                    snap["tick_count"] = tick_count
                    snap["volume_speed"] = speed
                    snap["snapshot_time"] = minute_now
                    snap["datetime"] = minute_now
                    snap["inserted_at"] = minute_now
                    snap["created_at"] = minute_now
                    snapshot_rows.append(snap)
            except Exception:
                logger.exception(
                    "[RANKING SNAPSHOT] add failed symbol=%s type=%s market=%s minute=%s",
                    symbol_norm,
                    type_name,
                    exch,
                    minute_now,
                )

        _sleep_between_ranking_calls(call_index, total_planned, spacing_sec)

    _log_type_counts("RANKING COLLECT RAW-BEFORE-NORMALIZE", raw_rows)
    _log_market_counts("RANKING COLLECT RAW-BEFORE-NORMALIZE", raw_rows)
    _log_type_market_counts("RANKING COLLECT RAW-BEFORE-NORMALIZE", raw_rows)

    _log_type_counts("RANKING COLLECT SNAPSHOT-BEFORE-NORMALIZE", snapshot_rows)
    _log_market_counts("RANKING COLLECT SNAPSHOT-BEFORE-NORMALIZE", snapshot_rows)
    _log_type_market_counts("RANKING COLLECT SNAPSHOT-BEFORE-NORMALIZE", snapshot_rows)

    raw_rows = normalize_raw_ranking_rows(
        raw_rows,
        symbolname_resolver=resolve_symbolname_from_global,
        base_time=minute_now,
    )
    snapshot_rows = normalize_snapshot_rows_for_db(
        snapshot_rows,
        symbolname_resolver=resolve_symbolname_from_global,
        base_time=minute_now,
    )

    _log_type_counts("RANKING COLLECT RAW-AFTER-NORMALIZE", raw_rows)
    _log_market_counts("RANKING COLLECT RAW-AFTER-NORMALIZE", raw_rows)
    _log_type_market_counts("RANKING COLLECT RAW-AFTER-NORMALIZE", raw_rows)

    _log_type_counts("RANKING COLLECT SNAPSHOT-AFTER-NORMALIZE", snapshot_rows)
    _log_market_counts("RANKING COLLECT SNAPSHOT-AFTER-NORMALIZE", snapshot_rows)
    _log_type_market_counts("RANKING COLLECT SNAPSHOT-AFTER-NORMALIZE", snapshot_rows)

    logger.info(
        "[RANKING COLLECT] done minute=%s raw_rows=%s snapshot_rows=%s api_calls=%s api_success=%s api_empty=%s api_fail=%s elapsed=%.3fs spacing=%.3fs spread_window=%.1fs",
        minute_now,
        len(raw_rows),
        len(snapshot_rows),
        total_api_calls,
        total_api_success,
        total_api_empty,
        total_api_fail,
        time.perf_counter() - collect_t0,
        spacing_sec,
        spacing_sec * max(total_planned - 1, 0),
    )

    return raw_rows, snapshot_rows
