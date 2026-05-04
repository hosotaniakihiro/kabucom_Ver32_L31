#Position.py

import urllib.request
import json
import pprint
import configparser
#import ats_get_token
import pandas as pd

# 設定ファイルを読み込む
conf = configparser.ConfigParser()
conf.read('settings.ini', encoding="utf-8")
APIPassword = conf['aukabu']['apipassword']
Password = conf['aukabu']['password']
Token = conf['aukabu']['token']
# 行の表示数の上限を撤廃
pd.set_option('display.max_rows', None)
pd.set_option('display.max_columns', None)
config = configparser.ConfigParser()
config.read("settings.ini", encoding="utf-8")
# リクエストのパラメータを設定
symbol = '9501'
url = 'http://localhost:18080/kabusapi/positions'
params = {
    'product': 0,  # product - 0:すべて、1:現物、2:信用、3:先物、4:OP
    'symbol': symbol,  # symbol='xxxx'
    'side': '2',  # 1:売、2:買
    'addinfo': 'false'  # true:追加情報を出力する、false:追加情報を出力しない
}

# リクエストを作成
req = urllib.request.Request(f'{url}?{urllib.parse.urlencode(params)}', method='GET')
req.add_header('Content-Type', 'application/json')
req.add_header('X-API-KEY', Token)

# リクエストを実行し、レスポンスを処理
try:
    with urllib.request.urlopen(req) as res:
        print(res.status, res.reason)
        for header in res.getheaders():
            print(header)
        print()
        content = json.loads(res.read())
        pprint.pprint(content)

        # JSONレスポンスを pandas DataFrame に変換
        df = pd.DataFrame(content)
#        print(df)
except urllib.error.HTTPError as e:
    print(e)
    content = json.loads(e.read())
    pprint.pprint(content)
except Exception as e:
    print(e)

# DataFrame を表示
print("取得したデータ:")
print(df)
