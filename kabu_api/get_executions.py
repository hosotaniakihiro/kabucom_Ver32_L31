import requests
import json
from token_manager import get_valid_token
from config_loader import API_URL

def get_executions():
    """
    kabuステーション API から直近の約定履歴を取得する
    """
    token = get_valid_token()
    url = f"{API_URL}/executions"
    headers = {"Content-Type": "application/json", "X-API-KEY": token}

    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()
        executions = response.json()

        print("=== 約定履歴 ===")
        if not executions:
            print("⚠️ 約定履歴がありません")
            return []

        for exe in executions:
            print(
                f"約定ID: {exe.get('ExecutionID')}, "
                f"銘柄: {exe.get('Symbol')} {exe.get('SymbolName')}, "
                f"取引: {exe.get('Side')}, "
                f"株数: {exe.get('Qty')}, "
                f"価格: {exe.get('Price')}, "
                f"日時: {exe.get('ExecutionDay')}"
            )
        return executions

    except Exception as e:
        print(f"❌ 約定履歴取得エラー: {e}")
        try:
            print("レスポンス本文:", response.json())
        except Exception:
            pass
        return []
