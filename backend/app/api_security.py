"""Bounded per-peer write limiter for a single-worker local deployment."""

from collections import OrderedDict, deque
import math
import threading


class RateLimiter:
    def __init__(self, limit=5, window=60, max_clients=10000, idle_ttl=120):
        self.limit, self.window = limit, window
        self.max_clients, self.idle_ttl = max_clients, idle_ttl
        self.clients: OrderedDict[str, tuple[deque[float], float]] = OrderedDict()
        self._lock = threading.Lock()

    def consume(self, client, now):
        with self._lock:
            while self.clients:
                key, (_, used) = next(iter(self.clients.items()))
                if now - used < self.idle_ttl:
                    break
                self.clients.pop(key)
            if client not in self.clients and len(self.clients) >= self.max_clients:
                return False, max(1, math.ceil(self.idle_ttl))
            history, _ = self.clients.pop(client, (deque(), now))
            while history and now - history[0] >= self.window:
                history.popleft()
            self.clients[client] = history, now
            if len(history) >= self.limit:
                return False, max(1, math.ceil(self.window - (now - history[0])))
            history.append(now)
            return True, 0
