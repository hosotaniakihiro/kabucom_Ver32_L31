# ============================================================
# File   : trading/exit/tonosama_exit_policy.py
# Version: PRODUCTION-STABLE-REV3.0-5SEC-SAFE-EXIT
# Purpose:
#   殿様イナゴ用 EXIT 判定
#
# 方針:
#   - 損切りはAIに任せない
#   - 利確は分割
#   - VWAP割れ / 高値からの失速 / ランキング脱落で逃げる
#   - 5秒足が使える場合は短期失速検知として追加する
#
# 重要:
#   - 5秒足は必須ではない
#   - None の場合は従来ロジックだけで判定する
#   - 急落系5秒足シグナルは最低保有秒数より優先して逃げる
#   - ただし微細ノイズで即全決済しすぎないよう保護条件を入れる
#
# 想定呼び出し元:
#   trading/exit/exit_loop.py
#
# judge_tonosama_exit() に渡す optional 5秒足引数:
#   bar5s_drop_pct
#   bar5s_consecutive_down
#   bar5s_volume_ratio
#   bar5s_vwap_break
#   bar5s_high_after_entry
# ============================================================

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Any


PCT_EPS = 1e-9


# ============================================================
# dataclasses
# ============================================================

@dataclass
class TonosamaExitConfig:
    # --------------------------------------------------------
    # 基本EXIT
    # --------------------------------------------------------
    stop_loss_pct: float = -0.8
    first_take_profit_pct: float = 2.0
    second_take_profit_pct: float = 4.0

    # --------------------------------------------------------
    # 高値からの失速
    # current_price が high_after_entry から何%落ちたか
    # 例: -0.5 = 高値から0.5%下落
    # --------------------------------------------------------
    trailing_drop_pct: float = -0.5
    hard_trailing_drop_pct: float = -0.5

    # 分割利確後は残玉を守るため、少し厳しめにトレールする
    after_first_tp_trailing_drop_pct: float = -0.80
    after_second_tp_trailing_drop_pct: float = -0.60

    # --------------------------------------------------------
    # VWAP / ランキング
    # --------------------------------------------------------
    vwap_break_exit: bool = True
    ranking_lost_exit_minutes: int = 5

    # ranking_lost で逃げる最大含み益
    # これ以上利益がある場合は利確/トレール側で判定する
    ranking_lost_exit_max_pnl_pct: float = 1.0

    # --------------------------------------------------------
    # 最低保有時間
    # ただし stop_loss / 5秒足急落系は即時EXIT許可
    # --------------------------------------------------------
    min_hold_seconds: int = 30

    # --------------------------------------------------------
    # 5秒足 optional EXIT
    # --------------------------------------------------------
    use_5sec_exit: bool = True

    # 直近5秒足の下落率がこれ以下なら即逃げ候補
    # 例: -0.40 = 直近5秒足で -0.40%
    bar5s_hard_drop_pct: float = -0.40

    # 5秒足の連続陰線数
    bar5s_consecutive_down_exit: int = 2

    # 5秒足VWAP割れ + 陰線連続で逃げる
    bar5s_vwap_break_exit: bool = True

    # 5秒足VWAP割れ単独で逃げるか
    # 本番では False 推奨。陰線連続などと組み合わせる。
    bar5s_vwap_break_single_exit: bool = False

    # 出来高急減判定
    # 例: 0.35 = 直近5秒足出来高が平均の35%以下
    bar5s_volume_dryup_ratio: float = 0.35

    # 出来高急減だけでは逃げず、陰線連続やVWAP割れと組み合わせる
    bar5s_volume_dryup_exit: bool = True

    # エントリー後高値からの5秒足短期ドローダウン
    # 例: -0.80 = 高値から0.8%落ちた
    bar5s_drop_from_high_exit_pct: float = -0.80

    # 5秒足の高値失速判定を使う最低含み益
    # これ未満では小さなブレで逃げすぎるため判定しない
    bar5s_drop_from_high_min_pnl_pct: float = 0.30

    # 5秒足VWAP割れ + 陰線連続で逃げるときの最大含み損益条件
    # 例: 1.50 なら +1.5%以下は逃げる。大きな利益は利確/トレールに任せる。
    bar5s_vwap_break_exit_max_pnl_pct: float = 1.50

    # 5秒足出来高急減 + 陰線連続で逃げるときの最大含み損益条件
    bar5s_volume_dryup_exit_max_pnl_pct: float = 1.50


@dataclass
class TonosamaExitDecision:
    should_exit: bool
    action: str
    reason: str
    sell_ratio: float = 0.0
    pnl_pct: float = 0.0


DEFAULT_TONOSAMA_EXIT_CONFIG = TonosamaExitConfig()


# ============================================================
# helpers
# ============================================================

def calc_pct(current_price: float, base_price: float) -> float:
    try:
        current_price = float(current_price)
        base_price = float(base_price)
        if base_price <= 0:
            return 0.0
        return (current_price / base_price - 1.0) * 100.0
    except Exception:
        return 0.0


def _is_valid_number(value: Any) -> bool:
    try:
        if value is None:
            return False
        v = float(value)
        return v == v
    except Exception:
        return False


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        v = float(value)
        if v != v:
            return default
        return v
    except Exception:
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        if isinstance(value, bool):
            return int(value)
        return int(float(value))
    except Exception:
        return default


def _to_bool_or_none(value: Any) -> Optional[bool]:
    try:
        if value is None:
            return None
        if isinstance(value, bool):
            return value

        s = str(value).strip().lower()

        if s in {"1", "true", "yes", "on", "y", "ok", "break", "broken", "below"}:
            return True

        if s in {"0", "false", "no", "off", "n", "ng", "", "none", "above"}:
            return False

        return None
    except Exception:
        return None


def _hold(
    *,
    reason: str,
    pnl_pct: float,
) -> TonosamaExitDecision:
    return TonosamaExitDecision(
        should_exit=False,
        action="HOLD",
        reason=reason,
        sell_ratio=0.0,
        pnl_pct=pnl_pct,
    )


def _exit_all(
    *,
    reason: str,
    pnl_pct: float,
) -> TonosamaExitDecision:
    return TonosamaExitDecision(
        should_exit=True,
        action="EXIT_ALL",
        reason=reason,
        sell_ratio=1.0,
        pnl_pct=pnl_pct,
    )


def _take_profit(
    *,
    action: str,
    sell_ratio: float,
    reason: str,
    pnl_pct: float,
) -> TonosamaExitDecision:
    return TonosamaExitDecision(
        should_exit=True,
        action=action,
        reason=reason,
        sell_ratio=sell_ratio,
        pnl_pct=pnl_pct,
    )


# ============================================================
# 5sec policy
# ============================================================

def _judge_5sec_exit(
    *,
    current_price: float,
    high_after_entry: Optional[float],
    pnl_pct: float,
    bar5s_drop_pct: Optional[float],
    bar5s_consecutive_down: Optional[int],
    bar5s_volume_ratio: Optional[float],
    bar5s_vwap_break: Optional[bool],
    bar5s_high_after_entry: Optional[float],
    already_first_tp: bool,
    already_second_tp: bool,
    config: TonosamaExitConfig,
) -> Optional[TonosamaExitDecision]:
    """
    5秒足による短期失速EXIT判定。

    引数の意味:
      bar5s_drop_pct:
        直近5秒足の騰落率。
        例: -0.75 は直近5秒足で -0.75%

      bar5s_consecutive_down:
        直近5秒足の連続陰線数。
        例: 2 なら2本連続陰線。

      bar5s_volume_ratio:
        直近5秒足出来高 / 直近平均出来高。
        例: 0.30 なら平均の30%まで出来高が減った。

      bar5s_vwap_break:
        5秒足ベースでVWAPを割ったか。

      bar5s_high_after_entry:
        5秒足ベースで見たエントリー後高値。
        未指定なら high_after_entry を使う。
    """

    if not config.use_5sec_exit:
        return None

    consecutive_down = _to_int(bar5s_consecutive_down, 0)
    vwap_break = _to_bool_or_none(bar5s_vwap_break)

    # --------------------------------------------------------
    # 1. 5秒足の急落
    # これは最低保有時間中でも逃げる。
    # 殿様イナゴでは初動崩れを早く逃げるため。
    # --------------------------------------------------------
    if _is_valid_number(bar5s_drop_pct):
        drop_pct = _to_float(bar5s_drop_pct)

        if drop_pct <= config.bar5s_hard_drop_pct + PCT_EPS:
            return _exit_all(
                reason=(
                    f"5sec_hard_drop "
                    f"bar5s_drop={drop_pct:.2f}% "
                    f"down={consecutive_down} "
                    f"pnl={pnl_pct:.2f}%"
                ),
                pnl_pct=pnl_pct,
            )

    # --------------------------------------------------------
    # 2. 5秒足VWAP割れ単独
    # 通常は False 推奨。
    # --------------------------------------------------------
    if config.bar5s_vwap_break_single_exit and vwap_break is True:
        return _exit_all(
            reason=(
                f"5sec_vwap_break_single "
                f"down={consecutive_down} "
                f"pnl={pnl_pct:.2f}%"
            ),
            pnl_pct=pnl_pct,
        )

    # --------------------------------------------------------
    # 3. 5秒足VWAP割れ + 陰線連続
    # 大きな利益が出ている場合は分割利確/トレールに任せる。
    # --------------------------------------------------------
    if (
        config.bar5s_vwap_break_exit
        and vwap_break is True
        and consecutive_down >= config.bar5s_consecutive_down_exit
        and pnl_pct <= config.bar5s_vwap_break_exit_max_pnl_pct + PCT_EPS
    ):
        return _exit_all(
            reason=(
                f"5sec_vwap_break_and_down "
                f"down={consecutive_down} "
                f"pnl={pnl_pct:.2f}%"
            ),
            pnl_pct=pnl_pct,
        )

    # --------------------------------------------------------
    # 4. 5秒足の出来高急減 + 陰線連続
    # 出来高が消えて陰線が続く場合、イナゴの勢いが抜けた可能性。
    # これも大きな利益が出ている場合は利確/トレールに任せる。
    # --------------------------------------------------------
    if (
        config.bar5s_volume_dryup_exit
        and _is_valid_number(bar5s_volume_ratio)
        and _to_float(bar5s_volume_ratio) <= config.bar5s_volume_dryup_ratio + PCT_EPS
        and consecutive_down >= config.bar5s_consecutive_down_exit
        and pnl_pct <= config.bar5s_volume_dryup_exit_max_pnl_pct + PCT_EPS
    ):
        volume_ratio = _to_float(bar5s_volume_ratio)

        return _exit_all(
            reason=(
                f"5sec_volume_dryup_and_down "
                f"volume_ratio={volume_ratio:.2f} "
                f"down={consecutive_down} "
                f"pnl={pnl_pct:.2f}%"
            ),
            pnl_pct=pnl_pct,
        )

    # --------------------------------------------------------
    # 5. 5秒足ベースの高値からの短期失速
    # bar5s_high_after_entry がなければ high_after_entry を使う。
    #
    # 注意:
    #   含み益が小さい段階ではノイズで逃げすぎるため、
    #   bar5s_drop_from_high_min_pnl_pct 以上の利益がある場合のみ判定。
    #
    #   ただし、分割利確済みなら残玉保護のため判定を許可。
    # --------------------------------------------------------
    base_high = None

    if _is_valid_number(bar5s_high_after_entry) and _to_float(bar5s_high_after_entry) > 0:
        base_high = _to_float(bar5s_high_after_entry)
    elif _is_valid_number(high_after_entry) and _to_float(high_after_entry) > 0:
        base_high = _to_float(high_after_entry)

    allow_drop_from_high = (
        pnl_pct + PCT_EPS >= config.bar5s_drop_from_high_min_pnl_pct
        or already_first_tp
        or already_second_tp
    )

    if base_high and base_high > 0 and allow_drop_from_high:
        drop_from_high_pct = calc_pct(current_price, base_high)

        if drop_from_high_pct <= config.bar5s_drop_from_high_exit_pct + PCT_EPS:
            return _exit_all(
                reason=(
                    f"5sec_drop_from_high "
                    f"drop={drop_from_high_pct:.2f}% "
                    f"base_high={base_high:.1f} "
                    f"pnl={pnl_pct:.2f}%"
                ),
                pnl_pct=pnl_pct,
            )

    return None


# ============================================================
# main policy
# ============================================================

def judge_tonosama_exit(
    *,
    symbol: str,
    entry_price: float,
    current_price: float,
    high_after_entry: Optional[float] = None,
    vwap: Optional[float] = None,
    hold_seconds: Optional[int] = None,
    ranking_lost_minutes: Optional[int] = None,
    already_first_tp: bool = False,
    already_second_tp: bool = False,

    # --------------------------------------------------------
    # optional: 5秒足特徴量
    # None の場合は従来ロジックのみで動く
    # --------------------------------------------------------
    bar5s_drop_pct: Optional[float] = None,
    bar5s_consecutive_down: Optional[int] = None,
    bar5s_volume_ratio: Optional[float] = None,
    bar5s_vwap_break: Optional[bool] = None,
    bar5s_high_after_entry: Optional[float] = None,

    config: TonosamaExitConfig = DEFAULT_TONOSAMA_EXIT_CONFIG,
) -> TonosamaExitDecision:
    """
    殿様イナゴ用 EXIT 判定。

    戻り値:
      action:
        HOLD
        EXIT_ALL
        TAKE_PROFIT_30
        TAKE_PROFIT_50

    優先順位:
      1. 損切り
      2. 5秒足の急落/短期失速
      3. 最低保有秒数ガード
      4. VWAP割れ
      5. ランキング脱落
      6. 高値からの失速（-0.5%強制EXIT、利益時トレール）
      7. 分割利確（+4%で50%、+2%で30%）
      8. HOLD

    5秒足について:
      - optional
      - 渡さなければ従来ロジックで判定
      - 渡した場合は短期失速判定として利用
      - 急落系は min_hold_seconds より優先して逃げる
    """

    _ = symbol  # ログ側・将来拡張用に引数として維持

    # --------------------------------------------------------
    # price validation
    # --------------------------------------------------------
    if not entry_price or not current_price:
        return TonosamaExitDecision(
            should_exit=False,
            action="HOLD",
            reason="invalid_price",
            sell_ratio=0.0,
            pnl_pct=0.0,
        )

    try:
        entry_price = float(entry_price)
        current_price = float(current_price)
    except Exception:
        return TonosamaExitDecision(
            should_exit=False,
            action="HOLD",
            reason="invalid_price_cast",
            sell_ratio=0.0,
            pnl_pct=0.0,
        )

    if entry_price <= 0 or current_price <= 0:
        return TonosamaExitDecision(
            should_exit=False,
            action="HOLD",
            reason=(
                f"invalid_price "
                f"entry={entry_price:.4f} "
                f"current={current_price:.4f}"
            ),
            sell_ratio=0.0,
            pnl_pct=0.0,
        )

    pnl_pct = calc_pct(current_price, entry_price)

    # --------------------------------------------------------
    # 1. 損切り
    # 最優先。pnl_pct <= -0.8% は最低保有秒数を無視して即時EXIT。
    # --------------------------------------------------------
    if pnl_pct <= config.stop_loss_pct + PCT_EPS:
        return _exit_all(
            reason=f"stop_loss pnl={pnl_pct:.2f}%",
            pnl_pct=pnl_pct,
        )

    # --------------------------------------------------------
    # 2. 5秒足の急落・短期失速
    # これも最低保有秒数より前に判定する。
    # 殿様イナゴでは初動崩れを早く逃げるため。
    # --------------------------------------------------------
    decision_5sec = _judge_5sec_exit(
        current_price=current_price,
        high_after_entry=high_after_entry,
        pnl_pct=pnl_pct,
        bar5s_drop_pct=bar5s_drop_pct,
        bar5s_consecutive_down=bar5s_consecutive_down,
        bar5s_volume_ratio=bar5s_volume_ratio,
        bar5s_vwap_break=bar5s_vwap_break,
        bar5s_high_after_entry=bar5s_high_after_entry,
        already_first_tp=already_first_tp,
        already_second_tp=already_second_tp,
        config=config,
    )
    if decision_5sec is not None:
        return decision_5sec

    # --------------------------------------------------------
    # 3. 最低保有秒数
    # ただし上の stop_loss / 5秒足急落はすでに判定済み
    # --------------------------------------------------------
    if hold_seconds is not None:
        try:
            hold_seconds_int = int(hold_seconds)
        except Exception:
            hold_seconds_int = 0

        if hold_seconds_int < config.min_hold_seconds:
            return _hold(
                reason=(
                    f"min_hold "
                    f"hold_seconds={hold_seconds_int} "
                    f"min={config.min_hold_seconds} "
                    f"pnl={pnl_pct:.2f}%"
                ),
                pnl_pct=pnl_pct,
            )

    # --------------------------------------------------------
    # 4. VWAP割れ
    # --------------------------------------------------------
    if config.vwap_break_exit and _is_valid_number(vwap) and _to_float(vwap) > 0:
        vwap_f = _to_float(vwap)

        if current_price < vwap_f:
            return _exit_all(
                reason=(
                    f"vwap_break "
                    f"current={current_price:.1f} "
                    f"vwap={vwap_f:.1f} "
                    f"pnl={pnl_pct:.2f}%"
                ),
                pnl_pct=pnl_pct,
            )

    # --------------------------------------------------------
    # 5. ランキングから一定時間消えたら逃げる
    # 利益が大きい場合は即全撤退せず、通常の利確・トレールに任せる
    # --------------------------------------------------------
    if ranking_lost_minutes is not None:
        try:
            lost_min = int(ranking_lost_minutes)
        except Exception:
            lost_min = 0

        if (
            lost_min >= config.ranking_lost_exit_minutes
            and pnl_pct <= config.ranking_lost_exit_max_pnl_pct + PCT_EPS
        ):
            return _exit_all(
                reason=(
                    f"ranking_lost "
                    f"{lost_min}min "
                    f"threshold={config.ranking_lost_exit_minutes}min "
                    f"pnl={pnl_pct:.2f}%"
                ),
                pnl_pct=pnl_pct,
            )

    # --------------------------------------------------------
    # 6. 高値からの失速
    # --------------------------------------------------------
    if _is_valid_number(high_after_entry) and _to_float(high_after_entry) > 0:
        high_f = _to_float(high_after_entry)
        drop_from_high_pct = calc_pct(current_price, high_f)

        # 高値からの失速は -0.5% 下落で強制 EXIT_ALL。
        if drop_from_high_pct <= config.hard_trailing_drop_pct + PCT_EPS:
            return _exit_all(
                reason=(
                    f"hard_trailing_drop "
                    f"drop={drop_from_high_pct:.2f}% "
                    f"high={high_f:.1f} "
                    f"pnl={pnl_pct:.2f}%"
                ),
                pnl_pct=pnl_pct,
            )

        # 分割利確後は残玉を守るため、通常より浅めに逃げる
        trailing_threshold = config.trailing_drop_pct

        if already_second_tp:
            trailing_threshold = min(
                trailing_threshold,
                config.after_second_tp_trailing_drop_pct,
            )
        elif already_first_tp:
            trailing_threshold = min(
                trailing_threshold,
                config.after_first_tp_trailing_drop_pct,
            )

        if pnl_pct > 1.0 and drop_from_high_pct <= trailing_threshold + PCT_EPS:
            return _exit_all(
                reason=(
                    f"trailing_drop "
                    f"drop={drop_from_high_pct:.2f}% "
                    f"threshold={trailing_threshold:.2f}% "
                    f"high={high_f:.1f} "
                    f"pnl={pnl_pct:.2f}% "
                    f"first_tp={already_first_tp} "
                    f"second_tp={already_second_tp}"
                ),
                pnl_pct=pnl_pct,
            )

    # --------------------------------------------------------
    # 7. 分割利確
    # --------------------------------------------------------
    if not already_second_tp and pnl_pct + PCT_EPS >= config.second_take_profit_pct:
        return _take_profit(
            action="TAKE_PROFIT_50",
            sell_ratio=0.5,
            reason=f"second_take_profit pnl={pnl_pct:.2f}%",
            pnl_pct=pnl_pct,
        )

    if not already_first_tp and pnl_pct + PCT_EPS >= config.first_take_profit_pct:
        return _take_profit(
            action="TAKE_PROFIT_30",
            sell_ratio=0.3,
            reason=f"first_take_profit pnl={pnl_pct:.2f}%",
            pnl_pct=pnl_pct,
        )

    # --------------------------------------------------------
    # 8. 継続保有
    # --------------------------------------------------------
    return _hold(
        reason=f"hold pnl={pnl_pct:.2f}%",
        pnl_pct=pnl_pct,
    )


__all__ = [
    "TonosamaExitConfig",
    "TonosamaExitDecision",
    "DEFAULT_TONOSAMA_EXIT_CONFIG",
    "calc_pct",
    "judge_tonosama_exit",
]