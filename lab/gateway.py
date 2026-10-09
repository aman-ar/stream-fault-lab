import asyncio
from collections import OrderedDict
import re
import time
from urllib.parse import urljoin, urlsplit
import uuid

from aiohttp import ClientError, ClientTimeout, web


SCOPES = {"whep-post", "hls-playlist", "hls-media", "playback"}
PRESETS = {"http503", "http502", "delay", "timeout", "edge-down"}
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "host", "content-length"}


def forward_headers(headers):
    connection_tokens = {part.strip().lower() for part in headers.get("Connection", "").split(",")}
    return {k: v for k, v in headers.items()
            if k.lower() not in HOP_HEADERS | connection_tokens and not k.lower().startswith("access-control-")}


def request_kind(request):
    if request.path == "/live/whep" and request.method == "POST":
        return "whep-post"
    if request.path.startswith("/live/whep"):
        return "whep-lifecycle"
    if request.path.endswith(".m3u8"):
        return "hls-playlist"
    return "hls-media"


class Faults:
    def __init__(self, runtime, events):
        self.runtime = runtime
        self.events = events
        self.active = None
        self.timer = None
        self.lock = asyncio.Lock()

    def status(self):
        return dict(self.active) if self.active else None

    def match(self, kind):
        fault = self.active
        if not fault or fault["preset"] == "edge-down":
            return None
        if kind == fault["scope"] or (fault["scope"] == "playback" and kind != "whep-lifecycle"):
            return dict(fault)
        return None

    async def apply(self, preset, scope, duration, delay_ms=3000):
        if preset not in PRESETS or scope not in SCOPES:
            raise ValueError("Unknown fault preset or scope.")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not 1 <= duration <= 600:
            raise ValueError("Fault duration must be 1–600 seconds.")
        if not isinstance(delay_ms, (int, float)) or isinstance(delay_ms, bool) or not 100 <= delay_ms <= 30000:
            raise ValueError("Response delay must be 100–30000 milliseconds.")
        async with self.lock:
            await self._restore()
            fault = dict(id=uuid.uuid4().hex[:12], preset=preset, scope=scope,
                         duration=duration, delay_ms=delay_ms, expires_at=time.time() + duration)
            self.events.emit("controller", "fault_requested", f"Apply {preset}", **fault)
            try:
                if preset == "edge-down":
                    await self.runtime.set_edge(False)
                self.active = fault
                self.events.emit("controller", "fault_applied", f"{preset} active", **fault)
                self.timer = asyncio.create_task(self._expire(fault["id"], duration))
                return self.status()
            except Exception as exc:
                self.events.emit("controller", "fault_failed", type(exc).__name__, level="ERROR", **fault)
                raise

    async def _expire(self, fault_id, duration):
        await asyncio.sleep(duration)
        async with self.lock:
            if self.active and self.active["id"] == fault_id:
                self.events.emit("controller", "fault_expired", "Fault duration elapsed", fault_id=fault_id)
                try:
                    await self._restore()
                except Exception as exc:
                    self.events.emit("controller", "restore_failed", str(exc), level="ERROR", fault_id=fault_id)

    async def _restore(self):
        if self.timer and self.timer is not asyncio.current_task():
            self.timer.cancel()
            await asyncio.gather(self.timer, return_exceptions=True)
        self.timer = None
        old = self.active
        # Clear only after a required edge restart succeeds.
        if not self.runtime.alive("edge"):
            await self.runtime.set_edge(True)
        self.active = None
        if old:
            self.events.emit("controller", "fault_restored", "Edge running; HTTP fault cleared", fault_id=old["id"])

    async def restore(self):
        async with self.lock:
            self.events.emit("controller", "restore_requested", "Restore healthy requested")
            try:
                await self._restore()
                self.events.emit("controller", "healthy_confirmed", "Edge running; HTTP fault cleared")
            except Exception as exc:
                self.events.emit("controller", "restore_failed", str(exc), level="ERROR")
                raise

    async def close(self):
        if self.timer:
            self.timer.cancel()
            await asyncio.gather(self.timer, return_exceptions=True)


class Gateway:
    def __init__(self, session, faults, events,
                 whep_upstream="http://127.0.0.1:18889", hls_upstream="http://127.0.0.1:18888"):
        self.session = session
        self.faults = faults
        self.events = events
        self.whep_upstream = whep_upstream
        self.hls_upstream = hls_upstream
        self.resources = OrderedDict()

    def rewrite_location(self, location, upstream_url):
        resolved = urljoin(upstream_url, location)
        resource = urlsplit(resolved)
        trusted = urlsplit(self.whep_upstream)
        if (resource.scheme, resource.netloc) != (trusted.scheme, trusted.netloc) or not resource.path.startswith("/live/whep/"):
            raise ValueError("Unexpected WHEP resource Location from edge.")
        alias = uuid.uuid4().hex
        self.resources[alias] = resolved
        while len(self.resources) > 2000:
            self.resources.popitem(last=False)
        return "/live/whep/session/" + alias, alias

    def upstream_url(self, request, kind):
        if kind in ("whep-post", "whep-lifecycle"):
            if request.path == "/live/whep" and request.method in ("POST", "OPTIONS"):
                return self.whep_upstream + "/live/whep" + ("?" + request.query_string if request.query_string else "")
            match = re.fullmatch(r"/live/whep/session/([a-f0-9]{32})", request.path)
            if not match or match[1] not in self.resources:
                raise web.HTTPNotFound(text="Unknown WHEP session resource.")
            if request.method not in ("PATCH", "DELETE", "OPTIONS"):
                raise web.HTTPMethodNotAllowed(request.method, ["PATCH", "DELETE", "OPTIONS"])
            return self.resources[match[1]]
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            raise web.HTTPMethodNotAllowed(request.method, ["GET", "HEAD", "OPTIONS"])
        # Preserve the encoded suffix and the HLS query (including LL-HLS parameters).
        suffix = request.raw_path[len("/live/"):]
        if ".." in request.path.split("/"):
            raise web.HTTPBadRequest(text="Invalid media path.")
        return self.hls_upstream + "/live-hls/" + suffix

    def rewrite_playlist(self, body):
        text = body.decode("utf-8")
        trusted = urlsplit(self.hls_upstream)

        def uri(value):
            parsed = urlsplit(value)
            if parsed.scheme or parsed.netloc:
                if parsed.netloc != trusted.netloc:
                    raise ValueError("External HLS resource URL cannot be tested through this gateway.")
                value = parsed.path + ("?" + parsed.query if parsed.query else "")
            if value.startswith("/live-hls/"):
                value = "/live/" + value[len("/live-hls/"):]
            return value

        lines = []
        for line in text.splitlines():
            if line and not line.startswith("#"):
                line = uri(line)
            elif line.startswith("#"):
                line = re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + uri(m[1]) + '"', line)
            lines.append(line)
        return ("\n".join(lines) + "\n").encode()

    def rewrite_hls_location(self, location, upstream_url):
        resource = urlsplit(urljoin(upstream_url, location))
        trusted = urlsplit(self.hls_upstream)
        if (resource.scheme, resource.netloc) != (trusted.scheme, trusted.netloc) or not resource.path.startswith("/live-hls/"):
            raise ValueError("Unexpected HLS redirect from edge.")
        return "/live/" + resource.path[len("/live-hls/"):] + ("?" + resource.query if resource.query else "")

    async def handle(self, request):
        if request.method == "OPTIONS":
            return web.Response(status=204)
        kind = request_kind(request)
        request_id = uuid.uuid4().hex[:12]
        start = time.monotonic()
        # Queries, authorization, cookies, SDP and payloads never enter the trace.
        path = request.path
        alias = path.rsplit("/", 1)[-1] if "/session/" in path else None
        self.events.emit("gateway", "request_received", f"{request.method} {path}",
                         request_id=request_id, method=request.method, path=path,
                         kind=kind, resource_alias=alias)
        upstream_status = None
        response_status = None
        bytes_received = 0
        bytes_sent = 0
        fault = self.faults.match(kind)
        outcome = "forwarded"
        streaming_response = None
        try:
            body = await request.read()
            bytes_received = len(body)
            url = self.upstream_url(request, kind)
            if fault:
                preset = fault["preset"]
                self.events.emit("gateway", "fault_matched", f"{preset} matched {kind}",
                                 request_id=request_id, fault_id=fault["id"])
                if preset in ("http503", "http502"):
                    response_status = 503 if preset == "http503" else 502
                    outcome = "injected"
                    text = f"Lab injected HTTP {response_status}\n"
                    bytes_sent = len(text.encode())
                    return web.Response(status=response_status, text=text,
                                        headers={"Retry-After": "5"} if response_status == 503 else {})
                if preset == "timeout":
                    outcome = "injected_timeout"
                    # Intentionally send no response. Separate 60s guard bounds resources.
                    until = time.monotonic() + 60
                    while request.transport and not request.transport.is_closing() and time.monotonic() < until:
                        await asyncio.sleep(0.1)
                    if request.transport and not request.transport.is_closing():
                        response_status = 504
                        outcome = "timeout_guard_504"
                        return web.Response(status=504, text="Lab 60-second timeout guard elapsed.\n")
                    return web.Response(status=204)
                if preset == "delay":
                    await asyncio.sleep(fault["delay_ms"] / 1000)
                    outcome = "delayed"
            headers = forward_headers(request.headers)
            headers.pop("Accept-Encoding", None)
            async with self.session.request(request.method, url, headers=headers, data=body,
                                            allow_redirects=False, timeout=ClientTimeout(total=30)) as upstream:
                upstream_status = upstream.status
                response_status = upstream.status
                response_headers = forward_headers(upstream.headers)
                response_headers.pop("Content-Encoding", None)
                response_headers["Cache-Control"] = "no-store"
                if "Location" in upstream.headers:
                    if kind in ("whep-post", "whep-lifecycle"):
                        location, alias = self.rewrite_location(upstream.headers["Location"], str(upstream.url))
                    else:
                        location = self.rewrite_hls_location(upstream.headers["Location"], str(upstream.url))
                    response_headers["Location"] = location
                if "Link" in response_headers:
                    # MediaMTX has no configured STUN/TURN servers in this lab.
                    # Avoid exposing an upstream absolute lifecycle URL through Link.
                    response_headers.pop("Link")
                if kind in ("whep-post", "whep-lifecycle", "hls-playlist"):
                    data = await upstream.read()
                    if kind == "hls-playlist" and upstream.status == 200:
                        data = self.rewrite_playlist(data)
                    bytes_sent = len(data)
                    if request.method == "DELETE" and upstream.status in (200, 204, 404):
                        self.resources.pop(alias, None)
                    return web.Response(status=upstream.status, headers=response_headers, body=data)
                response = web.StreamResponse(status=upstream.status, headers=response_headers)
                streaming_response = response
                await response.prepare(request)
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    await response.write(chunk)
                    bytes_sent += len(chunk)
                await response.write_eof()
                return response
        except asyncio.CancelledError:
            outcome = "client_disconnected"
            raise
        except (ClientError, OSError, asyncio.TimeoutError) as exc:
            if streaming_response is not None and streaming_response.prepared:
                outcome = "stream_interrupted"
                streaming_response.force_close()
                return streaming_response
            response_status = 504 if isinstance(exc, asyncio.TimeoutError) else 502
            outcome = "upstream_unavailable"
            return web.Response(status=response_status, text="Lab edge is unavailable.\n")
        except ValueError as exc:
            response_status = 502
            outcome = "invalid_upstream_response"
            return web.Response(status=502, text=str(exc))
        except web.HTTPException as exc:
            response_status = exc.status
            outcome = "rejected"
            raise
        finally:
            self.events.emit("gateway", "request_finished", f"{request.method} {kind}: {response_status or outcome}",
                             level="WARN" if response_status and response_status >= 400 else "INFO",
                             request_id=request_id, method=request.method, path=path, kind=kind,
                             direction="client → gateway → edge" if upstream_status is not None else "client → gateway",
                             resource_alias=alias, fault_id=fault["id"] if fault else None,
                             upstream_status=upstream_status, response_status=response_status,
                             request_bytes=bytes_received, response_bytes=bytes_sent,
                             duration_ms=round((time.monotonic() - start) * 1000), outcome=outcome)


@web.middleware
async def cors(request, handler):
    exception = None
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
        exception = exc
    if not response.prepared:
        response.headers.update({"Access-Control-Allow-Origin": "*",
                                 "Access-Control-Allow-Methods": "GET, HEAD, POST, PATCH, DELETE, OPTIONS",
                                 "Access-Control-Allow-Headers": "Content-Type, Authorization, If-Match, Range",
                                 "Access-Control-Expose-Headers": "Location, ETag, Retry-After, Content-Range",
                                 "Cache-Control": "no-store"})
    if exception:
        raise exception
    return response


async def prepare_cors(request, response):
    # Streamed segment responses are already prepared when middleware returns.
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Expose-Headers"] = "Location, ETag, Retry-After, Content-Range"
    response.headers["Cache-Control"] = "no-store"


def gateway_app(gateway):
    app = web.Application(middlewares=[cors], client_max_size=2 * 1024 * 1024)
    async def health(_):
        return web.json_response({"gateway": "running"})
    app.router.add_get("/healthz", health)
    app.router.add_route("*", "/live/{tail:.*}", gateway.handle)
    app.on_response_prepare.append(prepare_cors)
    return app
