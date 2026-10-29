#!/usr/bin/env python3
"""Desktop control panel for multilink-booster (tkinter, stdlib only).

Run:  py gui.py

Shows your links with live up/down status, runs speed tests, starts/stops
the load-balancing proxy, runs the combined-speed diagnosis, and downloads
one file with all links at once. Speed tests download ~10MB per link plus
the aggregate test, so avoid them on metered tethering if data is tight.

Note: this window does not have the dashboard's live chart or per-app
breakdown -- the dashboard's proxy runs in-process, so it can feed those
graphs, while this window runs the proxy as a separate process. What it
does share with the dashboard: detect, test, diagnose, one-task download,
and the solo-vs-combined result panel.
"""
import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
CONFIG = os.path.join(BASE, "config.json")

try:
    import tkinter as tk
    from tkinter import ttk, messagebox
except ImportError:
    sys.exit("tkinter is not available in this Python.\n"
             "Use the command line instead:\n"
             "  py multilink.py detect | test | proxy")

REFRESH_S = 15  # link status probe interval


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MultiLink Booster")
        self.geometry("620x660")

        self.links = []
        self.proxy_proc = None
        self.dl_running = False
        self.msg_q = queue.Queue()

        cols = ("kind", "adapter", "ip", "status", "speed")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=6)
        for c, w, label in (("kind", 70, "Kind"), ("adapter", 170, "Adapter"),
                            ("ip", 120, "IP"), ("status", 100, "Status"),
                            ("speed", 90, "Speed")):
            self.tree.heading(c, text=label)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="x", padx=10, pady=(10, 4))

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=4)
        self.btn_detect = ttk.Button(bar, text="Detect links", command=self.on_detect)
        self.btn_test = ttk.Button(bar, text="Test speeds", command=self.on_test)
        self.btn_proxy = ttk.Button(bar, text="Start proxy", command=self.on_proxy_toggle)
        self.btn_diagnose = ttk.Button(bar, text="Diagnose", command=self.on_diagnose)
        for b in (self.btn_detect, self.btn_test, self.btn_proxy,
                  self.btn_diagnose):
            b.pack(side="left", padx=4)

        ttk.Label(self,
                  text="While the proxy runs, set browser / Windows proxy to 127.0.0.1:8080.",
                  foreground="gray").pack(anchor="w", padx=10)
        self.lbl_proxy = ttk.Label(self, text="Proxy: stopped",
                                   foreground="gray")
        self.lbl_proxy.pack(anchor="w", padx=10)
        self.lbl_result = ttk.Label(
            self, text="No test yet — Test speeds shows solo sum vs combined here.",
            foreground="gray", wraplength=600)
        self.lbl_result.pack(anchor="w", padx=10)

        dl_bar = ttk.Frame(self)
        dl_bar.pack(fill="x", padx=10, pady=4)
        ttk.Label(dl_bar, text="One-task download:").pack(side="left")
        self.dl_url = ttk.Entry(dl_bar, width=40)
        self.dl_url.pack(side="left", padx=4, fill="x", expand=True)
        self.btn_dl = ttk.Button(dl_bar, text="Download", command=self.on_download)
        self.btn_dl.pack(side="left", padx=4)
        self.lbl_dl = ttk.Label(self, text="", foreground="gray")
        self.lbl_dl.pack(anchor="w", padx=10)

        self.log = tk.Text(self, height=14, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, padx=10, pady=8)

        self.load_config()
        self.refresh_table()
        self.after(200, self.pump_queue)
        threading.Thread(target=self.probe_loop, daemon=True).start()
        self.log_msg("Ready. Click 'Detect links' if the table is empty.")

    # ---------------- plumbing ----------------
    def log_msg(self, msg):
        self.msg_q.put(("log", f"[{time.strftime('%H:%M:%S')}] {msg}"))

    def pump_queue(self):
        try:
            while True:
                kind, payload = self.msg_q.get_nowait()
                if kind == "log":
                    self.log.configure(state="normal")
                    self.log.insert("end", payload + "\n")
                    self.log.see("end")
                    self.log.configure(state="disabled")
                elif kind == "table":
                    self.refresh_table()
                elif kind == "result":
                    self.lbl_result.configure(text=payload)
                elif kind == "proxy":
                    self.lbl_proxy.configure(text=payload)
                elif kind == "dl":
                    self.lbl_dl.configure(text=payload)
                elif kind == "busy":
                    state = "disabled" if payload else "normal"
                    self.btn_detect.configure(state=state)
                    self.btn_test.configure(state=state)
                    self.btn_diagnose.configure(state=state)
        except queue.Empty:
            pass
        self.after(200, self.pump_queue)

    def load_config(self):
        try:
            with open(CONFIG) as f:
                self.links = json.load(f).get("links", [])
        except (OSError, ValueError):
            self.links = []

    def save_config(self):
        try:
            with open(CONFIG) as f:
                cfg = json.load(f)
        except (OSError, ValueError):
            cfg = {}
        cfg["links"] = self.links
        with open(CONFIG, "w") as f:
            json.dump(cfg, f, indent=2)

    @staticmethod
    def _port_in_use(port):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            return False

    def _download_dest(self, name):
        dl_dir = os.path.expanduser("~/Downloads")
        if not os.path.isdir(dl_dir):
            dl_dir = os.getcwd()
        dest = os.path.join(dl_dir, name)
        base, ext = os.path.splitext(dest)
        i = 1
        while os.path.exists(dest):
            i += 1
            dest = f"{base} ({i}){ext}"
        return dest

    def refresh_table(self):
        for row in self.tree.get_children():
            self.tree.delete(row)
        for l in self.links:
            self.tree.insert("", "end", values=(
                l.get("kind", ""), l.get("name", ""), l.get("ip", ""),
                l.get("_status", "?"), l.get("_speed", "")))

    # ---------------- actions ----------------
    def on_detect(self):
        def work():
            self.msg_q.put(("busy", True))
            try:
                from links import detect_links
                links = detect_links()
                for l in links:
                    l["_status"] = "probing..."
                self.links = links
                self.save_config()
                self.msg_q.put(("table", None))
                self.log_msg(f"Detected {len(links)} link(s).")
            except Exception as exc:
                self.log_msg(f"Detect failed: {exc}")
            finally:
                self.msg_q.put(("busy", False))
        threading.Thread(target=work, daemon=True).start()

    def probe_loop(self):
        from speedtest import probe_link
        while True:
            for l in self.links:
                ok, ms = probe_link(l)
                new = f"up ({ms:.0f} ms)" if ok else "DOWN"
                if l.get("_status") != new:
                    l["_status"] = new
                    self.msg_q.put(("table", None))
                    self.log_msg(f"{l.get('name')}: {new}")
            time.sleep(REFRESH_S)

    def on_test(self):
        def work():
            self.msg_q.put(("busy", True))
            try:
                from speedtest import test_links, test_aggregate
                with open(CONFIG) as f:
                    urls = json.load(f).get("test_urls")
                self.log_msg("Measuring per-link speeds (~10 MB per link)...")
                results = test_links(self.links, urls=urls)
                for r in results:
                    for l in self.links:
                        if l["ip"] == r["link"]["ip"]:
                            l["_speed"] = f"{r['mbps']:.1f} Mbps" if r["ok"] else "failed"
                self.msg_q.put(("table", None))
                ok = [r for r in results if r["ok"]]
                solo_sum = sum(r["mbps"] for r in ok)
                if len(ok) > 1:
                    agg = test_aggregate([r["link"] for r in ok], urls=urls,
                                           speeds=[r["mbps"] for r in ok])
                    self.log_msg(f"Combined throughput: {agg:.1f} Mbps")
                    result = (f"Solo sum: {solo_sum:.1f} Mbps · Combined: "
                              f"{agg:.1f} Mbps "
                              f"({agg / solo_sum:.2f}x of solo sum)"
                              if solo_sum else "Combined measured, no solo sum.")
                    self.msg_q.put(("result", result))
                    self.log_msg(result)
                elif ok:
                    self.msg_q.put(("result",
                                    f"Only one working link: {solo_sum:.1f} Mbps"
                                    " — nothing to combine yet."))
                from balancer import auto_weight
                weights = auto_weight(results)
                if weights:
                    with open(CONFIG) as f:
                        cfg = json.load(f)
                    for l in cfg.get("links", []):
                        if l["ip"] in weights:
                            l["weight"] = weights[l["ip"]]
                    with open(CONFIG, "w") as f:
                        json.dump(cfg, f, indent=2)
                    self.log_msg("Auto-weighted by measured speed: " +
                                 ", ".join(f"{ip} -> {w}" for ip, w in weights.items()) +
                                 " (applies on next proxy start)")
                    if self.proxy_proc and self.proxy_proc.poll() is None:
                        self.log_msg("Proxy is running — restart it to apply "
                                     "the new weights (this window's proxy is "
                                     "a separate process, so it cannot be "
                                     "re-weighted live like the dashboard's).")
                self.log_msg("Done.")
            except Exception as exc:
                self.log_msg(f"Test failed: {exc}")
            finally:
                self.msg_q.put(("busy", False))
        threading.Thread(target=work, daemon=True).start()

    def on_proxy_toggle(self):
        if self.proxy_proc and self.proxy_proc.poll() is None:
            self.proxy_proc.terminate()
            self.proxy_proc = None
            self.btn_proxy.configure(text="Start proxy")
            self.lbl_proxy.configure(text="Proxy: stopped")
            self.log_msg("Proxy stopped.")
            return
        if not self.links:
            messagebox.showinfo("No links", "Detect links first.")
            return
        if self._port_in_use(8080):
            # A proxy started elsewhere (terminal / dashboard) -- starting
            # another one would just fail with a port error, invisibly.
            self.lbl_proxy.configure(
                text="Proxy: already running outside this window on :8080")
            self.log_msg("Port 8080 already in use: a proxy is already "
                         "running outside this window. Stop it first, or "
                         "use it instead of starting another one here.")
            return
        try:
            # PIPE, not DEVNULL: previously a proxy that exited immediately
            # left no trace at all. A reader thread forwards its output
            # to the log so startup errors are visible.
            self.proxy_proc = subprocess.Popen(
                [sys.executable, os.path.join(BASE, "multilink.py"), "proxy"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1)
        except Exception as exc:
            self.log_msg(f"Could not start proxy: {exc}")
            return

        def _pump_output(proc):
            try:
                for line in proc.stdout:
                    self.msg_q.put(("log", line.rstrip()))
            except Exception:
                pass

        threading.Thread(target=_pump_output, args=(self.proxy_proc,),
                         daemon=True).start()
        time.sleep(0.5)
        if self.proxy_proc.poll() is not None:
            self.log_msg("Proxy exited immediately - see its output above.")
            self.proxy_proc = None
            return
        self.btn_proxy.configure(text="Stop proxy")
        self.lbl_proxy.configure(text="Proxy: running on 127.0.0.1:8080")
        self.log_msg("Proxy running on 127.0.0.1:8080.")

    def on_diagnose(self):
        def work():
            self.msg_q.put(("busy", True))
            try:
                from diagnose import run_diagnosis
                from speedtest import DEFAULT_URLS
                try:
                    with open(CONFIG) as f:
                        urls = json.load(f).get("test_urls") or DEFAULT_URLS
                except (OSError, ValueError):
                    urls = DEFAULT_URLS
                self.log_msg("Diagnosing combined speed — this takes a "
                             "minute or two on real links...")
                report = run_diagnosis(self.links, urls)
                dest = os.path.join(BASE, "diagnose-report.json")
                with open(dest, "w") as f:
                    json.dump(report, f, indent=2)
                self.log_msg(f"Diagnosis done: solo sum "
                             f"{report['solo_sum_mbps']:.1f} Mbps. "
                             f"Report saved to {dest}")
                for var in report["variants"]:
                    if var.get("status") in ("ok", "partial"):
                        self.log_msg(f"  {var['mode']}: {var['mbps']:.1f} Mbps")
                    else:
                        self.log_msg(f"  {var['mode']}: {var['status']}")
                for hint in report["hints"]:
                    self.log_msg(f"  • {hint}")
            except Exception as exc:
                self.log_msg(f"Diagnosis failed: {exc}")
            finally:
                self.msg_q.put(("busy", False))
        threading.Thread(target=work, daemon=True).start()

    def on_download(self):
        url = self.dl_url.get().strip()
        if not url:
            self.log_msg("Paste a file URL first.")
            return
        if not self.links:
            self.log_msg("Detect links first.")
            return
        if self.dl_running:
            self.log_msg("A download is already running.")
            return
        self.dl_running = True

        def work():
            try:
                from downloader import download, filename_for
                dest = self._download_dest(filename_for(url))
                self.log_msg(f"Downloading one file with all links: {url}")

                def cb(done, total, _per_link, elapsed):
                    rate = (done * 8 / 1e6 / elapsed) if elapsed > 0 else 0.0
                    if total:
                        self.msg_q.put(("dl", f"{done / total * 100:.1f}% — "
                                              f"{done / 1e6:.1f}/{total / 1e6:.1f} MB"
                                              f" — {rate:.1f} Mbps"))
                    else:
                        self.msg_q.put(("dl", f"{done / 1e6:.1f} MB — "
                                              f"{rate:.1f} Mbps"))
                _dest, nbytes, secs = download(url, dest, self.links,
                                               segments=16, progress_cb=cb)
                mbps = (nbytes * 8 / 1e6 / secs) if secs > 0 else 0.0
                self.msg_q.put(("dl", f"Done: {os.path.basename(dest)} "
                                      f"({nbytes / 1e6:.1f} MB at {mbps:.1f} Mbps)"))
                self.log_msg(f"Download done: {dest} ({mbps:.1f} Mbps)")
            except Exception as exc:
                self.msg_q.put(("dl", f"Failed: {exc}"))
                self.log_msg(f"Download failed: {exc}")
            finally:
                self.dl_running = False
        threading.Thread(target=work, daemon=True).start()

    def on_close(self):
        if self.proxy_proc and self.proxy_proc.poll() is None:
            self.proxy_proc.terminate()
        self.destroy()


if __name__ == "__main__":
    app = App()
    app.protocol("WM_DELETE_WINDOW", app.on_close)
    app.mainloop()
