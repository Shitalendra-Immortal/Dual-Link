#!/usr/bin/env python3
"""multilink-booster: combine Wi-Fi + USB tether/cable bandwidth on Windows.

Usage (run from this folder):
    py multilink.py detect            find links, save config.json
    py multilink.py test              measure per-link + combined throughput
    py multilink.py test --save-weights
    py multilink.py proxy             start the load-balancing proxy
    py multilink.py download <url>    download ONE file using all links at once
    py multilink.py diagnose          why combined speed is below the solo sum

Needs Python 3.9+ and no extra packages.
"""
import argparse
import json
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
CONFIG = os.path.join(BASE, "config.json")

DEFAULT_TEST_URLS = [
    "https://proof.ovh.net/files/10Mb.dat",
    "http://ipv4.download.thinkbroadband.com/10MB.zip",
]


def cmd_detect(_args):
    from links import detect_links, print_links
    links = detect_links()
    print_links(links)
    if not links:
        sys.exit(1)
    cfg = {
        "links": links,
        "proxy_host": "127.0.0.1",
        "proxy_port": 8080,
        "test_urls": DEFAULT_TEST_URLS,
    }
    with open(CONFIG, "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"\nSaved {len(links)} link(s) to config.json")
    print("Next: run 'py multilink.py test' to measure your links.")


def load_config():
    if not os.path.exists(CONFIG):
        sys.exit("config.json not found. Run 'py multilink.py detect' first.")
    with open(CONFIG) as f:
        return json.load(f)


def cmd_proxy(_args):
    from balancer import Balancer
    from proxy import MultiLinkProxy
    cfg = load_config()
    if not cfg.get("links"):
        sys.exit("No links in config.json. Run 'py multilink.py detect' first.")
    proxy = MultiLinkProxy(
        Balancer(cfg["links"]),
        cfg.get("proxy_host", "127.0.0.1"),
        cfg.get("proxy_port", 8080),
    )
    proxy.run()


def cmd_test(args):
    from speedtest import test_links, test_aggregate
    cfg = load_config()
    links = cfg.get("links") or []
    if not links:
        sys.exit("No links in config.json. Run 'py multilink.py detect' first.")
    urls = cfg.get("test_urls") or DEFAULT_TEST_URLS

    results = test_links(links, urls=urls, per_link_bytes=args.bytes)
    ok = [r for r in results if r["ok"]]
    if not ok:
        print("\nNo working links - cannot run the aggregate test.")
        sys.exit(1)
    if len(ok) > 1:
        test_aggregate([r["link"] for r in ok], urls=urls,
                       workers_per_link=args.workers,
                       bytes_per_worker=args.aggregate_bytes,
                       speeds=[r["mbps"] for r in ok])
    else:
        print("\nOnly one working link - nothing to combine yet.")

    if args.save_weights and ok:
        from balancer import auto_weight
        weights = auto_weight(results)
        for r in results:
            ip = r["link"]["ip"]
            if ip in weights:
                r["link"]["weight"] = weights[ip]
        cfg["links"] = [r["link"] for r in results]
        with open(CONFIG, "w") as f:
            json.dump(cfg, f, indent=2)
        print("\nSaved measured weights to config.json - the proxy will use them.")


def cmd_download(args):
    from downloader import download, filename_for
    cfg = load_config()
    links = cfg.get("links") or []
    if not links:
        sys.exit("No links in config.json. Run 'py multilink.py detect' first.")
    dest = args.output or filename_for(args.url)
    dest = os.path.join(os.getcwd(), dest)
    print(f"Downloading one file with all {len(links)} link(s), "
          f"{args.segments} segments:\n  {args.url}\n  -> {dest}")

    def cb(done, total, _per_link, elapsed):
        rate = (done * 8 / 1e6 / elapsed) if elapsed > 0 else 0.0
        if total:
            pct = done / total * 100
            sys.stdout.write(f"\r  {pct:5.1f}%  {done/1e6:8.1f}/{total/1e6:.1f} MB"
                             f"  {rate:6.1f} Mbps   ")
        else:
            sys.stdout.write(f"\r  {done/1e6:8.1f} MB  {rate:6.1f} Mbps   ")
        sys.stdout.flush()

    try:
        dest, nbytes, secs = download(args.url, dest, links,
                                      segments=args.segments, progress_cb=cb)
    except KeyboardInterrupt:
        print("\ncancelled - partial file kept")
        sys.exit(1)
    except RuntimeError as exc:
        print(f"\ndownload failed: {exc}")
        sys.exit(1)
    mbps = (nbytes * 8 / 1e6 / secs) if secs > 0 else 0.0
    print(f"\nDone: {nbytes/1e6:.1f} MB in {secs:.1f}s = {mbps:.1f} Mbps")


def cmd_diagnose(args):
    """Diagnose combined speed: interleaved solos + aggregate variants.

    Saves a JSON report (default diagnose-report.json in the current
    folder) so the numbers can be compared across runs or pasted back
    for interpretation, instead of relying on one printed line.
    """
    from diagnose import format_report, run_diagnosis
    cfg = load_config()
    links = cfg.get("links") or []
    if not links:
        sys.exit("No links in config.json. Run 'py multilink.py detect' first.")
    urls = [args.url] if args.url else (
        cfg.get("test_urls") or DEFAULT_TEST_URLS)
    print("Diagnosing combined speed "
          f"({args.repeats} interleaved solo repeats + 3 aggregate variants; "
          "give it a minute or two on real links)...\n")
    report = run_diagnosis(
        links, urls, repeats=args.repeats, per_link_bytes=args.bytes,
        warmup_bytes=args.warmup_bytes, workers_per_link=args.workers,
        aggregate_bytes=args.aggregate_bytes)
    print(format_report(report))
    dest = args.output or os.path.join(os.getcwd(), "diagnose-report.json")
    with open(dest, "w") as f:
        json.dump(report, f, indent=2)
    print(f"\nReport saved to {dest}")


def main():
    ap = argparse.ArgumentParser(description="Combine Wi-Fi + tether/cable bandwidth on Windows.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("detect", help="Find usable links and save config.json")

    t = sub.add_parser("test", help="Measure per-link and combined throughput")
    t.add_argument("--bytes", type=int, default=10_000_000,
                   help="Bytes to download per link (default 10MB)")
    t.add_argument("--aggregate-bytes", type=int, default=8_000_000,
                   help="Bytes per worker in the aggregate test (default 8MB)")
    t.add_argument("--workers", type=int, default=2,
                   help="Parallel downloads per link in the aggregate test (default 2)")
    t.add_argument("--save-weights", action="store_true",
                   help="Save measured speeds as proxy weights in config.json")

    sub.add_parser("proxy", help="Start the load-balancing proxy on 127.0.0.1:8080")

    d = sub.add_parser("download", help="Download ONE file using all links at once")
    d.add_argument("url", help="http(s) URL of the file")
    d.add_argument("-o", "--output", default=None,
                   help="Save as this filename (default: from URL)")
    d.add_argument("--segments", type=int, default=16,
                   help="Parallel segments (default 16)")

    g = sub.add_parser("diagnose",
                       help="Diagnose why combined speed is below the solo sum")
    g.add_argument("--url", default=None,
                   help="Test file URL (default: test_urls from config.json)")
    g.add_argument("--repeats", type=int, default=3,
                   help="Interleaved solo repeats per link (default 3)")
    g.add_argument("--bytes", type=int, default=10_000_000,
                   help="Measured bytes per solo run, after warmup (default 10MB)")
    g.add_argument("--warmup-bytes", type=int, default=1_000_000,
                   help="Bytes discarded per measured fetch, same connection "
                        "(default 1MB)")
    g.add_argument("--workers", type=int, default=2,
                   help="Parallel workers per link in aggregates (default 2)")
    g.add_argument("--aggregate-bytes", type=int, default=8_000_000,
                   help="Bytes per worker in aggregate variants (default 8MB)")
    g.add_argument("--output", default=None,
                   help="JSON report path (default: diagnose-report.json)")

    args = ap.parse_args()
    {"detect": cmd_detect, "test": cmd_test, "proxy": cmd_proxy,
     "download": cmd_download, "diagnose": cmd_diagnose}[args.cmd](args)


if __name__ == "__main__":
    main()
