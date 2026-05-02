# ============================================================
# File   : trading/push/subscription_manager/ranking_source.py
# Function:
#   - ranking DB / SBI寄前CSV / 履歴DB から株ステーション PUSH 購読候補 symbol を取得する
#   - 外部公開入口
# ------------------------------------------------------------
# Version: PRODUCTION-REV3.1-MODULAR-RANKING-SOURCE
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional, Sequence

from .ranking_source_history import (
    load_latest_subscription_symbols_from_history,
    save_subscription_symbols_history,
)
from .ranking_source_paths import (
    REGISTER_MAX_SYMBOLS,
    is_existing_file,
    is_intraday,
    is_opening_csv_window,
    resolve_ranking_db_paths,
    today_ymd,
)
from .ranking_source_premarket import load_premarket_csv_symbols
from .ranking_source_retention import (
    RETENTION_MINUTES,
    apply_symbol_retention,
    get_symbol_retention_state,
    has_retention_state,
    normalize_symbols,
    reset_symbol_retention_state,
    seed_retention_state,
    append_unique,
)
from .ranking_source_selector import (
    detect_existing_table,
    read_ranking_df_from_db,
    select_subscription_symbols_from_ranking_df,
)

logger = logging.getLogger(__name__)

_history_restored_date: Optional[str] = None
_premarket_loaded_date: Optional[str] = None


def _now() -> dt.datetime:
    return dt.datetime.now()


def _restore_retention_from_history_if_needed(
    *,
    limit: int,
    now: Optional[dt.datetime] = None,
) -> List[str]:
    """
    場中再起動時用。
    メモリ上の retention state が空なら、DB履歴から復元する。
    """
    global _history_restored_date

    now = now or _now()
    trade_date = now.strftime("%Y-%m-%d")

    if _history_restored_date == trade_date:
        return []

    if has_retention_state():
        _history_restored_date = trade_date
        return []

    if not is_intraday(now):
        return []

    syms = load_latest_subscription_symbols_from_history(
        limit=limit,
        trade_date=trade_date,
        max_age_minutes=None,
        now=now,
    )

    if not syms:
        _history_restored_date = trade_date
        return []

    seed_retention_state(syms, now=now)
    _history_restored_date = trade_date

    logger.info(
        "[SUB MANAGER] retention restored from history trade_date=%s count=%d",
        trade_date,
        len(syms),
    )

    return syms[:limit]


def _load_opening_initial_symbols_if_needed(
    *,
    limit: int,
    priority_symbols: Optional[Sequence[Any]],
    now: Optional[dt.datetime] = None,
) -> List[str]:
    """
    寄り付き初期は SBI寄前CSV を最優先に使う。
    同一日で一度読み込んだ後は、毎分CSVに固定せず ranking DB 選定へ移行する。
    """
    global _premarket_loaded_date

    now = now or _now()
    ymd = today_ymd(now)

    if not is_opening_csv_window(now):
        return []

    if _premarket_loaded_date == ymd:
        return []

    csv_syms = load_premarket_csv_symbols(limit=limit, ymd=ymd)

    if not csv_syms:
        _premarket_loaded_date = ymd
        return []

    priority = normalize_symbols(priority_symbols)

    result: List[str] = []
    append_unique(result, priority, limit=limit)
    append_unique(result, csv_syms, limit=limit)

    retained = apply_symbol_retention(
        result,
        limit=limit,
        retention_minutes=RETENTION_MINUTES,
        now=now,
        priority_symbols=priority,
    )

    save_subscription_symbols_history(
        retained,
        source="premarket_csv",
        reason="SBI寄前CSV 上昇50+下落50",
        priority_symbols=priority,
        now=now,
    )

    _premarket_loaded_date = ymd

    logger.info(
        "[SUB MANAGER] opening initial symbols selected from premarket csv count=%d",
        len(retained),
    )

    return retained[:limit]


def load_ranking_symbols(
    limit: int = REGISTER_MAX_SYMBOLS,
    *,
    priority_symbols: Optional[Sequence[Any]] = None,
    retention_minutes: int = RETENTION_MINUTES,
    apply_retention: bool = True,
    use_premarket_csv: bool = True,
    restore_history: bool = True,
) -> List[str]:
    """
    ranking DB / SBI寄前CSV / 履歴DB から株ステーション PUSH 購読候補 symbol を取得する。

    優先順位:
      1. 寄り付き初期: SBI寄前CSV 上昇50 + 下落50
      2. 場中再起動: 履歴DBから直近登録100銘柄を復元
      3. 場中通常: ranking DBから優先枠方式で100銘柄選定
      4. 20分保持ルール適用
      5. 履歴DBへ保存

    Parameters
    ----------
    limit:
        最大返却件数。通常100。

    priority_symbols:
        エントリー済み / 保有中 / 注文中など、必ず購読対象にしたい銘柄。

    retention_minutes:
        一度候補に入った銘柄を保持する分数。デフォルト20分。

    apply_retention:
        Trueなら20分保持ルールを適用する。

    use_premarket_csv:
        Trueなら寄り付き初期にSBI寄前CSVを使う。

    restore_history:
        Trueなら場中再起動時に履歴DBから復元する。

    Returns
    -------
    List[str]
        購読候補symbol。重複なし。最大limit件。
    """
    limit = int(limit or REGISTER_MAX_SYMBOLS)

    if limit <= 0:
        return []

    now = _now()
    priority = normalize_symbols(priority_symbols)

    # 1. 寄り付き初期は SBI寄前CSV を最優先
    if use_premarket_csv:
        opening_symbols = _load_opening_initial_symbols_if_needed(
            limit=limit,
            priority_symbols=priority,
            now=now,
        )
        if opening_symbols:
            return opening_symbols[:limit]

    # 2. 場中再起動時は、履歴DBから retention state を復元
    restored_symbols: List[str] = []
    if restore_history:
        restored_symbols = _restore_retention_from_history_if_needed(
            limit=limit,
            now=now,
        )

    # 3. ranking DB から fresh symbols を選定
    db_paths = resolve_ranking_db_paths(now)

    for path in db_paths:
        if not path or not is_existing_file(path):
            continue

        df, tables = read_ranking_df_from_db(path)

        if df.empty:
            logger.info(
                "[SUB MANAGER] ranking db empty path=%s tables=%s",
                path,
                tables,
            )
            continue

        fresh_symbols = select_subscription_symbols_from_ranking_df(
            df,
            limit=limit,
            priority_symbols=priority,
        )

        if not fresh_symbols:
            logger.info(
                "[SUB MANAGER] ranking selection empty path=%s tables=%s rows=%d",
                path,
                tables,
                len(df),
            )
            continue

        merged_fresh: List[str] = []
        append_unique(merged_fresh, fresh_symbols, limit=limit)
        append_unique(merged_fresh, restored_symbols, limit=limit)

        if apply_retention:
            symbols = apply_symbol_retention(
                merged_fresh,
                limit=limit,
                retention_minutes=retention_minutes,
                priority_symbols=priority,
                now=now,
            )
        else:
            symbols = merged_fresh[:limit]

        if symbols:
            save_subscription_symbols_history(
                symbols,
                source="ranking_db",
                reason=f"ranking DB priority selection path={path} tables={','.join(tables)}",
                priority_symbols=priority,
                now=now,
            )

            logger.info(
                "[SUB MANAGER] ranking symbols loaded path=%s tables=%s rows=%d fresh=%d restored=%d result=%d retention=%s",
                path,
                ",".join(tables),
                len(df),
                len(fresh_symbols),
                len(restored_symbols),
                len(symbols),
                bool(apply_retention),
            )
            return symbols[:limit]

    # 4. ranking DBが読めない場合、復元履歴を返す
    if restored_symbols:
        symbols = apply_symbol_retention(
            restored_symbols,
            limit=limit,
            retention_minutes=retention_minutes,
            priority_symbols=priority,
            now=now,
        )

        save_subscription_symbols_history(
            symbols,
            source="history_fallback",
            reason="ranking DB unavailable; restored from subscription history",
            priority_symbols=priority,
            now=now,
        )

        logger.warning(
            "[SUB MANAGER] ranking unavailable; use restored subscription history count=%d",
            len(symbols),
        )
        return symbols[:limit]

    # 5. DBから取れない場合でも priority_symbols は返す
    if priority:
        save_subscription_symbols_history(
            priority,
            source="priority_fallback",
            reason="ranking unavailable; priority symbols only",
            priority_symbols=priority,
            now=now,
        )

        logger.warning(
            "[SUB MANAGER] ranking symbols unavailable; fallback priority symbols count=%d",
            len(priority),
        )
        return priority[:limit]

    logger.warning("[SUB MANAGER] ranking symbols unavailable")
    return []


def load_ranking_symbols_no_retention(
    limit: int = REGISTER_MAX_SYMBOLS,
    *,
    priority_symbols: Optional[Sequence[Any]] = None,
) -> List[str]:
    """
    デバッグ用。
    20分保持・寄前CSV・履歴復元を使わず、現在ranking DBからのfresh選定のみ返す。
    """
    return load_ranking_symbols(
        limit=limit,
        priority_symbols=priority_symbols,
        apply_retention=False,
        use_premarket_csv=False,
        restore_history=False,
    )


def load_ranking_symbols_with_priority(
    priority_symbols: Optional[Sequence[Any]],
    limit: int = REGISTER_MAX_SYMBOLS,
) -> List[str]:
    """
    priority_symbols を明示して呼ぶための互換API。
    """
    return load_ranking_symbols(
        limit=limit,
        priority_symbols=priority_symbols,
        retention_minutes=RETENTION_MINUTES,
        apply_retention=True,
        use_premarket_csv=True,
        restore_history=True,
    )


def load_premarket_symbols_for_debug(
    limit: int = REGISTER_MAX_SYMBOLS,
    *,
    ymd: Optional[str] = None,
) -> List[str]:
    """
    デバッグ用。
    SBI寄前CSVだけから銘柄を読む。
    """
    return load_premarket_csv_symbols(limit=limit, ymd=ymd)


def load_latest_subscription_history_for_debug(
    limit: int = REGISTER_MAX_SYMBOLS,
) -> List[str]:
    """
    デバッグ用。
    履歴DBだけから直近登録銘柄を読む。
    """
    return load_latest_subscription_symbols_from_history(limit=limit)


def reset_ranking_source_runtime_state() -> None:
    """
    テスト用 / 日跨ぎ用。
    ranking_source のメモリ状態をリセットする。
    """
    global _history_restored_date
    global _premarket_loaded_date

    reset_symbol_retention_state()
    _history_restored_date = None
    _premarket_loaded_date = None

    logger.info("[SUB MANAGER] ranking source runtime state reset")


def get_ranking_source_runtime_state() -> Dict[str, str]:
    """
    デバッグ用。
    retention state を返す。
    """
    return get_symbol_retention_state()