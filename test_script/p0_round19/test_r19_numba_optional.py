# -*- coding: utf-8 -*-
"""
第19ラウンド(追加): numba を任意依存にして起動不能を防ぐ (実注文なし)

2026-09-02 08:42:55〜08:48 の起動失敗:
    main.py:116
      from trading.handlers.exit_handler import build_5s_bar_fast, run_exit_pipeline
        -> trading/handlers/exit_handler.py  from numba import njit
          -> ImportError: DLL load failed while importing _devicearray:
             アプリケーション制御ポリシーによってこのファイルがブロックされました。
    sitecustomize 経由の先行 import でも同じ例外。その後 main.py の
    top-level import で未処理になり終了コード 1。

judgement/計算に numba を使うのは exit_handler.calc_ohlc(prices) の 1 箇所だけで、
中身は prices[0] / max / min / prices[-1]。JIT が無くても結果は変わらないので、
core/compat/numba_compat.py が identity decorator へフォールバックする。

--- このテストの隔離方針 (重要) ---
trading.handlers.exit_handler の import は **副作用が無いわけではない**。
モジュール本体は

    from global_state import global_data
    from database import Session_position
    from database.models import Position, TradeHistory
    from kabu_api.close import process_exit
    from trading.entry.tonosama_logger import log_tonosama_trade   (try/except)

を読み込み、これらは実 DB セッション生成・ネットワークドライブ上の
DB/設定アクセス・kabu API クライアントの初期化を伴う。
実際、素朴に import しただけのテストは 30 秒以上終了しなかった。

そこでこのテストでは、exit_handler を import する前に上記 5 モジュールを
軽量な ModuleType モックへ差し替える。さらに sys.meta_path へガードを挿し、
実依存 (database / kabu_api / sqlalchemy / global_state / main /
token_manager / main_database) を本当に読みに行った瞬間に **失敗する**
ようにしている (握り潰して実物へフォールバックしない)。
trading パッケージ本体は exit_handler を import するために本物を使い、
trading.entry / trading.entry.tonosama_logger だけを差し替える。

後始末は try/finally で必ず行う:
  - 差し替えた sys.modules を元へ戻す (numba のエントリも含む)
  - 親パッケージに残った属性 (trading.handlers.exit_handler など) も戻す。
    exit_handler の module globals にはモックの global_data / process_exit が
    入っているので、放置すると後続の import が汚染される。
  - logger の level / handler を元へ戻す

WARNING の検証は numba_compat 側の logger 名に依存しないよう、root logger
にも capture handler を付け、root level を一時的に WARNING へ下げて行う
(伝播で同じ record が二重に届くので handler 側で重複を弾く)。

検証する不変条件:
  1. numba の import が失敗しても exit_handler は import でき、起動が落ちない
  2. calc_ohlc の戻り値が JIT 有無で一致する
  3. njit が `@njit` / `@njit(...)` の両方の書き方で使える
  4. フォールバックの WARNING は import ごとに 1 回だけ / traceback を出さない
  5. numba を戻せば元の状態へ復帰する

main.py は実売買につながるため一切起動しない。
実 DB / 実 API / 実注文 / 取消 / 返済 / settings.ini には触れない。
"""
from __future__ import annotations

import importlib
import logging
import os
import sys
from types import ModuleType

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import numpy as np  # noqa: E402

PASS = 0
FAIL = 0


def check(label, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  OK   {label} {detail}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {detail}")


def section(title):
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# ============================================================
# 実依存へ落ちたら失敗させるガード
# ============================================================

# 先頭要素でブロックする (実 DB / 実 API / 実トークン / 起動処理)
_BLOCKED_ROOTS = {
    "database",
    "kabu_api",
    "sqlalchemy",
    "global_state",
    "main",
    "main_database",
    "token_manager",
}
# 完全一致でブロックする (trading 配下は exit_handler 本体を通したいので個別指定)
_BLOCKED_EXACT = {
    "trading.entry",
    "trading.entry.tonosama_logger",
    "trading.board",
    "trading.board.board_signal",
    "trading.board.board_state",
    "core.global_context",
    "core.global_context.context",
}


class _IsolationGuard:
    """モックが漏れて実依存を読みに行ったら ImportError で落とす。

    sys.modules にモックが入っていれば import 機構はそちらを使い、
    この finder は呼ばれない。呼ばれた = 隔離が破れている。
    """

    def __init__(self) -> None:
        self.violations: list[str] = []

    def find_spec(self, fullname, path=None, target=None):
        root = str(fullname).split(".")[0]
        if root in _BLOCKED_ROOTS or fullname in _BLOCKED_EXACT:
            self.violations.append(fullname)
            raise ImportError(
                f"隔離違反: 実依存 {fullname} を import しようとしました "
                "(このテストはモックだけで動く必要があります)"
            )
        return None


# ============================================================
# 軽量モック
# ============================================================

def _never_called(name):
    def _f(*args, **kwargs):
        raise AssertionError(f"テスト中に {name} が呼ばれました (実発注/実DB経路)")
    return _f


def _build_mocks() -> dict[str, ModuleType]:
    """exit_handler の import に必要な属性だけを正確に用意する。

    必要な属性は trading/handlers/exit_handler.py の top-level import から:
        global_state.global_data
        database.Session_position
        database.models.Position / TradeHistory
        kabu_api.close.process_exit
        trading.entry.tonosama_logger.log_tonosama_trade
    build_5s_bar_fast() は global_data.get_push_df() だけを使う。
    """
    mods: dict[str, ModuleType] = {}

    # ---- global_state ----
    global_state = ModuleType("global_state")

    class _GlobalDataStub:
        def get_push_df(self):
            # push データ無し。build_5s_bar_fast は None を返して終わる。
            return None

    global_state.global_data = _GlobalDataStub()
    mods["global_state"] = global_state

    # ---- database / database.models ----
    database = ModuleType("database")
    database.__path__ = []  # パッケージとして扱わせる
    database.Session_position = _never_called("database.Session_position")

    database_models = ModuleType("database.models")

    class Position:  # noqa: D401 - ダミー
        """Position のダミー (型注釈は from __future__ import annotations で文字列)。"""

    class TradeHistory:  # noqa: D401 - ダミー
        """TradeHistory のダミー。"""

    database_models.Position = Position
    database_models.TradeHistory = TradeHistory
    database.models = database_models

    mods["database"] = database
    mods["database.models"] = database_models

    # ---- kabu_api.close ----
    kabu_api = ModuleType("kabu_api")
    kabu_api.__path__ = []
    kabu_api_close = ModuleType("kabu_api.close")
    # 実発注/実返済が万一呼ばれたら即失敗させる
    kabu_api_close.process_exit = _never_called("kabu_api.close.process_exit")
    kabu_api.close = kabu_api_close

    mods["kabu_api"] = kabu_api
    mods["kabu_api.close"] = kabu_api_close

    # ---- trading.entry.tonosama_logger ----
    # trading / trading.handlers は本物を使う (テスト対象がその下にあるため)。
    trading_entry = ModuleType("trading.entry")
    trading_entry.__path__ = []
    tonosama_logger = ModuleType("trading.entry.tonosama_logger")
    tonosama_logger.log_tonosama_trade = lambda *a, **k: None
    trading_entry.tonosama_logger = tonosama_logger

    mods["trading.entry"] = trading_entry
    mods["trading.entry.tonosama_logger"] = tonosama_logger

    return mods


class _CaptureHandler(logging.Handler):
    """複数の logger に付けても同じ record を二重に数えない capture handler。

    numba_compat 側の logger 名を決め打ちできないので root にも付ける。
    伝播により同一 record が複数回 emit されるため id() で重複を弾く。
    id が再利用されないよう record 自身への参照を保持しておく。
    """

    def __init__(self):
        super().__init__()
        self.records = []
        self._seen = {}

    def emit(self, record):
        if id(record) in self._seen:
            return
        self._seen[id(record)] = record
        try:
            self.records.append((record.levelname, record.getMessage()))
        except Exception:
            self.records.append((record.levelname, str(record.msg)))

    def clear(self):
        self.records = []
        self._seen = {}

    def messages(self, level=None):
        return [m for lvl, m in self.records if level is None or lvl == level]


# ============================================================
SAMPLES = [
    np.array([100.0], dtype=float),
    np.array([100.0, 101.5, 99.25, 100.75], dtype=float),
    np.array([2500.0, 2500.0, 2500.0], dtype=float),
    np.array([1.0, -2.0, 3.5, 0.0, -0.5], dtype=float),
    np.arange(0.0, 50.0, 0.5, dtype=float),
]


def reference_ohlc(prices):
    """calc_ohlc の期待値 (素の numpy)。"""
    return prices[0], prices.max(), prices.min(), prices[-1]


def _as_floats(t):
    return tuple(float(x) for x in t)


def _snapshot(keys):
    return {k: sys.modules[k] for k in keys if k in sys.modules}


# ============================================================
def run() -> None:
    mocks = _build_mocks()
    mock_keys = list(mocks.keys())

    # exit_handler / numba_compat 自身も、テスト後に元へ戻せるよう退避する
    target_keys = [
        "trading.handlers.exit_handler",
        "core.compat.numba_compat",
    ]
    numba_keys = [k for k in list(sys.modules) if k == "numba" or k.startswith("numba.")]

    saved_all = _snapshot(mock_keys + target_keys + numba_keys + ["numba"])
    guard = _IsolationGuard()

    handler = _CaptureHandler()
    eh_logger = logging.getLogger("exit_ultra")
    compat_logger = logging.getLogger("core.compat.numba_compat")
    root_logger = logging.getLogger()
    # numba_compat の logger 名に依存しないよう root でも拾う。
    # root level が CRITICAL のままだと logger 側で WARNING が捨てられるので
    # 一時的に WARNING まで下げる (finally で戻す)。
    capture_loggers = [eh_logger, compat_logger, root_logger]
    prev_levels = [lg.level for lg in capture_loggers]

    try:
        # ---- 隔離環境を作る ----
        sys.modules.update(mocks)
        for k in target_keys:
            sys.modules.pop(k, None)
        sys.meta_path.insert(0, guard)

        for lg in capture_loggers:
            lg.addHandler(handler)
        eh_logger.setLevel(logging.DEBUG)
        compat_logger.setLevel(logging.DEBUG)
        root_logger.setLevel(logging.WARNING)

        # ============================================================
        section("[1] 隔離した状態で exit_handler が import できる")

        import trading.handlers.exit_handler as eh

        check("実依存へ落ちていない", not guard.violations, str(guard.violations))
        check("global_state はモック",
              sys.modules["global_state"] is mocks["global_state"])
        check("database / kabu_api もモック",
              sys.modules["database"] is mocks["database"]
              and sys.modules["kabu_api.close"] is mocks["kabu_api.close"])
        check("njit が定義されている", callable(getattr(eh, "njit", None)))
        check("NUMBA_AVAILABLE が bool", isinstance(eh.NUMBA_AVAILABLE, bool),
              f"NUMBA_AVAILABLE={eh.NUMBA_AVAILABLE}")
        check("calc_ohlc が呼べる", callable(eh.calc_ohlc))
        check("build_5s_bar_fast が呼べる", callable(eh.build_5s_bar_fast))
        check("run_exit_pipeline がある (main.py の import 対象)",
              callable(getattr(eh, "run_exit_pipeline", None)))

        compat_mod = sys.modules.get("core.compat.numba_compat")
        check("core.compat.numba_compat が読み込まれている (numba 判定の唯一の窓口)",
              compat_mod is not None)
        if compat_mod is None:
            raise AssertionError(
                "core.compat.numba_compat を import できていません。"
                "この状態では numba フォールバックの検証ができません。"
            )

        baseline_available = eh.NUMBA_AVAILABLE
        print(f"  (この環境の NUMBA_AVAILABLE = {baseline_available})")

        baseline = []
        for prices in SAMPLES:
            got = _as_floats(eh.calc_ohlc(prices))
            want = _as_floats(reference_ohlc(prices))
            baseline.append(got)
            check(f"calc_ohlc が期待値と一致 len={len(prices)}", got == want,
                  f"got={got} want={want}")

        # ============================================================
        section("[2] numba の import 失敗 (DLL拒否) を模擬しても動く")

        handler.clear()

        # sys.modules に None を入れると `import numba` は ImportError になる。
        for k in [k for k in list(sys.modules) if k == "numba" or k.startswith("numba.")]:
            del sys.modules[k]
        sys.modules["numba"] = None

        compat_nojit = importlib.reload(compat_mod)
        eh_nojit = importlib.reload(eh)

        check("実依存へ落ちていない (reload 後も)", not guard.violations, str(guard.violations))
        check("numba_compat: NUMBA_AVAILABLE=False",
              compat_nojit.NUMBA_AVAILABLE is False,
              f"={compat_nojit.NUMBA_AVAILABLE}")
        check("numba_compat: import エラーを記録している",
              bool(compat_nojit.NUMBA_IMPORT_ERROR),
              compat_nojit.NUMBA_IMPORT_ERROR[:120])
        check("exit_handler: NUMBA_AVAILABLE=False",
              eh_nojit.NUMBA_AVAILABLE is False,
              f"={eh_nojit.NUMBA_AVAILABLE}")
        check("calc_ohlc が呼べる", callable(eh_nojit.calc_ohlc))
        check("build_5s_bar_fast が呼べる", callable(eh_nojit.build_5s_bar_fast))
        check("run_exit_pipeline も残っている",
              callable(getattr(eh_nojit, "run_exit_pipeline", None)))

        # ---- 戻り値が JIT 有無で一致する ----
        for i, prices in enumerate(SAMPLES):
            got = _as_floats(eh_nojit.calc_ohlc(prices))
            want = _as_floats(reference_ohlc(prices))
            check(f"JIT なしでも期待値と一致 len={len(prices)}", got == want,
                  f"got={got} want={want}")
            check(f"JIT あり/なしで戻り値が同じ len={len(prices)}", got == baseline[i],
                  f"nojit={got} baseline={baseline[i]}")

        # ---- identity decorator の使用形 ----
        def _plain(x):
            return x * 2

        check("@njit 形 (引数なし) は関数をそのまま返す",
              eh_nojit.njit(_plain) is _plain)
        check("@njit(...) 形も同じ関数を返す",
              eh_nojit.njit(cache=True, fastmath=True)(_plain) is _plain)
        check("フォールバックした関数が普通に動く", eh_nojit.njit(_plain)(21) == 42)

        # ---- build_5s_bar_fast が安全に None を返す ----
        try:
            res = eh_nojit.build_5s_bar_fast("7203")
            check("build_5s_bar_fast が例外を投げない", True, f"result={res!r}")
        except Exception as e:  # noqa: BLE001
            check("build_5s_bar_fast が例外を投げない", False, f"{type(e).__name__}: {e}")

        # ---- WARNING は各 1 回だけ / traceback を出さない ----
        warns = handler.messages("WARNING")
        eh_warns = [m for m in warns if "[EXIT HANDLER]" in m and "JIT なし" in m]
        compat_warns = [m for m in warns if "[NUMBA COMPAT]" in m]
        check("exit_handler のフォールバック WARNING が 1 回だけ",
              len(eh_warns) == 1, f"count={len(eh_warns)}")
        check("numba_compat のフォールバック WARNING が 1 回だけ",
              len(compat_warns) == 1, f"count={len(compat_warns)}")
        if compat_warns:
            check("WARNING に reason= が入る", "reason=" in compat_warns[0],
                  compat_warns[0][:160])
        tracebacks = [m for _l, m in handler.records
                      if "Traceback (most recent call last)" in m]
        check("traceback を出していない", not tracebacks, f"count={len(tracebacks)}")

        # ---- log_status_once() は 2 回目を出さない ----
        handler.clear()
        compat_nojit.log_status_once()
        check("log_status_once() は 2 回目を出さない",
              not handler.messages("WARNING"), str(handler.messages("WARNING")))

        # ============================================================
        section("[3] numba を戻せば元の状態へ復帰する")

        del sys.modules["numba"]
        for k in numba_keys:
            if k in saved_all:
                sys.modules[k] = saved_all[k]

        compat_back = importlib.reload(compat_mod)
        eh_back = importlib.reload(eh)

        check("実依存へ落ちていない (復帰後も)", not guard.violations, str(guard.violations))
        check("復帰後の NUMBA_AVAILABLE が最初と同じ",
              eh_back.NUMBA_AVAILABLE == baseline_available,
              f"now={eh_back.NUMBA_AVAILABLE} first={baseline_available} "
              f"compat={compat_back.NUMBA_AVAILABLE}")
        check("復帰後も calc_ohlc が期待値どおり",
              _as_floats(eh_back.calc_ohlc(SAMPLES[1]))
              == _as_floats(reference_ohlc(SAMPLES[1])))

    finally:
        # ---- 差し替えた sys.modules を必ず元へ戻す ----
        try:
            sys.meta_path.remove(guard)
        except ValueError:
            pass
        for lg, lvl in zip(capture_loggers, prev_levels):
            lg.removeHandler(handler)
            lg.setLevel(lvl)

        # 1) `sys.modules["numba"] = None` を絶対に残さない。
        #    残すと以後このプロセスのあらゆる numba import が壊れる。
        if sys.modules.get("numba", "__absent__") is None:
            del sys.modules["numba"]

        # 2) モックと、モックを掴んだままのモジュールを取り除く。
        #    exit_handler の module globals にはモックの global_data /
        #    process_exit が入っているので、そのまま残すと本物の実行を汚す。
        touched = {k: sys.modules.get(k) for k in mock_keys + target_keys}
        for k in mock_keys + target_keys:
            if k not in saved_all:
                sys.modules.pop(k, None)

        # 3) 元々あったものを戻す (退避した numba のエントリもここで戻る)。
        for k, v in saved_all.items():
            sys.modules[k] = v

        # 4) 親パッケージ側に残った属性も戻す。
        #    sys.modules から消しても `from trading.handlers import exit_handler`
        #    は親の属性を先に見るため、モックを掴んだ古いモジュールが再利用される。
        #    テストが読み込んだ実体と一致する時だけ触る (本物には手を出さない)。
        for full in mock_keys + target_keys:
            parent_name, _, attr = full.rpartition(".")
            if not parent_name:
                continue
            parent = sys.modules.get(parent_name)
            if parent is None:
                continue
            if full in saved_all:
                try:
                    setattr(parent, attr, saved_all[full])
                except Exception:
                    pass
                continue
            obj = touched.get(full)
            if obj is not None and getattr(parent, attr, None) is obj:
                try:
                    delattr(parent, attr)
                except Exception:
                    pass


# ============================================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)

    try:
        run()
    except Exception as e:  # noqa: BLE001
        # 隔離違反 (実依存 import) もここに落ちる。握り潰さず必ず FAIL にする。
        FAIL += 1
        print()
        print(f"  FAIL テストが例外で中断: {type(e).__name__}: {e}")

    print()
    print("=" * 70)
    print(f"PASS={PASS} FAIL={FAIL}")
    print("=" * 70)
    sys.exit(1 if FAIL else 0)
