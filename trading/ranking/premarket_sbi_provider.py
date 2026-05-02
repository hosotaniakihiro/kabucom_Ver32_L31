# ============================================================
# File   : trading/ranking/premarket_sbi_provider.py
# Version: PRODUCTION-STABLE-REV1.0-SBI-PREMARKET-RANKING-PROVIDER
# ------------------------------------------------------------
# Purpose:
#   - 寄前ランキングCSVから、マーケット開始前の初期監視銘柄を作る
#   - SBI_yorimae_ranking フォルダ内の
#       ランキング_寄前気配上昇率上位YYYYMMDD.csv
#       ランキング_寄前気配下落率上位YYYYMMDD.csv
#     を読み込む
#   - 上昇率上位50 + 下落率上位50 を結合
#   - 重複除外
#   - symbol_flags.db でエントリー可否 / ETF除外 / 市場条件を確認
#   - 最大100銘柄を返す
#
# Notes:
#   - このモジュールは発注しない
#   - 株ステーション登録もしない
#   - あくまで「寄前の初期候補リスト」を作るだけ
#   - active_symbol_manager.py から呼び出す想定
#
# Expected log:
#   [PREMARKET SBI] file resolved ...
#   [PREMARKET SBI] loaded rise=50 fall=50 merged=100 dedup=98
#   [PREMARKET SBI] symbol_flags filter before=98 after=92 removed=6
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# Default paths
# ============================================================

DEFAULT_SBI_PREMARKET_DIR = (
    r"\\192.168.0.22\AutoStockBuyAndSell\raw_data\SBI\SBI_yorimae_ranking"
)

DEFAULT_SYMBOL_FLAGS_DB = (
    r"\\192.168.0.22\AutoStockBuyAndSell\Basic\symbol_flags.db"
)

RISE_KEYWORDS: Tuple[str, ...] = (
    "上昇率",
    "値上がり",
    "騰落率上位",
)

FALL_KEYWORDS: Tuple[str, ...] = (
    "下落率",
    "値下がり",
)

DEFAULT_RISE_FILE_TEMPLATE = "ランキング_寄前気配上昇率上位{ymd}.csv"
DEFAULT_FALL_FILE_TEMPLATE = "ランキング_寄前気配下落率上位{ymd}.csv"

DEFAULT_MAX_RISE = 50
DEFAULT_MAX_FALL = 50
DEFAULT_MAX_TOTAL = 100


# ============================================================
# Environment helpers
# ============================================================

def _env_str(name: str, default: str) -> str:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return default
        return str(v).strip()
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        v = os.environ.get(name)
        if v is None or str(v).strip() == "":
            return int(default)
        return int(float(str(v).strip()))
    except Exception:
        return int(default)


def _env_bool(name: str, default: bool = False) -> bool:
    try:
        v = os.environ.get(name)
        if v is None:
            return bool(default)

        s = str(v).strip().lower()

        if s in {"1", "true", "yes", "y", "on", "ok", "enable", "enabled"}:
            return True

        if s in {"0", "false", "no", "n", "off", "ng", "disable", "disabled"}:
            return False

        return bool(default)
    except Exception:
        return bool(default)


SBI_PREMARKET_DIR = _env_str(
    "SBI_PREMARKET_RANKING_DIR",
    DEFAULT_SBI_PREMARKET_DIR,
)

SYMBOL_FLAGS_DB = _env_str(
    "SYMBOL_FLAGS_DB_PATH",
    DEFAULT_SYMBOL_FLAGS_DB,
)

MAX_RISE = _env_int("SBI_PREMARKET_MAX_RISE", DEFAULT_MAX_RISE)
MAX_FALL = _env_int("SBI_PREMARKET_MAX_FALL", DEFAULT_MAX_FALL)
MAX_TOTAL = _env_int("SBI_PREMARKET_MAX_TOTAL", DEFAULT_MAX_TOTAL)

# symbol_flagsの確認条件
REQUIRE_SYMBOL_FLAGS = _env_bool("SBI_PREMARKET_REQUIRE_SYMBOL_FLAGS", True)
ALLOW_BUY_TARGET = _env_bool("SBI_PREMARKET_ALLOW_BUY_TARGET", True)
ALLOW_SELL_TARGET = _env_bool("SBI_PREMARKET_ALLOW_SELL_TARGET", True)
EXCLUDE_ETF = _env_bool("SBI_PREMARKET_EXCLUDE_ETF", True)

# 100銘柄未満でも古いACTIVE等から補充しない方針
ALLOW_LESS_THAN_100 = _env_bool("SBI_PREMARKET_ALLOW_LESS_THAN_100", True)


# ============================================================
# Symbol normalize
# ============================================================

def normalize_symbol(symbol: Any) -> Optional[str]:
    """
    銘柄コードを kabu Station 用の文字列に正規化する。

    Examples:
        7203.T -> 7203
        7203.0 -> 7203
        " 456A " -> 456A
    """
    if symbol is None:
        return None

    s = str(symbol).strip().upper()

    if not s:
        return None

    # Excel/CSV由来で "7203.0" になる場合
    if s.endswith(".0"):
        s2 = s[:-2]
        if s2.isdigit():
            s = s2

    if s.endswith(".T"):
        s = s[:-2]

    # 余計な記号を除去しすぎると英字コードを壊すので、
    # ここでは空白と全角スペース中心に留める
    s = s.replace("　", "").replace(" ", "")

    if s in {"NAN", "NONE", "NULL", "-", "0"}:
        return None

    # 日本株コード: 4桁 or 英字入り 3〜5文字を許容
    if not (3 <= len(s) <= 5):
        return None

    if not re.match(r"^[0-9A-Z]+$", s):
        return None

    return s


def _dedupe_keep_order(items: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: Set[str] = set()

    for x in items:
        s = normalize_symbol(x)
        if not s:
            continue

        if s in seen:
            continue

        seen.add(s)
        out.append(s)

    return out


# ============================================================
# Date helpers
# ============================================================

def normalize_ymd(target_date: Optional[Any] = None) -> str:
    """
    target_date を YYYYMMDD に正規化する。
    """
    if target_date is None:
        return dt.datetime.now().strftime("%Y%m%d")

    if isinstance(target_date, dt.datetime):
        return target_date.strftime("%Y%m%d")

    if isinstance(target_date, dt.date):
        return target_date.strftime("%Y%m%d")

    s = str(target_date).strip()

    if re.match(r"^\d{8}$", s):
        return s

    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return s.replace("-", "")

    if re.match(r"^\d{4}/\d{2}/\d{2}$", s):
        return s.replace("/", "")

    raise ValueError(f"Invalid target_date: {target_date!r}")


def ymd_to_date(ymd: str) -> dt.date:
    return dt.datetime.strptime(ymd, "%Y%m%d").date()


# ============================================================
# File resolving
# ============================================================

def _safe_path(p: str | Path) -> Path:
    return Path(str(p))


def _path_exists(path: str | Path) -> bool:
    try:
        return Path(path).exists()
    except Exception:
        return False


def _find_file_by_keywords(
    *,
    directory: str | Path,
    ymd: str,
    keywords: Sequence[str],
) -> Optional[Path]:
    """
    テンプレート名で見つからない場合のfallback。
    指定日付 + キーワードを含むCSVを探す。
    """
    base = _safe_path(directory)

    if not _path_exists(base):
        return None

    try:
        files = list(base.glob(f"*{ymd}*.csv"))
    except Exception:
        logger.debug(
            "[PREMARKET SBI] glob failed directory=%s ymd=%s",
            base,
            ymd,
            exc_info=True,
        )
        return None

    for f in files:
        name = f.name
        if all(k in name for k in keywords):
            return f

    for f in files:
        name = f.name
        if any(k in name for k in keywords):
            return f

    return None


def resolve_premarket_files(
    *,
    target_date: Optional[Any] = None,
    directory: str | Path = SBI_PREMARKET_DIR,
) -> Tuple[Optional[Path], Optional[Path]]:
    """
    寄前ランキングCSVの上昇率 / 下落率ファイルを解決する。
    """
    ymd = normalize_ymd(target_date)
    base = _safe_path(directory)

    rise_path = base / DEFAULT_RISE_FILE_TEMPLATE.format(ymd=ymd)
    fall_path = base / DEFAULT_FALL_FILE_TEMPLATE.format(ymd=ymd)

    if not _path_exists(rise_path):
        rise_path = _find_file_by_keywords(
            directory=base,
            ymd=ymd,
            keywords=("寄前", "上昇率"),
        )

    if not _path_exists(fall_path):
        fall_path = _find_file_by_keywords(
            directory=base,
            ymd=ymd,
            keywords=("寄前", "下落率"),
        )

    logger.info(
        "[PREMARKET SBI] file resolved ymd=%s dir=%s rise=%s exists=%s fall=%s exists=%s",
        ymd,
        base,
        rise_path,
        _path_exists(rise_path) if rise_path else False,
        fall_path,
        _path_exists(fall_path) if fall_path else False,
    )

    return rise_path, fall_path


# ============================================================
# CSV reader
# ============================================================

def _read_csv_any_encoding(path: str | Path) -> pd.DataFrame:
    """
    SBI CSVは環境により cp932 / utf-8-sig があり得るため順に試す。
    """
    p = _safe_path(path)

    encodings = (
        "cp932",
        "shift_jis",
        "utf-8-sig",
        "utf-8",
    )

    last_err: Optional[BaseException] = None

    for enc in encodings:
        try:
            return pd.read_csv(p, encoding=enc)
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"Failed to read csv path={p} last_err={last_err!r}")


def _find_symbol_column(df: pd.DataFrame) -> Optional[str]:
    if df is None or df.empty:
        return None

    cols = list(df.columns)

    candidates = (
        "symbol",
        "Symbol",
        "code",
        "Code",
        "コード",
        "銘柄コード",
        "証券コード",
        "銘柄",
    )

    for c in candidates:
        if c in cols:
            return c

    # 部分一致
    for c in cols:
        sc = str(c)
        if "コード" in sc:
            return c

    return None


def _find_name_column(df: pd.DataFrame) -> Optional[str]:
    if df is None or df.empty:
        return None

    cols = list(df.columns)

    candidates = (
        "symbolname",
        "name",
        "Name",
        "銘柄名",
        "名称",
    )

    for c in candidates:
        if c in cols:
            return c

    for c in cols:
        sc = str(c)
        if "銘柄名" in sc or "名称" in sc:
            return c

    return None


def load_premarket_ranking_csv(
    path: str | Path,
    *,
    kind: str,
    max_rows: int,
) -> pd.DataFrame:
    """
    寄前ランキングCSVを読み、symbol列を正規化する。
    """
    p = _safe_path(path)

    if not _path_exists(p):
        logger.warning("[PREMARKET SBI] csv not found kind=%s path=%s", kind, p)
        return pd.DataFrame()

    try:
        df = _read_csv_any_encoding(p)
    except Exception:
        logger.exception("[PREMARKET SBI] csv read failed kind=%s path=%s", kind, p)
        return pd.DataFrame()

    if df is None or df.empty:
        logger.warning("[PREMARKET SBI] csv empty kind=%s path=%s", kind, p)
        return pd.DataFrame()

    symbol_col = _find_symbol_column(df)
    name_col = _find_name_column(df)

    if not symbol_col:
        logger.warning(
            "[PREMARKET SBI] symbol column not found kind=%s path=%s columns=%s",
            kind,
            p,
            list(df.columns),
        )
        return pd.DataFrame()

    out = pd.DataFrame()
    out["symbol"] = df[symbol_col].map(normalize_symbol)
    out["source_kind"] = kind
    out["source_file"] = str(p)

    if name_col:
        out["symbolname"] = df[name_col].astype(str)
    else:
        out["symbolname"] = ""

    # 元順位
    out["source_rank"] = range(1, len(out) + 1)

    out = out[out["symbol"].notna()].copy()
    out = out.drop_duplicates(subset=["symbol"], keep="first")

    if max_rows > 0:
        out = out.head(int(max_rows)).copy()

    logger.info(
        "[PREMARKET SBI] csv loaded kind=%s path=%s rows=%d symbols=%d head=%s",
        kind,
        p,
        len(df),
        out["symbol"].nunique() if "symbol" in out.columns else 0,
        out["symbol"].head(10).tolist() if "symbol" in out.columns else [],
    )

    return out.reset_index(drop=True)


# ============================================================
# symbol_flags filter
# ============================================================

def _connect_sqlite(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _get_table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [str(r["name"]) for r in rows]
    except Exception:
        return []


def load_symbol_flags_eligible_symbols(
    *,
    db_path: str | Path = SYMBOL_FLAGS_DB,
    table: str = "symbol_flags",
) -> Tuple[Set[str], Dict[str, Dict[str, Any]]]:
    """
    symbol_flags.db からエントリー可能銘柄集合を作る。

    条件:
      - symbolが存在
      - ETF除外
      - buy_target=1 または sell_target=1
        ※環境変数で buy / sell の許可は変更可
    """
    eligible: Set[str] = set()
    info_map: Dict[str, Dict[str, Any]] = {}

    p = _safe_path(db_path)

    if not _path_exists(p):
        logger.warning("[PREMARKET SBI] symbol_flags db not found path=%s", p)
        return eligible, info_map

    try:
        with _connect_sqlite(p) as conn:
            if not _table_exists(conn, table):
                logger.warning(
                    "[PREMARKET SBI] symbol_flags table not found db=%s table=%s",
                    p,
                    table,
                )
                return eligible, info_map

            cols = _get_table_columns(conn, table)

            wanted_cols = [
                "symbol",
                "symbolname",
                "buy_target",
                "sell_target",
                "is_etf",
                "market",
                "market_type",
                "ats_ok",
                "short_ok",
                "is_margin",
            ]

            select_cols = [c for c in wanted_cols if c in cols]

            if "symbol" not in select_cols:
                logger.warning(
                    "[PREMARKET SBI] symbol column missing in symbol_flags db=%s cols=%s",
                    p,
                    cols,
                )
                return eligible, info_map

            sql = f"SELECT {', '.join(select_cols)} FROM {table}"
            rows = conn.execute(sql).fetchall()

            for r in rows:
                d = {k: r[k] for k in r.keys()}

                sym = normalize_symbol(d.get("symbol"))
                if not sym:
                    continue

                is_etf = int(d.get("is_etf") or 0) if "is_etf" in d else 0
                buy_target = int(d.get("buy_target") or 0) if "buy_target" in d else 0
                sell_target = int(d.get("sell_target") or 0) if "sell_target" in d else 0

                if EXCLUDE_ETF and is_etf == 1:
                    continue

                ok_by_side = False
                if ALLOW_BUY_TARGET and buy_target == 1:
                    ok_by_side = True
                if ALLOW_SELL_TARGET and sell_target == 1:
                    ok_by_side = True

                if not ok_by_side:
                    continue

                eligible.add(sym)
                info_map[sym] = d

        logger.info(
            "[PREMARKET SBI] symbol_flags eligible loaded db=%s eligible=%d require=%s buy=%s sell=%s exclude_etf=%s",
            p,
            len(eligible),
            REQUIRE_SYMBOL_FLAGS,
            ALLOW_BUY_TARGET,
            ALLOW_SELL_TARGET,
            EXCLUDE_ETF,
        )

        return eligible, info_map

    except Exception:
        logger.exception("[PREMARKET SBI] symbol_flags load failed db=%s", p)
        return eligible, info_map


def filter_by_symbol_flags(
    symbols: Sequence[str],
    *,
    db_path: str | Path = SYMBOL_FLAGS_DB,
) -> List[str]:
    """
    symbol_flagsでエントリー可否を確認して絞る。
    """
    cleaned = _dedupe_keep_order(symbols)

    if not REQUIRE_SYMBOL_FLAGS:
        logger.info(
            "[PREMARKET SBI] symbol_flags filter disabled before=%d",
            len(cleaned),
        )
        return cleaned

    eligible, _ = load_symbol_flags_eligible_symbols(db_path=db_path)

    if not eligible:
        logger.warning(
            "[PREMARKET SBI] symbol_flags eligible empty -> return empty before=%d db=%s",
            len(cleaned),
            db_path,
        )
        return []

    kept: List[str] = []
    removed: List[str] = []

    for s in cleaned:
        if s in eligible:
            kept.append(s)
        else:
            removed.append(s)

    logger.info(
        "[PREMARKET SBI] symbol_flags filter before=%d after=%d removed=%d removed_head=%s",
        len(cleaned),
        len(kept),
        len(removed),
        removed[:30],
    )

    return kept


# ============================================================
# Main provider
# ============================================================

def load_premarket_sbi_candidates(
    *,
    target_date: Optional[Any] = None,
    directory: str | Path = SBI_PREMARKET_DIR,
    symbol_flags_db: str | Path = SYMBOL_FLAGS_DB,
    max_rise: int = MAX_RISE,
    max_fall: int = MAX_FALL,
    max_total: int = MAX_TOTAL,
    apply_symbol_flags: bool = True,
) -> List[str]:
    """
    SBI寄前ランキングCSVから初期監視銘柄を作る。

    Returns:
        symbol list
    """
    ymd = normalize_ymd(target_date)

    rise_path, fall_path = resolve_premarket_files(
        target_date=ymd,
        directory=directory,
    )

    rise_df = pd.DataFrame()
    fall_df = pd.DataFrame()

    if rise_path:
        rise_df = load_premarket_ranking_csv(
            rise_path,
            kind="rise",
            max_rows=max_rise,
        )

    if fall_path:
        fall_df = load_premarket_ranking_csv(
            fall_path,
            kind="fall",
            max_rows=max_fall,
        )

    rise_symbols = rise_df["symbol"].tolist() if not rise_df.empty and "symbol" in rise_df.columns else []
    fall_symbols = fall_df["symbol"].tolist() if not fall_df.empty and "symbol" in fall_df.columns else []

    merged = _dedupe_keep_order(list(rise_symbols) + list(fall_symbols))

    logger.info(
        "[PREMARKET SBI] loaded ymd=%s rise=%d fall=%d merged=%d dedup=%d head=%s",
        ymd,
        len(rise_symbols),
        len(fall_symbols),
        len(rise_symbols) + len(fall_symbols),
        len(merged),
        merged[:20],
    )

    if apply_symbol_flags:
        merged = filter_by_symbol_flags(
            merged,
            db_path=symbol_flags_db,
        )

    if max_total > 0:
        merged = merged[:int(max_total)]

    logger.info(
        "[PREMARKET SBI] candidates done ymd=%s total=%d max_total=%d head=%s",
        ymd,
        len(merged),
        max_total,
        merged[:20],
    )

    return merged


def load_premarket_sbi_dataframe(
    *,
    target_date: Optional[Any] = None,
    directory: str | Path = SBI_PREMARKET_DIR,
    symbol_flags_db: str | Path = SYMBOL_FLAGS_DB,
    max_rise: int = MAX_RISE,
    max_fall: int = MAX_FALL,
    apply_symbol_flags: bool = True,
) -> pd.DataFrame:
    """
    デバッグや詳細表示用。
    rise/fallのDataFrameを結合して返す。
    """
    ymd = normalize_ymd(target_date)

    rise_path, fall_path = resolve_premarket_files(
        target_date=ymd,
        directory=directory,
    )

    frames: List[pd.DataFrame] = []

    if rise_path:
        rise_df = load_premarket_ranking_csv(
            rise_path,
            kind="rise",
            max_rows=max_rise,
        )
        if not rise_df.empty:
            frames.append(rise_df)

    if fall_path:
        fall_df = load_premarket_ranking_csv(
            fall_path,
            kind="fall",
            max_rows=max_fall,
        )
        if not fall_df.empty:
            frames.append(fall_df)

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)

    if apply_symbol_flags:
        eligible, info_map = load_symbol_flags_eligible_symbols(
            db_path=symbol_flags_db,
        )

        if eligible:
            df["symbol_flags_ok"] = df["symbol"].isin(eligible)
            df["flag_symbolname"] = df["symbol"].map(
                lambda s: str(info_map.get(str(s), {}).get("symbolname", ""))
            )
            df = df[df["symbol_flags_ok"]].copy()
        else:
            df["symbol_flags_ok"] = False
            df = df.iloc[0:0].copy()

    logger.info(
        "[PREMARKET SBI] dataframe done ymd=%s rows=%d symbols=%d head=%s",
        ymd,
        len(df),
        df["symbol"].nunique() if "symbol" in df.columns else 0,
        df["symbol"].head(20).tolist() if "symbol" in df.columns else [],
    )

    return df.reset_index(drop=True)


# ============================================================
# Time condition helper
# ============================================================

def is_premarket_time(
    *,
    now: Optional[dt.datetime] = None,
    start_hour: int = 7,
    start_minute: int = 0,
    end_hour: int = 9,
    end_minute: int = 0,
) -> bool:
    """
    寄前モードかどうか。

    デフォルト:
      07:00 <= now < 09:00
    """
    n = now or dt.datetime.now()

    cur = n.hour * 60 + n.minute
    start = int(start_hour) * 60 + int(start_minute)
    end = int(end_hour) * 60 + int(end_minute)

    return start <= cur < end


def should_use_premarket_sbi(
    *,
    now: Optional[dt.datetime] = None,
    today_ranking_available: bool = False,
) -> bool:
    """
    active_symbol_manager.py 側から使う判定。

    方針:
      - 寄前時間帯はSBI寄前CSVを使う
      - 9:00以降でも当日ランキングが空なら、暫定的にSBI寄前CSVを使える
    """
    n = now or dt.datetime.now()

    if is_premarket_time(now=n):
        return True

    if not today_ranking_available:
        # 起動直後・ranking未取得時の暫定fallback
        return True

    return False


# ============================================================
# Debug / manual execution
# ============================================================

def debug_premarket_sbi_provider(
    *,
    target_date: Optional[Any] = None,
) -> Dict[str, Any]:
    ymd = normalize_ymd(target_date)

    symbols = load_premarket_sbi_candidates(target_date=ymd)

    payload = {
        "ymd": ymd,
        "total": len(symbols),
        "head": symbols[:30],
        "directory": SBI_PREMARKET_DIR,
        "symbol_flags_db": SYMBOL_FLAGS_DB,
        "max_rise": MAX_RISE,
        "max_fall": MAX_FALL,
        "max_total": MAX_TOTAL,
        "require_symbol_flags": REQUIRE_SYMBOL_FLAGS,
        "allow_buy_target": ALLOW_BUY_TARGET,
        "allow_sell_target": ALLOW_SELL_TARGET,
        "exclude_etf": EXCLUDE_ETF,
    }

    logger.info("[PREMARKET SBI DEBUG] %s", payload)

    return payload


__all__ = [
    "SBI_PREMARKET_DIR",
    "SYMBOL_FLAGS_DB",
    "normalize_symbol",
    "normalize_ymd",
    "resolve_premarket_files",
    "load_premarket_ranking_csv",
    "load_symbol_flags_eligible_symbols",
    "filter_by_symbol_flags",
    "load_premarket_sbi_candidates",
    "load_premarket_sbi_dataframe",
    "is_premarket_time",
    "should_use_premarket_sbi",
    "debug_premarket_sbi_provider",
]