# ============================================================
# test_script/test_force_exit.py
# Ver26-FINAL-EXECUTOR-COMPAT
# ============================================================

from trading.handlers.exit_handler import build_5s_bar_fast
from trading.exit.executor import execute_exit
from global_state import global_data


def run_force_exit_test():

    print("\n=== 🔥 強制 EXIT テスト START ===")

    print("open_positions =", global_data.open_positions)

    if not global_data.open_positions:
        print("❌ open_positions が空 → EXIT不可")
        print("=== 強制 EXIT テスト END ===")
        return

    for sym in list(global_data.open_positions.keys()):

        print(f"\n🔥 EXIT試行: {sym}")

        # --- 5秒足取得 ---
        bar = build_5s_bar_fast(sym)
        print(" 5s bar =", bar)

        if not bar or "close" not in bar:
            print(" ❌ 5秒足が取得できないため EXIT不可")
            continue

        exit_price = bar["close"]

        # --- EXIT実行（Ver26 executor 形式） ---
        try:
            result = execute_exit(
                symbol=sym,
                reason="FORCE_TEST",
                exit_price=exit_price,
            )

            if result:
                print(" ✅ EXIT成功")
            else:
                print(" ❌ EXIT未実行（ポジション無し or CLOSING）")

        except Exception as e:
            print(" ❌ EXITエラー:", e)

    print("\n=== 🔥 強制 EXIT テスト END ===")