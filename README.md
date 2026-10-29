# multilink-booster (Windows prototype v1)

Combine your **Wi-Fi** and **USB-tethered phone / Ethernet cable** bandwidth on a
Windows PC. It does **per-connection load balancing**: every new connection your
apps make is sent out over a different link (round-robin, weighted by measured
speed), so multi-connection traffic — browsers with many tabs, download
managers, streaming + downloads — uses both links at once. A dead link is
skipped automatically (failover).

**What it is NOT:** true single-stream bonding. One single-threaded download
still goes over one link, because combining one stream needs a server on the
other end to reassemble it. See "Going further" below.

## Requirements

- Windows 10/11, Python 3.9+ (from python.org or the Microsoft Store)
- No extra packages needed — standard library only
- Wi-Fi connected, **and** phone plugged in via USB with **USB tethering**
  enabled (it appears in Windows as an "Ethernet" adapter), or a wired
  Ethernet cable

## Quick start

Open a terminal (PowerShell or CMD) in this folder:

```bat
py multilink.py detect
```

You should see your links, e.g. `wifi (Wi-Fi)` and `tether (Ethernet 2)`.
This saves them to `config.json`.

```bat
py multilink.py test --save-weights
```

Measures each link, then hammers all links in parallel to show combined
throughput. `--save-weights` stores the measured speeds so the proxy prefers
the faster link proportionally. The dashboard's "Test speeds" button does this
automatically: after every test it re-weights each link by its measured Mbps
(slowest working link = 1.0, faster links scale up), saves the config, and
applies the new weights live to a running proxy or bond tunnel — no restart,
no dropped connections. This adds zero overhead to the data path: only the
weight numbers change, the per-connection pick stays exactly as fast.

```bat
py multilink.py diagnose
```

If combined speed comes in below the sum of the links, don't guess at the
cause — run this. In one session it measures each link with interleaved
repeats (so a drifting Wi-Fi speed can't masquerade as a link difference,
and warmup bytes are discarded in the same connection), then runs three
aggregate variants back to back on the same server: the usual same-file
aggregate, a distinct-Range-segments aggregate, and parallel downloads on
each link alone. Comparing those three separates fluctuation, server
per-connection throttling, same-object throttling, and combining itself.
It prints heuristic hints (labelled as such, with the ratios) and saves
`diagnose-report.json` so runs can be compared. Options:
`--url`, `--repeats`, `--bytes`, `--warmup-bytes`, `--workers`,
`--aggregate-bytes`, `--output`.

```bat
py multilink.py proxy
```

Starts the proxy on `127.0.0.1:8080`. Then point your apps at it:

- **Whole system:** Settings → Network & internet → Proxy → Manual proxy
  setup → On → address `127.0.0.1`, port `8080` (tick "Don't use the proxy
  server for local addresses").
- **Just the browser:** Firefox → Settings → Network Settings → Manual proxy
  configuration → HTTP Proxy `127.0.0.1:8080`, and tick
  "Also use this proxy for HTTPS".

Use HTTPS sites normally — the proxy tunnels them with `CONNECT`.

```bat
py multilink.py download https://example.com/bigfile.zip
```

Downloads **one file with all links at once**: the file is split into 16
segments fetched in parallel, each over a different link (faster links get
more segments, and a dying link's segments are retried on the others). No
proxy setting needed — the file lands in the current folder. The dashboard
has the same thing as a "One-task download" panel (paste a link, watch the
live progress bar; it saves to your Downloads folder).

## Graphical control panel

Prefer buttons over the terminal? Run:

## Web dashboard (recommended UI)

```bat
py dashboard.py
```

Opens **http://127.0.0.1:8899** in your browser: link cards with live up/down
status, real-time throughput graphs (per-link plus a dashed combined total)
fed by the actual bytes flowing through the proxy/bond pipes, one-click
speed tests, proxy start/stop, and a bonding control panel (enter your VPS
IP + token there — no command line needed). Controls are at the top, a
"Last test" panel shows solo speeds, the solo sum, and the combined speed
side by side, and a "Diagnose combined speed" panel runs the diagnose
command in the UI. If a proxy is already running outside the dashboard
(for example from a terminal), the dashboard says so in a banner instead
of showing a misleading 0 bps graph — its graphs only track a proxy it
starts itself.

It also has a **per-app breakdown**: every connection through the proxy is
attributed to its application (process name on Windows, destination host
otherwise), so you can see exactly which app is eating your bandwidth right
now, with live rates and totals. Hit the 🌙 button in the header for a
light theme — your choice is remembered.

## Classic desktop panel (fallback)

```bat
py gui.py
```

A plain tkinter window with the link table, live status probing, speed tests
(with a solo-sum vs combined result line), proxy controls, a Diagnose
button, and a one-task download box, if you'd rather not use a browser.
(Standard tkinter — included with normal Windows Python installs.) It does
not have the dashboard's live chart or per-app breakdown, and its proxy
runs as a separate process, so new weights need a proxy restart to apply —
the window says so after a test instead of pretending otherwise.

Double-click **`install.bat`** to add Start Menu shortcuts
("MultiLink Booster" GUI without a console window, plus a terminal shortcut);
`py install.py --uninstall` removes them again.

## Tips / troubleshooting

- Re-run `detect` whenever adapters change (tethering toggled, Wi-Fi
  reconnected).
- Disable any VPN while testing — it hides the real links.
- If both links go through the same phone/carrier or the same router/ISP,
  gains may be small: you're sharing one upstream pipe.
- Some apps (many games, some launchers) ignore proxy settings; browsers and
  most download tools honor them.
- The proxy only listens on your own PC (`127.0.0.1`), so nothing else on the
  network can use it.

## Files

| File | Purpose |
|---|---|
| `multilink.py` | CLI: `detect` / `test` / `proxy` |
| `links.py` | Finds Wi-Fi / tether / Ethernet adapters on Windows |
| `balancer.py` | Weighted round-robin + failover across links |
| `proxy.py` | Local HTTP(S) proxy, binds each connection to a link |
| `speedtest.py` | Per-link and aggregate throughput measurements |
| `diagnose.py` | Combined-speed diagnosis: interleaved solos + aggregate variants |
| `config.json` | Your links + settings (created by `detect`) |
| `gen_cert.py` | Makes a self-signed TLS cert for the bond server |
| `install.py` / `install.bat` | Windows installer: Start Menu shortcuts |

## True bonding: one download, both links (experimental)

New: `bond_client.py` + `bond_server.py` + `bond_common.py` stripe a **single**
connection across all your links via a small VPS you control — so even a
one-connection download uses Wi-Fi + tether together, with automatic failover
if a link drops mid-transfer. Full setup guide: **[BONDING.md](BONDING.md)**.

> Security note: the prototype tunnel protocol has **no encryption**. Fine for
> testing; run it inside WireGuard/SSH for anything sensitive.

## Going further

- **True bonding** (one download using both links at once) needs an aggregation
  server/VPN endpoint — e.g. a small VPS running a bonding VPN, or a tool like
  Speedify/OpenMPTCProuter. Ask and I can design that setup.
- **System-wide without proxy config** needs a Windows driver / WFP
  integration — much bigger build.
- A tiny GUI (system-tray app showing live per-link speeds) is a natural next
  step if you like the prototype.
