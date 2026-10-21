# ============================================================
# File   : data_collectors/log_mirror.py
# Version: LOG-MIRROR-V1
# Function:
#   - ローカルに書いたコンソールログを NAS へ追記コピーする
# ============================================================
r"""ログはローカルに書き、NAS へは追記でミラーする。

ログを NAS へ直接書いていると、NAS がハングした瞬間に
**logging がブロックして全プロセスが止まる**。しかも同時に調査手段
(ログ) も失う。2026-09-05 に「最近 NAS がハングすることがある」と
報告を受けたため、ログの書き込み先をローカルへ移した。

ただし NAS 上のログは今までどおり参照できたほうが都合がよいので、
定期的に **増えたぶんだけ** NAS へ追記する。コンソールログは
append-only のテキストなので、前回どこまで送ったかを覚えておけば
差分だけを送れる (1 日 60MB を毎回まるごと送らずに済む)。

NAS に触る部分はすべて utils.nas_guard 経由で締め切りを付ける。
ミラーが失敗してもローカルのログは無傷なので、後から拾える。

環境変数:
  KABU_LOG_MIRROR_TO_NAS        0 でミラーしない (既定 1)
  KABU_LOG_MIRROR_INTERVAL_SEC  既定 300
  KABU_LOG_MIRROR_TIMEOUT_SEC   既定 60
  KABU_LOG_NAS_DIR              ミラー先 (既定 X:\logs\console)
"""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Dict

from utils.nas_guard import new_slot, run_with_deadline

logger = logging.getLogger(__name__)

__all__ = ["register", "mirror_once", "start_background_mirror", "nas_log_dir"]

_SLOT = new_slot()
_LOCK = threading.Lock()
_OFFSETS: Dict[str, int] = {}   # ローカルのログ -> 送信済みバイト数
_THREAD: threading.Thread | None = None
_CHUNK = 4 * 1024 * 1024


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return default
        return float(str(v).strip())
    except Exception:
        return default


def mirror_enabled() -> bool:
    return _env_bool("KABU_LOG_MIRROR_TO_NAS", True)


def mirror_interval_sec() -> float:
    return max(10.0, _env_float("KABU_LOG_MIRROR_INTERVAL_SEC", 300.0))


def mirror_timeout_sec() -> float:
    return max(5.0, _env_float("KABU_LOG_MIRROR_TIMEOUT_SEC", 60.0))


def nas_log_dir() -> Path:
    return Path(os.environ.get("KABU_LOG_NAS_DIR", r"X:\logs\console"))


def register(path: Path | str) -> None:
    """このプロセスが書くログを、ミラー対象として覚える。"""
    with _LOCK:
        _OFFSETS.setdefault(str(path), 0)


def _append_new_bytes(local: Path, dst_dir: Path) -> int:
    """local の未送信ぶんを NAS へ追記する。送ったバイト数を返す。"""
    sent = 0
    key = str(local)
    offset = _OFFSETS.get(key, 0)
    try:
        size = local.stat().st_size
    except Exception:
        return 0

    if size <= offset:
        # ローテーション等で縮んでいたら送り直す
        if size < offset:
            offset = 0
        else:
            return 0

    dst = dst_dir / local.name
    with open(local, "rb") as fsrc:
        fsrc.seek(offset)
        mode = "ab" if offset else "wb"
        with open(dst, mode) as fdst:
            while True:
                chunk = fsrc.read(_CHUNK)
                if not chunk:
                    break
                fdst.write(chunk)
                sent += len(chunk)

    with _LOCK:
        _OFFSETS[key] = offset + sent
    return sent


def _mirror_all() -> dict:
    dst_dir = nas_log_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        targets = list(_OFFSETS.keys())

    total = 0
    files = 0
    for t in targets:
        try:
            n = _append_new_bytes(Path(t), dst_dir)
            if n:
                total += n
                files += 1
        except Exception as e:  # noqa: BLE001
            logger.debug("[LOG MIRROR] %s の転送に失敗: %s", t, e)
    return {"files": files, "bytes": total}


def mirror_once(*, reason: str = "periodic") -> dict:
    """増えたぶんを NAS へ送る。NAS がハングしても呼び出し側を固めない。"""
    if not mirror_enabled():
        return {"ok": True, "skipped": True, "error": "mirror 無効"}

    guard = run_with_deadline(
        _mirror_all,
        timeout_sec=mirror_timeout_sec(),
        slot=_SLOT,
        label="log_mirror:%s" % reason,
        thread_name="log_mirror_once",
    )
    if guard["ok"]:
        r = guard["result"] or {}
        if r.get("bytes"):
            logger.info(
                "[LOG MIRROR] %d ファイル %.1fMB を NAS へ送った (%.1fs)",
                r.get("files", 0), r.get("bytes", 0) / 1024 / 1024, guard["elapsed_sec"],
            )
    elif guard["timed_out"]:
        logger.error(
            "[LOG MIRROR] 時間切れ (%.0fs)。NAS のハングを疑う。"
            "ローカルのログは書けているので調査はそちらを見ること: %s",
            mirror_timeout_sec(), Path(next(iter(_OFFSETS), "")).parent,
        )
    return guard


def start_background_mirror() -> None:
    """プロセスに 1 本だけミラースレッドを立てる。"""
    global _THREAD
    if not mirror_enabled():
        return
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return

        interval = mirror_interval_sec()

        def _loop() -> None:
            while True:
                time.sleep(interval)
                try:
                    mirror_once()
                except Exception:
                    logger.debug("[LOG MIRROR] 定期ミラーで例外", exc_info=True)

        _THREAD = threading.Thread(target=_loop, daemon=True, name="log_mirror")
        _THREAD.start()
