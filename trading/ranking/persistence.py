# ============================================================
# File   : trading/ranking/persistence.py
# Version: Ver1.7-RANKING-PERSISTENCE-SAVE-LEGACY-CONTROLLED
# ------------------------------------------------------------
# ✔ ranking raw / snapshot DB 保存
# ✔ DB lock retry
# ✔ ranking engine 安全解決
# ✔ build_ranking_tables_from_snapshot 呼び出し
# ✔ ranking_raw_1min を更新
# ✔ ranking_snapshot_1min を更新
# ✔ 旧テーブル（値上がり率_ALL / TICK回数_ALL 等）保存対応
# ✔ 旧テーブル保存は save_legacy=True のときだけ実行
# ✔ FAST保存では raw/snapshot を優先し、legacy を任意化
# ✔ FULL保存や5分ジョブでは legacy 保存可能
# ✔ ranking_ma_1min 更新は builder 側へ委譲
# ✔ rank_type / ranking_type / category の再補完
# ✔ type別件数ログ
# ✔ builder 前後ログ強化
# ✔ snapshot_time 欠損時の安全補完
# ✔ ranking snapshot は rankingDB へ完全固定
# ✔ PUSH writer / pushDB への混入を完全排除
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from collections import Counter, defaultdict
from typing import Any, Callable

from database import session as db_session_module
from database.crud.crud_ranking import save_ranking_rows
from database.crud.crud_ranking_raw import insert_ranking_raw_1min

from trading.ranking.ranking_table_builder import build_ranking_tables_from_snapshot
from trading.ranking.snapshot_writer import save_ranking_snapshot_rows

from .normalizers import (
    normalize_raw_ranking_rows,
    normalize_snapshot_rows_for_db,
)
from .runtime_state import (
    ensure_global_defaults,
    get_global_data,
    resolve_symbolname_from_global,
)

logger = logging.getLogger(__name__)

ensure_global_defaults()
global_data = get_global_data()

# DBへの実書き込みを短時間だけ直列化するロック
ranking_db_write_lock = threading.RLock()

# builderは重いので snapshot保存とは別ロックにする
ranking_builder_lock = threading.Lock()

DB_LOCK_RETRY_MAX = 10
DB_LOCK_RETRY_SLEEP_BASE_SEC = 0.35
DB_LOCK_RETRY_SLEEP_MAX_SEC = 3.0


# ============================================================
# env helpers
# ============================================================

def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return bool(default)

    return str(value).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "y",
    }


# ============================================================
# lock / retry helpers
# ============================================================

def is_database_locked_error(exc: BaseException) -> bool:
    try:
        msg = str(exc).lower()
        return (
            "database is locked" in msg
            or "database table is locked" in msg
            or "database schema is locked" in msg
            or "database is busy" in msg
            or "busy" in msg
            or "locked" in msg
        )
    except Exception:
        return False


def sleep_with_backoff(attempt_index: int) -> float:
    sleep_sec = min(
        DB_LOCK_RETRY_SLEEP_BASE_SEC * (2 ** max(attempt_index, 0)),
        DB_LOCK_RETRY_SLEEP_MAX_SEC,
    )
    time.sleep(sleep_sec)
    return sleep_sec


def call_with_db_lock_retry(
    fn: Callable[..., Any],
    *args,
    op_name: str = "db_op",
    retry_max: int = DB_LOCK_RETRY_MAX,
    use_write_lock: bool = False,
    **kwargs,
) -> Any:
    last_exc: BaseException | None = None

    for attempt in range(retry_max):
        try:
            if use_write_lock:
                with ranking_db_write_lock:
                    return fn(*args, **kwargs)
            return fn(*args, **kwargs)

        except Exception as exc:
            last_exc = exc

            if not is_database_locked_error(exc):
                raise

            slept = sleep_with_backoff(attempt)
            logger.warning(
                "[RANKING DB] %s retry=%d/%d sleep=%.2fs cause=%s",
                op_name,
                attempt + 1,
                retry_max,
                slept,
                exc,
            )

    if last_exc is not None:
        raise last_exc

    return None


# ============================================================
# datetime helpers
# ============================================================

def _parse_dt_safe(value: Any) -> dt.datetime | None:
    if value is None or value == "":
        return None

    if isinstance(value, dt.datetime):
        return value

    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min)

    try:
        s = str(value).strip()
        if not s:
            return None

        s = s.replace("T", " ")

        if "+" in s:
            s = s.split("+", 1)[0].strip()

        if s.endswith("Z"):
            s = s[:-1].strip()

        if "." in s:
            s = s.split(".", 1)[0]

        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y/%m/%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y/%m/%d %H:%M",
            "%Y%m%d %H:%M:%S",
            "%Y%m%d %H:%M",
        ):
            try:
                return dt.datetime.strptime(s, fmt)
            except Exception:
                pass

        return dt.datetime.fromisoformat(s)

    except Exception:
        return None


def _floor_to_minute(value: Any) -> Any:
    d = _parse_dt_safe(value)
    if d is None:
        return value

    d = d.replace(second=0, microsecond=0)
    return d.strftime("%Y-%m-%d %H:%M:%S")


def _minute_key(value: Any) -> str:
    v = _floor_to_minute(value)
    return str(v) if v is not None else ""


def _force_minute_timestamp_fields(rows: list[dict], base_time=None) -> list[dict]:
    """
    ranking_snapshot_1min / ranking_raw_1min の時刻を必ず分丸めする。

    目的:
      - 09:07:01 / 09:07:03 のような秒ズレを防ぐ
      - GROUP BY datetime で間欠に見える問題を防ぐ
      - snapshot_time と datetime の不一致を防ぐ
    """
    if not rows:
        return []

    base = _floor_to_minute(base_time) if base_time is not None else None
    out: list[dict] = []

    for r in rows:
        if not isinstance(r, dict):
            continue

        row = dict(r)

        t = (
            row.get("snapshot_time")
            or row.get("datetime")
            or row.get("created_at")
            or base
        )

        t = _floor_to_minute(t)

        if t is not None:
            row["snapshot_time"] = t
            row["datetime"] = t

        out.append(row)

    return out


# ============================================================
# engine resolve
# ============================================================

def resolve_ranking_engine():
    """
    rankingDB 用 engine を安全に取得する。
    PUSH DB engine はここでは一切扱わない。
    """
    try:
        eng = getattr(db_session_module, "ranking_engine", None)
        if eng is not None:
            return eng
    except Exception:
        pass

    try:
        eng = getattr(db_session_module, "_ranking_engine", None)
        if eng is not None:
            return eng
    except Exception:
        pass

    try:
        getter = getattr(db_session_module, "get_ranking_engine", None)
        if callable(getter):
            eng = getter()
            if eng is not None:
                return eng
    except Exception:
        pass

    logger.warning("⚠ [RANKING DB] ranking_engine is not available")
    return None


# ============================================================
# log helpers
# ============================================================

def _row_type_name(row: dict) -> str:
    try:
        return str(
            row.get("rank_type")
            or row.get("ranking_type")
            or row.get("category")
            or "?"
        )
    except Exception:
        return "?"


def _row_market_name(row: dict) -> str:
    try:
        return str(row.get("market") or row.get("exchange") or "ALL")
    except Exception:
        return "ALL"


def _log_type_counts(label: str, rows: list[dict]) -> None:
    try:
        cnt = Counter(_row_type_name(r) for r in (rows or []))
        logger.info("[%s] type_counts=%s", label, dict(cnt))
    except Exception:
        logger.exception("[%s] type_counts log failed", label)


def _log_market_counts(label: str, rows: list[dict]) -> None:
    try:
        cnt = Counter(_row_market_name(r) for r in (rows or []))
        logger.info("[%s] market_counts=%s", label, dict(cnt))
    except Exception:
        logger.exception("[%s] market_counts log failed", label)


def _safe_snapshot_time_range(rows: list[dict]) -> tuple[Any, Any]:
    try:
        vals = [
            _minute_key(x.get("snapshot_time") or x.get("datetime") or x.get("created_at"))
            for x in rows
            if isinstance(x, dict)
            and (
                x.get("snapshot_time") is not None
                or x.get("datetime") is not None
                or x.get("created_at") is not None
            )
        ]
        vals = [v for v in vals if v]
        if not vals:
            return None, None
        return min(vals), max(vals)
    except Exception:
        return None, None


def _safe_nonnull_count(rows: list[dict], key: str) -> int:
    try:
        return sum(
            1
            for r in (rows or [])
            if isinstance(r, dict) and r.get(key) not in (None, "")
        )
    except Exception:
        return -1


def _safe_len(v: Any) -> int:
    try:
        return len(v)
    except Exception:
        return 0


# ============================================================
# row patch helpers
# ============================================================

def _backfill_snapshot_type_fields(rows: list[dict]) -> list[dict]:
    out: list[dict] = []

    for r in rows or []:
        if not isinstance(r, dict):
            continue

        row = dict(r)

        type_name = (
            row.get("rank_type")
            or row.get("ranking_type")
            or row.get("category")
            or ""
        )

        if type_name:
            row["rank_type"] = type_name
            row["ranking_type"] = type_name
            row["category"] = type_name

        if row.get("market") in (None, ""):
            row["market"] = row.get("exchange") or "ALL"

        if row.get("exchange") in (None, ""):
            row["exchange"] = row.get("market") or "ALL"

        if row.get("price") is None and row.get("current_price") is not None:
            row["price"] = row.get("current_price")
        if row.get("current_price") is None and row.get("price") is not None:
            row["current_price"] = row.get("price")

        if row.get("volume") is None and row.get("trading_volume") is not None:
            row["volume"] = row.get("trading_volume")
        if row.get("trading_volume") is None and row.get("volume") is not None:
            row["trading_volume"] = row.get("volume")

        if row.get("turnover") is None and row.get("trading_value") is not None:
            row["turnover"] = row.get("trading_value")
        if row.get("trading_value") is None and row.get("turnover") is not None:
            row["trading_value"] = row.get("turnover")

        if row.get("change_percentage") is None and row.get("change_rate") is not None:
            row["change_percentage"] = row.get("change_rate")
        if row.get("change_rate") is None and row.get("change_percentage") is not None:
            row["change_rate"] = row.get("change_percentage")

        if row.get("snapshot_time") is None and row.get("datetime") is not None:
            row["snapshot_time"] = row.get("datetime")
        if row.get("datetime") is None and row.get("snapshot_time") is not None:
            row["datetime"] = row.get("snapshot_time")

        if row.get("source") in (None, ""):
            row["source"] = "ranking"

        out.append(row)

    return out


def _convert_to_legacy_api_rows(raw_rows: list[dict]) -> dict[tuple[str, str], list[dict]]:
    """
    旧テーブル保存用に
    (ranking_type, market) ごとへ rows を変換する。
    """
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)

    for r in raw_rows or []:
        if not isinstance(r, dict):
            continue

        ranking_type = str(
            r.get("rank_type")
            or r.get("ranking_type")
            or r.get("category")
            or ""
        ).strip()
        if not ranking_type:
            continue

        market = str(r.get("market") or r.get("exchange") or "ALL").strip() or "ALL"

        grouped[(ranking_type, market)].append(
            {
                "Symbol": r.get("symbol"),
                "SymbolName": r.get("symbolname"),
                "CurrentPrice": r.get("current_price", r.get("price")),
                "ChangePercentage": r.get("change_percentage", r.get("change_rate")),
                "TradingVolume": r.get("trading_volume", r.get("volume")),
                "TradingValue": r.get("trading_value", r.get("turnover")),
                "Turnover": r.get("turnover", r.get("trading_value")),
                "TickCount": r.get("tick_count"),
            }
        )

    return grouped


def _assert_no_push_writer_payload(rows: list[dict], label: str) -> None:
    """
    ranking persistence に PUSH payload が混ざっていないかを軽く検査する。
    """
    try:
        if not rows:
            return

        suspicious = 0
        for r in rows[:20]:
            if not isinstance(r, dict):
                continue

            if any(k in r for k in ("CurrentPriceTime", "Sell1", "Buy1", "ExchangeName")):
                suspicious += 1

        if suspicious > 0:
            logger.warning(
                "[%s] suspicious push-like payload detected count=%s sample_checked=%s",
                label,
                suspicious,
                min(len(rows), 20),
            )
    except Exception:
        logger.exception("[%s] push-like payload check failed", label)


def _extract_snapshot_time(snapshot_rows: list[dict], now_dt=None):
    """
    builder に渡す snapshot_time を安全に決める。
    """
    if now_dt is not None:
        return _floor_to_minute(now_dt)

    try:
        if snapshot_rows:
            v = (
                snapshot_rows[0].get("snapshot_time")
                or snapshot_rows[0].get("datetime")
                or snapshot_rows[0].get("created_at")
            )
            if v is not None:
                return _floor_to_minute(v)
    except Exception:
        pass

    dt_min, _ = _safe_snapshot_time_range(snapshot_rows)
    return _floor_to_minute(dt_min)


# ============================================================
# legacy save
# ============================================================

def save_legacy_rows(raw_rows: list[dict]) -> int:
    """
    旧系テーブル（値上がり率_ALL / TICK回数_ALL 等）へ保存する。

    注意:
      - この関数は save_legacy=True のときだけ呼ぶ
      - 毎分FAST保存では通常呼ばない
      - 5分ごと / FULL保存 / 手動保存で呼ぶ想定
    """
    if not raw_rows:
        logger.info("[RANKING LEGACY SAVE] skipped: empty raw_rows")
        return 0

    grouped = _convert_to_legacy_api_rows(raw_rows)
    if not grouped:
        logger.warning("[RANKING LEGACY SAVE] skipped: no grouped rows")
        return 0

    total_groups = 0
    total_rows = 0
    skipped_groups = 0

    for (ranking_type, market), rows in grouped.items():
        if not rows:
            continue

        try:
            # legacy table は補助なので長時間待たない
            call_with_db_lock_retry(
                save_ranking_rows,
                rows=rows,
                ranking_type=ranking_type,
                market=market,
                op_name=f"save_legacy_rows:{ranking_type}_{market}",
                retry_max=2,
                use_write_lock=True,
            )

            total_groups += 1
            total_rows += len(rows)

            logger.info(
                "[RANKING LEGACY SAVE] done type=%s market=%s rows=%s",
                ranking_type,
                market,
                len(rows),
            )

        except Exception as exc:
            if is_database_locked_error(exc):
                skipped_groups += 1
                logger.warning(
                    "[RANKING LEGACY SAVE] skipped by db locked type=%s market=%s rows=%s cause=%s",
                    ranking_type,
                    market,
                    len(rows),
                    exc,
                )
                continue

            logger.exception(
                "[RANKING LEGACY SAVE] failed type=%s market=%s rows=%s",
                ranking_type,
                market,
                len(rows),
            )

    logger.info(
        "[RANKING LEGACY SAVE] finished groups=%s rows=%s skipped_groups=%s",
        total_groups,
        total_rows,
        skipped_groups,
    )
    return total_rows


# ============================================================
# raw save
# ============================================================

def save_raw_rows(
    raw_rows: list[dict],
    now_dt=None,
    save_legacy: bool = False,
) -> int:
    """
    ranking_raw_1min を rankingDB に保存する。
    PUSH DB / PUSH writer とは完全分離。

    Parameters
    ----------
    raw_rows:
        ranking API 由来の raw rows。

    now_dt:
        保存対象分。None の場合は rows から補完。

    save_legacy:
        True の場合のみ、旧カテゴリ別テーブル
        値上がり率_ALL / TICK回数_ALL 等へも保存する。

        FAST毎分保存では False 推奨。
        5分ごと保存や FULL保存では True 推奨。
    """
    if not raw_rows:
        logger.info("[RANKING RAW SAVE] skipped: empty raw_rows")
        return 0

    _assert_no_push_writer_payload(raw_rows, "RANKING RAW SAVE INPUT")

    raw_rows = normalize_raw_ranking_rows(
        raw_rows,
        symbolname_resolver=resolve_symbolname_from_global,
        base_time=now_dt,
    )
    raw_rows = _backfill_snapshot_type_fields(raw_rows)
    raw_rows = _force_minute_timestamp_fields(raw_rows, base_time=now_dt)

    if not raw_rows:
        logger.warning("[RANKING RAW SAVE] skipped after normalize: empty")
        return 0

    _log_type_counts("RANKING RAW SAVE", raw_rows)
    _log_market_counts("RANKING RAW SAVE", raw_rows)

    try:
        sample_keys = sorted(list(raw_rows[0].keys()))
    except Exception:
        sample_keys = []

    try:
        logger.info(
            "[RANKING RAW SAVE] start rows=%s save_legacy=%s sample_keys=%s sample_row=%s",
            len(raw_rows),
            save_legacy,
            sample_keys,
            raw_rows[0],
        )
    except Exception:
        logger.info(
            "[RANKING RAW SAVE] start rows=%s save_legacy=%s sample_keys=%s",
            len(raw_rows),
            save_legacy,
            sample_keys,
        )

    ranking_engine = resolve_ranking_engine()
    if ranking_engine is None:
        raise RuntimeError("ranking_engine is not available")

    try:
        logger.info("[RANKING RAW SAVE] engine=%s", getattr(ranking_engine, "url", None))
    except Exception:
        pass

    call_with_db_lock_retry(
        insert_ranking_raw_1min,
        ranking_engine,
        rows=raw_rows,
        op_name="insert_ranking_raw_1min",
        use_write_lock=True,
    )

    if save_legacy:
        try:
            save_legacy_rows(raw_rows)
        except Exception:
            logger.exception("[RANKING LEGACY SAVE] failed in save_raw_rows")
    else:
        logger.info("[RANKING LEGACY SAVE] skipped save_legacy=False")

    dt_min, dt_max = _safe_snapshot_time_range(raw_rows)

    logger.info(
        "[RANKING RAW SAVE] done rows=%s dt_min=%s dt_max=%s symbolname_nonnull=%s rank_type_nonnull=%s market_nonnull=%s save_legacy=%s",
        len(raw_rows),
        dt_min,
        dt_max,
        _safe_nonnull_count(raw_rows, "symbolname"),
        _safe_nonnull_count(raw_rows, "rank_type"),
        _safe_nonnull_count(raw_rows, "market"),
        save_legacy,
    )
    return len(raw_rows)


# ============================================================
# snapshot save + builder
# ============================================================

def _save_snapshot_only(snapshot_rows: list[dict]) -> dict[str, Any]:
    """
    snapshot_writer を retry + write_lock 経由で呼ぶ。
    """
    save_result = call_with_db_lock_retry(
        save_ranking_snapshot_rows,
        snapshot_rows,
        op_name="save_ranking_snapshot_rows",
        use_write_lock=True,
    )

    if not isinstance(save_result, dict):
        logger.warning(
            "[RANKING SNAPSHOT SAVE] snapshot_writer returned non-dict result=%r",
            save_result,
        )
        return {
            "ok": False,
            "saved_rows": 0,
            "error": "snapshot_writer returned non-dict",
            "raw_result": repr(save_result),
        }

    return save_result


def _run_builder_nonblocking(snapshot_time: Any, inserted_snapshot: int) -> bool:
    """
    builder を多重起動しない。
    ただし builder 実行中でも次回 snapshot 保存は止めない。

    戻り値:
      True  = builder 実行した
      False = builder 実行中のためスキップ
    """
    acquired = ranking_builder_lock.acquire(blocking=False)
    if not acquired:
        logger.warning(
            "[RANKING SNAPSHOT SAVE] builder skipped because previous builder still running snapshot_time=%s inserted_snapshot=%s",
            snapshot_time,
            inserted_snapshot,
        )
        return False

    try:
        logger.info(
            "[RANKING SNAPSHOT SAVE] builder start snapshot_time=%s inserted_snapshot=%s",
            snapshot_time,
            inserted_snapshot,
        )

        call_with_db_lock_retry(
            build_ranking_tables_from_snapshot,
            snapshot_time,
            op_name="build_ranking_tables_from_snapshot",
            use_write_lock=False,
        )

        logger.info(
            "[RANKING SNAPSHOT SAVE] builder done snapshot_time=%s",
            snapshot_time,
        )
        return True

    except Exception:
        logger.exception(
            "[RANKING SNAPSHOT SAVE] builder failed snapshot_time=%s",
            snapshot_time,
        )
        return True

    finally:
        try:
            ranking_builder_lock.release()
        except Exception:
            pass


def save_snapshot_and_build(snapshot_rows: list[dict], now_dt=None) -> int:
    """
    ranking_snapshot_1min を rankingDB に保存し、
    保存成功時のみ ranking_table_builder を実行する。

    重要:
      - PUSH DB には一切保存しない
      - PUSH writer は一切呼ばない
      - snapshot保存は必ず先に短時間ロックで完了させる
      - builder は snapshot保存ロックの外で実行する
      - builder 実行中でも次の snapshot保存を止めない
    """
    if not snapshot_rows:
        logger.info("[RANKING SNAPSHOT SAVE] skipped: empty snapshot_rows")
        return 0

    _assert_no_push_writer_payload(snapshot_rows, "RANKING SNAPSHOT SAVE INPUT")

    _log_type_counts("RANKING SNAPSHOT SAVE BEFORE-NORMALIZE", snapshot_rows)
    _log_market_counts("RANKING SNAPSHOT SAVE BEFORE-NORMALIZE", snapshot_rows)

    snapshot_rows = normalize_snapshot_rows_for_db(
        snapshot_rows,
        symbolname_resolver=resolve_symbolname_from_global,
        base_time=now_dt,
    )
    snapshot_rows = _backfill_snapshot_type_fields(snapshot_rows)
    snapshot_rows = _force_minute_timestamp_fields(snapshot_rows, base_time=now_dt)

    if not snapshot_rows:
        logger.warning("[RANKING SNAPSHOT SAVE] skipped after normalize: empty")
        return 0

    _log_type_counts("RANKING SNAPSHOT SAVE AFTER-NORMALIZE", snapshot_rows)
    _log_market_counts("RANKING SNAPSHOT SAVE AFTER-NORMALIZE", snapshot_rows)

    snapshot_time = _extract_snapshot_time(snapshot_rows, now_dt=now_dt)

    if snapshot_time is None:
        raise ValueError("snapshot_time missing in normalized snapshot_rows")

    try:
        logger.info(
            "[RANKING SNAPSHOT SAVE] start rows=%s snapshot_time=%s "
            "symbolname_nonnull=%s rank_type_nonnull=%s market_nonnull=%s "
            "price_nonnull=%s volume_nonnull=%s turnover_nonnull=%s sample_row=%s",
            len(snapshot_rows),
            snapshot_time,
            _safe_nonnull_count(snapshot_rows, "symbolname"),
            _safe_nonnull_count(snapshot_rows, "rank_type"),
            _safe_nonnull_count(snapshot_rows, "market"),
            _safe_nonnull_count(snapshot_rows, "price"),
            _safe_nonnull_count(snapshot_rows, "volume"),
            _safe_nonnull_count(snapshot_rows, "turnover"),
            snapshot_rows[0],
        )
    except Exception:
        logger.info(
            "[RANKING SNAPSHOT SAVE] start rows=%s snapshot_time=%s",
            len(snapshot_rows),
            snapshot_time,
        )

    save_result = _save_snapshot_only(snapshot_rows)

    if not save_result.get("ok"):
        logger.warning(
            "[RANKING SNAPSHOT SAVE] snapshot_writer failed -> builder skipped result=%s",
            save_result,
        )
        try:
            global_data.ranking_pipeline_available = False
            global_data.ranking_last_snapshot_save_result = save_result
        except Exception:
            pass
        return 0

    inserted_snapshot = int(
        save_result.get("saved_rows")
        or save_result.get("normalized_rows")
        or len(snapshot_rows)
        or 0
    )

    try:
        global_data.ranking_pipeline_available = True
        global_data.ranking_last_snapshot_save_result = save_result
    except Exception:
        pass

    logger.info(
        "[RANKING SNAPSHOT SAVE] snapshot_writer done snapshot_time=%s saved=%s result=%s",
        snapshot_time,
        inserted_snapshot,
        save_result,
    )

    _run_builder_nonblocking(snapshot_time, inserted_snapshot)

    logger.info(
        "[RANKING SNAPSHOT SAVE] done rows=%s snapshot_time=%s saved=%s",
        len(snapshot_rows),
        snapshot_time,
        inserted_snapshot,
    )
    return inserted_snapshot


# ============================================================
# public high-level API
# ============================================================

def save_ranking_data(
    *,
    raw_rows: list[dict] | None = None,
    snapshot_rows: list[dict] | None = None,
    now_dt=None,
    save_raw: bool = True,
    save_snapshot: bool = True,
    build_after_snapshot: bool = True,
    save_legacy: bool = False,
) -> dict[str, Any]:
    """
    ranking 保存の高水準 API

    用途:
      - snapshot_rows は ranking_snapshot_1min へ保存
      - snapshot 保存後に ranking_table_builder を実行可能
      - raw_rows は ranking_raw_1min へ保存
      - save_legacy=True のときだけ旧カテゴリテーブルも保存

    運用推奨:
      毎分FAST:
        save_snapshot=True
        save_raw=True
        build_after_snapshot=False
        save_legacy=False

      5分/FULL:
        save_snapshot=True
        save_raw=True
        build_after_snapshot=True
        save_legacy=True
    """
    raw_rows = list(raw_rows or [])
    snapshot_rows = list(snapshot_rows or [])

    result: dict[str, Any] = {
        "ok": True,
        "raw_saved": 0,
        "snapshot_saved": 0,
        "builder_ran": False,
        "snapshot_first": True,
        "legacy_saved": 0,
        "save_legacy": bool(save_legacy),
    }

    try:
        if save_snapshot and snapshot_rows:
            if build_after_snapshot:
                try:
                    before_builder_locked = ranking_builder_lock.locked()

                    result["snapshot_saved"] = save_snapshot_and_build(
                        snapshot_rows,
                        now_dt=now_dt,
                    )

                    result["builder_ran"] = (
                        result["snapshot_saved"] > 0
                        and not before_builder_locked
                    )

                except Exception:
                    logger.exception("[RANKING SAVE] snapshot save/build failed")
                    result["ok"] = False
            else:
                try:
                    normalized_snapshot_rows = normalize_snapshot_rows_for_db(
                        snapshot_rows,
                        symbolname_resolver=resolve_symbolname_from_global,
                        base_time=now_dt,
                    )
                    normalized_snapshot_rows = _backfill_snapshot_type_fields(
                        normalized_snapshot_rows
                    )
                    normalized_snapshot_rows = _force_minute_timestamp_fields(
                        normalized_snapshot_rows,
                        base_time=now_dt,
                    )

                    if normalized_snapshot_rows:
                        save_result = _save_snapshot_only(normalized_snapshot_rows)

                        if isinstance(save_result, dict) and save_result.get("ok"):
                            result["snapshot_saved"] = int(
                                save_result.get("saved_rows")
                                or save_result.get("normalized_rows")
                                or _safe_len(normalized_snapshot_rows)
                            )
                        else:
                            logger.warning(
                                "[RANKING SAVE] snapshot no_builder failed result=%s",
                                save_result,
                            )
                            result["ok"] = False
                    else:
                        logger.warning(
                            "[RANKING SAVE] snapshot no_builder skipped after normalize: empty"
                        )

                except Exception:
                    logger.exception("[RANKING SAVE] snapshot no_builder failed")
                    result["ok"] = False

        if save_raw and raw_rows:
            try:
                result["raw_saved"] = save_raw_rows(
                    raw_rows,
                    now_dt=now_dt,
                    save_legacy=save_legacy,
                )

                if save_legacy:
                    # save_raw_rows 内で legacy 保存済み。
                    # 厳密件数は raw_saved と同等扱いにする。
                    result["legacy_saved"] = result["raw_saved"]
            except Exception:
                logger.exception("[RANKING SAVE] raw save failed")
                result["ok"] = False

        logger.info(
            "[RANKING SAVE] done ok=%s snapshot_saved=%s raw_saved=%s legacy_saved=%s builder_ran=%s snapshot_first=%s save_legacy=%s",
            result["ok"],
            result["snapshot_saved"],
            result["raw_saved"],
            result["legacy_saved"],
            result["builder_ran"],
            result["snapshot_first"],
            result["save_legacy"],
        )
        return result

    except Exception:
        logger.exception("[RANKING SAVE] failed")
        result["ok"] = False
        return result


__all__ = [
    "save_raw_rows",
    "save_snapshot_and_build",
    "save_ranking_data",
    "save_legacy_rows",
    "resolve_ranking_engine",
    "call_with_db_lock_retry",
    "is_database_locked_error",
]