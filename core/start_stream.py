# ============================================================
# start_stream.py
# Ver2.0-PRODUCTION-ENTRYPOINT-FINAL
# ------------------------------------------------------------
# ✔ ストリーム起動エントリーポイント
# ✔ ログ初期化
# ✔ 例外完全耐性
# ✔ Ctrl+C安全終了
# ✔ Windows TaskScheduler対応
# ✔ 本番常駐設計
# ============================================================

from __future__ import annotations

import logging
import sys
import signal
import traceback
from pathlib import Path

from core.runtime.stream_orchestrator import StreamOrchestrator


# ============================================================
# ログ設定
# ============================================================

def setup_logging():

    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    log_file = log_dir / "stream_runtime.log"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ============================================================
# グローバル制御
# ============================================================

orchestrator: StreamOrchestrator | None = None


def shutdown_handler(signum, frame):

    logging.info("⏹ Shutdown signal received")

    global orchestrator

    if orchestrator:
        orchestrator.stop()

    logging.info("✅ Stream stopped safely")
    sys.exit(0)


# ============================================================
# メイン
# ============================================================

def main():

    global orchestrator

    setup_logging()

    logging.info("🚀 Starting Stream Engine")

    orchestrator = StreamOrchestrator(
        sleep_sec=0.2   # 必要に応じて調整
    )

    try:
        orchestrator.start()

    except KeyboardInterrupt:
        logging.info("⌨ KeyboardInterrupt received")
        shutdown_handler(None, None)

    except Exception:
        logging.error("🔥 Fatal error in stream engine")
        traceback.print_exc()
        shutdown_handler(None, None)


# ============================================================
# シグナル登録
# ============================================================

if __name__ == "__main__":

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    main()