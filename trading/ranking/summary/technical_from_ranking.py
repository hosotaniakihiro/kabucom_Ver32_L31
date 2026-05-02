# ============================================================
# File   : trading/ranking/summary/technical_from_ranking.py
# Version: PRODUCTION-STABLE-REV1.3-COMPAT-SHIM-DF-OPTIONAL
# ------------------------------------------------------------
# 【概要】
#   ranking summary technical indicator の旧import互換 shim
#
# 【目的】
#   - 旧 import:
#       trading.ranking.summary.technical_from_ranking
#     を復旧する
#
#   - 実体:
#       trading.ranking.summary.technicals
#     へ委譲する
#
# 【REV1.3】
#   - build_ranking_summary_technical(df=None, ...) に変更
#   - runner.py 側が df を渡さない呼び出しでも TypeError で落ちない
#   - technicals.py 側に build_ranking_summary_technical があれば優先委譲
#   - なければ _apply_technical_indicators に委譲
#   - df が無い場合は kwargs 内の df/source_df/base_df/ranking_df/rows を探索
#   - 最後まで DataFrame が無ければ空 DataFrame を返して起動継続
#
# 【重要】
#   - 機能本体は technicals.py
#   - このファイルは互換維持専用
#   - ranking_summary_engine / runtime_state / __init__.py の
#     import failure を防ぐ
# ============================================================

from __future__ import annotations

import importlib
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# technicals.py の安全ロード
# ============================================================

_TECH_MODULE_NAME = "trading.ranking.summary.technicals"

try:
    _technicals = importlib.import_module(_TECH_MODULE_NAME)
except Exception:
    _technicals = None
    logger.exception(
        "[technical_from_ranking] failed to import %s",
        _TECH_MODULE_NAME,
    )


def _get_attr(name: str, default: Any = None) -> Any:
    """
    technicals.py から属性を安全取得する。
    欠落していても、この shim の import 自体は失敗させない。
    """
    if _technicals is None:
        return default

    try:
        return getattr(_technicals, name, default)
    except Exception:
        logger.exception(
            "[technical_from_ranking] getattr failed name=%s",
            name,
        )
        return default


def _to_dataframe(obj: Any) -> Any:
    """
    任意オブジェクトを DataFrame 化する。
    pandas import 失敗時は元オブジェクトを返す。
    """
    try:
        import pandas as pd

        if obj is None:
            return pd.DataFrame()

        if isinstance(obj, pd.DataFrame):
            return obj

        return pd.DataFrame(obj)

    except Exception:
        return obj


def _empty_dataframe() -> Any:
    try:
        import pandas as pd

        return pd.DataFrame()
    except Exception:
        return None


def _identity_df(df: Any = None, *args: Any, **kwargs: Any) -> Any:
    """
    fallback:
      technicals.py が読めない場合でも DataFrame をそのまま返す。
    """
    if df is None:
        return _empty_dataframe()
    return df


def _empty_none(*args: Any, **kwargs: Any) -> None:
    """
    fallback:
      欠落関数用 no-op。
    """
    return None


def _extract_df_from_args_kwargs(
    df: Any = None,
    args: tuple[Any, ...] = (),
    kwargs: Optional[dict[str, Any]] = None,
) -> Any:
    """
    df が明示されていない場合に、args / kwargs から DataFrame 候補を探す。

    対応候補:
      - df
      - source_df
      - base_df
      - ranking_df
      - summary_df
      - rows
      - data
    """
    if kwargs is None:
        kwargs = {}

    if df is not None:
        return df

    # args の先頭が DataFrame / list[dict] などなら採用
    if args:
        first = args[0]
        if first is not None:
            return first

    for key in (
        "df",
        "source_df",
        "base_df",
        "ranking_df",
        "summary_df",
        "rows",
        "data",
    ):
        val = kwargs.get(key)
        if val is not None:
            return val

    return None


# ============================================================
# Constants
# ============================================================

TECH_MIN_BARS_FOR_SLOPE = _get_attr("TECH_MIN_BARS_FOR_SLOPE", 3)
TECH_MIN_BARS_FOR_RSI = _get_attr("TECH_MIN_BARS_FOR_RSI", 14)
TECH_MIN_BARS_FOR_MACD = _get_attr("TECH_MIN_BARS_FOR_MACD", 26)

TECHNICAL_COLUMNS = _get_attr(
    "TECHNICAL_COLUMNS",
    [
        "score_slope",
        "slope",
        "rsi",
        "macd",
        "macd_signal",
        "macd_hist",
        "atr",
        "vwap",
        "best_rank",
    ],
)


# ============================================================
# Delegated functions from technicals.py
# ============================================================

set_indicator_mode = _get_attr("set_indicator_mode", _empty_none)
get_indicator_mode = _get_attr("get_indicator_mode", lambda *a, **k: None)

_resolve_external_indicator_fn = _get_attr(
    "_resolve_external_indicator_fn",
    lambda *a, **k: None,
)

_build_ohlcv_compatible = _get_attr(
    "_build_ohlcv_compatible",
    _identity_df,
)

_prepare_external_indicator_input = _get_attr(
    "_prepare_external_indicator_input",
    _identity_df,
)

_fallback_rsi = _get_attr(
    "_fallback_rsi",
    _identity_df,
)

_fallback_macd = _get_attr(
    "_fallback_macd",
    _identity_df,
)

_fallback_atr = _get_attr(
    "_fallback_atr",
    _identity_df,
)

_fallback_vwap = _get_attr(
    "_fallback_vwap",
    _identity_df,
)

_apply_fallback_indicators = _get_attr(
    "_apply_fallback_indicators",
    _identity_df,
)

_apply_external_indicators = _get_attr(
    "_apply_external_indicators",
    _identity_df,
)

_merge_indicator_output = _get_attr(
    "_merge_indicator_output",
    _identity_df,
)

_repair_best_rank_for_display = _get_attr(
    "_repair_best_rank_for_display",
    _identity_df,
)

_apply_technical_indicators = _get_attr(
    "_apply_technical_indicators",
    _identity_df,
)

# technicals.py 側に公開 build 関数がある場合は優先して使う
_core_build_ranking_summary_technical = _get_attr(
    "build_ranking_summary_technical",
    None,
)


# ============================================================
# Public compatibility functions
# ============================================================

def build_ranking_summary_technical(
    df: Any = None,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """
    旧 import 互換関数。

    旧:
      trading.ranking.summary.technical_from_ranking.build_ranking_summary_technical

    対応する呼び出し:
      1. build_ranking_summary_technical(df)
      2. build_ranking_summary_technical(df=...)
      3. build_ranking_summary_technical(source_df=...)
      4. build_ranking_summary_technical(base_df=...)
      5. build_ranking_summary_technical(ranking_df=...)
      6. build_ranking_summary_technical(interval=1, lookback=240, ...)
         ※ df が無い場合でも TypeError で落とさず空 DataFrame を返す

    優先順位:
      1. technicals.py の build_ranking_summary_technical があれば委譲
      2. なければ _apply_technical_indicators に委譲
      3. df が無ければ空 DataFrame
    """
    # --------------------------------------------------------
    # technicals.py に正式 build 関数がある場合は優先
    # --------------------------------------------------------
    if callable(_core_build_ranking_summary_technical):
        try:
            if df is None:
                return _core_build_ranking_summary_technical(*args, **kwargs)
            return _core_build_ranking_summary_technical(df, *args, **kwargs)

        except TypeError as e:
            logger.warning(
                "[technical_from_ranking] core build TypeError -> fallback. err=%s",
                e,
            )

        except Exception:
            logger.exception(
                "[technical_from_ranking] core build failed -> fallback"
            )

    # --------------------------------------------------------
    # df 候補を探索
    # --------------------------------------------------------
    source_df = _extract_df_from_args_kwargs(
        df=df,
        args=args,
        kwargs=kwargs,
    )

    if source_df is None:
        logger.warning(
            "[technical_from_ranking] build_ranking_summary_technical called without df. "
            "return empty DataFrame. kwargs_keys=%s",
            list(kwargs.keys()),
        )
        return _empty_dataframe()

    source_df = _to_dataframe(source_df)

    # --------------------------------------------------------
    # fallback technical attach
    # --------------------------------------------------------
    try:
        return _apply_technical_indicators(source_df)
    except TypeError:
        try:
            return _apply_technical_indicators(source_df, *args, **kwargs)
        except Exception:
            logger.exception(
                "[technical_from_ranking] _apply_technical_indicators failed"
            )
            return source_df
    except Exception:
        logger.exception(
            "[technical_from_ranking] _apply_technical_indicators failed"
        )
        return source_df


def get_latest_ranking_summary_rows(
    df: Any = None,
    *args: Any,
    **kwargs: Any,
) -> Any:
    """
    旧 import 互換:
      trading.ranking.summary.technical_from_ranking.get_latest_ranking_summary_rows

    ranking summary 用 DataFrame から、symbol ごとの最新行を返す。
    可能なら technical 指標を付与してから返す。

    処理:
      1. DataFrame 化
      2. technical 指標を付与
      3. datetime / end_time / start_time / time / inserted_at /
         created_at / snapshot_time のいずれかで symbol ごとの最新行を抽出
      4. 日時列がない場合は symbol ごとの最後の行を採用
    """
    try:
        import pandas as pd

        source_df = _extract_df_from_args_kwargs(
            df=df,
            args=args,
            kwargs=kwargs,
        )

        if source_df is None:
            return pd.DataFrame()

        if not isinstance(source_df, pd.DataFrame):
            source_df = pd.DataFrame(source_df)

        if source_df.empty:
            return source_df

        x = source_df.copy()

        # ----------------------------------------------------
        # technical columns を補完
        # ----------------------------------------------------
        try:
            x = build_ranking_summary_technical(x)
        except Exception:
            logger.exception(
                "[technical_from_ranking] technical attach failed in latest rows"
            )

        if x is None:
            return pd.DataFrame()

        if not isinstance(x, pd.DataFrame):
            x = pd.DataFrame(x)

        if x.empty:
            return x

        # ----------------------------------------------------
        # symbol 正規化
        # ----------------------------------------------------
        if "symbol" not in x.columns:
            return x.reset_index(drop=True)

        x["symbol"] = x["symbol"].astype(str).str.strip()
        x = x[x["symbol"] != ""].copy()

        if x.empty:
            return x.reset_index(drop=True)

        # ----------------------------------------------------
        # datetime 候補列を探索
        # ----------------------------------------------------
        dt_col: Optional[str] = None
        for c in (
            "datetime",
            "end_time",
            "start_time",
            "time",
            "inserted_at",
            "created_at",
            "snapshot_time",
        ):
            if c in x.columns:
                dt_col = c
                break

        # ----------------------------------------------------
        # 日時列がある場合: symbol ごと最新
        # ----------------------------------------------------
        if dt_col is not None:
            x["__latest_dt__"] = pd.to_datetime(
                x[dt_col],
                errors="coerce",
            )

            try:
                x["__latest_dt__"] = x["__latest_dt__"].dt.tz_localize(None)
            except Exception:
                pass

            x = x.sort_values(
                ["symbol", "__latest_dt__"],
                ascending=[True, False],
                na_position="last",
                kind="mergesort",
            )

            x = x.drop_duplicates(
                subset=["symbol"],
                keep="first",
            )

            return (
                x.drop(columns=["__latest_dt__"], errors="ignore")
                .reset_index(drop=True)
            )

        # ----------------------------------------------------
        # 日時列がない場合: symbol ごと最後の行
        # ----------------------------------------------------
        return (
            x.drop_duplicates(subset=["symbol"], keep="last")
            .reset_index(drop=True)
        )

    except Exception:
        import pandas as pd

        logger.exception(
            "[technical_from_ranking] get_latest_ranking_summary_rows failed"
        )
        return pd.DataFrame()


# ============================================================
# 旧名互換 alias
# ============================================================

apply_technical_from_ranking = build_ranking_summary_technical
add_technical_from_ranking = build_ranking_summary_technical
enrich_technical_from_ranking = build_ranking_summary_technical
build_technical_from_ranking = build_ranking_summary_technical


# ============================================================
# Export
# ============================================================

__all__ = [
    "TECH_MIN_BARS_FOR_SLOPE",
    "TECH_MIN_BARS_FOR_RSI",
    "TECH_MIN_BARS_FOR_MACD",
    "TECHNICAL_COLUMNS",
    "set_indicator_mode",
    "get_indicator_mode",
    "_resolve_external_indicator_fn",
    "_build_ohlcv_compatible",
    "_prepare_external_indicator_input",
    "_fallback_rsi",
    "_fallback_macd",
    "_fallback_atr",
    "_fallback_vwap",
    "_apply_fallback_indicators",
    "_apply_external_indicators",
    "_merge_indicator_output",
    "_repair_best_rank_for_display",
    "_apply_technical_indicators",
    "build_ranking_summary_technical",
    "apply_technical_from_ranking",
    "add_technical_from_ranking",
    "enrich_technical_from_ranking",
    "build_technical_from_ranking",
    "get_latest_ranking_summary_rows",
]