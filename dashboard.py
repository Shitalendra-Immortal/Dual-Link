#!/usr/bin/env python3
"""Web dashboard for MultiLink Booster.

Run:  py dashboard.py [--port 8899] [--no-browser]
Then open http://127.0.0.1:8899 in your browser.

Link cards with live up/down status, real-time throughput graphs fed by the
proxy/bond byte counters, one-click speed tests, and start/stop controls for
the proxy and the bonding client. Standard library only.
"""
import argparse
import asyncio
import json
import os
import socket
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
CONFIG = os.path.join(BASE, "config.json")
HTML_FILE = os.path.join(BASE, "dashboard.html")

PROBE_INTERVAL = 15


class State:
    def __init__(self):
        self.links = []
        self.proxy_running = False
        self.proxy_loop = None
        self.proxy_error = None
        self.proxy_stats = {}
        self.bond_running = False
        self.bond_loop = None
        self.bond_client = None
        self.bond_error = None
        self.bond_info = {}
        self.test_running = False
        self.test_result = None  # last test: solo per link, solo sum, combined
        self.diagnose_running = False
        self.diagnose_report = None
        self.live = {}
        self.history = deque(maxlen=180)
        self.log = deque(maxlen=300)
        # per-app breakdown
        self.proxy_obj = None
        self.conn_log = deque(maxlen=500)
        self.app_done = {}          # key -> [up_bytes, down_bytes, conns] (completed)
        self.app_live = {}          # key -> {"up": bps, "down": bps}
        self.app_total = {}         # key -> {"up", "down", "conns"} (done + active)
        self._port_names = {}       # peer port -> (process name, timestamp)
        self._port_refresh_ts = 0
        self._last_conn_seq = -1
        self._port_resolve_failed = False
        # one-task download state
        self.dl = None  # dict while a download runs / recently finished


state = State()


def log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    state.log.append(line)
    print(line, flush=True)


def load_config():
    try:
        with open(CONFIG) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_links():
    cfg = load_config()
    cfg["links"] = [{k: v for k, v in l.items() if not k.startswith("_")}
                    for l in state.links]
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)


# ---------------- background workers ----------------

def prober_loop():
    from speedtest import probe_link
    while True:
        for l in state.links:
            ok, ms = probe_link(l)
            new = "up" if ok else "down"
            if l.get("_status") != new:
                l["_status"] = new
                l["_latency"] = round(ms, 1) if ok else None
                log(f"{l.get('name')}: link {new}" +
                    (f" ({ms:.0f} ms)" if ok else ""))
        time.sleep(PROBE_INTERVAL)


def _app_key(c):
    """Identify a connection's app: process name if known, else destination host."""
    port = c.get("port") or 0
    name, _ts = state._port_names.get(port, (None, 0))
    if name and name != "unknown":
        return name
    host = (c.get("host") or "").split(":")[0]
    if host:
        return "\U0001f310 " + host
    return f"port {port}"


def _refresh_port_names():
    """Map client ports -> owning process names (Windows). Cached 15 s."""
    if sys.platform != "win32":
        return
    import subprocess
    ps = (
        "$cs = Get-NetTCPConnection -State Established |"
        " Where-Object { $_.LocalPort -eq 8080 };"
        "$o = @{}; foreach ($c in $cs) {"
        " if (-not $o.ContainsKey($c.RemotePort)) {"
        " $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue;"
        " $o[$c.RemotePort] = if ($p) { $p.ProcessName } else { 'unknown' } } };"
        "$o | ConvertTo-Json -Compress"
    )
    try:
        p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                            "-Command", ps],
                           capture_output=True, text=True, timeout=20)
        data = json.loads(p.stdout) if p.stdout.strip() else {}
        now = time.time()
        for port, name in data.items():
            state._port_names[int(port)] = (name, now)
    except Exception:
        if not state._port_resolve_failed:
            state._port_resolve_failed = True
            log("could not resolve app names (port -> process lookup failed)")


def sampler_loop():
    prev = None
    while True:
        time.sleep(1)
        totals = {}
        for l in state.links:
            totals[l["ip"]] = [0, 0]
        for ip, d in state.proxy_stats.items():
            t = totals.setdefault(ip, [0, 0])
            t[0] += d.get("up", 0)
            t[1] += d.get("down", 0)
        bc = state.bond_client
        if bc is not None:
            try:
                pipes = list(bc.session.pipes)
            except Exception:
                pipes = []
            for p in pipes:
                link = next((l for l in state.links if l.get("name") == p.name), None)
                if link is not None:
                    t = totals.setdefault(link["ip"], [0, 0])
                    t[0] += p.bytes_sent
                    t[1] += p.bytes_recv
        now = time.time()
        if prev is not None:
            dt = max(0.001, now - prev["ts"])
            live = {}
            for ip, (up, down) in totals.items():
                pu, pd = prev["totals"].get(ip, (up, down))
                live[ip] = {"up": max(0.0, (up - pu) * 8 / dt),
                            "down": max(0.0, (down - pd) * 8 / dt)}
            state.live = live
            state.history.append({"t": now, "links": live})
        # ---- per-app aggregation ----
        proxy = state.proxy_obj
        active_conns, new_done = [], []
        if proxy is not None:
            try:
                active_conns = list(proxy.active.values())
            except Exception:
                active_conns = []
            try:
                for c in proxy.conn_log:
                    if c.get("seq", -1) > state._last_conn_seq:
                        new_done.append(c)
            except Exception:
                pass
            if new_done:
                state._last_conn_seq = max(c["seq"] for c in new_done)
        if now - state._port_refresh_ts > 15:
            state._port_refresh_ts = now
            _refresh_port_names()
        for c in new_done:
            key = _app_key(c)
            t = state.app_done.setdefault(key, [0, 0, 0])
            t[0] += c["up"]
            t[1] += c["down"]
            t[2] += 1
        combined = {}
        for c in active_conns:
            key = _app_key(c)
            b = combined.setdefault(key, [0, 0, 0])
            b[0] += c["up"]
            b[1] += c["down"]
            b[2] += 1
        for key, (up, down, n) in state.app_done.items():
            b = combined.setdefault(key, [0, 0, 0])
            b[0] += up
            b[1] += down
            b[2] += n
        if prev is not None:
            dt = max(0.001, now - prev["ts"])
            prev_apps = prev.get("apps", {})
            live = {}
            for key, (up, down, _n) in combined.items():
                pu, pd = prev_apps.get(key, (up, down))
                live[key] = {"up": max(0.0, (up - pu) * 8 / dt),
                             "down": max(0.0, (down - pd) * 8 / dt)}
            state.app_live = live
        state.app_total = {k: {"up": b[0], "down": b[1], "conns": b[2]}
                           for k, b in combined.items()}
        prev = {"ts": now, "totals": totals,
                "apps": {k: (b[0], b[1]) for k, b in combined.items()}}


def do_detect():
    try:
        from links import detect_links
        links = detect_links()
        for l in links:
            l["_status"] = "probing"
        state.links = links
        save_links()
        log(f"detected {len(links)} link(s)")
        return {"ok": True, "count": len(links)}
    except Exception as exc:
        log(f"detect failed: {exc}")
        return {"ok": False, "error": str(exc)}


def do_test():
    if state.test_running:
        return {"ok": False, "error": "test already running"}
    state.test_running = True

    def work():
        try:
            from speedtest import test_links, test_aggregate
            urls = load_config().get("test_urls")
            log("speed test started (~10 MB per link)")
            results = test_links(state.links, urls=urls)
            for r in results:
                for l in state.links:
                    if l["ip"] == r["link"]["ip"]:
                        l["_speed"] = f"{r['mbps']:.1f} Mbps" if r["ok"] else "failed"
            ok = [r for r in results if r["ok"]]
            agg = None
            if len(ok) > 1:
                agg = test_aggregate([r["link"] for r in ok], urls=urls,
                                       speeds=[r["mbps"] for r in ok])
                log(f"combined throughput: {agg:.1f} Mbps")
            solo_sum = sum(r["mbps"] for r in ok)
            state.test_result = {
                "solo": [{"name": r["link"]["name"], "mbps": r["mbps"],
                          "ok": r["ok"]} for r in results],
                "solo_sum": solo_sum, "combined": agg,
                "ratio": (agg / solo_sum) if agg and solo_sum else None,
            }
            from balancer import auto_weight
            weights = auto_weight(results)
            if weights:
                for l in state.links:
                    if l["ip"] in weights:
                        l["weight"] = weights[l["ip"]]
                save_links()
                # Live-refresh a running proxy: LinkStates read the same dicts,
                # so just rebuild the pick pools (microseconds, no dropped conns).
                if state.proxy_obj is not None:
                    try:
                        state.proxy_obj.balancer.refresh_weights()
                    except Exception:
                        pass
                # Same for a running bond client: re-weight pipes, no reconnect.
                bc = state.bond_client
                if bc is not None:
                    try:
                        bc.session.update_pipe_weights(
                            {l["name"]: l.get("weight", 1.0) for l in state.links})
                    except Exception:
                        pass
                log("auto-weighted by measured speed: " +
                    ", ".join(f"{ip} -> {w}" for ip, w in weights.items()))
            else:
                log("auto-weight skipped (need 2+ successful link tests)")
            log("speed test done")
        except Exception as exc:
            log(f"speed test failed: {exc}")
        finally:
            state.test_running = False

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


def do_diagnose():
    """Run the combined-speed diagnosis in the background (see diagnose.py).

    The frontend polls /api/diagnose for the finished report; the button
    stays disabled while it runs, like the speed test.
    """
    if state.diagnose_running:
        return {"ok": False, "error": "diagnosis already running"}
    if state.test_running:
        return {"ok": False, "error": "a speed test is already running"}
    if not state.links:
        return {"ok": False, "error": "no links; run Detect first"}
    state.diagnose_running = True
    state.diagnose_report = None

    def work():
        try:
            from diagnose import run_diagnosis
            from speedtest import DEFAULT_URLS
            urls = load_config().get("test_urls") or DEFAULT_URLS
            report = run_diagnosis(state.links, urls)
            state.diagnose_report = report
            log(f"diagnosis done: solo sum "
                f"{report['solo_sum_mbps']:.1f} Mbps")
            for hint in report["hints"]:
                log(f"diagnosis: {hint}")
        except Exception as exc:
            log(f"diagnosis failed: {exc}")
        finally:
            state.diagnose_running = False

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


def _port_in_use(port):
    """True if something is already listening on 127.0.0.1:port."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def do_proxy_start():
    if state.proxy_running:
        return {"ok": False, "error": "proxy already running"}
    if not state.links:
        return {"ok": False, "error": "no links; run Detect first"}
    if _port_in_use(8080):
        return {"ok": False,
                "error": "Port 8080 already in use: a proxy is already "
                         "running outside the dashboard (for example from a "
                         "terminal). Stop that proxy first, or use it instead "
                         "of starting another one here."}
    from balancer import Balancer
    from proxy import MultiLinkProxy

    state.proxy_error = None

    def run():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            state.proxy_loop = loop
            proxy = MultiLinkProxy(Balancer(state.links), "127.0.0.1", 8080,
                                   stats=state.proxy_stats,
                                   conn_log=state.conn_log)
            state.proxy_obj = proxy
            loop.run_until_complete(proxy._serve())
        except Exception as exc:
            state.proxy_error = str(exc)
        finally:
            try:
                loop.close()
            except Exception:
                pass

    state.proxy_stats.clear()
    state.conn_log.clear()
    state.app_done.clear()
    state.app_live.clear()
    state.app_total.clear()
    state._last_conn_seq = -1
    threading.Thread(target=run, daemon=True).start()
    time.sleep(0.8)
    if state.proxy_error or state.proxy_loop is None:
        state.proxy_loop = None
        return {"ok": False, "error": state.proxy_error or "proxy failed to start"}
    state.proxy_running = True
    log("proxy started on 127.0.0.1:8080 — point apps/browsers at it")
    return {"ok": True}


def do_proxy_stop():
    if state.proxy_loop is not None:
        try:
            state.proxy_loop.call_soon_threadsafe(state.proxy_loop.stop)
        except Exception:
            pass
    state.proxy_running = False
    state.proxy_loop = None
    state.proxy_obj = None
    log("proxy stopped")
    return {"ok": True}


def do_bond_start(payload):
    if state.bond_running:
        return {"ok": False, "error": "bond client already running"}
    server = (payload.get("server") or "").strip()
    token = (payload.get("token") or "").strip()
    if not server or not token:
        return {"ok": False, "error": "server and token are required"}
    if not state.links:
        return {"ok": False, "error": "no links; run Detect first"}
    try:
        port = int(payload.get("port") or 9000)
    except (TypeError, ValueError):
        return {"ok": False, "error": "bad port"}
    fingerprint = (payload.get("fingerprint") or "").strip() or None
    from bond_client import BondClient

    state.bond_error = None
    state.bond_info = {"server": server, "port": port,
                       "tls": bool(fingerprint)}

    def run():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            state.bond_loop = loop
            client = BondClient(server, port, token, state.links, 1080,
                                tls_fingerprint=fingerprint)
            state.bond_client = client
            loop.run_until_complete(client.run())
        except Exception as exc:
            state.bond_error = str(exc)
        finally:
            try:
                loop.close()
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()
    time.sleep(1.0)
    if state.bond_error:
        state.bond_client = None
        state.bond_loop = None
        return {"ok": False, "error": state.bond_error}
    state.bond_running = True
    log(f"bond client started -> {server}:{port} (SOCKS5 on 127.0.0.1:1080)")
    return {"ok": True}


def do_download_start(payload):
    """Download ONE file with all links at once (segmented, no proxy needed)."""
    if state.dl and state.dl.get("active"):
        return {"ok": False, "error": "a download is already running"}
    url = (payload.get("url") or "").strip()
    if not url:
        return {"ok": False, "error": "paste a file URL first"}
    if not state.links:
        return {"ok": False, "error": "no links; run Detect first"}
    from downloader import download, filename_for
    name = filename_for((payload.get("name") or "").strip() or url)
    dl_dir = os.path.expanduser("~/Downloads")
    if not os.path.isdir(dl_dir):
        dl_dir = os.getcwd()
    dest = os.path.join(dl_dir, name)
    base, ext = os.path.splitext(dest)
    i = 1
    while os.path.exists(dest):
        i += 1
        dest = f"{base} ({i}){ext}"
    st = {"active": True, "url": url, "dest": dest, "done": 0, "total": None,
          "per_link": {}, "started": time.time(), "status": "running",
          "error": None, "mbps": 0.0}
    state.dl = st
    links = state.links

    def work():
        try:
            def cb(done, total, per_link, elapsed):
                st["done"] = done
                st["total"] = total
                st["per_link"] = per_link
                st["mbps"] = (done * 8 / 1e6 / elapsed) if elapsed > 0 else 0.0
            log(f"one-task download started ({len(links)} links): {url}")
            _, nbytes, secs = download(url, dest, links, segments=16,
                                       progress_cb=cb)
            st["status"] = "done"
            st["mbps"] = (nbytes * 8 / 1e6 / secs) if secs > 0 else 0.0
            log(f"download done: {os.path.basename(dest)} "
                f"({nbytes/1e6:.1f} MB at {st['mbps']:.1f} Mbps)")
        except Exception as exc:
            st["status"] = "error"
            st["error"] = str(exc)
            log(f"download failed: {exc}")
        finally:
            st["active"] = False

    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "dest": dest}


def do_bond_stop():
    if state.bond_loop is not None:
        try:
            state.bond_loop.call_soon_threadsafe(state.bond_loop.stop)
        except Exception:
            pass
    state.bond_running = False
    state.bond_loop = None
    state.bond_client = None
    log("bond client stopped")
    return {"ok": True}


def status_snapshot():
    links = []
    for l in state.links:
        links.append({
            "kind": l.get("kind"), "name": l.get("name"), "ip": l.get("ip"),
            "status": l.get("_status", "?"), "latency": l.get("_latency"),
            "speed": l.get("_speed", ""), "weight": l.get("weight", 1.0),
            "live": state.live.get(l.get("ip"), {"up": 0, "down": 0}),
        })
    pipes = []
    bc = state.bond_client
    if bc is not None:
        try:
            for p in list(bc.session.pipes):
                pipes.append({"name": p.name, "up": p.bytes_sent,
                              "down": p.bytes_recv})
        except Exception:
            pass
    return {
        "links": links,
        "proxy": {"running": state.proxy_running, "port": 8080},
        # External proxy: something on :8080 that this dashboard did not
        # start. Its traffic is invisible to this dashboard's graphs, so
        # the UI must say so instead of showing a misleading 0 bps.
        "external_proxy": (not state.proxy_running and _port_in_use(8080)),
        "test_result": state.test_result,
        "diagnosing": state.diagnose_running,
        "bond": {"running": state.bond_running, "pipes": pipes,
                 "info": state.bond_info},
        "testing": state.test_running,
        "history": list(state.history),
    }


# ---------------- HTTP ----------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                data = open(HTML_FILE, "rb").read()
            except OSError:
                self.send_error(500, "dashboard.html missing")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path == "/api/status":
            self._json(status_snapshot())
        elif self.path == "/api/log":
            self._json({"log": list(state.log)})
        elif self.path == "/api/apps":
            apps = []
            for key, live in state.app_live.items():
                t = state.app_total.get(key, {})
                apps.append({"name": key,
                             "down_bps": live["down"], "up_bps": live["up"],
                             "down_total": t.get("down", 0),
                             "up_total": t.get("up", 0),
                             "conns": t.get("conns", 0)})
            seen = set(state.app_live)
            for key, t in state.app_total.items():
                if key not in seen:
                    apps.append({"name": key, "down_bps": 0, "up_bps": 0,
                                 "down_total": t["down"], "up_total": t["up"],
                                 "conns": t["conns"]})
            apps.sort(key=lambda a: a["down_bps"] + a["up_bps"], reverse=True)
            self._json({"apps": apps[:12]})
        elif self.path == "/api/download":
            self._json(state.dl or {"active": False})
        elif self.path == "/api/diagnose":
            self._json({"running": state.diagnose_running,
                        "report": state.diagnose_report})
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else {}
        except ValueError:
            payload = {}
        routes = {
            "/api/detect": do_detect,
            "/api/test": do_test,
            "/api/diagnose": do_diagnose,
            "/api/proxy/start": do_proxy_start,
            "/api/proxy/stop": do_proxy_stop,
            "/api/bond/stop": do_bond_stop,
        }
        if self.path == "/api/bond/start":
            self._json(do_bond_start(payload))
        elif self.path == "/api/download/start":
            self._json(do_download_start(payload))
        elif self.path in routes:
            self._json(routes[self.path]())
        else:
            self.send_error(404)


def main():
    ap = argparse.ArgumentParser(description="MultiLink Booster web dashboard.")
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    state.links = cfg.get("links", [])
    for l in state.links:
        l.setdefault("_status", "?")

    threading.Thread(target=prober_loop, daemon=True).start()
    threading.Thread(target=sampler_loop, daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    log(f"dashboard at {url}")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\ndashboard stopped")


if __name__ == "__main__":
    main()
