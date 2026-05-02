# ============================================================
# File   : trading/summary/ma_conf_splitter.py
# Ver    : 1.0.0-FINAL-MA-CONF-SPLITTER-ABSOLUTE
# ------------------------------------------------------------
# ✔ MA conf を時間帯・銘柄タイプ別に split する専用ユーティリティ
# ✔ ma_conf_utils を唯一の閾値決定元とする（思想一元化）
# ✔ summary / entry_gate / AI すべてから利用可能
# ✔ DataFrame / dict / scalar すべて対応
# ✔ DB・cache への副作用ゼロ
# ============================================================

from __future__ import annotations

import datetime as dt
import pandas as pd
from typing import Literal, Dict, Any

from trading.summary.ma_conf_utils import (
    get_ma_conf_threshold,
    is_ma_conf_sufficient,
)

# ============================================================
# 型定義
# ============================================================

MAType = Literal["ma5", "ma25", "ma75"]
SymbolType = Literal["large", "normal", "small"]


# ============================================================
# 内部ユーティリティ
# ============================================================

def _safe_float(v, default: float = 0.0) -> float:
    try:
        v = float(v)
        return v if v == v else default  # NaN guard
    except Exception:
        return default


def _resolve_now(now: dt.datetime | None) -> dt.datetime:
    return now if isinstance(now, dt.datetime) else dt.datetime.now()


# ============================================================
# 公開API①：単一 conf 判定（dict / row 用）
# ============================================================

def split_ma_conf(
    *,
    ma: MAType,
    conf: float | None,
    now: dt.datetime | None = None,
    symbol_type: SymbolType = "normal",
) -> Dict[str, Any]:
    """
    MA conf を時間帯・銘柄タイプ別に評価して返す

    Returns
    -------
    {
        "ok": bool,
        "conf": float,
        "threshold": float,
        "ma": str,
    }
    """

    now = _resolve_now(now)
    conf_v = _safe_float(conf, 0.0)

    ok, threshold = is_ma_conf_sufficient(
        ma=ma,
        conf=conf_v,
        now=now,
        symbol_type=symbol_type,
    )

    return {
        "ok": bool(ok),
        "conf": conf_v,
        "threshold": float(threshold),
        "ma": ma,
    }


# ============================================================
# 公開API②：DataFrame 用一括 split
# ============================================================

def split_ma_conf_df(
    df: pd.DataFrame,
    *,
    now: dt.datetime | None = None,
    symbol_type: SymbolType = "normal",
    ma_cols: Dict[MAType, str] | None = None,
    prefix: str = "ma_conf",
) -> pd.DataFrame:
    """
    DataFrame に MA conf 判定列を付与する

    Parameters
    ----------
    df : DataFrame
        対象 DF（summary / scoring 後を想定）
    ma_cols : dict
        {
            "ma5":  "ma5_conf",
            "ma25": "ma25_conf",
            "ma75": "ma75_conf",
        }
    prefix : str
        出力列 prefix（default: ma_conf）

    生成列例
    -------
    ma_conf_ma75_ok
    ma_conf_ma75_threshold
    """

    if df is None or df.empty:
        return df

    now = _resolve_now(now)

    if ma_cols is None:
        ma_cols = {
            "ma5": "ma5_conf",
            "ma25": "ma25_conf",
            "ma75": "ma75_conf",
        }

    df_out = df.copy()

    for ma, col in ma_cols.items():
        if col not in df_out.columns:
            continue

        oks = []
        thresholds = []

        for _, row in df_out.iterrows():
            conf = row.get(col)
            ok, threshold = is_ma_conf_sufficient(
                ma=ma,
                conf=_safe_float(conf),
                now=now,
                symbol_type=symbol_type,
            )
            oks.append(bool(ok))
            thresholds.append(float(threshold))

        df_out[f"{prefix}_{ma}_ok"] = oks
        df_out[f"{prefix}_{ma}_threshold"] = thresholds

    return df_out


# ============================================================
# 公開API③：ENTRY_GATE 用 quick 判定
# ============================================================

def is_entry_ma_conf_ok(
    *,
    row: Dict[str, Any],
    ma: MAType = "ma75",
    now: dt.datetime | None = None,
    symbol_type: SymbolType = "normal",
    conf_key: str | None = None,
) -> bool:
    """
    ENTRY 判定用の簡易 MA conf チェック
    （理由付けは entry_gate 側で行う想定）

    Returns
    -------
    bool
    """

    if not isinstance(row, dict):
        return False

    now = _resolve_now(now)

    if conf_key is None:
        conf_key = f"{ma}_conf"

    conf = row.get(conf_key)

    ok, _ = is_ma_conf_sufficient(
        ma=ma,
        conf=_safe_float(conf),
        now=now,
        symbol_type=symbol_type,
    )

    return bool(ok)
