import asyncio
from collections import OrderedDict
import re
import time
from urllib.parse import urljoin, urlsplit, urlencode
import uuid

from aiohttp import ClientError, ClientTimeout, web
from multidict import CIMultiDict


from .faults import Faults, PRESETS, SCOPES, HTTP_CODES

HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def forward_headers(headers):
    connection_tokens = {part.strip().lower() for part in headers.get("Connection", "").split(",")}
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in HOP_HEADERS | connection_tokens
        and not k.lower().startswith("access-control-")
    }


def request_kind(request):
    if request.path == "/live/whep" and request.method == "POST":
        return "whep-post"
    if request.path.startswith("/live/whep"):
        return "whep-lifecycle"
    if request.path.endswith(".m3u8"):
        return "hls-playlist"
    return "hls-media"


def change_whep_answer(body, ice_ports=None, bad_fingerprint=False, media_host=None):
    text = body.decode("utf-8")
    candidates = fingerprints = 0
    if ice_ports:

        def candidate(match):
            nonlocal candidates
            candidates += 1
            port = ice_ports[0] if match[2].lower() == "udp" else ice_ports[1]
            prefix = match[3]
            if media_host:
                prefix = re.sub(r"\S+(\s+)$", lambda m: media_host + m[1], prefix)
            return match[1] + match[2] + prefix + str(port) + match[5]

        text = re.sub(
            r"^(a=candidate:\S+\s+\d+\s+)(udp|tcp)(\s+\d+\s+\S+\s+)(\d+)([^\r\n]*)",
            candidate,
            text,
            flags=re.MULTILINE | re.IGNORECASE,
        )
        if not candidates:
            raise ValueError("ICE fault could not find UDP/TCP candidates in the edge answer.")
    if bad_fingerprint:

        def fingerprint(match):
            nonlocal fingerprints
            fingerprints += 1
            value = match[2]
            return match[1] + f"{int(value[:2], 16) ^ 1:02X}" + value[2:]

        text = re.sub(
            r"^(a=fingerprint:\S+ )([A-Fa-f0-9]{2}(?::[A-Fa-f0-9]{2})+)(?=\r?$)",
            fingerprint,
            text,
            flags=re.MULTILINE,
        )
        if not fingerprints:
            raise ValueError(
                "DTLS fault could not find a certificate fingerprint in the edge answer."
            )
    return text.encode(), candidates, fingerprints


class Gateway:
    def __init__(
        self,
        session,
        faults,
        events,
        whep_upstream="http://127.0.0.1:18889",
        hls_upstream="http://127.0.0.1:18888",
    ):
        self.session = session
        self.faults = faults
        self.events = events
        self.whep_upstream = whep_upstream
        self.hls_upstream = hls_upstream
        self.resources = OrderedDict()
        self.metadata = {}
        self.stale_id = None
        self.stale = OrderedDict()

    def rewrite_location(self, location, upstream_url):
        resolved = urljoin(upstream_url, location)
        resource = urlsplit(resolved)
        trusted = urlsplit(self.whep_upstream)
        if (resource.scheme, resource.netloc) != (
            trusted.scheme,
            trusted.netloc,
        ) or not resource.path.startswith("/live/whep/"):
            raise ValueError("Unexpected WHEP resource Location from edge.")
        alias = uuid.uuid4().hex
        self.resources[alias] = resolved
        while len(self.resources) > 2000:
            expired, _ = self.resources.popitem(last=False)
            self.metadata.pop(expired, None)
        return "/live/whep/session/" + alias, alias

    def upstream_url(self, request, kind):
        if kind in ("whep-post", "whep-lifecycle"):
            if request.path == "/live/whep" and request.method in ("POST", "OPTIONS"):
                query = urlencode(
                    [(k, v) for k, v in request.query.items() if k != "_lab_redirect"]
                )
                return self.whep_upstream + "/live/whep" + ("?" + query if query else "")
            match = re.fullmatch(r"/live/whep/session/([a-f0-9]{32})", request.path)
            if not match or match[1] not in self.resources:
                raise web.HTTPNotFound(text="Unknown WHEP session resource.")
            if request.method not in ("PATCH", "DELETE", "OPTIONS"):
                raise web.HTTPMethodNotAllowed(request.method, ["PATCH", "DELETE", "OPTIONS"])
            return self.resources[match[1]]
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            raise web.HTTPMethodNotAllowed(request.method, ["GET", "HEAD", "OPTIONS"])
        # Preserve the encoded suffix and the HLS query (including LL-HLS parameters).
        suffix = request.raw_path[len("/live/") :]
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
                    raise ValueError(
                        "External HLS resource URL cannot be tested through this gateway."
                    )
                value = parsed.path + ("?" + parsed.query if parsed.query else "")
            if value.startswith("/live-hls/"):
                value = "/live/" + value[len("/live-hls/") :]
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
        if (resource.scheme, resource.netloc) != (
            trusted.scheme,
            trusted.netloc,
        ) or not resource.path.startswith("/live-hls/"):
            raise ValueError("Unexpected HLS redirect from edge.")
        return (
            "/live/"
            + resource.path[len("/live-hls/") :]
            + ("?" + resource.query if resource.query else "")
        )

    async def handle(self, request):
        if request.method == "OPTIONS":
            headers = CIMultiDict()
            turn = getattr(self.faults.runtime, "turn", None)
            if request.path == "/live/whep" and self.faults.runtime.protocol == "whep" and turn:
                for link in turn.links():
                    headers.add("Link", link)
            return web.Response(status=204, headers=headers)
        kind = request_kind(request)
        request_id = uuid.uuid4().hex[:12]
        start = time.monotonic()
        # Queries, authorization, cookies, SDP and payloads never enter the trace.
        path = request.path
        alias = path.rsplit("/", 1)[-1] if "/session/" in path else None
        self.events.emit(
            "gateway",
            "request_received",
            f"{request.method} {path}",
            request_id=request_id,
            method=request.method,
            path=path,
            kind=kind,
            resource_alias=alias,
        )
        upstream_status = None
        response_status = None
        bytes_received = 0
        bytes_sent = 0
        fault = self.faults.match(kind)
        if not fault or fault["id"] != self.stale_id:
            self.stale.clear()
            self.stale_id = fault["id"] if fault else None
        outcome = "forwarded"
        streaming_response = None
        try:
            protocol = "whep" if kind.startswith("whep-") else "hls"
            if protocol != self.faults.runtime.protocol:
                raise web.HTTPConflict(
                    text=f"{protocol.upper()} output is disabled. Select it in the controller.\n"
                )
            body = await request.read()
            bytes_received = len(body)
            url = self.upstream_url(request, kind)
            if fault:
                preset = fault["preset"]
                self.events.emit(
                    "gateway",
                    "fault_matched",
                    f"{preset} matched {kind}",
                    request_id=request_id,
                    fault_id=fault["id"],
                )
                if preset in HTTP_CODES:
                    response_status = HTTP_CODES[preset]
                    outcome = "injected"
                    text = f"Lab injected HTTP {response_status}\n"
                    bytes_sent = len(text.encode())
                    return web.Response(
                        status=response_status,
                        text=text,
                        headers=(
                            {"Retry-After": str(fault["options"].get("retry_after", 5))}
                            if response_status in (429, 503)
                            else {"WWW-Authenticate": 'Bearer realm="lab"'}
                            if response_status == 401
                            else {}
                        ),
                    )
                if preset == "redirect307" and request.query.get("_lab_redirect") != fault["id"]:
                    response_status, outcome = 307, "injected_redirect"
                    query = [(k, v) for k, v in request.query.items() if k != "_lab_redirect"]
                    query.append(("_lab_redirect", fault["id"]))
                    return web.Response(
                        status=307, headers={"Location": request.path + "?" + urlencode(query)}
                    )
                if preset == "stale-playlist" and path in self.stale:
                    response_status, outcome = 200, "injected_stale_playlist"
                    data, cached_headers = self.stale[path]
                    bytes_sent = len(data)
                    return web.Response(status=200, body=data, headers=cached_headers)
                if preset == "timeout":
                    outcome = "injected_timeout"
                    # Intentionally send no response. Separate 60s guard bounds resources.
                    until = time.monotonic() + 60
                    while (
                        request.transport
                        and not request.transport.is_closing()
                        and time.monotonic() < until
                    ):
                        await asyncio.sleep(0.1)
                    if request.transport and not request.transport.is_closing():
                        response_status = 504
                        outcome = "timeout_guard_504"
                        return web.Response(
                            status=504, text="Lab 60-second timeout guard elapsed.\n"
                        )
                    return web.Response(status=204)
                if preset == "delay":
                    await asyncio.sleep(fault["delay_ms"] / 1000)
                    outcome = "delayed"
            headers = forward_headers(request.headers)
            headers.pop("Accept-Encoding", None)
            async with self.session.request(
                request.method,
                url,
                headers=headers,
                data=body,
                allow_redirects=False,
                timeout=ClientTimeout(total=30),
            ) as upstream:
                upstream_status = upstream.status
                response_status = upstream.status
                response_headers = CIMultiDict(forward_headers(upstream.headers))
                response_headers.pop("Content-Encoding", None)
                response_headers["Cache-Control"] = "no-store"
                if "Location" in upstream.headers:
                    if kind in ("whep-post", "whep-lifecycle"):
                        location, alias = self.rewrite_location(
                            upstream.headers["Location"], str(upstream.url)
                        )
                    else:
                        location = self.rewrite_hls_location(
                            upstream.headers["Location"], str(upstream.url)
                        )
                    response_headers["Location"] = location
                response_headers.popall("Link", None)
                turn = getattr(self.faults.runtime, "turn", None)
                if kind == "whep-post" and turn:
                    for link in turn.links():
                        response_headers.add("Link", link)
                if kind in ("whep-post", "whep-lifecycle", "hls-playlist"):
                    data = await upstream.read()
                    if kind == "whep-post" and upstream.status == 201:
                        relay = self.faults.runtime.media_relay
                        data, candidates, _ = change_whep_answer(
                            data,
                            (relay.udp_port, relay.tcp_port),
                            media_host=getattr(self.faults.runtime, "media_host", None),
                        )
                        if hasattr(relay, "register_answer"):
                            self.metadata[alias] = relay.register_answer(data)
                        self.events.emit(
                            "gateway",
                            "media_route_configured",
                            "WebRTC candidates routed through UDP/TCP relay",
                            request_id=request_id,
                            candidates_changed=candidates,
                        )
                    if (
                        kind == "whep-post"
                        and upstream.status == 201
                        and fault
                        and fault["preset"] in ("ice-unreachable", "dtls-fingerprint")
                    ):
                        target = self.faults.runtime.ice_blackhole
                        ports = (
                            (target.udp_port, target.tcp_port)
                            if fault["preset"] == "ice-unreachable"
                            else None
                        )
                        data, candidates, fingerprints = change_whep_answer(
                            data, ports, fault["preset"] == "dtls-fingerprint"
                        )
                        outcome = (
                            "injected_ice_candidates" if ports else "injected_dtls_fingerprint"
                        )
                        self.events.emit(
                            "gateway",
                            "whep_answer_changed",
                            "HTTP 201 retained; transport fault injected",
                            request_id=request_id,
                            fault_id=fault["id"],
                            preset=fault["preset"],
                            candidates_changed=candidates,
                            fingerprints_changed=fingerprints,
                        )
                    if (
                        kind == "whep-lifecycle"
                        and request.method == "PATCH"
                        and upstream.status == 200
                        and data
                    ):
                        relay = self.faults.runtime.media_relay
                        data, _, _ = change_whep_answer(
                            data,
                            (relay.udp_port, relay.tcp_port),
                            media_host=getattr(self.faults.runtime, "media_host", None),
                        )
                        if hasattr(relay, "register_answer"):
                            self.metadata[alias] = relay.register_answer(
                                data, self.metadata.get(alias)
                            )
                    if (
                        kind == "whep-post"
                        and upstream.status == 201
                        and fault
                        and fault["preset"] == "bad-sdp"
                    ):
                        data = b"Lab invalid SDP answer\n"
                        outcome = "injected_bad_sdp"
                    if kind == "hls-playlist" and upstream.status == 200:
                        data = self.rewrite_playlist(data)
                        if fault and fault["preset"] == "stale-playlist":
                            if len(data) <= 65536:
                                self.stale[path] = (data, response_headers.copy())
                                while len(self.stale) > 60:
                                    self.stale.popitem(last=False)
                            outcome = "injected_stale_playlist"
                    bytes_sent = len(data)
                    if request.method == "DELETE" and upstream.status in (200, 204, 404):
                        self.resources.pop(alias, None)
                        self.metadata.pop(alias, None)
                    return web.Response(status=upstream.status, headers=response_headers, body=data)
                if (
                    fault
                    and fault["preset"] == "truncate-media"
                    and upstream.status == 200
                    and request.method == "GET"
                ):
                    data = await upstream.read()
                    if len(data) > 1:
                        kept = max(
                            1, int(len(data) * fault["options"].get("cut_percent", 50) / 100)
                        )
                        response_headers["Content-Length"] = str(len(data))
                        response = web.StreamResponse(status=200, headers=response_headers)
                        response.force_close()
                        streaming_response = response
                        await response.prepare(request)
                        await response.write(data[:kept])
                        bytes_sent, outcome = kept, "injected_truncated_media"
                        if request.transport:
                            request.transport.close()
                        return response
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
            self.events.emit(
                "gateway",
                "request_finished",
                f"{request.method} {kind}: {response_status or outcome}",
                level="WARN" if response_status and response_status >= 400 else "INFO",
                request_id=request_id,
                method=request.method,
                path=path,
                kind=kind,
                direction="client → gateway → edge"
                if upstream_status is not None
                else "client → gateway",
                resource_alias=alias,
                fault_id=fault["id"] if fault else None,
                upstream_status=upstream_status,
                response_status=response_status,
                request_bytes=bytes_received,
                response_bytes=bytes_sent,
                duration_ms=round((time.monotonic() - start) * 1000),
                outcome=outcome,
            )


@web.middleware
async def cors(request, handler):
    exception = None
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
        exception = exc
    if not response.prepared:
        response.headers.update(
            {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Methods": "GET, HEAD, POST, PATCH, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization, If-Match, Range",
                "Access-Control-Expose-Headers": "Location, ETag, Link, Retry-After, Content-Range",
                "Cache-Control": "no-store",
            }
        )
    if exception:
        raise exception
    return response


async def prepare_cors(request, response):
    # Streamed segment responses are already prepared when middleware returns.
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Expose-Headers"] = (
        "Location, ETag, Link, Retry-After, Content-Range"
    )
    response.headers["Cache-Control"] = "no-store"


def gateway_app(gateway):
    app = web.Application(middlewares=[cors], client_max_size=2 * 1024 * 1024)

    async def health(_):
        return web.json_response({"gateway": "running"})

    app.router.add_get("/healthz", health)
    app.router.add_route("*", "/live/{tail:.*}", gateway.handle)
    app.on_response_prepare.append(prepare_cors)
    return app
