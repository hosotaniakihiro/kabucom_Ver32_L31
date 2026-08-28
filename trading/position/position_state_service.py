# ============================================================
# File   : trading/position/position_state_service.py
# Version: V1.0-POSITION-STATE-SERVICE
# ------------------------------------------------------------
# positions の read owner を 1 本にする。
#
# 背景 (2026-08-27 のログ):
#   - positions.db を毎秒読む経路が 6 系統あった
#       trading.position.open_position_sync
#       core.position_sync
#       kabu_api.positions
#       trading.exit.executor (_exit_closing_reconcile_loop / _exit_fill_confirm_loop)
#       entry precheck
#       scheduler (exit_loop_5s)
#     engine は NullPool なので 1 クエリ = NAS への新規接続 1 本。
#   - NAS(SMB) のオープン上限に当たると
#       sqlite3.OperationalError: unable to open database file
#       sqlite3.OperationalError: disk I/O error
#     が同時多発し、15:21:44 以降は復旧しなかった。
#   - どの経路も失敗時に {} / [] を返していたので、
#     「DB が読めない」と「建玉が無い」が区別できていなかった。
#
# このモジュールの責務:
#   1. positions.db の読み取りを singleflight + TTL キャッシュで 1 本化する
#   2. DB 障害時に空を返さない (last-known を保持する)
#   3. broker(kabu Station) を authoritative source として使う
#   4. DB 障害中は指数バックオフして NAS へのリトライストームを止める
#   5. POSITIONS_STATE ログで db_ok / broker_ok / source_used /
#      last_good_age / entry_allowed を必ず出す
#
# 安全規則 (絶対):
#   - DB read failure != positions empty
#   - position unknown で ENTRY を許可しない (fail-closed)
#   - Exit は last-known position state を保持し、勝手に 0 件にしない
# ============================================================

from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


# ============================================================
# ENV
# ============================================================

def _env_float(name: str, default: float) -> float:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return float(default)
        return float(str(v).strip())
    except Exception:
        return float(default)


def _env_bool(name: str, default: bool) -> bool:
    try:
        v = os.getenv(name)
        if v is None or str(v).strip() == "":
            return bool(default)
        return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}
    except Exception:
        return bool(default)


def _cache_ttl() -> float:
    """このTTLの間は NAS を読まずメモリキャッシュを返す。"""
    return max(0.2, _env_float("POSITION_STATE_CACHE_TTL_SEC", 1.0))


def _backoff_schedule() -> Tuple[float, ...]:
    """DB 障害時の再試行間隔 (bounded exponential backoff)。"""
    raw = os.getenv("POSITION_STATE_DB_BACKOFF_SEC", "1,2,5,10,30")
    out = []
    for part in str(raw).split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(max(0.5, float(part)))
        except Exception:
            continue
    return tuple(out) if out else (1.0, 2.0, 5.0, 10.0, 30.0)


def _entry_state_max_age() -> float:
    """ENTRY 判定に使ってよいスナップショットの最大齢。

    TTL キャッシュは EXIT / 表示には十分だが、ENTRY は「今この瞬間に
    建玉状態を確認できているか」で判断しなければならない。
    キャッシュがこの秒数より古ければ entry_allowed は False に落とす
    (負荷試験で、両系統ダウン直後の 1 TTL 分だけ ENTRY が
     許可されてしまう窓が実測 102 件あったため)。
    """
    return max(0.1, _env_float("POSITION_STATE_ENTRY_MAX_AGE_SEC", 1.0))


def _last_good_max_age() -> float:
    """last-good をこの秒数まで「使える建玉情報」として扱う。

    これを超えたら建玉状態は unknown。unknown では ENTRY を許可しない。
    Exit 側は unknown でも last-good を捨てない (0 件扱いしない)。
    """
    return max(30.0, _env_float("POSITION_STATE_LAST_GOOD_MAX_AGE_SEC", 300.0))


# ============================================================
# STATE
# ============================================================

SOURCE_DB = "db"
SOURCE_BROKER = "broker_authoritative"
SOURCE_DB_BROKER = "db+broker"
SOURCE_LAST_GOOD = "last_good"
SOURCE_UNKNOWN = "unknown"


@dataclass
class PositionsState:
    """建玉状態のスナップショット。consumer はこれだけを見る。"""

    positions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    source_used: str = SOURCE_UNKNOWN
    db_ok: bool = False
    broker_ok: bool = False
    db_error: str = ""
    broker_error: str = ""
    last_good_age: float = float("inf")
    entry_allowed: bool = False
    positions_known: bool = False
    at: dt.datetime = field(default_factory=dt.datetime.now)

    @property
    def count(self) -> int:
        return len(self.positions)

    @property
    def symbols(self) -> list:
        return sorted(self.positions.keys())

    def to_log_dict(self) -> dict:
        return {
            "db_ok": self.db_ok,
            "broker_ok": self.broker_ok,
            "source_used": self.source_used,
            "last_good_age": (
                None if self.last_good_age == float("inf") else round(self.last_good_age, 1)
            ),
            "entry_allowed": self.entry_allowed,
            "positions_known": self.positions_known,
            "count": self.count,
            "symbols": self.symbols,
            "db_error": self.db_error[:120],
            "broker_error": self.broker_error[:120],
        }


# ============================================================
# READERS
# ============================================================

def _read_db_positions_raising() -> Dict[str, Dict[str, Any]]:
    """positions.db の未クローズ建玉を読む。失敗は必ず例外で伝える。

    ここで例外を握り潰して {} を返してはならない。
    「読めなかった」と「0 件だった」を区別できなくなる。
    """
    from trading.position.open_position_sync import load_open_positions_from_db_strict

    return load_open_positions_from_db_strict()


def _read_broker_positions() -> Tuple[Dict[str, Dict[str, Any]], bool, str]:
    """kabu Station の信用建玉を読む。(positions, ok, error) を返す。

    DB が壊れていても broker の安全確認は必ず走らせる。
    """
    try:
        from trading.position.kabu_position_reader import (
            read_kabu_open_positions,
            get_last_read_status,
        )
    except Exception as e:  # noqa: BLE001
        return {}, False, f"import failed: {e}"

    try:
        rows = read_kabu_open_positions() or {}
    except Exception as e:  # noqa: BLE001
        logger.exception("[POSITIONS_STATE] broker read failed")
        return {}, False, str(e)

    status = {}
    try:
        status = get_last_read_status() or {}
    except Exception:
        logger.debug("[POSITIONS_STATE] broker status unavailable", exc_info=True)

    ok = bool(status.get("ok", False))
    err = str(status.get("error") or "")
    if not isinstance(rows, dict):
        return {}, False, f"unexpected broker payload type={type(rows).__name__}"

    out: Dict[str, Dict[str, Any]] = {}
    for key, value in rows.items():
        if not isinstance(value, dict):
            continue
        symbol = str(value.get("symbol") or value.get("Symbol") or key or "").strip()
        if symbol.endswith(".0"):
            symbol = symbol[:-2]
        if not symbol:
            continue
        pos = dict(value)
        pos["symbol"] = symbol
        pos.setdefault("_position_source", "BROKER.kabu.credit")
        out[symbol] = pos
    return out, ok, err


# ============================================================
# SERVICE
# ============================================================

class PositionStateService:
    """positions の唯一の read owner。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._inflight = threading.Lock()      # singleflight
        self._state: Optional[PositionsState] = None
        self._state_at: float = 0.0

        # last-good = 最後に「建玉状態を確実に知れた」時のスナップショット
        self._last_good: Dict[str, Dict[str, Any]] = {}
        self._last_good_at: float = 0.0
        self._last_good_source: str = ""

        # DB backoff
        self._db_fail_streak: int = 0
        self._db_next_try_at: float = 0.0
        self._db_last_error: str = ""

        # 診断カウンタ
        self._db_reads = 0
        self._db_errors = 0
        self._broker_reads = 0
        self._broker_errors = 0
        self._cache_hits = 0
        self._first_db_error_at: Optional[dt.datetime] = None
        self._last_db_error_at: Optional[dt.datetime] = None
        self._db_error_episodes = 0
        self._in_db_error_episode = False

    # --------------------------------------------------------
    # backoff
    # --------------------------------------------------------

    def _db_read_allowed(self, now: float) -> bool:
        return now >= self._db_next_try_at

    def _note_db_ok(self) -> None:
        self._db_reads += 1
        self._db_fail_streak = 0
        self._db_next_try_at = 0.0
        self._db_last_error = ""
        if self._in_db_error_episode:
            self._in_db_error_episode = False
            logger.warning(
                "[POSITIONS_STATE] positions DB recovered errors=%d episodes=%d",
                self._db_errors, self._db_error_episodes,
            )

    def _note_db_error(self, err: str, now: float) -> None:
        self._db_reads += 1
        self._db_errors += 1
        self._db_last_error = err
        self._last_db_error_at = dt.datetime.now()
        if self._first_db_error_at is None:
            self._first_db_error_at = self._last_db_error_at
        if not self._in_db_error_episode:
            self._in_db_error_episode = True
            self._db_error_episodes += 1

        schedule = _backoff_schedule()
        idx = min(self._db_fail_streak, len(schedule) - 1)
        wait = schedule[idx]
        self._db_fail_streak += 1
        self._db_next_try_at = now + wait
        logger.warning(
            "[POSITIONS_STATE] positions DB read failed streak=%d next_retry_in=%.1fs "
            "episode=%d total_errors=%d err=%s "
            "(即時リトライしない: NAS へのリトライストームを止める)",
            self._db_fail_streak, wait, self._db_error_episodes, self._db_errors, err[:160],
        )

    # --------------------------------------------------------
    # main entry
    # --------------------------------------------------------

    @staticmethod
    def _aged(state: PositionsState, age: float) -> PositionsState:
        """キャッシュから返す前に「齢」を反映させる。

        EXIT / 表示は TTL 内のキャッシュで十分だが、ENTRY だけは
        「今この瞬間に建玉状態を確認できているか」で判断する。
        齢が ENTRY 許容値を超えたスナップショットでは entry_allowed を落とす。
        """
        if age <= _entry_state_max_age():
            return state
        if not state.entry_allowed:
            return state
        aged = replace(
            state,
            entry_allowed=False,
            last_good_age=(
                state.last_good_age if state.last_good_age == float("inf")
                else state.last_good_age + age
            ),
        )
        logger.warning(
            "[POSITIONS_STATE] キャッシュが古いため ENTRY を許可しない "
            "state_age=%.2fs max=%.2fs source_used=%s count=%d",
            age, _entry_state_max_age(), state.source_used, state.count,
        )
        return aged

    def get_state(self, *, force: bool = False, caller: str = "") -> PositionsState:
        """建玉状態を返す。TTL 内は NAS を読まずキャッシュを返す。"""
        now = time.monotonic()

        if not force:
            with self._lock:
                st = self._state
                if st is not None and (now - self._state_at) < _cache_ttl():
                    self._cache_hits += 1
                    return self._aged(st, now - self._state_at)

        # singleflight: 同時に来た consumer は 1 本の実読み取りを共有する
        if not self._inflight.acquire(blocking=False):
            self._inflight.acquire()
            try:
                with self._lock:
                    if self._state is not None:
                        self._cache_hits += 1
                        return self._aged(self._state, time.monotonic() - self._state_at)
            finally:
                self._inflight.release()

        try:
            with self._lock:
                st = self._state
                if (not force) and st is not None and (time.monotonic() - self._state_at) < _cache_ttl():
                    self._cache_hits += 1
                    return self._aged(st, time.monotonic() - self._state_at)
            return self._refresh(caller=caller)
        finally:
            try:
                self._inflight.release()
            except RuntimeError:
                logger.debug("[POSITIONS_STATE] inflight release skipped", exc_info=True)

    def _refresh(self, *, caller: str = "") -> PositionsState:
        now = time.monotonic()

        # ---- DB ----
        db_positions: Dict[str, Dict[str, Any]] = {}
        db_ok = False
        db_error = ""
        if self._db_read_allowed(now):
            try:
                db_positions = _read_db_positions_raising() or {}
                db_ok = True
                self._note_db_ok()
            except Exception as e:  # noqa: BLE001
                db_error = f"{type(e).__name__}: {e}"
                self._note_db_error(db_error, now)
        else:
            db_error = f"backoff (retry in {max(0.0, self._db_next_try_at - now):.1f}s): {self._db_last_error}"

        # ---- broker ----
        # DB 障害でも broker の安全確認は必ず行う。
        broker_positions, broker_ok, broker_error = _read_broker_positions()
        self._broker_reads += 1
        if not broker_ok:
            self._broker_errors += 1

        state = self._decide(
            db_positions=db_positions,
            db_ok=db_ok,
            db_error=db_error,
            broker_positions=broker_positions,
            broker_ok=broker_ok,
            broker_error=broker_error,
        )

        with self._lock:
            self._state = state
            self._state_at = time.monotonic()

        self._log_state(state, caller=caller)
        return state

    # --------------------------------------------------------
    # decision table (P0-14 A/B/C/D)
    # --------------------------------------------------------

    def _decide(
        self,
        *,
        db_positions: Dict[str, Dict[str, Any]],
        db_ok: bool,
        db_error: str,
        broker_positions: Dict[str, Dict[str, Any]],
        broker_ok: bool,
        broker_error: str,
    ) -> PositionsState:
        now = time.monotonic()
        last_good_age = (
            float("inf") if self._last_good_at <= 0.0 else (now - self._last_good_at)
        )

        if db_ok and broker_ok:
            # A: 通常。broker を正、DB を補助情報としてマージする。
            merged = dict(db_positions)
            for symbol, pos in broker_positions.items():
                base = dict(merged.get(symbol) or {})
                base.update({k: v for k, v in pos.items() if v is not None and v != ""})
                merged[symbol] = base
            # broker が authoritative なので、broker に無い信用建玉は残さない。
            # ただし DB 側にしか無い行は「約定直後で broker 未反映」の可能性が
            # あるため、broker が 0 件でも DB 行は落とさずマージのまま残す。
            positions = merged
            source = SOURCE_DB_BROKER
            known = True

        elif (not db_ok) and broker_ok:
            # B / C: DB 障害。broker が読めているので broker を正にする。
            #   B: broker empty  -> 建玉なしと確定してよい
            #   C: broker に建玉 -> 保持して EXIT 対象にする
            positions = dict(broker_positions)
            source = SOURCE_BROKER
            known = True

        elif db_ok and (not broker_ok):
            # broker だけ NG。DB は読めているので DB を使う。
            positions = dict(db_positions)
            source = SOURCE_DB
            known = True

        else:
            # D: DB も broker も NG。建玉状態は unknown。
            #    last-known を保持し、勝手に 0 件にしない。
            positions = dict(self._last_good)
            source = SOURCE_LAST_GOOD if positions or self._last_good_at > 0 else SOURCE_UNKNOWN
            known = False

        if known:
            self._last_good = dict(positions)
            self._last_good_at = now
            self._last_good_source = source
            last_good_age = 0.0

        # ENTRY 可否: 建玉状態が確定している時だけ許可する (fail-closed)。
        entry_allowed = bool(known)
        if not known:
            entry_allowed = False

        return PositionsState(
            positions=positions,
            source_used=source,
            db_ok=db_ok,
            broker_ok=broker_ok,
            db_error=db_error,
            broker_error=broker_error,
            last_good_age=last_good_age,
            entry_allowed=entry_allowed,
            positions_known=known,
            at=dt.datetime.now(),
        )

    # --------------------------------------------------------
    # logging
    # --------------------------------------------------------

    def _log_state(self, state: PositionsState, *, caller: str = "") -> None:
        level = logger.info if (state.db_ok and state.broker_ok) else logger.warning
        level(
            "[POSITIONS_STATE] db_ok=%s broker_ok=%s source_used=%s last_good_age=%s "
            "entry_allowed=%s positions_known=%s count=%d symbols=%s caller=%s "
            "db_err=%s broker_err=%s",
            state.db_ok, state.broker_ok, state.source_used,
            "inf" if state.last_good_age == float("inf") else "%.1f" % state.last_good_age,
            state.entry_allowed, state.positions_known, state.count, state.symbols,
            caller or "-", state.db_error[:120] or "-", state.broker_error[:120] or "-",
        )

    # --------------------------------------------------------
    # diagnostics
    # --------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            return {
                "db_reads": self._db_reads,
                "db_errors": self._db_errors,
                "db_error_episodes": self._db_error_episodes,
                "db_fail_streak": self._db_fail_streak,
                "first_db_error_at": self._first_db_error_at,
                "last_db_error_at": self._last_db_error_at,
                "broker_reads": self._broker_reads,
                "broker_errors": self._broker_errors,
                "cache_hits": self._cache_hits,
                "last_good_count": len(self._last_good),
                "last_good_source": self._last_good_source,
                "cache_ttl_sec": _cache_ttl(),
            }

    def reset_for_test(self) -> None:
        with self._lock:
            self._state = None
            self._state_at = 0.0
            self._last_good = {}
            self._last_good_at = 0.0
            self._last_good_source = ""
            self._db_fail_streak = 0
            self._db_next_try_at = 0.0
            self._db_last_error = ""
            self._db_reads = 0
            self._db_errors = 0
            self._broker_reads = 0
            self._broker_errors = 0
            self._cache_hits = 0
            self._first_db_error_at = None
            self._last_db_error_at = None
            self._db_error_episodes = 0
            self._in_db_error_episode = False


# ============================================================
# MODULE-LEVEL API
# ============================================================

_service = PositionStateService()


def get_positions_state(*, force: bool = False, caller: str = "") -> PositionsState:
    """建玉状態のスナップショット。全 consumer はこれを読む。"""
    return _service.get_state(force=force, caller=caller)


def get_open_positions(*, caller: str = "") -> Dict[str, Dict[str, Any]]:
    """建玉 dict だけが要る consumer 向け。

    注意: これが空でも「建玉が無い」とは限らない。
    ENTRY 判定には必ず entry_allowed() / get_positions_state() を使うこと。
    """
    return dict(get_positions_state(caller=caller).positions)


def entry_allowed(*, caller: str = "") -> bool:
    """ENTRY を許可してよいか。建玉状態 unknown なら False (fail-closed)。

    ここは発注の可否そのものを決めるゲートなので、必ず今この瞬間の
    状態を取り直す (force=True)。TTL キャッシュを使うと、
    DB と broker が両方落ちた直後の TTL 1 回分だけ
    「障害前の状態」で ENTRY を許可してしまう窓ができる
    (負荷試験で実測 102 件)。

    このゲートは 1 エントリー判断につき数回しか呼ばれないので、
    force=True でも NAS への負荷にはならない
    (毎秒ポーリングする EXIT / 表示は get_positions_state() を使うこと)。
    """
    return bool(get_positions_state(force=True, caller=caller).entry_allowed)


def stats() -> dict:
    return _service.stats()


def reset_for_test() -> None:
    _service.reset_for_test()


__all__ = [
    "PositionsState",
    "PositionStateService",
    "get_positions_state",
    "get_open_positions",
    "entry_allowed",
    "stats",
    "reset_for_test",
    "SOURCE_DB",
    "SOURCE_BROKER",
    "SOURCE_DB_BROKER",
    "SOURCE_LAST_GOOD",
    "SOURCE_UNKNOWN",
]
