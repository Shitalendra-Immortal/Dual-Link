"""Shared protocol for the true-bonding tunnel (bond_client <-> bond_server).

One session = one set of pipes (one TCP connection per local link) to the
server. Application streams are multiplexed as frames; frames are striped
across pipes with global sequence numbers, so a *single* download uses every
link at once. The receiver reorders by sequence number and sends cumulative
ACKs. If a pipe dies, its unacknowledged frames are re-sent on the survivors.

Frame layout:  seq:u32 | stream:u16 | type:u8 | len:u16 | payload
seq == 0 marks an unsequenced control frame (ACK).
"""
from __future__ import annotations

import asyncio
import struct

MAGIC = b"BOND1\0\0\0"            # 8-byte handshake magic
HDR = struct.Struct("!I H B H")   # seq, stream_id, ftype, length
HDR_LEN = HDR.size                # 9 bytes
MAX_PAYLOAD = 16 * 1024

F_OPEN = 1    # payload: host bytes + b"\x00" + port u16be   (client -> server)
F_DATA = 2    # payload: raw bytes
F_CLOSE = 3   # payload: empty
F_ACK = 4     # stream=0, seq=0, payload: cumulative ack u32be

ACK_EVERY = 16        # send a cumulative ACK every N received frames
MAX_UNACKED = 8192    # sender pauses when this many frames are unacknowledged
MAX_REORDER = 4096    # safety cap on the reorder buffer


class NoPipesError(RuntimeError):
    pass


def encode_frame(seq: int, stream_id: int, ftype: int, payload: bytes) -> bytes:
    return HDR.pack(seq, stream_id, ftype, len(payload)) + payload


async def read_frame(reader: asyncio.StreamReader):
    """Return (seq, stream_id, ftype, payload), or None on clean EOF."""
    try:
        hdr = await reader.readexactly(HDR_LEN)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
    seq, stream_id, ftype, length = HDR.unpack(hdr)
    if length > MAX_PAYLOAD + 64:
        raise ValueError(f"oversize frame: {length}")
    if length:
        try:
            payload = await reader.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionError):
            return None
    else:
        payload = b""
    return seq, stream_id, ftype, payload


def encode_open(host: str, port: int) -> bytes:
    return host.encode("utf-8") + b"\x00" + port.to_bytes(2, "big")


def decode_open(payload: bytes):
    host_b, port_b = payload.rsplit(b"\x00", 1)
    return host_b.decode("utf-8"), int.from_bytes(port_b, "big")


class _Pipe:
    def __init__(self, session, reader, writer, name="", weight=1.0):
        self.session = session
        self.reader = reader
        self.writer = writer
        self.name = name
        self.weight = max(0.1, float(weight or 1.0))
        self.bytes_sent = 0
        self.bytes_recv = 0

    def start(self):
        asyncio.create_task(self._run())

    async def _run(self):
        try:
            while True:
                frame = await read_frame(self.reader)
                if frame is None:
                    break
                await self.session._on_pipe_frame(self, frame)
        except (ConnectionError, asyncio.IncompleteReadError, ValueError, OSError):
            pass
        except Exception:
            pass
        finally:
            await self.session._drop_pipe(self)


class BondSession:
    """One bonded session: many pipes, many multiplexed streams."""

    def __init__(self, token: str):
        self.token = token
        self.frame_handler = None   # async fn(session, stream_id, ftype, payload)
        self.streams = {}           # stream_id -> owner-managed context
        self.pipes = []             # live _Pipe objects
        self.closed = False

        self._send_seq = 0
        self._recv_expected = 1
        self._reorder = {}          # seq -> (stream_id, ftype, payload)
        self._unacked = {}          # seq -> [pipe, raw_bytes]
        self._send_lock = asyncio.Lock()
        self._wrr = []              # weighted round-robin pool of pipes
        self._wrr_idx = 0
        self._since_ack = 0

    # ---------------- pipes ----------------
    def add_pipe(self, reader, writer, name="", weight=1.0):
        pipe = _Pipe(self, reader, writer, name, weight)
        self.pipes.append(pipe)
        self._rebuild_wrr()
        pipe.start()
        asyncio.create_task(self._flush_orphans())
        return pipe

    def _rebuild_wrr(self):
        pool = []
        for p in self.pipes:
            pool.extend([p] * max(1, round(p.weight)))
        self._wrr = pool
        self._wrr_idx = 0

    def update_pipe_weights(self, weights):
        """Live-update pipe weights (e.g. after a speed test) without reconnecting."""
        for p in self.pipes:
            if p.name in weights:
                try:
                    p.weight = max(0.1, float(weights[p.name]))
                except (TypeError, ValueError):
                    pass
        self._rebuild_wrr()

    def _next_pipe(self):
        wrr = self._wrr  # single snapshot: safe if another thread rebuilds mid-call
        if not wrr:
            return None
        self._wrr_idx = (self._wrr_idx + 1) % len(wrr)
        return wrr[self._wrr_idx]

    async def _drop_pipe(self, pipe):
        orphans = []
        async with self._send_lock:
            if pipe in self.pipes:
                self.pipes.remove(pipe)
                self._rebuild_wrr()
            try:
                pipe.writer.close()
            except Exception:
                pass
            for seq, (p, raw) in list(self._unacked.items()):
                if p is pipe:
                    orphans.append((seq, raw))
        for seq, raw in orphans:
            await self._resend(seq, raw)

    async def _resend(self, seq, raw):
        async with self._send_lock:
            entry = self._unacked.get(seq)
            if entry is None or entry[1] is not raw:
                return  # already acked
            for _ in range(len(self._wrr)):
                pipe = self._next_pipe()
                if pipe is None:
                    return
                try:
                    pipe.writer.write(raw)
                    await pipe.writer.drain()
                    pipe.bytes_sent += len(raw)
                    self._unacked[seq] = [pipe, raw]
                    return
                except (ConnectionError, OSError, asyncio.IncompleteReadError, RuntimeError):
                    asyncio.create_task(self._drop_pipe(pipe))
            # No live pipe right now; entry stays and flushes when one returns.

    async def _flush_orphans(self):
        async with self._send_lock:
            items = [(s, r) for s, (p, r) in self._unacked.items() if p not in self.pipes]
        for seq, raw in items:
            await self._resend(seq, raw)

    # ---------------- sending ----------------
    async def send_frame(self, ftype, stream_id, payload=b""):
        if self.closed:
            raise NoPipesError("session closed")
        while len(self._unacked) >= MAX_UNACKED:
            if self.closed:
                raise NoPipesError("session closed")
            await asyncio.sleep(0.05)
        for _ in range(150):  # wait ~30s for a pipe
            if self.pipes or self.closed:
                break
            await asyncio.sleep(0.2)
        if self.closed or not self.pipes:
            raise NoPipesError("no live pipes")
        async with self._send_lock:
            self._send_seq += 1
            seq = self._send_seq
            raw = encode_frame(seq, stream_id, ftype, payload)
            for _ in range(len(self._wrr)):
                pipe = self._next_pipe()
                if pipe is None:
                    break
                try:
                    pipe.writer.write(raw)
                    await pipe.writer.drain()
                    pipe.bytes_sent += len(raw)
                    self._unacked[seq] = [pipe, raw]
                    return
                except (ConnectionError, OSError, asyncio.IncompleteReadError, RuntimeError):
                    if pipe in self.pipes:
                        self.pipes.remove(pipe)
                        self._rebuild_wrr()
                    asyncio.create_task(self._drop_pipe(pipe))
            raise NoPipesError("all pipes failed while sending")

    # ---------------- receiving ----------------
    async def _on_pipe_frame(self, pipe, frame):
        seq, stream_id, ftype, payload = frame
        pipe.bytes_recv += HDR_LEN + len(payload)
        if seq == 0:  # unsequenced control frame
            if ftype == F_ACK and len(payload) >= 4:
                (ack,) = struct.unpack("!I", payload[:4])
                async with self._send_lock:
                    for s in [s for s in self._unacked if s <= ack]:
                        del self._unacked[s]
            return
        if seq < self._recv_expected:
            self._maybe_ack()  # duplicate; make sure the sender gets its ACK
            return
        if seq == self._recv_expected:
            await self._deliver(stream_id, ftype, payload)
            self._recv_expected = seq + 1
            while self._recv_expected in self._reorder:
                s, t, p = self._reorder.pop(self._recv_expected)
                await self._deliver(s, t, p)
                self._recv_expected += 1
        else:
            if len(self._reorder) >= MAX_REORDER:
                self.close()  # safety valve; shouldn't happen on healthy links
                return
            self._reorder[seq] = (stream_id, ftype, payload)
        self._maybe_ack()

    async def _deliver(self, stream_id, ftype, payload):
        if self.frame_handler is not None:
            await self.frame_handler(self, stream_id, ftype, payload)

    def _maybe_ack(self):
        self._since_ack += 1
        if self._since_ack >= ACK_EVERY:
            self._since_ack = 0
            asyncio.create_task(self._send_ack())

    async def _send_ack(self):
        raw = encode_frame(0, 0, F_ACK, struct.pack("!I", self._recv_expected - 1))
        async with self._send_lock:
            pipes = list(self.pipes)
        for pipe in pipes:
            try:
                pipe.writer.write(raw)
                await pipe.writer.drain()
                return
            except (ConnectionError, OSError, asyncio.IncompleteReadError, RuntimeError):
                asyncio.create_task(self._drop_pipe(pipe))

    def close(self):
        self.closed = True
        for pipe in list(self.pipes):
            try:
                pipe.writer.close()
            except Exception:
                pass
