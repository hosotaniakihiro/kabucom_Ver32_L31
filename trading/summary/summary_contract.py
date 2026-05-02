# ============================================================
# summary_contract.py
# ------------------------------------------------------------
# ✔ summary_dict 契約の唯一の定義
# ✔ 型不正 / 空 dict / 非 DataFrame は即 FATAL
# ✔ ★ empty DataFrame は許容（起動直後・寄り前対応）
# ✔ initial / incremental / bulk / loader 全共通
# ✔ ★ key 正規化チェック追加
# ✔ ★ 許可キー限定（誤キー完全排除）
# ✔ ★ 異常メッセージ強化
# ✔ ★ None / 異常混入防止
# ============================================================

from __future__ import annotations

import pandas as pd
from typing import Dict
from trading.logger.timeframe_logger import log_tf_close

# ============================================================
# 許可キー定義（正規契約）
# ============================================================

_ALLOWED_KEYS = {
    "1min", "3min", "5min",
    "1m", "3m", "5m",
}


# ============================================================
# 契約チェック本体
# ============================================================

def assert_summary_dict(
    summary_dict,
    *,
    caller: str = "unknown",
) -> Dict[str, pd.DataFrame]:
    """
    summary_dict 契約を強制する

    Contract:
      - dict[str, DataFrame]
      - key: "1min" / "3min" / "5min" / "1m" / "3m" / "5m"
      - value: pandas.DataFrame（empty 可）
    """

    # --------------------------------------------------------
    # None チェック
    # --------------------------------------------------------
    if summary_dict is None:
        raise TypeError(
            f"[SUMMARY_CONTRACT_FATAL] {caller}: "
            f"summary_dict is None"
        )

    # --------------------------------------------------------
    # 型チェック
    # --------------------------------------------------------
    if not isinstance(summary_dict, dict):
        raise TypeError(
            f"[SUMMARY_CONTRACT_FATAL] {caller}: "
            f"summary_dict is not dict: "
            f"type={type(summary_dict)} "
            f"value={summary_dict}"
        )

    # --------------------------------------------------------
    # ★ dict が空なのは致命
    # --------------------------------------------------------
    if len(summary_dict) == 0:
        raise TypeError(
            f"[SUMMARY_CONTRACT_FATAL] {caller}: "
            f"summary_dict is empty"
        )

    # --------------------------------------------------------
    # key / value チェック
    # --------------------------------------------------------
    for k, v in summary_dict.items():

        # ------------------------
        # key 型チェック
        # ------------------------
        if not isinstance(k, str):
            raise TypeError(
                f"[SUMMARY_CONTRACT_FATAL] {caller}: "
                f"invalid key type={type(k)} key={k}"
            )

        # ------------------------
        # 許可キー限定（★追加）
        # ------------------------
        if k not in _ALLOWED_KEYS:
            raise TypeError(
                f"[SUMMARY_CONTRACT_FATAL] {caller}: "
                f"invalid key='{k}' "
                f"allowed_keys={sorted(_ALLOWED_KEYS)}"
            )

        # ------------------------
        # value 型チェック
        # ------------------------
        if not isinstance(v, pd.DataFrame):
            raise TypeError(
                f"[SUMMARY_CONTRACT_FATAL] {caller}: "
                f"summary_dict['{k}'] is not DataFrame: "
                f"type={type(v)} value={v}"
            )

        # ------------------------
        # ★ empty DataFrame は正常
        # ------------------------
        if v.empty:
            # 起動直後 / 寄り前 は正常
            # ロガー依存を増やさないため出力なし
            pass

    return summary_dict