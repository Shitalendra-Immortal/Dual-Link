# True bonding with a VPS (experimental prototype)

The `multilink.py proxy` from v1 spreads *connections* across links. This
prototype goes further: it stripes a **single connection** across all your
links, so even one download uses Wi-Fi + tether/cable at the same time.

## How it works

```
 your apps                    your PC                      VPS
┌──────────┐   SOCKS5   ┌──────────────────┐   2x TCP pipes   ┌──────────────────┐
│ browser / │ ────────► │  bond_client.py  │ ───────────────► │  bond_server.py  │ ──► internet
│ dl manager│ 127.0.0.1 │  stripes frames  │  1 per link IP   │  reassembles     │
└──────────┘   :1080    └──────────────────┘                  └──────────────────┘
```

- `bond_client.py` opens **one TCP pipe per link** (each bound to that link's
  IP) to your VPS and exposes a SOCKS5 proxy on `127.0.0.1:1080`.
- Every app connection becomes a *stream*; its bytes are cut into frames,
  numbered with **global sequence numbers**, and striped across the pipes
  (weighted by measured link speed).
- `bond_server.py` on the VPS **reorders frames by sequence number**, sends
  cumulative ACKs, connects to the real destination, and relays the reply
  back through the same striped tunnel.
- If a pipe dies mid-download, its unacknowledged frames are **re-sent on the
  surviving pipes** — the download keeps going on the remaining link(s).

## Setup

### 1. Get a VPS

Any cheap VPS with a public IPv4 address works (the tunnel's combined speed
can't exceed the VPS's own bandwidth, and VPS bandwidth is usually metered —
keep that in mind). Install Python 3.9+ on it.

### 2. Run the server on the VPS

Copy `bond_common.py` and `bond_server.py` to the VPS, then:

```bash
python3 bond_server.py --port 9000 --token "a-long-random-secret-here"
```

Open TCP port 9000 in the VPS firewall. Keep this running (use `tmux`,
`nohup`, or a systemd service for permanence).

### 3. Run the client on your Windows PC

In the `multilink-booster` folder (after `py multilink.py detect`):

```bat
py bond_client.py --server <VPS-IP> --token "a-long-random-secret-here"
```

You should see one `pipe up` line per link. Then set your apps to use
**SOCKS5** `127.0.0.1:1080` (browsers, download managers like IDM/Free Download
Manager, curl: `curl --socks5 127.0.0.1:1080 ...`).

### 4. Verify it's really bonding

Download a large file with a **single-connection** download through the SOCKS
proxy and compare against each link alone. On the server you can watch both
pipes carry traffic.

## Encrypting the tunnel (recommended)

The prototype framing from the previous section is now TLS-capable, using
**certificate pinning** (no certificate authority needed):

```bash
# on any machine with openssl:
python3 gen_cert.py --out ./certs
```

Copy `certs/bond.crt` and `certs/bond.key` to the VPS, then:

```bash
# server:
python3 bond_server.py --port 9000 --token SECRET \
    --tls-cert certs/bond.crt --tls-key certs/bond.key
```

```bat
:: client (fingerprint printed by gen_cert.py):
py bond_client.py --server VPS_IP --token SECRET --tls-fingerprint <hex>
```

The client refuses to connect unless the server presents the exact pinned
certificate, which defeats man-in-the-middle attacks. Plain (unencrypted)
mode still works if you omit the TLS flags — handy for local testing.

## Limitations — read before trusting it

- **Without the TLS flags above, the tunnel has no encryption.** Anyone on the
  path could read the traffic. Use `--tls-fingerprint` for anything beyond
  experiments, or additionally run the tunnel inside WireGuard/SSH.
- **Head-of-line blocking.** Frames are delivered in order, so if one link is
  much slower or very jittery, it can hold back the fast link. Weights (from
  `test --save-weights`) reduce this. Real-world tuning (congestion control,
  forward error correction) is beyond this prototype.
- **CPU.** Python-level framing costs CPU at very high speeds (hundreds of
  Mbps); for gigabit bonding you'd want a compiled implementation.
- **Silent network death.** If a link dies without TCP noticing (rare), the
  client only finds out when writes start failing. Reconnect is automatic
  once detected.
- Single token = single user. Don't share the token.

## What's next if you like it

- TLS-encrypt the pipes (wrapping the existing framing in `ssl` is a small,
  well-contained change).
- Per-pipe congestion-aware scheduling instead of weighted round-robin.
- A tiny live dashboard on the server showing per-pipe throughput.
