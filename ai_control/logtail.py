# ============================================================
# File   : ai_control/logtail.py
# Purpose:
#   - NAS 上のログディレクトリから、prefix ごとに「最新の 1 本」を追尾する
#   - 追記分だけを行単位で返す
# ------------------------------------------------------------
# 前提:
#   core/logging/console_tee.py は起動ごとに
#       <prefix>_<YYYYmmdd>_<HHMMSS>_<PID>.log
#   を新規作成する。ファイル名が固定されないので「最新を選び直す」必要がある。
#
# NAS への配慮:
#   - ディレクトリ走査 (scandir) は rescan_interval_sec ごと。毎ポーリングやらない
#   - 既知ファイルは stat() だけ。サイズが変わっていなければ open すらしない
#   - 1 回に読む量に上限を置く。溢れたら古い側を捨てて追いつく（本体は止めない）
# ============================================================

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass
class LogLine:
    prefix: str
    file_name: str
    text: str


@dataclass
class _FileState:
    path: Path
    offset: int = 0
    partial: str = ""
    dropped_bytes: int = 0


class LogTailer:
    """prefix ごとに最新ログ 1 本を追尾する。"""

    def __init__(
        self,
        log_dir: str | Path,
        prefixes: Iterable[str],
        *,
        suffix: str = ".log",
        start_at: str = "end",
        max_read_bytes: int = 262144,
        rescan_interval_sec: float = 60.0,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.prefixes = list(prefixes)
        self.suffix = suffix
        self.start_at = start_at
        self.max_read_bytes = int(max_read_bytes)
        self.rescan_interval_sec = float(rescan_interval_sec)

        self._active: Dict[str, _FileState] = {}
        self._last_rescan: float = 0.0
        self._first_scan_done: bool = False
        self._last_growth_ts: float = time.time()

    # -- 公開 ---------------------------------------------------

    @property
    def last_growth_ts(self) -> float:
        """いずれかのログが最後に伸びた時刻 (epoch)。停止検出に使う。"""
        return self._last_growth_ts

    def active_files(self) -> Dict[str, str]:
        return {p: st.path.name for p, st in self._active.items()}

    def dropped_bytes(self) -> Dict[str, int]:
        return {p: st.dropped_bytes for p, st in self._active.items() if st.dropped_bytes}

    def poll(self) -> List[LogLine]:
        """追記された行を返す。呼ぶたびに前回からの差分だけ。"""
        now = time.time()
        if now - self._last_rescan >= self.rescan_interval_sec or not self._first_scan_done:
            self._rescan()
            self._last_rescan = now

        out: List[LogLine] = []
        for prefix, state in list(self._active.items()):
            out.extend(self._read_new(prefix, state))
        if out:
            self._last_growth_ts = time.time()
        return out

    # -- 内部 ---------------------------------------------------

    def _rescan(self) -> None:
        """prefix ごとに mtime が最新のファイルを選び直す。"""
        newest: Dict[str, Tuple[float, Path]] = {}
        try:
            with os.scandir(self.log_dir) as it:
                for entry in it:
                    if not entry.is_file():
                        continue
                    name = entry.name
                    if not name.endswith(self.suffix):
                        continue
                    for prefix in self.prefixes:
                        # console_20260828_152301_70020.log
                        if not name.startswith(prefix + "_"):
                            continue
                        # console と console_xxx_runner を取り違えないよう、
                        # より長い prefix が一致するならそちらを優先する
                        best = max(
                            (p for p in self.prefixes if name.startswith(p + "_")),
                            key=len,
                        )
                        if best != prefix:
                            continue
                        try:
                            mtime = entry.stat().st_mtime
                        except OSError:
                            continue
                        cur = newest.get(prefix)
                        if cur is None or mtime > cur[0]:
                            newest[prefix] = (mtime, Path(entry.path))
                        break
        except OSError as e:
            # NAS が一時的に見えないことはある。次の周回で復帰させる
            print(f"[logtail] scandir failed: {self.log_dir}: {e}", flush=True)
            return

        for prefix, (_mtime, path) in newest.items():
            cur = self._active.get(prefix)
            if cur is not None and cur.path == path:
                continue

            if cur is None and not self._first_scan_done and self.start_at == "end":
                # supervisor 起動時に既に存在したファイル → 末尾から
                try:
                    offset = path.stat().st_size
                except OSError:
                    offset = 0
            else:
                # 起動後に現れた新しいログ = プロセスが再起動した → 先頭から
                offset = 0

            self._active[prefix] = _FileState(path=path, offset=offset)
            print(f"[logtail] watch {prefix}: {path.name} (offset={offset})", flush=True)

        self._first_scan_done = True

    def _read_new(self, prefix: str, state: _FileState) -> List[LogLine]:
        try:
            size = state.path.stat().st_size
        except OSError:
            return []

        if size < state.offset:
            # ローテートや切り詰め
            state.offset = 0
            state.partial = ""

        if size <= state.offset:
            return []

        available = size - state.offset
        if available > self.max_read_bytes:
            # 追いつけない量が溜まった。古い側を捨てて最新に寄せる。
            skipped = available - self.max_read_bytes
            state.offset = size - self.max_read_bytes
            state.dropped_bytes += skipped
            state.partial = ""
            print(
                f"[logtail] {prefix}: skipped {skipped} bytes to catch up "
                f"(total dropped {state.dropped_bytes})",
                flush=True,
            )
            available = self.max_read_bytes

        try:
            with open(state.path, "rb") as f:
                f.seek(state.offset)
                raw = f.read(available)
        except OSError as e:
            print(f"[logtail] read failed: {state.path.name}: {e}", flush=True)
            return []

        state.offset += len(raw)
        text = state.partial + raw.decode("utf-8", errors="replace")

        # 最後の行が途中で切れている可能性があるので繰り越す
        if text.endswith("\n"):
            lines = text.splitlines()
            state.partial = ""
        else:
            lines = text.splitlines()
            state.partial = lines.pop() if lines else text

        return [LogLine(prefix=prefix, file_name=state.path.name, text=ln) for ln in lines]
