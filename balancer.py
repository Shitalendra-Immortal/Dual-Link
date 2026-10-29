"""Per-connection load balancing across detected links, with failover."""
from __future__ import annotations

import itertools
import threading
import time


class LinkState:
    def __init__(self, link):
        self.link = link
        self.failures = 0
        self.cooldown_until = 0.0
        self._lock = threading.Lock()

    @property
    def weight(self):
        try:
            return max(0.1, float(self.link.get("weight", 1.0)))
        except (TypeError, ValueError):
            return 1.0

    @property
    def healthy(self):
        return time.time() >= self.cooldown_until

    def mark_failure(self):
        with self._lock:
            self.failures += 1
            self.cooldown_until = time.time() + min(60.0, 5.0 * self.failures)

    def mark_success(self):
        with self._lock:
            self.failures = 0
            self.cooldown_until = 0.0


class Balancer:
    """Weighted round-robin over healthy links. Failed links cool down, then retry."""

    def __init__(self, links):
        if not links:
            raise ValueError("Balancer needs at least one link.")
        self.states = [LinkState(l) for l in links]
        self._lock = threading.Lock()
        self._rebuild_pool()

    def _rebuild_pool(self):
        min_w = min(s.weight for s in self.states)
        pool = []
        for idx, s in enumerate(self.states):
            pool.extend([idx] * max(1, round(s.weight / min_w)))
        self._pool = pool
        self._cycle = itertools.cycle(pool)

    def refresh_weights(self):
        with self._lock:
            self._rebuild_pool()

    def pick(self):
        """Return a healthy LinkState; if all are cooling down, try the first anyway."""
        for _ in range(len(self._pool)):
            with self._lock:
                idx = next(self._cycle)
            st = self.states[idx]
            if st.healthy:
                return st
        return self.states[0]


def auto_weight(results):
    """Compute {ip: weight} from speed-test results, proportional to measured Mbps.

    The slowest working link gets weight 1.0; faster links scale up, so each
    link receives traffic in proportion to its real capacity. Links whose test
    failed keep their existing weight (a failed test isn't proof of a dead
    link). Returns {} when fewer than 2 links tested OK — nothing to balance.

    This only produces numbers; applying them costs nothing on the data path.
    """
    ok = [r for r in results if r.get("ok") and r.get("mbps", 0) > 0]
    if len(ok) < 2:
        return {}
    slowest = min(r["mbps"] for r in ok)
    return {r["link"]["ip"]: round(r["mbps"] / slowest, 2) for r in ok}
