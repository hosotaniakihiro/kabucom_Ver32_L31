# ============================================================
# core/bootstrap/load_symbol_map.py
# Ver1.0-PRODUCTION-SYMBOL-MASTER-LOADER
# ------------------------------------------------------------
# ✔ optional_data から symbol_name_map 生成
# ✔ symbol型安全化
# ✔ 列名自動検出
# ✔ NaN / None / 空白 排除
# ✔ 高速dict生成
# ✔ global_dataへ保存
# ✔ 起動時1回ロード
# ============================================================

from __future__ import annotations

import logging
import pandas as pd

from global_state import global_data

logger = logging.getLogger(__name__)


# ============================================================
# utility
# ============================================================

def _detect_symbol_column(df: pd.DataFrame):

    candidates = [
        "symbol",
        "code",
        "銘柄コード",
        "証券コード",
    ]

    for c in candidates:
        if c in df.columns:
            return c

    return None


def _detect_name_column(df: pd.DataFrame):

    candidates = [
        "symbolname",
        "name",
        "company_name",
        "銘柄名",
        "銘柄名称",
    ]

    for c in candidates:
        if c in df.columns:
            return c

    return None


# ============================================================
# main builder
# ============================================================

def build_symbol_name_map():

    try:

        opt = getattr(global_data, "optional_data", None)

        if opt is None:

            logger.warning("[SYMBOL MAP] optional_data not loaded")
            global_data.symbol_name_map = {}
            return

        if not isinstance(opt, pd.DataFrame) or opt.empty:

            logger.warning("[SYMBOL MAP] optional_data empty")
            global_data.symbol_name_map = {}
            return

        df = opt.copy()

        # ----------------------------------------------------
        # 列検出
        # ----------------------------------------------------

        sym_col = _detect_symbol_column(df)
        name_col = _detect_name_column(df)

        if sym_col is None:

            logger.warning("[SYMBOL MAP] symbol column not found")
            global_data.symbol_name_map = {}
            return

        if name_col is None:

            logger.warning("[SYMBOL MAP] name column not found")
            global_data.symbol_name_map = {}
            return

        # ----------------------------------------------------
        # 型安全化
        # ----------------------------------------------------

        df[sym_col] = df[sym_col].astype(str)

        df[name_col] = (
            df[name_col]
            .astype(str)
            .str.strip()
        )

        # ----------------------------------------------------
        # 無効銘柄名除去
        # ----------------------------------------------------

        df = df[
            (df[name_col] != "")
            & (df[name_col] != "nan")
            & (df[name_col] != "None")
        ]

        # ----------------------------------------------------
        # map生成
        # ----------------------------------------------------

        symbol_map = dict(
            zip(
                df[sym_col],
                df[name_col]
            )
        )

        global_data.symbol_name_map = symbol_map

        logger.info(
            "[SYMBOL MAP] loaded %d symbols",
            len(symbol_map)
        )

    except Exception:

        logger.exception("[SYMBOL MAP] build failed")

        global_data.symbol_name_map = {}