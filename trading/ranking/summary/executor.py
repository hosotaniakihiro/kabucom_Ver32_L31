# ============================================================
# File   : trading/ranking/summary/executor.py
# Version: PRODUCTION-STABLE-COMPAT-REV1.0
# Purpose:
#   ranking_summary bootstrap_loader 互換 executor
#
# Features:
#   - DEFAULT_MAX_ENTRIES を提供
#   - execute_ai_ok_entries_bulk を提供
#   - ranking summary bootstrap が import error で停止しないようにする
#   - デフォルトでは発注しない安全設計
#
# Notes:
#   - このモジュール単体では発注しない
#   - 実発注は summary_ai runner / order executor 側に任せる
#   - bootstrap 中に誤発注しないよう dry-run 風の戻り値にする
# ============================================================

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# ============================================================
# defaults
# ============================================================

DEFAULT_MAX_ENTRIES = 3
DEFAULT_MIN_CONFIDENCE = 0.65
DEFAULT_MIN_BUY_SCORE = 5.0
DEFAULT_MAX_SELL_SCORE = 2.0
DEFAULT_MIN_PRICE = 1.0
DEFAULT_MIN_VOLUME = 0.0


# ============================================================
# helpers
# ============================================================

def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        if pd.isna(v):
            return default
        return float(v)
    except Exception:
        return default


def _safe_str(v: Any, default: str = "") -> str:
    try:
        if v is None:
            return default
        if pd.isna(v):
            return default
        return str(v)
    except Exception:
        return default


def _normalize_symbol(v: Any) -> str:
    text = _safe_str(v).strip()

    if not text:
        return ""

    if text.endswith(".0"):
        text = text[:-2]

    if "." in text:
        head = text.split(".", 1)[0]
        if head.isdigit():
            text = head

    if text.isdigit():
        text = text.zfill(4)

    return text


def _pick_col(df: pd.DataFrame, names: Iterable[str]) -> Optional[str]:
    if df is None or df.empty:
        return None

    cols = list(df.columns)
    lower_map = {str(c).lower(): c for c in cols}

    for name in names:
        if name in df.columns:
            return name

    for name in names:
        hit = lower_map.get(str(name).lower())
        if hit is not None:
            return hit

    return None


def _to_dataframe(ai_ok_entries: Any) -> pd.DataFrame:
    """
    ai_ok_entries を DataFrame に寄せる。
    """
    if ai_ok_entries is None:
        return pd.DataFrame()

    if isinstance(ai_ok_entries, pd.DataFrame):
        return ai_ok_entries.copy()

    if isinstance(ai_ok_entries, pd.Series):
        return ai_ok_entries.to_frame().T

    if isinstance(ai_ok_entries, dict):
        # {"results": [...]} や {"approved": [...]} 形式にも軽く対応
        for key in ("results", "approved", "entries", "rows", "data"):
            val = ai_ok_entries.get(key)
            if isinstance(val, list):
                return pd.DataFrame(val)
            if isinstance(val, pd.DataFrame):
                return val.copy()

        return pd.DataFrame([ai_ok_entries])

    if isinstance(ai_ok_entries, list):
        if not ai_ok_entries:
            return pd.DataFrame()
        return pd.DataFrame(ai_ok_entries)

    try:
        return pd.DataFrame(list(ai_ok_entries))
    except Exception:
        logger.warning(
            "[RANKING SUMMARY EXECUTOR] cannot convert entries to dataframe type=%s",
            type(ai_ok_entries),
        )
        return pd.DataFrame()


def _filter_approved(
    df: pd.DataFrame,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    min_buy_score: float = DEFAULT_MIN_BUY_SCORE,
    max_sell_score: float = DEFAULT_MAX_SELL_SCORE,
    min_price: float = DEFAULT_MIN_PRICE,
    min_volume: float = DEFAULT_MIN_VOLUME,
) -> pd.DataFrame:
    """
    AI OK / entry候補を安全に絞る。
    カラムが存在しない場合は落とさず、存在する条件だけ使う。
    """
    if df is None or df.empty:
        return pd.DataFrame()

    work = df.copy()

    symbol_col = _pick_col(work, ["symbol", "code", "Code", "銘柄コード"])
    if symbol_col:
        work["symbol"] = work[symbol_col].map(_normalize_symbol)
        work = work[work["symbol"].astype(str).str.len() > 0].copy()

    if work.empty:
        return work

    conf_col = _pick_col(work, ["confidence", "conf", "ai_confidence", "prob"])
    if conf_col:
        work["_conf_filter"] = pd.to_numeric(work[conf_col], errors="coerce").fillna(0.0)
        work = work[work["_conf_filter"] >= float(min_confidence)].copy()

    buy_col = _pick_col(work, ["score_buy", "buy_score", "buy", "disp_buy_score"])
    if buy_col:
        work["_buy_filter"] = pd.to_numeric(work[buy_col], errors="coerce").fillna(0.0)
        work = work[work["_buy_filter"] >= float(min_buy_score)].copy()

    sell_col = _pick_col(work, ["score_sell", "sell_score", "sell", "disp_sell_score"])
    if sell_col:
        work["_sell_filter"] = pd.to_numeric(work[sell_col], errors="coerce").fillna(0.0)
        work = work[work["_sell_filter"] <= float(max_sell_score)].copy()

    price_col = _pick_col(
        work,
        [
            "close",
            "close_price",
            "current_price",
            "price",
            "現在値",
        ],
    )
    if price_col:
        work["_price_filter"] = pd.to_numeric(work[price_col], errors="coerce").fillna(0.0)
        work = work[work["_price_filter"] >= float(min_price)].copy()

    volume_col = _pick_col(work, ["volume", "出来高", "売買高"])
    if volume_col and float(min_volume) > 0:
        work["_volume_filter"] = pd.to_numeric(work[volume_col], errors="coerce").fillna(0.0)
        work = work[work["_volume_filter"] >= float(min_volume)].copy()

    # 並び順
    sort_cols: list[str] = []
    ascending: list[bool] = []

    final_col = _pick_col(work, ["final_score", "display_score", "score_total", "score"])
    if final_col:
        sort_cols.append(final_col)
        ascending.append(False)

    if conf_col:
        sort_cols.append(conf_col)
        ascending.append(False)

    if buy_col:
        sort_cols.append(buy_col)
        ascending.append(False)

    if sort_cols:
        try:
            work = work.sort_values(sort_cols, ascending=ascending)
        except Exception:
            pass

    if "symbol" in work.columns:
        work = work.drop_duplicates(subset=["symbol"], keep="first").copy()

    if max_entries and int(max_entries) > 0:
        work = work.head(int(max_entries)).copy()

    drop_cols = [c for c in work.columns if str(c).startswith("_") and str(c).endswith("_filter")]
    if drop_cols:
        work = work.drop(columns=drop_cols, errors="ignore")

    return work.reset_index(drop=True)


# ============================================================
# public API
# ============================================================

def execute_ai_ok_entries_bulk(
    ai_ok_entries: Any = None,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    dry_run: bool = True,
    executor_callable: Optional[Callable[..., Any]] = None,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    min_buy_score: float = DEFAULT_MIN_BUY_SCORE,
    max_sell_score: float = DEFAULT_MAX_SELL_SCORE,
    min_price: float = DEFAULT_MIN_PRICE,
    min_volume: float = DEFAULT_MIN_VOLUME,
    source: str = "RANKING_SUMMARY",
    interval: int | str | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """
    AI OK 済み候補を最大 max_entries 件まで承認する互換関数。

    重要:
      - デフォルト dry_run=True
      - executor_callable が明示された場合のみ、それを呼ぶ
      - この関数自体は直接発注しない

    Returns:
      dict:
        candidates
        approved
        approved_symbols
        executed
        dry_run
        results
        skip
    """
    df = _to_dataframe(ai_ok_entries)

    if df.empty:
        logger.info(
            "[RANKING SUMMARY EXECUTOR] no ai_ok_entries source=%s interval=%s",
            source,
            interval,
        )
        return {
            "candidates": 0,
            "approved": 0,
            "approved_symbols": [],
            "executed": False,
            "dry_run": dry_run,
            "results": [],
            "skip": "no_entries",
        }

    approved_df = _filter_approved(
        df,
        max_entries=max_entries,
        min_confidence=min_confidence,
        min_buy_score=min_buy_score,
        max_sell_score=max_sell_score,
        min_price=min_price,
        min_volume=min_volume,
    )

    approved_symbols: list[str] = []
    if not approved_df.empty:
        symbol_col = _pick_col(approved_df, ["symbol", "code", "Code", "銘柄コード"])
        if symbol_col:
            approved_symbols = [
                _normalize_symbol(v)
                for v in approved_df[symbol_col].tolist()
                if _normalize_symbol(v)
            ]

    if approved_df.empty:
        logger.info(
            "[RANKING SUMMARY EXECUTOR] approved empty candidates=%s source=%s interval=%s",
            len(df),
            source,
            interval,
        )
        return {
            "candidates": int(len(df)),
            "approved": 0,
            "approved_symbols": [],
            "executed": False,
            "dry_run": dry_run,
            "results": [],
            "skip": "approved_empty",
        }

    results: list[Any] = []
    executed = False

    # 明示的に executor_callable が渡された場合だけ呼ぶ
    if executor_callable is not None and not dry_run:
        for _, row in approved_df.iterrows():
            try:
                res = executor_callable(row=row, source=source, interval=interval, **kwargs)
                results.append(res)
                executed = True
            except TypeError:
                # 古い callable が row だけ受ける場合
                try:
                    res = executor_callable(row)
                    results.append(res)
                    executed = True
                except Exception as e:
                    logger.exception(
                        "[RANKING SUMMARY EXECUTOR] executor callable failed symbol=%s err=%s",
                        row.get("symbol", ""),
                        e,
                    )
                    results.append({"ok": False, "error": str(e)})
            except Exception as e:
                logger.exception(
                    "[RANKING SUMMARY EXECUTOR] executor callable failed symbol=%s err=%s",
                    row.get("symbol", ""),
                    e,
                )
                results.append({"ok": False, "error": str(e)})

    logger.info(
        "[RANKING SUMMARY EXECUTOR] done candidates=%s approved=%s executed=%s dry_run=%s symbols=%s source=%s interval=%s",
        len(df),
        len(approved_df),
        executed,
        dry_run,
        approved_symbols,
        source,
        interval,
    )

    return {
        "candidates": int(len(df)),
        "approved": int(len(approved_df)),
        "approved_symbols": approved_symbols,
        "executed": bool(executed),
        "dry_run": bool(dry_run),
        "results": results,
        "skip": None,
        "approved_df": approved_df,
    }


def execute_entries_bulk(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return execute_ai_ok_entries_bulk(*args, **kwargs)


def execute_approved_entries_bulk(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return execute_ai_ok_entries_bulk(*args, **kwargs)


def execute_ai_entries_bulk(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return execute_ai_ok_entries_bulk(*args, **kwargs)


def approve_ai_ok_entries(
    ai_ok_entries: Any = None,
    *,
    max_entries: int = DEFAULT_MAX_ENTRIES,
    **kwargs: Any,
) -> pd.DataFrame:
    df = _to_dataframe(ai_ok_entries)

    return _filter_approved(
        df,
        max_entries=max_entries,
        min_confidence=float(kwargs.get("min_confidence", DEFAULT_MIN_CONFIDENCE)),
        min_buy_score=float(kwargs.get("min_buy_score", DEFAULT_MIN_BUY_SCORE)),
        max_sell_score=float(kwargs.get("max_sell_score", DEFAULT_MAX_SELL_SCORE)),
        min_price=float(kwargs.get("min_price", DEFAULT_MIN_PRICE)),
        min_volume=float(kwargs.get("min_volume", DEFAULT_MIN_VOLUME)),
    )


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_MIN_CONFIDENCE",
    "DEFAULT_MIN_BUY_SCORE",
    "DEFAULT_MAX_SELL_SCORE",
    "DEFAULT_MIN_PRICE",
    "DEFAULT_MIN_VOLUME",
    "execute_ai_ok_entries_bulk",
    "execute_entries_bulk",
    "execute_approved_entries_bulk",
    "execute_ai_entries_bulk",
    "approve_ai_ok_entries",
]