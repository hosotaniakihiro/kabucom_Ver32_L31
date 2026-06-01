from __future__ import annotations

import logging
import pandas as pd

logger = logging.getLogger(__name__)
_INSTALLED = False


def install() -> bool:
    global _INSTALLED
    if _INSTALLED:
        return True
    try:
        import trading.yahoo.pipeline.complement.compute as c
        old = getattr(c, 'ensure_actual_db_schema_columns', None)
        if getattr(old, '_na_guard_v1', False):
            _INSTALLED = True
            return True

        def patched(df, interval):
            try:
                out = c.safe_df(df)
                if out.empty:
                    return pd.DataFrame()
                table = c.summary_table_for_interval(interval)
                db_cols = c.get_table_columns(table)
                if not db_cols:
                    return out
                before_cols = set(map(str, out.columns))
                added_cols = []
                zero_filled_cols = []
                for col in db_cols:
                    if col == 'id' or col in out.columns:
                        continue
                    dv = c._default_value_for_missing_db_col(col)
                    out[col] = dv
                    added_cols.append(col)
                    is_zero = False
                    try:
                        if not pd.isna(dv) and not isinstance(dv, bool):
                            is_zero = isinstance(dv, (int, float)) and float(dv) == 0.0
                    except Exception:
                        is_zero = False
                    if is_zero:
                        zero_filled_cols.append(col)
                after_cols = set(map(str, out.columns))
                still_missing = [x for x in db_cols if x != 'id' and x not in after_cols]
                computed_or_existing = [x for x in db_cols if x != 'id' and x in before_cols]
                logger.warning('[YAHOO SUMMARY SCHEMA CHECK] table=%s interval=%s db_cols=%s df_cols_before=%s df_cols_after=%s added_cols=%s zero_filled_cols=%s still_missing=%s computed_or_existing=%s', table, interval, len(db_cols), len(before_cols), len(out.columns), added_cols[:120], zero_filled_cols[:120], still_missing[:120], computed_or_existing[:120])
                preferred = [x for x in db_cols if x in out.columns and x != 'id']
                others = [x for x in out.columns if x not in preferred]
                return out[preferred + others].copy()
            except Exception:
                logger.exception('[YAHOO COMPUTE] ensure actual db schema columns failed interval=%s', interval)
                return c.safe_df(df)

        patched._na_guard_v1 = True
        patched._original = old
        c.ensure_actual_db_schema_columns = patched
        _INSTALLED = True
        logger.warning('[YAHOO COMPUTE SCHEMA NA GUARD] installed V1')
        return True
    except Exception:
        logger.exception('[YAHOO COMPUTE SCHEMA NA GUARD] install failed')
        return False

try:
    install()
except Exception:
    logger.exception('[YAHOO COMPUTE SCHEMA NA GUARD] auto install failed')

__all__ = ['install']
