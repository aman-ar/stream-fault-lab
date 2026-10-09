# Validation record

Checked on 2026-10-09, on an Apple Silicon Mac with Python 3.14.7, MediaMTX 1.21.2, FFmpeg 9.0.2, and temporary headless Google Chrome sessions. This record covers the local lab, not a production CDN or the user's production player.

## Automated contracts

`.venv/bin/python -m unittest discover -s tests -v`: **15 tests passed**.

Verified distinct WHEP POST resources; correct PATCH/DELETE routing including private resource queries and ETags; 503 with Retry-After without upstream forwarding; cleanup requests unaffected by POST faults; separate HLS playlist/media scopes; playlist and HLS session redirect rewriting; streamed response CORS; response delay and expiry; no response before a client timeout; confirmed edge restoration with failure state retained; invalid fault input; rejection of untrusted upstream Locations; log URL redaction; and local control request validation.

## Real media checks

| Check | Observed result |
| --- | --- |
| Generated H.264/Opus stream | Origin and edge ready; increasing server RX counters |
| HLS audio bridge | H.264 video copied, AAC audio present on separate path |
| WHEP reference playback | Chrome established a PeerConnection and rendered more than 30 video frames |
| HTTP 503 on new WHEP POST | Existing playback increased from 34 to 79 frames during the fault; new POST returned 503 |
| Fresh connection after clearing 503 | New resource alias and rendered frames |
| Edge outage and timed restoration | Edge process stopped and restarted with a new PID; origin PID stayed the same; fresh WHEP playback rendered frames |
| HLS reference playback | Chrome/hls.js rendered more than 30 frames through gateway playlists and segments |
| Native HLS client | FFmpeg decoded video/audio through the gateway for 3 seconds without error |
| HTTP HLS input | A temporary generated HLS source was pulled, converted to H.264/Opus, bridged to H.264/AAC, and decoded through the output gateway |
| Real WHIP publishing | Chrome published H.264/Opus using WHIP to the origin; downstream WHEP rendered more than 30 frames |
| Server cleanup | PATCH/DELETE contract tests passed. Actual browser DELETE requests reached the edge; observed responses were 404 after the PeerConnection closed. The edge session inventory returned to empty |
| Packaged smoke script | Passed real playlist/segment routing, isolated 503/502 scopes, delay, client timeout, edge restart, and preservation of origin PID |
| UI | Active source initialized correctly; HTTP scope hidden for an edge outage; log search/filter/details worked; 360px viewport had no horizontal overflow; no uncaught page errors |
| Process lifecycle | Ctrl+C stopped lab-owned origin, edge, and FFmpeg processes; immediate restart succeeded |

HLS stream readiness precedes the first completed segments. Startup checks allow time for the muxer to warm up. A newly started source or edge can briefly return unavailable HLS playlists. The UI explains this; it does not conceal these responses.

## Remaining acceptance checks

- Real OBS UI publishing; the protocol was exercised with a Chrome WHIP publisher, not OBS itself.
- The user's Android/web players and their retry, fallback, cleanup, and rendering policies.
- Physical LAN devices, local firewall/VPN behavior, and HTTPS/cleartext policy.
- Intel Mac execution. Its official binary digest is pinned, but it was not executed here.
- Long-running sessions, load, and fault scenarios outside the five implemented presets.

Development traces and temporary test sources are excluded from the source package. Run the included smoke check or use Export trace to capture your own evidence.
