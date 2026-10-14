"""Exercise a running lab with its generated source. Replaces a lab-owned source.

Does not replace an active OBS/external publisher. Leaves the test pattern running.
"""

import argparse
import asyncio
import time
from urllib.parse import urljoin

from aiohttp import ClientSession, ClientTimeout


async def main(base):
    async with ClientSession(timeout=ClientTimeout(total=20)) as client:

        async def command(path, data):
            async with client.post(base + path, json=data) as response:
                result = await response.json()
                if response.status != 200:
                    raise RuntimeError(result)
                return result

        async def status():
            async with client.get(base + "/api/status") as response:
                response.raise_for_status()
                return await response.json()

        async def wait_for(predicate, timeout=30):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                result = await status()
                if predicate(result):
                    return result
                await asyncio.sleep(0.5)
            raise AssertionError("Timed out waiting for lab state")

        def ready(result):
            return (
                all(p["ready"] for p in result["components"]["edge"]["paths"])
                and len(result["components"]["edge"]["paths"]) == 1
            )

        original_protocol = (await status())["protocol"]
        await command("/api/restore", {})
        try:
            await command("/api/source", {"kind": "demo"})
            await command("/api/protocol", {"protocol": "hls"})
            current = await wait_for(
                lambda state: ready(state) and state["components"]["origin"]["rx_bps"] is not None
            )
            assert current["components"]["origin"]["rx_bps"] is not None
            print("PASS: generated source, origin, edge, and HLS audio bridge ready")
            hls = current["urls"]["hls"]
            # Stream readiness precedes the first complete HLS segment. Wait for the muxer.
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                async with client.get(hls) as response:
                    if response.status == 200:
                        break
                await asyncio.sleep(0.5)
            async with client.get(hls) as response:
                assert response.status == 200
                master = await response.text()
                master_base = str(response.url)
            variant = next(
                line for line in master.splitlines() if line and not line.startswith("#")
            )
            async with client.get(urljoin(master_base, variant)) as response:
                assert response.status == 200
                playlist = await response.text()
                playlist_base = str(response.url)
            segment = next(
                line for line in playlist.splitlines() if line and not line.startswith("#")
            )
            segment_url = urljoin(playlist_base, segment)
            async with client.get(segment_url) as response:
                assert response.status == 200
                assert len(await response.read()) > 100
            print("PASS: master, variant, and media segment traverse gateway")
            await command("/api/fault", dict(preset="http503", scope="hls-playlist", duration=10))
            async with client.get(hls) as response:
                assert response.status == 503 and response.headers["Retry-After"] == "5"
            async with client.get(segment_url) as response:
                assert response.status == 200
            print("PASS: playlist 503 leaves segment requests healthy")
            await command("/api/fault", dict(preset="http502", scope="hls-media", duration=10))
            async with client.get(segment_url) as response:
                assert response.status == 502
            async with client.get(hls) as response:
                assert response.status == 200
            print("PASS: segment 502 leaves playlist requests healthy")
            await command(
                "/api/fault", dict(preset="delay", scope="hls-playlist", duration=10, delay_ms=300)
            )
            started = time.monotonic()
            async with client.get(hls) as response:
                assert response.status == 200
            assert time.monotonic() - started >= 0.28
            print("PASS: delayed HTTP response")
            await command("/api/fault", dict(preset="timeout", scope="hls-playlist", duration=10))
            try:
                async with client.get(hls, timeout=ClientTimeout(total=0.25)):
                    raise AssertionError("Timeout fault answered too early")
            except asyncio.TimeoutError:
                pass
            print("PASS: timeout fault sends no response before client deadline")
            before = await status()
            origin_pid = before["components"]["origin"]["pid"]
            old_edge_pid = before["components"]["edge"]["pid"]
            await command("/api/fault", dict(preset="edge-down", scope="playback", duration=2))
            stopped = await status()
            assert stopped["components"]["edge"]["process"] == "stopped"
            async with client.get(hls) as response:
                assert response.status == 502
            restored = await wait_for(lambda state: state["fault"] is None and ready(state))
            assert restored["components"]["origin"]["pid"] == origin_pid
            assert restored["components"]["edge"]["pid"] != old_edge_pid
            print("PASS: edge auto-restores without restarting origin")
        finally:
            await command("/api/restore", {})
            await command("/api/protocol", {"protocol": original_protocol})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--controller", default="http://127.0.0.1:9091")
    args = parser.parse_args()
    asyncio.run(main(args.controller.rstrip("/")))
