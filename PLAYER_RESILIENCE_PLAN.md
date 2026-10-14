# Player resilience standards and next checks

Research date: 2026-10-09. Primary sources only. This note proposes tests; it does not report new player test results.

## Protocol references versus product acceptance

- **WebRTC:** the W3C specification defines ICE and DTLS states and the API. It recommends ICE restart on `failed`; on `disconnected`, it suggests consulting stats before deciding. `disconnected` is transient. These are useful recovery signals, but the spec does not prescribe our app's retry count, retry backoff, stall deadline, or acceptable freeze duration. [W3C WebRTC](https://www.w3.org/TR/webrtc/#dom-rtciceconnectionstate)
- **Consent freshness:** RFC 7675 §5.1 requires a sender to stop transmitting on a candidate pair when no valid consent response has arrived for 30 seconds. Responses arriving after expiry do not restore consent; new ICE credentials via restart or a new session are needed. **This 30-second transport security rule is not a mandatory app recovery timeout.** Start tests at 5/15/45 seconds; judge app behavior against its configured policy, not against the consent timer. [RFC 7675](https://www.rfc-editor.org/rfc/rfc7675.html#section-5.1)
- **WHEP:** currently an active Internet-Draft, `draft-ietf-wish-whep-04`, revision 2026-06-22, not a published RFC. §4.7 requires POST redirect handling and recommends 307 to preserve the request; 301/302 must not be used. It describes 503 and Retry-After. §4.5 recommends ICE restart/trickle, while §4.6.3 says support is optional: do not assert that every WHEP implementation must support restart. Supported restart uses PATCH and updated ICE information, not general SDP renegotiation. §4.3.9 describes a stream-not-yet-published 409 with Retry-After. Align tests with the deployed server/client contract and draft version. [WHEP draft](https://datatracker.ietf.org/doc/draft-ietf-wish-whep/)
- **WebRTC fallback:** RFC 8835 distinguishes direct ICE-TCP from client-to-TURN over TCP/TLS. It requires WebRTC endpoints to support both TURN transports for UDP-blocking networks, and ICE-TCP candidates. A successful direct TCP connection in our lab does **not** validate TURN/TLS or restrictive corporate-network behavior. [RFC 8835 §3.4](https://www.rfc-editor.org/rfc/rfc8835.html#section-3.4)
- **HLS:** RFC 8216 is the published baseline. Its live-playlist reload and media-sequence rules motivate stale-playlist tests. Modern LL-HLS requires newer material: Apple's authoring specification explicitly points to evolving HLS2 and its LL-HLS server profile. The latest IETF-listed HLS2 draft is `draft-pantos-hls-rfc8216bis-22` (2026-05-01), still an Internet-Draft, currently sent to the RFC Editor. [RFC 8216 §6.3.4](https://www.rfc-editor.org/rfc/rfc8216.html#section-6.3.4), [Apple authoring specification](https://developer.apple.com/streaming/hls-authoring-specification-for-apple-devices.html), [HLS2 draft status](https://datatracker.ietf.org/doc/draft-pantos-hls-rfc8216bis/)

I did not find a single universal CDN/player resilience certification checklist in these protocol references. They establish protocol behavior; our deployment and product policy must define resilience acceptance. This is a bounded finding about the reviewed sources, not an exhaustive claim that no such suite exists.

## Small next set, ranked

These are **proposed product tests**, not specification mandates:

1. **Outage duration boundaries:** repeat current media outage at 5, 15, and 45 seconds, plus just below/above our configured recovery budget. Validate short-path recovery and long-outage exhaustion/rejoin behavior separately.
2. **Repeated interruptions:** 5 seconds down / 10 seconds healthy, repeated 5–10 times. Verify retry state resets only when appropriate, one active session, no overlapping recovery loop, and no cumulative delay/resource growth.
3. **Network handover:** Wi-Fi to mobile data and back; airplane mode then reconnect. Real device test, including address/NAT mapping changes. A relay blackout alone does not reproduce these changes.
4. **Degradation rather than full outage:** rate limiting below encoded bitrate, random and burst loss, variable latency/jitter, and packet reordering. Observe adaptation, freeze duration, and return to healthy latency. Needs packet shaping; HTTP delay is not media jitter.
5. **Silent media stall with healthy transport:** stop video or drop RTP/SRTP while preserving STUN consent; test audio continuing with frozen video too. Verify decoded/rendered-frame stall detection rather than relying solely on connection-state failure. Current all-transport discard does not isolate this case.
6. **UDP unavailable:** force UDP failure with a usable alternative. Observe selected candidate pair for direct ICE-TCP; separately configure real TURN TCP/TLS if production supports it. Verify with real device/network restrictions.
7. **HTTP/session lifecycle:** 307 redirect, 429/503 with Retry-After, auth expiry/refresh, unavailable/deleted session, and delayed POST/PATCH response after cancellation or a newer attempt. Ensure old responses cannot replace the current session and exits cancel retries.
8. **Source/edge transitions:** origin publisher disappears while edge stays available, encoder restarts, and edge replacement gives a new session/address. Confirm first decoded frame and acceptable return to live position, not merely HTTP 201 or AudioStarted.

## HLS follow-ups

- Stale 200 playlist and inconsistent cache freshness; track advancement of media sequence and return to live edge.
- Missing/truncated segment or init fragment; verify bounded retry/skip/variant fallback according to player policy.
- Failed high-bitrate variant with healthy lower variant; requires an actual multi-rendition source.
- For production LL-HLS, blocked playlist reload, missing partial segments, preload hints, and delta-update recovery; current fMP4 HLS output alone does not validate LL-HLS.

Apple's `mediastreamvalidator` checks stream/playlist/segment conformance and server delivery. It complements fault-injection tests; it does not certify our Android retry policy. [Apple HLS tools](https://developer.apple.com/documentation/http-live-streaming/using-apple-s-http-live-streaming-hls-tools)

## Acceptance evidence for every test

Capture fault start/end, interruption detection, frames/bytes progression, ICE/DTLS state, retry attempts/backoff, first decoded/rendered frame after restoration, freeze duration, live latency, and final outcome. Correlate server fault proof with client events. Require one recovery owner applying its normal policy, no runaway retries, cleanup after user exit, and a clear exhaustion outcome. Put numerical recovery deadlines in the product policy rather than labeling them WebRTC or CDN standards.

## Lab coverage before the expansion (historical snapshot)

Local implementation reviewed on 2026-10-09: the fault duration accepts 1–600 seconds, one active preset at a time, with HTTP delay/timeout, edge process outage, new-answer ICE/DTLS mutation, and bidirectional all-traffic UDP/TCP media discard. HLS uses hlsVariant=fmp4. STUN/TURN configuration is empty. These are local configuration findings, not protocol claims. See lab/gateway.py and lab/runtime.py.

| Next check | Available now? | Main evidence |
| --- | --- | --- |
| Media outage at 45 and 90 seconds; fault longer than the configured recovery budget | Yes, duration control | Healthy recovery inside the policy window, or documented exhaustion outside it; actual frame recovery |
| 5 seconds down / 10 seconds healthy, five repetitions | Yes, repeated manual application | Correct reset after healthy playback; no overlapping retries or session growth |
| OBS stops publishing, then resumes while the edge remains up | Yes, manually stop/start OBS | Upstream failure classification, healthy reconnection, live position, first recovered frame |
| Exit during delayed POST or outage; re-enter | Yes, player actions plus delay/outage | Retry cancellation, resource cleanup, old answers cannot revive or replace the current player |
| RTP/video-only stop while ICE checks continue | Needs a selective relay fault | Frame-stall detection while the transport state remains healthy; test audio/video separately |
| Loss, jitter, low bandwidth, then restoration | Needs media packet shaping | Freeze/latency measurements and recovery; adaptation only where the source/player supports it |
| Network handover and UDP-blocked fallback | Physical device/reachable endpoint; separate TURN setup | New address/path negotiation and selected candidate pair; direct ICE-TCP is separate from TURN |
| WHEP redirect, expired session/authentication, stale successful response | Needs additional HTTP fixtures or a real test endpoint | Contract-specific handling and normal recovery policy; no blind repeated attempt with expired credentials |

A Mac LAN endpoint is generally unreachable from a phone on mobile data. To test Wi-Fi/cellular handover rather than mere disconnection, use an endpoint reachable from both networks. The current all-media relay does not reproduce NAT migration, TURN, selective RTP stalls, TCP retransmission, or TCP RST/FIN. Single-rendition fMP4 HLS does not test multi-variant ABR, LL-HLS, CDN cache behavior, or multi-CDN steering. Those require representative sources/infrastructure.

Suggested first three runs: 45-second media outage; repeated 5-second outages; exit/re-enter during a delayed WHEP response. Record results independently from the already confirmed Android runs. No extra faults were applied while preparing this research note.

## Priority update — VPN/TURN field case

The user has observed unstable WHEP under a VPN and successful operation after configuring TURN credentials. Promote VPN/TURN connectivity to a priority reproduction check. Compare VPN-off direct playback, VPN-on without a usable relay, and VPN-on with TURN configured; prove relay usage with the selected candidate pair and measure frames, freeze time, and recovery. The report does not establish which TURN transport was used or whether UDP was blocked. Include unreachable/rejected TURN and mid-stream VPN toggling as subsequent checks. This requires a TURN service and a reachable edge endpoint; the current direct UDP/ICE-TCP lab is insufficient.

## Implemented expansion — 2026-10-09

The earlier table is retained as the pre-expansion snapshot. New lab controls now cover selective RTP/audio/video stalls, UDP blackout, bounded packet impairment, additional HTTP codes and Retry-After, 307 POST redirect, invalid SDP, lifecycle-only HTTP faults, active-session closure, origin-only outage, stale successful HLS playlists, truncated downloads, an LL-HLS profile, and finite outage sequences. Native/external TURN configuration and relay-only browser checks add evidence for the VPN reproduction. See docs/REFERENCE.md for exact fixture behavior and VALIDATION.md for implementation verification.

| Area | Available lab check | Remaining acceptance work |
| --- | --- | --- |
| Silent media failures | Selective audio/video/all-RTP controls preserving ICE | Verify Android stall/rebuffer events and the normal recovery policy, including no Error when transport stays healthy |
| Degradation | Loss/burst loss, jitter, latency, UDP extra delay, per-peer bandwidth cap | Measure player freezes/live delay under realistic encoded sources; no multi-variant adaptation assertion |
| UDP / TURN | UDP relay blackout plus separate native TURN UDP/TCP and external TURN/TLS config; selected-pair stats | Physical-device VPN matrix; production trusted TURN/TLS and real client UDP restrictions |
| HTTP / session | Status codes, Retry-After, redirect, invalid SDP, PATCH/DELETE faults, read-session closure | Real token expiry/refresh, stale-attempt cancellation, fallback and cleanup in Android |
| HLS / LL-HLS | Stale 200 playlists, incomplete init/segment/part delivery, LL-HLS profile, reload query preservation | Production caches, multi-rendition ABR, selective variant failure, real CDN steering; Apple HTTPS validation |
| Upstream / repeated outages | Origin-only outage, edge outage, finite repeated sequence | OBS encoder restart, 45+ second consent exhaustion, long soak/resource trend and app retry-policy boundaries |
| Network changes | Manual VPN/network/device scenarios remain in the checklist | Reachable endpoint on both Wi-Fi/mobile; NAT migration and real DNS/TLS failures |

Use 5/15/45-second duration tests, then repeated 5 seconds faulty / 10 seconds healthy. Record fault proof, detection time, retry timing, first recovered video frame, audio progress, freeze duration, cleanup, and final outcome. Automatic short-outage browser recovery is implementation evidence, not a result for the Android app. The Android DTLS change remains a separate pending client fix under the user's lab-only scope.
