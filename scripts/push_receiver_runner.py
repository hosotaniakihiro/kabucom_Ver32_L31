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

# 2026-09-25: Norton が注入する SSLKEYLOGFILE を外す。
#   プロセス環境に SSLKEYLOGFILE=\\.\nllMonFltProxy\... が入っており
#   (Machine/User の環境変数には無い。Norton が各プロセスへ注入する)、
#   urllib3 は HTTPS 接続ごとにこれを読んで ssl context の
#   keylog_filename に設定する。その setter が Norton の名前付きパイプを
#   open() しようとして応答待ちになり、スレッドが永久に止まる。
#   socket より手前なので timeout= は効かない。
#   9/25 は ranking_summary_bg が Discord 通知の中でこれに掛かり、
#   generation=1 のまま 20 分以上完走しなかった。
#   env AUTOSTOCK_KEEP_SSLKEYLOGFILE=1 で従来どおり残す。
import os as _autostock_os

if _autostock_os.environ.get("SSLKEYLOGFILE") and str(
    _autostock_os.environ.get("AUTOSTOCK_KEEP_SSLKEYLOGFILE", "0")
).strip().lower() not in ("1", "true", "yes", "on"):
    _autostock_keylog = _autostock_os.environ.pop("SSLKEYLOGFILE", None)
    print(
        "[TLS ENV GUARD] SSLKEYLOGFILE を外した (Norton の TLS 監視が"
        " HTTPS 接続を固めるため): %s" % (_autostock_keylog,)
    )


import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 2026-09-25: summary DB の列突き合わせを engine 作成前に無効化する。
#   NAS (X:) 上の summary DB に対して PRAGMA table_info を引くため
#   1 テーブル 66〜82 秒かかり、起動が 10 分になっていた。その間
#   WebSocket を張らないので PUSH が全断し、[PUSH STALL] が出続ける。
#   結果は "table schema ok" = 追加列 0 件で、何も変えていない。
#   main.py は _install_main_runtime_patches で同じ関数を呼んでいるが
#   scripts/*_runner.py は呼んでいなかった (env だけではフラグが立たない)。
#   列の補完が必要なときは FAST_STARTUP_SKIP_SUMMARY_SCHEMA_BOOTSTRAP=0。
try:
    from database.session import apply_fast_startup_schema_skip as _autostock_fs_skip

    _autostock_fs_skip()
except Exception as _autostock_fs_err:  # noqa: BLE001
    print("[FAST STARTUP] summary schema skip を適用できなかった: %r" % (_autostock_fs_err,))

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
