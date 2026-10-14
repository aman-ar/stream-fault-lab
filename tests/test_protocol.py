import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

from lab.events import Events
from lab.runtime import Runtime


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / ".runtime").mkdir()
        self.events = Events(self.root / ".runtime/traces")
        self.runtime = Runtime(self.root, "10.0.2.2", self.events, None)

    async def asyncTearDown(self):
        self.events.close()
        self.directory.cleanup()

    async def test_whep_does_not_prepare_hls(self):
        config = self.runtime.config(True)
        self.assertTrue(config["webrtc"])
        self.assertFalse(config["hls"])
        self.assertEqual(list(config["paths"]), ["live"])
        self.runtime.spawn = AsyncMock()
        await self.runtime._bridge()
        self.runtime.spawn.assert_not_awaited()

    async def test_switch_keeps_origin_and_source_and_saves_choice(self):
        self.runtime.stop = AsyncMock()
        self.runtime.start_server = AsyncMock()
        await self.runtime.set_protocol("hls")
        stopped = [call.args[0] for call in self.runtime.stop.await_args_list]
        self.assertEqual(stopped, ["edge", "hls-bridge"])
        self.runtime.start_server.assert_awaited_once_with("edge")
        config = self.runtime.config(True)
        self.assertFalse(config["webrtc"])
        self.assertTrue(config["hls"])
        self.assertEqual(list(config["paths"]), ["live-hls"])
        self.assertTrue(self.runtime.config(False)["webrtc"], "WHIP ingest stays available for OBS")
        restarted = Runtime(self.root, "10.0.2.2", self.events, None)
        self.assertEqual(restarted.protocol, "hls")

    async def test_failed_switch_restores_previous_protocol(self):
        self.runtime.stop = AsyncMock()
        self.runtime.start_server = AsyncMock(side_effect=[RuntimeError("edge failed"), None])
        with self.assertRaisesRegex(RuntimeError, "edge failed"):
            await self.runtime.set_protocol("hls")
        self.assertEqual(self.runtime.protocol, "whep")
        self.assertFalse((self.root / ".runtime/protocol").exists())
        self.assertEqual(self.runtime.start_server.await_count, 2)
