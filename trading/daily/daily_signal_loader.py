# ============================================================
# File   : trading/daily/daily_signal_loader.py
# Version: PRODUCTION-STABLE-REV1.1-NAS-KABU-DAILY-DB
# ------------------------------------------------------------
# 目的:
#   日足分析DB:
#     \\192.168.0.22\kabu\stock_data\daily_db\stock_analysis.db
#
#   の stock_analysis_latest を読み、
#   自動売買のエントリー / EXIT 判定で使える日足情報を付与する。
#
# 重要:
#   - 場中に stock_analysis_history 全件は読まない
#   - 基本は stock_analysis_latest のみ参照
#   - 候補銘柄だけ WHERE stock_code IN (...) で読む
#   - 読み取り専用 mode=ro で接続する
# ============================================================

from __future__ import annotations

import os
import sqlite3
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any, Iterable, List

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# DB 設定
# ============================================================

DAILY_DB_PATH = r"\\192.168.0.22\kabu\stock_data\daily_db\stock_analysis.db"

DB_TABLE_LATEST = "stock_analysis_latest"
DB_TABLE_HISTORY = "stock_analysis_history"


# 自動売買で使う主要列だけ読む
DAILY_USE_COLUMNS = [
    "stock_code",
    "stock_name",
    "market",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",

    "MA_5",
    "MA_25",
    "MA_75",
    "MA_200",
    "MA_5_Slope",
    "MA_25_Slope",
    "MA_75_Slope",
    "MA_200_Slope",

    "RSI",
    "MACD",
    "MACD_Signal",
    "MACD_Diff",

    "ATR_14",
    "ATR_14_Pct",

    "change_1d_pct",
    "change_3d_pct",
    "change_5d_pct",
    "change_last_week_pct",
    "change_last_month_pct",
    "change_last_year_pct",

    "turnover",

    "Signal_Golden_Cross",
    "Signal_Dead_Cross",
    "Signal_New_High_10",
    "Signal_New_Low_10",
    "Signal_Break_Res_10",
    "Signal_Break_Support_10",
    "Signal_MA25_Lower_Dev",
    "Signal_MA25_Upper_Dev",
    "Signal_Trendline_Break",
    "Signal_First_Pullback",
    "Signal_Engulfing_Bull",
    "Signal_Three_White_Soldiers",
    "Signal_Three_Black_Crows",
    "Signal_Evening_Star",
    "Signal_Shooting_Star_as_Bear",
    "Signal_Tweezers_Top",

    "Limit_Up_Permanent",
    "Limit_Up_Touched",
    "Limit_Down_Permanent",
    "Limit_Down_Touched",
]


@dataclass(frozen=True)
class DailyDecision:
    symbol: str
    name: str = ""
    date: str = ""

    daily_ok_buy: bool = False
    daily_ok_sell: bool = False
    daily_exit_warn: bool = False

    daily_score: float = 0.0
    daily_buy_score: float = 0.0
    daily_sell_score: float = 0.0

    close: float = 0.0
    ma25: float = 0.0
    ma75: float = 0.0
    rsi: float = 0.0
    macd: float = 0.0
    macd_signal: float = 0.0

    reason: str = ""


# ============================================================
# Utility
# ============================================================

def _to_symbol4(x: Any) -> str:
    s = str(x).strip()
    if s.endswith(".T"):
        s = s[:-2]
    if s.isdigit():
        return s.zfill(4)
    return s


def _num(row: pd.Series, col: str, default: float = 0.0) -> float:
    if col not in row.index:
        return default
    try:
        v = pd.to_numeric(row.get(col), errors="coerce")
        if pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def _flag(row: pd.Series, col: str) -> bool:
    return _num(row, col, 0.0) > 0


def _safe_date_text(v: Any) -> str:
    if v is None or pd.isna(v):
        return ""
    try:
        return pd.Timestamp(v).strftime("%Y-%m-%d")
    except Exception:
        return str(v)


def daily_db_exists(db_path: str = DAILY_DB_PATH) -> bool:
    return os.path.isfile(db_path)


# ============================================================
# DB 読み取り
# ============================================================

def _get_existing_columns(
    db_path: str = DAILY_DB_PATH,
    table: str = DB_TABLE_LATEST,
) -> List[str]:
    if not os.path.isfile(db_path):
        return []

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        try:
            rows = con.execute(f'PRAGMA table_info("{table}")').fetchall()
            return [str(r[1]) for r in rows]
        finally:
            con.close()
    except Exception as e:
        logger.warning("[DAILY DB] column check failed path=%s table=%s err=%s", db_path, table, e)
        return []


def load_daily_latest(
    symbols: Optional[Iterable[str]] = None,
    db_path: str = DAILY_DB_PATH,
    use_columns: Optional[List[str]] = None,
) -> pd.DataFrame:
    """
    stock_analysis_latest を読む。

    symbols が指定された場合:
      候補銘柄だけ読む

    symbols が None の場合:
      全銘柄の latest を読む
    """

    if not os.path.isfile(db_path):
        logger.warning("[DAILY DB] not found path=%s", db_path)
        return pd.DataFrame()

    existing_cols = _get_existing_columns(db_path=db_path, table=DB_TABLE_LATEST)
    if not existing_cols:
        logger.warning("[DAILY DB] no columns found table=%s path=%s", DB_TABLE_LATEST, db_path)
        return pd.DataFrame()

    wanted = use_columns or DAILY_USE_COLUMNS
    cols = [c for c in wanted if c in existing_cols]

    if "stock_code" not in cols:
        logger.warning("[DAILY DB] stock_code column not found table=%s", DB_TABLE_LATEST)
        return pd.DataFrame()

    col_sql = ", ".join([f'"{c}"' for c in cols])

    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
        try:
            if symbols:
                syms = [_to_symbol4(s) for s in symbols if str(s).strip()]
                syms = sorted(set(syms))

                if not syms:
                    return pd.DataFrame()

                placeholders = ",".join(["?"] * len(syms))
                sql = f"""
                    SELECT {col_sql}
                    FROM {DB_TABLE_LATEST}
                    WHERE stock_code IN ({placeholders})
                """
                df = pd.read_sql_query(sql, con, params=syms)
            else:
                sql = f"""
                    SELECT {col_sql}
                    FROM {DB_TABLE_LATEST}
                """
                df = pd.read_sql_query(sql, con)

        finally:
            con.close()

        if df.empty:
            return df

        df["stock_code"] = df["stock_code"].map(_to_symbol4)

        if "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"], errors="coerce")

        return df

    except Exception as e:
        logger.exception("[DAILY DB] load failed path=%s err=%s", db_path, e)
        return pd.DataFrame()


# ============================================================
# 日足判定
# ============================================================

def judge_daily_row(row: pd.Series) -> DailyDecision:
    symbol = _to_symbol4(row.get("stock_code", ""))
    name = str(row.get("stock_name", "") or "")

    close = _num(row, "close")
    ma5 = _num(row, "MA_5")
    ma25 = _num(row, "MA_25")
    ma75 = _num(row, "MA_75")
    ma200 = _num(row, "MA_200")

    ma5_slope = _num(row, "MA_5_Slope")
    ma25_slope = _num(row, "MA_25_Slope")
    ma75_slope = _num(row, "MA_75_Slope")

    rsi = _num(row, "RSI")
    macd = _num(row, "MACD")
    macd_signal = _num(row, "MACD_Signal")
    macd_diff = _num(row, "MACD_Diff")

    change_1d = _num(row, "change_1d_pct")
    change_3d = _num(row, "change_3d_pct")
    change_5d = _num(row, "change_5d_pct")
    turnover = _num(row, "turnover")

    buy_score = 0.0
    sell_score = 0.0
    reasons: List[str] = []

    # --------------------------------------------------------
    # トレンド
    # --------------------------------------------------------
    if close > 0 and ma25 > 0:
        if close >= ma25:
            buy_score += 2.0
            reasons.append("日足終値>MA25")
        else:
            sell_score += 2.0
            reasons.append("日足終値<MA25")

    if close > 0 and ma75 > 0:
        if close >= ma75:
            buy_score += 1.0
            reasons.append("日足終値>MA75")
        else:
            sell_score += 1.0
            reasons.append("日足終値<MA75")

    if ma5 > 0 and ma25 > 0:
        if ma5 >= ma25:
            buy_score += 1.0
            reasons.append("MA5>MA25")
        else:
            sell_score += 1.0
            reasons.append("MA5<MA25")

    if ma25 > 0 and ma75 > 0:
        if ma25 >= ma75:
            buy_score += 1.0
            reasons.append("MA25>MA75")
        else:
            sell_score += 1.0
            reasons.append("MA25<MA75")

    if ma75 > 0 and ma200 > 0:
        if ma75 >= ma200:
            buy_score += 0.5
        else:
            sell_score += 0.5

    # --------------------------------------------------------
    # 傾き
    # --------------------------------------------------------
    if ma25_slope > 0:
        buy_score += 1.0
        reasons.append("MA25上向き")
    elif ma25_slope < 0:
        sell_score += 1.0
        reasons.append("MA25下向き")

    if ma75_slope > 0:
        buy_score += 0.5
        reasons.append("MA75上向き")
    elif ma75_slope < 0:
        sell_score += 0.5
        reasons.append("MA75下向き")

    if ma5_slope > 0:
        buy_score += 0.5
    elif ma5_slope < 0:
        sell_score += 0.5

    # --------------------------------------------------------
    # MACD
    # --------------------------------------------------------
    if macd >= macd_signal:
        buy_score += 1.0
        reasons.append("MACD強い")
    else:
        sell_score += 1.0
        reasons.append("MACD弱い")

    if macd_diff > 0:
        buy_score += 0.5
    elif macd_diff < 0:
        sell_score += 0.5

    # --------------------------------------------------------
    # RSI
    # --------------------------------------------------------
    if 45 <= rsi <= 72:
        buy_score += 1.0
        reasons.append("RSI良好")
    elif rsi >= 80:
        sell_score += 1.0
        reasons.append("RSI過熱")
    elif 0 < rsi <= 35:
        sell_score += 0.5
        reasons.append("RSI弱い")

    # --------------------------------------------------------
    # 買いシグナル
    # --------------------------------------------------------
    buy_signal_cols = [
        "Signal_Golden_Cross",
        "Signal_New_High_10",
        "Signal_Break_Res_10",
        "Signal_Trendline_Break",
        "Signal_First_Pullback",
        "Signal_Engulfing_Bull",
        "Signal_Three_White_Soldiers",
        "Signal_MA25_Lower_Dev",
    ]

    buy_hits = 0
    for c in buy_signal_cols:
        if _flag(row, c):
            buy_hits += 1

    if buy_hits:
        buy_score += buy_hits * 1.0
        reasons.append(f"日足買いSignal={buy_hits}")

    # --------------------------------------------------------
    # 売りシグナル
    # --------------------------------------------------------
    sell_signal_cols = [
        "Signal_Dead_Cross",
        "Signal_New_Low_10",
        "Signal_Break_Support_10",
        "Signal_MA25_Upper_Dev",
        "Signal_Evening_Star",
        "Signal_Three_Black_Crows",
        "Signal_Shooting_Star_as_Bear",
        "Signal_Tweezers_Top",
    ]

    sell_hits = 0
    for c in sell_signal_cols:
        if _flag(row, c):
            sell_hits += 1

    if sell_hits:
        sell_score += sell_hits * 1.0
        reasons.append(f"日足売りSignal={sell_hits}")

    # --------------------------------------------------------
    # 騰落率・売買代金
    # --------------------------------------------------------
    if change_1d >= 3.0:
        buy_score += 0.5
        reasons.append("1日強い")
    elif change_1d <= -3.0:
        sell_score += 0.5
        reasons.append("1日弱い")

    if change_3d >= 5.0:
        buy_score += 0.5
        reasons.append("3日強い")
    elif change_3d <= -5.0:
        sell_score += 0.5
        reasons.append("3日弱い")

    if change_5d >= 8.0:
        buy_score += 0.5
    elif change_5d <= -8.0:
        sell_score += 0.5

    if turnover >= 300_000_000:
        buy_score += 0.5
        reasons.append("売買代金良好")

    # --------------------------------------------------------
    # ストップ高・ストップ安系
    # --------------------------------------------------------
    if _flag(row, "Limit_Up_Touched") or _flag(row, "Limit_Up_Permanent"):
        buy_score += 1.0
        reasons.append("S高系")

    if _flag(row, "Limit_Down_Touched") or _flag(row, "Limit_Down_Permanent"):
        sell_score += 1.0
        reasons.append("S安系")

    daily_score = buy_score - sell_score

    daily_ok_buy = daily_score >= 2.0 and sell_score <= 3.5
    daily_ok_sell = daily_score <= -2.0
    daily_exit_warn = sell_score >= 3.0 or daily_score <= -1.5

    return DailyDecision(
        symbol=symbol,
        name=name,
        date=_safe_date_text(row.get("date", "")),
        daily_ok_buy=bool(daily_ok_buy),
        daily_ok_sell=bool(daily_ok_sell),
        daily_exit_warn=bool(daily_exit_warn),
        daily_score=float(daily_score),
        daily_buy_score=float(buy_score),
        daily_sell_score=float(sell_score),
        close=float(close),
        ma25=float(ma25),
        ma75=float(ma75),
        rsi=float(rsi),
        macd=float(macd),
        macd_signal=float(macd_signal),
        reason=" / ".join(reasons),
    )


def build_daily_decision_map(
    symbols: Optional[Iterable[str]] = None,
    db_path: str = DAILY_DB_PATH,
) -> Dict[str, DailyDecision]:
    df = load_daily_latest(symbols=symbols, db_path=db_path)
    if df.empty:
        return {}

    out: Dict[str, DailyDecision] = {}

    for _, row in df.iterrows():
        dec = judge_daily_row(row)
        if dec.symbol:
            out[dec.symbol] = dec

    logger.info(
        "[DAILY DB] decision map loaded symbols=%s db=%s",
        len(out),
        db_path,
    )
    return out


# ============================================================
# DataFrame へ日足情報を付与
# ============================================================

def attach_daily_decision(
    df: pd.DataFrame,
    db_path: str = DAILY_DB_PATH,
    symbol_col: str = "symbol",
    filter_buy: bool = False,
) -> pd.DataFrame:
    """
    候補DFに日足判定列を追加する。

    追加列:
      daily_score
      daily_buy_score
      daily_sell_score
      daily_ok_buy
      daily_ok_sell
      daily_exit_warn
      daily_reason

    filter_buy=True の場合:
      daily_ok_buy=True の銘柄だけ残す
    """

    if df is None or df.empty:
        return df

    out = df.copy()

    if symbol_col not in out.columns:
        if "stock_code" in out.columns:
            symbol_col = "stock_code"
        elif "code" in out.columns:
            symbol_col = "code"
        else:
            logger.warning("[DAILY DB] symbol column not found")
            return out

    symbols = [_to_symbol4(s) for s in out[symbol_col].dropna().unique()]
    dmap = build_daily_decision_map(symbols=symbols, db_path=db_path)

    out["daily_score"] = 0.0
    out["daily_buy_score"] = 0.0
    out["daily_sell_score"] = 0.0
    out["daily_ok_buy"] = False
    out["daily_ok_sell"] = False
    out["daily_exit_warn"] = False
    out["daily_reason"] = ""
    out["daily_date"] = ""

    for idx, row in out.iterrows():
        sym = _to_symbol4(row.get(symbol_col, ""))
        dec = dmap.get(sym)

        if dec is None:
            continue

        out.at[idx, "daily_score"] = dec.daily_score
        out.at[idx, "daily_buy_score"] = dec.daily_buy_score
        out.at[idx, "daily_sell_score"] = dec.daily_sell_score
        out.at[idx, "daily_ok_buy"] = dec.daily_ok_buy
        out.at[idx, "daily_ok_sell"] = dec.daily_ok_sell
        out.at[idx, "daily_exit_warn"] = dec.daily_exit_warn
        out.at[idx, "daily_reason"] = dec.reason
        out.at[idx, "daily_date"] = dec.date

    if filter_buy:
        before = len(out)
        out = out[out["daily_ok_buy"] == True].copy()
        logger.info("[DAILY DB] filter_buy before=%s after=%s", before, len(out))

    return out


# ============================================================
# 単体確認用
# ============================================================

def debug_print_daily_decisions(
    symbols: Optional[Iterable[str]] = None,
    db_path: str = DAILY_DB_PATH,
    limit: int = 20,
) -> None:
    dmap = build_daily_decision_map(symbols=symbols, db_path=db_path)

    logger.info("[DAILY DB DEBUG] loaded=%s", len(dmap))

    for i, dec in enumerate(dmap.values(), start=1):
        if i > limit:
            break

        logger.info(
            "[DAILY DB DEBUG] %s %s date=%s daily_score=%.2f buy=%.2f sell=%.2f "
            "ok_buy=%s exit_warn=%s close=%.1f ma25=%.1f ma75=%.1f rsi=%.2f reason=%s",
            dec.symbol,
            dec.name,
            dec.date,
            dec.daily_score,
            dec.daily_buy_score,
            dec.daily_sell_score,
            dec.daily_ok_buy,
            dec.daily_exit_warn,
            dec.close,
            dec.ma25,
            dec.ma75,
            dec.rsi,
            dec.reason,
        )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    debug_print_daily_decisions(
        symbols=["7203", "6857", "9984"],
        limit=10,
    )