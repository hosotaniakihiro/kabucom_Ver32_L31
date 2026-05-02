# ============================================================
# File   : trading/push/subscription_manager/globals_access.py
# Function:
#   - global_data の安全取得
#   - safe getattr / safe setattr を提供
#   - global_data 依存をこのモジュールへ閉じ込める
# ------------------------------------------------------------
# Notes:
#   - import 失敗や属性差異があっても落ちないようにする
# ============================================================

from __future__ import annotations

from typing import Any


def safe_get_global_data() -> Any:
    candidates = [
        ("global_state", "global_data"),
        ("core.global_state", "global_data"),
        ("core.global_context.context", "global_data"),
        ("core.global_context", "global_data"),
    ]
    for mod_name, attr_name in candidates:
        try:
            mod = __import__(mod_name, fromlist=[attr_name])
            gd = getattr(mod, attr_name, None)
            if gd is not None:
                return gd
        except Exception:
            continue
    return None


def safe_getattr(obj: Any, name: str, default: Any = None) -> Any:
    try:
        return getattr(obj, name, default)
    except Exception:
        return default


def safe_setattr(obj: Any, name: str, value: Any) -> bool:
    try:
        setattr(obj, name, value)
        return True
    except Exception:
        return False