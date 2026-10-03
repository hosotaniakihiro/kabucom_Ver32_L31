# ============================================================
# File   : nas_root.py
# Version: Ver1.0-NAS-ROOT-SINGLE-OWNER (R18)
# ------------------------------------------------------------
# 【なぜ必要か】
#   同じ共有に UNC (\\192.168.0.22\AutoStockBuyAndSell) と
#   ドライブレター (X:) の 2 表記で触ると、Windows は
#   **別々の SMB セッション**として扱う。
#   2026-08-28 17:1x の実測:
#       os.path.exists(UNC + r"\raw_data\...\ranking20260828.db") -> False
#       os.path.exists(r"X:\raw_data\...\ranking20260828.db")     -> True (607.2MB)
#   UNC 側のセッションが切れると path_exists() が False になり、
#   read_sqlite_table() が「db not found」で **空 DataFrame を返す**。
#   「無かった」と「読めなかった」が同じ空フレームになるのは危険。
#
#   第17ラウンドでは trading/ranking/summary/utils.py にだけ
#   到達可能表記へ寄せる処理を入れたが、UNC 表記のハードコードは
#   他にも多数残っていた。所有者をこのモジュール 1 つに統一する。
#
# 【依存】
#   標準ライブラリのみ。ルート直下に置く
#   (global_state / settings と同じ既存の慣習)。
#
# 【ENV】
#   AUTOSTOCK_NAS_ROOT           明示指定 (最優先)
#   AUTOSTOCK_NAS_ROOT_FALLBACK  既定 "X:" (UNC が到達不能なときに使う)
# ============================================================

from __future__ import annotations

import logging
import os
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# AutoStockBuyAndSell 共有の既定 UNC 表記
UNC_ROOT = r"\\192.168.0.22\AutoStockBuyAndSell"

# 既定のドライブレター表記 (X: = \\192.168.0.22\AutoStockBuyAndSell)
DEFAULT_FALLBACK = "X:"

_LOCK = threading.Lock()
_CACHE: Optional[str] = None


def _probe(path: str) -> bool:
    try:
        return os.path.exists(path)
    except Exception:
        logger.debug("[NAS ROOT] probe failed path=%s", path, exc_info=True)
        return False


def _as_dir_root(root: str) -> str:
    """"X:" のようなドライブ名だけなら末尾に区切りを付けて "X:" + 区切り にする。

    2026-10-03: "X:" のままだと os.path.join("X:", "raw_data") や Path("X:") / "raw_data" が
    "X:raw_data" (X ドライブの「カレントフォルダ」からの相対パス) になる。10/2 の yahoo_complement は
    起動時に UNC の確認が一時的に失敗して "X:" に落ち、yahoo_backfill_status の書き込みが
    "unable to open database file" で 1 日約 1.2 万回失敗していた。
    """
    r = str(root or "")
    if len(r) == 2 and r[1] == ":":
        return r + os.sep
    return r


def nas_root() -> str:
    """到達可能な NAS root 表記を返す (プロセス内で 1 回だけ判定)。

    優先順:
      1. AUTOSTOCK_NAS_ROOT の明示指定
      2. UNC (到達可能なら)
      3. AUTOSTOCK_NAS_ROOT_FALLBACK / 既定 "X:"
      4. どちらも駄目なら UNC を返す (呼び出し側で not found 扱い)
    """
    global _CACHE

    cached = _CACHE
    if cached is not None:
        return cached

    with _LOCK:
        if _CACHE is not None:
            return _CACHE

        explicit = str(os.getenv("AUTOSTOCK_NAS_ROOT", "")).strip()
        if explicit:
            _CACHE = _as_dir_root(explicit.rstrip("\\/"))
            logger.warning("[NAS ROOT] env 指定を使う root=%s", _CACHE)
            return _CACHE

        if _probe(UNC_ROOT):
            _CACHE = UNC_ROOT
            return _CACHE

        fallback = str(
            os.getenv("AUTOSTOCK_NAS_ROOT_FALLBACK", DEFAULT_FALLBACK)
        ).strip().rstrip("\\/")

        if fallback and _probe(fallback + "\\"):
            _CACHE = _as_dir_root(fallback)
            logger.warning(
                "[NAS ROOT] UNC %s へ到達できないため %s を使う "
                "(同一共有への二重 SMB セッションを避ける。"
                "AUTOSTOCK_NAS_ROOT で明示指定できる)",
                UNC_ROOT, fallback,
            )
            return _CACHE

        _CACHE = UNC_ROOT
        logger.error(
            "[NAS ROOT] NAS root へ到達できない unc=%s fallback=%s "
            "(NAS 上の DB は読めない見込み)",
            UNC_ROOT, fallback,
        )
        return _CACHE


def nas_path(*parts: str) -> str:
    r"""NAS root からの相対パスを連結する。

    nas_path(r"raw_data\kabu_station\ranking") など。
    """
    root = nas_root()
    tail = "\\".join(str(p).strip("\\/") for p in parts if str(p).strip("\\/"))
    return (root.rstrip("\\/") + "\\" + tail) if tail else root


def reset_cache_for_test() -> None:
    global _CACHE
    with _LOCK:
        _CACHE = None


__all__ = ["nas_root", "nas_path", "reset_cache_for_test", "UNC_ROOT", "DEFAULT_FALLBACK"]
