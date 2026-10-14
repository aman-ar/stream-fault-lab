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
- Long-running sessions, load, and representative infrastructure failures beyond the implemented fixtures.

Development traces and temporary test sources are excluded from the source package. Run the included smoke check or use Export trace to capture your own evidence.

## Exclusive playback output (2026-10-09)

The controller now selects one output protocol, saved in `.runtime/protocol` (WHEP by default). Changing the output clears any active fault and restarts only the edge, while preserving the source and origin. Inactive playback endpoints return HTTP 409.

- 22 gateway, control, and protocol tests passed, including rejection of inactive output requests, fault-scope validation, saved choice, and rollback after a failed edge start.
- Isolated native MediaMTX/FFmpeg checks passed: WHEP had no HLS listener or audio bridge; HLS had no edge WHEP listener and served video that FFmpeg decoded through the gateway; switching back removed HLS serving and conversion. Origin and source PIDs stayed unchanged across both switches.
- JavaScript syntax check passed.
- Installed Chrome UI check passed: selector changes the real edge protocol, clears an active fault, shows only the selected URL and matching fault scopes, synchronizes the reference player, retains the choice on reload, and has no page errors or horizontal overflow at 360px. Final running mode: WHEP, HLS bridge disabled, no active fault.

## ICE and DTLS presets (2026-10-09)

- 26 contract/control/protocol/socket tests passed. The ICE target discards UDP and TCP traffic without replying, releases active sockets promptly on restore, and can be started again on the same ports. Both mutations preserve HTTP 201 and lifecycle Location; payloads and fingerprints are absent from traces. HLS rejects these presets.
- Isolated native MediaMTX and generated FFmpeg source verified with installed Chrome: healthy WHEP rendered more than 30 frames; UDP-only and TCP-only connections using changed ICE candidates received HTTP 201 but never connected or rendered frames during observation. Both discard endpoints received traffic. Origin/edge PIDs stayed unchanged and existing video continued rendering.
- The DTLS fingerprint mutation retained HTTP 201, reached connected ICE, then produced a failed browser DTLS transport and no frames. Fresh healthy WHEP connections rendered more than 30 frames after restoring either preset.
- The UI exposed both presets in WHEP mode, hid their fixed request scope, disabled them in HLS mode, and had no browser page errors.
- These are lab/browser checks. Android ICE/DTLS error classification, automatic recovery, and rendered-frame timings remain pending user tests. The ICE preset tests new connections; it does not interrupt an established media path. Previously changed answers require a fresh healthy WHEP attempt after restoration.

## Mid-stream WebRTC transport outage (2026-10-09)

- 31 contract/control/protocol/socket tests passed, including healthy candidate port rewriting, bidirectional UDP/TCP forwarding and discarding, split/coalesced TCP frame handling, restoration without replaying discarded frames, prompt shutdown, rebind, HTTP lifecycle availability during media outages, timed restoration, and rejection of the preset in HLS mode.
- Native MediaMTX and generated FFmpeg video verified with installed Chrome over both UDP and TCP: existing connections rendered more than 30 frames, a 12-second `media-outage` stopped rendered-frame progress, and both resumed rendering more than 30 additional frames after timed restoration without manually replacing the PeerConnections. A new WHEP attempt during the outage still returned 201. Both transport discard counters increased. Origin and edge PIDs remained unchanged. A fresh connection after restoration also rendered video.
- Regression check through the new relay retained ICE unreachable and DTLS fingerprint behaviors, including connected ICE / failed DTLS for the fingerprint case and healthy fresh video after restoration. The protocol UI disabled WHEP transport presets for HLS, closed the relay in HLS mode, and restarted it on switching back without browser errors.
- This is a framed-traffic discard relay. TCP sockets remain open; it does not reproduce TCP retransmission/RST/FIN behavior. All traffic, including ICE checks and encrypted media, is discarded. A selective RTP-only stall preserving ICE remains outside this preset. Android automatic recovery and error classification for this new mid-stream fault remain to be observed.

## Expanded controls (2026-10-09)

**40 automated tests passed** against real local HTTP/socket fixtures. New contracts cover negotiated audio/video packet classification and STUN lookup, selective-direction stalls preserving control traffic, bounded bandwidth queues, queued-packet discard on restoration, HTTP429/Retry-After, lifecycle-only HTTP410, a 307 retaining POST/body, stale successful HLS snapshots, actual incomplete Content-Length responses, repeated TURN Link fields/CORS and credential exclusion, and finite-sequence cancellation.

Isolated native servers and real headless Google Chrome verified:

- Video-only, audio-only and all-RTP stalls over UDP and direct ICE TCP. Video/audio byte/frame progression stopped selectively; ICE stayed connected/completed. Restoring forwarding resumed frames on the established peers.
- Native loss/latency/jitter impairment expired, clearing the configuration and queues.
- WHEP 307 redirect preserved POST and reached rendered playback. Closing active edge read sessions allowed a fresh playable session while the edge remained available.
- Local coturn UDP and TCP: reference player required relay-only candidates, received more than 30 video frames, and selected candidate stats reported the corresponding TURN URL/relay transport. Proxy mapping produced a prflx local candidate in Chrome; TURN transport evidence and relay-only policy were checked, not just candidateType.
- With public UDP media blackholed, a fresh WHEP attempt selected direct ICE TCP and rendered video.
- Standard HLS remained decodable by FFmpeg. The LL-HLS profile served EXT-X-PART / server-control tags and actual part bytes. Stale-playlist mode held a snapshot across time and the live playlist advanced after restoration. Truncated native media produced aiohttp ClientPayloadError.
- Origin-only outage preserved the edge PID. Restoration restarted the generated source and HLS bridge, returning the edge path to ready.
- A two-round fault/healthy sequence completed with no active fault. Protocol switching still disabled the inactive output and retained origin/source.

These are lab/reference-browser implementation checks, not new Android results. Production VPN behavior, physical device routing, external TURN/TLS, multiple CDNs, token refresh, multi-rendition ABR, and long-running load remain acceptance work. Use PLAYER_ACCEPTANCE_CHECKLIST.md to track them. The Android DTLS recovery-policy fix is still open and Android source was not changed.

Final native regression: healthy WHEP rendered frames; initial ICE blocked both candidate transports while existing video continued; DTLS fingerprint mismatch retained HTTP201 and connected ICE but failed DTLS without frames; fresh healthy attempts recovered. No browser page errors were reported.

Installed dashboard: no horizontal overflow at 390px or uncaught browser errors. The restarted user lab was healthy with WHEP selected, TURN off, no fault/sequence, and was waiting for OBS input; installed-source playback was therefore not marked passed. A separate native generated-source check verified Mac reference playback when PUBLIC_HOST uses 10.0.2.2: local HTTP/ICE alias translation rendered frames while the copied Android URL retained the alias.
