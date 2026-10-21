# ============================================================
# File   : core/startup/summary/__init__.py
# ------------------------------------------------------------
# 起動時の SUMMARY 構築・復元まわり。
# もとは core/startup 直下に置かれていたが、いずれもモンキーパッチではなく
# 起動サブシステムのモジュールなのでサブパッケージへまとめた。
#   bootstrap.py              旧 summary_bootstrap.py
#   startup.py                旧 summary_startup.py            (facade)
#   restore.py                旧 startup_summary_restore.py
#   push_incremental_ma75.py  旧 startup_push_incremental_ma75.py
#   mtf_history_runner.py     旧 mtf_history_bootstrap_runner.py
#   merged_access.py          旧 merged_summary_access.py
#   runtime.py                旧 summary_runtime.py
# 既存の core/startup/summary_runtime_pkg/ は別物なので触っていない。
# ============================================================
