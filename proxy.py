"""Local HTTP(S) proxy that spreads every outbound connection across your links.

Point your browser/apps at 127.0.0.1:8080. Each new connection is bound to a
different link's IP address (weighted round-robin), so multi-connection traffic
uses Wi-Fi + tether/cable together. Failed links are skipped automatically.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import urlsplit

BUFFER_SIZE = 64 * 1024
READ_TIMEOUT = 15


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, count=None):
    try:
        while True:
            data = await reader.read(BUFFER_SIZE)
            if not data:
                break
            writer.write(data)
            await writer.drain()
            if count:
                count(len(data))
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    data = b""
    try:
        while b"\r\n\r\n" not in data:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=READ_TIMEOUT)
            if not chunk:
                break
            data += chunk
            if len(data) > 65536:
                break
    except asyncio.TimeoutError:
        pass
    return data


class MultiLinkProxy:
    def __init__(self, balancer, host="127.0.0.1", port=8080, stats=None, conn_log=None):
        self.balancer = balancer
        self.host = host
        self.port = port
        # Optional live counters: stats[link_ip] = {"up": bytes, "down": bytes}
        self.stats = stats
        # Optional per-connection tracking for the dashboard's per-app view:
        # self.active maps id -> live conn dict; finished conns go to conn_log.
        self.conn_log = conn_log
        self.active = {}
        self.conn_seq = 0

    def _counter(self, st, direction, conn=None):
        if self.stats is None and conn is None:
            return None
        ip = st.link["ip"]

        def cb(n):
            if self.stats is not None:
                d = self.stats.setdefault(ip, {"up": 0, "down": 0})
                d[direction] += n
            if conn is not None:
                conn[direction] += n

        return cb

    async def handle_client(self, reader, writer):
        peer = writer.get_extra_info("peername")
        conn = {"port": peer[1] if peer else 0, "host": "",
                "up": 0, "down": 0, "t0": time.time()}
        cid = id(conn)
        self.active[cid] = conn
        try:
            await self._handle_client_impl(reader, writer, conn)
        finally:
            self.active.pop(cid, None)
            conn["dur"] = time.time() - conn["t0"]
            if self.conn_log is not None and conn["host"]:
                conn["seq"] = self.conn_seq
                self.conn_seq += 1
                self.conn_log.append(conn)

    async def _connect_via_link(self, host, port):
        """Open a connection bound to one link's source IP, failing over as needed."""
        tried = set()
        last_err = None
        for _ in range(len(self.balancer.states)):
            st = self.balancer.pick()
            if st is None or id(st) in tried:
                break
            tried.add(id(st))
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port, local_addr=(st.link["ip"], 0)),
                    timeout=12,
                )
                st.mark_success()
                return reader, writer, st
            except Exception as exc:  # noqa: BLE001 - failover must be broad
                last_err = exc
                st.mark_failure()
        raise ConnectionError(f"Could not reach {host}:{port} on any link: {last_err}")

    async def _handle_client_impl(self, reader, writer, conn):
        try:
            raw = await _read_head(reader)
            if not raw:
                writer.close()
                return
            head, _, rest = raw.partition(b"\r\n\r\n")
            try:
                request_line, *header_lines = head.decode("latin-1").split("\r\n")
                method, target, _version = request_line.split(" ", 2)
            except ValueError:
                writer.close()
                return
            method = method.upper()

            if method == "CONNECT":
                # HTTPS tunnel: byte-transparent relay after 200.
                host, _, port_s = target.rpartition(":")
                if not host:
                    host, port_s = target, "443"
                try:
                    port = int(port_s)
                except ValueError:
                    port = 443
                upstream_r, upstream_w, st = await self._connect_via_link(host, port)
                conn["host"] = f"{host}:{port}"
                writer.write(b"HTTP/1.1 200 Connection Established\r\n"
                             b"Proxy-Agent: multilink/1.0\r\n\r\n")
                await writer.drain()
                await asyncio.gather(
                    _relay(reader, upstream_w, self._counter(st, "up", conn)),
                    _relay(upstream_r, writer, self._counter(st, "down", conn)),
                )
                return

            # Plain HTTP: forward the request to the origin server.
            parts = urlsplit(target)
            host = parts.hostname
            headers = {}
            for line in header_lines:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            if host:
                path = parts.path or "/"
                if parts.query:
                    path += "?" + parts.query
                port = parts.port or 80
            else:
                # Non-proxy-style request; fall back to Host header.
                host = headers.get("host", "").split(":")[0]
                path, port = target, 80
            if not host:
                writer.close()
                return

            out_lines = [f"{method} {path} HTTP/1.1"]
            host_sent = False
            for line in header_lines:
                low = line.lower()
                if low.startswith("proxy-connection:"):
                    out_lines.append("Connection:" + line.split(":", 1)[1])
                elif low.startswith("host:"):
                    host_sent = True
                    out_lines.append(line)
                else:
                    out_lines.append(line)
            if not host_sent:
                out_lines.append(f"Host: {host}")

            upstream_r, upstream_w, st = await self._connect_via_link(host, port)
            conn["host"] = host
            req_out = ("\r\n".join(out_lines) + "\r\n\r\n").encode("latin-1")
            upstream_w.write(req_out)
            if rest:
                upstream_w.write(rest)
            await upstream_w.drain()
            up_counter = self._counter(st, "up", conn)
            if up_counter:
                up_counter(len(req_out) + len(rest))
            await asyncio.gather(
                _relay(reader, upstream_w, up_counter),
                _relay(upstream_r, writer, self._counter(st, "down", conn)),
            )
        except (ConnectionError, asyncio.TimeoutError, OSError):
            try:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            except Exception:
                pass
            writer.close()
        except Exception:
            try:
                writer.close()
            except Exception:
                pass

    async def _serve(self):
        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        addrs = ", ".join(str(s.getsockname()) for s in server.sockets or [])
        print(f"Multi-link proxy listening on {addrs}")
        print(f"Set your browser/system proxy to http://{self.host}:{self.port} "
              "(use it for both HTTP and HTTPS).")
        print("Press Ctrl+C to stop.\n")
        async with server:
            await server.serve_forever()

    def run(self):
        try:
            asyncio.run(self._serve())
        except KeyboardInterrupt:
            print("\nProxy stopped.")
