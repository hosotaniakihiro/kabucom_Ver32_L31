# ============================================================
# File   : trading/push/push_stream/rotation_settings.py
# Version: PRODUCTION-STABLE-REV1.7-PUSH-STRICT-AB-ROTATION
# ------------------------------------------------------------
# PUSH A/Bローテーションの時間設定を集約する。
#
# User design / default flow:
#   A register -> hold 4.8s -> unregister/all -> wait 0.2s -> B register
#   B register -> hold 4.8s -> unregister/all -> wait 0.2s -> A register
#
# Notes:
#   - register/unregister は kabu Station の REST API。
#   - rotation では必ず unregister_all を使う。
#   - 外部で環境変数を明示指定している場合は、その値を優先する。
# ============================================================

from __future__ import annotations

import os

from .constants import (
    DEFAULT_REGISTER_CHUNK_SIZE,
    DEFAULT_REGISTER_MAX_SYMBOLS,
)

VERSION = "PRODUCTION-STABLE-REV1.7-PUSH-STRICT-AB-ROTATION"


# 既存 rotation.py 互換のため、import 時点で環境変数デフォルトを注入する。
# os.environ.setdefault のため、ユーザーが明示指定した値は上書きしない。
os.environ.setdefault("PUSH_ROTATION_HOLD_SEC", "4.8")
os.environ.setdefault("PUSH_ROTATION_UNREGISTER_WAIT_SEC", "0.2")
os.environ.setdefault("PUSH_ROTATION_WAIT_AFTER_CLEAR_SEC", "0.2")
os.environ.setdefault("PUSH_ROTATION_CLEAR_FIRST", "1")
os.environ.setdefault("PUSH_ROTATION_UNREGISTER_FIRST", "1")
os.environ.setdefault("PUSH_ROTATION_REGISTER_FORCE", "1")
os.environ.setdefault("PUSH_ROTATION_REGISTER_REQUIRE_WS", "0")
os.environ.setdefault("PUSH_ROTATION_WS_STABLE_GRACE_SEC", "0.0")
os.environ.setdefault("PUSH_ROTATION_WS_STABLE_MAX_WAIT_SEC", "1.0")
os.environ.setdefault("PUSH_ROTATION_WS_WAIT_LOG_INTERVAL_SEC", "10.0")
os.environ.setdefault("PUSH_ROTATION_REGISTER_TIMEOUT_SEC", "18.0")
os.environ.setdefault("PUSH_ROTATION_LIQ_GUARD_TIMEOUT_SEC", "0.25")
os.environ.setdefault("PUSH_ROTATION_LIQ_GUARD_TIMEOUT_FAIL_OPEN", "1")

# 2026-09-11: 登録母集団を kabu Station の上限 50 に合わせる。
#   既定は 100 (constants.DEFAULT_REGISTER_MAX_SYMBOLS)。設計コメントは
#   「A50 -> unregister/all -> B50」だが、その方式は差分登録
#   (PUSH_REGISTER_DIFFERENTIAL_ENABLED, 既定 True) に置き換わっている。
#   差分登録は「目標リスト = あるべき登録状態」として動くため、
#   枠 50 に対し母集団 100 を回すと 5 秒ごとに 48 銘柄を総入れ替えする
#   ([DIFF REGISTER] current=50 target=50 keep=2 remove=48 add=48)。
#   解除 48 と登録 48 が同一サイクルで走るので一瞬 最大 98 登録となり
#   上限 50 を超え、4001019 (一部の銘柄が登録できません) で部分失敗する。
#   こうなると /board /symbol が銘柄を問わず 4002006 を返し、
#   登録済みの銘柄ですら板が取れない。
#   実測 (2026-09-11 後場, 同一条件で比較):
#     母集団 100 -> 1,590 行/分 / 1 分あたり 28 銘柄 / DIFF REGISTER 多発
#     母集団  50 -> 3,147 行/分 / 1 分あたり 49 銘柄 / DIFF REGISTER 0 回
#   名目 100 銘柄より実効カバレッジが高い。差分がゼロになり登録が安定する。
#   100 銘柄を回したいなら、差分登録と共存する形に作り直すこと
#   (例: 枠 50 のうち入れ替えるのは数銘柄に留める)。この 2 行はその暫定対処。
os.environ.setdefault("PUSH_ROTATION_TARGET_MAX_KEEP", "50")
os.environ.setdefault("PUSH_REGISTER_MAX_KEEP", "50")

# 2026-10-21 (ユーザー判断): 50 銘柄ずつのローテーション(A=1〜50 番目 / B=51〜100 番目を交互に登録)。
#   中継(2 つ目の口座)がつながっていないと 51〜100 番目はどこでも受信されず、
#   順位の低い銘柄は「直近の動き順」で上位 50 に入ったときしか PUSH が来なかった
#   (例: 3436 は 9:40〜15:00 の 320 分のうち約 200 分途切れ、上昇の 10:03〜10:44 を丸ごと逃した)。
#   9/11 の 4001019 (同じサイクルで 48 解除と 48 登録 → 一瞬 98 登録) を避けるため、
#   A→B の入れ替えは rotation_core で「両方に共通の銘柄だけ残す(=解除) → 待つ → 登録」の 2 段にする。
#   PUSH_ROTATION_AB50_ENABLED=0 で従来(上位 50 + 固定だけの B)に戻る。
# 2026-10-23: 既定を 0 に戻す。AB50 を有効にした 10/22 から、9:00〜11:30 の WS 切断中の割合が
#   0〜5% (10/19〜10/21) → 28% (10/22) → 42% (10/23) に増えた (kabu 側からの reset_by_peer、接続から数秒で切れる)。
#   PUSH_ROTATION_AB50_ENABLED=1 で再び有効になる。
AB50_ENABLED = str(os.environ.get("PUSH_ROTATION_AB50_ENABLED", "0")).strip().lower() not in ("0", "false", "no", "off")
if AB50_ENABLED:
    os.environ["PUSH_ROTATION_TARGET_MAX_KEEP"] = os.environ.get("PUSH_ROTATION_AB50_TARGET_MAX_KEEP", "100")
os.environ.setdefault("PUSH_ROTATION_AB50_SWAP_WAIT_SEC", "0.3")

# subscription_manager.register_ops 用。
# rotation側は PUSH_ROTATION_UNREGISTER_WAIT_SEC=0.2 を優先する。
# startup等のrotation以外は安全側の既定値を維持する。
os.environ.setdefault("KABU_REGISTER_UNREGISTER_WAIT_SEC", "1.5")
os.environ.setdefault("KABU_REGISTER_COUNT_ERROR_RETRY_WAIT_SEC", "2.0")


# 登録した銘柄を維持する秒数。
ROTATE_HOLD_SEC = float(os.environ["PUSH_ROTATION_HOLD_SEC"])

# A/B切替時の unregister/all 後の待機秒数。
UNREGISTER_TO_REGISTER_WAIT_SEC = float(os.environ["PUSH_ROTATION_UNREGISTER_WAIT_SEC"])

# WebSocket準備待ちログの間隔。
WS_WAIT_LOG_INTERVAL_SEC = float(os.environ["PUSH_ROTATION_WS_WAIT_LOG_INTERVAL_SEC"])

# 登録API呼び出しの最大待機秒数。hold時間とは別。
REGISTER_TIMEOUT_SEC = float(os.environ["PUSH_ROTATION_REGISTER_TIMEOUT_SEC"])


__all__ = [
    "VERSION",
    "DEFAULT_REGISTER_CHUNK_SIZE",
    "DEFAULT_REGISTER_MAX_SYMBOLS",
    "ROTATE_HOLD_SEC",
    "UNREGISTER_TO_REGISTER_WAIT_SEC",
    "WS_WAIT_LOG_INTERVAL_SEC",
    "REGISTER_TIMEOUT_SEC",
    "AB50_ENABLED",
]
