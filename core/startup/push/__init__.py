# ============================================================
# File   : core/startup/push/__init__.py
# ------------------------------------------------------------
# PUSH (kabu ステーション WebSocket) の起動まわり。
# もとは core/startup 直下に push_*.py として置かれていたが、
# いずれもモンキーパッチではなく起動サブシステムのモジュールなので
# サブパッケージへまとめた。
#   bootstrap.py         旧 push_bootstrap.py
#   startup.py           旧 push_startup.py          (facade)
#   stream_bootstrap.py  旧 push_stream_bootstrap.py
#   storage_bootstrap.py 旧 push_storage_bootstrap.py
#   symbol_bridge.py     旧 push_symbol_bridge.py
# symbol bridge の実装本体は core/startup/push_symbol_bridge_modules/ に
# 残してある (直下ではないので移動していない)。
# ============================================================
