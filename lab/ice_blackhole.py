import asyncio


class IceBlackhole(asyncio.DatagramProtocol):
    """Discard ICE traffic at lab-owned endpoints. Never change the host firewall."""

    def __init__(self, events, host="0.0.0.0", udp_port=18289, tcp_port=18286):
        self.events = events
        self.host = host
        self.udp_port = udp_port
        self.tcp_port = tcp_port
        self.udp = None
        self.udp_closed = None
        self.tcp = None
        self.tasks = set()
        self.writers = set()
        self.counts = {}

    def status(self):
        return dict(running=self.udp is not None and self.tcp is not None, **self.counts)

    def datagram_received(self, data, address):
        self.counts["udp_packets"] += 1
        self.counts["udp_bytes"] += len(data)
        if self.counts["udp_packets"] == 1:
            self.events.emit("ice-blackhole", "traffic_received", "UDP ICE traffic reached the discard endpoint")

    def connection_lost(self, error):
        if self.udp_closed and not self.udp_closed.done():
            self.udp_closed.set_result(None)

    async def _discard_tcp(self, reader, writer):
        task = asyncio.current_task()
        if self.tcp is None or len(self.tasks) >= 128:
            writer.close()
            return
        self.tasks.add(task)
        self.writers.add(writer)
        self.counts["tcp_connections"] += 1
        if self.counts["tcp_connections"] == 1:
            self.events.emit("ice-blackhole", "traffic_received", "TCP ICE connection reached the discard endpoint")
        try:
            while data := await asyncio.wait_for(reader.read(65536), 60):
                self.counts["tcp_bytes"] += len(data)
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass
            self.writers.discard(writer)
            self.tasks.discard(task)

    async def start(self):
        if self.udp is not None:
            return
        self.counts = dict(udp_packets=0, udp_bytes=0, tcp_connections=0, tcp_bytes=0)
        try:
            loop = asyncio.get_running_loop()
            self.udp_closed = loop.create_future()
            self.udp, _ = await loop.create_datagram_endpoint(lambda: self, local_addr=(self.host, self.udp_port))
            self.udp_port = self.udp.get_extra_info("sockname")[1]
            self.tcp = await asyncio.start_server(self._discard_tcp, self.host, self.tcp_port)
            self.tcp_port = self.tcp.sockets[0].getsockname()[1]
        except OSError as exc:
            await self.close()
            raise RuntimeError("Cannot bind ICE discard ports. Stop the application using those ports and retry.") from exc
        self.events.emit("ice-blackhole", "started", "UDP and TCP ICE discard endpoints ready",
                         udp_port=self.udp_port, tcp_port=self.tcp_port)

    async def close(self):
        active = self.udp is not None or self.tcp is not None
        if self.udp:
            self.udp.close()
            self.udp = None
            await self.udp_closed
        server = self.tcp
        self.tcp = None
        if server:
            server.close()
        tasks = tuple(self.tasks)
        for writer in tuple(self.writers):
            writer.close()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if server:
            await server.wait_closed()
        if active:
            self.events.emit("ice-blackhole", "stopped", "ICE discard endpoints closed", **self.counts)
