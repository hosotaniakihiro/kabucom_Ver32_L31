# ============================================================
# File   : trading/scoring/flags/flag_generator.py
# Version: Ver2.0-PRODUCTION-FLAG-GENERATOR-TIME-WINDOW-STABLE
# ------------------------------------------------------------
# ✔ Ver1.9 全機能保持（削除ゼロ）
# ✔ 全flag module統合
# ✔ time_window_flags 統合
# ✔ score_config.ini 完全対応
# ✔ add_scores 完全互換
# ✔ DataFrame in / out
# ✔ indicator欠損安全
# ✔ NaN / inf 安全
# ✔ vectorized高速処理
# ✔ フラグ生成責務完全分離
# ✔ bool → int 自動変換
# ✔ 列名ゆらぎ完全吸収
# ✔ 巨大DataFrame耐性
# ✔ module fail safe
# ✔ indicator列自動検出
# ✔ logger強化
# ✔ pandas alignment crash防止
# ✔ dtype stabilization
# ✔ MultiIndex安全化
# ✔ DEBUGログ制御
# ✔ memory safe
# ✔ flag column auto guarantee
# ✔ module merge safe
# ✔ duplicate column guard
# ✔ 約120 flags + time window flags
# ✔ production ultra stable
# ============================================================

from __future__ import annotations

import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

LOG_FLAG_GENERATOR = False


# ============================================================
# SAFE IMPORT
# ============================================================

def _safe_import(module_path, func):

    try:
        module = __import__(module_path, fromlist=[func])
        return getattr(module, func)
    except Exception:
        logger.warning(f"[FLAG GEN] module import failed: {module_path}")
        return None


generate_structure_flags_pro = _safe_import(
    "trading.scoring.flags.structure_flags_pro",
    "generate_structure_flags_pro"
)

generate_trend_flags = _safe_import(
    "trading.scoring.flags.trend_flags",
    "generate_trend_flags"
)

generate_trend_flags_pro = _safe_import(
    "trading.scoring.flags.trend_flags_pro",
    "generate_trend_flags_pro"
)

generate_range_flags = _safe_import(
    "trading.scoring.flags.range_flags",
    "generate_range_flags"
)

generate_pullback_flags = _safe_import(
    "trading.scoring.flags.pullback_flags",
    "generate_pullback_flags"
)

generate_momentum_flags = _safe_import(
    "trading.scoring.flags.momentum_flags",
    "generate_momentum_flags"
)

generate_volume_flags = _safe_import(
    "trading.scoring.flags.volume_flags",
    "generate_volume_flags"
)

generate_orderflow_flags = _safe_import(
    "trading.scoring.flags.orderflow_flags",
    "generate_orderflow_flags"
)

generate_pattern_flags = _safe_import(
    "trading.scoring.flags.pattern_flags",
    "generate_pattern_flags"
)

generate_wick_flags = _safe_import(
    "trading.scoring.flags.wick_flags",
    "generate_wick_flags"
)

generate_combo_flags = _safe_import(
    "trading.scoring.flags.combo_flags",
    "generate_combo_flags"
)

generate_ai_flags = _safe_import(
    "trading.scoring.flags.ai_flags",
    "generate_ai_flags"
)

generate_tosama_flags = _safe_import(
    "trading.scoring.flags.tosama_flags",
    "generate_tosama_flags"
)

# ------------------------------------------------------------
# NEW: time window flags
# ------------------------------------------------------------
generate_time_window_flags = _safe_import(
    "trading.scoring.flags.time_window_flags",
    "generate_time_window_flags"
)


# ============================================================
# SAFE NUMERIC
# ============================================================

def _safe_numeric(series):

    if series is None:
        return None

    try:
        s = pd.to_numeric(series, errors="coerce")

        if isinstance(s, pd.Series):
            s = s.replace([np.inf, -np.inf], np.nan)

        return s

    except Exception:
        return series


# ============================================================
# DATAFRAME SANITIZE
# ============================================================

def _sanitize_dataframe(df):

    if not isinstance(df, pd.DataFrame):
        return None

    if df.empty:
        return df

    try:

        if isinstance(df.columns, pd.MultiIndex):

            df.columns = [
                "_".join([str(x) for x in c if x not in ("", None)])
                for c in df.columns
            ]

        df.columns = [str(c) for c in df.columns]

    except Exception:
        pass

    return df

# ============================================================
# COLUMN NORMALIZATION
# ============================================================

def _normalize_columns(df):

    if df is None or df.empty:
        return df

    lower_map = {c.lower(): c for c in df.columns}

    def rename(src, dst):

        if src in lower_map and dst not in df.columns:

            try:
                df[dst] = df[lower_map[src]]
            except Exception:
                pass

    rename("close", "close_price")
    rename("last", "close_price")
    rename("price", "close_price")

    rename("open", "open_price")
    rename("high", "high_price")
    rename("low", "low_price")

    rename("closeprice", "close_price")
    rename("openprice", "open_price")
    rename("highprice", "high_price")
    rename("lowprice", "low_price")

    rename("vol", "volume")
    rename("volume_total", "volume")

    rename("ma_5", "ma5")
    rename("ma_25", "ma25")
    rename("ma_75", "ma75")

    rename("sma5", "ma5")
    rename("sma25", "ma25")
    rename("sma75", "ma75")

    rename("ema_12", "ema12")
    rename("ema_26", "ema26")

    rename("macd_signal", "signal")

    rename("bbupper", "bb_upper")
    rename("bblower", "bb_lower")
    rename("bbmid", "bb_mid")

    rename("vwap_price", "vwap")

    # datetime aliases
    rename("time", "datetime")
    rename("timestamp", "datetime")

    return df


# ============================================================
# NUMERIC SANITIZE
# ============================================================

def _sanitize_numeric_columns(df):

    numeric_cols = [

        "open_price",
        "close_price",
        "high_price",
        "low_price",

        "volume",
        "vwap",

        "ma5",
        "ma25",
        "ma75",

        "ema12",
        "ema26",

        "macd",
        "signal",
        "hist",

        "rsi",
        "rci",

        "atr",

        "bb_upper",
        "bb_lower",
        "bb_mid",

        # optional score-ish numeric columns often reused by flag modules
        "score",
        "score_buy",
        "score_sell",
        "score_total",
        "final_score",
        "display_score",
        "score_mtf",
        "score_slope",
        "slope",
        "slope_atr_scaled",
    ]

    for c in numeric_cols:

        if c in df.columns:

            try:
                df[c] = _safe_numeric(df[c])
            except Exception:
                pass

    return df


# ============================================================
# FLAG NORMALIZATION
# ============================================================

def _normalize_flag_columns(df):

    flag_cols = [c for c in df.columns if c.startswith("flag_")]

    for c in flag_cols:

        try:

            df[c] = (
                pd.to_numeric(df[c], errors="coerce")
                .replace([np.inf, -np.inf], np.nan)
                .fillna(0)
                .astype(int)
            )

        except Exception:

            df[c] = 0

    return df


# ============================================================
# FLAG GUARANTEE
# ============================================================

def _ensure_flag_columns(df):

    flag_cols = [c for c in df.columns if c.startswith("flag_")]

    if len(flag_cols) == 0:

        logger.warning("[FLAG GEN] no flag columns generated")

        df["flag_dummy"] = 0

    return df


# ============================================================
# SAFE MERGE
# ============================================================

def _safe_merge_flags(df_base, df_new):

    if df_base is None or not isinstance(df_base, pd.DataFrame):
        return df_base

    if df_new is None or not isinstance(df_new, pd.DataFrame):
        return df_base

    try:

        new_cols = [c for c in df_new.columns if str(c).startswith("flag_")]

        if not new_cols:
            return df_base

        if df_new.empty:
            logger.warning(
                "[FLAG GEN] merge skipped: new flag DataFrame is empty base_rows=%s",
                len(df_base),
            )
            return df_base

        base_len = len(df_base)
        new_len = len(df_new)

        if base_len == 0:
            return df_base

        # pandasのindex自動整列で MultiIndex / RangeIndex 混在時に落ちるため、
        # 必ず位置ベースのSeriesに変換してから base の行数へ合わせる。
        for c in new_cols:

            try:
                s = df_new[c]

                # 重複列名の場合は DataFrame になることがあるため先頭列だけ採用
                if isinstance(s, pd.DataFrame):
                    s = s.iloc[:, 0]

                s = s.reset_index(drop=True)

                if new_len >= base_len:
                    values = s.iloc[:base_len].values
                else:
                    values = pd.Series(0, index=range(base_len))
                    values.iloc[:new_len] = s.values
                    values = values.values

                df_base[c] = values

            except Exception:
                logger.exception(
                    "[FLAG GEN] merge column failed col=%s base_rows=%s new_rows=%s",
                    c,
                    base_len,
                    new_len,
                )
                if c not in df_base.columns:
                    df_base[c] = 0

    except Exception:

        logger.exception("[FLAG GEN] merge failed")

    return df_base


# ============================================================
# SAFE MODULE CALL
# ============================================================

def _safe_call(module_func, df):

    if module_func is None:

        logger.debug("[FLAG GEN] module missing")
        return df

    try:

        df_new = module_func(df.copy())

        if not isinstance(df_new, pd.DataFrame):

            logger.warning("[FLAG GEN] module returned invalid")
            return df

        df = _safe_merge_flags(df, df_new)

        return df

    except Exception:

        logger.exception(f"[FLAG GEN] module failed: {module_func}")
        return df


# ============================================================
# DEBUG
# ============================================================

def _debug_flag_columns(df):

    if not LOG_FLAG_GENERATOR:
        return

    try:

        flag_cols = [c for c in df.columns if c.startswith("flag_")]

        logger.info(f"[FLAG GEN] flags={len(flag_cols)}")

        if len(flag_cols) < 5:
            logger.warning("[FLAG GEN] suspicious flag count")

        # optional: time window flags summary
        tw_cols = [c for c in flag_cols if "open_" in c or "lunch" in c or "close_" in c or "reentry_1400" in c]
        if tw_cols:
            logger.info(f"[FLAG GEN] time_window_flags={len(tw_cols)}")

    except Exception:
        pass


# ============================================================
# MAIN
# ============================================================

def generate_all_flags(df: pd.DataFrame) -> pd.DataFrame:

    if df is None:
        return df

    df = _sanitize_dataframe(df)

    if df is None or df.empty:
        return df

    try:

        df = _normalize_columns(df)

        df = _sanitize_numeric_columns(df)

        # ====================================================
        # flag modules（プロ順序）
        #   市場構造 → トレンド → レンジ/押し目 → モメンタム/出来高
        #   → 板/ローソク/ヒゲ → 時間帯 → コンボ → AI → 殿様
        # ====================================================

        df = _safe_call(generate_structure_flags_pro, df)

        df = _safe_call(generate_trend_flags_pro, df)
        df = _safe_call(generate_trend_flags, df)

        df = _safe_call(generate_range_flags, df)
        df = _safe_call(generate_pullback_flags, df)

        df = _safe_call(generate_momentum_flags, df)
        df = _safe_call(generate_volume_flags, df)

        df = _safe_call(generate_orderflow_flags, df)

        df = _safe_call(generate_pattern_flags, df)
        df = _safe_call(generate_wick_flags, df)

        # NEW: time window flags
        # ローソク/出来高/VWAP/MA系が先に整っている前提で後段に置く
        df = _safe_call(generate_time_window_flags, df)

        df = _safe_call(generate_combo_flags, df)

        df = _safe_call(generate_ai_flags, df)

        df = _safe_call(generate_tosama_flags, df)

        # ====================================================
        # normalize flags
        # ====================================================

        df = _normalize_flag_columns(df)

        df = _ensure_flag_columns(df)

        _debug_flag_columns(df)

        return df

    except Exception:

        logger.exception("[FLAG GEN] generation failed")

        return df
