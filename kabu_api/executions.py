# kabu_api/executions.py（Ver18-FINAL）
import requests
from kabu_api.utils import API_URL, get_valid_token
import logging

logger = logging.getLogger(__name__)


def get_executions():
    """
    kabuステAPI /executions
    当日の全約定一覧を取得
    """
    try:
        token = get_valid_token()
        url = f"{API_URL}/executions"
        headers = {"X-API-KEY": token}

        res = requests.get(url, headers=headers, timeout=5)
        res.raise_for_status()

        return res.json()

    except Exception as e:
        logger.error(f"❌ get_executions エラー: {e}")
        return []
