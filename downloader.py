"""One-task download: pull a single file through ALL links at once.

Splits the file into N byte ranges (HTTP Range requests) and fetches each
range over a different link, bound to that link's IP. Faster links get more
segments, proportional to their measured weight. A segment that fails on one
link is retried on the others, so one dying link can't kill the download.

Falls back to a plain single-connection download (on the fastest link) when
the server doesn't report a size or doesn't honor Range requests.

Stdlib only. Needs Python 3.9+.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit, unquote

from speedtest import _BoundHTTPConnection, _BoundHTTPSConnection

CHUNK = 256 * 1024
UA = "multilink-dl/1.0"


def _conn_for(url, src_ip):
    parts = urlsplit(url)
    if parts.scheme == "https":
        cls, default_port = _BoundHTTPSConnection, 443
    else:
        cls, default_port = _BoundHTTPConnection, 80
    return cls(parts.hostname, parts.port or default_port, src_ip=src_ip), parts


def filename_for(url):
    """Sensible local filename from a URL (Windows-safe)."""
    path = urlsplit(url).path.rstrip("/")
    name = path.rsplit("/", 1)[-1] if path else ""
    name = unquote(name.split("?")[0].split("#")[0])
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "_")
    name = name.strip().strip(".")
    return name or "download.bin"


def _head_probe(url, src_ip):
    """HEAD request. Returns (size or None, advertises_ranges bool)."""
    conn, parts = _conn_for(url, src_ip)
    try:
        conn.request("HEAD", parts.path or "/", headers={"User-Agent": UA})
        resp = conn.getresponse()
        resp.read()
        size = resp.getheader("Content-Length")
        ranges = (resp.getheader("Accept-Ranges") or "").lower() == "bytes"
        return (int(size) if size else None), ranges
    except Exception:
        return None, False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _range_probe(url, src_ip):
    """GET bytes=0-0. Returns (size or None, honors_ranges bool)."""
    conn, parts = _conn_for(url, src_ip)
    try:
        conn.request("GET", parts.path or "/",
                     headers={"User-Agent": UA, "Range": "bytes=0-0"})
        resp = conn.getresponse()
        resp.read()
        cr = resp.getheader("Content-Range") or ""  # e.g. "bytes 0-0/12345"
        if resp.status == 206 and "/" in cr:
            return int(cr.rsplit("/", 1)[1]), True
        size = resp.getheader("Content-Length")
        return (int(size) if size else None), False
    except Exception:
        return None, False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def probe(url, src_ip=None):
    """Return (size_bytes or None, accepts_ranges bool) for url."""
    size, ranges = _head_probe(url, src_ip)
    if size and not ranges:
        # Some servers honor ranges without advertising it; verify cheaply.
        _, ranges = _range_probe(url, src_ip)
    elif not size:
        size, ranges = _range_probe(url, src_ip)
    return size, ranges


class _RangeIgnored(Exception):
    """Server returned 200 to a Range request: it doesn't really do ranges."""


def _assign_links(links, n_segments):
    """Weighted distribution: faster (higher-weight) links get more segments,
    interleaved so no link's segments all bunch up."""
    weights = []
    for link in links:
        try:
            weights.append(max(0.01, float(link.get("weight", 1.0) or 1.0)))
        except (TypeError, ValueError):
            weights.append(1.0)
    total = sum(weights)
    quotas = [w / total * n_segments for w in weights]
    counts = [int(q) for q in quotas]
    # Hand out leftover segments to the largest fractional quotas.
    rem = n_segments - sum(counts)
    by_frac = sorted(range(len(links)), key=lambda i: quotas[i] - counts[i],
                     reverse=True)
    for i in range(rem):
        counts[by_frac[i % len(by_frac)]] += 1
    # Interleave round-robin across links for an even spread.
    buckets = [[links[i]] * counts[i] for i in range(len(links))]
    result = []
    while any(buckets):
        for b in buckets:
            if b:
                result.append(b.pop(0))
    return result


def _fetch_range(url, link, start, end, fh, fh_lock, progress, stop):
    """Fetch bytes [start, end] over link, writing at file offset. Bool ok."""
    conn, parts = _conn_for(url, link["ip"])
    try:
        conn.request("GET", parts.path or "/", headers={
            "User-Agent": UA,
            "Accept-Encoding": "identity",
            "Range": f"bytes={start}-{end}",
        })
        resp = conn.getresponse()
        if resp.status == 200:
            # Server ignored our Range; don't write - caller falls back.
            raise _RangeIgnored()
        if resp.status != 206:
            return False
        offset = start
        while not stop.is_set():
            data = resp.read(CHUNK)
            if not data:
                break
            with fh_lock:
                fh.seek(offset)
                fh.write(data)
            offset += len(data)
            with progress["lock"]:
                progress["done"] += len(data)
                progress["per_link"][link["name"]] = \
                    progress["per_link"].get(link["name"], 0) + len(data)
        return offset > start
    except Exception:
        return False
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _fetch_segment(url, start, end, links_ordered, fh, fh_lock, progress, stop):
    """Try a segment on its assigned link, then fail over to the others."""
    for link in links_ordered:
        if stop.is_set():
            return False
        if _fetch_range(url, link, start, end, fh, fh_lock, progress, stop):
            return True
    return False


def _run_monitor(progress, total, start, cb, stop):
    while not stop.is_set():
        time.sleep(0.4)
        with progress["lock"]:
            cb(progress["done"], total, dict(progress["per_link"]),
               time.time() - start)
    with progress["lock"]:
        cb(progress["done"], total, dict(progress["per_link"]),
           time.time() - start)


def _segmented(url, dest, size, links, n_segments, progress_cb, t0):
    n_segments = max(2, min(n_segments, max(2, size // 65536)))
    base = size // n_segments
    bounds = []
    for i in range(n_segments):
        s = i * base
        e = size - 1 if i == n_segments - 1 else (i + 1) * base - 1
        bounds.append((s, e))
    assigned = _assign_links(links, n_segments)

    with open(dest, "wb") as f:
        f.truncate(size)

    progress = {"done": 0, "per_link": {}, "lock": threading.Lock()}
    stop = threading.Event()
    mon = None
    if progress_cb:
        mon = threading.Thread(target=_run_monitor,
                               args=(progress, size, t0, progress_cb, stop),
                               daemon=True)
        mon.start()
    fh_lock = threading.Lock()
    failed = []
    try:
        with open(dest, "r+b") as fh:
            with ThreadPoolExecutor(max_workers=min(n_segments, 32)) as ex:
                futs = {}
                for i, (s, e) in enumerate(bounds):
                    first = assigned[i]
                    # assigned link first, then the rest for failover
                    ordered = [first] + [l for l in links if l["ip"] != first["ip"]]
                    futs[ex.submit(_fetch_segment, url, s, e, ordered,
                                   fh, fh_lock, progress, stop)] = i
                for fut in as_completed(futs):
                    if not fut.result():
                        failed.append(futs[fut])
                        stop.set()
    finally:
        stop.set()
        if mon:
            mon.join(timeout=2)

    if failed:
        raise RuntimeError(f"{len(failed)} segment(s) failed on every link; "
                           f"download incomplete: {dest}")
    with progress["lock"]:
        done = progress["done"]
    if done < size:
        raise RuntimeError(f"short download: got {done} of {size} bytes")
    return dest, done, time.time() - t0


def _single_on_link(url, dest, link, size, progress_cb, t0):
    """Plain one-connection download over the given link."""
    progress = {"done": 0, "per_link": {}, "lock": threading.Lock()}
    stop = threading.Event()
    mon = None
    if progress_cb:
        mon = threading.Thread(target=_run_monitor,
                               args=(progress, size, t0, progress_cb, stop),
                               daemon=True)
        mon.start()
    conn, parts = _conn_for(url, link["ip"])
    try:
        conn.request("GET", parts.path or "/",
                     headers={"User-Agent": UA, "Accept-Encoding": "identity"})
        resp = conn.getresponse()
        if resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status} from server")
        total = size
        if total is None:
            cl = resp.getheader("Content-Length")
            total = int(cl) if cl else None
        with open(dest, "wb") as f:
            while True:
                data = resp.read(CHUNK)
                if not data:
                    break
                f.write(data)
                with progress["lock"]:
                    progress["done"] += len(data)
                    progress["per_link"][link["name"]] = \
                        progress["per_link"].get(link["name"], 0) + len(data)
    finally:
        stop.set()
        if mon:
            mon.join(timeout=2)
        try:
            conn.close()
        except Exception:
            pass
    with progress["lock"]:
        done = progress["done"]
    return dest, done, time.time() - t0


def _single(url, dest, links_ordered, size, progress_cb, t0):
    """One-connection download, trying links best-first until one works."""
    last_exc = None
    for link in links_ordered:
        try:
            return _single_on_link(url, dest, link, size, progress_cb, t0)
        except _RangeIgnored:
            raise
        except Exception as exc:  # link dead? try the next one
            last_exc = exc
    raise RuntimeError(f"download failed on every link (last: {last_exc})")


def download(url, dest, links, segments=16, progress_cb=None):
    """Download url to dest using all links.

    progress_cb(done_bytes, total_bytes_or_None, per_link_dict, elapsed_secs)
    is called ~2.5x/sec. Returns (dest, bytes_downloaded, seconds).
    Raises RuntimeError on failure.
    """
    if not links:
        raise RuntimeError("no links configured")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise RuntimeError("URL must be http(s)://…")
    t0 = time.time()
    size, ranges = probe(url, links[0]["ip"])
    ordered = sorted(links, key=lambda l: float(l.get("weight", 1.0) or 1.0),
                     reverse=True)
    if size and ranges and len(links) > 1 and segments > 1:
        try:
            return _segmented(url, dest, size, links, segments, progress_cb, t0)
        except _RangeIgnored:
            pass  # server lied about ranges; fall through to single
    if progress_cb and (not size or not ranges):
        progress_cb(0, size, {}, 0.0)
    return _single(url, dest, ordered, size, progress_cb, t0)
