# ============================================================
# trading/signals/timeframe/multi_tf.py
# ------------------------------------------------------------
# ✔ 複数時間足（1m / 3m / 5m）の整合性チェック
# ✔ 上位足は「環境」、下位足は「トリガ」
# ✔ BUY / SELL 非依存
# ✔ entry_checker の前段フィルタ
# ============================================================

from typing import Dict

from trading.signals.factors import trend, momentum
from trading.signals.timeframe.single_tf import check_single_tf


# ============================================================
# Multi TF 判定
# ============================================================

def check_multi_tf(
    *,
    tf_1m: Dict,
    tf_3m: Dict,
    tf_5m: Dict,
) -> Dict[str, bool]:
    """
    複数時間足の状態を総合判定する

    引数:
        tf_1m / tf_3m / tf_5m:
            single_tf.check_single_tf に渡す row データ(dict)

    戻り値:
        {
            "tf1_ok": bool,
            "tf3_ok": bool,
            "tf5_ok": bool,
            "alignment_ok": bool,
            "multi_tf_ok": bool,
        }
    """

    # --- 各時間足の単体チェック ---
    res_1m = check_single_tf(**tf_1m)
    res_3m = check_single_tf(**tf_3m)
    res_5m = check_single_tf(**tf_5m)

    # --- トレンド方向（符号） ---
    def trend_dir(row: Dict) -> int:
        """
        +1: 上昇
        -1: 下降
         0: 不明 / レンジ
        """
        if trend.is_uptrend(
            ma_short=row["ma5"],
            ma_mid=row["ma25"],
            ma_long=row["ma75"],
        ):
            return 1
        if trend.is_downtrend(
            ma_short=row["ma5"],
            ma_mid=row["ma25"],
            ma_long=row["ma75"],
        ):
            return -1
        return 0

    dir_1m = trend_dir(tf_1m)
    dir_3m = trend_dir(tf_3m)
    dir_5m = trend_dir(tf_5m)

    # --- モメンタム補助 ---
    def momentum_ok(row: Dict) -> bool:
        return momentum.momentum_up(
            rsi=row["rsi"],
            rci=row["rci"],
        ) or momentum.momentum_down(
            rsi=row["rsi"],
            rci=row["rci"],
        )

    mom_1m = momentum_ok(tf_1m)
    mom_3m = momentum_ok(tf_3m)
    mom_5m = momentum_ok(tf_5m)

    # ============================================================
    # 時間足の役割
    # ------------------------------------------------------------
    # 5m : 大局（逆張り防止）
    # 3m : 中期（同方向推奨）
    # 1m : トリガ（多少のブレ許容）
    # ============================================================

    tf1_ok = res_1m["tf_ok"]
    tf3_ok = res_3m["tf_ok"]
    tf5_ok = res_5m["tf_ok"]

    # --- 方向整合 ---
    alignment_ok = (
        dir_5m != 0
        and dir_3m == dir_5m
        and dir_1m in (dir_5m, 0)
    )

    # --- モメンタム整合 ---
    momentum_align_ok = mom_3m and mom_5m

    # --- 総合 ---
    multi_tf_ok = (
        tf5_ok
        and tf3_ok
        and tf1_ok
        and alignment_ok
        and momentum_align_ok
    )

    return {
        "tf1_ok": tf1_ok,
        "tf3_ok": tf3_ok,
        "tf5_ok": tf5_ok,
        "alignment_ok": alignment_ok,
        "momentum_align_ok": momentum_align_ok,
        "multi_tf_ok": multi_tf_ok,
        "dir_1m": dir_1m,
        "dir_3m": dir_3m,
        "dir_5m": dir_5m,
    }


# ============================================================
# entry_checker 用 簡易判定
# ============================================================

def is_multi_tf_ok(
    *,
    tf_1m: Dict,
    tf_3m: Dict,
    tf_5m: Dict,
) -> bool:
    """
    True / False のみ返す簡易版
    """
    result = check_multi_tf(
        tf_1m=tf_1m,
        tf_3m=tf_3m,
        tf_5m=tf_5m,
    )
    return result["multi_tf_ok"]
