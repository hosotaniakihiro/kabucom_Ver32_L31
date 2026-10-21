# ============================================================
# File   : core/startup/bootstrap/__init__.py
# ------------------------------------------------------------
# 起動時の各種 bootstrap。
# もとは core/startup 直下に *_bootstrap.py として置かれていたが、
# いずれもモンキーパッチではなく起動サブシステムのモジュールなので
# サブパッケージへまとめた。
#   schedule_loop.py  旧 schedule_loop.py          (schedule ループ本体)
#   anchor.py         旧 anchor_bootstrap.py
#   db.py             旧 db_bootstrap.py
#   background.py     旧 background_bootstrap.py
#   symbol_flags.py   旧 symbol_flags_bootstrap.py
#   symbol.py         旧 symbol_bootstrap.py
# ============================================================
