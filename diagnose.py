"""Diagnose why combined speed is below the sum of the links.

This does NOT try to fix anything. It runs, back to back in one session,
the measurements needed to separate four different explanations:

  1. Fluctuation: solo speeds measured at different times are not comparable.
     Fixed by interleaving repeats (Wi-Fi, tether, Wi-Fi, tether, ...) and
     reporting median + min/max per link instead of one shot.
  2. Parallel-connection degradation: parallel downloads on ONE link alone
     already lose speed. If so, the bottleneck is server per-connection
     throttling or local CPU, not combining links.
  3. Same-object throttling: every aggregate worker fetching the SAME file
     from the start is slower than workers fetching DISTINCT byte ranges.
     If so, the test server, not your links, is the limiter.
  4. Combining itself: only if solo, single-parallel, and distinct-range
     aggregates all look fine but the same-file aggregate does not -- or
     all of them stay low -- is there something left to blame on combining.

Warmup: each measured fetch discards warmup_bytes in the same connection
before the clock starts (see speedtest.fetch_bytes), so TCP slow-start does
not pollute the numbers. Aggregate speeds use the measured-phase window
(max worker end - min worker start), not wall-clock including warmup.

All interpretation in interpret() is heuristic, labelled as such, with
ratios printed so the user can judge for themselves. Stdlib only.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from speedtest import (RangeIgnoredError, fetch_bytes, median,
                       pick_url_for_links, summarize_runs)


def _mbps(nbytes, seconds):
    return (nbytes * 8 / 1e6 / seconds) if seconds > 0 else 0.0


# ---------------- solo: interleaved repeats ----------------

def solo_repeats(links, url, repeats=3, per_link_bytes=10_000_000,
                 warmup_bytes=1_000_000):
    """Interleaved solo runs. Returns {link_name: {runs, summary, ...}}.

    Interleaving (A, B, A, B, ...) matters: measuring all of A first and
    then all of B lets a slow drift in Wi-Fi masquerade as a link difference.
    """
    out = {}
    for link in links:
        out[link["name"]] = {"link": link, "runs": [], "errors": []}
    for _rep in range(max(1, repeats)):
        for link in links:
            entry = out[link["name"]]
            try:
                info = fetch_bytes(url, src_ip=link["ip"],
                                   max_bytes=per_link_bytes,
                                   warmup_bytes=warmup_bytes,
                                   return_info=True)
                speed = _mbps(info["bytes"], info["seconds"])
                entry["runs"].append(
                    {"mbps": speed, "bytes": info["bytes"],
                     "seconds": info["seconds"],
                     "full": info["bytes"] >= per_link_bytes})
                if info["bytes"] < per_link_bytes:
                    entry["errors"].append(
                        "server file smaller than requested bytes "
                        f"({info['bytes']} of {per_link_bytes})")
            except Exception as exc:
                entry["errors"].append(str(exc))
    for name, entry in out.items():
        entry["summary"] = summarize_runs([r["mbps"] for r in entry["runs"]])
        entry["median"] = entry["summary"].get("median", 0.0)
    return out


# ---------------- aggregate variants ----------------

def _worker_fetch(url, link, max_bytes, byte_range, warmup_bytes):
    """One aggregate worker. Returns a dict, never raises for a bad worker."""
    try:
        info = fetch_bytes(url, src_ip=link["ip"], max_bytes=max_bytes,
                           byte_range=byte_range,
                           warmup_bytes=warmup_bytes, return_info=True)
        if byte_range is not None and info["status"] != 206:
            return {"link": link["name"], "bytes": 0, "ok": False,
                    "error": f"expected 206, got {info['status']}"}
        return {"link": link["name"], "bytes": info["bytes"], "ok": True,
                "t0": info["t0"], "t1": info["t1"], "error": None}
    except RangeIgnoredError:
        return {"link": link["name"], "bytes": 0, "ok": False,
                "error": "range_ignored", "range_ignored": True}
    except Exception as exc:
        return {"link": link["name"], "bytes": 0, "ok": False,
                "error": str(exc)}


def _aggregate(url, links, solo, total_budget, workers_per_link,
               warmup_bytes, mode, file_size=None):
    """Run one aggregate variant. Mode: 'same', 'range', or 'single'.

    'same': every worker fetches the file from the start (current test).
    'range': every worker fetches one distinct byte range of the file.
    'single': each link is run alone, in parallel, one at a time; the
    returned mbps is the SUM of the per-link parallel speeds (the control).
    """
    if mode == "range":
        if not file_size or file_size <= 0:
            return {"mode": mode, "status": "skipped",
                    "note": "file size unknown, cannot plan distinct ranges"}
        total = min(total_budget, file_size)
        note = ("distinct ranges cover "
                f"{total / 1e6:.1f} MB of a {file_size / 1e6:.1f} MB file"
                + (" (capped at file size)" if total < total_budget else ""))
    else:
        total = total_budget
        note = ""
    speeds = [solo[l["name"]]["median"] for l in links]
    total_speed = sum(max(s or 0, 1e-6) for s in speeds)

    # Worker plan: (link, max_bytes, byte_range or None), ranges sequential.
    jobs = []
    cursor = 0
    for link, speed in zip(links, speeds):
        share = max(speed or 0, 1e-6) / total_speed
        n_workers = max(1, round(workers_per_link * len(links) * share))
        per_worker = max(64_000, round(total * share / n_workers))
        for _ in range(n_workers):
            if mode == "range":
                end = min(total - 1, cursor + per_worker - 1)
                jobs.append((link, per_worker, (cursor, end)))
                cursor = end + 1
            else:
                jobs.append((link, per_worker, None))
    if mode == "range" and jobs:
        # Last worker takes any rounding remainder so ranges cover `total`.
        link, nbytes, (s, _e) = jobs[-1]
        jobs[-1] = (link, nbytes, (s, total - 1))

    if mode == "single":
        # Run each link alone; sum the per-link parallel speeds.
        per_link_mbps = {}
        workers_all = []
        failed = 0
        for link in links:
            link_jobs = [(l, nb, br) for (l, nb, br) in jobs
                         if l["name"] == link["name"]]
            with ThreadPoolExecutor(max_workers=len(link_jobs)) as ex:
                futs = [ex.submit(_worker_fetch, url, l, nb, br, warmup_bytes)
                        for (l, nb, br) in link_jobs]
                results = [f.result() for f in futs]
            workers_all.extend(results)
            ok = [r for r in results if r["ok"]]
            failed += len(results) - len(ok)
            if ok:
                wall = max(r["t1"] for r in ok) - min(r["t0"] for r in ok)
                per_link_mbps[link["name"]] = _mbps(
                    sum(r["bytes"] for r in ok), wall)
            else:
                per_link_mbps[link["name"]] = 0.0
        return {"mode": mode, "status": "ok" if failed == 0 else "partial",
                "mbps": sum(per_link_mbps.values()),
                "per_link_mbps": per_link_mbps,
                "workers": workers_all,
                "failed_workers": failed, "note": note}

    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
        futs = {ex.submit(_worker_fetch, url, l, nb, br, warmup_bytes): (l, nb, br)
                for (l, nb, br) in jobs}
        results = [f.result() for f in as_completed(futs)]
    if any(r.get("range_ignored") for r in results):
        return {"mode": mode, "status": "range_unsupported",
                "note": "server answered 200 to Range requests, so distinct "
                        "ranges are not actually distinct; this variant is "
                        "invalid for this server, not a link result",
                "workers": results, "failed_workers": len(results)}
    ok = [r for r in results if r["ok"]]
    failed = len(results) - len(ok)
    if not ok:
        return {"mode": mode, "status": "failed", "mbps": 0.0,
                "workers": results, "failed_workers": failed, "note": note}
    wall = max(r["t1"] for r in ok) - min(r["t0"] for r in ok)
    total_bytes = sum(r["bytes"] for r in ok)
    per_link = {}
    for r in ok:
        per_link[r["link"]] = per_link.get(r["link"], 0) + r["bytes"]
    return {"mode": mode, "status": "ok" if failed == 0 else "partial",
            "mbps": _mbps(total_bytes, wall),
            "total_bytes": total_bytes, "wall_seconds": wall,
            "per_link_bytes": per_link, "workers": results,
            "failed_workers": failed, "note": note}


# ---------------- interpretation (heuristic, labelled) ----------------

def interpret(solo, variants):
    """Heuristic hints from the ratios. Never a definitive verdict.

    Ratios are variant_mbps / sum_of_solo_medians. Thresholds (0.85, and a
    0.15 gap between range and same) are judgement calls, chosen so a
    small measurement wobble does not flip the story.
    """
    solo_sum = sum(v["median"] for v in solo.values())
    hints = []
    if solo_sum <= 0:
        return ["No usable solo speeds, so no ratio can be interpreted. "
                "Fix link detection / test-server reachability first."]
    # Fluctuation first: it contaminates every other comparison.
    for name, entry in solo.items():
        s = entry["summary"]
        if s and s.get("min", 0) > 0 and s["max"] / s["min"] >= 1.5:
            hints.append(
                f"FLUCTUATION: {name} swung {s['min']:.1f} -> {s['max']:.1f} Mbps "
                f"across repeats (median {s['median']:.1f}). A single earlier "
                f"test could easily have caught it on a fast or slow swing, "
                f"so treat any one-shot combined number with suspicion.")
    by_mode = {v["mode"]: v for v in variants}
    same = by_mode.get("same")
    rng = by_mode.get("range")
    single = by_mode.get("single")
    ratios = {}
    for mode, var in by_mode.items():
        if var.get("status") == "ok" and var.get("mbps"):
            ratios[mode] = var["mbps"] / solo_sum
    if single and ratios.get("single", 1.0) < 0.85:
        hints.append(
            f"PARALLEL-ONLY LOSS: parallel downloads on single links sum to "
            f"{single['mbps']:.1f} Mbps vs {solo_sum:.1f} Mbps solo "
            f"({ratios['single']:.2f}x). The bottleneck is likely server "
            f"per-connection throttling or local CPU, NOT combining links, "
            f"because no combining happened in this variant.")
    if same and rng and ratios.get("same") is not None \
            and ratios.get("range") is not None \
            and ratios["range"] - ratios["same"] >= 0.15:
        hints.append(
            f"SAME-OBJECT EFFECT: distinct ranges gave {rng['mbps']:.1f} Mbps "
            f"vs {same['mbps']:.1f} Mbps for the same file "
            f"({ratios['range']:.2f}x vs {ratios['same']:.2f}x of solo sum). "
            f"The test server may throttle or deprioritise many connections "
            f"for the same object. Try a different test server before "
            f"blaming the links.")
    if same and ratios.get("same", 0) >= 0.85:
        hints.append(
            f"COMBINED LOOKS FINE THIS RUN: same-file aggregate "
            f"{same['mbps']:.1f} Mbps is {ratios['same']:.2f}x the solo sum "
            f"({solo_sum:.1f} Mbps). The earlier low number may have been "
            f"fluctuation or that server at that time, not a standing bug.")
    elif same and ratios.get("same", 0) < 0.85 \
            and ratios.get("single", 1.0) >= 0.85 \
            and (rng is None or rng.get("status") != "ok"
                 or ratios.get("range", 0) < 0.85):
        hints.append(
            "COMBINING STILL SUSPECT, BUT UNPROVEN: solo and single-parallel "
            "look fine, yet combined aggregates stay low. Next steps that "
            "would actually narrow it down: a different test server, a "
            "longer test (more bytes, not more workers), and watching CPU "
            "during the aggregate run. Do not tune the proxy on this "
            "evidence alone.")
    if rng and rng.get("status") == "range_unsupported":
        hints.append(
            "RANGE VARIANT INVALID HERE: this server ignores Range requests, "
            "so the distinct-range comparison could not run. Pick a "
            "Range-capable server (or a larger file on the same server) "
            "to get that comparison.")
    partial = [v["mode"] for v in variants if v.get("status") == "partial"]
    if partial:
        hints.append(
            f"PARTIAL VARIANTS ({', '.join(partial)}): some workers failed, "
            f"so those speeds understate the links. Check the failed-worker "
            f"errors in the JSON report before drawing conclusions.")
    if not hints:
        hints.append(
            "No strong pattern in this run. The ratios are in the report; "
            "re-run diagnose once more (same command) and compare the two "
            "reports before changing any code.")
    return hints


# ---------------- orchestration ----------------

def run_diagnosis(links, urls, repeats=3, per_link_bytes=10_000_000,
                  warmup_bytes=1_000_000, workers_per_link=2,
                  aggregate_bytes=8_000_000):
    """Run the full diagnosis. Returns a JSON-serialisable report dict."""
    t0 = time.time()
    url, url_report = pick_url_for_links(links, urls)
    report = {"started": time.strftime("%Y-%m-%d %H:%M:%S"),
              "candidate_urls": urls, "url_report": url_report,
              "common_url": url, "repeats": repeats,
              "per_link_bytes": per_link_bytes,
              "warmup_bytes": warmup_bytes,
              "workers_per_link": workers_per_link,
              "aggregate_bytes": aggregate_bytes}
    if url is None:
        # No common server: solo per link on its own working URL, no aggregates.
        report["note"] = ("No test URL worked on every link, so aggregates "
                          "would compare different servers and prove nothing. "
                          "Solo runs below used each link's own working URL.")
        solo = {}
        for link in links:
            own = None
            for cand in urls:
                if url_report.get(cand, {}).get(link["name"]):
                    own = cand
                    break
            if own:
                solo.update(solo_repeats([link], own, repeats=repeats,
                                         per_link_bytes=per_link_bytes,
                                         warmup_bytes=warmup_bytes))
        report["solo"] = {k: {"runs": v["runs"], "summary": v["summary"],
                              "median": v["median"], "errors": v["errors"]}
                          for k, v in solo.items()}
        report["solo_sum_mbps"] = sum(v["median"] for v in solo.values())
        report["variants"] = []
        report["hints"] = interpret(solo, [])
        report["seconds"] = time.time() - t0
        return report

    solo = solo_repeats(links, url, repeats=repeats,
                        per_link_bytes=per_link_bytes,
                        warmup_bytes=warmup_bytes)
    report["solo"] = {k: {"runs": v["runs"], "summary": v["summary"],
                          "median": v["median"], "errors": v["errors"]}
                      for k, v in solo.items()}
    solo_sum = sum(v["median"] for v in solo.values())
    report["solo_sum_mbps"] = solo_sum

    # File size for the range variant, from the downloader probe (Range 0-0).
    file_size = None
    try:
        from downloader import probe
        file_size, _ranges = probe(url, links[0]["ip"])
    except Exception:
        file_size = None
    report["file_size"] = file_size

    total_budget = aggregate_bytes * workers_per_link * len(links)
    variants = []
    for mode in ("same", "range", "single"):
        var = _aggregate(url, links, solo, total_budget, workers_per_link,
                         warmup_bytes, mode, file_size=file_size)
        # Strip per-worker timing details from the report's variant copy?
        # Keep them: they are the evidence if a variant looks odd.
        variants.append(var)
    report["variants"] = variants
    report["hints"] = interpret(solo, variants)
    report["seconds"] = time.time() - t0
    return report


def format_report(report):
    """Human-readable rendering of run_diagnosis() output."""
    lines = []
    lines.append("=== Dual Link diagnosis ===")
    lines.append(f"Common test URL: {report.get('common_url') or '(none)'}")
    if report.get("note"):
        lines.append(f"Note: {report['note']}")
    lines.append("")
    lines.append("Solo speeds (interleaved repeats, warmup discarded):")
    for name, entry in report["solo"].items():
        s = entry["summary"]
        runs = ", ".join(f"{r['mbps']:.1f}" for r in entry["runs"])
        if s:
            lines.append(f"  {name:<28} median {s['median']:6.1f} Mbps "
                         f"(min {s['min']:.1f}, max {s['max']:.1f})  runs: {runs}")
        else:
            lines.append(f"  {name:<28} no successful runs")
        for err in entry["errors"]:
            lines.append(f"    ! {err}")
    lines.append(f"  Solo sum (medians): {report['solo_sum_mbps']:.1f} Mbps")
    if report.get("file_size"):
        lines.append(f"  Test file size: {report['file_size'] / 1e6:.1f} MB")
    lines.append("")
    lines.append("Aggregate variants (same session, same server):")
    solo_sum = report["solo_sum_mbps"] or 1.0
    for var in report["variants"]:
        if var.get("status") in ("ok", "partial"):
            lines.append(f"  {var['mode']:<8} {var['mbps']:6.1f} Mbps "
                         f"({var['mbps'] / solo_sum:.2f}x solo sum)  "
                         f"status={var['status']}"
                         + (f"  failed workers: {var['failed_workers']}"
                            if var.get("failed_workers") else ""))
        else:
            lines.append(f"  {var['mode']:<8} {var['status']}"
                         + (f" -- {var['note']}" if var.get("note") else ""))
        if var.get("note") and var.get("status") in ("ok", "partial"):
            lines.append(f"           {var['note']}")
    lines.append("")
    lines.append("What this suggests (heuristics, not a verdict):")
    for hint in report["hints"]:
        lines.append(f"  - {hint}")
    lines.append(f"\nTook {report['seconds']:.1f}s.")
    return "\n".join(lines)
