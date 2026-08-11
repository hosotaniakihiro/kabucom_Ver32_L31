# ============================================================
# File   : core/startup/closed_day/__init__.py
# ------------------------------------------------------------
# 休場日サマリーの再構築・表示まわり。
# もとは core/startup 直下に closed_day_db.py / closed_day_display.py /
# display_debug.py として置かれていたが、いずれもモンキーパッチではなく
# 起動サブシステム内部のヘルパーだったのでサブパッケージへまとめた。
# 参照元は core/startup 内に閉じている (startup_orchestrator /
# summary_runtime_pkg.closed_day)。
# ============================================================
