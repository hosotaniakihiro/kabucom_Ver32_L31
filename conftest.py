# ============================================================
# pytest 専用 conftest.py
# kabucom_Ver18_04 プロジェクトをモジュールとして認識させる
# ============================================================

import sys
import os

# このファイル（conftest.py）のあるディレクトリ＝プロジェクトルート
ROOT_DIR = os.path.dirname(os.path.abspath(__file__))

# trading/ や kabu_api/ を import 可能にする
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

print(f"[pytest] PYTHONPATH 追加: {ROOT_DIR}")
