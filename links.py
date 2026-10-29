"""Detect usable network links on Windows: Wi-Fi, USB tethering, Ethernet."""
from __future__ import annotations

import json
import re
import subprocess
import sys


def _powershell_json(ps_command: str):
    """Run a PowerShell command ending with ConvertTo-Json and parse the result."""
    cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_command]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except FileNotFoundError as exc:
        raise RuntimeError("PowerShell was not found. Link detection needs Windows.") from exc
    out = proc.stdout.strip()
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Could not parse PowerShell output: {exc}") from exc
    if isinstance(data, dict):
        return [data]
    return data or []


def _parse_link_speed(text):
    """'866.7 Mbps' -> 866.7 ; '1 Gbps' -> 1000.0 ; None when unknown."""
    if not text:
        return None
    m = re.match(r"\s*([\d.,]+)\s*([kKmMgGtT])?[Bb]ps", str(text))
    if not m:
        return None
    value = float(m.group(1).replace(",", ""))
    mult = {"": 1, "k": 1e-3, "m": 1, "g": 1e3, "t": 1e6}[(m.group(2) or "").lower()]
    return value * mult  # in Mbps


def _classify(name, description, media_type):
    s = " ".join(x for x in (name, description, media_type) if x).lower()
    if "bluetooth" in s:
        return "bluetooth"
    if "rndis" in s or "remote ndis" in s:
        return "tether"
    if any(k in s for k in ("wi-fi", "wifi", "wireless", "802.11", "wlan")):
        return "wifi"
    if "usb" in s and "ethernet" in s:
        return "tether"
    if any(k in s for k in ("ethernet", "802.3", "gigabit")):
        return "ethernet"
    return "other"


# Note: "bluetooth" is intentionally NOT in this list - Bluetooth PAN tethering
# is a usable (if slow) link. Adapters without an IPv4 address are skipped anyway.
_VIRTUAL_HINTS = ("hyper-v", "virtual", "vpn", "tap-", "tap ", "wireguard", "vmware", "virtualbox")


def detect_links():
    """Return usable links as dicts: kind/name/description/ip/gateway/link_mbps/weight."""
    if sys.platform != "win32":
        raise RuntimeError(
            "Automatic link detection currently supports Windows only. "
            "On another OS, create config.json manually."
        )

    adapters = _powershell_json(
        "Get-NetAdapter | Where-Object {$_.Status -eq 'Up'} | "
        "Select-Object Name, InterfaceDescription, MediaType, LinkSpeed | "
        "ConvertTo-Json -Depth 2"
    )
    addresses = _powershell_json(
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Where-Object {$_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1'} | "
        "Select-Object InterfaceAlias, IPAddress | ConvertTo-Json -Depth 2"
    )
    routes = _powershell_json(
        "Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue | "
        "Select-Object InterfaceAlias, NextHop, RouteMetric | ConvertTo-Json -Depth 2"
    )

    ip_by_alias = {}
    for row in addresses:
        alias, ip = row.get("InterfaceAlias"), row.get("IPAddress")
        if alias and ip and alias not in ip_by_alias:
            ip_by_alias[alias] = ip

    gw_by_alias = {}
    for row in routes:
        alias = row.get("InterfaceAlias")
        if not alias:
            continue
        try:
            metric = int(row.get("RouteMetric"))
        except (TypeError, ValueError):
            metric = 9999
        cur = gw_by_alias.get(alias)
        if cur is None or metric < cur[1]:
            gw_by_alias[alias] = (row.get("NextHop"), metric)

    links = []
    for ad in adapters:
        name = ad.get("Name") or ""
        desc = ad.get("InterfaceDescription") or ""
        media = ad.get("MediaType") or ""
        kind = _classify(name, desc, media)
        if kind == "other":
            continue
        if any(h in desc.lower() for h in _VIRTUAL_HINTS):
            continue
        ip = ip_by_alias.get(name)
        if not ip:
            continue  # no usable IPv4 address on this adapter
        gw = gw_by_alias.get(name, (None, None))[0]
        links.append({
            "kind": kind,
            "name": name,
            "description": desc,
            "ip": ip,
            "gateway": gw,
            "link_mbps": _parse_link_speed(ad.get("LinkSpeed")),
            "weight": 1.0,
        })
    return links


def print_links(links):
    if not links:
        print("No usable links found. Make sure Wi-Fi is connected and/or USB tethering is enabled.")
        return
    print(f"{'#':<3}{'kind':<9}{'adapter':<28}{'ip':<16}{'gateway':<16}{'link speed'}")
    print("-" * 92)
    for i, l in enumerate(links, 1):
        speed = f"{l['link_mbps']:.0f} Mbps" if l.get("link_mbps") else "?"
        print(f"{i:<3}{l['kind']:<9}{l['name'][:27]:<28}{l['ip']:<16}{str(l['gateway']):<16}{speed}")
