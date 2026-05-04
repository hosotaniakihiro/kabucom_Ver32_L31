# ============================================================
# tools/debug_exit.py
# EXIT デバッグ専用 — Ver24-PRO-AI-ULTRA 完全対応版
# ============================================================

import datetime as dt
from global_state import global_data
from database import Session_position
from database.models import Position
from trading.handlers.exit_handler import (
    build_5s_bar_fast,
    check_exit_condition,
)


def debug_exit():
    print("\n====================== EXIT DEBUG ======================")

    # ------------------------------------------------------
    # 1. PUSHデータ
    # ------------------------------------------------------
    df_push = global_data.get_push_df()
    print(f"\n📌 push_df rows = {len(df_push)}")
    if df_push is not None and not df_push.empty:
        print(df_push.tail(5))
    else:
        print("⚠ push_df が空です → 5秒足は生成されません")

    # ------------------------------------------------------
    # 2. Position DB の OPEN 建玉
    # ------------------------------------------------------
    sp = Session_position()
    positions = sp.query(Position).filter_by(status="OPEN").all()

    print(f"\n📌 OPEN positions = {len(positions)}")
    if not positions:
        print("⚠ 建玉なし → EXITなし\n")
        sp.close()
        return

    # ------------------------------------------------------
    # 3. 各銘柄の EXIT状況を個別チェック
    # ------------------------------------------------------
    for pos in positions:
        sym = pos.symbol
        print("\n------------------------------------------------------")
        print(f"🟦 Symbol: {sym}")

        # 最新5秒足バー
        bar = build_5s_bar_fast(sym)
        if not bar:
            print("⚠ 5秒足が生成できません（push_df不足 or symbolデータなし）")
            continue

        print(f" 5秒足バー: {bar}")

        price = bar["close"]
        print(f" 現在値(close): {price}")

        # 高値/安値更新の確認
        high = pos.high_since_entry
        low = pos.low_since_entry
        print(f" high_since_entry={high}, low_since_entry={low}")

        # EXIT条件チェック
        reason = check_exit_condition(pos, price, bar)
        print(f" EXIT条件: {reason}")

        # EXIT条件あり
        if reason:
            print(f" ➤ EXIT発火する理由 = {reason}")
        else:
            print(" ➤ EXIT条件未達 → 発火しません")

    sp.close()
    print("\n====================== EXIT DEBUG END ======================\n")
