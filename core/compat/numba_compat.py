# ============================================================
# File   : core/compat/numba_compat.py
# Version: V1.0-NUMBA-OPTIONAL-DEPENDENCY
# ------------------------------------------------------------
# numba を「あれば速い」だけの任意依存として扱うための唯一の窓口。
#
# 【なぜ必要か】(2026-09-02 08:42-08:48 の起動失敗)
#   main.py が終了コード 1 で停止した。実体は 1 行の top-level import:
#
#     main.py:116
#       from trading.handlers.exit_handler import build_5s_bar_fast, run_exit_pipeline
#         -> trading/handlers/exit_handler.py
#              from numba import njit
#                -> numba/core/dispatcher.py -> numba._dispatcher
#                   -> ImportError: DLL load failed while importing _devicearray:
#                      アプリケーション制御ポリシーによってこのファイルがブロックされました。
#
#   ブロックの主体は Windows の **Smart App Control** (SAC)。
#     HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy
#       VerifiedAndReputablePolicyState = 1   (= 強制)
#     CodeIntegrity/Operational  Event 3033 / 3077
#       Policy ID {0283ac0f-fff1-49ae-ada1-8a933130cad6}  (SAC の base policy)
#     対象ファイル
#       .venv\Lib\site-packages\numba\_devicearray.cp313-win_amd64.pyd
#       (12,288 bytes / 未署名 / WinError 4551)
#   同じ numba の他の .pyd (_dispatcher / _dynfunc / _helperlib / mviewbuf) は
#   すべてロードできる。C: へコピーしても同じく 4551 で落ちるので、
#   ネットワークドライブ (W:) が理由ではなく、その 1 ファイルの評価が理由。
#
#   つまり「numba を入れ直せば直る」類ではなく、
#   **OS 側の判断でいつでも再発しうる**。高速化のためだけの依存で
#   売買システム全体が起動しないのは設計として誤りなので、
#   ここで任意依存に格下げする。
#
# 【方針】
#   - このモジュールは import 時に絶対に例外を投げない。
#   - numba が使えるときは numba.njit をそのまま渡す (挙動は完全に従来どおり)。
#   - 使えないときは identity decorator を返す。
#     デコレートされた関数は **素の Python として同じ式を実行する**。
#     売買判定・EXIT 条件・計算式は一切変えない。
#   - 警告は import 時に 1 回だけ。起動ログを潰さないため traceback は出さない。
#
# 【使い方】
#     from core.compat.numba_compat import njit, NUMBA_AVAILABLE
#
#     @njit                 # どちらの書き方でも動く
#     def f(x): ...
#
#     @njit(cache=True)
#     def g(x): ...
#
#   numba が無い環境では cache= / fastmath= などの引数は黙って無視される
#   (numba 専用のチューニング指定であり、計算結果には影響しないため)。
#
# 【注意】
#   njit された関数に numba 固有の型制約 (reflected list など) を前提にした
#   コードを書かないこと。ここでフォールバックすると同じコードが
#   素の Python でも実行される。逆に言えば、
#   「素の Python でも同じ結果になる式」だけを njit すること。
# ============================================================

from __future__ import annotations

import logging
import os
from typing import Any, Callable

logger = logging.getLogger(__name__)

VERSION = "V1.0-NUMBA-OPTIONAL-DEPENDENCY"

# 明示的に JIT を切りたいとき (障害切り分け / 再現テスト用)。
_DISABLE_ENV = "AUTOSTOCK_DISABLE_NUMBA"


def _numba_disabled_by_env() -> bool:
    v = os.getenv(_DISABLE_ENV)
    if v is None or str(v).strip() == "":
        return False
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


_numba_njit: Callable[..., Any] | None = None
NUMBA_IMPORT_ERROR: str = ""
NUMBA_VERSION: str = ""

if _numba_disabled_by_env():
    NUMBA_IMPORT_ERROR = "disabled by %s" % _DISABLE_ENV
else:
    try:
        # numba は import しただけで _dispatcher -> _devicearray を掴みに行く。
        # DLL ロード失敗は ImportError にも OSError にもなりうるので広く受ける。
        from numba import njit as _numba_njit  # type: ignore[no-redef]
    except Exception as exc:  # noqa: BLE001
        # ImportError / OSError / DLL ロード失敗など。
        # ここで握らないと main.py の top-level import が落ちる。
        _numba_njit = None
        NUMBA_IMPORT_ERROR = "%s: %s" % (type(exc).__name__, exc)
    else:
        try:
            import numba as _numba_mod  # type: ignore

            NUMBA_VERSION = str(getattr(_numba_mod, "__version__", "") or "")
        except Exception:
            NUMBA_VERSION = ""

NUMBA_AVAILABLE: bool = _numba_njit is not None


def njit(*args: Any, **kwargs: Any):
    """numba.njit の薄いラッパ。

    numba が使えるときは numba.njit そのもの。
    使えないときは「関数をそのまま返す」identity decorator。
    `@njit` と `@njit(...)` のどちらの書き方でも動く。
    """
    if NUMBA_AVAILABLE and _numba_njit is not None:
        return _numba_njit(*args, **kwargs)

    # @njit (引数なしで直接デコレート)
    if len(args) == 1 and not kwargs and callable(args[0]):
        return args[0]

    # @njit(cache=True, ...) (引数付き) -> デコレータを返す
    def _identity(func):
        return func

    return _identity


# numba を使う側が「JIT なし」を明示的に判定したいとき用。
def jit_enabled() -> bool:
    return bool(NUMBA_AVAILABLE)


def status() -> dict:
    return {
        "numba_available": NUMBA_AVAILABLE,
        "numba_version": NUMBA_VERSION,
        "import_error": NUMBA_IMPORT_ERROR[:200],
        "disabled_by_env": _numba_disabled_by_env(),
        "version": VERSION,
    }


_LOGGED = False


def log_status_once() -> dict:
    """JIT の有無を 1 回だけログへ残す。

    モジュールは sys.modules にキャッシュされるので、
    sitecustomize 経由と main.py 経由で二度出ることはない。
    """
    global _LOGGED
    st = status()
    if _LOGGED:
        return st
    _LOGGED = True
    if NUMBA_AVAILABLE:
        logger.info("[NUMBA COMPAT] JIT 有効 version=%s", NUMBA_VERSION or "?")
    else:
        logger.warning(
            "[NUMBA COMPAT] numba を import できないため JIT なしで動作します "
            "(デコレートされた関数は通常の Python として実行。売買判定・計算式は不変) "
            "reason=%s "
            "(Windows Smart App Control が numba の _devicearray*.pyd を "
            "ブロックしている場合、これは想定内。%s=1 で明示的に無効化もできる)",
            NUMBA_IMPORT_ERROR[:200] or "unknown",
            _DISABLE_ENV,
        )
    return st


# import 時に 1 回だけ状態を出す。
# (ここで例外が出ても import を失敗させない)
try:
    log_status_once()
except Exception:  # pragma: no cover
    pass


__all__ = [
    "VERSION",
    "NUMBA_AVAILABLE",
    "NUMBA_VERSION",
    "NUMBA_IMPORT_ERROR",
    "njit",
    "jit_enabled",
    "status",
    "log_status_once",
]
