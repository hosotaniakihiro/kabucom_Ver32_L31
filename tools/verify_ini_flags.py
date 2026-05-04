# ============================================================
# tools/verify_ini_flags.py
# Ver24-FINAL-INI-FLAG-VERIFIER
# ------------------------------------------------------------
# ✔ ini に定義された全スコアキーを抽出
# ✔ df の flag 列と突合
# ✔ 未実装 / 未使用 / 未定義 を検出
# ============================================================

import configparser
import logging

logger = logging.getLogger(__name__)


# ------------------------------------------------------------
# ini から score key を抽出
# ------------------------------------------------------------
def _load_ini_keys(ini_path: str) -> set[str]:
    conf = configparser.ConfigParser()
    conf.read(ini_path, encoding="utf-8")

    keys = set()

    for section in conf.sections():
        for k in conf[section].keys():
            # score / threshold 系は除外
            if k.endswith("_threshold"):
                continue
            if k in ("threshold", "sell_threshold", "budget", "unit_size"):
                continue
            keys.add(k)

    return keys


# ------------------------------------------------------------
# メイン検証関数
# ------------------------------------------------------------
def verify_ini_vs_flags(df, ini_path: str):
    """
    df : scoring 後 DataFrame
    ini_path : score_config.ini
    """

    ini_keys = _load_ini_keys(ini_path)

    # df に存在する flag 列（0/1 前提）
    df_flags = {
        c for c in df.columns
        if (
            c in ini_keys
            or c.startswith("rsi_")
            or c.startswith("bb_")
            or c.startswith("ma")
            or c.startswith("volume_")
        )
    }

    # score_reasons に実際に出現したキー
    used_keys = set()
    if "score_reasons" in df.columns:
        for r in df["score_reasons"]:
            if isinstance(r, dict):
                used_keys |= set(r.keys())

    # --------------------------------------------------------
    # 差分計算
    # --------------------------------------------------------
    missing_flags = ini_keys - df_flags
    unused_flags = df_flags - ini_keys
    undefined_used = used_keys - ini_keys

    # --------------------------------------------------------
    # ログ出力
    # --------------------------------------------------------
    #logger.info("🧪 [INI ↔ FLAG VERIFY]")
    #logger.info("  ini keys        : %d", len(ini_keys))
    #logger.info("  df flag columns : %d", len(df_flags))
    #logger.info("  used keys       : %d", len(used_keys))

    """if missing_flags:
        logger.warning("❌ missing flag implementation:")
        for k in sorted(missing_flags):
            logger.warning("   - %s", k)

    if unused_flags:
        logger.warning("⚠ unused flag columns:")
        for k in sorted(unused_flags):
            logger.warning("   - %s", k)

    if undefined_used:
        logger.warning("❌ undefined keys detected:")
        for k in sorted(undefined_used):
            logger.warning("   - %s", k)

    if not (missing_flags or unused_flags or undefined_used):
        logger.info("✅ ini / flags / usage are perfectly aligned")

    return {
        "missing_flags": missing_flags,
        "unused_flags": unused_flags,
        "undefined_used": undefined_used,
    }"""
