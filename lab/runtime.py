import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import signal
import time
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientTimeout

from .ice_blackhole import IceBlackhole
from .media_relay import MediaRelay
from .turn import Turn


ORIGIN_API = "http://127.0.0.1:9997"
EDGE_API = "http://127.0.0.1:10097"
ORIGIN_RTSP = "rtsp://127.0.0.1:18554"


class Runtime:
    """Own only processes started by this instance. Never kill by name or port."""

    def __init__(self, root: Path, host, events, session):
        self.root = root
        self.host = host
        self.media_host = host
        self.hls_variant = "fmp4"
        self.turn = Turn(root, events)
        self.source_url = ""
        self.origin_resume = None
        self.events = events
        self.session = session
        self.ice_blackhole = IceBlackhole(events)
        self.media_relay = MediaRelay(events)
        choice = root / ".runtime" / "protocol"
        self.protocol = choice.read_text().strip() if choice.exists() else "whep"
        if self.protocol not in ("whep", "hls"):
            raise ValueError("Saved playback protocol must be whep or hls.")
        self.processes = {}
        self.watchers = set()
        self.source = "whip"
        self.source_generation = 0
        self.source_configured_at = 0
        self.edge_wanted = True
        self.lock = asyncio.Lock()
        self.snapshot = {
            name: {
                "process": "starting",
                "available": False,
                "paths": [],
                "sessions": [],
                "rx_bps": None,
                "tx_bps": None,
            }
            for name in ("origin", "edge")
        }
        self.baselines = {}
        self.last_states = {}
        self.bridge_retry_at = 0
        self.closing = False
        self.poll_task = None

    def alive(self, name):
        proc = self.processes.get(name)
        return proc is not None and proc.returncode is None

    async def spawn(self, name, command):
        if self.alive(name):
            return
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
            limit=256 * 1024,
        )
        self.processes[name] = proc
        self.events.emit(name, "process_started", "Process started", pid=proc.pid)
        task = asyncio.create_task(self._watch(name, proc))
        self.watchers.add(task)
        task.add_done_callback(self.watchers.discard)

    async def _watch(self, name, proc):
        async for raw in proc.stdout:
            text = raw.decode(errors="replace").strip()
            if not text:
                continue
            try:
                data = json.loads(text)
                message = data.get("message", text)
                level = str(data.get("level", "INFO")).upper()
                level = {"INF": "INFO", "WAR": "WARN", "ERR": "ERROR", "DEB": "DEBUG"}.get(
                    level, level
                )
            except (ValueError, AttributeError):
                message = text
                level = "WARN" if "error" in text.lower() else "INFO"
            self.events.emit(name, "server_log", message, level=level)
        code = await proc.wait()
        self.events.emit(
            name,
            "process_exited",
            "Process stopped",
            exit_code=code,
            level="INFO" if code in (0, -signal.SIGTERM) else "WARN",
        )

    async def stop(self, name):
        proc = self.processes.get(name)
        if proc is None:
            return
        if proc.returncode is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 4)
            except asyncio.TimeoutError:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await proc.wait()
        if self.processes.get(name) is proc:
            self.processes.pop(name, None)

    def config(self, edge):
        # JSON is a YAML subset, avoids a YAML dependency and quoting interpolation.
        base = dict(
            logLevel="info",
            logStructured=True,
            logDestinations=["stdout"],
            api=True,
            metrics=True,
            rtmp=False,
            srt=False,
            moq=False,
            playback=False,
            rtsp=True,
            rtspTransports=["tcp"],
            webrtc=True,
            webrtcIPsFromInterfaces=False,
            webrtcAdditionalHosts=[self.media_host if edge else "127.0.0.1"],
            webrtcICEServers2=self.turn.mediamtx() if edge else [],
            webrtcAllowOrigins=["*"],
            authInternalUsers=[
                dict(
                    user="any",
                    pass_="",
                    ips=[],
                    permissions=[dict(action="publish", path=""), dict(action="read", path="")],
                ),
                dict(
                    user="any",
                    pass_="",
                    ips=["127.0.0.1", "::1"],
                    permissions=[dict(action="api"), dict(action="metrics")],
                ),
            ],
        )
        for user in base["authInternalUsers"]:
            user["pass"] = user.pop("pass_")
        if edge:
            base.update(
                rtspAddress="127.0.0.1:18555",
                apiAddress="127.0.0.1:10097",
                metricsAddress="127.0.0.1:10098",
                webrtcAddress="127.0.0.1:18889",
                webrtcLocalUDPAddress=f"127.0.0.1:{self.media_relay.upstream_udp_port}",
                webrtcLocalTCPAddress=f"127.0.0.1:{self.media_relay.upstream_tcp_port}",
                webrtc=self.protocol == "whep",
                hls=self.protocol == "hls",
                hlsAddress="127.0.0.1:18888",
                hlsVariant=self.hls_variant,
                hlsPartDuration="200ms",
                hlsAlwaysRemux=True,
                hlsSegmentDuration="2s",
                hlsSegmentCount=7,
                paths={
                    path: dict(source=ORIGIN_RTSP + "/" + path, rtspTransport="tcp")
                    for path in (["live"] if self.protocol == "whep" else ["live-hls"])
                },
            )
        else:
            base.update(
                rtspAddress="127.0.0.1:18554",
                apiAddress="127.0.0.1:9997",
                metricsAddress="127.0.0.1:9998",
                webrtcAddress="127.0.0.1:8899",
                webrtcLocalUDPAddress="127.0.0.1:8188",
                webrtcLocalTCPAddress="",
                hls=False,
                paths={"live": {}, "live-hls": {}},
            )
        return base

    async def start_server(self, name):
        if name == "edge":
            if self.protocol == "whep":
                await self.media_relay.start()
            else:
                await self.media_relay.close()
        path = self.root / ".runtime" / f"{name}.json"
        path.touch(mode=0o600, exist_ok=True)
        path.chmod(0o600)
        path.write_text(json.dumps(self.config(name == "edge"), indent=2))
        self.baselines.pop(name, None)
        await self.spawn(name, [str(self.root / ".runtime/bin/mediamtx"), str(path)])
        url = EDGE_API if name == "edge" else ORIGIN_API
        for _ in range(50):
            if not self.alive(name):
                raise RuntimeError(f"{name} exited. Check server logs.")
            try:
                async with self.session.get(
                    url + "/v3/paths/list", timeout=ClientTimeout(total=0.5)
                ) as res:
                    if res.status == 200:
                        return
            except (ClientError, OSError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(0.1)
        raise RuntimeError(f"{name} did not become ready.")

    async def start(self):
        await self.start_server("origin")
        await self.start_server("edge")
        self.poll_task = asyncio.create_task(self.poll())

    async def set_edge(self, running):
        self.edge_wanted = running
        if running:
            await self.start_server("edge")
        else:
            await self.stop("edge")
            self.baselines.pop("edge", None)
            self.snapshot["edge"] = dict(
                process="stopped", available=False, paths=[], sessions=[], rx_bps=None, tx_bps=None
            )

    async def set_origin(self, running):
        if not running:
            self.origin_resume = (self.source, self.source_url) if self.alive("source") else None
            await self.stop("source")
            await self.stop("hls-bridge")
            await self.stop("origin")
            self.snapshot["origin"] = dict(
                process="stopped", available=False, paths=[], sessions=[], rx_bps=None, tx_bps=None
            )
        else:
            await self.start_server("origin")
            resume, self.origin_resume = self.origin_resume, None
            if resume:
                await self.set_source(*resume)
            self.events.emit(
                "controller",
                "origin_restored",
                "Origin running; external WHIP publisher may need to reconnect",
            )

    async def expire_edge_sessions(self):
        items, page = [], 0
        while True:
            sessions = await self.fetch_json(
                EDGE_API + f"/v3/webrtc/sessions/list?page={page}&itemsPerPage=100"
            )
            items.extend(sessions.get("items", []))
            page += 1
            if page >= sessions.get("pageCount", 1):
                break
            if page >= 20:
                raise RuntimeError("Too many sessions for this local lab control.")
        count = 0
        for item in items:
            if item.get("state") != "read" or item.get("path") != "live":
                continue
            async with self.session.post(
                EDGE_API + "/v3/webrtc/sessions/kick/" + item["id"]
            ) as response:
                if response.status not in (200, 404):
                    raise RuntimeError("Edge session could not be closed.")
                count += response.status == 200
        self.events.emit(
            "edge",
            "sessions_expired",
            "Active read sessions closed; new WHEP POST remains available",
            count=count,
        )

    async def set_hls_variant(self, variant):
        if variant not in ("fmp4", "lowLatency"):
            raise ValueError("Choose fmp4 or lowLatency HLS.")
        async with self.lock:
            old = self.hls_variant
            if old == variant:
                return
            self.hls_variant = variant
            if self.protocol == "hls":
                await self.stop("edge")
                try:
                    await self.start_server("edge")
                except Exception:
                    self.hls_variant = old
                    await self.stop("edge")
                    await self.start_server("edge")
                    raise
            self.events.emit(
                "controller", "hls_profile_configured", "HLS profile configured", variant=variant
            )

    async def set_turn(self, data):
        # Changing ICE configuration terminates existing edge sessions.
        async with self.lock:
            old_host = self.media_host
            old = self.turn.configuration()
            try:
                await self.turn.configure(data)
                self.media_host = self.turn.media_host or self.host
                if self.protocol == "whep":
                    await self.stop("edge")
                    await self.start_server("edge")
            except Exception:
                if self.turn.configuration() != old:
                    await self.turn.configure(old)
                self.media_host = old_host
                if self.protocol == "whep" and not self.alive("edge"):
                    await self.stop("edge")
                    await self.start_server("edge")
                raise

    async def set_source(self, kind, url=""):
        if kind not in ("whip", "demo", "hls"):
            raise ValueError("Choose whip, demo or hls.")
        if kind == "hls":
            parsed = urlsplit(url)
            if (
                parsed.scheme not in ("http", "https")
                or not parsed.hostname
                or len(url) > 4000
                or any(ord(c) < 32 for c in url)
            ):
                raise ValueError("Enter a complete http(s) HLS playlist URL.")
        async with self.lock:
            # The origin permits one publisher. OBS must stop before switching input.
            ready = any(
                p["name"] == "live" and p["ready"] for p in self.snapshot["origin"]["paths"]
            )
            if ready and not self.alive("source") and kind != "whip":
                raise ValueError(
                    "Stop the current OBS/external publisher before changing the source."
                )
            self.source_generation += 1
            await self.stop("source")
            await self.stop("hls-bridge")
            self.bridge_retry_at = time.monotonic() + 1
            self.source = kind
            self.source_url = url
            if kind != "whip":
                ffmpeg = shutil.which("ffmpeg")
                if not ffmpeg:
                    raise RuntimeError("FFmpeg is missing. Install: brew install ffmpeg")
                command = [ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostdin"]
                if kind == "demo":
                    command += [
                        "-re",
                        "-f",
                        "lavfi",
                        "-i",
                        "testsrc2=size=640x360:rate=30",
                        "-re",
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=440:sample_rate=48000",
                        "-map",
                        "0:v:0",
                        "-map",
                        "1:a:0",
                    ]
                else:
                    command += ["-re", "-i", url, "-map", "0:v:0", "-map", "0:a:0?"]
                command += [
                    "-c:v",
                    "libx264",
                    "-profile:v",
                    "baseline",
                    "-pix_fmt",
                    "yuv420p",
                    "-preset",
                    "ultrafast",
                    "-tune",
                    "zerolatency",
                    "-g",
                    "60",
                    "-keyint_min",
                    "60",
                    "-sc_threshold",
                    "0",
                    "-c:a",
                    "libopus",
                    "-b:a",
                    "96k",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-rtsp_transport",
                    "tcp",
                    "-f",
                    "rtsp",
                    ORIGIN_RTSP + "/live",
                ]
                await self.spawn("source", command)
            self.events.emit(
                "controller", "source_configured", f"Source mode: {kind}", source_kind=kind
            )
            self.source_configured_at = time.time()
            # Discard status measured before source replacement; do not display old readiness.
            for name in ("origin", "edge"):
                self.snapshot[name] = dict(
                    process="running" if self.alive(name) else "stopped",
                    available=False,
                    paths=[],
                    sessions=[],
                    rx_bps=None,
                    tx_bps=None,
                    last_seen=None,
                )
                self.baselines.pop(name, None)

    async def set_protocol(self, protocol):
        if protocol not in ("whep", "hls"):
            raise ValueError("Choose whep or hls.")
        async with self.lock:
            if protocol == self.protocol:
                return
            previous = self.protocol
            await self.stop("edge")
            await self.stop("hls-bridge")
            self.protocol = protocol
            self.source_generation += 1
            try:
                await self.start_server("edge")
                (self.root / ".runtime" / "protocol").write_text(protocol + "\n")
            except Exception:
                await self.stop("edge")
                self.protocol = previous
                await self.start_server("edge")
                raise
            self.edge_wanted = True
            self.snapshot["edge"] = dict(
                process="running",
                available=False,
                paths=[],
                sessions=[],
                rx_bps=None,
                tx_bps=None,
                last_seen=None,
            )
            self.events.emit(
                "controller",
                "protocol_configured",
                f"Playback: {protocol.upper()}",
                protocol=protocol,
            )

    async def _bridge(self):
        if (
            self.protocol != "hls"
            or self.alive("hls-bridge")
            or time.monotonic() < self.bridge_retry_at
        ):
            return
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return
        self.bridge_retry_at = time.monotonic() + 5
        await self.spawn(
            "hls-bridge",
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "warning",
                "-nostdin",
                "-rtsp_transport",
                "tcp",
                "-i",
                ORIGIN_RTSP + "/live",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-rtsp_transport",
                "tcp",
                "-f",
                "rtsp",
                ORIGIN_RTSP + "/live-hls",
            ],
        )

    async def fetch_json(self, url):
        async with self.session.get(url, timeout=ClientTimeout(total=1.5)) as res:
            res.raise_for_status()
            return await res.json()

    async def inspect(self, name, api, metrics_port):
        result = dict(
            process="running" if self.alive(name) else "stopped",
            available=False,
            paths=[],
            sessions=[],
            rx_bps=None,
            tx_bps=None,
            hls_http_bps=None,
            last_seen=None,
            pid=self.processes[name].pid if self.alive(name) else None,
        )
        if not self.alive(name):
            return result
        try:
            paths = await self.fetch_json(api + "/v3/paths/list")
            sessions = (
                await self.fetch_json(api + "/v3/webrtc/sessions/list")
                if name == "origin" or self.protocol == "whep"
                else {"items": []}
            )
            result.update(
                available=True,
                last_seen=time.time(),
                paths=[
                    dict(
                        name=p["name"],
                        ready=p.get("ready", False),
                        codecs=p.get("tracks", []),
                        readers=len(p.get("readers") or []),
                    )
                    for p in paths.get("items", [])
                ],
                sessions=[
                    {
                        k: s.get(k)
                        for k in (
                            "id",
                            "path",
                            "state",
                            "peerConnectionEstablished",
                            "bytesReceived",
                            "bytesSent",
                            "inboundBytes",
                            "outboundBytes",
                        )
                        if k in s
                    }
                    for s in sessions.get("items", [])
                ],
            )
            async with self.session.get(
                f"http://127.0.0.1:{metrics_port}/metrics", timeout=ClientTimeout(total=1.5)
            ) as res:
                res.raise_for_status()
                text = await res.text()
            totals = {"rx": 0.0, "tx": 0.0, "hls_http": 0.0}
            found = set()
            for line in text.splitlines():
                match = re.match(r"paths_(inbound|outbound)_bytes\{[^}]*\}\s+([\d.eE+\-]+)", line)
                if match:
                    direction = "rx" if match[1] == "inbound" else "tx"
                    totals[direction] += float(match[2])
                    found.add(direction)
                hls_match = re.match(
                    r'hls_muxers_outbound_bytes\{name="live-hls"\}\s+([\d.eE+\-]+)', line
                )
                if hls_match:
                    totals["hls_http"] = float(hls_match[1])
                    found.add("hls_http")
            now = time.monotonic()
            previous = self.baselines.get(name)
            if previous:
                elapsed = now - previous[0]
                for key in found:
                    delta = totals[key] - previous[1].get(key, totals[key])
                    if delta >= 0 and elapsed > 0:
                        result[key + "_bps"] = round(delta * 8 / elapsed)
            self.baselines[name] = (now, totals)
            result["bytes"] = totals if found else None
        except (ClientError, OSError, asyncio.TimeoutError, ValueError) as exc:
            self.baselines.pop(name, None)
            result["error"] = type(exc).__name__
        return result

    async def poll(self):
        while not self.closing:
            try:
                for name, api, port in (("origin", ORIGIN_API, 9998), ("edge", EDGE_API, 10098)):
                    generation = self.source_generation
                    process = self.processes.get(name)
                    result = await self.inspect(name, api, port)
                    if generation != self.source_generation or process is not self.processes.get(
                        name
                    ):
                        continue
                    self.snapshot[name] = result
                    state = (
                        result["process"],
                        result["available"],
                        tuple((p["name"], p["ready"], tuple(p["codecs"])) for p in result["paths"]),
                        tuple(s["id"] for s in result["sessions"]),
                    )
                    if state != self.last_states.get(name):
                        self.last_states[name] = state
                        self.events.emit(
                            name,
                            "status_changed",
                            "Process, stream or session state changed",
                            **result,
                        )
                    if result.get("bytes") is not None:
                        self.events.emit(
                            name,
                            "traffic_sample",
                            "Server traffic counters sampled",
                            bytes=result["bytes"],
                            rx_bps=result["rx_bps"],
                            tx_bps=result["tx_bps"],
                            hls_http_bps=result["hls_http_bps"],
                        )
                async with self.lock:
                    ready = any(
                        p["name"] == "live" and p["ready"] for p in self.snapshot["origin"]["paths"]
                    )
                    if ready and self.protocol == "hls":
                        await self._bridge()
                    elif self.alive("hls-bridge"):
                        await self.stop("hls-bridge")
                if self.protocol == "whep":
                    self.events.emit(
                        "media-relay",
                        "traffic_sample",
                        "Forwarded and discarded traffic totals",
                        **self.media_relay.status(),
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.events.emit("controller", "poll_failed", type(exc).__name__, level="WARN")
            await asyncio.sleep(2)

    async def close(self):
        self.closing = True
        if self.poll_task:
            self.poll_task.cancel()
            await asyncio.gather(self.poll_task, return_exceptions=True)
        await self.turn.close()
        await self.ice_blackhole.close()
        await self.media_relay.close()
        for name in ("source", "hls-bridge", "edge", "origin"):
            await self.stop(name)
        await asyncio.gather(*self.watchers, return_exceptions=True)
