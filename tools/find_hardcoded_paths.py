# ============================================================
# find_hardcoded_paths.py
# ------------------------------------------------------------
# プロジェクトルートから直書きパスを検出
# ・画面表示
# ・CSV 出力（hardcoded_paths.csv）
# ============================================================

from pathlib import Path
import csv

# ------------------------------------------------------------
# プロジェクトルートを自動判定
# tools/find_hardcoded_paths.py → 1階層上
# ------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent

# ------------------------------------------------------------
# 設定
# ------------------------------------------------------------
TARGET_EXTENSIONS = {".py"}

TARGET_PATTERNS = [
    "X:/",
    "Y:/",
    "\\\\192.168.",
]

EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    "venv",
    ".venv",
    "env",
    "logs",
}

# ------------------------------------------------------------
def main():
    hits = 0
    rows = []

    print(f"[INFO] Project root = {ROOT_DIR}")

    for file_path in ROOT_DIR.rglob("*"):
        # 除外ディレクトリ
        if any(part in EXCLUDE_DIRS for part in file_path.parts):
            continue

        # 拡張子チェック
        if file_path.suffix not in TARGET_EXTENSIONS:
            continue

        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern in TARGET_PATTERNS:
                if pattern in line:
                    print(f"{file_path}:{lineno}: {line.strip()}")
                    rows.append([str(file_path), lineno, line.strip()])
                    hits += 1

    # --------------------------------------------------------
    # CSV 出力
    # --------------------------------------------------------
    csv_path = ROOT_DIR / "hardcoded_paths.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["file", "line", "content"])
        writer.writerows(rows)

    print("-" * 60)
    print(f"Total hits: {hits}")
    print(f"[INFO] CSV written to: {csv_path}")


# ------------------------------------------------------------
if __name__ == "__main__":
    main()
