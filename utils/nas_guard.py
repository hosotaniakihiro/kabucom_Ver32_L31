# ============================================================
# File   : utils/nas_guard.py
# Version: NAS-GUARD-V1
# Function:
#   - NAS (SMB) に触る処理を「時間で見切る」ための共通部品
# ============================================================
"""NAS に触る処理は、必ずここを通して時間で見切る。

SMB のハングは `sqlite3` の timeout でも `socket` の timeout でも抜けられない。
OS の I/O でブロックするので、呼んだスレッドはそのまま固まる。
ハングした NAS への書き込みが取り込みや停止を道連れにするのを防ぐには、
**別スレッドへ出して締め切りで見切る**しかない。

見切ったスレッドは放置する (daemon なのでプロセス終了を妨げない)。
NAS が返ってくれば勝手に終わり、そのとき slot が解放されて次が動く。

同じ相手への処理を重ねると SMB のセッションを食い潰すので
(2026-09-02 の reconnect storm で実際に枯渇した)、呼び出し側が持つ
Semaphore で同時実行を 1 本に絞る。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = ["run_with_deadline", "new_slot"]


def new_slot() -> threading.Semaphore:
    """同時実行を 1 本に絞るための slot を作る。用途ごとに 1 つ持つ。"""
    return threading.Semaphore(1)


def run_with_deadline(
    fn: Callable[[], Any],
    *,
    timeout_sec: float,
    slot: Optional[threading.Semaphore] = None,
    slot_wait_sec: float = 0.0,
    label: str = "",
    thread_name: str = "nas_guard",
) -> dict:
    """``fn`` を別スレッドで実行し、``timeout_sec`` で見切る。

    戻り値:
        ok        : fn が例外なく終わった
        timed_out : 締め切りまでに終わらなかった (スレッドは放置)
        skipped   : slot が空かず実行しなかった
        error     : 例外の文字列
        result    : fn の戻り値 (ok のときだけ)
        elapsed_sec

    例外は投げない。NAS が不調でも呼び出し側の処理を続けさせるため。
    """
    started = time.perf_counter()
    out: dict = {
        "ok": False, "timed_out": False, "skipped": False,
        "error": "", "result": None, "elapsed_sec": 0.0, "label": label,
    }

    if slot is not None:
        if slot_wait_sec > 0:
            got = slot.acquire(blocking=True, timeout=float(slot_wait_sec))
        else:
            got = slot.acquire(blocking=False)
        if not got:
            out["skipped"] = True
            out["error"] = "前回の処理がまだ終わっていない (NAS が詰まっている可能性)"
            out["elapsed_sec"] = time.perf_counter() - started
            logger.warning("[NAS GUARD] skip label=%s (前回が未完了)", label)
            return out

    box: dict = {}

    def _worker() -> None:
        try:
            box["result"] = fn()
            box["ok"] = True
        except Exception as e:  # noqa: BLE001
            box["error"] = "%s: %s" % (type(e).__name__, e)
        finally:
            if slot is not None:
                slot.release()

    th = threading.Thread(target=_worker, daemon=True, name=thread_name)
    th.start()
    th.join(timeout=max(0.0, float(timeout_sec)))
    out["elapsed_sec"] = time.perf_counter() - started

    if th.is_alive():
        out["timed_out"] = True
        out["error"] = "%.0f 秒で完了しなかった (NAS のハングを疑う)" % float(timeout_sec)
        logger.error(
            "[NAS GUARD] 時間切れ label=%s %.0fs (処理は放置する。ローカル側は無事)",
            label, float(timeout_sec),
        )
        return out

    out["ok"] = bool(box.get("ok"))
    out["result"] = box.get("result")
    out["error"] = box.get("error", "")
    return out
