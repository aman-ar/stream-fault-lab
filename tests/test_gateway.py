import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestClient, TestServer

from lab.events import Events
from lab.app import admin_app
from lab.gateway import Faults, Gateway, gateway_app


class FakeRuntime:
    def __init__(self):
        self.media_relay = SimpleNamespace(set_blocked=lambda value: setattr(self.media_relay, "blocked", value), blocked=False, udp_port=8189, tcp_port=8186)
        self.edge = True
        self.fail_restart = False
        self.protocol = "whep"
        self.ice_blackhole = SimpleNamespace(start=AsyncMock(), close=AsyncMock(), udp_port=18289, tcp_port=18286)

    def alive(self, name):
        return self.edge

    async def set_edge(self, running):
        if running and self.fail_restart:
            raise RuntimeError("simulated restart failure")
        self.edge = running

    async def set_protocol(self, protocol):
        self.protocol = protocol


class GatewayContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.events = Events(Path(self.directory.name))
        self.runtime = FakeRuntime()
        self.faults = Faults(self.runtime, self.events)
        self.seen = []
        self.answer = "v=0\r\na=candidate:1 1 UDP 123 127.0.0.1 8189 typ host\r\n"

        async def upstream(request):
            body = await request.read()
            self.seen.append((request.method, request.path, request.query_string, body))
            if request.method == "POST":
                session_id = uuid.uuid4().hex
                return web.Response(status=201, text=self.answer, headers={
                    "Content-Type": "application/sdp", "Location": f"/live/whep/{session_id}?secret=server-secret",
                    "ETag": '"etag-value"'})
            if request.method == "PATCH":
                return web.Response(status=204, headers={"ETag": '"etag-value"'})
            if request.method == "DELETE":
                return web.Response(status=200)
            if request.path.endswith(".m3u8"):
                base = str(self.upstream.make_url("/"))[:-1]
                return web.Response(text=f'#EXTM3U\n#EXT-X-MAP:URI="/live-hls/init.mp4"\n{base}/live-hls/seg.mp4\n', content_type="application/vnd.apple.mpegurl")
            return web.Response(body=b"media-data", content_type="video/mp4")

        app = web.Application()
        app.router.add_route("*", "/{path:.*}", upstream)
        self.upstream = TestServer(app)
        await self.upstream.start_server()
        self.session = ClientSession()
        upstream_base = str(self.upstream.make_url("/"))[:-1]
        self.gateway = Gateway(self.session, self.faults, self.events, upstream_base, upstream_base)
        self.client = TestClient(TestServer(gateway_app(self.gateway), handler_cancellation=True, shutdown_timeout=0.1))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.faults.close()
        await self.client.close()
        await self.session.close()
        await self.upstream.close()
        self.events.close()
        self.directory.cleanup()

    async def post(self):
        response = await self.client.post("/live/whep", data="offer-secret", headers={"Content-Type": "application/sdp"})
        self.assertEqual(response.status, 201)
        self.assertEqual(await response.text(), self.answer)
        return response.headers["Location"]

    async def test_fresh_posts_and_lifecycle_route_to_their_own_resources(self):
        first, second = await self.post(), await self.post()
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("/live/whep/session/"))
        self.assertNotIn("server-secret", first)
        patch = await self.client.patch(first, data="patch-secret", headers={"If-Match": '"etag-value"'})
        self.assertEqual(patch.status, 204)
        self.assertEqual(patch.headers["ETag"], '"etag-value"')
        self.assertEqual((await self.client.delete(second)).status, 200)
        self.assertEqual((await self.client.delete(first)).status, 200)
        lifecycle = [item for item in self.seen if item[0] in ("PATCH", "DELETE")]
        self.assertEqual(lifecycle[0][1], lifecycle[2][1])
        self.assertNotEqual(lifecycle[0][1], lifecycle[1][1])
        self.assertEqual(lifecycle[0][2], "secret=server-secret")
        trace = json.dumps(list(self.events.records))
        for secret in ("offer-secret", "patch-secret", "server-secret"):
            self.assertNotIn(secret, trace)
        self.assertEqual((await self.client.delete(first)).status, 404)

    async def test_503_is_not_forwarded_and_delete_is_not_blocked(self):
        location = await self.post()
        await self.faults.apply("http503", "whep-post", 5)
        response = await self.client.post("/live/whep", data="offer")
        self.assertEqual(response.status, 503)
        self.assertEqual(response.headers["Retry-After"], "5")
        self.assertEqual(len(self.seen), 1)
        self.assertEqual((await self.client.delete(location)).status, 200)
        finished = [e for e in self.events.records if e["event"] == "request_finished" and e["response_status"] == 503][0]
        self.assertIsNone(finished["upstream_status"])
        self.assertEqual(finished["request_bytes"], 5)

    async def test_hls_playlist_and_media_scopes_are_independent(self):
        self.runtime.protocol = "hls"
        await self.faults.apply("http502", "hls-playlist", 5)
        self.assertEqual((await self.client.get("/live/index.m3u8")).status, 502)
        media = await self.client.get("/live/seg.mp4")
        self.assertEqual(await media.read(), b"media-data")
        await self.faults.apply("http503", "hls-media", 5)
        self.assertEqual((await self.client.get("/live/index.m3u8")).status, 200)
        self.assertEqual((await self.client.get("/live/seg.mp4")).status, 503)

    async def test_playlist_urls_and_queries_stay_on_gateway(self):
        self.runtime.protocol = "hls"
        response = await self.client.get("/live/index.m3u8?_HLS_msn=3&token=client-secret")
        self.assertEqual(await response.text(), '#EXTM3U\n#EXT-X-MAP:URI="/live/init.mp4"\n/live/seg.mp4\n')
        self.assertEqual(self.seen[0][1], "/live-hls/index.m3u8")
        self.assertEqual(self.seen[0][2], "_HLS_msn=3&token=client-secret")
        self.assertNotIn("client-secret", json.dumps(list(self.events.records)))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_cors_preflight_and_streamed_media_headers(self):
        self.runtime.protocol = "hls"
        preflight = await self.client.options("/live/whep")
        self.assertEqual(preflight.status, 204)
        self.assertIn("PATCH", preflight.headers["Access-Control-Allow-Methods"])
        media = await self.client.get("/live/seg.mp4")
        self.assertEqual(media.headers["Access-Control-Allow-Origin"], "*")
        self.assertIn("Content-Range", media.headers["Access-Control-Expose-Headers"])
        self.assertEqual(await media.read(), b"media-data")

    async def test_delay_then_automatic_expiry(self):
        self.runtime.protocol = "hls"
        await self.faults.apply("delay", "hls-playlist", 1, 150)
        start = time.monotonic()
        response = await self.client.get("/live/index.m3u8")
        self.assertEqual(response.status, 200)
        self.assertGreaterEqual(time.monotonic() - start, 0.14)
        await asyncio.sleep(1)
        self.assertIsNone(self.faults.status())
        self.assertIn("fault_expired", [e["event"] for e in self.events.records])

    async def test_timeout_sends_no_http_error_before_client_deadline(self):
        await self.faults.apply("timeout", "whep-post", 5)
        with self.assertRaises(asyncio.TimeoutError):
            await self.client.post("/live/whep", data="offer", timeout=ClientTimeout(total=0.2))
        self.assertEqual(self.seen, [])
        await asyncio.sleep(0.15)
        finished = [e for e in self.events.records if e["event"] == "request_finished"]
        self.assertTrue(finished)
        self.assertIsNone(finished[-1]["response_status"])

    async def test_edge_failure_restore_is_confirmed_and_failure_is_retained(self):
        await self.faults.apply("edge-down", "playback", 10)
        self.assertFalse(self.runtime.edge)
        self.runtime.fail_restart = True
        with self.assertRaises(RuntimeError):
            await self.faults.restore()
        self.assertEqual(self.faults.status()["preset"], "edge-down")
        self.runtime.fail_restart = False
        await self.faults.restore()
        self.assertTrue(self.runtime.edge)
        self.assertIsNone(self.faults.status())

    async def test_invalid_fault_values_do_not_modify_current_fault(self):
        await self.faults.apply("http503", "whep-post", 10)
        fault_id = self.faults.status()["id"]
        for duration in (0, 601, True, float("nan")):
            with self.assertRaises(ValueError):
                await self.faults.apply("http502", "whep-post", duration)
        self.assertEqual(self.faults.status()["id"], fault_id)

    async def test_untrusted_location_is_rejected(self):
        with self.assertRaises(ValueError):
            self.gateway.rewrite_location("http://other.example/live/whep/session", self.gateway.whep_upstream + "/live/whep")

    async def test_hls_session_redirect_stays_on_gateway(self):
        location = self.gateway.rewrite_hls_location("/live-hls/index.m3u8?cookieCheck=1", self.gateway.hls_upstream + "/live-hls/index.m3u8")
        self.assertEqual(location, "/live/index.m3u8?cookieCheck=1")
        with self.assertRaises(ValueError):
            self.gateway.rewrite_hls_location("https://other.test/live-hls/index.m3u8", self.gateway.hls_upstream + "/live-hls/index.m3u8")

    async def test_unselected_output_never_reaches_edge(self):
        response = await self.client.get("/live/index.m3u8")
        self.assertEqual(response.status, 409)
        self.assertIn("HLS output is disabled", await response.text())
        self.runtime.protocol = "hls"
        response = await self.client.post("/live/whep", data="offer")
        self.assertEqual(response.status, 409)
        self.assertIn("WHEP output is disabled", await response.text())
        self.assertEqual(self.seen, [])

    async def test_wrong_protocol_scope_preserves_active_fault(self):
        await self.faults.apply("http503", "whep-post", 10)
        fault_id = self.faults.status()["id"]
        with self.assertRaises(ValueError):
            await self.faults.apply("http502", "hls-media", 10)
        self.assertEqual(self.faults.status()["id"], fault_id)

    async def test_ice_fault_keeps_201_and_rewrites_udp_and_tcp_candidates(self):
        self.answer = ('v=0\r\na=ice-pwd:private-ice-password\r\n'
                       'a=candidate:1 1 UDP 123 10.0.2.2 8189 typ host\r\n'
                       'a=candidate:2 1 TCP 122 10.0.2.2 8186 typ host tcptype passive\r\n')
        await self.faults.apply("ice-unreachable", "whep-post", 5)
        response = await self.client.post("/live/whep", data="offer")
        self.assertEqual(response.status, 201)
        changed = await response.text()
        self.assertIn("10.0.2.2 18289 typ host", changed)
        self.assertIn("10.0.2.2 18286 typ host tcptype passive", changed)
        self.assertIn("Location", response.headers)
        self.assertEqual((await self.client.delete(response.headers['Location'])).status, 200)
        self.runtime.ice_blackhole.start.assert_awaited_once()
        self.assertNotIn("private-ice-password", json.dumps(list(self.events.records)))
        await self.faults.restore()
        response = await self.client.post("/live/whep", data="offer")
        self.assertEqual(await response.text(), self.answer)

    async def test_dtls_fault_keeps_201_and_only_changes_fingerprints(self):
        fingerprint = ':'.join(['AB'] * 32)
        self.answer = f'v=0\r\na=fingerprint:sha-256 {fingerprint}\r\na=candidate:1 1 UDP 123 10.0.2.2 8189 typ host\r\na=fingerprint:sha-256 {fingerprint}\r\n'
        await self.faults.apply("dtls-fingerprint", "whep-post", 5)
        response = await self.client.post("/live/whep", data="offer")
        self.assertEqual(response.status, 201)
        changed = await response.text()
        self.assertEqual(changed, self.answer.replace('sha-256 AB:', 'sha-256 AA:'))
        self.assertIn("Location", response.headers)
        self.assertEqual(len(self.seen), 1)
        self.assertNotIn(fingerprint, json.dumps(list(self.events.records)))
        await self.faults.restore()
        response = await self.client.post("/live/whep", data="offer")
        self.assertEqual(await response.text(), self.answer)

    async def test_transport_faults_are_rejected_for_hls(self):
        self.runtime.protocol = "hls"
        for preset in ('ice-unreachable', 'dtls-fingerprint'):
            with self.assertRaises(ValueError):
                await self.faults.apply(preset, "whep-post", 5)
        self.runtime.ice_blackhole.start.assert_not_awaited()

    async def test_midstream_outage_leaves_signaling_and_cleanup_healthy(self):
        await self.faults.apply("media-outage", "playback", 1)
        self.assertTrue(self.runtime.media_relay.blocked)
        location = await self.post()
        self.assertEqual((await self.client.patch(location, data="patch")).status, 204)
        self.assertEqual((await self.client.delete(location)).status, 200)
        self.assertTrue(self.runtime.edge)
        await asyncio.sleep(1.1)
        self.assertFalse(self.runtime.media_relay.blocked)
        self.assertIsNone(self.faults.status())

    async def test_midstream_outage_rejects_wrong_protocol_or_scope(self):
        with self.assertRaises(ValueError):
            await self.faults.apply("media-outage", "whep-post", 5)
        self.runtime.protocol = "hls"
        with self.assertRaises(ValueError):
            await self.faults.apply("media-outage", "playback", 5)

    async def test_healthy_candidates_use_relay_public_ports(self):
        self.answer = ('v=0\r\na=candidate:1 1 UDP 123 10.0.2.2 18189 typ host\r\n'
                       'a=candidate:2 1 TCP 122 10.0.2.2 18186 typ host tcptype passive\r\n')
        response = await self.client.post("/live/whep", data="offer")
        self.assertEqual(response.status, 201)
        answer = await response.text()
        self.assertIn("10.0.2.2 8189 typ host", answer)
        self.assertIn("10.0.2.2 8186 typ host tcptype passive", answer)
        self.assertNotIn("18189", answer)


class TraceTests(unittest.TestCase):
    def test_process_url_credentials_are_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            events = Events(Path(directory))
            events.emit("source", "server_log", "Cannot read https://user:password@example.test/live.m3u8?token=secret")
            events.close()
            trace = next(Path(directory).glob("*.jsonl")).read_text()
            for value in ("password", "secret", "example.test"):
                self.assertNotIn(value, trace)


class LocalControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.events = Events(Path(self.directory.name))
        self.runtime = FakeRuntime()
        self.faults = Faults(self.runtime, self.events)
        self.client = TestClient(TestServer(admin_app(self.runtime, self.faults, self.events, "127.0.0.1")))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.faults.close()
        self.events.close()
        self.directory.cleanup()

    async def test_control_rejects_foreign_origin_and_non_json(self):
        foreign = await self.client.post("/api/restore", json={}, headers={"Origin": "https://other.example"})
        self.assertEqual(foreign.status, 403)
        form = await self.client.post("/api/restore", data={"preset": "edge-down"})
        self.assertEqual(form.status, 415)
        self.assertTrue(self.runtime.edge)

    async def test_controller_rejects_untrusted_host(self):
        response = await self.client.get("/api/status", headers={"Host": "other.example:9091"})
        self.assertEqual(response.status, 403)

    async def test_malformed_json_does_not_apply_fault(self):
        response = await self.client.post("/api/fault", data='{"preset":', headers={"Content-Type": "application/json"})
        self.assertEqual(response.status, 400)
        self.assertIsNone(self.faults.status())

    async def test_selecting_protocol_restores_active_fault(self):
        await self.faults.apply("edge-down", "playback", 10)
        response = await self.client.post("/api/protocol", json={"protocol": "hls"})
        self.assertEqual(response.status, 200)
        self.assertEqual((await response.json())["protocol"], "hls")
        self.assertTrue(self.runtime.edge)
        self.assertIsNone(self.faults.status())

    async def test_invalid_protocol_preserves_fault_and_choice(self):
        await self.faults.apply("http503", "whep-post", 10)
        response = await self.client.post("/api/protocol", json={"protocol": "both"})
        self.assertEqual(response.status, 400)
        self.assertEqual(self.runtime.protocol, "whep")
        self.assertIsNotNone(self.faults.status())


if __name__ == "__main__":
    unittest.main()
