# ============================================================
# File   : trading/ranking/summary/bootstrap_loader.py
# Version: Ver1.0-PRODUCTION-RANKING-SUMMARY-BOOTSTRAP-LOADER
# ------------------------------------------------------------
# 【概要】
#   ranking snapshot / PUSH summary DB 読み込み
# ============================================================

from __future__ import annotations

import importlib
import logging
import os
from typing import Any, Callable, Iterable, Optional

import pandas as pd

from trading.ranking.summary.bootstrap_config import (
    PUSH_SUMMARY_TABLES,
    RANKING_SNAPSHOT_TABLE,
)
from trading.ranking.summary.bootstrap_db import (
    connect_sqlite,
    quote_ident,
    table_exists,
)

logger = logging.getLogger(__name__)


def resolve_callable(candidates: Iterable[tuple[str, str]]) -> Optional[Callable[..., Any]]:
    for module_name, func_name in candidates:
        try:
            mod = importlib.import_module(module_name)
            fn = getattr(mod, func_name, None)
            if callable(fn):
                logger.info(
                    "[RANKING SUMMARY BOOTSTRAP LOADER] resolved %s.%s",
                    module_name,
                    func_name,
                )
                return fn
        except Exception:
            continue
    return None


# ============================================================
# 追加P0-2: bootstrap 読み込み量の上限設定
# ============================================================

def _bootstrap_lookback_min() -> int:
    """bootstrap で遡る分数。0 以下で無制限 (従来動作)。"""
    try:
        v = os.getenv("RANKING_BOOTSTRAP_LOOKBACK_MIN")
        if v and str(v).strip():
            return int(float(str(v).strip()))
    except Exception:
        pass
    return 180


def _bootstrap_max_rows() -> int:
    """bootstrap で読む最大行数。0 以下で無制限 (従来動作)。"""
    try:
        v = os.getenv("RANKING_BOOTSTRAP_MAX_ROWS")
        if v and str(v).strip():
            return int(float(str(v).strip()))
    except Exception:
        pass
    return 80000


def load_ranking_snapshot_fallback(
    ranking_db_path: str,
    *,
    start_dt: pd.Timestamp | None = None,
    end_dt: pd.Timestamp | None = None,
) -> pd.DataFrame:
    if not os.path.exists(ranking_db_path):
        logger.warning(
            "[RANKING SUMMARY BOOTSTRAP LOADER] ranking db not found path=%s",
            ranking_db_path,
        )
        return pd.DataFrame()

    try:
        with connect_sqlite(ranking_db_path, readonly=True) as conn:
            if not table_exists(conn, RANKING_SNAPSHOT_TABLE):
                logger.warning(
                    "[RANKING SUMMARY BOOTSTRAP LOADER] table not found db=%s table=%s",
                    ranking_db_path,
                    RANKING_SNAPSHOT_TABLE,
                )
                return pd.DataFrame()

            where = []
            params: list[Any] = []

            # ============================================================
            # 追加P0-2: bootstrap の読み込み量を有界にする
            # ------------------------------------------------------------
            # 2026-08-28 15:26-15:32 の実測:
            #   [BOOTSTRAP LOADER] loaded ranking snapshot rows=290,939
            #                      start=03:40:00 end=None
            #   [BOOTSTRAP OHLCV]  1min built rows=113,789 symbols=1,321
            #   [BOOTSTRAP LOADER] loaded push summary rows=76,531
            #   [BOOTSTRAP RESAMPLE] 3min built rows=43,496
            #   [BOOTSTRAP] done elapsed=362.52s
            #   RSS 338.6MB -> 1,717.0MB (約 6 分で 5 倍)
            #
            # 巨大フレームを同時に複数保持することが RSS 急増の主因。
            # indicator に必要なのは銘柄あたり最大 105 bars 程度なので、
            # 一日分すべてを読む必要はない。
            #
            # start_dt が指定されていない / 過度に古い場合は
            # RANKING_BOOTSTRAP_LOOKBACK_MIN (既定 180 分) で切る。
            # 併せて SQL 側に LIMIT も掛ける。
            # ============================================================
            eff_start = start_dt
            lookback_min = _bootstrap_lookback_min()
            if lookback_min > 0:
                base_end = pd.to_datetime(end_dt) if end_dt is not None else pd.Timestamp.now()
                cutoff = base_end - pd.Timedelta(minutes=lookback_min)
                if eff_start is None or pd.to_datetime(eff_start) < cutoff:
                    eff_start = cutoff

            if eff_start is not None:
                where.append("datetime >= ?")
                params.append(pd.to_datetime(eff_start).strftime("%Y-%m-%d %H:%M:%S"))

            if end_dt is not None:
                where.append("datetime <= ?")
                params.append(pd.to_datetime(end_dt).strftime("%Y-%m-%d %H:%M:%S"))

            sql = f"SELECT * FROM {quote_ident(RANKING_SNAPSHOT_TABLE)}"
            if where:
                sql += " WHERE " + " AND ".join(where)
            # 直近側を確実に取るため DESC + LIMIT で切ってから昇順へ戻す。
            max_rows = _bootstrap_max_rows()
            if max_rows > 0:
                sql += " ORDER BY datetime DESC LIMIT %d" % int(max_rows)
            else:
                sql += " ORDER BY datetime ASC"

            df = pd.read_sql_query(sql, conn, params=params)

            if max_rows > 0 and not df.empty and "datetime" in df.columns:
                df = df.sort_values("datetime", kind="stable").reset_index(drop=True)

            logger.warning(
                "[RANKING SUMMARY BOOTSTRAP LOADER] loaded ranking snapshot rows=%d "
                "start=%s (requested=%s) end=%s lookback_min=%s max_rows=%s "
                "(bootstrap の読み込み量を有界化。RANKING_BOOTSTRAP_LOOKBACK_MIN / "
                "RANKING_BOOTSTRAP_MAX_ROWS で調整可)",
                len(df),
                eff_start,
                start_dt,
                end_dt,
                lookback_min,
                max_rows,
            )
            return df

    except Exception:
        logger.exception(
            "[RANKING SUMMARY BOOTSTRAP LOADER] load ranking snapshot failed db=%s",
            ranking_db_path,
        )
        return pd.DataFrame()


def load_ranking_snapshot(
    ranking_db_path: str,
    *,
    start_dt: pd.Timestamp | None = None,
    end_dt: pd.Timestamp | None = None,
) -> pd.DataFrame:
    fn = resolve_callable(
        [
            ("trading.ranking.summary.loader", "load_ranking_snapshot_1min"),
            ("trading.ranking.summary.loader", "load_ranking_snapshot"),
            ("trading.ranking.summary.snapshot_loader", "load_ranking_snapshot_1min"),
        ]
    )

    if callable(fn):
        for kwargs in [
            {"db_path": ranking_db_path, "start_dt": start_dt, "end_dt": end_dt},
            {"ranking_db_path": ranking_db_path, "start_dt": start_dt, "end_dt": end_dt},
            {"start_dt": start_dt, "end_dt": end_dt},
            {"start_datetime": start_dt, "end_datetime": end_dt},
        ]:
            try:
                clean = {k: v for k, v in kwargs.items() if v is not None}
                out = fn(**clean)
                if isinstance(out, pd.DataFrame):
                    logger.info(
                        "[RANKING SUMMARY BOOTSTRAP LOADER] loaded via existing snapshot loader rows=%d",
                        len(out),
                    )
                    return out
            except TypeError:
                continue
            except Exception:
                logger.exception(
                    "[RANKING SUMMARY BOOTSTRAP LOADER] existing snapshot loader failed"
                )
                break

    return load_ranking_snapshot_fallback(
        ranking_db_path,
        start_dt=start_dt,
        end_dt=end_dt,
    )


def load_push_summary_fallback(
    summary_db_path: str,
    *,
    interval: int,
    symbols: list[str] | None = None,
    start_dt: pd.Timestamp | None = None,
    end_dt: pd.Timestamp | None = None,
) -> pd.DataFrame:
    table = PUSH_SUMMARY_TABLES.get(interval, f"stock_summary_{interval}min")

    if not os.path.exists(summary_db_path):
        logger.warning(
            "[RANKING SUMMARY BOOTSTRAP LOADER] push summary db not found path=%s",
            summary_db_path,
        )
        return pd.DataFrame()

    try:
        with connect_sqlite(summary_db_path, readonly=True) as conn:
            if not table_exists(conn, table):
                logger.warning(
                    "[RANKING SUMMARY BOOTSTRAP LOADER] push summary table not found db=%s table=%s",
                    summary_db_path,
                    table,
                )
                return pd.DataFrame()

            where = []
            params: list[Any] = []

            if start_dt is not None:
                where.append("datetime >= ?")
                params.append(pd.to_datetime(start_dt).strftime("%Y-%m-%d %H:%M:%S"))

            if end_dt is not None:
                where.append("datetime <= ?")
                params.append(pd.to_datetime(end_dt).strftime("%Y-%m-%d %H:%M:%S"))

            if symbols:
                symbols = [str(s).strip() for s in symbols if str(s).strip()]
                if symbols:
                    placeholders = ",".join(["?"] * len(symbols))
                    where.append(f"symbol IN ({placeholders})")
                    params.extend(symbols)

            sql = f"SELECT * FROM {quote_ident(table)}"
            if where:
                sql += " WHERE " + " AND ".join(where)
            sql += " ORDER BY datetime ASC"

            df = pd.read_sql_query(sql, conn, params=params)

            logger.info(
                "[RANKING SUMMARY BOOTSTRAP LOADER] loaded push summary interval=%s rows=%d table=%s",
                interval,
                len(df),
                table,
            )
            return df

    except Exception:
        logger.exception(
            "[RANKING SUMMARY BOOTSTRAP LOADER] load push summary failed interval=%s db=%s",
            interval,
            summary_db_path,
        )
        return pd.DataFrame()


def load_push_summary(
    summary_db_path: str,
    *,
    interval: int,
    symbols: list[str] | None = None,
    start_dt: pd.Timestamp | None = None,
    end_dt: pd.Timestamp | None = None,
) -> pd.DataFrame:
    fn = resolve_callable(
        [
            ("trading.ranking.summary.loader", "load_push_summary_for_ranking"),
            ("trading.ranking.summary.loader", "load_push_summary"),
            ("trading.summary.persistence.summary_loader", "load_summary_df"),
            ("trading.summary.persistence.summary_loader", "load_summary"),
        ]
    )

    if callable(fn):
        for kwargs in [
            {
                "db_path": summary_db_path,
                "interval": interval,
                "symbols": symbols,
                "start_dt": start_dt,
                "end_dt": end_dt,
            },
            {
                "summary_db_path": summary_db_path,
                "interval": interval,
                "symbols": symbols,
                "start_dt": start_dt,
                "end_dt": end_dt,
            },
            {
                "interval": interval,
                "symbols": symbols,
                "start_dt": start_dt,
                "end_dt": end_dt,
            },
        ]:
            try:
                clean = {k: v for k, v in kwargs.items() if v is not None}
                out = fn(**clean)
                if isinstance(out, pd.DataFrame):
                    logger.info(
                        "[RANKING SUMMARY BOOTSTRAP LOADER] loaded via existing push loader interval=%s rows=%d",
                        interval,
                        len(out),
                    )
                    return out
            except TypeError:
                continue
            except Exception:
                logger.exception(
                    "[RANKING SUMMARY BOOTSTRAP LOADER] existing push loader failed"
                )
                break

    return load_push_summary_fallback(
        summary_db_path,
        interval=interval,
        symbols=symbols,
        start_dt=start_dt,
        end_dt=end_dt,
    )


__all__ = [
    "resolve_callable",
    "load_ranking_snapshot_fallback",
    "load_ranking_snapshot",
    "load_push_summary_fallback",
    "load_push_summary",
]