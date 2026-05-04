from configparser import ConfigParser

def verify_ini_vs_flags(df, ini_path: str):
    """
    ini / df.columns / condition flags の完全整合性チェック
    """
    parser = ConfigParser()
    parser.read(ini_path, encoding="utf-8")

    ini_keys = set()
    for sec in parser.sections():
        ini_keys |= set(parser[sec].keys())

    df_flags = set(df.columns)

    missing_in_df = ini_keys - df_flags
    unused_in_df = df_flags - ini_keys

    if missing_in_df:
        raise RuntimeError(f"[INI ERROR] flags missing in df: {sorted(missing_in_df)}")

    # df 側に余分な flag があるのは WARNING に留める
    if unused_in_df:
        print(f"[INI WARN] unused flags in df: {sorted(unused_in_df)}")

    return True
