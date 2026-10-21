# ============================================================
# File   : core/startup/runtime_env/__init__.py
# ------------------------------------------------------------
# 起動時の環境変数既定値と settings.ini の読み込み。
# もとは core/startup 直下に置かれていたが、いずれもこのサブシステムの
# 設定レイヤーなのでまとめた。
#   defaults.py         旧 runtime_env_defaults.py         (既定値の辞書群)
#   registry.py         旧 runtime_env_default_registry.py (キーのメタデータ)
#   installer.py        旧 runtime_env_defaults_patch.py   (適用 + install dispatcher)
#   settings_loader.py  旧 runtime_settings_ini_loader.py  (settings.ini -> env)
#   settings_ini.py     旧 settings_ini_loader.py          (get_setting 系)
# ============================================================
