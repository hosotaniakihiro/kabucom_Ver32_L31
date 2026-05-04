# ============================================================
# File   : database/crud/crud_ranking.py
# Version: PRODUCTION-FINAL-RANKING-SAVE-FIX-RAW-SNAPSHOT-REV2.0
# ------------------------------------------------------------
# 【概要】
#   kabu Station ランキング取得 rows を保存する本体。
#
# 【保存先】
#   1. 旧カテゴリテーブル
#      - 値上がり率_ALL
#      - 値上がり率_TP
#      - 値下がり率_ALL
#      - 売買代金_ALL
#      - 売買高上位_ALL
#      - TICK回数_ALL
#      など
#
#   2. ranking_snapshot_1min
#      - ランキングサマリー作成用
#
#   3. ranking_raw_1min
#      - ATS / AI / スコア計算用
#
# 【重要】
#   このファイルは「保存本体」。
#   1分毎に保存されるかどうかは、この save_ranking_rows() を
#   scheduler / ranking fetch job 側が毎分呼んでいるかで決まる。
#
# 【REV2.1 FAST SAVE】
#   - 毎分保存では旧カテゴリテーブル保存を既定OFF
#   - ranking_snapshot_1min / ranking_raw_1min を優先
#   - mode="full" または save_legacy=True のときのみ旧カテゴリ保存
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pandas as pd
from sqlalchemy import text
from sqlalchemy.orm import Session

from database.session import Session_ranking
from database.crud.crud_ranking_snapshot import insert_ranking_snapshot_1min
from database.crud.crud_ranking_raw import insert_ranking_raw_1min

logger = logging.getLogger(__name__)


# ============================================================
# settings
# ============================================================

RETRY_MAX = 5
RETRY_SLEEP = 0.30
SQLITE_BUSY_TIMEOUT_MS = 15000


# ============================================================
# ranking type map
# ============================================================

RANK_TYPE_ID_MAP: dict[str, int] = {
    "値上がり率": 1,
    "値上がり": 1,

    "値下がり率": 2,
    "値下がり": 2,

    "売買高上位": 3,
    "売買高": 3,

    "売買高急増": 4,

    "売買代金": 5,

    "売買代金急増": 6,

    "TICK回数": 7,
    "TICK": 7,
    "Tick": 7,
    "tick": 7,
    "ティック": 7,
}


MARKET_NORMALIZE_MAP: dict[str, str] = {
    "": "ALL",
    "ALL": "ALL",
    "全市場": "ALL",
    "全": "ALL",

    "TP": "TP",
    "P": "TP",
    "PRIME": "TP",
    "Prime": "TP",
    "プライム": "TP",

    "TS": "TS",
    "S": "TS",
    "STANDARD": "TS",
    "Standard": "TS",
    "スタンダード": "TS",

    "TG": "TG",
    "G": "TG",
    "GROWTH": "TG",
    "Growth": "TG",
    "グロース": "TG",
}


# ============================================================
# basic helpers
# ============================================================

def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return bool(default)
    return str(v).strip().lower() in {"1", "true", "yes", "on", "y"}


def _is_locked(e: BaseException) -> bool:
    s = str(e).lower()
    return (
        "database is locked" in s
        or "database table is locked" in s
        or "database schema is locked" in s
        or "database is busy" in s
        or "sqlite busy" in s
        or "locked" in s
        or "busy" in s
    )


def _now_min() -> dt.datetime:
    return dt.datetime.now().replace(second=0, microsecond=0, tzinfo=None)


def _safe_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        x = float(str(v).replace(",", "").replace("%", "").replace("％", "").strip())
        if pd.isna(x):
            return None
        return x
    except Exception:
        return None


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None or v == "":
            return None
        return int(float(str(v).replace(",", "").strip()))
    except Exception:
        return None


def _norm_symbol(v: Any) -> str:
    try:
        s = str(v or "").strip()
        if not s:
            return ""

        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]

        # 7203.T のような入力が来ても、DBキーは 7203 に寄せる
        if s.endswith(".T") and s[:-2].isdigit():
            s = s[:-2]

        return s
    except Exception:
        return ""


def _first(*values: Any, default: Any = None) -> Any:
    for v in values:
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return default


def _as_dict(row: Any) -> Optional[dict]:
    """
    dict / pandas Series / object を dict 風に扱う。
    """
    if row is None:
        return None

    if isinstance(row, dict):
        return row

    if hasattr(row, "to_dict"):
        try:
            d = row.to_dict()
            if isinstance(d, dict):
                return d
        except Exception:
            pass

    if hasattr(row, "__dict__"):
        try:
            return dict(row.__dict__)
        except Exception:
            pass

    return None


def _normalize_market(market: Any) -> str:
    s = str(market or "").strip()
    if not s:
        return "ALL"

    return MARKET_NORMALIZE_MAP.get(s, MARKET_NORMALIZE_MAP.get(s.upper(), s))


def _normalize_ranking_type(ranking_type: Any) -> str:
    s = str(ranking_type or "").strip()
    if not s:
        return "不明"
    return s


def _rank_type_id(ranking_type: str) -> int:
    s = str(ranking_type or "").strip()

    if not s:
        return 0

    for key, value in RANK_TYPE_ID_MAP.items():
        if key in s:
            return value

    # 数値コードが来た場合
    try:
        n = int(float(s))
        if n > 0:
            return n
    except Exception:
        pass

    return 0


def _legacy_table_name(ranking_type: str, market: str) -> str:
    rt = _normalize_ranking_type(ranking_type)
    mk = _normalize_market(market)
    return f"{rt}_{mk}"


def _iter_rows(rows: Any) -> Iterable[dict]:
    if rows is None:
        return []

    if isinstance(rows, pd.DataFrame):
        return [
            dict(r)
            for r in rows.to_dict(orient="records")
            if isinstance(r, dict)
        ]

    if isinstance(rows, dict):
        # 単一行 dict として扱う
        return [rows]

    try:
        out = []
        for r in rows:
            d = _as_dict(r)
            if isinstance(d, dict):
                out.append(d)
        return out
    except Exception:
        return []


# ============================================================
# category table schema
# ============================================================

def _ensure_category_table(session: Session, table: str) -> None:
    """
    旧カテゴリテーブルを作成し、不足列を追加する。
    """
    session.execute(
        text(
            f"""
            CREATE TABLE IF NOT EXISTS "{table}" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rank INTEGER,
                symbol TEXT,
                symbolname TEXT,
                current_price REAL,
                change_percentage REAL,
                change_ratio REAL,
                trading_volume REAL,
                trading_value REAL,
                turnover REAL,
                tick_count INTEGER,
                inserted_at TEXT
            )
            """
        )
    )

    try:
        existing = {
            str(r[1])
            for r in session.execute(text(f'PRAGMA table_info("{table}")')).fetchall()
        }
    except Exception:
        existing = set()

    required = {
        "rank": "INTEGER",
        "symbol": "TEXT",
        "symbolname": "TEXT",
        "current_price": "REAL",
        "change_percentage": "REAL",
        "change_ratio": "REAL",
        "trading_volume": "REAL",
        "trading_value": "REAL",
        "turnover": "REAL",
        "tick_count": "INTEGER",
        "inserted_at": "TEXT",
    }

    added = 0

    for col, typ in required.items():
        if col in existing:
            continue

        try:
            session.execute(text(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {typ}'))
            added += 1
            logger.warning(
                "[RANKING SAVE] added column table=%s column=%s type=%s",
                table,
                col,
                typ,
            )
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                logger.exception(
                    "[RANKING SAVE] add column failed table=%s column=%s",
                    table,
                    col,
                )

    if added:
        try:
            session.commit()
        except Exception:
            session.rollback()


# ============================================================
# row normalizer
# ============================================================

def _extract_symbol(r: dict) -> str:
    return _norm_symbol(
        _first(
            r.get("symbol"),
            r.get("Symbol"),
            r.get("code"),
            r.get("Code"),
            r.get("銘柄コード"),
            r.get("銘柄"),
        )
    )


def _extract_symbolname(r: dict) -> str:
    return str(
        _first(
            r.get("symbolname"),
            r.get("symbol_name"),
            r.get("SymbolName"),
            r.get("name"),
            r.get("Name"),
            r.get("銘柄名"),
            default="",
        )
        or ""
    ).strip()


def _extract_rank(r: dict, fallback: int) -> int:
    return (
        _safe_int(
            _first(
                r.get("rank"),
                r.get("rank_position"),
                r.get("rank_no"),
                r.get("No"),
                r.get("Rank"),
                r.get("順位"),
                default=fallback,
            )
        )
        or fallback
    )


def _extract_current_price(r: dict) -> Optional[float]:
    return _safe_float(
        _first(
            r.get("current_price"),
            r.get("CurrentPrice"),
            r.get("price"),
            r.get("Price"),
            r.get("close"),
            r.get("close_price"),
            r.get("現在値"),
            r.get("株価"),
        )
    )


def _extract_change_percentage(r: dict) -> Optional[float]:
    return _safe_float(
        _first(
            r.get("change_percentage"),
            r.get("ChangePercentage"),
            r.get("change_rate"),
            r.get("rate"),
            r.get("change_percent"),
            r.get("change_pct"),
            r.get("騰落率"),
            r.get("値上がり率"),
            r.get("値下がり率"),
        )
    )


def _extract_change_ratio(r: dict) -> Optional[float]:
    return _safe_float(
        _first(
            r.get("change_ratio"),
            r.get("ChangeRatio"),
            r.get("change"),
            r.get("前日比"),
        )
    )


def _extract_trading_volume(r: dict) -> Optional[float]:
    return _safe_float(
        _first(
            r.get("trading_volume"),
            r.get("TradingVolume"),
            r.get("volume"),
            r.get("Volume"),
            r.get("出来高"),
            r.get("売買高"),
        )
    )


def _extract_trading_value(r: dict) -> Optional[float]:
    return _safe_float(
        _first(
            r.get("trading_value"),
            r.get("TradingValue"),
            r.get("amount"),
            r.get("value"),
            r.get("売買代金"),
            r.get("売買金額"),
        )
    )


def _extract_turnover(r: dict, trading_value: Optional[float]) -> Optional[float]:
    return _safe_float(
        _first(
            r.get("turnover"),
            r.get("Turnover"),
            r.get("trading_value"),
            r.get("TradingValue"),
            trading_value,
        )
    )


def _extract_tick_count(r: dict) -> Optional[int]:
    return _safe_int(
        _first(
            r.get("tick_count"),
            r.get("TickCount"),
            r.get("tick"),
            r.get("ticks"),
            r.get("TICK回数"),
            r.get("約定回数"),
        )
    )
def _normalize_rows(
    rows: Any,
    *,
    ranking_type: str,
    market: str,
    snapshot_time: Optional[dt.datetime] = None,
) -> Tuple[list[dict], list[dict], list[dict]]:
    """
    API取得 rows を3種類の保存形式に正規化する。

    Returns
    -------
    category_rows:
      旧カテゴリテーブル用

    snapshot_rows:
      ranking_snapshot_1min 用

    raw_rows:
      ranking_raw_1min 用
    """
    now = snapshot_time or _now_min()
    rt = _normalize_ranking_type(ranking_type)
    mk = _normalize_market(market)
    rank_type_id = _rank_type_id(rt)

    category_rows: list[dict] = []
    snapshot_rows: list[dict] = []
    raw_rows: list[dict] = []

    src_rows = list(_iter_rows(rows))

    for i, r in enumerate(src_rows, start=1):
        if not isinstance(r, dict):
            continue

        symbol = _extract_symbol(r)
        if not symbol:
            continue

        rank = _extract_rank(r, i)
        symbolname = _extract_symbolname(r)

        current_price = _extract_current_price(r)
        change_percentage = _extract_change_percentage(r)
        change_ratio = _extract_change_ratio(r)
        trading_volume = _extract_trading_volume(r)
        trading_value = _extract_trading_value(r)
        turnover = _extract_turnover(r, trading_value)
        tick_count = _extract_tick_count(r)

        change_rate = change_percentage if change_percentage is not None else change_ratio

        category_rows.append(
            {
                "rank": rank,
                "symbol": symbol,
                "symbolname": symbolname,
                "current_price": current_price,
                "change_percentage": change_percentage,
                "change_ratio": change_ratio,
                "trading_volume": trading_volume,
                "trading_value": trading_value,
                "turnover": turnover,
                "tick_count": tick_count,
                "inserted_at": now.isoformat(sep=" "),
            }
        )

        snapshot_rows.append(
            {
                "symbol": symbol,
                "symbolname": symbolname,
                "rank": rank,
                "rank_type": rt,
                "ranking_type": rt,
                "market": mk,
                "price": current_price,
                "change_rate": change_rate,
                "volume": trading_volume,
                "turnover": turnover,
                "category": f"{rt}_{mk}",
                "snapshot_time": now,
                "datetime": now,
            }
        )

        raw_rows.append(
            {
                "symbol": symbol,
                "snapshot_time": now,
                "symbolname": symbolname,
                "rank_type": rt,
                "rank_type_id": rank_type_id,
                "market": mk,
                "rank_position": rank,
                "current_price": current_price,
                "change_percentage": change_percentage,
                "change_ratio": change_ratio,
                "trading_volume": trading_volume,
                "trading_value": trading_value,
                "turnover": turnover,
                "tick_count": tick_count,
                "volume_speed": _safe_float(r.get("volume_speed")),
                "price_delta_1m": _safe_float(r.get("price_delta_1m")),
                "volume_delta_1m": _safe_float(r.get("volume_delta_1m")),
                "minute_of_day": now.hour * 60 + now.minute,
                "source": "kabu_station_ranking",
                "inserted_at": now,
                "created_at": now,
            }
        )

    return category_rows, snapshot_rows, raw_rows


# ============================================================
# legacy category save
# ============================================================

def _save_category_rows(
    session: Session,
    *,
    table: str,
    rows: list[dict],
    ranking_type: str,
    market: str,
) -> int:
    """
    旧カテゴリテーブルへ保存する。
    """
    if not rows:
        return 0

    _ensure_category_table(session, table)

    sql = text(
        f"""
        INSERT INTO "{table}" (
            rank,
            symbol,
            symbolname,
            current_price,
            change_percentage,
            change_ratio,
            trading_volume,
            trading_value,
            turnover,
            tick_count,
            inserted_at
        )
        VALUES (
            :rank,
            :symbol,
            :symbolname,
            :current_price,
            :change_percentage,
            :change_ratio,
            :trading_volume,
            :trading_value,
            :turnover,
            :tick_count,
            :inserted_at
        )
        """
    )

    for attempt in range(1, RETRY_MAX + 1):
        try:
            session.execute(text(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}"))
            session.execute(sql, rows)
            session.commit()

            logger.info(
                "[RANKING SAVE] category OK table=%s rows=%d type=%s market=%s attempt=%d/%d",
                table,
                len(rows),
                ranking_type,
                market,
                attempt,
                RETRY_MAX,
            )
            return len(rows)

        except Exception as e:
            try:
                session.rollback()
            except Exception:
                pass

            if _is_locked(e) and attempt < RETRY_MAX:
                logger.warning(
                    "[RANKING SAVE] category locked retry table=%s type=%s market=%s attempt=%d/%d sleep=%.2fs",
                    table,
                    ranking_type,
                    market,
                    attempt,
                    RETRY_MAX,
                    RETRY_SLEEP,
                )
                time.sleep(RETRY_SLEEP)
                continue

            logger.exception(
                "❌ save_ranking_rows category failed table=%s type=%s market=%s",
                table,
                ranking_type,
                market,
            )
            return 0

    return 0


# ============================================================
# public API
# ============================================================

def save_ranking_rows(
    rows: Any,
    ranking_type: str,
    market: str = "ALL",
    *,
    snapshot_time: Optional[dt.datetime] = None,
    mode: str = "fast",
    save_legacy: Optional[bool] = None,
    save_snapshot: bool = True,
    save_raw: bool = True,
) -> dict[str, Any]:
    """
    kabu Station ランキング rows を保存する。

    FAST方針:
      - 毎分ジョブでは mode="fast" を使う
      - fast では旧カテゴリテーブル保存を既定OFF
      - ranking_snapshot_1min / ranking_raw_1min を優先保存

    FULL方針:
      - mode="full" または save_legacy=True で旧カテゴリも保存
    """
    started = time.perf_counter()

    mode = str(mode or "fast").lower().strip()
    if mode not in {"fast", "full"}:
        mode = "fast"

    rt = _normalize_ranking_type(ranking_type)
    mk = _normalize_market(market)

    if save_legacy is None:
        save_legacy = (
            mode == "full"
            or _env_bool("RANKING_SAVE_LEGACY_EVERY_MINUTE", False)
        )

    category_rows, snapshot_rows, raw_rows = _normalize_rows(
        rows,
        ranking_type=rt,
        market=mk,
        snapshot_time=snapshot_time,
    )

    logger.info(
        "[RANKING SAVE] normalize mode=%s save_legacy=%s type=%s market=%s input=%s category=%d snapshot=%d raw=%d symbols=%d",
        mode,
        save_legacy,
        rt,
        mk,
        len(list(_iter_rows(rows))) if rows is not None else 0,
        len(category_rows),
        len(snapshot_rows),
        len(raw_rows),
        len({r.get("symbol") for r in snapshot_rows if r.get("symbol")}),
    )

    if not snapshot_rows and not raw_rows and not category_rows:
        logger.warning(
            "[RANKING SAVE] skipped empty after normalize type=%s market=%s",
            rt,
            mk,
        )
        return {
            "ok": False,
            "mode": mode,
            "ranking_type": rt,
            "market": mk,
            "category_rows": 0,
            "snapshot_rows": 0,
            "raw_rows": 0,
            "saved_category": 0,
            "saved_snapshot": 0,
            "saved_raw": 0,
            "elapsed_sec": time.perf_counter() - started,
        }

    saved_category = 0
    saved_snapshot = 0
    saved_raw = 0

    if save_legacy and category_rows:
        table = _legacy_table_name(rt, mk)
        session = Session_ranking()
        try:
            saved_category = _save_category_rows(
                session,
                table=table,
                rows=category_rows,
                ranking_type=rt,
                market=mk,
            )
        finally:
            try:
                session.close()
            except Exception:
                pass
    else:
        logger.info(
            "[RANKING SAVE] category skipped mode=%s save_legacy=%s type=%s market=%s rows=%d",
            mode,
            save_legacy,
            rt,
            mk,
            len(category_rows),
        )

    if save_snapshot and snapshot_rows:
        try:
            saved_snapshot = int(insert_ranking_snapshot_1min(snapshot_rows) or 0)
            logger.info(
                "[RANKING SAVE] snapshot saved rows=%d type=%s market=%s",
                saved_snapshot,
                rt,
                mk,
            )
        except Exception:
            logger.exception(
                "[RANKING SAVE] snapshot save failed type=%s market=%s rows=%d",
                rt,
                mk,
                len(snapshot_rows),
            )

    if save_raw and raw_rows:
        try:
            saved_raw = int(insert_ranking_raw_1min(raw_rows) or 0)
            logger.info(
                "[RANKING SAVE] raw saved rows=%d type=%s market=%s",
                saved_raw,
                rt,
                mk,
            )
        except Exception:
            logger.exception(
                "[RANKING SAVE] raw save failed type=%s market=%s rows=%d",
                rt,
                mk,
                len(raw_rows),
            )

    elapsed = time.perf_counter() - started

    ok = bool(saved_category or saved_snapshot or saved_raw or snapshot_rows or raw_rows)

    logger.info(
        "[RANKING SAVE] done mode=%s type=%s market=%s ok=%s saved_category=%d saved_snapshot=%d saved_raw=%d elapsed=%.3fs",
        mode,
        rt,
        mk,
        ok,
        saved_category,
        saved_snapshot,
        saved_raw,
        elapsed,
    )

    return {
        "ok": ok,
        "mode": mode,
        "ranking_type": rt,
        "market": mk,
        "category_rows": len(category_rows),
        "snapshot_rows": len(snapshot_rows),
        "raw_rows": len(raw_rows),
        "saved_category": saved_category,
        "saved_snapshot": saved_snapshot,
        "saved_raw": saved_raw,
        "elapsed_sec": elapsed,
    }


# ============================================================
# compatibility aliases
# ============================================================

def save_rows(
    rows: Any,
    ranking_type: str,
    market: str = "ALL",
    **kwargs,
) -> dict[str, Any]:
    return save_ranking_rows(
        rows,
        ranking_type,
        market,
        **kwargs,
    )


def insert_ranking_rows(
    rows: Any,
    ranking_type: str,
    market: str = "ALL",
    **kwargs,
) -> dict[str, Any]:
    return save_ranking_rows(
        rows,
        ranking_type,
        market,
        **kwargs,
    )


# ============================================================
# public read
# ============================================================

def get_latest_ranking(
    ranking_type: str = "値上がり率",
    market: str = "ALL",
    limit: int = 50,
) -> pd.DataFrame:
    """
    最新の旧カテゴリ別ランキングテーブルを読み込む。

    例:
      get_latest_ranking("値上がり率", "ALL")
      -> 値上がり率_ALL の最新 inserted_at 行を返す

    Notes:
      - database/crud/__init__.py から import される互換API
      - 旧カテゴリテーブルが空でも落とさず empty DataFrame を返す
      - FAST保存で旧カテゴリ保存をOFFにしている場合、この関数は空を返すことがある
    """
    rt = _normalize_ranking_type(ranking_type)
    mk = _normalize_market(market)
    table = _legacy_table_name(rt, mk)

    session = Session_ranking()

    try:
        session.execute(text(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}"))

        # テーブルが無いだけで import / 起動を落とさないため保証する
        _ensure_category_table(session, table)

        latest = session.execute(
            text(
                f"""
                SELECT MAX(inserted_at)
                  FROM "{table}"
                 WHERE inserted_at IS NOT NULL
                """
            )
        ).scalar()

        if not latest:
            logger.info(
                "[RANKING READ] no latest row table=%s type=%s market=%s",
                table,
                rt,
                mk,
            )
            return pd.DataFrame()

        rows = session.execute(
            text(
                f"""
                SELECT *
                  FROM "{table}"
                 WHERE inserted_at = :latest
                 ORDER BY rank ASC, id ASC
                 LIMIT :limit
                """
            ),
            {
                "latest": latest,
                "limit": int(limit),
            },
        ).mappings().all()

        df = pd.DataFrame([dict(r) for r in rows])

        logger.info(
            "[RANKING READ] latest ranking loaded table=%s rows=%d latest=%s",
            table,
            len(df),
            latest,
        )

        return df

    except Exception:
        logger.exception("[RANKING READ] failed table=%s", table)
        return pd.DataFrame()

    finally:
        try:
            session.close()
        except Exception:
            pass


def get_top_symbols(
    ranking_type: str = "値上がり率",
    market: str = "ALL",
    limit: int = 50,
) -> List[str]:
    """
    最新ランキングから symbol だけを抽出する。
    """
    df = get_latest_ranking(
        ranking_type=ranking_type,
        market=market,
        limit=limit,
    )

    if df.empty or "symbol" not in df.columns:
        return []

    out: List[str] = []
    seen: set[str] = set()

    for x in df["symbol"].tolist():
        s = _norm_symbol(x)
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)

    return out


def get_top_symbols_from_ranking(
    ranking_type: str = "値上がり率",
    market: str = "ALL",
    limit: int = 50,
) -> List[str]:
    """
    旧コード互換API。
    """
    return get_top_symbols(
        ranking_type=ranking_type,
        market=market,
        limit=limit,
    )


def save_ranking_rows_and_snapshot(
    rows: Any,
    ranking_type: str,
    market: str = "ALL",
) -> int:
    """
    旧コード互換API。

    現在の save_ranking_rows は category / snapshot / raw を同時保存する。
    """
    return save_ranking_rows(
        rows=rows,
        ranking_type=ranking_type,
        market=market,
    )


# ============================================================
# exports
# ============================================================

__all__ = [
    "save_ranking_rows",
    "save_ranking_rows_and_snapshot",
    "get_latest_ranking",
    "get_top_symbols",
    "get_top_symbols_from_ranking",
]