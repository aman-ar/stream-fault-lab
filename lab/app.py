import argparse
import asyncio
import errno
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import socket
import sys
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, web

from .events import Events
from .gateway import Faults, Gateway, gateway_app
from .runtime import Runtime

ROOT = Path(__file__).resolve().parents[1]


def load_host():
    host = os.environ.get("PUBLIC_HOST")
    env = ROOT / ".env"
    if host is None and env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("PUBLIC_HOST="):
                host = line.split("=", 1)[1].strip().strip('"\'')
    return host or "127.0.0.1"


def validate_host(host):
    # IPv4 / DNS names only, keep URL and ICE candidate formatting unambiguous.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.\-]{0,252}", host):
        raise ValueError("PUBLIC_HOST must be a hostname or IPv4 address, without scheme or port.")
    return host


def check_ports(ui_port):
    ports = ((socket.SOCK_STREAM, p) for p in (ui_port, 8080, 18554, 18555, 8899, 18889, 18888,
                                              9997, 9998, 10097, 10098, 8186))
    sockets = []
    try:
        for kind, port in list(ports) + [(socket.SOCK_DGRAM, 8188), (socket.SOCK_DGRAM, 8189)]:
            sock = socket.socket(socket.AF_INET, kind)
            sockets.append(sock)
            try:
                if kind == socket.SOCK_STREAM:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("0.0.0.0", port))
                if kind == socket.SOCK_STREAM:
                    sock.listen(1)
            except OSError as exc:
                if exc.errno == errno.EADDRINUSE:
                    raise RuntimeError(f"Port {port} is occupied. Stop the conflicting application and retry.") from exc
                raise RuntimeError(f"Cannot bind port {port}: {exc.strerror}") from exc
    finally:
        for sock in sockets:
            sock.close()


@web.middleware
async def local_admin(request, handler):
    host = urlsplit("http://" + request.host).hostname
    if host not in ("localhost", "127.0.0.1", "::1"):
        raise web.HTTPForbidden(text="Open the controller using localhost or 127.0.0.1.")
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin and origin != "http://" + request.host:
            raise web.HTTPForbidden(text="Control requests must come from the local controller.")
        if request.content_type != "application/json":
            raise web.HTTPUnsupportedMediaType(text="Control requests require application/json.")
    try:
        return await handler(request)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except RuntimeError as exc:
        return web.json_response({"error": str(exc)}, status=503)


def admin_app(runtime, faults, events, host):
    app = web.Application(middlewares=[local_admin], client_max_size=16 * 1024)

    async def index(_):
        return web.FileResponse(ROOT / "web/index.html", headers={"Cache-Control": "no-store"})

    async def status(_):
        return web.json_response(dict(
            run_id=events.run_id, public_host=host, source_kind=runtime.source,
            source_configured_at=runtime.source_configured_at,
            components=runtime.snapshot,
            source_process="running" if runtime.alive("source") else "stopped",
            hls_bridge="running" if runtime.alive("hls-bridge") else "stopped",
            fault=faults.status(),
            urls=dict(whip="http://127.0.0.1:8899/live/whip",
                      whep=f"http://{host}:8080/live/whep",
                      hls=f"http://{host}:8080/live/index.m3u8")),
            headers={"Cache-Control": "no-store"})

    async def source(request):
        data = await request.json()
        await runtime.set_source(data["kind"], data.get("url", ""))
        return web.json_response({"source_kind": runtime.source})

    async def fault(request):
        data = await request.json()
        result = await faults.apply(data["preset"], data.get("scope", "whep-post"),
                                    data.get("duration", 15), data.get("delay_ms", 3000))
        return web.json_response({"fault": result})

    async def restore(_):
        await faults.restore()
        return web.json_response({"fault": None, "edge": "running"})

    async def history(_):
        return web.json_response(list(events.records), headers={"Cache-Control": "no-store"})

    async def stream(request):
        after = int(request.headers.get("Last-Event-ID", request.query.get("after", "0")))
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream",
                                               "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
        await response.prepare(request)
        queue = events.subscribe()
        last = after

        async def send(record):
            nonlocal last
            if record["sequence"] <= last:
                return
            last = record["sequence"]
            data = f'id: {last}\ndata: {json.dumps(record)}\n\n'
            await response.write(data.encode())

        try:
            replay = list(events.records)
            for record in replay if after else replay[-200:]:
                await send(record)
            while True:
                try:
                    record = await asyncio.wait_for(queue.get(), 10)
                    # Slow consumers can miss records in the bounded queue.
                    if record["sequence"] > last + 1:
                        await response.write(b'event: gap\ndata: {"message":"Some live records were skipped. Export the trace for the complete retained log."}\n\n')
                    await send(record)
                except asyncio.TimeoutError:
                    await response.write(b": keepalive\n\n")
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            events.subscribers.discard(queue)
        return response

    async def trace(request):
        response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson",
                    "Content-Disposition": f'attachment; filename="{events.run_id}.jsonl"'})
        await response.prepare(request)
        # Each run is rotated; concatenate its retained parts in sequence.
        for path in sorted(events.directory.glob(events.run_id + "-*.jsonl")):
            with path.open("rb") as file:
                # Snapshot file length: don't chase records appended during export.
                remaining = path.stat().st_size
                while remaining:
                    chunk = file.read(min(remaining, 64 * 1024))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    await response.write(chunk)
        await response.write_eof()
        return response

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_post("/api/source", source)
    app.router.add_post("/api/fault", fault)
    app.router.add_post("/api/restore", restore)
    app.router.add_get("/api/history", history)
    app.router.add_get("/api/events", stream)
    app.router.add_get("/api/trace", trace)
    app.router.add_static("/assets/", ROOT / "web", show_index=False)
    return app


async def run(host, ui_port):
    check_ports(ui_port)
    events = Events(ROOT / ".runtime/traces")
    session = ClientSession(timeout=ClientTimeout(total=30), auto_decompress=True, cookie_jar=DummyCookieJar())
    runtime = Runtime(ROOT, host, events, session)
    faults = Faults(runtime, events)
    runners = []
    done = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, done.set)
    try:
        await runtime.start()
        for app, address, port in ((admin_app(runtime, faults, events, host), "127.0.0.1", ui_port),
                                   (gateway_app(Gateway(session, faults, events)), "0.0.0.0", 8080)):
            runner = web.AppRunner(app, access_log=None, handler_cancellation=True, shutdown_timeout=1)
            runners.append(runner)
            await runner.setup()
            await web.TCPSite(runner, address, port).start()
        events.emit("controller", "lab_started", "Lab ready", public_host=host)
        print(f"\nStream Fault Lab is running: http://localhost:{ui_port}\nPress Ctrl+C to stop all lab processes.\n", flush=True)
        await done.wait()
    finally:
        await faults.close()
        for runner in reversed(runners):
            await runner.cleanup()
        await runtime.close()
        await session.close()
        events.emit("controller", "lab_stopped", "All owned processes stopped")
        events.close()


def main():
    parser = argparse.ArgumentParser(description="Local WHEP/HLS delivery fault lab")
    parser.add_argument("--public-host", default=load_host(), help="Mac IP reachable by the player")
    parser.add_argument("--ui-port", type=int, default=9091, help="Local controller port (default: 9091)")
    args = parser.parse_args()
    runtime_dir = ROOT / ".runtime"
    runtime_dir.mkdir(exist_ok=True)
    # Prevent two instances from owning the same processes/config/traces.
    with (runtime_dir / "lab.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("This checkout already has a running lab.")
        try:
            if not 1024 <= args.ui_port <= 65535:
                raise ValueError("UI port must be 1024–65535.")
            asyncio.run(run(validate_host(args.public_host), args.ui_port))
        except (ValueError, RuntimeError, OSError) as exc:
            print(f"Cannot start lab: {exc}", file=sys.stderr)
            raise SystemExit(1)


if __name__ == "__main__":
    main()
