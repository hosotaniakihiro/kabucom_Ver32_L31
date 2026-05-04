# ============================================================
# scoring/config_sync.py
# ------------------------------------------------------------
# ・score_config.ini と code 側 signal_key の同期チェック
# ・ini を唯一の定義源とする
# ・[trade] セクションは同期対象外
# ・ini 未実装（将来候補）は OK
# ・ini 欠損（code 側が参照）は ERROR
# ・単体実行対応（python config_sync.py）
# ============================================================

from configparser import ConfigParser
from pathlib import Path
import sys
from typing import Set, Dict


# ============================================================
# 🔧 ini 側キー取得（trade セクション除外）
# ============================================================
def load_ini_keys(ini_path: str | Path = "score_config.ini") -> Set[str]:
    ini_path = Path(ini_path)

    if not ini_path.exists():
        raise FileNotFoundError(
            f"score_config.ini not found: {ini_path.resolve()}"
        )

    conf = ConfigParser()
    conf.read(ini_path, encoding="utf-8")

    keys: Set[str] = set()

    for sec in conf.sections():
        # trade はスコア同期対象外
        if sec.lower() == "trade":
            continue

        for k in conf[sec]:
            keys.add(k)

    return keys


# ============================================================
# 🔧 code 側 signal_key 一覧
# ※ code は ini に従属する（ini が正）
# ============================================================
def collect_code_keys() -> Set[str]:
    """
    code 側で実装済みの signal_key を列挙
    ※ ENTRY / BONUS / dispatcher すべて対象
    """
    return {
        # =========================
        # BUY ENTRY
        # =========================
        "ma5_ma25_cross",
        "macd_gc",
        "perfect_order_event",
        "first_pullback",
        "breakout_high",
        "breakout_3",
        "fib_rebound",
        "rebound_on_ma25",

        # =========================
        # BUY BONUS
        # =========================
        "perfect_order",
        "ma5_above_ma25",
        "rsi_trend_strong",
        "higher_high",
        "lower_wick_low_zone",
        "vwap_break",
        "vwap_breakout",
        "bollinger_rebound",
        "bb_3sigma_rebound",
        "volume_high_keep",
        "volume_spike",
        "volume_surge",
        "volume_price_breakout",
        "tick_surge",

        # =========================
        # SELL ENTRY
        # =========================
        "ma_dead_cross",
        "rsi_overheat_fail",
        "macd_dead_cross",
        "double_top",
        "upper_wick_series",
        "bearish_engulfing",
        "bearish_engulfing2",
        "evening_star",
        "big_red",

        # =========================
        # SELL BONUS
        # =========================
        "ma5_below_ma25",
        "ma_downtrend",
        "rsi_trend_weak",
        "lower_low",
        "vwap_fail",
        "red_series",
        "bb_3sigma_breakdown",
        "bollinger_breakdown",
        "volume_drop",
        "volume_peak_out",
        "volume_price_breakdown",
        "gapdown_red",
        "upper_wick_long",
    }


# ============================================================
# 🔥 同期チェック本体
# ============================================================
def check_sync(ini_path: str | Path = "score_config.ini") -> Dict[str, list]:
    ini_keys = load_ini_keys(ini_path)
    code_keys = collect_code_keys()

    # ini にあるが code 未実装 → OK（将来用）
    unused_in_code = ini_keys - code_keys

    # code にあるが ini に無い → ❌ バグ
    missing_in_ini = code_keys - ini_keys

    return {
        "unused_in_code": sorted(unused_in_code),
        "missing_in_ini": sorted(missing_in_ini),
    }


# ============================================================
# 🖥 CLI 実行部
# ============================================================
def main():
    try:
        ini_path = sys.argv[1] if len(sys.argv) > 1 else "score_config.ini"

        result = check_sync(ini_path)

        print("\n================ CONFIG SYNC CHECK ================\n")

        # --------------------------------------------
        # ini にあるが code 未実装（将来候補）
        # --------------------------------------------
        if result["unused_in_code"]:
            print("🟡 ini に定義されているが code 未実装（将来候補）:")
            for k in result["unused_in_code"]:
                print(f"  - {k}")
        else:
            print("✅ ini 未実装キー: なし")

        print()

        # --------------------------------------------
        # code にあるが ini に存在しない（致命的）
        # --------------------------------------------
        if result["missing_in_ini"]:
            print("🔴 code で使用されているが ini に存在しないキー:")
            for k in result["missing_in_ini"]:
                print(f"  - {k}")
        else:
            print("✅ ini 欠損キー: なし")

        print("\n===================================================\n")

        # ❌ ini 欠損のみエラー（CI / 本番用）
        if result["missing_in_ini"]:
            sys.exit(1)

        sys.exit(0)

    except Exception as e:
        print(f"❌ config_sync error: {e}")
        sys.exit(2)


# ============================================================
# エントリーポイント
# ============================================================
if __name__ == "__main__":
    main()
