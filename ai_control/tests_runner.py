# ============================================================
# File   : ai_control/tests_runner.py
# Purpose:
#   - 修正後の worktree に対して、安全に実行できる検証だけを走らせる
# ------------------------------------------------------------
# このプロジェクトには pytest.ini も tests/ も無い。そこで:
#   LEVEL 1  compileall     … 構文チェック。コードを実行しないので完全に安全
#   LEVEL 2  import smoke   … 対象モジュールを import できるか
#
# import smoke は「コードを実行する」ので、無条件にはやらない:
#   - 起動系 (main.py / main_database.py / run_*.py) は denylist で除外
#   - 発注系 (sell.py / cancel_order.py / SEND_exit_order.py) も除外
#   - test_script/ / batch/ / scripts/ / TaskSchedule/ も除外
#   - 1 モジュールずつ別プロセス + タイムアウトで実行する
#   - cwd は worktree。本番ツリーのコードは読まれない
#
# 本番 API への発注、DB の破壊的操作は行わない。
# ============================================================

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import List, Optional

# import してはいけないもの（起動・発注・バッチ・テスト）
IMPORT_DENY = [
    "main.py",
    "main_database.py",
    "run_*.py",
    "daily_retrain.py",
    "sell.py",
    "cancel_order.py",
    "SEND_exit_order.py",
    "force_cancel_loop.py",
    "kabus_position_sync.py",
    "monitor_orders.py",
    "register_cycle.py",
    "scripts/*",
    "batch/*",
    "tools/*",
    "TaskSchedule/*",
    "test_script/*",
    "backtest/*",
    "YAHOO_DATA_download/*",
    "ai_control/*",
    "conftest.py",
    "sitecustomize.py",
]


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class TestReport:
    ok: bool = True
    results: List[CheckResult] = field(default_factory=list)

    def add(self, r: CheckResult) -> None:
        self.results.append(r)
        if not r.ok:
            self.ok = False

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checks": [{"name": r.name, "ok": r.ok, "detail": r.detail[:2000]} for r in self.results],
        }

    def summary(self) -> str:
        passed = sum(1 for r in self.results if r.ok)
        return "%d/%d passed" % (passed, len(self.results))


def _norm(p: str) -> str:
    return str(p).replace("\\", "/").lstrip("./")


def is_import_safe(rel_path: str) -> bool:
    p = _norm(rel_path)
    if not p.endswith(".py"):
        return False
    base = p.rsplit("/", 1)[-1]
    for pat in IMPORT_DENY:
        if fnmatch(p, pat) or fnmatch(base, pat):
            return False
    return True


def module_name(rel_path: str) -> Optional[str]:
    p = _norm(rel_path)
    if not p.endswith(".py"):
        return None
    p = p[:-3]
    if p.endswith("/__init__"):
        p = p[: -len("/__init__")]
    parts = [x for x in p.split("/") if x]
    if not parts or any(not x.isidentifier() for x in parts):
        return None
    return ".".join(parts)


class TestsRunner:
    def __init__(
        self,
        python_exe: Path,
        *,
        compile_timeout_sec: float = 300.0,
        import_timeout_sec: float = 90.0,
        import_smoke: bool = True,
        max_import_modules: int = 12,
    ) -> None:
        self.python_exe = Path(python_exe)
        self.compile_timeout_sec = float(compile_timeout_sec)
        self.import_timeout_sec = float(import_timeout_sec)
        self.import_smoke = bool(import_smoke)
        self.max_import_modules = int(max_import_modules)

    def _env(self) -> dict:
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["AI_CONTROL_IMPORT_SMOKE"] = "1"
        return env

    def run(self, worktree: Path, changed_files: List[str]) -> TestReport:
        report = TestReport()
        py_files = [f for f in changed_files if _norm(f).endswith(".py")]

        if not py_files:
            report.add(CheckResult("no_python_changes", True, "Python ファイルの変更なし"))
            return report

        report.add(self._compileall(worktree, py_files))

        if not self.import_smoke:
            return report
        if not report.ok:
            report.add(CheckResult("import_smoke", True, "compileall 失敗のため import は行わない"))
            return report

        targets = [f for f in py_files if is_import_safe(f)][: self.max_import_modules]
        skipped = [f for f in py_files if not is_import_safe(f)]
        if skipped:
            report.add(
                CheckResult(
                    "import_skipped",
                    True,
                    "安全のため import しない: " + ", ".join(_norm(f) for f in skipped),
                )
            )
        for f in targets:
            mod = module_name(f)
            if mod is None:
                continue
            report.add(self._import_one(worktree, mod))
        return report

    def _compileall(self, worktree: Path, py_files: List[str]) -> CheckResult:
        args = [str(self.python_exe), "-m", "compileall", "-q"]
        args += [str(worktree / _norm(f)) for f in py_files]
        try:
            proc = subprocess.run(
                args,
                cwd=str(worktree),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.compile_timeout_sec,
                env=self._env(),
            )
        except subprocess.TimeoutExpired:
            return CheckResult("compileall", False, "timeout")
        out = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        return CheckResult("compileall", proc.returncode == 0, out.strip()[:2000] or "OK")

    def _import_one(self, worktree: Path, mod: str) -> CheckResult:
        code = "import importlib, sys; importlib.import_module(%r); print('IMPORT OK')" % mod
        try:
            proc = subprocess.run(
                [str(self.python_exe), "-c", code],
                cwd=str(worktree),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.import_timeout_sec,
                env=self._env(),
            )
        except subprocess.TimeoutExpired:
            return CheckResult("import:" + mod, False, "timeout (%.0fs)" % self.import_timeout_sec)
        out = proc.stdout.decode("utf-8", errors="replace") if proc.stdout else ""
        ok = proc.returncode == 0 and "IMPORT OK" in out
        return CheckResult("import:" + mod, ok, out.strip()[-2000:] or "")
