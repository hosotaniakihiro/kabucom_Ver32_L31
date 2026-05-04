# ============================================================
# trading/scoring/score_table.py
# Ver27.0-ABSOLUTE-FLAG-COMPATIBLE
# ------------------------------------------------------------
# ✔ 既存仕様完全保持（削除ゼロ）
# ✔ score_config.ini を唯一の定義源
# ✔ BUY / SELL / ENTRY / BONUS 自動分類維持
# ✔ absolute 廃止維持
# ✔ Runtime互換維持
# ✔ build_score_tables / build_score_table 維持
# ✔ TABLES / SCORE_TABLE 公開維持
# ✔ 🔥 flag_ 接頭辞自動対応（今回の核心修正）
# ✔ 🔥 大文字小文字・空白安全化
# ✔ 🔥 iniキーとflag列の整合保証
# ============================================================

from configparser import ConfigParser
from pathlib import Path
from typing import Dict


# ============================================================
# 🔧 ini パス解決（cwd 非依存）
# ============================================================

def _resolve_ini_path(ini_path: str | Path | None = None) -> Path:

    scoring_dir = Path(__file__).resolve().parent
    project_root = scoring_dir.parent.parent

    candidates = []

    if ini_path:
        candidates.append(Path(ini_path))

    candidates.extend([
        scoring_dir / "score_config.ini",
        project_root / "score_config.ini",
    ])

    for p in candidates:
        if p.exists():
            return p

    raise FileNotFoundError(
        "score_config.ini not found. Tried:\n"
        + "\n".join(str(p) for p in candidates)
    )


def _load_config(ini_path: str | Path | None = None) -> ConfigParser:
    path = _resolve_ini_path(ini_path)
    conf = ConfigParser()
    conf.read(path, encoding="utf-8")
    return conf


def _as_int(v) -> int:
    try:
        return int(float(v))
    except Exception:
        return 0


def _normalize_key(key: str) -> str:
    """
    iniキーを安全正規化
    - strip
    - lower
    """
    return key.strip().lower()


def _with_flag_prefix(key: str) -> str:
    """
    add_scores 側と一致させるため flag_ を付与
    """
    key = _normalize_key(key)

    if key.startswith("flag_"):
        return key

    return f"flag_{key}"


# ============================================================
# 🔥 BUY / SELL / ENTRY / BONUS テーブル生成
# ============================================================

def build_score_tables(
    ini_path: str | Path | None = None,
) -> Dict[str, Dict[str, int] | Dict[str, str]]:

    conf = _load_config(ini_path)

    buy_entry: Dict[str, int] = {}
    buy_bonus: Dict[str, int] = {}
    sell_entry: Dict[str, int] = {}
    sell_bonus: Dict[str, int] = {}
    reentry: Dict[str, str] = {}

    # --------------------------------------------------------
    # reentry
    # --------------------------------------------------------
    if conf.has_section("reentry"):
        for k, v in conf["reentry"].items():
            reentry[_normalize_key(k)] = v

    # --------------------------------------------------------
    # BUY scoring
    # --------------------------------------------------------
    if conf.has_section("scoring"):
        for key, val in conf["scoring"].items():

            score = _as_int(val)
            if score <= 0:
                continue

            key_norm = _normalize_key(key)

            if (
                key_norm.endswith("_event")
                or key_norm == "dir_up"
                or key_norm in reentry
            ):
                buy_entry[key_norm] = score
            else:
                buy_bonus[key_norm] = score

    # --------------------------------------------------------
    # SELL scoring
    # --------------------------------------------------------
    if conf.has_section("short_scoring"):
        for key, val in conf["short_scoring"].items():

            score = _as_int(val)
            if score >= 0:
                continue

            key_norm = _normalize_key(key)

            if key_norm == "dir_down" or key_norm in reentry:
                sell_entry[key_norm] = score
            else:
                sell_bonus[key_norm] = score

    return {
        "buy_entry": buy_entry,
        "buy_bonus": buy_bonus,
        "sell_entry": sell_entry,
        "sell_bonus": sell_bonus,
        "reentry": reentry,
    }


# ============================================================
# 🔥 統一 SCORE_TABLE（add_scores 用）
# ============================================================

def build_score_table(
    ini_path: str | Path | None = None,
) -> Dict[str, int]:
    """
    add_scores.py が参照する唯一のスコア表
    ・ini に存在するキーのみ
    ・score != 0 のもののみ
    ・flag_ 接頭辞付きで返す（今回の核心修正）
    """

    conf = _load_config(ini_path)

    table: Dict[str, int] = {}

    for section in ("scoring", "short_scoring"):

        if not conf.has_section(section):
            continue

        for key, val in conf[section].items():

            score = _as_int(val)
            if score == 0:
                continue

            key_flag = _with_flag_prefix(key)
            table[key_flag] = score

    return table


# ============================================================
# ★ 公開定数（Runtime 用）
# ============================================================

TABLES = build_score_tables()
SCORE_TABLE: Dict[str, int] = build_score_table()


# ============================================================
# 🧪 単体実行（デバッグ）
# ============================================================

if __name__ == "__main__":

    print("\n========= SCORE TABLES =========\n")

    tables = build_score_tables()
    for name, table in tables.items():
        print(f"[{name}]")
        for k, v in sorted(table.items()):
            print(f"  {k:30s} {v}")
        print()

    print("[SCORE_TABLE / USED BY add_scores]")
    for k, v in sorted(SCORE_TABLE.items()):
        print(f"  {k:30s} {v}")