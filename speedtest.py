"""Throughput tests: per-link speeds and aggregate multi-link speed.

Every test socket is bound to a specific link's IP address, so each link is
measured independently, then all links are hammered in parallel to show the
combined capacity.
"""
from __future__ import annotations

import http.client
import socket
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

DEFAULT_URLS = [
    "https://proof.ovh.net/files/10Mb.dat",
    "http://ipv4.download.thinkbroadband.com/10MB.zip",
]

CHUNK = 256 * 1024


class RangeIgnoredError(RuntimeError):
    """Server answered 200 to a Range request: it ignored the range."""


class _BoundHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, port=None, src_ip=None, timeout=20):
        self._src_ip = src_ip
        super().__init__(host, port, timeout=timeout)

    def connect(self):
        kwargs = {}
        if self._src_ip:
            kwargs["source_address"] = (self._src_ip, 0)
        self.sock = socket.create_connection((self.host, self.port), self.timeout, **kwargs)


class _BoundHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, port=None, src_ip=None, timeout=20, context=None):
        self._src_ip = src_ip
        super().__init__(host, port, timeout=timeout, context=context)

    def connect(self):
        kwargs = {}
        if self._src_ip:
            kwargs["source_address"] = (self._src_ip, 0)
        sock = socket.create_connection((self.host, self.port), self.timeout, **kwargs)
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        else:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def fetch_bytes(url, src_ip=None, max_bytes=None, duration=None,
                byte_range=None, warmup_bytes=0, return_info=False):
    """Download (part of) url, optionally bound to a source IP.

    Returns (bytes_downloaded, seconds_elapsed) by default. The seconds
    cover only the measured phase: any warmup_bytes are read and discarded
    first, in the same connection, so TCP slow-start does not pollute the
    measurement. If byte_range=(start, end) is given, a Range request is
    sent; a 200 answer raises RangeIgnoredError instead of silently
    counting bytes from the start of the file as if they were the range.
    With return_info=True, returns a dict with bytes, seconds, status,
    content_range, and the measured-phase timestamps t0/t1.
    """
    parts = urlsplit(url)
    cls = _BoundHTTPSConnection if parts.scheme == "https" else _BoundHTTPConnection
    default_port = 443 if parts.scheme == "https" else 80
    conn = cls(parts.hostname, parts.port or default_port, src_ip=src_ip)
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    headers = {
        "User-Agent": "multilink-test/1.0",
        "Accept-Encoding": "identity",
    }
    if byte_range is not None:
        headers["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    if resp.status >= 400:
        conn.close()
        raise RuntimeError(f"HTTP {resp.status} from test server")
    if byte_range is not None and resp.status == 200:
        conn.close()
        raise RangeIgnoredError(
            "server ignored Range request (answered HTTP 200)")
    # Warmup, same connection: discard warmup_bytes before starting the clock.
    remaining = warmup_bytes or 0
    try:
        while remaining > 0:
            data = resp.read(min(CHUNK, remaining))
            if not data:
                break
            remaining -= len(data)
        total = 0
        t0 = time.time()
        try:
            while True:
                data = resp.read(CHUNK)
                if not data:
                    break
                total += len(data)
                elapsed = time.time() - t0
                if max_bytes and total >= max_bytes:
                    break
                if duration and elapsed >= duration:
                    break
        finally:
            conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass
        raise
    t1 = time.time()
    if return_info:
        return {"bytes": total, "seconds": t1 - t0, "status": resp.status,
                "content_range": resp.getheader("Content-Range"),
                "t0": t0, "t1": t1}
    return total, t1 - t0


def _pick_working_url(urls):
    for url in urls:
        try:
            n, _ = fetch_bytes(url, max_bytes=CHUNK, duration=15)
            if n > 0:
                return url
        except Exception:
            continue
    raise RuntimeError("No test server reachable. Check your internet connection.")


def median(values):
    """Median of a non-empty list of numbers (stdlib, no statistics import)."""
    vals = sorted(values)
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return float(vals[mid])
    return (vals[mid - 1] + vals[mid]) / 2.0


def summarize_runs(values):
    """Median/mean/min/max for a list of Mbps runs; {} when empty."""
    if not values:
        return {}
    vals = [float(v) for v in values]
    return {"count": len(vals), "median": median(vals),
            "mean": sum(vals) / len(vals), "min": min(vals),
            "max": max(vals)}


def pick_url_for_links(links, urls):
    """Pick a test URL that works when bound to EVERY link.

    The old _pick_working_url is unbound, so it can pick a server that only
    works over the default route. For a fair combined-speed comparison all
    links must be measured against the same server. Returns
    (url_or_None, report) where report maps url -> {link_name: ok_bool}.
    """
    report = {}
    for url in urls:
        per_link = {}
        for link in links:
            try:
                n, _ = fetch_bytes(url, src_ip=link["ip"],
                                   max_bytes=CHUNK, duration=15)
                per_link[link["name"]] = n > 0
            except Exception:
                per_link[link["name"]] = False
        report[url] = per_link
        if per_link and all(per_link.values()):
            return url, report
    return None, report


def _mbps(nbytes, seconds):
    return (nbytes * 8 / 1e6 / seconds) if seconds > 0 else 0.0


def probe_link(link, host="1.1.1.1", port=443, timeout=5):
    """Lightweight check: TCP connect bound to the link's IP.

    Returns (ok: bool, latency_ms: float | None). Uses almost no data.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        start = time.time()
        s.bind((link["ip"], 0))
        s.connect((host, port))
        return True, (time.time() - start) * 1000
    except OSError:
        return False, None
    finally:
        s.close()


def test_links(links, urls=None, per_link_bytes=10_000_000, duration=45):
    url = _pick_working_url(urls or DEFAULT_URLS)
    print(f"Test server: {url}\n")
    results = []
    for link in links:
        label = f"{link['kind']} ({link['name']})"
        try:
            n, secs = fetch_bytes(url, src_ip=link["ip"],
                                  max_bytes=per_link_bytes, duration=duration)
            speed = _mbps(n, secs)
            print(f"  {label:<38} {speed:7.1f} Mbps")
            results.append({"link": link, "mbps": speed, "ok": True})
        except Exception as exc:
            print(f"  {label:<38} FAILED ({exc})")
            results.append({"link": link, "mbps": 0.0, "ok": False})
    return results


def _safe_fetch(url, ip, max_bytes, duration):
    try:
        n, _ = fetch_bytes(url, src_ip=ip, max_bytes=max_bytes, duration=duration)
        return n
    except Exception:
        return 0


def _timed_fetch(url, ip, max_bytes, duration):
    """Like _safe_fetch, but also returns the worker's own elapsed seconds."""
    start = time.time()
    return _safe_fetch(url, ip, max_bytes, duration), time.time() - start


def test_aggregate(links, urls=None, workers_per_link=2,
                   bytes_per_worker=8_000_000, duration=60, speeds=None):
    links = list(links)
    url = _pick_working_url(urls or DEFAULT_URLS)
    total_budget = bytes_per_worker * workers_per_link * len(links)
    if speeds and len(speeds) == len(links) and sum(speeds) > 0:
        # Split the byte budget proportional to each link's measured speed, so
        # every link finishes at roughly the same time. Without this, a slow
        # link given an equal share becomes a straggler and the wall-clock
        # average collapses to ~its speed (a test artifact, not real perf).
        total_speed = sum(max(s or 0, 1e-6) for s in speeds)
        jobs = []
        for link, s in zip(links, speeds):
            share = max(s or 0, 1e-6) / total_speed
            # No floor on n_bytes: the proportional split already equalizes
            # finish times; a floor would reintroduce a straggler.
            n_bytes = round(total_budget * share)
            n_workers = max(1, round(workers_per_link * len(links) * share))
            per_worker = max(64_000, n_bytes // n_workers)
            jobs.extend([(link, per_worker)] * n_workers)
    else:
        jobs = [(link, bytes_per_worker)
                for _ in range(workers_per_link) for link in links]
    print(f"\nAggregate test: {len(jobs)} parallel downloads across {len(links)} link(s)...")
    total = 0
    start = time.time()
    per_link = {}  # name -> [bytes, slowest_worker_secs]
    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {ex.submit(_timed_fetch, url, link["ip"], nbytes, duration): link
                for link, nbytes in jobs}
        for fut in as_completed(futs):
            link = futs[fut]
            nbytes, secs = fut.result()
            total += nbytes
            b, t = per_link.get(link["name"], (0, 0.0))
            per_link[link["name"]] = (b + nbytes, max(t, secs))
    wall = time.time() - start
    speed = _mbps(total, wall)
    for name, (b, t) in per_link.items():
        print(f"    {name}: {_mbps(b, t):.1f} Mbps ({b / 1e6:.1f} MB in {t:.1f}s)")
    print(f"  Combined throughput: {speed:.1f} Mbps "
          f"({total / 1e6:.1f} MB in {wall:.1f}s)")
    return speed
