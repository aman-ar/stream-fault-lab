import asyncio
from pathlib import Path
import socket
import tempfile
import time
import unittest

from lab.events import Events
from lab.ice_blackhole import IceBlackhole


class IceBlackholeTests(unittest.IsolatedAsyncioTestCase):
    async def test_udp_and_tcp_discard_without_reply_and_release_on_restore(self):
        with tempfile.TemporaryDirectory() as directory:
            events = Events(Path(directory))
            target = IceBlackhole(events, host='127.0.0.1', udp_port=0, tcp_port=0)
            client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            client.setblocking(False)
            writer = None
            try:
                await target.start()
                loop = asyncio.get_running_loop()
                await loop.sock_sendto(client, b'udp-private-payload', ('127.0.0.1', target.udp_port))
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(loop.sock_recvfrom(client, 100), .1)
                reader, writer = await asyncio.open_connection('127.0.0.1', target.tcp_port)
                writer.write(b'tcp-private-payload'); await writer.drain()
                with self.assertRaises(asyncio.TimeoutError):
                    await asyncio.wait_for(reader.read(1), .1)
                self.assertEqual(target.status()['udp_packets'], 1)
                self.assertEqual(target.status()['tcp_connections'], 1)
                self.assertGreater(target.status()['tcp_bytes'], 0)
                start = time.monotonic()
                await asyncio.wait_for(target.close(), 1)
                self.assertLess(time.monotonic() - start, 1)
                self.assertEqual(await asyncio.wait_for(reader.read(1), 1), b'')
                self.assertFalse(target.status()['running'])
                for kind,port in ((socket.SOCK_DGRAM,target.udp_port),(socket.SOCK_STREAM,target.tcp_port)):
                    with socket.socket(socket.AF_INET,kind) as probe:
                        probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
                        probe.bind(('127.0.0.1',port))
                await target.start(); await target.close()
                trace=''.join(p.read_text() for p in Path(directory).glob('*.jsonl'))
                self.assertNotIn('private-payload',trace)
            finally:
                if writer: writer.close(); await writer.wait_closed()
                client.close(); await target.close(); events.close()
