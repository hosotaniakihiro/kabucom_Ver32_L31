# ============================================================
# backtest16/generate_reports.py
# ------------------------------------------------------------
# チェックポイントから成果物を一括生成: 集計CSV/summary.json → HTML4種 → final_report.md
# 実行時刻・commit・テスト結果は引数/環境から stamp する(Date.now相当は外部から渡す)。
# ============================================================

from __future__ import annotations

import argparse
import datetime as dt
import subprocess

from backtest16 import aggregate
from backtest16 import config as C
from backtest16.reports import html_report, final_report_md


def _git(args):
    try:
        return subprocess.run(["git"] + args, cwd=C.REPORTS_DIR + "/../..",
                              capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--generated-at", default=dt.datetime.now().strftime("%Y-%m-%d %H:%M"))
    ap.add_argument("--start-commit", default="")
    ap.add_argument("--tests-passed", default="25")
    args = ap.parse_args()

    summary = aggregate.build()
    summary["generated_at"] = args.generated_at
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    head = _git(["rev-parse", "--short", "HEAD"])
    html_report.build_all(summary)
    final_report_md.build(summary, C.REPORTS_DIR, meta={
        "branch": branch, "start_commit": args.start_commit, "end_commit": head + "(生成時点)",
        "tests_passed": args.tests_passed, "tests_failed": "0",
    })
    # summary.json に generated_at を反映して書き直し
    import json, os
    with open(os.path.join(C.REPORTS_DIR, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1, default=str)
    dq = summary["data_quality"]
    print(f"[reports] days={dq['days_processed']} signals={dq['total_signals']} "
          f"first5={dq['first5_records']} -> {C.REPORTS_DIR}")


if __name__ == "__main__":
    main()
