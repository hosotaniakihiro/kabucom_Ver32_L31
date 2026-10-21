# ============================================================
# File   : core/startup/scheduler/__init__.py
# ------------------------------------------------------------
# schedule ジョブの登録・起動まわり。
# もとは core/startup 直下に scheduler_*.py として 8 ファイル置かれていたが、
# いずれもモンキーパッチではなく起動サブシステム内部のモジュールなので
# サブパッケージへまとめた。
#   bootstrap.py          旧 scheduler_bootstrap.py          (ジョブ登録本体)
#   startup.py            旧 scheduler_startup.py            (facade)
#   exit_bootstrap.py     旧 scheduler_exit_bootstrap.py
#   tonosama_bootstrap.py 旧 scheduler_tonosama_bootstrap.py
#   ranking_bootstrap.py  旧 scheduler_ranking_bootstrap.py
#   helpers.py            旧 scheduler_helpers.py
#   market_guard.py       旧 scheduler_market_guard.py
#   summary_debug.py      旧 scheduler_summary_debug.py
# ============================================================
