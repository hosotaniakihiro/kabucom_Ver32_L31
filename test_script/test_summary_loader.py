# ============================================================
# test_summary_loader.py（Ver23-A75-FULL CHECK）
# ------------------------------------------------------------
# ・summary_loader が 3日分を FULL LOAD できているか検証
# ・date / time / time_range / datetime の型チェック
# ・interval別の time_range 幅チェック（1/3/5min）
# ・行数の確認：0行になっていないか
# ============================================================

import datetime as dt
import pandas as pd

from trading.summary.summary_loader import load_summary_from_db


def check_date_type(df, label):
    if "date" not in df.columns:
        print(f"[NG] {label} → date 欠落")
        return False

    ok = df["date"].apply(lambda x: isinstance(x, dt.date)).all()
    print(f"[OK] date 型チェック: {label}" if ok else f"[NG] date 型不一致: {label}")
    return ok


def check_time_type(df, label):
    ok1 = ok2 = ok3 = True

    if "time" in df.columns:
        ok1 = df["time"].dropna().apply(lambda x: isinstance(x, dt.time)).all()

    if "start_time" in df.columns:
        ok2 = df["start_time"].dropna().apply(lambda x: isinstance(x, dt.time)).all()

    if "end_time" in df.columns:
        ok3 = df["end_time"].dropna().apply(lambda x: isinstance(x, dt.time)).all()

    all_ok = ok1 and ok2 and ok3
    print(f"[OK] time 型チェック: {label}" if all_ok else f"[NG] time 型不一致: {label}")
    return all_ok


def check_datetime(df, label):
    if "datetime" not in df.columns:
        print(f"[NG] datetime 欠落: {label}")
        return False

    ok = df["datetime"].dropna().apply(lambda x: x.tzinfo is None).all()
    print(f"[OK] datetime tz-naive: {label}" if ok else f"[NG] datetime tz付き: {label}")
    return ok


def check_time_range(df, interval):
    if "time_range" not in df.columns:
        print(f"[NG] time_range 欠落（{interval}min）")
        return False

    # intervalごとの幅を確認
    # "HH:MM - HH:MM"
    import re

    pattern = re.compile(r"(\d\d):(\d\d) - (\d\d):(\d\d)")

    ok = True
    for tr in df["time_range"].dropna().head(200):
        m = pattern.match(str(tr))
        if not m:
            ok = False
            break

        h1, m1, h2, m2 = map(int, m.groups())
        start = h1 * 60 + m1
        end = h2 * 60 + m2
        diff = (end - start) if end >= start else (end + 1440 - start)

        if diff != interval:
            ok = False
            break

    print(f"[OK] time_range 幅チェック（{interval}min）" if ok else f"[NG] time_range 幅不正（{interval}min）")
    return ok


def check_sorted(df, interval):
    if "id" not in df.columns:
        print(f"[NG] id 欠落（{interval}min）")
        return False

    ok = df["id"].is_monotonic_increasing
    print(f"[OK] id昇順（{interval}min）" if ok else f"[NG] idがソートされていません（{interval}min）")
    return ok


def main():

    print("===== summary_loader テスト開始 =====")

    summary_dict = load_summary_from_db()

    df1 = summary_dict.get(1, pd.DataFrame())
    df3 = summary_dict.get(3, pd.DataFrame())
    df5 = summary_dict.get(5, pd.DataFrame())

    print(f"1min rows: {len(df1)}")
    print(f"3min rows: {len(df3)}")
    print(f( "5min rows: {len(df5)}") if df5 is not None else "5min empty")

    # --------------------------------------------
    # 1) 行数チェック（空なら NG）
    # --------------------------------------------
    for df, label in [(df1, "1min"), (df3, "3min"), (df5, "5min")]:
        if df.empty:
            print(f"[NG] {label} ロード0件")
        else:
            print(f"[OK] {label} ロード成功")

    # --------------------------------------------
    # 2) 型チェック
    # --------------------------------------------
    check_date_type(df1, "1min")
    check_date_type(df3, "3min")
    check_date_type(df5, "5min")

    check_time_type(df1, "1min")
    check_time_type(df3, "3min")
    check_time_type(df5, "5min")

    check_datetime(df1, "1min")
    check_datetime(df3, "3min")
    check_datetime(df5, "5min")

    # --------------------------------------------
    # 3) time_range の interval幅チェック
    # --------------------------------------------
    check_time_range(df1, 1)
    check_time_range(df3, 3)
    check_time_range(df5, 5)

    # --------------------------------------------
    # 4) id ソート状態
    # --------------------------------------------
    check_sorted(df1, 1)
    check_sorted(df3, 3)
    check_sorted(df5, 5)

    print("===== summary_loader テスト完了 =====")


if __name__ == "__main__":
    main()
