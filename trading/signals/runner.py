import logging

logger = logging.getLogger(__name__)


def run_condition(
    cond_func,
    curr: dict,
    prev: dict,
    recent_data,
    scoring_config: dict,
    prev_state: dict,
    symbol: str,
    side: str,
):
    """
    共通のシグナル条件実行ランナー

    Args:
        cond_func: 条件関数
        curr: 最新のレコード（dict）
        prev: 1つ前のレコード（dict）
        recent_data: 直近のDataFrame
        scoring_config: {条件名: スコア} のdict
        prev_state: 過去状態保持用
        symbol: 銘柄コード
        side: "BUY" or "SELL"

    Returns:
        tuple[int, str | None, bool]:
            - score: int スコア値
            - reason: str 理由文言（None の場合は未成立）
            - flag: bool 条件成立フラグ
    """
    try:
        # 条件関数を実行（必ず2値返却を想定）
        flag, reason_key = cond_func(curr, prev, recent_data, prev_state)

        # 未成立ならスコアなし
        if not flag or not reason_key:
            return 0, None, False

        # スコア取得（デフォルト0）
        score = scoring_config.get(reason_key, 0)

        # 理由文字列
        sign = f"+{score}" if score > 0 else str(score)
        reason = f"{side}条件成立: {reason_key} ({sign})"

        return score, reason, True

    except Exception as e:
        logger.error(
            f"❌ run_condition エラー {symbol} {side}: {e}",
            exc_info=True
        )
        return 0, None, False
