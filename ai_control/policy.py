# ============================================================
# File   : ai_control/policy.py
# Purpose:
#   - Codex の診断結果を GREEN / YELLOW / RED に分類する
# ------------------------------------------------------------
# 判定は二重化する。Codex の自己申告 (category) だけを信じない。
#   1. category による判定
#   2. files のパスによる判定 (policy.yaml の *_path_patterns)
# 両者のうち危険側を採用する。過小申告があっても発注系には落ちない。
# ============================================================

from __future__ import annotations

from fnmatch import fnmatch
from typing import Iterable, List, Tuple

GREEN, YELLOW, RED = "GREEN", "YELLOW", "RED"
_RANK = {GREEN: 0, YELLOW: 1, RED: 2}

# 分類ごとに第2段階以降で許す動作
ACTION = {
    GREEN: "AUTO_FIX_AND_RESTART",
    YELLOW: "AUTO_FIX_DRY_RUN_ONLY",
    RED: "HUMAN_REVIEW_REQUIRED",
}


def _norm(path: str) -> str:
    return str(path).replace("\\", "/").lstrip("./")


class Policy:
    def __init__(self, data: dict) -> None:
        self.auto_fix = set(data.get("auto_fix") or [])
        self.dry_run_only = set(data.get("dry_run_only") or [])
        self.manual_approval = set(data.get("manual_approval") or [])
        self.red_paths: List[str] = list(data.get("red_path_patterns") or [])
        self.yellow_paths: List[str] = list(data.get("yellow_path_patterns") or [])
        self.never: List[str] = list(data.get("never") or [])

    def _by_category(self, category: str) -> Tuple[str, str]:
        c = (category or "").strip()
        if c in self.manual_approval:
            return RED, f"category '{c}' は manual_approval"
        if c in self.dry_run_only:
            return YELLOW, f"category '{c}' は dry_run_only"
        if c in self.auto_fix:
            return GREEN, f"category '{c}' は auto_fix"
        # 未知・unknown は安全側に倒す
        return RED, f"category '{c or '(空)'}' は policy.yaml に無い → 安全側で RED"

    def _by_paths(self, files: Iterable[str]) -> Tuple[str, str]:
        worst, reason = GREEN, "対象ファイルは制限パターンに該当しない"
        for f in files or []:
            p = _norm(f)
            base = p.rsplit("/", 1)[-1]
            for pat in self.red_paths:
                if fnmatch(p, pat) or fnmatch(base, pat) or fnmatch(p, "*/" + pat):
                    return RED, f"'{f}' が red_path_patterns '{pat}' に該当"
            for pat in self.yellow_paths:
                if fnmatch(p, pat) or fnmatch(base, pat) or fnmatch(p, "*/" + pat):
                    if _RANK[YELLOW] > _RANK[worst]:
                        worst, reason = YELLOW, f"'{f}' が yellow_path_patterns '{pat}' に該当"
        return worst, reason

    def classify(self, category: str, files: Iterable[str]) -> dict:
        cat_level, cat_reason = self._by_category(category)
        path_level, path_reason = self._by_paths(files)
        level = cat_level if _RANK[cat_level] >= _RANK[path_level] else path_level
        return {
            "level": level,
            "action": ACTION[level],
            "by_category": {"level": cat_level, "reason": cat_reason},
            "by_paths": {"level": path_level, "reason": path_reason},
            "decided_by": "category" if _RANK[cat_level] >= _RANK[path_level] else "paths",
        }
