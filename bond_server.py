#!/usr/bin/env python3
"""Bonding server. Run on a VPS with a public IP:

    python3 bond_server.py --port 9000 --token <long-random-secret>

Open TCP <port> in the VPS firewall. Then point bond_client.py at it.

Prototype notes: the tunnel protocol has NO encryption. Fine for testing;
for real traffic run it inside WireGuard/SSH, or treat as an experiment.
"""
import argparse
import asyncio
import ssl
import sys

from bond_common import (
    MAGIC, MAX_PAYLOAD, BondSession, NoPipesError,
    F_OPEN, F_DATA, F_CLOSE, decode_open,
)


class BondServer:
    def __init__(self, tokens):
        self.tokens = set(tokens)
        self.sessions = {}  # token -> BondSession

    async def handle_pipe(self, reader, writer):
        try:
            try:
                magic = await asyncio.wait_for(reader.readexactly(8), 10)
            except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                writer.close()
                return
            if magic != MAGIC:
                writer.close()
                return
            try:
                line = await asyncio.wait_for(reader.readline(), 10)
            except asyncio.TimeoutError:
                writer.close()
                return
            token = line.decode("utf-8", "replace").strip()
            if token not in self.tokens:
                writer.write(b"ERR bad token\n")
                await writer.drain()
                writer.close()
                print(f"rejected pipe with bad token from {writer.get_extra_info('peername')}")
                return
            writer.write(b"OK\n")
            await writer.drain()

            sess = self.sessions.get(token)
            if sess is None or sess.closed:
                sess = BondSession(token)
                sess.frame_handler = self._on_frame
                self.sessions[token] = sess
            peer = writer.get_extra_info("peername")
            sess.add_pipe(reader, writer, name=str(peer))
            print(f"pipe up from {peer} (total pipes: {len(sess.pipes)})")
        except (ConnectionError, OSError):
            try:
                writer.close()
            except Exception:
                pass

    async def _on_frame(self, sess, stream_id, ftype, payload):
        ctx = sess.streams.get(stream_id)
        if ftype == F_OPEN:
            if ctx is not None:
                return
            try:
                host, port = decode_open(payload)
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), 15)
            except Exception:
                try:
                    await sess.send_frame(F_CLOSE, stream_id, b"")
                except NoPipesError:
                    pass
                return
            sess.streams[stream_id] = writer
            asyncio.create_task(self._relay(sess, stream_id, reader, writer))
        elif ftype == F_DATA:
            if ctx is None:
                return
            try:
                ctx.write(payload)
                await ctx.drain()
            except (ConnectionError, OSError, asyncio.IncompleteReadError, RuntimeError):
                await self._close_stream(sess, stream_id)
        elif ftype == F_CLOSE:
            await self._close_stream(sess, stream_id)

    async def _relay(self, sess, stream_id, reader, writer):
        try:
            while True:
                if stream_id not in sess.streams:
                    break
                data = await reader.read(MAX_PAYLOAD)
                if not data:
                    break
                await sess.send_frame(F_DATA, stream_id, data)
        except (ConnectionError, NoPipesError, OSError, asyncio.IncompleteReadError):
            pass
        finally:
            try:
                await sess.send_frame(F_CLOSE, stream_id, b"")
            except NoPipesError:
                pass
            await self._close_stream(sess, stream_id)

    async def _close_stream(self, sess, stream_id):
        writer = sess.streams.pop(stream_id, None)
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass

    async def serve(self, host, port, tls_cert=None, tls_key=None):
        ctx = None
        if tls_cert or tls_key:
            if not (tls_cert and tls_key):
                raise ValueError("need both --tls-cert and --tls-key")
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(tls_cert, tls_key)
            print("TLS enabled on tunnel pipes")
        server = await asyncio.start_server(self.handle_pipe, host, port, ssl=ctx)
        print(f"bond server listening on {host}:{port}")
        async with server:
            await server.serve_forever()


def main():
    ap = argparse.ArgumentParser(description="Multilink bonding server (run on VPS).")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--token", required=True, help="pre-shared secret; use a long random string")
    ap.add_argument("--tls-cert", default=None, help="PEM certificate -> enables TLS on pipes")
    ap.add_argument("--tls-key", default=None, help="PEM private key for --tls-cert")
    args = ap.parse_args()
    asyncio.run(BondServer([args.token]).serve(args.host, args.port,
                                               tls_cert=args.tls_cert, tls_key=args.tls_key))


if __name__ == "__main__":
    main()
