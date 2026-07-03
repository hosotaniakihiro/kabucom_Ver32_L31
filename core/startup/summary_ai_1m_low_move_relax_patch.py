# -*- coding: utf-8 -*-
from __future__ import annotations

from core.startup.summary_ai_1m_range_relax_patch import VERSION, install

try:
    install()
except Exception:
    pass

__all__ = ["VERSION", "install"]
