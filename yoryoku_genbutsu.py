#取引余力（現物）
#コマンド：python kabusapi_cash.py

import urllib.request
import json
import pprint
import configparser
import settings
import ats_get_token

conf = configparser.ConfigParser()
conf.read('settings.ini')
#APIPassword = settings.APIPassword
APIPassword = conf['aukabu']['APIPassword']
Password = conf['aukabu']['Password']
#Token =conf['aukabu']['Token']
Token = ats_get_token.generate_token(APIPassword)
#
print(Token)

url = 'http://localhost:18080/kabusapi/wallet/cash'
req = urllib.request.Request(url, method='GET')
req.add_header('Content-Type', 'application/json')
req.add_header('X-API-KEY', Token)

try:
    with urllib.request.urlopen(req) as res:
        print(res.status, res.reason)
        for header in res.getheaders():
            print(header)
        print()
        content = json.loads(res.read())
        pprint.pprint(content)
except urllib.error.HTTPError as e:
    print(e)
    content = json.loads(e.read())
    pprint.pprint(content)
except Exception as e:
    print(e)
