"""第17ラウンド dry-run テスト (場後).

実注文なし。NAS 本番 DB は読み取りのみ。

対象:
  追加P0-1  Tonosama fail_open_empty の owner 一本化 (env 上書きに勝つ)
  追加P0-2  ranking bootstrap 読み込み量の有界化
  追加P1-3  大引け後に realtime worker を起動しない
  P0-3      settings owner 一本化 (閾値 seed を止める)
  P0-4      stale_fail_open 全 call-site fail-close
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import os
import sqlite3
import sys
import tempfile
import traceback

ROOT = os.environ.get(
    "KABU_SRC_ROOT",
    r"W:\BUSINESS\事業\04_株式自動売買システム\ソースコード",
)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["AUTOSTOCK_DRY_RUN"] = "1"
os.environ["ALLOW_ORDERS"] = "0"

RESULTS: list = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))


def section(t: str) -> None:
    print()
    print("=" * 74)
    print(t)
    print("=" * 74)


# ============================================================
# 追加P0-1  Tonosama fail_open owner
# ============================================================

def test_tonosama_owner_wins() -> None:
    section("追加P0-1  fail_open_empty の owner が env 上書きに勝つ")
    src = io.open(os.path.join(ROOT, "trading", "entry", "tonosama",
                               "fresh_summary_wait.py"), encoding="utf-8").read()
    check("installer は setdefault ではなく代入で 0 を入れる",
          'os.environ["TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY"] = "0"' in src)
    check("外部指定を許すには専用 env が要る",
          "TONOSAMA_ALLOW_EXTERNAL_FAIL_OPEN" in src)
    check("上書きしたことをログに残す", "fail_open_empty を上書きした" in src)

    tasks = io.open(os.path.join(ROOT, "trading", "entry_exit", "tasks.py"),
                    encoding="utf-8").read()
    check("2 つ目の実装 (entry_exit/tasks.py) も既定 False",
          'TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", False' in tasks)
    check("tasks.py に True 既定が残っていない",
          'TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_EMPTY", True' not in tasks)


# ============================================================
# 追加P0-2  bootstrap 有界化
# ============================================================

def test_bootstrap_bounded() -> None:
    section("追加P0-2  ranking bootstrap の読み込み量が有界")
    import pandas as pd
    import trading.ranking.summary.bootstrap_loader as bl

    check("lookback 既定は 180 分", bl._bootstrap_lookback_min() == 180,
          str(bl._bootstrap_lookback_min()))
    check("max_rows 既定は 80,000", bl._bootstrap_max_rows() == 80000,
          str(bl._bootstrap_max_rows()))
    os.environ["RANKING_BOOTSTRAP_LOOKBACK_MIN"] = "0"
    os.environ["RANKING_BOOTSTRAP_MAX_ROWS"] = "0"
    try:
        check("env=0 で無制限 (従来動作) へ戻せる",
              bl._bootstrap_lookback_min() == 0 and bl._bootstrap_max_rows() == 0)
    finally:
        os.environ.pop("RANKING_BOOTSTRAP_LOOKBACK_MIN", None)
        os.environ.pop("RANKING_BOOTSTRAP_MAX_ROWS", None)

    # 実データで有界化されるか (一時 DB)
    tmp = tempfile.mkdtemp(prefix="bootstrap_")
    db = os.path.join(tmp, "ranking.db")
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE ranking_snapshot_1min (symbol TEXT, datetime TEXT, close REAL)")
    now = dt.datetime.now().replace(second=0, microsecond=0)
    rows = []
    # 1 日分 (600 分) x 300 銘柄 = 180,000 行
    for m in range(600):
        ts = (now - dt.timedelta(minutes=m)).strftime("%Y-%m-%d %H:%M:%S")
        for s in range(300):
            rows.append((str(1000 + s), ts, 100.0))
    c.executemany("INSERT INTO ranking_snapshot_1min VALUES (?,?,?)", rows)
    c.commit()
    total = c.execute("SELECT COUNT(*) FROM ranking_snapshot_1min").fetchone()[0]
    c.close()

    df = bl.load_ranking_snapshot_fallback(db)
    check(f"DB 全体 {total:,} 行 -> 読み込み {len(df):,} 行 (有界)",
          len(df) < total and len(df) <= 80000, f"{total} -> {len(df)}")
    if not df.empty:
        oldest = pd.to_datetime(df["datetime"]).min()
        age_min = (pd.Timestamp(now) - oldest).total_seconds() / 60.0
        check(f"lookback 180 分以内に収まる (最古 {age_min:.0f} 分前)",
              age_min <= 200, f"{age_min:.0f}min")
        check("昇順に整列されている",
              df["datetime"].is_monotonic_increasing, "")

    # 無制限に戻すと全部読む
    os.environ["RANKING_BOOTSTRAP_LOOKBACK_MIN"] = "0"
    os.environ["RANKING_BOOTSTRAP_MAX_ROWS"] = "0"
    try:
        df2 = bl.load_ranking_snapshot_fallback(db)
        check(f"env=0 なら従来どおり全件 ({len(df2):,} 行)", len(df2) == total,
              f"{len(df2)} vs {total}")
    finally:
        os.environ.pop("RANKING_BOOTSTRAP_LOOKBACK_MIN", None)
        os.environ.pop("RANKING_BOOTSTRAP_MAX_ROWS", None)


# ============================================================
# 追加P1-3  大引け後の停止
# ============================================================

def test_eod_stop() -> None:
    section("追加P1-3  大引け後に realtime tick を起動しない")
    import scheduler_jobs.summary.scheduler as sch

    sch.reset_eod_state_for_test()
    today = dt.date.today()

    # 場中は止めない
    sch.reset_eod_state_for_test()
    mid = dt.datetime.combine(today, dt.time(10, 30))
    check("場中 (10:30) は停止しない", sch._summary_stop_after_market_close(mid) is False)

    # 昼休みも止めない (11:30-12:30 は取引時間内扱いの継続)
    sch.reset_eod_state_for_test()
    lunch = dt.datetime.combine(today, dt.time(12, 0))
    check("昼休み (12:00) は停止しない", sch._summary_stop_after_market_close(lunch) is False)

    # 大引け直後の 1 回だけ EOD として通す
    sch.reset_eod_state_for_test()
    just_closed = dt.datetime.combine(today, dt.time(15, 30, 30))
    first = sch._summary_stop_after_market_close(just_closed)
    second = sch._summary_stop_after_market_close(just_closed)
    third = sch._summary_stop_after_market_close(just_closed)
    check("大引け直後の 1 回目は EOD final として実行する", first is False, str(first))
    check("2 回目以降は停止する", second is True and third is True,
          f"2nd={second} 3rd={third}")

    # 猶予を過ぎていれば EOD も走らせない
    sch.reset_eod_state_for_test()
    late = dt.datetime.combine(today, dt.time(15, 45))
    check("猶予超過 (15:45) は EOD も実行しない",
          sch._summary_stop_after_market_close(late) is True)

    # env で従来動作へ
    sch.reset_eod_state_for_test()
    os.environ["SUMMARY_STOP_AFTER_MARKET_CLOSE"] = "0"
    try:
        check("SUMMARY_STOP_AFTER_MARKET_CLOSE=0 で従来動作",
              sch._summary_stop_after_market_close(late) is False)
    finally:
        os.environ.pop("SUMMARY_STOP_AFTER_MARKET_CLOSE", None)
        sch.reset_eod_state_for_test()

    src = io.open(os.path.join(ROOT, "scheduler_jobs", "summary", "scheduler.py"),
                  encoding="utf-8").read()
    check("parent tick の先頭で判定している",
          "if _summary_stop_after_market_close(now):" in src)


# ============================================================
# P0-3  settings owner
# ============================================================

def test_settings_owner() -> None:
    section("P0-3  閾値 env の owner を strict 1 か所にする")
    src = io.open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    check("main.py は既定で閾値 env を設定しない",
          "閾値 env は" in src and "SUMMARY_AI_THRESHOLD_LEGACY_SEED" in src)
    check("skip したことをログに残す",
          "[SUMMARY AI THRESHOLD ENV PATCH] skipped" in src)

    strict = io.open(os.path.join(ROOT, "core", "startup", "strict_entry_defaults.py"),
                     encoding="utf-8").read()
    for k in ("SUMMARY_AI_MIN_BUY", "SUMMARY_AI_MIN_CONF", "MIN_COMPOSITE_SCORE_BUY"):
        check(f"strict が {k} を持っている", k in strict, "")


# ============================================================
# P0-4  stale_fail_open
# ============================================================

def test_stale_fail_close() -> None:
    section("P0-4  stale_fail_open は全 call-site で fail-close")
    ecu = io.open(os.path.join(ROOT, "trading", "entry", "entry_count_unblock.py"),
                  encoding="utf-8").read()
    check("entry_count_unblock の既定が False",
          'TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE", False' in ecu)
    check("entry_count_unblock に True 既定が残っていない",
          'TONOSAMA_WAIT_PUSH_SUMMARY_FAIL_OPEN_IF_STALE", True' not in ecu)

    efr = io.open(os.path.join(ROOT, "trading", "ranking", "entry_from_ranking.py"),
                  encoding="utf-8").read()
    check("ranking DB failopen の seed が 0",
          'RANKING_ENTRY_SOURCE_DB_FAILOPEN_STALE", "0"' in efr)
    check("ranking global failopen の seed が 0",
          'RANKING_ENTRY_SOURCE_FAILOPEN_STALE_GLOBAL", "0"' in efr)
    check("ranking failopen に True 既定が残っていない",
          'RANKING_ENTRY_SOURCE_DB_FAILOPEN_STALE", True' not in efr
          and 'RANKING_ENTRY_SOURCE_FAILOPEN_STALE_GLOBAL", True' not in efr)

    strict = io.open(os.path.join(ROOT, "core", "startup", "strict_entry_defaults.py"),
                     encoding="utf-8").read()
    check("strict も 0 を宣言している",
          '"RANKING_ENTRY_SOURCE_DB_FAILOPEN_STALE": "0"' in strict
          and '"RANKING_ENTRY_SOURCE_FAILOPEN_STALE_GLOBAL": "0"' in strict)


def main() -> int:
    tests = [
        test_tonosama_owner_wins,
        test_bootstrap_bounded,
        test_eod_stop,
        test_settings_owner,
        test_stale_fail_close,
    ]
    for fn in tests:
        try:
            fn()
        except Exception:
            print(f"[FAIL] {fn.__name__} が例外で落ちた")
            traceback.print_exc()
            RESULTS.append((fn.__name__, False, "exception"))

    section("SUMMARY")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"total={len(RESULTS)} passed={passed} failed={len(failed)}")
    for n in failed:
        print("  FAILED:", n)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
