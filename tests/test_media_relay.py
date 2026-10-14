import asyncio
from pathlib import Path
import tempfile
import unittest

from lab.events import Events
from lab.media_relay import MediaRelay


class QueueDatagrams(asyncio.DatagramProtocol):
    def __init__(self):
        self.received = asyncio.Queue()

    def datagram_received(self, data, address):
        self.received.put_nowait((data, address))


class MediaRelayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.events = Events(Path(self.directory.name))
        loop = asyncio.get_running_loop()
        self.upstream_udp, self.server_datagrams = await loop.create_datagram_endpoint(
            QueueDatagrams, local_addr=("127.0.0.1", 0)
        )
        self.tcp_received = asyncio.Queue()
        self.server_writers = []

        async def receive(reader, writer):
            self.server_writers.append(writer)
            await self.tcp_received.put((reader, writer))

        self.upstream_tcp = await asyncio.start_server(receive, "127.0.0.1", 0)
        self.relay = MediaRelay(
            self.events,
            host="127.0.0.1",
            udp_port=0,
            tcp_port=0,
            upstream_udp_port=self.upstream_udp.get_extra_info("sockname")[1],
            upstream_tcp_port=self.upstream_tcp.sockets[0].getsockname()[1],
        )
        await self.relay.start()
        self.client_udp, self.client_datagrams = await loop.create_datagram_endpoint(
            QueueDatagrams, local_addr=("127.0.0.1", 0)
        )
        self.client_writers = []

    async def asyncTearDown(self):
        self.client_udp.close()
        self.upstream_udp.close()
        for writer in self.client_writers + self.server_writers:
            writer.close()
            await writer.wait_closed()
        await self.relay.close()
        self.upstream_tcp.close()
        await self.upstream_tcp.wait_closed()
        self.events.close()
        self.directory.cleanup()

    async def test_udp_both_directions_drop_without_buffering_and_restore(self):
        self.client_udp.sendto(b"hello", ("127.0.0.1", self.relay.udp_port))
        data, upstream_address = await asyncio.wait_for(self.server_datagrams.received.get(), 1)
        self.assertEqual(data, b"hello")
        self.upstream_udp.sendto(b"reply", upstream_address)
        data, _ = await asyncio.wait_for(self.client_datagrams.received.get(), 1)
        self.assertEqual(data, b"reply")
        self.relay.set_blocked(True)
        self.client_udp.sendto(b"discard-in", ("127.0.0.1", self.relay.udp_port))
        self.upstream_udp.sendto(b"discard-out", upstream_address)
        await asyncio.sleep(0.05)
        self.assertTrue(self.server_datagrams.received.empty())
        self.assertTrue(self.client_datagrams.received.empty())
        self.assertEqual(
            self.relay.status()["udp_dropped_bytes"], len(b"discard-in") + len(b"discard-out")
        )
        self.relay.set_blocked(False)
        self.upstream_udp.sendto(b"new-reply", upstream_address)
        data, _ = await asyncio.wait_for(self.client_datagrams.received.get(), 1)
        self.assertEqual(data, b"new-reply")
        self.assertNotIn("discard-in", str(list(self.events.records)))

    async def test_tcp_both_directions_drop_and_reuse_open_connection(self):
        client_reader, client_writer = await asyncio.open_connection(
            "127.0.0.1", self.relay.tcp_port
        )
        self.client_writers.append(client_writer)
        server_reader, server_writer = await asyncio.wait_for(self.tcp_received.get(), 1)
        client_writer.write(b"\x00\x05hello")
        await client_writer.drain()
        self.assertEqual(await asyncio.wait_for(server_reader.readexactly(7), 1), b"\x00\x05hello")
        server_writer.write(b"\x00\x05reply")
        await server_writer.drain()
        self.assertEqual(await asyncio.wait_for(client_reader.readexactly(7), 1), b"\x00\x05reply")
        self.relay.set_blocked(True)
        # Split a frame across writes; coalesce two reverse-direction frames.
        client_writer.write(b"\x00")
        await client_writer.drain()
        await asyncio.sleep(0.01)
        client_writer.write(b"\x0adiscard-in")
        await client_writer.drain()
        server_writer.write(b"\x00\x0bdiscard-out\x00\x00")
        await server_writer.drain()
        await asyncio.sleep(0.05)
        self.assertEqual(
            self.relay.status()["tcp_dropped_bytes"], len(b"discard-in") + len(b"discard-out") + 6
        )
        self.relay.set_blocked(False)
        client_writer.write(b"\x00\x05fresh")
        await client_writer.drain()
        server_writer.write(b"\x00\x05fresh")
        await server_writer.drain()
        self.assertEqual(await asyncio.wait_for(server_reader.readexactly(7), 1), b"\x00\x05fresh")
        self.assertEqual(await asyncio.wait_for(client_reader.readexactly(7), 1), b"\x00\x05fresh")
        await asyncio.wait_for(self.relay.close(), 1)
        self.assertEqual(await asyncio.wait_for(client_reader.read(), 1), b"")
        await self.relay.start()
        self.assertTrue(self.relay.status()["running"])
