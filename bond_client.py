#!/usr/bin/env python3
"""Bonding client. Run on the Windows PC:

    py bond_client.py --server <VPS_IP> --token <same-secret-as-server>

Reads links from config.json (run `py multilink.py detect` first), opens one
tunnel pipe per link to the VPS, and exposes a SOCKS5 proxy on 127.0.0.1:1080.
Every app connection through that SOCKS proxy is striped across all links, so
even a single download uses Wi-Fi + tether/cable together.

Needs Python 3.9+, no extra packages.
"""
import argparse
import asyncio
import hashlib
import json
import os
import socket
import ssl
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from bond_common import (
    MAGIC, MAX_PAYLOAD, BondSession, NoPipesError,
    F_OPEN, F_DATA, F_CLOSE, encode_open,
)


class BondClient:
    def __init__(self, server, port, token, links, socks_port=1080, tls_fingerprint=None):
        self.server = server
        self.port = port
        self.token = token
        self.links = links
        self.socks_port = socks_port
        self.tls_fingerprint = ((tls_fingerprint or "").replace(":", "").lower() or None)
        self._tls_ctx = None
        if self.tls_fingerprint:
            # No CA involved: we pin the server certificate's SHA256 fingerprint.
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._tls_ctx = ctx
        self.session = BondSession("client")
        self.session.frame_handler = self._on_frame
        self._next_stream = 0

    # ---------------- pipes: one per link, with reconnect ----------------
    async def _pipe_loop(self, link):
        name = link.get("name", link["ip"])
        delay = 1
        while True:
            try:
                reader, writer = await asyncio.open_connection(
                    self.server, self.port, local_addr=(link["ip"], 0),
                    ssl=self._tls_ctx,
                    server_hostname=self.server if self._tls_ctx else None)
                if self._tls_ctx:
                    der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
                    fp = hashlib.sha256(der).hexdigest()
                    if fp != self.tls_fingerprint:
                        writer.close()
                        raise RuntimeError(
                            "TLS certificate fingerprint mismatch "
                            "(wrong cert, or possible man-in-the-middle)")
                    print(f"[{name}] TLS certificate pinned OK")
                writer.write(MAGIC + self.token.encode("utf-8") + b"\n")
                await writer.drain()
                line = await asyncio.wait_for(reader.readline(), 10)
                if line.strip() != b"OK":
                    raise RuntimeError(f"handshake rejected: {line!r}")
                pipe = self.session.add_pipe(
                    reader, writer, name=name,
                    weight=link.get("weight", 1.0))
                print(f"[{name}] pipe up via {link['ip']}")
                delay = 1
                while pipe in self.session.pipes and not self.session.closed:
                    await asyncio.sleep(1)
                print(f"[{name}] pipe down, reconnecting...")
            except Exception as exc:
                print(f"[{name}] pipe error: {exc}")
            await asyncio.sleep(delay)
            delay = min(30, delay * 2)

    # ---------------- SOCKS5 front end ----------------
    def _new_stream_id(self):
        self._next_stream = (self._next_stream % 65535) + 1
        return self._next_stream

    async def _handle_socks(self, reader, writer):
        try:
            ver, nmethods = await reader.readexactly(2)
            if ver != 5:
                writer.close()
                return
            await reader.readexactly(nmethods)
            writer.write(b"\x05\x00")
            await writer.drain()

            req = await reader.readexactly(4)
            ver, cmd, _rsv, atyp = req
            if ver != 5 or cmd != 1:  # only CONNECT
                writer.write(b"\x05\x07\x00\x01" + b"\x00" * 6)
                await writer.drain()
                writer.close()
                return
            if atyp == 1:      # IPv4
                host = socket.inet_ntoa(await reader.readexactly(4))
            elif atyp == 3:    # domain
                ln = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(ln)).decode("utf-8", "replace")
            elif atyp == 4:    # IPv6
                host = socket.inet_ntop(socket.AF_INET6, await reader.readexactly(16))
            else:
                writer.write(b"\x05\x08\x00\x01" + b"\x00" * 6)
                await writer.drain()
                writer.close()
                return
            port = int.from_bytes(await reader.readexactly(2), "big")

            stream_id = self._new_stream_id()
            self.session.streams[stream_id] = writer
            try:
                await self.session.send_frame(F_OPEN, stream_id, encode_open(host, port))
            except NoPipesError:
                writer.write(b"\x05\x04\x00\x01" + b"\x00" * 6)  # host unreachable
                await writer.drain()
                writer.close()
                self.session.streams.pop(stream_id, None)
                return
            writer.write(b"\x05\x00\x00\x01" + b"\x00" * 6)
            await writer.drain()
            asyncio.create_task(self._app_relay(stream_id, reader, writer))
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            try:
                writer.close()
            except Exception:
                pass

    async def _app_relay(self, stream_id, reader, writer):
        try:
            while True:
                if stream_id not in self.session.streams:
                    break
                data = await reader.read(MAX_PAYLOAD)
                if not data:
                    break
                await self.session.send_frame(F_DATA, stream_id, data)
        except (ConnectionError, NoPipesError, OSError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                await self.session.send_frame(F_CLOSE, stream_id, b"")
            except NoPipesError:
                pass
            self.session.streams.pop(stream_id, None)
            try:
                writer.close()
            except Exception:
                pass

    async def _on_frame(self, sess, stream_id, ftype, payload):
        if ftype == F_DATA:
            w = sess.streams.get(stream_id)
            if w is None:
                return
            try:
                w.write(payload)
                await w.drain()
            except (ConnectionError, OSError, asyncio.IncompleteReadError, RuntimeError):
                sess.streams.pop(stream_id, None)
                try:
                    w.close()
                except Exception:
                    pass
        elif ftype == F_CLOSE:
            w = sess.streams.pop(stream_id, None)
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass

    async def run(self):
        for link in self.links:
            asyncio.create_task(self._pipe_loop(link))
        server = await asyncio.start_server(self._handle_socks, "127.0.0.1", self.socks_port)
        print(f"SOCKS5 on 127.0.0.1:{self.socks_port} -> bonded tunnel to {self.server}:{self.port}")
        print("Point your apps / download manager at the SOCKS5 proxy.")
        async with server:
            await server.serve_forever()


def load_links(args):
    if args.link_ip:
        return [{"ip": ip, "name": ip, "kind": "manual", "weight": 1.0}
                for ip in args.link_ip]
    cfg_path = args.config
    if not os.path.exists(cfg_path):
        sys.exit(f"{cfg_path} not found. Run 'py multilink.py detect' first, or pass --link-ip.")
    with open(cfg_path) as f:
        links = json.load(f).get("links", [])
    if not links:
        sys.exit("No links in config. Run 'py multilink.py detect' first, or pass --link-ip.")
    return links


def main():
    ap = argparse.ArgumentParser(
        description="Bonding client: stripe one SOCKS5 proxy across all links to a VPS.")
    ap.add_argument("--server", required=True, help="VPS public IP / hostname")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--token", required=True, help="same pre-shared secret as the server")
    ap.add_argument("--socks-port", type=int, default=1080)
    ap.add_argument("--tls-fingerprint", default=None,
                    help="SHA256 fingerprint of the server cert -> enables TLS with pinning "
                         "(get it from gen_cert.py)")
    ap.add_argument("--config", default=os.path.join(BASE, "config.json"))
    ap.add_argument("--link-ip", action="append", default=None,
                    help="use this source IP as a link (repeatable); overrides config.json")
    args = ap.parse_args()
    links = load_links(args)
    print(f"links: {', '.join(l['ip'] for l in links)}")
    if args.tls_fingerprint:
        print("TLS enabled (certificate pinning)")
    asyncio.run(BondClient(args.server, args.port, args.token, links,
                           args.socks_port,
                           tls_fingerprint=args.tls_fingerprint).run())


if __name__ == "__main__":
    main()
