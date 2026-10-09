# Stream Fault Lab

A small local lab for testing how a player responds to HTTP delivery failures and a real edge outage. Runs directly on macOS. No Docker, Node.js, database, or frontend build is required.

## Start on your Mac

You need Python 3.11+ and FFmpeg. If needed, install them once with Homebrew:

```sh
brew install python ffmpeg
```

From a clone or an extracted copy of this project:

```sh
./start.sh
```

Open **http://localhost:9091**. Select **Generated test stream**, then **Use this source**. Wait until the origin and edge show `live: ready`. Click **Play** in the reference player, or copy a playback URL into your own player.

The first start installs Python dependencies into `.venv/` and downloads MediaMTX **v1.21.2** into `.runtime/bin/`. The download is verified against pinned SHA-256 digests from the official release. Later starts reuse them. The Chrome HLS preview uses a vendored, integrity-verified hls.js **1.6.13** file, so the UI needs no internet connection. Your HLS source may need internet access.

Press **Ctrl+C** in the terminal to stop the app and every media process it started. It does not stop unrelated processes or change macOS firewall rules. Start it again with the same command. If the controller port is occupied, use `./start.sh --ui-port 9092`. Other occupied ports are reported before media processes start.

## Stack and data flow

| Module | Implementation | Purpose |
| --- | --- | --- |
| Controller and HTTP gateway | Python + aiohttp 3.14.4 | Local UI, fault controls, HTTP forwarding, traces, live SSE logs |
| Origin | Native MediaMTX 1.21.2 process | Receives WHIP or an FFmpeg publisher |
| Edge | Separate native MediaMTX 1.21.2 process | Pulls from the origin over RTSP/TCP; serves WHEP and HLS |
| Source and audio bridge | Native FFmpeg | Test stream, HLS input conversion, AAC audio for HLS |
| UI and reference player | HTML, CSS, JavaScript; native WebRTC and hls.js | Controller, log viewer, optional pipeline check |

```text
OBS --WHIP------------------------> origin /live (H.264 + Opus)
HLS URL --FFmpeg conversion-------> origin /live (H.264 + Opus)
Test pattern --FFmpeg------------> origin /live (H.264 + Opus)
                                       |
                          RTSP/TCP     +--> edge /live --WHEP HTTP--> gateway --> player
                                       |       +------ICE/DTLS/SRTP-----------> player
                                       |
                                  FFmpeg audio bridge
                                  copy H.264, audio to AAC
                                       |
                                  origin /live-hls
                                       |
                                  RTSP/TCP
                                       |
                                  edge /live-hls --HLS HTTP--> gateway /live/... --> player
```

The edge can stop while the origin and publisher stay up. WebRTC media bypasses the HTTP gateway. HLS playlists, initialization files, and segments pass through it. WHEP session `Location` values are rewritten to opaque gateway resource aliases; PATCH and DELETE reach the original edge resource. Independent POST requests create independent resources. Edge restart clears its in-memory sessions.

## Input sources

### OBS / WHIP

1. Select **OBS / WHIP** and **Use this source**. This stops a lab-owned test/HLS source; it does not start OBS.
2. In OBS → Settings → Stream, select **WHIP** and use `http://127.0.0.1:8899/live/whip`.
3. Use **H.264 video** and **Opus audio**. Use a 2-second keyframe interval and no B-frames for WebRTC. Start streaming.

WHIP ingest is bound to this Mac's loopback address. OBS should run on this Mac. Stop OBS before switching to generated or HLS input; the origin supports one publisher on `/live`. Other WHIP publishing applications can use the same URL and compatible codecs.

The WHIP stream passes through to WHEP. FFmpeg copies its video and converts the audio to AAC on a separate HLS path for browser compatibility. This is not a comparison of identical audio codecs across protocols.

### Pull an HLS URL

Select **Pull an HLS URL**, enter a complete HTTP(S) playlist URL, and select **Use this source**. FFmpeg reads it and encodes H.264/Opus for the origin. The audio bridge produces H.264/AAC for HLS. This costs CPU and adds delay. An HLS URL is pulled; HLS is not a push publishing endpoint. DRM streams are not supported. A stopped or expired input appears in the logs; select Use this source again to retry. Source URLs are kept only in the running FFmpeg command, not in configuration, browser storage, or traces.

### Generated test stream

A moving 640×360 pattern at 30fps and a 440Hz audio tone. This is the quickest way to check the lab before connecting OBS. The reference player starts muted.

## Playback URLs and phones

Default playback URLs for a player on this Mac:

```text
WHEP: http://127.0.0.1:8080/live/whep
HLS:  http://127.0.0.1:8080/live/index.m3u8
```

For a phone or another LAN device:

```sh
cp .env.example .env
# Set PUBLIC_HOST to this Mac's reachable LAN IPv4 address.
./start.sh
# Or: ./start.sh --public-host 192.168.1.50
```

The player must reach TCP 8080 for HTTP, UDP 8189 for WebRTC, and optionally TCP 8186 for ICE-TCP. The Mac and device must share a reachable network; VPNs and client-isolated Wi-Fi can prevent this. The output URLs and ICE candidates use PUBLIC_HOST. Keep every player reconnect pointed at the lab URL.

The controller stays on localhost. The gateway and edge media ports accept LAN clients. Publishing and server control endpoints stay on loopback. This is an unauthenticated local test lab, not a public deployment. The first version serves HTTP; HTTPS webpages and apps that require TLS need a separate TLS setup before they can use it. Safari or Chrome can open the local controller; the reference player does not reconnect automatically and does not represent your production player's recovery policy.

## Five faults

| Preset | Behavior |
| --- | --- |
| HTTP 503 | Gateway returns 503 with `Retry-After: 5`; request does not reach the edge |
| HTTP 502 | Gateway returns 502; request does not reach the edge |
| Slow HTTP response | Gateway waits 100–30000ms before forwarding |
| HTTP request timeout | Gateway sends no response; the client's timeout should fire. A 60-second guard eventually returns 504 if the client still waits |
| Edge outage | App terminates only its edge process, then starts a new one on restoration |

HTTP scope can be **new WHEP POSTs**, **HLS playlists**, **HLS media files**, or **WHEP POST + all HLS requests**. WHEP PATCH/DELETE remain available for cleanup. Existing WebRTC media is unaffected by HTTP faults. Use **Edge outage** to interrupt an active WebRTC session.

Faults auto-restore after the selected duration (1–600 seconds). **Restore healthy** restores immediately. Only one fault is active; applying a new fault restores the previous one first. Restoration confirms that the edge process and control API are available, not that your player resumed rendering. A delayed request keeps its selected delay. An already matched timeout request keeps waiting, even after restoration; new requests can succeed.

This release does not implement packet loss, a media-only freeze, macOS firewall changes, CDN caching, DNS/TLS failures, multi-edge failover, vendor-specific signaling fallback, or load testing.

## Server logcat

The controller collects real structured MediaMTX logs, FFmpeg process logs, gateway request events, status snapshots, and fault lifecycle events. It polls private server APIs and counters every two seconds.

- Process state, stream readiness, codecs, readers, and edge WebRTC session inventory.
- Server RX/TX rates derived from path byte counter deltas, plus a separate edge HLS HTTP send rate; first/stale/reset measurements show unavailable, not a fabricated rate.
- HTTP method, safe path, request ID, resource alias, body sizes, duration, upstream status, player-facing status, and fault ID.
- Requested/applied/expired/restored faults and process exits.

Expand a log row to see fields; filter by component/level or search an ID. Gateway resource aliases and MediaMTX session IDs are different identifiers. The private MediaMTX session API and its own logs show server session IDs. Traffic counters measure server activity; they do not prove player receipt, decoded audio, rendered video, or recovery.

Logs use collector UTC timestamps and a per-run sequence. Trace bodies omit SDP, cookies, authorization, URL queries, and raw media. Process log URLs are redacted. The app keeps 2000 recent events in memory; the browser keeps 800 and displays the last 250 matching entries. SSE queues are bounded; a slow viewer is warned about a gap. Export the retained trace for a fuller record.

**Export trace** downloads JSONL for the current app run. Files also live in `.runtime/traces/`; each rotates at approximately 5MB, with at most 20 retained files across runs. Restarting the app starts a new run. Traces are not committed to Git.

## Verification

Gateway contract tests use real local HTTP servers and fake media endpoints. No OBS is needed:

```sh
./start.sh                 # first-time dependency installation, then Ctrl+C
.venv/bin/python -m unittest discover -s tests -v
```

With the lab running, this smoke check starts the generated source and exercises real HLS delivery and all five faults. It refuses to replace an active external publisher. It leaves the test pattern running and clears the fault:

```sh
.venv/bin/python scripts/smoke.py
```

For a quick decode check, while the generated source is running:

```sh
ffmpeg -hide_banner -loglevel error \
  -i http://127.0.0.1:8080/live/index.m3u8 -t 3 -f null -
```

For your player: start healthy playback, apply a fault, record failure/retry events, restore, and verify new changing frames. For WHEP, inspect fresh POSTs, PATCH/DELETE requests, and server session inventory. Closing the old PeerConnection alone does not prove DELETE was sent. Measure your client's retry timing and rendered-frame recovery separately from server readiness.

## Files and ports

`lab/app.py` owns startup and the local control interface. `lab/runtime.py` owns media processes, configuration and polling. `lab/gateway.py` owns HTTP forwarding and timed faults. `lab/events.py` owns redacted event collection. `web/` is the UI. `scripts/bootstrap.py` verifies/downloads MediaMTX.

| Listener | Bind / port |
| --- | --- |
| Controller | 127.0.0.1:9091 (configurable) |
| Playback gateway | 0.0.0.0:8080 |
| Origin WHIP HTTP / ICE | 127.0.0.1:8899 / 8188 UDP |
| Edge ICE | 0.0.0.0:8189 UDP / 8186 TCP |
| Origin / edge RTSP | 127.0.0.1:18554 / 18555 |
| Edge WHEP / HLS upstream | 127.0.0.1:18889 / 18888 |
| Origin control / metrics | 127.0.0.1:9997 / 9998 |
| Edge control / metrics | 127.0.0.1:10097 / 10098 |

Native Apple Silicon was exercised during development; the bootstrap also pins an official Intel Mac binary. Intel and physical-device playback still need their own acceptance checks. See [VALIDATION.md](VALIDATION.md) for the recorded checks.

## Dependency references

- [MediaMTX release v1.21.2](https://github.com/bluenviron/mediamtx/releases/tag/v1.21.2), [configuration](https://mediamtx.org/docs/references/configuration-file), [metrics](https://mediamtx.org/docs/features/metrics), and [OBS publishing](https://mediamtx.org/docs/publish/obs-studio).
- [aiohttp](https://docs.aiohttp.org/en/stable/) and [FFmpeg](https://ffmpeg.org/documentation.html).
- [hls.js 1.6.13](https://github.com/video-dev/hls.js/tree/v1.6.13). Vendored under its BSD-2-Clause license, included as `web/hls.LICENSE`. The npm tarball was checked against the package's published SHA-512 integrity value. MediaMTX's MIT license is downloaded with its binary; FFmpeg keeps the license of your installation.
