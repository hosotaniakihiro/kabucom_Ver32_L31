# -*- coding: utf-8 -*-
"""
第18ラウンド: UNC ハードコードの nas_root() 統一 (実注文なし)

検証の要点は 2 つ。
  1. 置換したモジュールが全部 import できること
     (import 時に評価される定数なので、ここが崩れると起動しない)
  2. 置換後の値が **旧リテラルと 1 文字も違わない** こと
     (UNC が到達可能な今は nas_root() が UNC を返すので同値でなければならない)

2 は、変更前のソースミラーから旧リテラルを取り出して突き合わせる。
ミラーが無い環境では該当チェックを skip する。
"""
from __future__ import annotations

import ast
import importlib
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

MIRROR = os.environ.get(
    "R18_SRC_MIRROR",
    r"C:\Users\hosot\AppData\Local\Temp\claude\C--Users-hosot"
    r"\2e83dc85-0214-4b83-b96a-7b25eb64e30e\scratchpad\src_mirror",
)

MARKER = "192.168.0.22"
ENCS = ("utf-8", "cp932", "utf-8-sig")

PASS = 0
FAIL = 0
SKIP = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


def read_any(p):
    try:
        raw = io.open(p, "rb").read()
    except OSError:
        return None
    for e in ENCS:
        try:
            return raw.decode(e)
        except UnicodeDecodeError:
            pass
    return None


def module_name(path):
    rel = os.path.relpath(path, ROOT)
    if rel.endswith("__init__.py"):
        rel = os.path.dirname(rel)
    else:
        rel = rel[:-3]
    return rel.replace(os.sep, ".")


# ============================================================
print("[1] nas_root / nas_path の基本")
# ============================================================
import nas_root as nr

check("nas_root() は空でない", bool(nr.nas_root()))
check("nas_root() は末尾に区切りを付けない", not nr.nas_root().endswith(("\\", "/")))
check("nas_path() が連結する",
      nr.nas_path("a", "b") == nr.nas_root() + "\\a\\b",
      nr.nas_path("a", "b"))
check("nas_path() は前後の区切りを吸収する",
      nr.nas_path("\\a\\", "\\b") == nr.nas_root() + "\\a\\b")
check("nas_path() 引数なしは root", nr.nas_path() == nr.nas_root())

nr.reset_cache_for_test()
os.environ["AUTOSTOCK_NAS_ROOT"] = "Q:"
check("AUTOSTOCK_NAS_ROOT が最優先", nr.nas_root() == "Q:")
os.environ["AUTOSTOCK_NAS_ROOT"] = "Q:\\"
nr.reset_cache_for_test()
check("末尾の区切りは落とす", nr.nas_root() == "Q:")
del os.environ["AUTOSTOCK_NAS_ROOT"]
nr.reset_cache_for_test()

nr.reset_cache_for_test()
os.environ["AUTOSTOCK_NAS_ROOT"] = "R:"
first = nr.nas_root()
os.environ["AUTOSTOCK_NAS_ROOT"] = "S:"
check("プロセス内で 1 回だけ判定する (キャッシュ)", nr.nas_root() == first)
del os.environ["AUTOSTOCK_NAS_ROOT"]
nr.reset_cache_for_test()

live_root = nr.nas_root()
print(f"    実行時の root = {live_root}")
check("実行時の root は到達可能", os.path.exists(live_root + "\\"), live_root)

# ============================================================
print("[2] 置換したモジュールが import できる / 値が変わっていない")
# ============================================================
targets = []
for dp, dn, fns in os.walk(ROOT):
    if any(x in dp for x in ("test_script", "__pycache__", ".venv", "scratchpad",
                             "backtest_replay", ".git")):
        continue
    for fn in fns:
        if not fn.endswith(".py") or fn.startswith("._"):
            continue
        p = os.path.join(dp, fn)
        src = read_any(p)
        if src and "from nas_root import" in src:
            targets.append(p)

print(f"    置換対象 {len(targets)} ファイル")
check("置換対象が 50 ファイル以上ある", len(targets) >= 50, str(len(targets)))

# import しただけで **本番 DB へ書き込む** モジュールがあると、テストが
# 本番データを壊す。trading/summary/summary_seed_bootstrap.py は R18 まで
# module-level スクリプトで、import すると前日 DB の末尾 120 bars を
# 当日 DB へ to_sql(append) していた (実際に summary20260828.db へ 120 行
# 書き込む事故を起こし、削除して復旧した)。
# R18 で `if __name__ == "__main__"` ガードを付けたので import して良いが、
# ガードが外れたら再発するので **import する前に AST で確認**する。
GUARD_REQUIRED = {"trading.summary.summary_seed_bootstrap"}


def has_main_guard(path) -> bool:
    """top-level に副作用が無く、`if __name__ == "__main__"` がある。"""
    try:
        tree = ast.parse(read_any(path))
    except Exception:
        return False
    has_guard = False
    for n in tree.body:
        if isinstance(n, ast.If):
            t = n.test
            if (isinstance(t, ast.Compare) and isinstance(t.left, ast.Name)
                    and t.left.id == "__name__"):
                has_guard = True
                continue
        if isinstance(n, (ast.For, ast.While)):
            return False
    return has_guard


imported = {}
import_failed = []
guard_missing = []
for p in targets:
    name = module_name(p)
    if name in GUARD_REQUIRED and not has_main_guard(p):
        # ガードが無い = import すると本番 DB を書く。絶対に import しない。
        guard_missing.append(name)
        continue
    try:
        imported[p] = importlib.import_module(name)
    except Exception as e:
        import_failed.append((name, f"{type(e).__name__}: {e}"))

check("import すると本番 DB を書くモジュールに main ガードがある",
      not guard_missing, str(guard_missing))
for name in guard_missing:
    print(f"       main ガードが無い: {name} (import を回避した)")

check("全モジュールが import / compile できる", not import_failed, str(import_failed[:5]))
for name, err in import_failed:
    print(f"       import 失敗: {name} : {err}")

compared = 0
mismatched = []
if os.path.isdir(MIRROR):
    for p in targets:
        mod = imported.get(p)
        if mod is None:
            continue
        old_path = os.path.join(MIRROR, os.path.relpath(p, ROOT))
        old_src = read_any(old_path)
        if not old_src or MARKER not in old_src:
            continue
        try:
            old_tree = ast.parse(old_src)
        except SyntaxError:
            continue
        for node in old_tree.body:
            if not isinstance(node, ast.Assign) or len(node.targets) != 1:
                continue
            tgt = node.targets[0]
            if not isinstance(tgt, ast.Name):
                continue
            try:
                old_val = ast.literal_eval(node.value)
            except Exception:
                continue
            if not isinstance(old_val, str) or MARKER not in old_val:
                continue
            if not hasattr(mod, tgt.id):
                continue
            new_val = str(getattr(mod, tgt.id))
            compared += 1
            if new_val != old_val:
                mismatched.append((module_name(p), tgt.id, old_val, new_val))
else:
    SKIP += 1
    print(f"    (ミラーが無いので旧値との突き合わせを skip: {MIRROR})")

print(f"    旧リテラルと突き合わせた定数 {compared} 件")
check("旧値と 1 文字も変わっていない", not mismatched, str(mismatched[:3]))
for name, attr, o, n in mismatched:
    print(f"       不一致 {name}.{attr}\n         旧={o}\n         新={n}")
if os.path.isdir(MIRROR):
    check("突き合わせが 10 件以上できている", compared >= 10, str(compared))

# ============================================================
print("[3] 代表的な NAS パスが実在する")
# ============================================================
for label, rel in [
    ("ranking DB ディレクトリ", r"raw_data\kabu_station\ranking"),
    ("summary DB ディレクトリ", r"raw_data\kabu_station\summary"),
    ("push DB ディレクトリ", r"raw_data\kabu_station\push"),
    ("Basic", "Basic"),
]:
    p = nr.nas_path(rel)
    check(f"{label} が存在する", os.path.isdir(p), p)

print(f"\n結果: {PASS} / {PASS + FAIL}" + (f"  (skip {SKIP})" if SKIP else ""))
sys.exit(1 if FAIL else 0)
