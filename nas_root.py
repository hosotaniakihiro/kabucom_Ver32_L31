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


# ============================================================
# 2026-10-24: 場中は NAS を使わない (ローカルのデータ root)
# ------------------------------------------------------------
# NAS の SMB が劣化すると (Errno 24 / Mup 139) summary DB を書けなくなり
# 発注が 0 になる (10/23 13:00〜13:45)。summary だけでなく Basic・daily_db・
# yahoo status・trade_guard なども nas_root() 基準で約 100 箇所が組み立てて
# いるので、個別に直さず root そのものをローカルの写しへ向ける。
#
#   - ローカル root は NAS と同じ並び (raw_data\kabu_station\summary ...)。
#     push / ranking / positions / order_book はジャンクションで既存の
#     ローカル置き場 (%LOCALAPPDATA%\autostock\push 等) を指す。
#   - root に READY_MARKER があるときだけ使う。写しが終わる前や、
#     マーカーを消したときは従来どおり NAS を返す (戻し方 = マーカー削除)。
#   - NAS への複製は ops/local_run/local_data_sync.py と各 *_db_location の
#     sync_to_nas() が real_nas_root() / to_real_nas() で行う。
#
# ENV:
#   AUTOSTOCK_LOCAL_DATA        0 で無効 (従来どおり NAS)。既定 1
#   AUTOSTOCK_LOCAL_DATA_ROOT   ローカル root (既定 %LOCALAPPDATA%\autostock\nas_local)
# ============================================================
READY_MARKER = "_LOCAL_DATA_READY.txt"

_LOCAL_CACHE: Optional[str] = None
_LOCAL_DECIDED = False


def default_local_data_root() -> str:
    explicit = str(os.getenv("AUTOSTOCK_LOCAL_DATA_ROOT", "")).strip()
    if explicit:
        return explicit.rstrip("\\/")
    base = os.getenv("LOCALAPPDATA") or os.getenv("TEMP") or r"C:\Temp"
    return os.path.join(base, "autostock", "nas_local")


def local_data_root() -> Optional[str]:
    """使えるローカル root を返す。無効・未準備なら None (プロセス内で 1 回だけ判定)。"""
    global _LOCAL_CACHE, _LOCAL_DECIDED
    if _LOCAL_DECIDED:
        return _LOCAL_CACHE
    with _LOCK:
        if _LOCAL_DECIDED:
            return _LOCAL_CACHE
        flag = str(os.getenv("AUTOSTOCK_LOCAL_DATA", "1")).strip().lower()
        root = default_local_data_root()
        if flag in {"0", "false", "no", "off"}:
            _LOCAL_CACHE = None
        elif _probe(os.path.join(root, READY_MARKER)):
            _LOCAL_CACHE = root
            logger.warning("[NAS ROOT] ローカルのデータ root を使う root=%s", root)
        else:
            _LOCAL_CACHE = None
            logger.warning("[NAS ROOT] ローカルのデータ root が未準備 (%s なし)。NAS を使う", os.path.join(root, READY_MARKER))
        _LOCAL_DECIDED = True
        return _LOCAL_CACHE


def nas_root() -> str:
    """データの root。ローカルの写しが準備済みならそれ、無ければ NAS (real_nas_root)。"""
    local = local_data_root()
    if local:
        return local
    return real_nas_root()


def to_real_nas(path) -> str:
    """ローカル root 配下のパスを、NAS 上の同じ位置へ読み替える。それ以外はそのまま返す。

    *_db_location の「NAS 側の置き場所」は config.paths / nas_path() から作るので、
    ローカル root が有効だとローカルを指してしまう (複製が自分自身へ上書きになる)。
    """
    s = str(path)
    local = local_data_root()
    if not local:
        return s
    lr = local.rstrip("\\/")
    if s.lower() == lr.lower() or s.lower().startswith(lr.lower() + "\\") or s.lower().startswith(lr.lower() + "/"):
        tail = s[len(lr):].strip("\\/")
        return real_nas_path(tail)
    return s


def real_nas_path(*parts: str) -> str:
    """NAS (real_nas_root) からの相対パス。ローカル root の有無に関係なく NAS を指す。"""
    root = real_nas_root()
    tail = "\\".join(str(p).strip("\\/") for p in parts if str(p).strip("\\/"))
    return (root.rstrip("\\/") + "\\" + tail) if tail else root


def real_nas_root() -> str:
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
    global _CACHE, _LOCAL_CACHE, _LOCAL_DECIDED
    with _LOCK:
        _CACHE = None
        _LOCAL_CACHE = None
        _LOCAL_DECIDED = False


__all__ = [
    "nas_root", "nas_path", "reset_cache_for_test", "UNC_ROOT", "DEFAULT_FALLBACK",
    "real_nas_root", "real_nas_path", "to_real_nas", "local_data_root", "default_local_data_root", "READY_MARKER",
]
