# Testing

Three parts: what the lab itself has verified, what to run against your player, and the standards behind those tests.

## 1. Lab verification (checked 2026-10-09)

Apple Silicon Mac, Python 3.14, MediaMTX 1.21.2, FFmpeg, headless Chrome. This covers the lab, not a production CDN or your player.

```sh
./test.sh           # 40 unit tests, real local HTTP/socket fixtures. Needs only Python.
./test.sh --smoke   # with the lab running (./start.sh): real HLS delivery and fault checks
```

| Area | Verified |
| --- | --- |
| Media paths | Generated and WHIP sources, WHEP and HLS playback, HLS audio bridge, HLS URL input |
| HTTP faults | 401/403/404/410/429/502/503, Retry-After, delay, timeout, 307 POST redirect, scoped PATCH/DELETE, session closure |
| WebRTC faults | ICE unreachable, DTLS mismatch, invalid SDP, transport blackout (UDP and TCP) with recovery on the same connection |
| Media stalls | Video-only, audio-only, all-RTP stalls with ICE still connected |
| Quality | Loss, latency, jitter, bandwidth cap, expiry and queue clearing |
| Infrastructure | Edge outage, origin-only outage, finite fault sequences |
| HLS | Stale playlists, truncated downloads, LL-HLS profile |
| TURN | Local coturn over UDP and TCP, relay-only playback, direct ICE-TCP fallback when UDP is blocked |
| Output switching | Switching WHEP/HLS keeps origin and source running, clears faults, disables the inactive output |
| UI and lifecycle | No page errors, no overflow at 360px, Ctrl+C stops every lab process |

Not yet verified: real OBS publishing, Intel Macs, physical devices, external TURN/TLS, long soak runs, and your own players.

## 2. Player acceptance checklist

Next-run checklist, not a pass report. For each run record: app build, source, protocol, device/network/VPN, fault and duration, start/end times, app events, selected ICE pair, retry timing, freeze interval, first recovered frame, cleanup, and final outcome. A 201 response or a "ready" status alone is not proof of playback.

| ID | Setup / action | Evidence to capture |
| --- | --- | --- |
| MEDIA-01 | WHEP, video-only stall 15 s | Frames freeze, audio continues, ICE stays connected, first recovered frame |
| MEDIA-02 | Audio-only and all-RTP stall, 15 s each | Independent stall detection, no invented signaling error |
| QUALITY-01 | 5% loss, burst 3, 100±50 ms delay; then bandwidth cap | Freezes, live delay, recovery |
| BUDGET-01 | Blackout 5 / 15 / 45 s and around your retry deadline | Retry budget, same-path vs new-session recovery, final outcome |
| REPEAT-01 | 5 rounds of 5 s faulty / 10 s healthy | Retry reset, no overlapping loops, no session growth |
| EXIT-01 | Delay WHEP POST 10 s; exit and re-enter | Old response cannot replace the new attempt, retries cancelled |
| HTTP-01 | 429/503 with Retry-After; 401/403/404/410 | Backoff and classification per policy |
| SESSION-01 | Close active sessions; 410 on lifecycle calls | Fresh attempt recovers, bounded cleanup |
| SETUP-01 | 307 redirect, invalid SDP, ICE/DTLS faults | Negotiation errors handled, fingerprint verification kept |
| SOURCE-01 | Origin outage, then OBS restart | Edge stays up, frames and live position return |
| HLS-01 | Stale playlist, then restore | Stale detection, live edge advances again |
| HLS-02 | Truncated media/init; 404/503 on media | Bounded retry or skip, playback restored |
| LLHLS-01 | LL-HLS with delayed reloads and failed parts | Query handling, preload/delta recovery |
| TURN-01 | Same device: VPN off, VPN on direct, VPN on with TURN | Selected pair and TURN transport, freezes |
| TURN-02 | TURN UDP / TCP / TLS; unreachable server; bad credentials | Direct path not mistaken for relay, fallback and errors |
| HANDOVER-01 | Wi-Fi ↔ mobile, airplane mode, VPN toggle | Path change, rejoin, first frame (needs an endpoint reachable on both networks) |
| CDN-01 | Multiple CDNs, DNS/TLS changes, token refresh | Needs real infrastructure |
| ABR-01 | Multi-rendition HLS, fail high variant | Needs a multi-rendition source |
| SOAK-01 | Repeated faults over a long session | Client memory/threads, server sessions, live delay over time |

## 3. Standards behind the tests

Use 5 / 15 / 45 second outages, then repeated 5 s faulty / 10 s healthy. Numeric recovery deadlines belong in your product policy, not in the standards.

- **WebRTC ([W3C](https://www.w3.org/TR/webrtc/#dom-rtciceconnectionstate)):** ICE `failed` should trigger an ICE restart; `disconnected` is transient. No retry count or stall deadline is prescribed.
- **Consent freshness ([RFC 7675](https://www.rfc-editor.org/rfc/rfc7675.html#section-5.1)):** the sender stops after 30 s without consent. This is a transport rule, not an app recovery timeout.
- **WHEP ([draft-ietf-wish-whep](https://datatracker.ietf.org/doc/draft-ietf-wish-whep/)):** still an Internet-Draft. POST redirects should use 307; 503 and 409 may carry Retry-After; ICE restart is optional.
- **Fallback ([RFC 8835](https://www.rfc-editor.org/rfc/rfc8835.html#section-3.4)):** direct ICE-TCP and TURN over TCP/TLS are different. A working direct TCP path does not validate TURN.
- **HLS ([RFC 8216](https://www.rfc-editor.org/rfc/rfc8216.html#section-6.3.4)):** playlist reload rules motivate stale-playlist tests. LL-HLS needs newer material, and Apple's `mediastreamvalidator` checks conformance, not retry policy.

The lab discards framed traffic, so it does not reproduce TCP retransmission, RST/FIN, or NAT migration.
