"""未定義名 (NameError 予備軍) の静的検出。

背景:
  core/startup/patches の本文化では「クロージャの中身をモジュール関数へ持ち上げる」
  作業を大量に行った。このとき

    * def 行だけ改名して呼び出し側が旧名のまま残る
    * 外側スコープにあった import / 定数を一緒に持ってこない
    * 旧 patch の target 引数を消したのに参照だけ残る

  という取りこぼしが起き、実運用で NameError になった (2026-08-17 の
  rotation_stability / ranking_db_writer の停止)。import が通っても
  「その関数を呼ぶまで」露見しないので、静的に潰す必要がある。

このテストは対象モジュールを AST で走査し、どのスコープからも束縛されていない
Load 参照を検出する。lambda 内は解析しないなど取りこぼしはあるので、
「これが通れば安全」ではなく「既知の再発を止める」ための下限チェックとして使う。
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]

# 現時点で未定義参照ゼロを維持したいモジュール。
# 障害の当事者と、同じ持ち上げ作業をした周辺を入れてある。
GUARDED = [
    "trading/push/push_stream/rotation_stability.py",
    "trading/push/push_stream/rotation_symbols.py",
    "trading/ranking/ranking_db_writer.py",
    "trading/ranking/db_handler.py",
    "trading/summary/engine/push_summary_engine.py",
    "trading/summary/push_raw_1m_source.py",
    "trading/summary/persistence/summary_save_guards.py",
    "trading/summary/summary_ai_pipeline.py",
    "trading/summary/summary_entry.py",
    "scripts/night_yahoo_daily_update_batch.py",
]

_BUILTIN = set(dir(builtins)) | {
    "__name__", "__file__", "__doc__", "__package__",
    "__spec__", "__loader__", "__builtins__", "__debug__",
}


def _bindings_of(node: ast.AST) -> set[str]:
    """このスコープ自身が束縛する名前 (入れ子スコープの中身は数えない)。"""
    out: set[str] = set()

    def walk(n: ast.AST) -> None:
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(child.name)
                continue
            if isinstance(child, ast.Lambda):
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                out.add(child.id)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    out.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(child, ast.ExceptHandler) and child.name:
                out.add(child.name)
            elif isinstance(child, (ast.Global, ast.Nonlocal)):
                out.update(child.names)
            walk(child)

    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = node.args
        for arg in list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs):
            out.add(arg.arg)
        if a.vararg:
            out.add(a.vararg.arg)
        if a.kwarg:
            out.add(a.kwarg.arg)
    walk(node)
    return out


def _loads_of(node: ast.AST) -> list[tuple[int, str]]:
    """このスコープ内で Load される Name (入れ子スコープの中身は数えない)。"""
    out: list[tuple[int, str]] = []

    def walk(n: ast.AST) -> None:
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                out.append((child.lineno, child.id))
            walk(child)

    walk(node)
    return out


def undefined_names(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), str(path))
    found: set[tuple[int, str, str]] = set()

    def descend(node: ast.AST, scope: set[str], where: str) -> None:
        """スコープを作らない子を辿り、入れ子の def/class だけ新スコープで訪問する。

        try/if の中に定義された関数を素通りさせると、その関数から見た
        「外側のローカル変数」を未定義と誤検出するので、構造どおりに降りる。
        """
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, scope, child.name)
            else:
                descend(child, scope, where)

    def visit(node: ast.AST, enclosing: set[str], where: str) -> None:
        scope = _bindings_of(node) | enclosing
        for lineno, name in _loads_of(node):
            if name not in scope and name not in _BUILTIN:
                found.add((lineno, where, name))
        descend(node, scope, where)

    visit(tree, set(), "<module>")
    return ["%s:%d %s() -> %s" % (path.name, lineno, where, name) for lineno, where, name in sorted(found)]


@pytest.mark.parametrize("rel", GUARDED)
def test_module_has_no_undefined_names(rel: str) -> None:
    path = _ROOT / rel
    if not path.exists():
        pytest.skip("module not present: %s" % rel)
    problems = undefined_names(path)
    assert not problems, "未定義名の参照:\n  " + "\n  ".join(problems)
