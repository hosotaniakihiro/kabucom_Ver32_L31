import datetime as dt

def get_time_weight():
    t = dt.datetime.now().time()
    if dt.time(9,0) <= t <= dt.time(10,0):
        return 1.2   # 寄り
    if dt.time(14,30) <= t:
        return 0.8   # 引け
    return 1.0