# config/global_config.py
from config.paths import get_path

# 旧コード互換用
GLOBAL_CONFIG = {
    "base_path": get_path("base"),
    "summary_dir": get_path("summary"),
    "push_dir": get_path("push"),
}
