subscription_manager 分割版 導入メモ
============================================================

配置先:
  trading/push/subscription_manager/

同梱ファイル:
  core.py
  target_builder.py
  priority_symbols.py
  rotation.py
  refresh_policy.py
  global_store.py
  manager_loop.py
  ranking_selector.py
  __init__.py

既存のまま必要な前提ファイル:
  state.py
  filters.py
  globals_access.py
  guards.py
  ranking_source.py
  register_ops.py
  register_symbol_logger.py
  symbols.py
  transport.py

重要:
  ranking_selector.py は最大100銘柄候補を作る。
  target_builder.py と rotation.py が最終的に50銘柄以内にする。
  core.py でも最終防衛として50件超を切る。

確認ログ:
  [PUSH RANKING SELECTOR] final symbols=100 max=100
  [SUB MANAGER ROTATION] split total=100 priority=0 A=50 B=50 chunk=50
  [SUB MANAGER TARGET] final counts ... selected=50
  [SUB MANAGER CORE] refresh start ... target=50

target=100 になっていたら、古い core.py がまだ使われています。
