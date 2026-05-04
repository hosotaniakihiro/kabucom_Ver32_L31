# utils_config.py
import os
from configparser import ConfigParser

CONFIG_FILE = "score_config.ini"

def _read_config(file_path: str = CONFIG_FILE) -> ConfigParser:
    config = ConfigParser()
    if not os.path.exists(file_path):
        print(f"⚠️ INIファイルが見つかりません: {file_path}")
        return config
    config.read(file_path, encoding="utf-8")
    return config


def _clean_value(val: str) -> str:
    """
    値からコメントや余計な空白を除去する
    - 「;」「#」以降を削除
    """
    if ";" in val:
        val = val.split(";", 1)[0]
    if "#" in val:
        val = val.split("#", 1)[0]
    return val.strip()


def load_scoring(section: str, file_path: str = CONFIG_FILE) -> dict:
    """
    スコア設定を読み込み dict で返す
    """
    config = _read_config(file_path)
    scoring = {}

    if not config.has_section(section):
        print(f"⚠️ セクションが見つかりません: [{section}]")
        return scoring

    for key, val in config.items(section):
        clean_val = _clean_value(val)
        if clean_val == "":
            continue
        try:
            scoring[key] = int(clean_val)
        except ValueError:
            try:
                scoring[key] = float(clean_val)
            except ValueError:
                print(f"⚠️ スコア設定変換エラー: {key} = {val} (cleaned={clean_val})")
    return scoring


def load_thresholds(file_path: str = CONFIG_FILE) -> dict:
    """
    [trade] セクションからしきい値を読み込み dict で返す
    """
    config = _read_config(file_path)
    thresholds = {}

    if not config.has_section("trade"):
        return thresholds

    for key, val in config.items("trade"):
        clean_val = _clean_value(val)
        if clean_val == "":
            continue
        try:
            thresholds[key.upper()] = int(clean_val)
        except ValueError:
            try:
                thresholds[key.upper()] = float(clean_val)
            except ValueError:
                thresholds[key.upper()] = clean_val
    return thresholds
