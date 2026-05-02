# ============================================================
# File   : trading/ranking/normalizers.py
# Version: Ver1.1-RANKING-NORMALIZERS-NUMERIC-PARSE-HARDENED
# ------------------------------------------------------------
# ✔ ranking 用の正規化 utility
# ✔ raw / snapshot row 正規化
# ✔ symbol / datetime / float / int 安全化
# ✔ dataframe 変換 helper
# ✔ NEW: comma / percent / signed string 数値を安全に解釈
# ✔ NEW: "1,234" / "12,345.6" / "+3.2" / "-4.5%" 対応
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)


def floor_to_minute(v: dt.datetime | None = None) -> dt.datetime:
    x = v or dt.datetime.now()
    return x.replace(second=0, microsecond=0, tzinfo=None)


def coerce_datetime(value: Any, default: dt.datetime | None = None) -> dt.datetime | None:
    if value is None:
        return default
    try:
        ts = pd.to_datetime(value, errors="coerce")
        if pd.isna(ts):
            return default
        if getattr(ts, "tzinfo", None) is not None:
            try:
                ts = ts.tz_localize(None)
            except Exception:
                try:
                    ts = ts.tz_convert(None)
                except Exception:
                    pass
        py = ts.to_pydatetime()
        return py.replace(second=0, microsecond=0, tzinfo=None)
    except Exception:
        return default


def _clean_numeric_text(value: Any) -> str:
    try:
        if value is None:
            return ""
        s = str(value).strip()
        if not s:
            return ""
        if s.lower() in {"nan", "none", "nat", "null"}:
            return ""

        # 全角カンマや通常カンマ除去
        s = s.replace("，", "").replace(",", "")

        # よくある装飾除去
        s = s.replace("＋", "+").replace("－", "-").replace("−", "-")
        s = s.replace("%", "").replace("％", "")
        s = s.replace("円", "").replace("株", "").replace("口", "")
        s = s.replace("_", "").strip()

        return s
    except Exception:
        return ""


_NUMERIC_RE = re.compile(r"^[+-]?(\d+(\.\d+)?|\.\d+)$")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default

        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                if pd.isna(value):
                    return default
            except Exception:
                pass
            return float(value)

        s = _clean_numeric_text(value)
        if not s:
            return default

        if _NUMERIC_RE.match(s):
            return float(s)

        # 数字以外が混じるケースの最後の保険
        m = re.search(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)", s)
        if m:
            return float(m.group(0))

        return default
    except Exception:
        return default


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default

        if isinstance(value, int) and not isinstance(value, bool):
            return int(value)

        fv = safe_float(value, default=float(default))
        return int(float(fv))
    except Exception:
        return default


def safe_str(value: Any) -> str:
    try:
        if value is None:
            return ""
        s = str(value).strip()
        if s.lower() in {"nan", "none", "nat", "null"}:
            return ""
        return s
    except Exception:
        return ""


def first_non_empty(*values: Any) -> Any:
    for v in values:
        try:
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            return v
        except Exception:
            continue
    return None


def normalize_symbol(value: Any) -> str:
    s = safe_str(value)
    if not s:
        return ""
    if "." in s:
        s = s.split(".", 1)[0].strip()
    return s


def coerce_symbol_list(items: Any) -> list[str]:
    try:
        out = []
        seen = set()
        for x in items or []:
            s = normalize_symbol(x)
            if not s or s in seen:
                continue
            seen.add(s)
            out.append(s)
        return out
    except Exception:
        return []


def safe_len(v: Any) -> int:
    try:
        if isinstance(v, pd.DataFrame):
            return len(v)
        if v is None:
            return 0
        return len(v)
    except Exception:
        return 0


def to_snapshot_df(snapshot_rows: list[dict]) -> pd.DataFrame:
    if not snapshot_rows:
        return pd.DataFrame()

    try:
        df = pd.DataFrame(snapshot_rows)

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [
                "_".join([str(y) for y in tup if str(y) != ""]).strip("_")
                for tup in df.columns
            ]

        df.columns = [str(c).strip() for c in df.columns]
        if df.columns.duplicated().any():
            df = df.loc[:, ~df.columns.duplicated()]

        if "snapshot_time" in df.columns:
            df["snapshot_time"] = pd.to_datetime(df["snapshot_time"], errors="coerce")
        elif "datetime" in df.columns:
            df["snapshot_time"] = pd.to_datetime(df["datetime"], errors="coerce")

        if "symbol" in df.columns:
            df["symbol"] = df["symbol"].map(normalize_symbol)
            df = df[df["symbol"] != ""].copy()

        dedup_cols = [c for c in ["symbol", "snapshot_time", "rank_type", "market"] if c in df.columns]
        if dedup_cols:
            df = df.drop_duplicates(subset=dedup_cols, keep="last")

        if {"symbol", "snapshot_time"}.issubset(df.columns):
            df = df.sort_values(["symbol", "snapshot_time"], kind="stable").reset_index(drop=True)

        return df
    except Exception:
        logger.exception("[RANKING SNAPSHOT] dataframe normalize failed")
        return pd.DataFrame()


def snapshot_df_to_rows(df: pd.DataFrame) -> list[dict]:
    try:
        if df is None or not isinstance(df, pd.DataFrame) or df.empty:
            return []
        out = df.copy()
        if "snapshot_time" in out.columns:
            out["snapshot_time"] = pd.to_datetime(out["snapshot_time"], errors="coerce")
        out = out.where(pd.notna(out), None)
        return out.to_dict("records")
    except Exception:
        logger.exception("[RANKING SNAPSHOT] dataframe -> rows failed")
        return []


def normalize_raw_ranking_rows(
    raw_rows: list[dict],
    symbolname_resolver=None,
    base_time: dt.datetime | None = None,
) -> list[dict]:
    if not raw_rows:
        return []

    out: list[dict] = []
    minute_ts = floor_to_minute(base_time)

    for r in raw_rows:
        try:
            row = dict(r or {})

            symbol = normalize_symbol(row.get("symbol"))
            if not symbol:
                continue

            symbolname = safe_str(row.get("symbolname"))
            if not symbolname and callable(symbolname_resolver):
                symbolname = safe_str(symbolname_resolver(symbol))

            snapshot_time = coerce_datetime(row.get("snapshot_time"), default=minute_ts)
            inserted_at = coerce_datetime(row.get("inserted_at"), default=minute_ts)
            created_at = coerce_datetime(row.get("created_at"), default=minute_ts)

            current_price = safe_float(row.get("current_price"), 0.0)
            change_percentage = safe_float(row.get("change_percentage"), 0.0)
            change_ratio = safe_float(row.get("change_ratio"), 0.0)
            trading_volume = safe_float(row.get("trading_volume"), 0.0)
            trading_value = safe_float(row.get("trading_value"), 0.0)
            turnover = safe_float(row.get("turnover"), trading_value)
            tick_count = safe_int(row.get("tick_count"), 0)

            normalized = {
                "symbol": symbol,
                "symbolname": symbolname or None,
                "rank_type_id": safe_int(row.get("rank_type_id"), 0),
                "rank_type": safe_str(row.get("rank_type")),
                "market": safe_str(row.get("market")),
                "rank_position": safe_int(row.get("rank_position"), 0),
                "value": safe_float(row.get("value"), 0.0),
                "current_price": current_price,
                "change_percentage": change_percentage,
                "change_ratio": change_ratio,
                "trading_volume": trading_volume,
                "trading_value": trading_value,
                "turnover": turnover,
                "tick_count": tick_count,
                "volume_speed": safe_float(row.get("volume_speed"), 0.0),
                "price_delta_1m": safe_float(row.get("price_delta_1m"), 0.0),
                "volume_delta_1m": safe_float(row.get("volume_delta_1m"), 0.0),
                "minute_of_day": safe_int(
                    row.get("minute_of_day"),
                    (snapshot_time.hour * 60 + snapshot_time.minute) if snapshot_time else 0,
                ),
                "snapshot_time": snapshot_time,
                "source": safe_str(row.get("source")) or "KABU_STATION",
                "inserted_at": inserted_at or minute_ts,
                "created_at": created_at or minute_ts,
            }
            out.append(normalized)

        except Exception:
            logger.exception("[RANKING RAW NORMALIZE] row normalize failed")

    return out


def normalize_snapshot_rows_for_db(
    snapshot_rows: list[dict],
    symbolname_resolver=None,
    base_time: dt.datetime | None = None,
) -> list[dict]:
    """
    ranking_snapshot_1min INSERT 用の正規化。
    CRUD 側が期待する最低限のキーを揃える。
    """
    if not snapshot_rows:
        return []

    minute_ts = floor_to_minute(base_time)
    out: list[dict] = []
    seen: set[tuple[str, dt.datetime, str, str]] = set()

    for r in snapshot_rows:
        try:
            row = dict(r or {})

            symbol = normalize_symbol(
                first_non_empty(
                    row.get("symbol"),
                    row.get("code"),
                )
            )
            if not symbol:
                continue

            symbolname = safe_str(
                first_non_empty(
                    row.get("symbolname"),
                    row.get("name"),
                    row.get("symbol_name"),
                )
            )
            if not symbolname and callable(symbolname_resolver):
                symbolname = safe_str(symbolname_resolver(symbol))

            snapshot_time = coerce_datetime(
                first_non_empty(
                    row.get("snapshot_time"),
                    row.get("datetime"),
                ),
                default=minute_ts,
            )

            rank_type = safe_str(
                first_non_empty(
                    row.get("rank_type"),
                    row.get("ranking_type"),
                    row.get("type_name"),
                )
            )
            market = safe_str(row.get("market"))

            rank_position = safe_int(
                first_non_empty(
                    row.get("rank_position"),
                    row.get("rank"),
                ),
                0,
            )

            price = safe_float(
                first_non_empty(
                    row.get("price"),
                    row.get("current_price"),
                    row.get("close"),
                ),
                0.0,
            )

            volume = safe_float(
                first_non_empty(
                    row.get("volume"),
                    row.get("trading_volume"),
                ),
                0.0,
            )

            turnover = safe_float(
                first_non_empty(
                    row.get("turnover"),
                    row.get("trading_value"),
                    row.get("value"),
                ),
                0.0,
            )

            change_rate = safe_float(
                first_non_empty(
                    row.get("change_rate"),
                    row.get("change_percentage"),
                    row.get("change_ratio"),
                ),
                0.0,
            )

            volume_speed = safe_float(row.get("volume_speed"), 0.0)
            prev_price = safe_float(
                first_non_empty(
                    row.get("prev_price"),
                    row.get("previous_close"),
                ),
                0.0,
            )

            dedup_key = (symbol, snapshot_time, rank_type, market)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            out.append(
                {
                    "symbol": symbol,
                    "symbolname": symbolname or None,
                    "rank": rank_position,
                    "rank_position": rank_position,
                    "rank_type": rank_type,
                    "ranking_type": rank_type,
                    "market": market,
                    "price": price,
                    "current_price": price,
                    "change_rate": change_rate,
                    "change_percentage": change_rate,
                    "volume": volume,
                    "trading_volume": volume,
                    "turnover": turnover,
                    "trading_value": turnover,
                    "value": turnover,
                    "volume_speed": volume_speed,
                    "prev_price": prev_price,
                    "snapshot_time": snapshot_time,
                    "datetime": snapshot_time,
                    "source": safe_str(row.get("source")) or "RANKING",
                }
            )
        except Exception:
            logger.exception("[RANKING SNAPSHOT NORMALIZE] row normalize failed")

    return out