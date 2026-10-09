import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestClient, TestServer

from lab.events import Events
from lab.app import admin_app
from lab.gateway import Faults, Gateway, gateway_app


class FakeRuntime:
    def __init__(self):
        self.edge = True
        self.fail_restart = False

    def alive(self, name):
        return self.edge

    async def set_edge(self, running):
        if running and self.fail_restart:
            raise RuntimeError("simulated restart failure")
        self.edge = running


class GatewayContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.events = Events(Path(self.directory.name))
        self.runtime = FakeRuntime()
        self.faults = Faults(self.runtime, self.events)
        self.seen = []

        async def upstream(request):
            body = await request.read()
            self.seen.append((request.method, request.path, request.query_string, body))
            if request.method == "POST":
                session_id = uuid.uuid4().hex
                return web.Response(status=201, text="v=0\r\n", headers={
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
        self.assertEqual(await response.text(), "v=0\r\n")
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
        await self.faults.apply("http502", "hls-playlist", 5)
        self.assertEqual((await self.client.get("/live/index.m3u8")).status, 502)
        media = await self.client.get("/live/seg.mp4")
        self.assertEqual(await media.read(), b"media-data")
        await self.faults.apply("http503", "hls-media", 5)
        self.assertEqual((await self.client.get("/live/index.m3u8")).status, 200)
        self.assertEqual((await self.client.get("/live/seg.mp4")).status, 503)

    async def test_playlist_urls_and_queries_stay_on_gateway(self):
        response = await self.client.get("/live/index.m3u8?_HLS_msn=3&token=client-secret")
        self.assertEqual(await response.text(), '#EXTM3U\n#EXT-X-MAP:URI="/live/init.mp4"\n/live/seg.mp4\n')
        self.assertEqual(self.seen[0][1], "/live-hls/index.m3u8")
        self.assertEqual(self.seen[0][2], "_HLS_msn=3&token=client-secret")
        self.assertNotIn("client-secret", json.dumps(list(self.events.records)))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_cors_preflight_and_streamed_media_headers(self):
        preflight = await self.client.options("/live/whep")
        self.assertEqual(preflight.status, 204)
        self.assertIn("PATCH", preflight.headers["Access-Control-Allow-Methods"])
        media = await self.client.get("/live/seg.mp4")
        self.assertEqual(media.headers["Access-Control-Allow-Origin"], "*")
        self.assertIn("Content-Range", media.headers["Access-Control-Expose-Headers"])
        self.assertEqual(await media.read(), b"media-data")

    async def test_delay_then_automatic_expiry(self):
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


if __name__ == "__main__":
    unittest.main()
