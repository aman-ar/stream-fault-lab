"""Pass, discard, or impair encrypted WebRTC traffic without changing signaling."""

import asyncio
from collections import OrderedDict
import random
import time

from .media_packets import answer_metadata, packet_kind, stun_usernames


class _Upstream(asyncio.DatagramProtocol):
    def __init__(self, relay, address):
        self.relay, self.address = relay, address
        self.transport = None
        self.last_seen = time.monotonic()
        self.payloads = {}

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, _):
        self.last_seen = time.monotonic()
        if self.relay.udp:
            self.relay.send_udp(
                data, lambda packet: self.relay.udp.sendto(packet, self.address), self, "downstream"
            )


class MediaRelay(asyncio.DatagramProtocol):
    def __init__(
        self,
        events,
        host="0.0.0.0",
        udp_port=8189,
        tcp_port=8186,
        upstream_udp_port=18189,
        upstream_tcp_port=18186,
    ):
        self.events, self.host = events, host
        self.udp_port, self.tcp_port = udp_port, tcp_port
        self.upstream_udp_port, self.upstream_tcp_port = upstream_udp_port, upstream_tcp_port
        self.udp = self.tcp = self.reaper = None
        self.clients, self.pending = {}, {}
        self.tasks, self.writers = set(), set()
        self.answers = OrderedDict()
        self.blocked = False
        self.impairment = None
        self.generation = 0
        self.random = random.Random(42)
        self.flows = {}
        self.scheduled = {}
        self.queued_bytes = 0
        self.counters = {
            f"{kind}_{action}_{unit}": 0
            for kind in ("udp", "tcp")
            for action in ("forwarded", "dropped")
            for unit in ("packets", "bytes")
        }
        self.classes = {
            kind: {action: 0 for action in ("forwarded", "dropped")}
            for kind in ("audio", "video", "rtp", "rtcp", "stun", "dtls", "other")
        }

    async def start(self):
        if self.udp:
            return
        loop = asyncio.get_running_loop()
        self.udp, _ = await loop.create_datagram_endpoint(
            lambda: self, local_addr=(self.host, self.udp_port)
        )
        self.udp_port = self.udp.get_extra_info("sockname")[1]
        try:
            self.tcp = await asyncio.start_server(self._accept, self.host, self.tcp_port)
            self.tcp_port = self.tcp.sockets[0].getsockname()[1]
        except BaseException:
            self.udp.close()
            self.udp = None
            raise
        self.reaper = asyncio.create_task(self._reap())
        self.events.emit(
            "media-relay",
            "relay_started",
            "WebRTC UDP and TCP forwarding enabled",
            udp_port=self.udp_port,
            tcp_port=self.tcp_port,
        )

    def register_answer(self, body, previous=None):
        ufrags, payloads = answer_metadata(body, previous)
        for ufrag in ufrags:
            self.answers[ufrag] = payloads
            self.answers.move_to_end(ufrag)
        while len(self.answers) > 2000:
            self.answers.popitem(last=False)
        return payloads

    def _cancel_delayed(self):
        for handle, (data, kind) in self.scheduled.items():
            handle.cancel()
            self._count("udp", data, kind, True)
        self.scheduled.clear()
        self.queued_bytes = 0
        self.flows.clear()
        self.generation += 1

    def configure(self, impairment=None):
        if impairment and not self.udp:
            raise RuntimeError("WebRTC media relay is not running.")
        self._cancel_delayed()
        self.impairment = dict(impairment) if impairment else None
        self.blocked = bool(impairment and impairment["mode"] == "all")
        self.random.seed((impairment or {}).get("seed", 42))
        self.events.emit(
            "media-relay",
            "media_blocked" if impairment else "media_restored",
            "Media fault applied; signaling stays available"
            if impairment
            else "Media forwarding restored",
            **self.status(),
        )

    def set_blocked(self, blocked):
        if self.blocked == blocked and (blocked or self.impairment is None):
            return
        self.configure(dict(mode="all", direction="both") if blocked else None)

    def _classify(self, data, context):
        if context is not None:
            for fragment in stun_usernames(data):
                if fragment in self.answers:
                    context.payloads = self.answers[fragment]
                    break
        return packet_kind(data, context.payloads if context is not None else {})

    def _count(self, transport, data, kind, dropped):
        action = "dropped" if dropped else "forwarded"
        self.counters[f"{transport}_{action}_packets"] += 1
        self.counters[f"{transport}_{action}_bytes"] += len(data)
        self.classes[kind][action] += 1

    def _decision(self, transport, data, context, direction):
        kind = self._classify(data, context)
        config = self.impairment
        if not config or config.get("direction", "both") not in ("both", direction):
            return kind, False, 0
        mode = config["mode"]
        if mode == "all" or mode == transport:
            return kind, True, 0
        media = kind in ("audio", "video", "rtp")
        if mode == "rtp" and media or mode == kind and mode in ("audio", "video"):
            return kind, True, 0
        if mode != "quality" or not (media or config.get("include_control", False)):
            return kind, False, 0
        key = (id(context), transport, direction)
        flow = self.flows.setdefault(key, dict(next_send=0, burst=0))
        if flow["burst"]:
            flow["burst"] -= 1
            return kind, True, 0
        if self.random.random() * 100 < config.get("loss_pct", 0):
            flow["burst"] = max(0, config.get("burst_packets", 1) - 1)
            return kind, True, 0
        milliseconds = max(
            0,
            config.get("latency_ms", 0)
            + self.random.uniform(-config.get("jitter_ms", 0), config.get("jitter_ms", 0)),
        )
        if transport == "udp" and self.random.random() * 100 < config.get("reorder_pct", 0):
            milliseconds += max(20, config.get("jitter_ms", 0))
        now = time.monotonic()
        rate = config.get("bandwidth_kbps", 0)
        if rate:
            # Per-peer, per-direction leaky bucket. Drop beyond two seconds of backlog.
            due = max(now, flow["next_send"]) + len(data) * 8 / (rate * 1000)
            if due - now > 2:
                return kind, True, 0
            flow["next_send"] = due
            milliseconds += (due - now) * 1000
        delay = milliseconds / 1000
        return kind, delay > 2, min(delay, 2)

    def transfer(self, transport, data):
        # Compatibility for simple callers; real traffic uses a peer context.
        kind, dropped, _ = self._decision(transport, data, None, "upstream")
        self._count(transport, data, kind, dropped)
        return not dropped

    def send_udp(self, data, send, context, direction):
        kind, dropped, delay = self._decision("udp", data, context, direction)
        if dropped or (
            delay and (len(self.scheduled) >= 4096 or self.queued_bytes + len(data) > 8_000_000)
        ):
            self._count("udp", data, kind, True)
            return
        if not delay:
            self._count("udp", data, kind, False)
            send(data)
            return
        generation = self.generation
        self.queued_bytes += len(data)
        handle = None

        def deliver():
            self.scheduled.pop(handle, None)
            self.queued_bytes -= len(data)
            if generation == self.generation and self.udp:
                self._count("udp", data, kind, False)
                send(data)

        handle = asyncio.get_running_loop().call_later(delay, deliver)
        self.scheduled[handle] = (data, kind)

    def datagram_received(self, data, address):
        if not self.udp:
            return
        upstream = self.clients.get(address)
        if upstream:
            upstream.last_seen = time.monotonic()
            self.send_udp(data, upstream.transport.sendto, upstream, "upstream")
        elif address not in self.pending and len(self.clients) + len(self.pending) < 256:
            self.pending[address] = asyncio.create_task(self._connect_udp(address, data))

    async def _connect_udp(self, address, data):
        transport = None
        try:
            transport, protocol = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: _Upstream(self, address), remote_addr=("127.0.0.1", self.upstream_udp_port)
            )
            if not self.udp:
                transport.close()
                return
            self.clients[address] = protocol
            self.send_udp(data, transport.sendto, protocol, "upstream")
        except asyncio.CancelledError:
            if transport:
                transport.close()
            raise
        except OSError as exc:
            self.events.emit("media-relay", "udp_connect_failed", type(exc).__name__, level="WARN")
        finally:
            self.pending.pop(address, None)

    async def _reap(self):
        while True:
            await asyncio.sleep(60)
            for address, protocol in list(self.clients.items()):
                if time.monotonic() - protocol.last_seen > 900:
                    self.clients.pop(address)
                    protocol.transport.close()
                    self._forget_flow(protocol)

    def _forget_flow(self, context):
        for key in list(self.flows):
            if key[0] == id(context):
                self.flows.pop(key, None)

    def _accept(self, reader, writer):
        if not self.tcp or len(self.tasks) >= 256:
            writer.close()
            return
        self.writers.add(writer)
        task = asyncio.create_task(self._connect_tcp(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _pump(self, reader, writer, context, direction):
        while True:
            try:
                header = await reader.readexactly(2)
                data = await reader.readexactly(int.from_bytes(header, "big"))
            except asyncio.IncompleteReadError:
                return
            kind, dropped, delay = self._decision("tcp", data, context, direction)
            framed = header + data
            generation = self.generation
            if not dropped and delay:
                await asyncio.sleep(delay)
                dropped = generation != self.generation
            self._count("tcp", framed, kind, dropped)
            if not dropped:
                writer.write(framed)
                await writer.drain()

    async def _connect_tcp(self, reader, writer):
        upstream = None
        pumps = []
        context = _Upstream(self, None)
        try:
            remote, upstream = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.upstream_tcp_port), 3
            )
            self.writers.add(upstream)
            pumps = [
                asyncio.create_task(self._pump(reader, upstream, context, "upstream")),
                asyncio.create_task(self._pump(remote, writer, context, "downstream")),
            ]
            done, _ = await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (OSError, asyncio.TimeoutError) as exc:
            self.events.emit(
                "media-relay", "tcp_connection_ended", type(exc).__name__, level="INFO"
            )
        finally:
            self._forget_flow(context)
            for task in pumps:
                task.cancel()
            await asyncio.gather(*pumps, return_exceptions=True)
            for stream in (writer, upstream):
                if stream:
                    stream.close()
                    self.writers.discard(stream)
                    try:
                        await stream.wait_closed()
                    except OSError:
                        pass

    def status(self):
        return dict(
            running=self.udp is not None and self.tcp is not None,
            blocked=self.blocked,
            impairment=dict(self.impairment) if self.impairment else None,
            udp_clients=len(self.clients),
            tcp_clients=len(self.tasks),
            queued_packets=len(self.scheduled),
            queued_bytes=self.queued_bytes,
            classified_packets={kind: dict(counts) for kind, counts in self.classes.items()},
            **self.counters,
        )

    async def close(self):
        server, self.tcp = self.tcp, None
        if server:
            server.close()
        self._cancel_delayed()
        if self.udp:
            self.udp.close()
            self.udp = None
        jobs = (
            list(self.tasks) + list(self.pending.values()) + ([self.reaper] if self.reaper else [])
        )
        for writer in list(self.writers):
            writer.close()
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        for protocol in self.clients.values():
            protocol.transport.close()
        self.clients.clear()
        self.pending.clear()
        self.answers.clear()
        self.reaper = None
        self.blocked = False
        self.impairment = None
        if server:
            await server.wait_closed()
            self.events.emit(
                "media-relay", "relay_stopped", "WebRTC relay stopped", **self.status()
            )
