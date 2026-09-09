# ============================================================
# File   : scripts/push_receiver_runner.py
# Version: DATA-COLLECTORS-PUSH-RECEIVER-RUNNER-V4-START-PUSH-STORAGE
# ------------------------------------------------------------
# PUSH パイプラインの所有関係 (2026-09-03 整理)
#
#   このプロセス (role=push_receiver) だけが以下すべてを所有する。
#   分割して別プロセスに持たせない。
#
#     WebSocket 接続        trading/push/push_stream/runner._run_forever_loop
#                           -> state._ws_thread (1 本のみ / ws_owner ロックで排他)
#     receive queue         state._push_queue   (on_message が put)
#     tick リング           state._push_rows    (メモリ df の材料)
#     flush worker          state._flush_thread (queue -> StreamDBWriter)
#     PUSH DB writer        trading/push/push_db_writer.stream_writer (singleton)
#                           -> state._stream_writer に **同じ実体** を注入する
#
#   注意:
#     push_db_writer.stream_writer (モジュール singleton) が start 済みでも、
#     state._stream_writer が None なら flush は 1 行も進まない。
#     health の writer_ready=False はこの state 側を見ている。
#     2026-09-03 の障害では runner が ws_owner ロックを取れずに
#     途中 return したため、singleton は起動済みなのに
#     state._stream_writer / _flush_thread が未設定のままだった。
# ============================================================

from __future__ import annotations

import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from data_collectors.logging_setup import setup_logging
from data_collectors.push_runtime import run_forever


def _start_push_storage_for_database_process(logger: logging.Logger) -> None:
    """
    PUSH DB保存は main.py では起動しない。
    main_database.py -> push_receiver_runner.py 側だけで、PUSH受信開始前に
    StreamDBWriter を明示起動する。

    これにより main.py 側は memory_only=True のまま維持しつつ、
    main_database.py 側では writer_ready/flush_alive/total_flushed が進む。
    """
    try:
        from core.startup.push.storage_bootstrap import start_push_storage

        # 2026-09-09: ここで 500 を直書きしていたため、push_db_writer 側で
        # 既定を 500 -> 5000 に上げた 2026-09-08 の修正が効いていなかった。
        # 本番でキューが 10 分で 22,000 行増え、上限 200,000 に向かっていた。
        # 引数を渡さず writer の既定 (_push_db_buffer_size / 既定 5000、
        # PUSH_DB_BUFFER_SIZE で変更可) に任せる。
        start_push_storage()
        logger.warning(
            "[PUSH RECEIVER RUNNER] push storage start requested in database process only"
        )
    except Exception:
        logger.exception("[PUSH RECEIVER RUNNER] push storage start failed")


def main() -> int:
    logger = setup_logging("push_receiver_runner")
    logger.info("[PUSH RECEIVER RUNNER] START")
    _start_push_storage_for_database_process(logger)
    return run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
