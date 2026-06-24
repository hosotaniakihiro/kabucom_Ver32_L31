import threading


class SignalState:
    def __init__(self):
        self._state = {}
        self._lock = threading.Lock()

    def is_first(self, symbol, key, *, allow_reentry=False, reentry_condition=False):
        if not symbol or not key:
            return False
        k = (str(symbol), str(key))
        with self._lock:
            if k not in self._state:
                self._state[k] = True
                return True
            if allow_reentry and reentry_condition:
                return True
            return False

    def reset(self, symbol, key):
        if not symbol or not key:
            return
        with self._lock:
            self._state.pop((str(symbol), str(key)), None)

    def reset_symbol(self, symbol):
        if not symbol:
            return
        symbol = str(symbol)
        with self._lock:
            for k in list(self._state.keys()):
                if k[0] == symbol:
                    self._state.pop(k, None)

    def reset_all(self):
        with self._lock:
            self._state.clear()

    def dump(self):
        with self._lock:
            return dict(self._state)


signal_state = SignalState()
