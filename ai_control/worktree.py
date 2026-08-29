# ============================================================
# File   : ai_control/worktree.py
# Purpose:
#   - AI 修正専用の git worktree を安全に用意する
# ------------------------------------------------------------
# なぜ worktree か:
#   本番の作業ツリー = いま稼働しているプロセスが読んでいるファイルそのもの。
#   Claude Code に本番ツリーを直接編集させると、テストが終わる前の
#   中途半端なファイルを稼働中の main.py が読む可能性がある。
#   別ディレクトリの worktree で修正し、テストを通してから人が反映する。
#
# 安全装置（どれか1つでも破れたら例外を投げて何もしない）:
#   1. worktree のパスが本番リポジトリの中にあってはならない
#   2. branch 名は auto-fix/ で始まらなければならない
#   3. reset --hard / clean は「そこが本当に目的の worktree か」を
#      git rev-parse --show-toplevel で確認してからしか実行しない
# ============================================================

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

BRANCH_PREFIX = "auto-fix/"


class WorktreeError(RuntimeError):
    pass


def _run(args: List[str], cwd: Optional[Path] = None, timeout: float = 180.0) -> Tuple[int, str]:
    # NAS 上のリポジトリでは worktree add が数分かかる。
    # タイムアウトは WorktreeError に変換して、呼び出し側で扱えるようにする。
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise WorktreeError("timeout (%.0fs): %s" % (timeout, " ".join(args[:4])))
    except OSError as e:
        raise WorktreeError("failed to run git: %s" % e)
    return proc.returncode, proc.stdout.decode("utf-8", errors="replace")


@dataclass
class WorktreeInfo:
    path: Path
    branch: str
    base_commit: str


class FixWorktree:
    """auto-fix 用の worktree を 1 つ使い回す。"""

    def __init__(self, repo_dir: Path, worktree_dir: Path, timeout_sec: float = 900.0) -> None:
        self.repo_dir = Path(repo_dir).resolve()
        self.worktree_dir = Path(worktree_dir).resolve()
        self.timeout_sec = float(timeout_sec)
        self._assert_outside_repo()

    def _git(self, *args: str) -> List[str]:
        """git コマンドを組み立てる。

        worktree のメタデータ (.git/worktrees/...) は SMB 上にあり所有者情報を
        持たないため、git が "dubious ownership" で実行を拒否する。
        グローバル設定を書き換えず、この呼び出しの中だけで例外を与える。
        """
        return [
            "git",
            "-c", "safe.directory=" + self.worktree_dir.as_posix(),
            "-c", "safe.directory=" + self.repo_dir.as_posix(),
            *args,
        ]

    # -- 安全装置 -------------------------------------------------

    def _assert_outside_repo(self) -> None:
        if self.worktree_dir == self.repo_dir:
            raise WorktreeError("worktree のパスが本番リポジトリと同一です")
        try:
            self.worktree_dir.relative_to(self.repo_dir)
        except ValueError:
            return  # 外にある = 正常
        raise WorktreeError(
            "worktree を本番リポジトリの内側に作ることはできません: %s" % self.worktree_dir
        )

    def _assert_is_this_worktree(self) -> None:
        rc, out = _run(self._git("rev-parse", "--show-toplevel"), cwd=self.worktree_dir)
        if rc != 0:
            raise WorktreeError("worktree が git 管理下にありません: " + out.strip())
        top = Path(out.strip()).resolve()
        if top != self.worktree_dir:
            raise WorktreeError(
                "worktree のトップが想定と違います (期待 %s / 実際 %s)" % (self.worktree_dir, top)
            )

    @staticmethod
    def _assert_branch_name(branch: str) -> None:
        if not branch.startswith(BRANCH_PREFIX):
            raise WorktreeError("branch 名は '%s' で始まる必要があります: %s" % (BRANCH_PREFIX, branch))

    # -- 本体 ---------------------------------------------------

    def head_commit(self) -> str:
        rc, out = _run(["git", "rev-parse", "HEAD"], cwd=self.repo_dir)
        if rc != 0:
            raise WorktreeError("HEAD を取得できません: " + out.strip())
        return out.strip()

    def prepare(self, branch: str, base_commit: str) -> WorktreeInfo:
        """worktree を base_commit の状態にして返す。"""
        self._assert_branch_name(branch)

        if not (self.worktree_dir / ".git").exists():
            self.worktree_dir.parent.mkdir(parents=True, exist_ok=True)
            rc, out = _run(
                self._git("worktree", "add", "--detach", str(self.worktree_dir), base_commit),
                cwd=self.repo_dir,
                timeout=self.timeout_sec,
            )
            if rc != 0:
                raise WorktreeError("worktree add に失敗: " + out.strip())

        self._assert_is_this_worktree()

        # base_commit から新しい auto-fix ブランチを作り直す。
        # ここは worktree の中だけを触る。本番ツリーには影響しない。
        rc, out = _run(self._git("checkout", "-B", branch, base_commit), cwd=self.worktree_dir, timeout=self.timeout_sec)
        if rc != 0:
            raise WorktreeError("checkout -B に失敗: " + out.strip())

        self._assert_is_this_worktree()
        rc, out = _run(self._git("reset", "--hard", base_commit), cwd=self.worktree_dir, timeout=self.timeout_sec)
        if rc != 0:
            raise WorktreeError("reset --hard に失敗: " + out.strip())

        rc, out = _run(self._git("clean", "-fdx", "-e", ".venv"), cwd=self.worktree_dir, timeout=self.timeout_sec)
        if rc != 0:
            raise WorktreeError("clean に失敗: " + out.strip())

        return WorktreeInfo(path=self.worktree_dir, branch=branch, base_commit=base_commit)

    def changed_files(self) -> List[str]:
        rc, out = _run(self._git("status", "--porcelain"), cwd=self.worktree_dir, timeout=self.timeout_sec)
        if rc != 0:
            return []
        files = []
        for line in out.splitlines():
            if len(line) > 3:
                files.append(line[3:].strip().strip('"'))
        return files

    def diff(self, max_chars: int = 200000) -> str:
        _run(self._git("add", "-A"), cwd=self.worktree_dir, timeout=self.timeout_sec)
        rc, out = _run(self._git("diff", "--cached"), cwd=self.worktree_dir, timeout=self.timeout_sec)
        if rc != 0:
            return ""
        if len(out) > max_chars:
            return out[:max_chars] + "\n...(以下省略)..."
        return out

    def diff_stat(self) -> str:
        rc, out = _run(self._git("diff", "--cached", "--stat"), cwd=self.worktree_dir, timeout=self.timeout_sec)
        return out if rc == 0 else ""

    def commit(self, message: str) -> Optional[str]:
        """修正を auto-fix ブランチにコミットして SHA を返す。"""
        self._assert_is_this_worktree()
        _run(self._git("add", "-A"), cwd=self.worktree_dir, timeout=self.timeout_sec)
        rc, out = _run(self._git("commit", "-m", message), cwd=self.worktree_dir, timeout=self.timeout_sec)
        if rc != 0:
            return None
        rc, sha = _run(self._git("rev-parse", "HEAD"), cwd=self.worktree_dir, timeout=self.timeout_sec)
        return sha.strip() if rc == 0 else None


class ReadOnlyWorktree:
    r"""Codex に読ませるための、ローカルディスク上の checkout。

    なぜ必要か:
        本番リポジトリは NAS (UNC \192.168.0.22\...) にある。
        codex exec の read-only サンドボックスは、ドライブレター (W:) でも
        UNC でも、-c sandbox_permissions=["disk-full-read-access"] を付けても
        ネットワークドライブを読めない（2026-08-29 に実測）。
        その結果 Codex は「ソースを読んで裏を取る」ことができず、
        ログだけの推測しか返せていなかった。

        ローカルディスク上の worktree なら読めることを実測で確認したので、
        診断役にはこちらを見せる。

    注意:
        ここは HEAD の内容であり、稼働中プロセスが実行している作業ツリー
        （未コミットの変更を含む）とは一致しないことがある。
        そのズレはプロンプトに明記して Codex に伝える。
    """

    def __init__(self, repo_dir: Path, worktree_dir: Path, timeout_sec: float = 900.0) -> None:
        self.repo_dir = Path(repo_dir).resolve()
        self.worktree_dir = Path(worktree_dir).resolve()
        self.timeout_sec = float(timeout_sec)
        if self.worktree_dir == self.repo_dir:
            raise WorktreeError("診断用 worktree のパスが本番リポジトリと同一です")
        try:
            self.worktree_dir.relative_to(self.repo_dir)
        except ValueError:
            pass
        else:
            raise WorktreeError(
                "診断用 worktree を本番リポジトリの内側に作ることはできません: %s" % self.worktree_dir
            )

    def _git(self, *args: str) -> List[str]:
        return [
            "git",
            "-c", "safe.directory=" + self.worktree_dir.as_posix(),
            "-c", "safe.directory=" + self.repo_dir.as_posix(),
            *args,
        ]

    def sync(self, commit: str) -> Path:
        """commit の内容にそろえて返す。失敗したら例外。"""
        if not (self.worktree_dir / ".git").exists():
            self.worktree_dir.parent.mkdir(parents=True, exist_ok=True)
            rc, out = _run(
                self._git("worktree", "add", "--detach", str(self.worktree_dir), commit),
                cwd=self.repo_dir,
                timeout=self.timeout_sec,
            )
            if rc != 0:
                raise WorktreeError("診断用 worktree add に失敗: " + out.strip())

        rc, out = _run(self._git("rev-parse", "--show-toplevel"), cwd=self.worktree_dir, timeout=self.timeout_sec)
        if rc != 0 or Path(out.strip()).resolve() != self.worktree_dir:
            raise WorktreeError("診断用 worktree が想定と違います: " + out.strip())

        rc, out = _run(
            self._git("checkout", "--detach", commit), cwd=self.worktree_dir, timeout=self.timeout_sec
        )
        if rc != 0:
            raise WorktreeError("診断用 worktree の checkout に失敗: " + out.strip())

        _run(self._git("reset", "--hard", commit), cwd=self.worktree_dir, timeout=self.timeout_sec)
        _run(self._git("clean", "-fdx"), cwd=self.worktree_dir, timeout=self.timeout_sec)
        return self.worktree_dir


def uncommitted_files(repo_dir: Path, timeout: float = 300.0) -> List[str]:
    """本番作業ツリーの未コミット変更を返す。診断のズレを伝えるために使う。"""
    try:
        rc, out = _run(["git", "status", "--porcelain"], cwd=Path(repo_dir), timeout=timeout)
    except WorktreeError:
        return []
    if rc != 0:
        return []
    return [ln[3:].strip().strip('"') for ln in out.splitlines() if len(ln) > 3]
